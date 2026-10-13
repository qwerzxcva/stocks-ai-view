#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/minute1_collect.py ═══
# 上游 commit: 2cb9ca9e980e · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""minute1_collect.py — 全市场1分钟K采集 → wh/minute1 parquet(按月分区)
盘中模式(增量): --live    datalen=30, 只补今天缺的hhmm, */2 cron
回补模式:      --backfill [days=5]  datalen=1023, 最近N交易日全量
线程12+限速, 去重按(symbol,date,hhmm)覆盖
"""
import json, os, re, sys, time, datetime, urllib.request, threading, queue, glob

HOME = os.path.expanduser("~")
sys.path.insert(0, f"{HOME}/stockdb/scripts")
import data_warehouse as wh
import pyarrow.parquet as pq
import pyarrow as pa

HDR = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
import duckdb

def symbols():
    con = duckdb.connect(f"{HOME}/stockdb/stock.duckdb", read_only=True)
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily ORDER BY symbol").fetchall()]
    con.close()
    return syms

def fetch_m1(sym6, datalen=30):
    u = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
         f"CN_MarketDataService.getKLineData?symbol={sym6}&scale=1&ma=no&datalen={datalen}")
    req = urllib.request.Request(u, headers=HDR)
    b = urllib.request.urlopen(req, timeout=8).read().decode()
    return json.loads(b[b.index("=(") + 2:b.rindex(")")])

def merge_rows(rows, ds="minute1"):
    """rows: [(symbol,date,hhmm,o,c,h,l,vol,amt)] 按月分区merge去重"""
    cols = wh.DATASETS[ds]
    by_month = {}
    for r in rows:
        key = r[1][:7].replace("-", "")  # YYYYMM
        by_month.setdefault(key, []).append(r)
    for month, rr in by_month.items():
        path = f"{wh.WH}/{ds}/{month}.parquet"
        old = []
        if os.path.exists(path):
            t = pq.read_table(path)
            old = [dict(zip(t.column_names, [c.as_py() for c in col]))
                   for col in zip(*[t.column(c) for c in t.column_names])]
        # 老数据dict化→key去重
        seen = {(x[0], x[1], x[2]) for x in rr}
        merged = [tuple(x[c] for c in cols) for x in old if (x["symbol"], x["date"], x["hhmm"]) not in seen]
        merged += rr
        merged.sort(key=lambda x: (x[0], x[1], x[2]))
        wh.write_partition(ds, month, merged, merge_on=(0, 1, 2))

def run(todo, datalen):
    q = queue.Queue()
    for s in todo: q.put(s)
    results, lock = [], threading.Lock()
    fails = [0]
    def worker():
        while True:
            try: sym = q.get_nowait()
            except queue.Empty: return
            try:
                bars = fetch_m1(sym, datalen)
                rows = [(sym, b["day"][:10], b["day"][11:16],
                         float(b["open"]), float(b["close"]), float(b["high"]), float(b["low"]),
                         float(b["volume"]), float(b.get("amount") or 0)) for b in bars]
                with lock:
                    results.extend(rows)
            except Exception:
                fails[0] += 1
            time.sleep(0.08)
    ts = [threading.Thread(target=worker, daemon=True) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return results, fails[0]

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--live"
    syms = symbols()
    t0 = time.time()
    if mode == "--tier1" or mode == "--tier2":
        # 分层采样: 只采注意力T1(1分)/T2(5分)票, 省API省时间
        try:
            tj = json.load(open("/home/agentuser/stockdb/data/sampler_tiers.json"))
            tier = tj["t1_1min"] if mode == "--tier1" else tj["t2_5min"]
            syms = [x["symbol"] for x in tier]
        except Exception:
            syms = syms[:100]
        rows, fails = run(syms, 5)
        today = datetime.date.today().isoformat()
        rows = [r for r in rows if r[1] == today]
        merge_rows(rows)
        print(f"{mode}: {len(rows)}根 {fails}fail {time.time()-t0:.0f}s")
    elif mode == "--live":
        rows, fails = run(syms, 30)
        today = datetime.date.today().isoformat()
        rows = [r for r in rows if r[1] == today]
        merge_rows(rows)
        print(f"live: {len(rows)}根 {fails}fail {time.time()-t0:.0f}s")
    else:
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        rows, fails = run(syms, 1023)
        # 只保留最近days个交易日
        dates = sorted({r[1] for r in rows}, reverse=True)[:days]
        rows = [r for r in rows if r[1] in dates]
        merge_rows(rows)
        print(f"backfill{days}d: {len(rows)}根 {len(dates)}日 {fails}fail {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
