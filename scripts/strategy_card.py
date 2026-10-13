#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/strategy_card.py ═══
# 上游 commit: 75230ac1fa67 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""strategy_card.py — 策略卡v1: 胜率导向组合 (2024-2026回测208万行网格搜索最优)
规则B(已证最优): 240日位<35% + 量比0.8-2.0 + 当日涨0-5% + 非妖股(次日振幅排除不可执行)
持有口径: 短线=次日冲高+2%止盈/破-2%止损(胜率44.3%, E+0.28%/笔)
         或持有5日收盘卖出(胜率55.5%, 基准48.8% → +6.7pp alpha)
市况: 全球> -15 才进(强市子集无额外增益, 弱市闸门仍保留)
输出: data/strategy_card.json — 当日B规则候选+说明
"""
import duckdb, json, datetime, urllib.request

def main():
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    rows = con.execute("""
    WITH b AS (
      SELECT symbol, date, open, close, high, low, volume,
        close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret,
        volume/NULLIF(AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING),0) AS vr,
        close/MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS pos240,
        high/NULLIF(MIN(low) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 9 PRECEDING AND 1 PRECEDING),0) AS pos10
      FROM daily WHERE symbol LIKE 'sh60%' OR symbol LIKE 'sz00%')
    SELECT symbol, close, ret, vr, pos240, pos10 FROM b
    WHERE date=(SELECT MAX(date) FROM daily)
      AND ret IS NOT NULL AND vr IS NOT NULL AND pos240 IS NOT NULL
      AND pos240<0.35 AND vr BETWEEN 0.8 AND 2.0 AND ret BETWEEN 0 AND 0.05 AND pos10<1.05
      AND symbol NOT LIKE '%30%' AND symbol NOT LIKE '%68%'
    ORDER BY vr DESC LIMIT 12""").fetchall()
    con.close()
    # 全球闸门
    try:
        g = json.load(open("/home/agentuser/stockdb/data/global/latest.json"))
        gs = (g.get("metrics") or {}).get("global_score", 0)
    except Exception:
        gs = 0
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "strategy": "B规则(低位+温和量+涨0-5%)+5日持有",
           "backtest": {"win_rate_5d": 55.5, "baseline_5d": 48.8, "alpha_pp": 6.7,
                        "win_rate_t1": 44.3, "E_t1_pct": 0.28, "n": 1995, "period": "2024-2026"},
           "gate": {"global_score": gs, "trade_ok": gs > -15},
           "candidates": [{"symbol": r[0], "close": r[1], "ret_pct": round(r[2]*100, 2),
                           "vratio": round(r[3], 2), "pos240": round(r[4], 3)} for r in rows],
           "discipline": ["T+1: 次日冲高+2%止盈(胜率口径44%)", "或持有5日收盘卖(胜率口径55.5%)",
                          "破-2%无条件止损", "仓位=半凯利(实盘胜率闭环)", "全球<-15空仓"],
           "note": "回测为成本前口径; 胜率天花板约55%(5日口径), 80%需改变持有周期或品种, 不构成投资建议"}
    json.dump(out, open("/home/agentuser/stockdb/data/strategy_card.json", "w"), ensure_ascii=False, indent=1)
    # 落picks.duckdb对账闭环(kind='bcard', 5日口径settle): 防重复(同日同票)
    try:
        pc = duckdb.connect("/home/agentuser/stockdb/picks.duckdb")
        today = str(datetime.date.today())
        for r in rows:
            ex = pc.execute("SELECT COUNT(*) FROM pick_log WHERE symbol=? AND kind='bcard' AND pick_date=?", [r[0], today]).fetchone()[0]
            if not ex:
                nid = pc.execute("SELECT COALESCE(MAX(id),0)+1 FROM pick_log").fetchone()[0]
                pc.execute("INSERT INTO pick_log VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,0)",
                           [nid, datetime.datetime.now().isoformat(timespec="seconds"), today, r[0], "", float(r[1]), "bcard", "B_5d"])
        pc.close()
        print(f"bcard落库: {len(rows)}票")
    except Exception as e:
        print("bcard落库失败:", e)
    print(f"策略卡: 候选{len(rows)}票, 全球{gs}, trade_ok={gs > -15}")

if __name__ == "__main__":
    main()
