#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/sync_wh_to_duckdb.py ═══
# 上游 commit: 98b2bf1828dc · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""sync_wh_to_duckdb.py — wh(parquet近3年) → stock.duckdb daily 增量同步 (anti-join补缺, 秒级)"""
import duckdb

def main():
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb")
    total = 0
    for yr in ("2024", "2025", "2026"):
        fp = f"/home/agentuser/stockdb/wh/daily/{yr}.parquet"
        try:
            missing = con.execute(f"""SELECT wh.* FROM read_parquet('{fp}') wh
                ANTI JOIN daily d ON d.symbol=wh.symbol AND d.date=wh.date""").fetchall()
        except Exception:
            continue
        if missing:
            con.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?)", missing)
            total += len(missing)
    mx = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    con.close()
    print(f"sync {total} rows, daily最新={mx}")

if __name__ == "__main__":
    main()
