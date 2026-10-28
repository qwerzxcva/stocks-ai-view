"""hive_event.py — E6 事件域 (非价格输入: 涨停池/龙虎榜/日历)
策略1 zt_pool_chain: 昨日涨停梯队 (连板高度=情绪辨识度)
策略2 lhb_seat_quality: 龙虎榜知名游资席位 (近30日, knowledge.lhb_seat)
策略3 calendar_alpha: 星期/月历 (周五规避, 月初加分)
"""
import duckdb
import pandas as pd


def run(ctx):
    sig = []
    t1 = ctx["t1"]
    # S1 涨停梯队 (qfq 全市场 ret>9.5%)
    try:
        con = duckdb.connect(f"{t1}/qfq_all.duckdb", read_only=True)
        con.execute("SET memory_limit='2GB'")
        zt = con.execute("""
          WITH px AS (SELECT sym, CAST(d AS DATE) dd, c, o FROM qfq WHERE (sym LIKE 'sh60%' OR sym LIKE 'sz00%')),
          r AS (SELECT sym, dd, o, c, c/lag(c) OVER (PARTITION BY sym ORDER BY dd) - 1 AS ret FROM px),
          seq AS (SELECT sym, dd, ret,
                    SUM(CASE WHEN ret>0.095 THEN 1 ELSE 0 END) OVER
                      (PARTITION BY sym ORDER BY dd ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS zt5
                  FROM r)
          SELECT sym, zt5 FROM seq WHERE dd = (SELECT max(dd) FROM seq) AND ret > 0.095
        """).fetchdf()
        con.close()
        for _, r in zt.iterrows():
            score = min(0.4 + 0.15 * r.zt5, 1.0)
            sig.append({"sym": r.sym, "score": round(score, 3), "conf": 0.7,
                        "strategy": "zt_chain", "why": f"涨停梯队{int(r.zt5)}"} )
    except Exception:
        pass
    # S2 游资席位 (近30日上榜且知名席位 ≥2 次)
    try:
        con = duckdb.connect("/home/agentuser/stockdb/knowledge.duckdb", read_only=True)
        hot = con.execute("""
          SELECT code, count(*) n FROM lhb_seat
          WHERE d >= CURRENT_DATE - INTERVAL 30 DAY AND code IS NOT NULL
          GROUP BY code HAVING count(*) >= 2 ORDER BY n DESC LIMIT 80
        """).fetchdf()
        con.close()
        for _, r in hot.iterrows():
            code = str(r.code).zfill(6)
            sym = ("sh" if code.startswith("6") else "sz") + code
            sig.append({"sym": sym, "score": round(min(0.3 + 0.1 * r.n, 0.8), 3),
                        "conf": 0.6, "strategy": "lhb_hot", "why": f"30日上榜{int(r.n)}次"})
    except Exception:
        pass
    # S3 日历
    dd = pd.Timestamp(ctx["date"])
    wd = dd.weekday()
    cal = 0.4 if wd <= 2 else (0.2 if wd == 3 else 0.1)   # 周初>周中>周五
    for s in sig:
        if s["strategy"] == "zt_chain":
            s["score"] = round(min(s["score"] * (0.8 + cal), 1.0), 3)
    sig.sort(key=lambda x: -x["score"])
    return {"engine": "E6", "domain": "event", "signals": sig[:150],
            "meta": {"n": len(sig)}}
