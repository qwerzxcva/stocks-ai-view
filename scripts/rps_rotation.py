#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/rps_rotation.py ═══
# 上游 commit: bf9cf060c570 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
RPS 相对强度轮动 (参考 tick-stock-panel rps_rotation, pandas版)
用法: python3 rps_rotation.py <duckdb路径>
对全市场每票计算 RPS(250/120/60/20日涨幅百分位), 输出强势股+强度趋势.
"""
from __future__ import annotations
import duckdb, pandas as pd, numpy as np, sys

WINDOWS = [("rps_250", 250), ("rps_120", 120), ("rps_60", 60), ("rps_20", 20)]

def build(con) -> pd.DataFrame:
    # 每票最新收盘 vs N天前收盘
    df = con.execute("""
        WITH latest AS (
          SELECT symbol, MAX(date) AS d FROM daily GROUP BY symbol)
        SELECT c.symbol, c.date, c.close FROM daily c
        JOIN latest l ON c.symbol=l.symbol AND c.date=l.d
    """).fetchdf()
    piv = con.execute("""
        SELECT symbol, date, close FROM daily ORDER BY symbol, date
    """).fetchdf().set_index(["symbol", "date"])
    out = []
    closes = piv.reset_index().pivot(index="date", columns="symbol", values="close").sort_index()
    for wname, w in WINDOWS:
        ret = closes.pct_change(w).iloc[-1]
        rps = ret.rank(pct=True) * 100
        out.append(pd.DataFrame({wname: rps}))
    rps_df = pd.concat(out, axis=1)
    rps_df["date"] = closes.index[-1]
    return rps_df.reset_index().rename(columns={"index": "symbol"})

def top_strong(rps_df: pd.DataFrame, n=30) -> pd.DataFrame:
    rps_df = rps_df.dropna(subset=["rps_60"]).copy()
    rps_df["strong_score"] = rps_df[["rps_250", "rps_120", "rps_60", "rps_20"]].mean(axis=1)
    return rps_df.sort_values("strong_score", ascending=False).head(n)

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/agentuser/stockdb/stock.duckdb"
    con = duckdb.connect(path, read_only=True)
    rps = build(con)
    top = top_strong(rps)
    print("=== RPS 强势 Top15 (四窗口均值) ===")
    print(top.head(15).round(1).to_string(index=False))
    con.close()
