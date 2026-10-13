#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/intraday_signal.py ═══
# 上游 commit: 3775ac1ced80 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""intraday_signal.py — 日内反转信号 (独特算法: 早盘强度→下午/次日行为)
口径: 早盘强度 = 10:30价/上午VWAP-1
弱市实证(10-12): 早强+1.5%→下午均-3.2% (兑现杀); 早弱-1%→下午+3.2% (超跌反弹)
输出: data/intraday_signal.json — ①冲高兑现榜(持仓若持有→次日开盘卖出提示) ②超跌反弹观察榜
"""
import duckdb, json, datetime

OUT = "/home/agentuser/stockdb/data/intraday_signal.json"

def main():
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM minute ORDER BY date DESC LIMIT 2").fetchall()]
    if not dates:
        print("no minute data"); return
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
    json.dump(out, open(OUT, "w"), ensure_ascii=False)
    print(f"{d1}: 冲高榜{len(chase)} 超跌榜{len(dip)}")

if __name__ == "__main__":
    main()
