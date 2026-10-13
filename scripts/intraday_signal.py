#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/intraday_signal.py ═══
# 上游 commit: 3775ac1ced80 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (duckdb minute 表 → 本项目无分钟库; 输出 data/intraday_signal.json; --out)
"""
intraday_signal.py — 日内反转信号 (独特算法: 早盘强度→下午/次日行为)
口径: 早盘强度 = 10:30价/上午VWAP-1
弱市实证(10-12): 早强+1.5%→下午均-3.2% (兑现杀); 早弱-1%→下午+3.2% (超跌反弹)

本项目限制: 上游依赖 stockdb/stock.duckdb 的 minute 表 (1分钟K), 本项目未采集分钟线
  (见 minute1_collect.py 属 B 类, pyarrow 亦未装) → 无分钟数据时输出 status=no_minute_data,
  不产生信号但保持 CI 可调用 (exit 0), 待分钟线接入后自动生效。
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, DUCKDB_PATH
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    DUCKDB_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "stocks-data-live", "data", "stocks.duckdb")


def _has_minute_table(path):
    if not os.path.exists(path):
        return False
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        con.close()
        return "minute" in tables
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="日内反转信号 (早盘强度 → 下午/次日行为)")
    ap.add_argument("--db", default=DUCKDB_PATH, help="DuckDB 路径 (需含 minute 表)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "intraday_signal.json"))
    args = ap.parse_args()

    if not _has_minute_table(args.db):
        out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "status": "no_minute_data",
               "reason": f"{args.db} 无 minute 表 — 本项目未采集1分钟K (minute1_collect 依赖 pyarrow, 未安装)",
               "chase_list": [], "dip_watch": [],
               "note": "无分钟数据, 不产生信号; 接入分钟线后自动生效, 不构成投资建议"}
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print("no minute data → 空信号 (status=no_minute_data)")
        print("->", args.out)
        return 0

    import duckdb

    con = duckdb.connect(args.db, read_only=True)
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM minute ORDER BY date DESC LIMIT 2").fetchall()]
    if not dates:
        print("no minute data"); con.close(); return 0
    d1 = dates[0]
    rows = con.execute(f"""
        WITH vwap AS (
          SELECT symbol, SUM(price*volume)/NULLIF(SUM(volume),0) AS vwap,
                 MAX(CASE WHEN hhmm='10:30' THEN price END) AS p1030
          FROM minute WHERE date='{d1}' GROUP BY symbol)
        SELECT symbol, p1030/vwap-1 AS strength FROM vwap
        WHERE p1030 IS NOT NULL AND vwap>0""").fetchall()
    con.close()
    chase = sorted([r for r in rows if r[1] >= 0.015], key=lambda x: -x[1])[:20]
    dip = sorted([r for r in rows if r[1] <= -0.01], key=lambda x: x[1])[:20]
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "date": d1,
           "chase_list": [{"symbol": s, "strength": round(v, 4), "hint": "隔日延续观察(实证+1.27%)"} for s, v in chase],
           "dip_watch": [{"symbol": s, "strength": round(v, 4), "hint": "隔日偏弱观察(实证-0.31%)"} for s, v in dip],
           "note": "早盘强度→隔日: 早强+1.27%/早弱-0.31%(n=3166, 2026-10-12→13), 不构成投资建议"}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"{d1}: 冲高榜{len(chase)} 超跌榜{len(dip)}")
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
