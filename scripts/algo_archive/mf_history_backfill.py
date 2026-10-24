# mf_history_backfill.py — 主力资金流历史回补 (新浪逐票60日, 边跑边存 duckdb)
import sys, time, json
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
import duckdb
from source_router import fundflow_sina

con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
con.execute("SET memory_limit='2GB'")
con.execute("""CREATE TABLE IF NOT EXISTS mf_daily (
    symbol VARCHAR, d DATE, net DOUBLE, ratio DOUBLE, turnover DOUBLE,
    PRIMARY KEY (symbol, d))""")
syms = con.execute("SELECT DISTINCT symbol FROM mf_factors LIMIT 500").fetchall()
syms = [r[0] for r in syms]
print('回补', len(syms), '票 x 60日资金流', flush=True)
ok = fail = 0
for i, sym in enumerate(syms):
    try:
        rows = fundflow_sina(sym, 60)
        for r in rows:
            con.execute("INSERT OR REPLACE INTO mf_daily VALUES (?,?,?,?,?)",
                        (sym, r['date'], r['net'], r['ratio'], r['turnover']))
        ok += 1
    except Exception:
        fail += 1
    if i % 50 == 0:
        con.execute("CHECKPOINT")
        print(i, 'ok', ok, 'fail', fail, flush=True)
    time.sleep(0.3)
con.execute("CHECKPOINT")
print('DONE ok', ok, 'fail', fail)
