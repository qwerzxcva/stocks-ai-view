#!/usr/bin/env python3
"""增量回补: 2026 内断档票的日线 → 临时 duckdb, 上传后合并"""
import duckdb, json, urllib.request, os, sys, time, re
T1 = "/home/agentuser/stockdb/t1"
def fetch_kline(sym, count=400):
    pref = sym[:2]
    code = sym[2:]
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={pref}{code},day,,,{count},qfq"
        d = json.loads(urllib.request.urlopen(url, timeout=15).read().decode())
        data = d['data'][f"{pref}{code}"]
        k = data.get('qfqday') or data.get('day')
        return k or []
    except Exception:
        return []
def main(start=0, end=10**9):
    con = duckdb.connect()
    con.execute(f"attach '{T1}/qfq_all.duckdb' as t1 (read_only)")
    syms = [r[0] for r in con.execute("""
        select sym from (select sym, max(d) last_d from t1.qfq group by sym)
        where cast(last_d as date) >= date '2026-01-01' and cast(last_d as date) < date '2026-10-13'
        order by sym""").fetchall()]
    todo = syms[start:end]
    print(f"[backfill] {len(syms)} stale-in-2026, shard [{start}:{end}]={len(todo)}", flush=True)
    out = duckdb.connect(f"{T1}/backfill_2026.duckdb")
    out.execute("create table if not exists qfq(sym varchar, d date, o double, c double, h double, l double, v double)")
    ok = fail = 0
    for i, s in enumerate(todo):
        k = fetch_kline(s)
        if not k:
            fail += 1
        else:
            rows = [(s, r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in k if r[0] >= '2026-01-01']
            if rows:
                out.executemany("insert into qfq values (?,?,?,?,?,?,?)", rows)
                ok += 1
            else:
                fail += 1
        if (i+1) % 100 == 0:
            print(f"[prog] {i+1}/{len(todo)} ok={ok} fail={fail}", flush=True)
            time.sleep(1)
    out.execute("checkpoint"); out.close()
    print(f"[done] ok={ok} fail={fail}", flush=True)
if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv)>1 else 0, int(sys.argv[2]) if len(sys.argv)>2 else 10**9)
