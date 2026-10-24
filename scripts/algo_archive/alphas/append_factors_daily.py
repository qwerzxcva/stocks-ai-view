# append_factors_daily.py — 每日因子截面增量入 factors_wide (16:15 cron, EOD回补后)
# 只算核心可增量因子(momentum/vol/position 族), 保证 LGBM 在线推理特征最新
import duckdb, datetime
con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
con.execute("SET memory_limit='3GB'")
con.execute("SET temp_directory='/tmp/ducktmp'")
last = con.execute("SELECT max(date) FROM factors_wide").fetchone()[0]
today = datetime.date.today().isoformat()
if str(last) >= today:
    print('up to date', last); raise SystemExit
# 增量: last+1 到 today 的因子截面 (复用 factors_wide 生成口径的核心子集)
try:
    con.execute("ATTACH '/home/agentuser/stockdb/t1/qfq_all.duckdb' AS q (READ_ONLY)")
    con.execute(f"""
      INSERT INTO factors_wide (symbol, date, momentum_5d, momentum_10d, momentum_20d, momentum_60d,
        vol_20d, close_position, gap_return, intraday_return, ret_1d)
      SELECT symbol, d,
        c/lag5 - 1, c/lag10 - 1, c/lag20 - 1, c/lag60 - 1,
        stddev, close_position, gap, intraday, ret1d
      FROM (
        SELECT sym AS symbol, d, d, c, o,
          lag(c,1) OVER w AS lag1, lag(c,5) OVER w AS lag5, lag(c,10) OVER w AS lag10,
          lag(c,20) OVER w AS lag20, lag(c,60) OVER w AS lag60,
          stddev(c) OVER (PARTITION BY sym ORDER BY d ROWS 19 PRECEDING) AS stddev,
          (c - MIN(c) OVER (PARTITION BY sym ORDER BY d ROWS 59 PRECEDING)) /
            NULLIF(MAX(c) OVER (PARTITION BY sym ORDER BY d ROWS 59 PRECEDING) - MIN(c) OVER (PARTITION BY sym ORDER BY d ROWS 59 PRECEDING), 0) AS close_position,
          o/lag1 - 1 AS gap, (c-o)/NULLIF(o,0) AS intraday, c/lag1 - 1 AS ret1d
        FROM q.qfq WINDOW w AS (PARTITION BY sym ORDER BY d)
      )
      WHERE CAST(d AS DATE) > DATE '{last}' AND CAST(d AS DATE) <= DATE '{today}' AND lag60 > 0
    """)
    con.execute("CHECKPOINT")
    print('appended factors', last, '->', today)
except Exception as e:
    print('ERR', str(e)[:200])
con.close()
