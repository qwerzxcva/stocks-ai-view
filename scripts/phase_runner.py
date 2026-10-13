#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/phase_runner.py ═══
# 上游 commit: db4c5a0d38e3 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""phase_runner.py — 市场阶段机数据供给器: 从daily表自算涨停结构 → 六阶段 → market_phase.json"""
import json, sys, datetime, duckdb
sys.path.insert(0, "/home/agentuser/stocks-algo")
from market_phase import classify

def main():
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    rows = con.execute("""
        WITH r AS (
          SELECT symbol, date, close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret
          FROM daily WHERE date >= (SELECT strftime(MAX(date)::DATE - 90, '%Y-%m-%d') FROM daily))
        SELECT date,
          COUNT(*) AS n,
          SUM(CASE WHEN ret >= 0.095 THEN 1 ELSE 0 END) AS fb
        FROM r GROUP BY date ORDER BY date""").fetchall()
    # 2板+: 简化= 连续两日涨停的票数 — 用自连接
    g2 = con.execute("""
        WITH r AS (
          SELECT symbol, date, close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret
          FROM daily WHERE date >= (SELECT strftime(MAX(date)::DATE - 90, '%Y-%m-%d') FROM daily))
        SELECT a.date, COUNT(*) FROM r a JOIN r b ON a.symbol=b.symbol AND b.date=CAST(a.date AS DATE)+1 AND b.date=CAST(b.date AS VARCHAR)
        WHERE a.ret>=0.095 AND b.ret>=0.095 GROUP BY a.date ORDER BY a.date""").fetchall()
    con.close()
    g2m = {str(d): c for d, c in g2}
    daily = [{"date": str(d), "first_board": fb, "ge2": g2m.get(str(d), 0), "max_height": 0, "promote_rate": 0, "seal_rate": 0} for d, n, fb in rows if fb is not None]
    phases = classify(daily)
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "dates": [x["date"] for x in daily], "phases": phases,
           "phase": phases[-1] if phases else "?",
           "label": {"ice": "冰点", "ignite": "启动", "rally": "主升", "climax": "高潮", "ebb": "退潮", "repair": "修复"}.get(phases[-1] if phases else "", "?"),
           "recent": list(zip([x["date"] for x in daily[-5:]], phases[-5:]))}
    json.dump(out, open("/home/agentuser/stockdb/data/market_phase.json", "w"), ensure_ascii=False)
    print("phase:", out["phase"], out["label"], out["recent"][-3:])

if __name__ == "__main__":
    main()
