#!/usr/bin/env python3
"""
expand_kline_coverage.py — 多源并发扩 K 线覆盖 (腾讯主 + 东财兜底)

背景:
  本地 bulk 只有 238/5343 只 (4.5%) 有 K 线, 导致:
    - kline_sim.py 回测 "无有效收益样本" (出场日超出数据范围)
    - reflect_engine/winrate_matrix 3/5/10 日持有期 n=0 (缺未来数据)
    - 低位池/多指标共振 大量 0 候选
  单靠腾讯 (web.ifzq.gtimg.cn) 会在 ~1750 次请求后被限流 (HTTP 501)。

方案:
  - 双源并发: 腾讯 fqkline(qfq) 为主, 东财 push2his(kline, fqt=1) 兜底
    两源按 code 交替分配 (偶数→腾讯, 奇数→东财), 单个源限流不影响另一源
  - 每个 code 独立 try/except, 失败不阻断
  - 增量: 跳过已有 bulk 里 K 线天数 >= 阈值的 code (避免重拉)
  - 输出: BULK/day_qfq_expand.csv (与 kline_sim.load_klines 兼容)
    行格式: code|date|open|close|high|low|vol  (code 带交易所前缀 sh/sz/bj)

用法:
  python3 expand_kline_coverage.py                    # 全量 universe, 120 天
  python3 expand_kline_coverage.py --limit 2000       # 前 2000 只 (按 liquidity)
  python3 expand_kline_coverage.py --days 60          # 拉近 60 天
  python3 expand_kline_coverage.py --min-days 30      # 已有>=30天才跳过
  python3 expand_kline_coverage.py --workers 8
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from paths import BULK, SNAPSHOT_GLOB

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}
UA_EM = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
         "Referer": "https://quote.eastmoney.com/"}

UNIVERSE = os.path.join(BULK, "universe.csv")
OUT = os.path.join(BULK, "day_qfq_expand.csv")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ─── universe / snapshot / bulk 读取 ──────────────────────────────
def load_universe():
    """[{code:'sh600519', name, market}]"""
    out = []
    if not os.path.exists(UNIVERSE):
        return out
    with open(UNIVERSE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            code = parts[0].strip()
            if not code:
                continue
            out.append({"code": code,
                        "name": parts[1].strip() if len(parts) > 1 else ""})
    return out


def snapshot_liquidity():
    """{bare_code: turnover} — 取最近一份快照的成交额做优先级。"""
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if not snaps:
        return {}
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
    except Exception:
        return {}
    liq = {}
    for sym, rec in d.items():
        bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        liq[bare] = rec.get("turnover") or 0
    return liq


def existing_coverage(threshold_days=0):
    """已有 bulk (day_qfq_*.csv, 不含 expand) 里 K线天数 >= threshold 的 code 集合。"""
    cov = {}
    for p in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
        if os.path.basename(p) in ("day_qfq_expand.csv", "day_qfq_recent.csv"):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                head = line.split("|", 1)[0]
                if head not in cov:
                    cov[head] = 0
                cov[head] += 1
    return {k for k, v in cov.items() if v >= threshold_days}


def existing_expand_codes():
    """expand.csv 已有 code 集合 (增量 append 时去重)。"""
    have = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                have.add(line.split("|", 1)[0].strip())
    return have


# ─── 双源 K 线抓取 ────────────────────────────────────────────────
def fetch_tencent(code, days):
    """腾讯 fqkline qfq。code 带前缀 (sh600519)。返回 rows [[d,o,c,h,l,v],...]"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{days},qfq")
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                  timeout=12).read()
    d = json.loads(body.decode("utf-8"))
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    rows = []
    for r in arr:
        # r = [date, open, close, high, low, volume, ...]
        if len(r) >= 6 and RE_DATE.match(r[0]):
            rows.append([r[0], r[1], r[2], r[3], r[4], r[5]])
    return rows


def fetch_eastmoney(code, days):
    """东财 push2his kline, fqt=1(qfq)。code 带前缀。返回 rows [[d,o,c,h,l,v],...]"""
    market, bare = code[:2], code[2:]
    secid = ("1." if market == "sh" else "0.") + bare
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
           f"&end=20500101&lmt={days}")
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA_EM),
                                  timeout=12).read()
    d = json.loads(body.decode("utf-8", "replace"))
    kl = ((d.get("data") or {}).get("klines")) or []
    rows = []
    for s in kl:
        # s = "date,open,close,high,low,volume,amount,..."
        p = s.split(",")
        if len(p) >= 6 and RE_DATE.match(p[0]):
            rows.append([p[0], p[1], p[2], p[3], p[4], p[5]])
    return rows


def fetch_one(item, days, min_days, have):
    code = item["code"]
    if code in have:
        return (code, [], "skip")
    src = "tencent" if int(code[2:] or 0) % 2 == 0 else "eastmoney"
    try:
        rows = (fetch_tencent if src == "tencent" else fetch_eastmoney)(code, days)
        return (code, rows, src if rows else "empty")
    except Exception:
        # 主源失败 → 另一源兜底
        fb = "eastmoney" if src == "tencent" else "tencent"
        try:
            rows = (fetch_eastmoney if fb == "eastmoney" else fetch_tencent)(code, days)
            return (code, rows, fb + "_fallback" if rows else "empty")
        except Exception as e:
            return (code, [], f"fail:{type(e).__name__}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="最多处理多少 code (0=全部 universe)")
    ap.add_argument("--days", type=int, default=120, help="每只拉多少根日K")
    ap.add_argument("--min-days", type=int, default=60,
                    help="已有 bulk 里 K线>=该天数的 code 跳过")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--by-liquidity", action="store_true",
                    help="按成交额降序处理 (需要最近快照)")
    ap.add_argument("--codes", default="",
                    help="逗号分隔的裸码列表, 只处理这些 code (定向补K线, 忽略 universe/min_days)")
    args = ap.parse_args()

    # 定向模式: 直接抓指定 code (带交易所前缀归一化)
    if args.codes.strip():
        def _pref(c):
            c = c.strip().zfill(6)
            if not c.isdigit():
                return c  # 已带前缀
            if c[0] in "69":
                return f"sh{c}"
            if c[0] in "03":
                return f"sz{c}"
            return f"bj{c}"
        targets = [{"code": _pref(c)} for c in args.codes.split(",") if c.strip()]
        have = set()
        todo = [t for t in targets if t["code"] not in have]
        print(f"[expand] 定向模式: 处理 {len(todo)} 只指定 code", flush=True)
    else:
        uni = load_universe()
        if not uni:
            print(f"[expand] universe 不存在: {UNIVERSE}", file=sys.stderr)
            return 1
        if args.by_liquidity:
            liq = snapshot_liquidity()
            uni.sort(key=lambda x: -liq.get(x["code"][2:], 0))
        have = existing_coverage(args.min_days) | existing_expand_codes()
        todo = [x for x in uni if x["code"] not in have]
        if args.limit:
            todo = todo[: args.limit]
        print(f"[expand] universe={len(uni)} 跳过已覆盖={len(have)} 本轮处理={len(todo)} "
              f"days={args.days} workers={args.workers}", flush=True)

    fresh = open(OUT, "a", encoding="utf-8") if os.path.exists(OUT) else open(OUT, "w", encoding="utf-8")
    if not os.path.exists(OUT):
        fresh.write(f"# expand_kline_coverage {datetime.now(CST):%Y%m%d} days={args.days}\n")

    stats = {"ok": 0, "fail": 0, "empty": 0, "skip": 0, "rows": 0}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, it, args.days, args.min_days, have): it for it in todo}
        for i, fut in enumerate(cf.as_completed(futs)):
            it = futs[fut]
            code, rows, tag = fut.result()
            if tag == "skip":
                stats["skip"] += 1
                continue
            if rows:
                fresh.write("\n".join(f"{code}|{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}"
                                       for r in rows) + "\n")
                stats["rows"] += len(rows)
                stats["ok"] += 1
                if (i + 1) % 100 == 0:
                    fresh.flush()
                    print(f"  [{i+1}/{len(todo)}] ok={stats['ok']} rows={stats['rows']} "
                          f"({time.time()-t0:.0f}s)", flush=True)
            elif tag.startswith("fail"):
                stats["fail"] += 1
            else:
                stats["empty"] += 1
    fresh.close()

    print(f"[expand] done: {stats} 用时 {time.time()-t0:.0f}s")
    print(f"[expand] 新数据已写入 {OUT} (增量, kline_sim 自动读取 day_qfq_*.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
