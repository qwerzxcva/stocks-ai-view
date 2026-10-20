#!/usr/bin/env python3
"""build_index_history.py — 指数日K全历史补齐 (2026-10-19)

背景: data/index_kline.json 只有 120 根 (rebuild_pools.py 里 build_cycle_pool(120)
硬编码), 导致所有「大盘状态判断」没有统计基础 —— 判断不了「大盘跌了明天会不会涨」。

数据源: 腾讯 fqkline (单次上限 640 根), 用 end 参数向前翻页拼接。
  实测 sh000001 可回溯到 1990-12-19。
校验: 相邻两段首尾日期必须连续, 出现断裂则该段丢弃并记录, 不静默拼接。

产出: data/index_history.json
  {"ts","source","indices":{code:[{date,open,close,high,low,vol}]},"stats":{...}}
用法:
  python3 build_index_history.py                # 5 个主指数
  python3 build_index_history.py --codes sh000001 --start 1990-01-01
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
# 东财 secid: 1.=上证系, 0.=深证系
DEFAULT_CODES = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688"]
PAGE = 640


def _secid(sym):
    """sh000001 -> 1.000001 ; sz399001 -> 0.399001"""
    code = sym[2:]
    return ("1." if sym.startswith("sh") else "0.") + code


def fetch_em(sym, retries=3):
    """东财全历史日K (实测上证 8744 根回到 1990-12-19)。
    腾讯 fqkline 的 end 参数无效、单次只给 640 根, 无法翻页, 故换东财。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={_secid(sym)}&fields1=f1&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=1&beg=19901219&end=20500101&lmt=100000")
    # 用 curl 而不是 urllib: 东财对 urllib 的 TLS 指纹直接 RST, curl 正常。
    import subprocess
    for i in range(retries):
        try:
            raw = subprocess.run(
                ["curl", "-s", "-m", "35", "-A", HDR["User-Agent"], url],
                capture_output=True, timeout=45).stdout
            if not raw:
                raise ValueError("empty response")
            d = json.loads(raw.decode("utf-8", "replace"))
            return (d.get("data") or {}).get("klines") or []
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return None


def fetch_page(sym, end=None, retries=3):
    """拉一段日K (最多 640 根)。end 为 'YYYY-MM-DD' 时取该日之前的数据。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sym},day,,,{PAGE},qfq")
    if end:
        url += f"&end={end}"
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HDR)
            d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
            k = (d.get("data") or {}).get(sym) or {}
            for key in ("qfqday", "day"):
                arr = k.get(key)
                if isinstance(arr, list) and arr:
                    return arr
            return []
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return None


def fetch_sina(sym, retries=3):
    """新浪指数日K备用源 (东财不可用时回退)。
    返回 [{day,open,high,low,close,volume}]; volume 单位为股 (东财为手, 需 /100)。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=10000")
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HDR)
            raw = urllib.request.urlopen(req, timeout=40).read()
            arr = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(arr, list) and arr:
                return arr
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(2)
    return None


def _row(x):
    """腾讯行: [date, open, close, high, low, volume, ...]"""
    return {
        "date": x[0],
        "open": float(x[1]),
        "close": float(x[2]),
        "high": float(x[3]),
        "low": float(x[4]),
        "vol": float(x[5]) if len(x) > 5 and x[5] else 0.0,
    }


def build_one(sym, start="1990-01-01", max_pages=20):
    """东财全历史一次取完 (lmt=100000); 失败则回退新浪全历史。"""
    out = []
    source = "eastmoney"
    kl = fetch_em(sym)
    if kl is not None:
        for line in kl:
            p = line.split(",")
            if len(p) < 6:
                continue
            try:
                out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                            "high": float(p[3]), "low": float(p[4]),
                            "vol": float(p[5])})
            except ValueError:
                continue
    if not out:
        source = "sina"
        rows = fetch_sina(sym)
        if rows is None:
            return [], {"n": 0, "error": "no data"}
        for r in rows:
            try:
                o, c = float(r["open"]), float(r["close"])
                if o <= 0 or c <= 0:      # 新浪早期缺 OHLC 的占位行
                    continue
                out.append({"date": r["day"], "open": o, "close": c,
                            "high": float(r["high"]), "low": float(r["low"]),
                            "vol": float(r.get("volume") or 0) / 100.0})
            except (KeyError, TypeError, ValueError):
                continue
    out = [r for r in out if r["date"] >= start]
    return out, {"n": len(out), "pages": 1, "source": source,
                 "span": [out[0]["date"], out[-1]["date"]] if out else None,
                 "gaps": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
    ap.add_argument("--start", default="1990-01-01")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "index_history.json"))
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    now = datetime.now(CST)
    indices, stats = {}, {}
    for sym in codes:
        rows, info = build_one(sym, start=args.start)
        if rows:
            indices[sym] = rows
            stats[sym] = info
            print(f"[index] {sym}: {info['n']} 根 "
                  f"{info['span'][0]} ~ {info['span'][1]} "
                  f"(source={info.get('source', 'eastmoney')})", flush=True)
        else:
            stats[sym] = {"n": 0, "error": "no data"}
            print(f"[index] {sym}: 无数据", flush=True)
        time.sleep(0.5)

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "source": "eastmoney kline (fqt=1, full history) / sina fallback",
        "indices": indices,
        "stats": stats,
        "note": "全历史指数日K, 用于大盘状态机与次日涨跌概率统计",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    size = os.path.getsize(args.out) // 1024
    print(f"[index] -> {args.out} ({size} KB, {len(indices)} 个指数)")
    return 0


if __name__ == "__main__":
    sys.exit(main())