#!/usr/bin/env python3
"""
回测框架 v1 — 用本地 ~/stockdb 全量库验证 pick_algo 打分有效性 (qwerzxcva/stocks-algo)

目的: 把 limitup-stats.md 的统计规律变成可验证的回测数字:
  1. 大盘环境因子: 个股打分叠加"当日大盘宽度"降权 (普跌日全体降权)
  2. 分层验证: score>=70 / 60-70 / <60 的次日收益是否单调递增 (算法有效性核心检验)
  3. 与基准对比: 全体首板次日均值(+0.42%) vs 高分组的次日均值

数据: 本地 DuckDB (5223只, 2022-07起)。不联网。
用法: python3 backtest.py [--start 2022-07-01] [--end 2026-10-09]
"""
from __future__ import annotations
import argparse, json, os, sys
import duckdb

DB = "SELECT * FROM read_parquet('/home/agentuser/stockdb/data/kline/sh/*.parquet') UNION ALL SELECT * FROM read_parquet('/home/agentuser/stockdb/data/kline/sz/*.parquet')"

def build_views(con):
    con.execute(f"CREATE VIEW kl AS {DB}")
    # 个股日线 + 次日
    con.execute("""
    CREATE VIEW pc AS
    SELECT symbol, date, open, close, high, low, volume,
      lag(close) OVER (PARTITION BY symbol ORDER BY date) pclose,
      lead(close) OVER (PARTITION BY symbol ORDER BY date) nclose,
      lead(open)  OVER (PARTITION BY symbol ORDER BY date) nopen,
      lag(volume) OVER (PARTITION BY symbol ORDER BY date) pvol,
      lag(close,20) OVER (PARTITION BY symbol ORDER BY date) c20,
      max(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) h60
    FROM kl WHERE (symbol LIKE 'sh60%' OR symbol LIKE 'sz00%') AND date>='2022-07-01'
    """)
    # 涨停首板事件表
    con.execute("""
    CREATE VIEW ev AS
    SELECT symbol, date, close, nclose, nopen,
      volume/pvol AS vr,
      close/c20-1 AS p20,
      CASE WHEN close>=h60 THEN 1 ELSE 0 END is_nh,
      CASE WHEN close>=high*0.999 THEN 1 ELSE 0 END zt,
      lag(CASE WHEN close>=high*0.999 THEN 1 ELSE 0 END) OVER (PARTITION BY symbol ORDER BY date) pzt
    FROM pc
    """)
    # 大盘宽度: 每日主板上涨占比
    con.execute("""
    CREATE VIEW width AS
    SELECT date, sum(CASE WHEN close>pclose THEN 1 ELSE 0 END)*1.0/count(*) AS up_ratio
    FROM pc WHERE pclose IS NOT NULL GROUP BY date
    """)

def env_mult(up_ratio: float) -> float:
    """环境乘数: 普跌日降权 (cycle-patterns: 普跌日次日-0.005 vs 普涨+0.119)"""
    if up_ratio is None:
        return 0.5
    if up_ratio < 0.25:  return 0.3   # 恐慌日
    if up_ratio < 0.40:  return 0.7   # 弱市
    if up_ratio > 0.80:  return 1.1   # 普涨日惯性
    return 1.0

def score_row(vr, p20, is_nh, up_ratio) -> float:
    """与 pick_algo.score_limitup 同构的首板版打分"""
    s = 0.0
    s += 30 if (vr or 0) > 3 else (18 if (vr or 0) > 1.5 else 3)
    s += 12 if (p20 or 0) > 0.15 else (10 if (p20 or 0) < 0 else 6)
    s += 10 if is_nh else 0
    s *= env_mult(up_ratio)
    return s

def run(start="2022-07-01", end="2026-10-09"):
    con = duckdb.connect()
    build_views(con)
    print(f"回测区间 {start} ~ {end}")
    q = con.execute(f"""
    WITH e AS (
      SELECT e.*, w.up_ratio
      FROM ev e JOIN width w USING(date)
      WHERE e.zt=1 AND coalesce(e.pzt,0)=0 AND e.nclose IS NOT NULL
        AND e.date>='{start}' AND e.date<='{end}'
    )
    SELECT symbol, date, vr, p20, is_nh, up_ratio, nclose, close, nopen
    FROM e
    """)
    rows = q.fetchall()
    print(f"首板样本: {len(rows)}")

    scored = []
    for sym, dt, vr, p20, nh, ur, nc, c, no in rows:
        s = score_row(vr, p20, nh, ur)
        # 两种买入口径: 次日收盘卖 / 次日开盘卖(更现实,打板客次日竞价看盘决定)
        scored.append({"s": s, "ret_c": (nc/c-1)*100, "ret_o": (nc/no-1)*100 if no else None, "date": dt})

    # 分层
    buckets = [(70,999,"A(>=70)"), (55,70,"B(55-70)"), (40,55,"C(40-55)"), (0,40,"D(<40)")]
    print(f"\n{'组':<10}{'n':>8}{'次日收(收卖)':>12}{'胜率':>8}{'次日开卖':>12}")
    all_ret = [x["ret_c"] for x in scored]
    print(f"{'全样本':<10}{len(all_ret):>8}{sum(all_ret)/len(all_ret):>+11.2f}%{100*sum(1 for r in all_ret if r>0)/len(all_ret):>7.1f}%")
    for lo, hi, name in buckets:
        sel = [x for x in scored if lo <= x["s"] < hi]
        if not sel:
            continue
        rc = [x["ret_c"] for x in sel]
        ro = [x["ret_o"] for x in sel if x["ret_o"] is not None]
        win = 100*sum(1 for r in rc if r>0)/len(rc)
        print(f"{name:<10}{len(rc):>8}{sum(rc)/len(rc):>+11.2f}%{win:>7.1f}%{sum(ro)/len(ro):>+11.2f}%")

    # 环境因子消融: 同分位下有/无 env_mult 的对比
    print("\n── 环境因子消融（同规则, 去掉 env_mult）──")
    scored2 = []
    for sym, dt, vr, p20, nh, ur, nc, c, no in rows:
        s2 = (30 if (vr or 0)>3 else (18 if (vr or 0)>1.5 else 3)) + (12 if (p20 or 0)>0.15 else (10 if (p20 or 0)<0 else 6)) + (10 if nh else 0)
        scored2.append({"s": s2, "ret": (nc/c-1)*100})
    for lo, hi, name in [(70,999,"A(>=70)"),(55,70,"B(55-70)"),(40,55,"C(40-55)")]:
        sel = [x for x in scored2 if lo <= x["s"] < hi]
        if sel:
            rc = [x["ret"] for x in sel]
            print(f"{name:<10}{len(rc):>8}{sum(rc)/len(rc):>+11.2f}%")

    # 按年稳定性
    print("\n── A组(>=70, 含环境降权) 按年 ──")
    by_year = {}
    for x in scored:
        if x["s"] >= 70:
            by_year.setdefault(x["date"][:4], []).append(x["ret_c"])
    for y in sorted(by_year):
        v = by_year[y]
        print(f"{y}: n={len(v)} avg={sum(v)/len(v):+.2f}% win={100*sum(1 for r in v if r>0)/len(v):.1f}%")

    con.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-07-01")
    ap.add_argument("--end", default="2026-10-09")
    a = ap.parse_args()
    run(a.start, a.end)
