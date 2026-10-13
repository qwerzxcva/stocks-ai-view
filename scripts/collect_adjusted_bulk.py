#!/usr/bin/env python3
"""
collect_adjusted_bulk.py — 全市场复权批量采集 (hfq 用腾讯, qfq 用新浪)

数据源策略 (2026-10-13 实测):
  - hfq 后复权: 腾讯 fqkline (hfqday) — 零负价, 全市场可用
  - qfq 前复权: 新浪 vip getKLineData — 腾讯 qfq 有负价 bug (茅台 3531/6019 根 <=0),
                新浪零负价且 datalen=6000 可拿全史
  - 指数:      腾讯 kline/kline (无复权概念)

代码清单来源 (本地, 零额度):
  - data/market/snapshot_YYYY-MM-DD.json (key = sh/sz 代码)
  - stocks-data-kline/data/bulk/day_qfq_*.csv (已有的 qfq 日线, 55 分片)

产出:
  data/adjusted/bulk/{code}_qfq.json   前复权 (新浪源)
  data/adjusted/bulk/{code}_hfq.json   后复权 (腾讯源)
  data/adjusted/bulk/_progress.json    进度 (断点续采)

用法:
  python3 collect_adjusted_bulk.py --all                 # 全市场
  python3 collect_adjusted_bulk.py --all --limit 200     # 先采 200 只试跑
  python3 collect_adjusted_bulk.py --codes 600519,600036 # 指定
  python3 collect_adjusted_bulk.py --etf-only            # 只采 ETF
  python3 collect_adjusted_bulk.py --resume              # 断点续采 (读 _progress.json)
"""
import argparse
import csv
import glob
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

HERE = os.path.dirname(os.path.abspath(__file__))
AI_VIEW = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(AI_VIEW, "data", "adjusted", "bulk")
SNAPSHOT_GLOB = os.path.join(os.path.dirname(AI_VIEW), "stocks-data-live",
                             "data", "market", "snapshot_*.json")
BULK_CSV_GLOB = os.path.join(os.path.dirname(AI_VIEW), "stocks-data-kline",
                             "data", "bulk", "day_qfq_*.csv")

ETF_PREFIX = ("51", "56", "58", "159", "511", "513", "515", "518",
              "520", "522", "525", "528", "558", "562", "563")

# 限流 (与 rate_limit.py 的默认值一致)
QPS_TENCENT = 20.0     # 实测腾讯 50/min 安全
QPS_SINA = 10.0        # 新浪 10/min


def _sleep_qps(qps):
    time.sleep(1.0 / qps if qps > 0 else 0.1)


def _tk(code):
    code = str(code).strip()
    if code.startswith(("sh", "sz")):
        return code
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _get(url, retries=3, timeout=25, referer="https://gu.qq.com/"):
    h = dict(UA)
    h["Referer"] = referer
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                return {"__error__": f"{type(e).__name__} {e}"}
            time.sleep(1.0 + i)
    return {"__error__": "unknown"}


# ─── 代码清单 ─────────────────────────────────────────────────
def load_universe():
    """从本地快照 + bulk CSV 汇总全市场代码。返回 {code: name}"""
    out = {}
    # 1 快照 (dict, key=sh/sz)
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    for p in snaps[-1:]:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for tk, v in d.items():
                    if isinstance(v, dict):
                        nm = v.get("name") or v.get("n") or ""
                    elif isinstance(v, list) and v:
                        nm = str(v[0])
                    else:
                        nm = ""
                    code = tk[2:] if tk[:2] in ("sh", "sz") else tk
                    out[code] = nm
        except Exception as e:
            print(f"[warn] 快照解析失败 {p}: {e}", file=sys.stderr)
    # 2 bulk CSV (已有的 qfq 日线)
    for p in glob.glob(BULK_CSV_GLOB):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#"):
                        # 注释行: "# sz000610"
                        c = line.strip().lstrip("#").strip()
                        if len(c) == 8 and c[:2] in ("sh", "sz"):
                            out.setdefault(c[2:], "")
                        continue
                    c = line.split("|")[0]
                    if len(c) == 8 and c[:2] in ("sh", "sz"):
                        out.setdefault(c[2:], "")
        except Exception:
            continue
    return out


# ─── 采集 ────────────────────────────────────────────────────
def fetch_hfq_tencent(code, max_pages=12):
    """腾讯后复权 (hfqday)。hfq 偶发返回空 → 重试 3 次, 递增 sleep。

    2026-10-13 实测: 并发 6 时空置率高 (000008/000012/000017 连续空),
    疑为按 IP 的动态风控; 重试 3 次 + sleep 2/4/8s 可救回大部分。
    """
    for attempt in range(3):
        tk = _tk(code)
        allr, end, guard = {}, "", 0
        while guard < max_pages:
            guard += 1
            d = _get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     f"?param={tk},day,,{end},800,hfq")
            node = (d.get("data") or {}).get(tk) or {}
            arr = node.get("hfqday") or []
            if not arr:
                break
            new = 0
            for r in arr:
                if len(r) >= 6 and r[0] not in allr:
                    allr[r[0]] = [r[0], float(r[1]), float(r[2]),
                                  float(r[3]), float(r[4]), float(r[5])]
                    new += 1
            if new == 0 or len(arr) < 800:
                break
            end = arr[0][0]      # 升序, 首行最早
            time.sleep(0.15)
        if allr:
            return [allr[k] for k in sorted(allr)]
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return []


def fetch_qfq_sina(code, datalen=6000):
    """新浪前复权 (getKLineData, datalen 一次拿全史)。"""
    tk = _tk(code)
    d = _get(f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"CN_MarketData.getKLineData?symbol={tk}&scale=240&ma=5&datalen={datalen}",
             referer="https://finance.sina.com.cn/")
    if isinstance(d, dict):
        return []      # 出错
    if not isinstance(d, list):
        return []
    return [[r["day"], float(r["open"]), float(r["close"]), float(r["high"]),
             float(r["low"]), float(r["volume"])] for r in d]


def validate(bars):
    if not bars:
        return False, "空"
    neg = sum(1 for b in bars if b[1] <= 0 or b[2] <= 0)
    if neg:
        return False, f"{neg}/{len(bars)} 根负价"
    bad = sum(1 for b in bars if b[3] < b[4])
    if bad:
        return False, f"{bad} 根 high<low"
    return True, "ok"


def save(code, adj, bars, source, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, f"{code}_{adj}.json")
    payload = {
        "timestamp": datetime.now(CST).isoformat(timespec="seconds"),
        "source": source, "code": code, "tk_code": _tk(code), "adj": adj,
        "asset_type": "etf" if str(code).startswith(ETF_PREFIX) else "stock",
        "n": len(bars),
        "span": [bars[0][0], bars[-1][0]] if bars else None,
        "bars": bars,
    }
    # 紧凑格式省空间
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--codes", help="逗号分隔")
    ap.add_argument("--etf-only", action="store_true")
    ap.add_argument("--stock-only", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--adj", default="both", choices=["qfq", "hfq", "both"])
    ap.add_argument("--resume", action="store_true", help="跳过已采的")
    ap.add_argument("--workers-report", type=int, default=50,
                    help="每 N 只报告一次进度")
    ap.add_argument("--threads", type=int, default=4,
                    help="并发线程数 (默认 4; 每个线程独立 sleep, 总 QPS = threads/interval)")
    args = ap.parse_args()

    uni = load_universe()
    print(f"[universe] 本地代码清单 {len(uni)} 只")
    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.all or args.etf_only or args.stock_only:
        for c in sorted(uni):
            is_etf = str(c).startswith(ETF_PREFIX)
            if args.etf_only and not is_etf:
                continue
            if args.stock_only and is_etf:
                continue
            codes.append(c)
    if not codes:
        ap.print_help()
        return 1

    # 断点续采
    done = set()
    prog_p = os.path.join(args.out_dir, "_progress.json")
    if args.resume and os.path.exists(prog_p):
        with open(prog_p, encoding="utf-8") as f:
            prog = json.load(f)
        done = set(prog.get("done") or [])
        print(f"[resume] 已完成 {len(done)} 只, 跳过")

    if args.limit:
        codes = codes[:args.limit]

    os.makedirs(args.out_dir, exist_ok=True)
    adjs = ["qfq", "hfq"] if args.adj == "both" else [args.adj]
    pending = [c for c in codes if f"{c}:{','.join(adjs)}" not in done]
    print(f"[plan] 待采 {len(pending)} 只 × adj={adjs} × threads={args.threads}")

    ok_n = fail_n = 0
    fails = []
    lock = threading.Lock()
    counter = {"i": 0, "ok": 0, "fail": 0}
    t0 = time.time()

    def worker(code):
        nonlocal fails
        local_fail = []
        local_ok = 0
        for adj in adjs:
            try:
                if adj == "hfq":
                    bars = fetch_hfq_tencent(code)
                    src = "tencent_fqkline_hfq"
                else:
                    bars = fetch_qfq_sina(code)
                    src = "sina_vip_getKLineData"
                ok_flag, msg = validate(bars)
                if not ok_flag:
                    local_fail.append((code, adj, msg))
                    continue
                save(code, adj, bars, src, args.out_dir)
                local_ok += 1
            except Exception as e:
                local_fail.append((code, adj, f"{type(e).__name__}"))
            time.sleep(0.1)
        with lock:
            counter["i"] += 1
            counter["ok"] += local_ok
            counter["fail"] += len(local_fail)
            fails.extend(local_fail)
            i = counter["i"]
            if i % args.workers_report == 0 or i == len(pending):
                el = time.time() - t0
                rate = i / el * 60 if el > 0 else 0
                eta = (len(pending) - i) / (i / el) / 60 if el > 0 and i else 0
                print(f"  [{i}/{len(pending)}] ok={counter['ok']} "
                      f"fail={counter['fail']} {el:.0f}s ({rate:.0f} 只/min, "
                      f"ETA {eta:.0f}min)")
                with open(prog_p, "w", encoding="utf-8") as f:
                    json.dump({"done": sorted(done), "last_update":
                               datetime.now(CST).isoformat(timespec="seconds"),
                               "ok": counter["ok"], "fail": counter["fail"]}, f)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        list(ex.map(worker, pending))

    ok_n, fail_n = counter["ok"], counter["fail"]
    with open(prog_p, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done), "last_update":
                   datetime.now(CST).isoformat(timespec="seconds"),
                   "ok": ok_n, "fail": fail_n, "fails": fails[:50]}, f)
    print(f"\n[done] ok={ok_n} fail={fail_n} 耗时 {time.time()-t0:.0f}s")
    if fails:
        print(f"失败样例: {fails[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
