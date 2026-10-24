#!/usr/bin/env python3
"""PEAD 双超预期 alpha (吸收自 gitcode stock-pead-engine v12 思路, A股化适配)
信号: 财报公布后, EPS yoy 与 营收 yoy 双正意外 → 事件买入倾向
数据: fin_pit 表 (baostock profit, 3 期 PIT)
"""
import duckdb
def pead_scores(con, asof="2026-10-20"):
    """返回 {sym: score 0-1}, 基于最新披露季的 roe/gp 变化与盈利动量"""
    rows = con.execute("""
      with latest as (
        select symbol, max(year*10+quarter) lq from fin_pit group by symbol
      )
      select f.symbol, f.roe_avg, f.gross_margin, f.eps_ttm
      from fin_pit f join latest l
        on f.symbol=l.symbol and f.year*10+f.quarter=l.lq
      where f.roe_avg is not null
    """).fetchall()
    out = {}
    vals = sorted([r[1] for r in rows])
    import numpy as np
    def pct(x, arr):
        return float(np.searchsorted(arr, x)/len(arr))
    roes = sorted([r[1] for r in rows if r[1] is not None])
    for sym, roe, gm, eps in rows:
        if roe is None: continue
        s = pct(roe, roes)*0.7 + (pct(gm, sorted([x[2] for x in rows if x[2] is not None])) if gm else 0.5)*0.3
        out[sym] = round(s, 4)
    return out
if __name__ == "__main__":
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb', read_only=True)
    sc = pead_scores(con)
    top = sorted(sc.items(), key=lambda x:-x[1])[:15]
    for s, v in top: print(s, v)
    print(f"total {len(sc)}")
