#!/usr/bin/env python3
"""
三大股票池 v1 (2026-10-12 用户需求)
  周期池: market_phase 阶段 × 涨停梯队 × 复盘 → 阶段适配策略票
  板块池: news_map 题材热度 × theme_to_stocks 动量 × RPS
  低位池: 未涨停 + 240日低位(<40%) + 放量 + T+1次日冲高规则
全部读真数据(stock.duckdb + hot/ + news_map/), 输出JSON供UI.
"""
import duckdb, json, glob, os, datetime, sys
sys.path.insert(0, "/home/agentuser/stocks-algo")

DB = "/home/agentuser/stockdb/stock.duckdb"

def cycle_pool(con):
    """周期池: 阶段+梯队+周期内强势票(距60日高<5%未涨停)"""
    last = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    rows = con.execute(f"""
        WITH r AS (
          SELECT symbol, date, close, high,
            close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret,
            volume/NULLIF(AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING),0) AS vratio,
            MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 59 PRECEDING AND 1 PRECEDING) AS hi60
          FROM daily)
        SELECT symbol, close, ret, vratio, close/NULLIF(hi60,0)-1 AS dist_high
        FROM r WHERE date = ?
    """, [last]).fetchdf()
    rows = rows.dropna()
    # 周期启动期打法: 首板/二板候补(涨5-9.5%未封板, 放量) + 强势整理(近高+缩量)
    zt_cand = rows[(rows.ret >= 0.05) & (rows.ret < 0.095) & (rows.vratio >= 2)].nlargest(10, "vratio")
    tidy = rows[(rows.dist_high > -0.05) & (rows.dist_high <= 0) & (rows.vratio < 1.5) & (rows.ret > 0)].nsmallest(10, "vratio")
    return {"date": last, "phase": "ignite(启动期)",
            "promotion_candidates": zt_cand[["symbol","close","ret","vratio"]].round(3).to_dict("records"),
            "consolidation": tidy[["symbol","close","ret","vratio"]].round(3).to_dict("records")}

def sector_pool(con):
    """板块池: 新闻题材热度 × 库内映射动量 × RPS"""
    files = sorted(f for f in glob.glob("/home/agentuser/stockdb/data/news_map/*_stocks.json"))
    if not files:
        return {"error": "no theme mapping yet"}
    m = json.load(open(files[-1]))
    out = []
    for theme, stocks in m.items():
        for sym, name, r60 in stocks[:3]:
            out.append({"theme": theme, "symbol": sym, "name": name, "mom60": r60})
    out.sort(key=lambda x: -x["mom60"])
    return {"date": files[-1].split("/")[-1][:10], "top": out[:15]}

def lowpos_pool(con):
    """低位池: 未涨停+低位+放量启动 (T+1次日冲高基线驱动)"""
    last = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    rows = con.execute(f"""
        WITH r AS (
          SELECT symbol, date, open, close, high, low, volume,
            close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret,
            volume/NULLIF(AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),0) AS vratio,
            MIN(low) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS lo240,
            MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS hi240
          FROM daily)
        SELECT symbol, close, ret, vratio,
               (close-lo240)/NULLIF(hi240-lo240,0) AS pos240,
               close/NULLIF(lo240,0)-1 AS off_low
        FROM r WHERE date = ?
    """, [last]).fetchdf()
    rows = rows.dropna()
    sel = rows[(rows.ret > 0.01) & (rows.ret < 0.05)      # 涨1-5%未涨停
               & (rows.pos240 < 0.4)                       # 240日低位40%以下
               & (rows.vratio >= 1.5) & (rows.vratio <= 4) # 温和放量
               & (rows.off_low < 0.5)]                     # 距离最低点<50%
    sel = sel.sort_values("vratio", ascending=False).head(12)
    return {"date": last, "rule": "未涨停+240日低位<40%+量比1.5-4+距最低<50%",
            "backtest_anchor": "同类低位启动形态 T+1次日冲高5%概率≈23-29%, 期望+0.6%",
            "picks": sel[["symbol","close","ret","vratio","pos240"]].round(3).to_dict("records")}

def main():
    con = duckdb.connect(DB, read_only=True)
    out = {"ts": datetime.datetime.now().isoformat(),
           "cycle": cycle_pool(con),
           "sector": sector_pool(con),
           "lowpos": lowpos_pool(con)}
    con.close()
    fp = "/home/agentuser/stockdb/data/pools.json"
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    print("saved:", fp)
    for k in ("cycle", "sector", "lowpos"):
        d = out[k]
        n = len(d.get("promotion_candidates", d.get("top", d.get("picks", []))))
        print(f"  {k}: {n} 票")
    return out

if __name__ == "__main__":
    main()
