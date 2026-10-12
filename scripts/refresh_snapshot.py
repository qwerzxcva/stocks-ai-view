#!/usr/bin/env python3
"""
refresh_snapshot.py — 用腾讯接口生成当日全市场快照
替代东财 scan_market.py（东财在容器里不可达，但腾讯 qt.gtimg.cn 可用）

输出 all_YYYY-MM-DD.txt，字段与东财格式兼容：
  code|name|price|chg%|amount|turnover%|PE|volratio|mktcap|floatcap|PB|main_net|main_ratio%|superlarge|super_ratio%|industry
说明: amount/PE/main_net/industry 等字段腾讯不直接提供，写0占位
      价格/涨跌幅/换手率/vol_ratio/mktcap 等核心字段是准确的
"""
import os, sys, json, re, time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOLS_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")

UNIVERSE_FILE = "/root/workspace/stocks-data-kline/data/bulk/universe.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}


def load_universe():
    """Load codes from universe.csv. Returns list of tencent-format codes (sh000001, sz000001)."""
    if not os.path.exists(UNIVERSE_FILE):
        print(f"[refresh] universe.csv not found: {UNIVERSE_FILE}", file=sys.stderr)
        return []
    codes = []
    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if parts and parts[0]:
                codes.append(parts[0].strip())
    print(f"[refresh] loaded {len(codes)} codes from universe.csv")
    return codes


def batch_fetch(codes, batch_size=60):
    """Fetch stock quotes from Tencent in batches."""
    records = {}
    total = len(codes)
    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        # Filter out any non-alphanumeric codes
        clean_batch = [c for c in batch if re.match(r'^[a-zA-Z0-9]+$', c)]
        if not clean_batch:
            continue
        url = f"https://qt.gtimg.cn/q={','.join(clean_batch)}"
        for attempt in range(3):
            try:
                body = urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=15
                ).read()
                text = body.decode("gbk", "replace")
                for m in re.finditer(r'v_(\w+)="([^"]+)"', text):
                    code = m.group(1)
                    payload = m.group(2).split("~")
                    if len(payload) < 40:
                        continue
                    try:
                        price = float(payload[3] or 0)
                        prev_close = float(payload[4] or 0)
                        chg_pct = float(payload[32]) if len(payload) > 32 else 0
                        # 实测映射 (茅台/比亚迪/平安银行交叉验证): p[38]换手 p[39]PE p[44]总市值亿 p[45]流通市值亿 p[46]PB
                        turnover = float(payload[38]) if len(payload) > 38 else 0
                        pe = float(payload[39]) if len(payload) > 39 else 0
                        mktcap_yi = float(payload[44]) if len(payload) > 44 else 0
                        mktcap = mktcap_yi * 1e8  # 亿元→元
                        floatcap_yi = float(payload[45]) if len(payload) > 45 else 0
                        floatcap = floatcap_yi * 1e8
                        pb = float(payload[46]) if len(payload) > 46 else 0
                        vol_ratio = 1.0  # 量比无独立字段, 默认 1.0 (避免 v5 软惩罚误杀)
                        name = payload[1]
                        records[code] = {
                            "name": name,
                            "price": price,
                            "chg_pct": chg_pct,
                            "turnover": turnover,
                            "vol_ratio": vol_ratio,
                            "mktcap": mktcap,
                            "floatcap": floatcap,
                            "pe": pe,
                            "pb": pb,
                        }
                    except (ValueError, IndexError):
                        continue
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(1)
                else:
                    print(f"[refresh] batch {i//batch_size+1} failed: {e}", file=sys.stderr)
        if (i // batch_size) % 10 == 0:
            print(f"  fetched {min(i+batch_size, total)}/{total} ({len(records)} records)", flush=True)
    return records


def write_snapshot(date_str, records):
    """Write snapshot in scan_market.py compatible format."""
    os.makedirs(POOLS_DIR, exist_ok=True)
    out_path = os.path.join(POOLS_DIR, f"all_{date_str}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# full market {date_str} (tencent refresh) total={len(records)}\n")
        f.write("# code|name|price|chg%|amount|turnover%|PE|volratio|mktcap|floatcap|PB|main_net|main_ratio%|superlarge|super_ratio%|industry\n")
        for code, r in records.items():
            # code is already prefixed (sh000001, sz002528, etc.)
            # write bare code (without prefix) to match existing format
            bare = code.replace("sh", "").replace("sz", "").replace("bj", "")
            f.write("|".join([
                bare,
                r["name"],
                str(round(r["price"], 2)),
                str(round(r["chg_pct"], 2)),
                "0",              # amount
                str(round(r["turnover"], 2)),
                str(round(r.get("pe", 0), 2)),   # PE
                str(round(r["vol_ratio"], 2)),
                str(round(r["mktcap"], 0)),      # mktcap (元)
                "0",              # floatcap
                str(round(r.get("pb", 0), 2)),   # PB
                "0",              # main_net
                "0",              # main_ratio
                "0",              # superlarge
                "0",              # super_ratio
                "",               # industry
            ]) + "\n")
    print(f"[refresh] wrote {len(records)} stocks -> {out_path}")
    return out_path


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(CST).strftime("%Y-%m-%d")
    print(f"=== refresh_snapshot {date_str} ===", flush=True)
    codes = load_universe()
    if not codes:
        print("ERROR: no codes", file=sys.stderr)
        sys.exit(1)
    records = batch_fetch(codes)
    if not records:
        print("ERROR: no records fetched", file=sys.stderr)
        sys.exit(1)
    path = write_snapshot(date_str, records)
    print(f"DONE: {len(records)} stocks in {path}", flush=True)
    # Print index data
    for c in ["sh000001", "sz399001", "sh000300", "sz399006"]:
        if c in records:
            r = records[c]
            print(f"  {c}: {r['price']} ({r['chg_pct']:+.2f}%)")


if __name__ == "__main__":
    main()
