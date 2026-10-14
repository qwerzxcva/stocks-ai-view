#!/usr/bin/env python3
"""
bulk_collect.py — 大批量历史数据补齐 (多源 + 断点续传 + 三账号分片)

用途: 把「上市至今」的 K 线 / 资金流 / 估值等历史数据一次性补齐,
      由 GitHub Actions 在三仓并行跑 (各自不同出口 IP, 避免单 IP 被限流)。

分片方式: 按 code 哈希取模分片, 每仓跑自己那 1/3:
  A 仓: hash(code) % 3 == 0
  B 仓: hash(code) % 3 == 1
  C 仓: hash(code) % 3 == 2

数据源 (按优先级):
  1. 新浪 getKLineData (datalen=10000, 上市至今全历史, 实测 6020 根)
  2. 腾讯 fqkline (640 根硬上限, 只作增量)
  3. KlineShare 看板 realtime (免 key, 当前快照)

用法:
  python3 bulk_collect.py --kind kline --shard 0 --out data/bulk_kline_0.json
  python3 bulk_collect.py --kind kline --shard 0 --resume     # 断点续传
  python3 bulk_collect.py --kind universe                    # 只拉股票清单
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = "/root/workspace/stocks-data-kline/data/bulk"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}
UA_TX = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
         "Referer": "https://gu.qq.com/"}
UA_KS = {"User-Agent": "Mozilla/5.0 Chrome/120", "Referer": "https://hq.klineshare.cn/"}

SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=10000")


def http(url, headers, timeout=20, retries=2):
    last = "unknown"
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace"), None
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:70])
            if i < retries:
                time.sleep(1.0 * (i + 1))
    return None, last


def prefixed(code):
    c = str(code).strip().zfill(6)
    if c.startswith(("60", "68", "9")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def ks_symbol(code):
    """KlineShare 看板要 600519.SH 风格。"""
    c = str(code).strip().zfill(6)
    if c.startswith(("60", "68", "9")):
        return "%s.SH" % c
    if c.startswith(("4", "8")):
        return "%s.BJ" % c
    return "%s.SZ" % c


# ─── 股票全集 ─────────────────────────────────────────────────
def load_universe():
    """股票清单: 优先本地 bulk 的 code, 其次历史预测快照, 最后内置常见池。"""
    codes = set()
    # 1. 本地 bulk 里已有的 code
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or "|" not in line:
                        continue
                    head = line.split("|", 1)[0]
                    bare = head[2:] if head[:2] in ("sh", "sz", "bj") else head
                    if len(bare) == 6 and bare.isdigit():
                        codes.add(bare)
        except Exception:
            continue
    # 2. 历史预测快照
    pdir = os.path.join(DATA_DIR, "predictions")
    if os.path.isdir(pdir):
        for p in sorted(glob.glob(os.path.join(pdir, "engine_v5_*.json")))[-30:]:
            try:
                d = json.load(open(p, encoding="utf-8"))
                for r in (d.get("top") or []):
                    c = str(r.get("code") or "").replace("sh", "").replace("sz", "")
                    if len(c) == 6 and c.isdigit():
                        codes.add(c)
            except Exception:
                pass
    # 3. valuation 里的全市场
    vp = os.path.join(DATA_DIR, "valuation.json")
    if os.path.exists(vp):
        try:
            d = json.load(open(vp, encoding="utf-8"))
            for k in (d.get("stocks") or d.get("items") or {}):
                c = str(k).replace("sh", "").replace("sz", "")
                if len(c) == 6 and c.isdigit():
                    codes.add(c)
        except Exception:
            pass
    return sorted(codes)


def shard_of(code, n_shards=3):
    """稳定分片: 用 md5 保证跨仓一致 (Python 内置 hash 有随机盐)。"""
    h = int(hashlib.md5(str(code).encode()).hexdigest(), 16)
    return h % n_shards


# ─── 各数据源 ─────────────────────────────────────────────────
def fetch_sina_full(code):
    """新浪全历史日 K (datalen=10000)。返回 [(date,o,c,h,l,v),...]"""
    sym = prefixed(code)
    body, err = http(SINA_KLINE.format(sym=sym), UA)
    if not body:
        return [], err
    try:
        d = json.loads(body)
    except Exception:
        return [], "非 JSON"
    if not isinstance(d, list):
        return [], "空列表"
    rows = []
    for r in d:
        try:
            rows.append([r["day"], r["open"], r["close"], r["high"], r["low"], r["volume"]])
        except Exception:
            continue
    return rows, None


def fetch_ks_realtime(code):
    """KlineShare 看板实时快照 (免 key)。"""
    u = "https://hq.klineshare.cn/api/realtime?symbol=" + urllib.parse.quote(ks_symbol(code))
    body, err = http(u, UA_KS, timeout=15, retries=1)
    if not body:
        return None, err
    try:
        return json.loads(body), None
    except Exception:
        return None, "非 JSON (限流返回 HTML)"


def existing_dates(code):
    """本地 bulk 里该 code 已有的日期集合 (增量用)。"""
    have = set()
    bare = str(code).zfill(6)
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        comma = os.path.basename(p) in ("day_qfq_extra.csv", "day_qfq_recent.csv")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or "|" not in line:
                        continue
                    head, _, rest = line.partition("|")
                    hb = head[2:] if head[:2] in ("sh", "sz", "bj") else head
                    if hb != bare:
                        continue
                    parts = (rest.strip().split(",") if (comma or "," in rest)
                             else rest.strip().split("|"))
                    if parts and len(parts[0]) == 10:
                        have.add(parts[0])
        except Exception:
            continue
    return have


def append_bulk(code, rows):
    """增量写 bulk (保持现有竖线格式, 与 kline_sim/engine_v5 兼容)。"""
    if not rows:
        return 0
    p = os.path.join(BULK, "day_qfq_bulk_%s.csv" % str(code).zfill(6)[:3])
    os.makedirs(BULK, exist_ok=True)
    have = existing_dates(code)
    new = [r for r in rows if r[0] not in have]
    if not new:
        return 0
    sym = prefixed(code)
    with open(p, "a", encoding="utf-8") as f:
        for r in new:
            f.write("%s|%s|%s|%s|%s|%s|%s\n" % (sym, r[0], r[1], r[2], r[3], r[4], r[5]))
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="kline", choices=["kline", "realtime", "universe"])
    ap.add_argument("--shard", type=int, default=0, help="分片号 0/1/2")
    ap.add_argument("--n-shards", type=int, default=3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--codes", default="", help="逗号分隔, 只跑这些 (调试用)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只 (0=全部)")
    ap.add_argument("--sleep", type=float, default=0.4, help="每股间隔秒")
    ap.add_argument("--resume", action="store_true", help="跳过已有数据的股票")
    args = ap.parse_args()

    if args.kind == "universe":
        codes = load_universe()
        out = args.out or os.path.join(DATA_DIR, "universe.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now(CST).isoformat(timespec="seconds"),
                       "n": len(codes), "codes": codes}, f, ensure_ascii=False)
        print("[bulk] 股票全集 %d 只 -> %s" % (len(codes), out))
        return 0

    if args.codes.strip():
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        allc = load_universe()
        codes = [c for c in allc if shard_of(c, args.n_shards) == args.shard]
    if args.limit:
        codes = codes[: args.limit]

    print("[bulk] kind=%s shard=%d/%d 待处理 %d 只" % (
        args.kind, args.shard, args.n_shards, len(codes)), flush=True)

    stats = {"ok": 0, "empty": 0, "fail": 0, "rows": 0, "skipped": 0}
    t0 = time.time()
    for i, code in enumerate(codes):
        if args.resume and args.kind == "kline" and existing_dates(code):
            stats["skipped"] += 1
            continue
        try:
            if args.kind == "kline":
                rows, err = fetch_sina_full(code)
                if rows:
                    n = append_bulk(code, rows)
                    stats["rows"] += n
                    stats["ok"] += 1
                elif err:
                    stats["fail"] += 1
                else:
                    stats["empty"] += 1
            elif args.kind == "realtime":
                d, err = fetch_ks_realtime(code)
                if d:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
        except Exception as e:
            stats["fail"] += 1
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            print("  %d/%d ok=%d rows=%d fail=%d skip=%d (%.0fs)" % (
                i + 1, len(codes), stats["ok"], stats["rows"],
                stats["fail"], stats["skipped"], el), flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    out = args.out or os.path.join(DATA_DIR, "bulk_%s_%d.json" % (args.kind, args.shard))
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(CST).isoformat(timespec="seconds"),
                   "engine": "bulk_collect v1",
                   "kind": args.kind, "shard": args.shard,
                   "n_shards": args.n_shards,
                   "n_codes": len(codes), "stats": stats,
                   "elapsed_s": round(time.time() - t0, 1)}, f, ensure_ascii=False)
    print("[bulk] done: %s (%.0fs)" % (stats, time.time() - t0))
    print("[bulk] -> %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
