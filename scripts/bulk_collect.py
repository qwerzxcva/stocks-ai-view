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


def fetch_em_daily_raw(code):
    """东财原始价 (不复权 fqt=0) 全历史。

    2026-10-17 净化链路兜底: 新浪挂时用它。腾讯 qfq 有污染 (九阳 2009-04-30
    实证 -49.8% 假跳空), 只在两路都断时由调用方决定。
    返回 [(date,o,c,h,l,v),...]
    """
    mkt = "1" if code[0] in "69" else "0"
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s.%s&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=0&beg=0&end=20500101&ut=7eea3edcaed734bea9cbfc24409ed989"
           % (mkt, code))
    try:
        d = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=20).read().decode())
        klines = ((d.get("data") or {}).get("klines")) or []
        rows = []
        for line in klines:
            p = line.split(",")
            if len(p) >= 6:
                rows.append([p[0], float(p[2]), float(p[3]),
                             float(p[4]), float(p[5]), float(p[6])])
        return rows, None
    except Exception as e:
        return [], str(e)


def fetch_tencent_qfq(code):
    """腾讯 qfq 全史 (640 根硬上限) —— 仅作最后兜底, 已知有污染。"""
    sym = prefixed(code)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,,,640,qfq")
    try:
        d = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=20).read().decode())
        node = (d.get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        rows = []
        for r in arr:
            if len(r) >= 6:
                rows.append([r[0], float(r[1]), float(r[2]),
                             float(r[3]), float(r[4]), float(r[5])])
        return rows, None
    except Exception as e:
        return [], str(e)


def fetch_tencent_kline(code):
    """腾讯 fqkline day 全史 (通常 640+ 根, 多为上市起完整)。

    第三兜底: 新浪/东财都挂时用。腾讯自身有污染 (九阳 2009-04-30 实证 -49.8%
    假跳空), 投票众数时其价值低于新浪/东财, 但三源里至少它有自己的数据。
    """
    sym = prefixed(code)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,,,1500,qfq")
    try:
        d = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=20).read().decode())
        node = (d.get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        rows = []
        for r in arr:
            if len(r) >= 6:
                rows.append([r[0], float(r[1]), float(r[2]),
                             float(r[3]), float(r[4]), float(r[5])])
        return rows, None
    except Exception as e:
        return [], str(e)


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
    # 分片命名与既有 bulk 保持一致 (day_qfq_NN.csv, NN = code 前 2 位),
    # 否则会分裂成两套分片, 消费方 glob day_qfq_*.csv 时虽能读到,
    # 但同一 code 的数据可能跨两个文件, 去重要靠 existing_dates() 逐行扫,
    # 代价随文件数线性增长。
    p = os.path.join(BULK, "day_qfq_%s.csv" % str(code).zfill(6)[:2])
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


def rewrite_bulk(code, rows):
    """整股重写: 从分片文件剔除该股所有旧行, 写入全历史 (净化口径单一)。

    2026-10-17 数据净化: existing append 模式让污染段永久残留在 bulk 里
    (603866 实测比值漂移 0.94→1.00), 只有整股重写才能保证口径单一。

    防掉点: 同批次两次拉取做 close 序列比对, 不一致(截断/掉点)则拒写,
    保留旧数据等下一轮——单次返回的污染段比陈旧数据更糟 (九阳 2023-06-19
    单点 -3% 若写入即固化)。
    """
    if not rows:
        return 0
    if len(rows) >= 200:
        rows2, _ = fetch_sina_full(code)
        if len(rows2) >= 200:
            def _f(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0
            s1 = [_f(r[2]) for r in rows[-120:]]
            s2 = [_f(r[2]) for r in rows2[-120:]]
            if len(s1) == len(s2) and any(abs(a - b) / max(1e-9, abs(a)) > 0.004
                                          for a, b in zip(s1, s2)):
                print("[rewrite] %s 双拉不一致 (>0.4%%), 拒写防污染" % code,
                      file=sys.stderr)
                return 0
    p = os.path.join(BULK, "day_qfq_%s.csv" % str(code).zfill(6)[:2])
    os.makedirs(BULK, exist_ok=True)
    bare = str(code).zfill(6)
    kept = []
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    kept.append(line)
                    continue
                head = line.split("|", 1)[0]
                hb = head[2:] if head[:2] in ("sh", "sz", "bj") else head
                if hb != bare:
                    kept.append(line)
    sym = prefixed(code)
    new = sorted(rows, key=lambda r: r[0])
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(kept)
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
    ap.add_argument("--rewrite", action="store_true",
                    help="重写模式: 丢弃该股本地历史, 用新浪全历史重拉整段"
                         " (2026-10-17 数据净化用: 现有 bulk 混入qfq污染,"
                         "  append 无法剔除, 必须整股重写)")
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
        if args.resume and not args.rewrite and args.kind == "kline" and existing_dates(code):
            stats["skipped"] += 1
            continue
        try:
            if args.kind == "kline":
                # 净化链路: 新浪全史(纯raw) → 东财原始价 → 腾讯qfq(已知污染,仅兜最底)
                rows, err = fetch_sina_full(code)
                if not rows:
                    rows, err2 = fetch_em_daily_raw(code)
                    if rows:
                        err = None
                    else:
                        rows, err3 = fetch_tencent_qfq(code)
                        if rows:
                            err = None
                        else:
                            err = err or err2 or err3
                if args.rewrite:
                    if rows:
                        n = rewrite_bulk(code, rows)
                        stats["rows"] += n
                        stats["ok"] += 1
                    elif err:
                        stats["fail"] += 1
                    else:
                        stats["empty"] += 1
                    continue
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
