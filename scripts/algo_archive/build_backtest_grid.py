#!/usr/bin/env python3
"""规则网格回测: 量比4/6/8×涨幅区间×位置阈值 → 全历史逐样本结果 parquet"""
import duckdb, pandas as pd, os

con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
df = con.execute("""
WITH r AS (
  SELECT symbol, date, open, close, high, volume,
    close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret,
    volume/NULLIF(AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING),0) AS vratio,
    MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 59 PRECEDING AND 1 PRECEDING) AS hi60,
    MIN(low) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS lo240,
    MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS hi240
  FROM daily)
SELECT symbol, date, open, close, ret, vratio, hi60, lo240, hi240,
  LEAD(open) OVER (PARTITION BY symbol ORDER BY date) AS next_open,
  LEAD(close) OVER (PARTITION BY symbol ORDER BY date) AS next_close,
  LEAD(high) OVER (PARTITION BY symbol ORDER BY date) AS next_high
FROM r
""").fetchdf()
con.close()
df = df.dropna(subset=["ret","vratio","hi60","next_open"])
df["pos240"] = (df.close - df.lo240) / (df.hi240 - df.lo240).replace(0, np.nan) if False else (df.close - df.lo240) / (df.hi240 - df.lo240)
df["next_ret"] = df.next_close / df.next_open - 1
df["next_high_ret"] = df.next_high / df.next_open - 1

import numpy as np
grid = []
for vlo in (3, 4, 6):
    for ret_lo, ret_hi in ((0.01,0.05),(0.02,0.05),(0.02,0.095),(0.03,0.095)):
        for pos in (0.0, 0.3, 0.5, 0.99):
            sel = df[(df.vratio>=vlo)&(df.vratio<=9)&(df.ret>=ret_lo)&(df.ret<=ret_hi)]
            if pos == 0.99:
                sel = sel[sel.close >= sel.hi60*0.99]
            else:
                sel = sel[sel.pos240 <= pos]
            if len(sel) < 30:
                continue
            grid.append({"vlo":vlo,"ret_lo":ret_lo,"ret_hi":ret_hi,"pos":pos,
                         "n":len(sel),"avg":sel.next_ret.mean(),
                         "win":(sel.next_ret>0).mean(),
                         "hi5":(sel.next_high_ret>=0.05).mean(),
                         "hi95":(sel.next_high_ret>=0.095).mean()})
g = pd.DataFrame(grid)
os.makedirs("/tmp/backtest_grid", exist_ok=True)
g.to_parquet("/tmp/backtest_grid/rules_grid.parquet", compression="zstd", index=False)
# 全样本明细(大件): 每日每票次日收益预测底表
df[["symbol","date","open","close","ret","vratio","pos240","next_open","next_close","next_high","next_ret","next_high_ret"]].to_parquet(
    "/tmp/backtest_grid/lead_panel.parquet", compression="zstd", index=False)
print("grid rules:", len(g))
print(g.round(4).sort_values("avg", ascending=False).head(10).to_string(index=False))
import os as _os
print("sizes:", {f: _os.path.getsize(f"/tmp/backtest_grid/{f}")//1024//1024 for f in _os.listdir("/tmp/backtest_grid")})
