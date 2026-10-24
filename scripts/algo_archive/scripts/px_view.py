#!/usr/bin/env python3
"""
px_view.py — 统一价格视图 (去重修复版 2026-10-18)
问题: qfq_all.duckdb(主库) 与 bs_qfq parquet 有 2370 票重叠, union 后同票同日双行,
     污染所有引擎的因子/动量/中位数计算 (28452 重复行)。
修复: qfq 优先, bs 只补主库没有的票 (1273 只退市票, 价值高不可弃)。
用法: from px_view import attach_px; attach_px(con)
"""
import duckdb

T1 = "/home/agentuser/stockdb/t1"

def attach_px(con):
    names = {r[0] for r in con.execute("select database_name from duckdb_databases()").fetchall()}
    if "t1" not in names:
        con.execute(f"attach '{T1}/qfq_all.duckdb' as t1 (read_only)")
    con.execute(f"""
    create view all_px as
    -- 主库全量 (复权校准过的, 优先)
    select sym, cast(d as date) d, cast(o as double) o, cast(c as double) c, cast(h as double) h, cast(l as double) l, cast(v as double) v from t1.qfq
    union all
    -- bs 只保留主库没有的票 (退市票)
    select replace(code,'.','') sym, cast(date as date) d, cast(open as double) o, cast(close as double) c, cast(high as double) h, cast(low as double) l, cast(volume as double) v
    from read_parquet('{T1}/bs_qfq/*.parquet') b
    where b.tradestatus='1'
      and replace(b.code,'.','') not in (select sym from t1.qfq)
    """)
    return con

if __name__ == "__main__":
    con = duckdb.connect()
    attach_px(con)
    r = con.execute("select count(*) n_rows, count(distinct sym) n_syms, count(distinct sym||d) n_keys from all_px").fetchone()
    print("去重后:", r, "| 重复行:", r[0]-r[2])
