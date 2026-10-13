#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/daily_today.py ═══
# 上游 commit: 056b5fdf2aab · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""daily_today.py — 每日收盘后抓当日日K(腾讯) → wh当日分区(触发sync_wh_to_duckdb同步)
cron: 15:20 交易日
"""
import json, urllib.request, duckdb, datetime, os, sys

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
WH = "/home/agentuser/stockdb/wh/daily"
sys.path.insert(0, "/home/agentuser/stockdb/scripts")
from data_warehouse import write_partition

def main():
    today = str(datetime.date.today())
    # 当日分区若已含today则跳过
    fp = f"{WH}/{today[:4]}.parquet"
    if os.path.exists(fp):
        con = duckdb.connect()
        try:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{fp}') WHERE date='{today}'").fetchone()[0]
            if n > 3000:
                print("already", n); return
        finally:
            con.close()
    # 主板票清单
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM mainboard ORDER BY symbol").fetchall()]
    con.close()
    rows = []
    for i in range(0, len(syms), 60):
        batch = syms[i:i+60]
        try:
            txt = urllib.request.urlopen(urllib.request.Request(
                "https://qt.gtimg.cn/q=" + ",".join(batch), headers=UA), timeout=15).read().decode("gbk", "ignore")
        except Exception:
            continue
        for line in txt.split(";"):
            if "=" not in line: continue
            f = line.split("=")[1].strip('"').split("~")
            if len(f) < 50 or not f[2]: continue
            sym = ("sh" if f[0] == "1" else "sz") + f[2]
            try:
                o, c, h, l, vol = float(f[4] or 0), float(f[3] or 0), float(f[33] or 0), float(f[34] or 0), float(f[36] or 0)
                if c > 0:
                    rows.append([sym, today, o, c, h, l, vol])
            except (ValueError, IndexError):
                continue
    if len(rows) < 1000:
        print("too few rows:", len(rows)); return
    write_partition("daily", today[:4], rows, merge_on=(0, 1))
    print(f"daily {today}: {len(rows)} rows → wh")

if __name__ == "__main__":
    main()
