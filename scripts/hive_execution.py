"""hive_execution.py — E7 执行域 (只对中枢给的票算执行计划, 不选票)
策略1 atr_zone: ATR14 买区/止损/冲高目标
策略2 gap_tactic: 昨收+跳空情境 (连板票 gap 0-2% 挂开盘*0.99)
策略3 vwap_pullback: 次日 VWAP 回踩估滑点
"""
import duckdb


def run(ctx):
    # 中枢融合后调用 fuse() 时会把候选票名单放 ctx['candidates']
    cands = ctx.get("candidates") or []
    if not cands:
        return {"engine": "E7", "domain": "execution", "signals": [], "meta": {"note": "no candidates yet"}}
    syms = [c if isinstance(c, str) else c["sym"] for c in cands]
    ph = ",".join(f"'{s}'" for s in syms)
    con = duckdb.connect(f"{ctx['t1']}/qfq_all.duckdb", read_only=True)
    con.execute("SET memory_limit='2GB'")
    rows = con.execute(f"""
      WITH px AS (SELECT sym, CAST(d AS DATE) dd, o, c, h, l FROM qfq WHERE sym IN ({ph})),
      r2 AS (SELECT sym, dd, o, c, h, l, lag(c) OVER w AS pc FROM px WINDOW w AS (PARTITION BY sym ORDER BY dd)),
      tr AS (SELECT sym, dd, c, pc, greatest(h-l, abs(h-pc), abs(l-pc)) AS tr FROM r2),
      m AS (SELECT sym, dd, c, pc,
              avg(tr) OVER (PARTITION BY sym ORDER BY dd ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr14
            FROM tr)
      SELECT sym, c, pc, atr14 FROM m WHERE dd = (SELECT max(dd) FROM m)
    """).fetchdf()
    con.close()
    sig = []
    for _, r in rows.iterrows():
        atr = r.atr14 or 0
        lo = min(r.pc, r.c) - 0.5 * atr
        hi = max(r.pc, r.c) + 0.3 * atr
        sig.append({"sym": r.sym, "score": 1.0, "conf": 1.0, "strategy": "atr_zone",
                    "why": "执行计划",
                    "buy_zone": [round(lo, 2), round(hi, 2)],
                    "stop": round(lo - 1.2 * atr, 2),
                    "target": round(lo + 2.4 * atr, 2),
                    "atr": round(atr, 3)})
    return {"engine": "E7", "domain": "execution", "signals": sig,
            "meta": {"n": len(sig)}}
