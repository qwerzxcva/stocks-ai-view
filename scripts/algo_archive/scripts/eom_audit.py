#!/usr/bin/env python
"""eom_audit.py — 月末截面诚实审计 (v3 组合 + 漂移监控)
口径铁律: ①月末最后交易日单一截面 ②T+22 LEAD 标签 ③含成本 0.7%×换手 ④对照基准=同截面全票均值
"""
import duckdb, pandas as pd, numpy as np, datetime, json, os, glob

DB = os.environ.get("PX_DB", "px_data/*.parquet")
F = ["wq_ts_rank_high_10","mic_close_loc_10","vp_vwap_mom_20","a158_range_pos_20",
     "a158_amt_vwap_dev_20","ma20_bias","gi_close_strength_10"]

def main():
    con = duckdb.connect()
    parq = glob.glob("px_data/*.parquet")
    if not parq:
        print("no px data"); return
    q = f"""
    WITH px AS (
      SELECT symbol, date, close, LEAD(close, 22) OVER (PARTITION BY symbol ORDER BY date) AS fwd22
      FROM read_parquet('{DB}', hive_partitioning=false) WHERE date >= current_date - INTERVAL 5 YEAR
    ), lastd AS (SELECT max(date) AS d FROM px GROUP BY substr(CAST(date AS VARCHAR),1,7))
    SELECT f.symbol, f.date, {', '.join('f.'+c for c in F)}, p.fwd22, p.close
    FROM factors_wide f JOIN px p USING (symbol, date) WHERE f.date IN (SELECT d FROM lastd)
    """
    try:
        df = con.execute(q).fetchdf()
    except Exception as e:
        print("audit needs factor matrix wiring:", e); return
    df["fwd22"] = df["fwd22"]/df["close"] - 1
    df["ym"] = df["date"].astype(str).str[:7]
    prev, nets = set(), []
    for ym, g in df.groupby("ym"):
        g = g.dropna(subset=["fwd22"])
        if len(g) < 500: continue
        g = g.copy()
        g["score"] = sum(g[f].rank(pct=True)-0.5 for f in F if f in g)/max(1,len([f for f in F if f in g]))
        sel = g.nsmallest(50, "score")
        s = set(sel["symbol"])
        to = 1 - len(s & prev)/50 if prev else 1.0
        prev = s
        nets.append(((sel["fwd22"].mean()-g["fwd22"].mean())*100 - to*0.7, ym))
    if not nets: return
    avg = sum(n for n,_ in nets)/len(nets)
    out = {"ts": datetime.datetime.utcnow().isoformat(), "net_monthly": round(avg,3),
           "net_yearly": round(avg*12,3), "n_months": len(nets),
           "last_6m": nets[-6:], "healthy": avg > 0.2}
    os.makedirs("data", exist_ok=True)
    json.dump(out, open("data/honest_audit.json","w"), indent=1)
    print(json.dumps(out, indent=1))
    if not out["healthy"]:
        print("!! DRIFT ALERT: v3 net excess below 0.2%/月")

if __name__ == "__main__":
    main()
