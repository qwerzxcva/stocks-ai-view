#!/usr/bin/env python3
# v3: v2 + 市场 regime 过滤 + top30 + 波动风控
import duckdb, json, numpy as np, pandas as pd
T1 = "/home/agentuser/stockdb/t1"
FACTORS = ["mom5","mom20","mom60","ma20_bias","vol20","volr","pos60"]

def get_snap(con, t0, lookback=280):
    df = con.execute(f"""
        with days as (select distinct d from all_px where d <= '{t0}' order by d desc limit {lookback})
        select a.* from all_px a join (select d from days) t on a.d=t.d
        where a.sym in (select sym from all_px where d='{t0}')
    """).fetchdf()
    if df.empty: return None
    g = df.sort_values(["sym","d"]).groupby("sym")
    last = g.tail(1).set_index("sym")
    c5  = g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-6]-1 if len(s)>=6 else np.nan)
    c20 = g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-21]-1 if len(s)>=21 else np.nan)
    c60 = g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-61]-1 if len(s)>=61 else np.nan)
    ma20 = g["c"].apply(lambda s: s.iloc[-1]/s.rolling(20).mean().iloc[-1]-1 if len(s)>=20 else np.nan)
    vol20 = g["c"].apply(lambda s: s.pct_change().iloc[-20:].std() if len(s)>=21 else np.nan)
    v5 = g["v"].apply(lambda s: s.iloc[-1]/s.iloc[-5:].mean()-1 if len(s)>=5 else np.nan)
    hi60 = g["h"].apply(lambda s: s.iloc[-60:].max() if len(s)>=60 else np.nan)
    lo60 = g["l"].apply(lambda s: s.iloc[-60:].min() if len(s)>=60 else np.nan)
    snap = pd.DataFrame({"mom5":c5,"mom20":c20,"mom60":c60,"ma20_bias":ma20,
                         "vol20":vol20,"volr":v5,"pos60":(last["c"]-lo60)/(hi60-lo60+1e-9)})
    return snap.dropna()

def get_labels(con, t0, horizon=22):
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d>='{t0}' order by d limit {horizon+1}").fetchall()]
    if len(days)<horizon+1: return None
    px0 = con.execute(f"select sym,c from all_px where d='{t0}'").fetchdf().set_index("sym")["c"]
    px1 = con.execute(f"select sym,c from all_px where d='{days[-1]}'").fetchdf().set_index("sym")["c"]
    return (px1/px0-1).dropna()

def train(con, t0, train_days=500, n_sub=8):
    days = sorted([r[0] for r in con.execute(f"select distinct d from all_px where d<'{t0}' order by d desc limit {train_days}").fetchall()])
    if len(days)<300: return None
    step = max(1,len(days)//(n_sub+1))
    sample = [days[-(i*step)-1] for i in range(1,n_sub+1)]
    ic_all = {f:[] for f in FACTORS}
    for sd in sample:
        snap = get_snap(con, sd)
        if snap is None or len(snap)<200: continue
        lab = get_labels(con, sd)
        if lab is None or len(lab)<100: continue
        m = snap.join(lab.rename("y"), how="inner").dropna()
        if len(m)<150: continue
        yr = m["y"].rank(pct=True)
        for f in FACTORS:
            ic_all[f].append(m[f].rank(pct=True).corr(yr))
    w = {}
    for f in FACTORS:
        arr = ic_all[f]
        w[f] = -np.mean(arr) if len(arr)>=3 else 0.0
    s = sum(abs(v) for v in w.values()) or 1.0
    return {k:v/s for k,v in w.items()}

def market_regime(con, t0, look=120):
    """市场指数动量: 用全市场中位 60d 动量; 负 → risk-off"""
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d<='{t0}' order by d desc limit {look+61}").fetchall()]
    if len(days) < 70: return 0.0
    d_end, d_start = days[0], days[-61]
    m_end = con.execute(f"select sym, c from all_px where d='{d_end}'").fetchdf().set_index("sym")["c"]
    m_start = con.execute(f"select sym, c from all_px where d='{d_start}'").fetchdf().set_index("sym")["c"]
    mom = (m_end/m_start-1)
    return float(mom.median())  # >0 上行, <0 下行

def run_round(con, t0, top_n=30, cost=0.0015):
    snap = get_snap(con, t0)
    if snap is None or len(snap)<300: return {"t0":t0,"skip":"universe"}
    lab = get_labels(con, t0)
    if lab is None or len(lab)<100: return {"t0":t0,"skip":"labels"}
    df = snap.join(lab.rename("fwd"), how="inner").dropna()
    if len(df)<200: return {"t0":t0,"skip":"overlap"}
    w = train(con, t0) or {"mom5":0,"mom20":0,"mom60":0.2,"ma20_bias":0.15,"vol20":0.25,"volr":0.1,"pos60":0.3}
    reg = market_regime(con, t0)
    score = sum(w[f]*df[f].rank(pct=True) for f in FACTORS)
    df["score"] = score
    # regime 过滤: 下行市 → 只选 score 最低一半中 vol20 最低的 (防守)
    if reg < 0:
        cand = df.nsmallest(len(df)//2, "score")
        picks = cand.nsmallest(top_n, "vol20")
        mode = "defensive"
    else:
        picks = df.nsmallest(top_n, "score")
        mode = "normal"
    fwd = picks["fwd"]
    net = fwd.mean()-2*cost
    mkt = df["fwd"].median()
    return {"t0":t0,"n":len(df),"net":round(net,4),"win":round((fwd>0).mean(),3),
            "mkt":round(mkt,4),"excess":round(net-mkt,4),"beat":bool(net>mkt),
            "mode":mode,"reg":round(reg,4),
            "drop5_pool":round((fwd<-0.05).mean(),3),"drop5_mkt":round((df["fwd"]<-0.05).mean(),3)}

def main(start="2016-01-04", rounds=60, step=3, top_n=30):
    con = duckdb.connect()
    con.execute(f"attach '{T1}/qfq_all.duckdb' as t1 (read_only)")
    con.execute(f"""
        create view all_px as
        select sym, d, o, c, h, l, v from t1.qfq
        union all
        select replace(code,'.','') sym, date d, cast(open as double) o, cast(close as double) c,
               cast(high as double) h, cast(low as double) l, cast(volume as double) v
        from read_parquet('{T1}/bs_qfq/*.parquet') where tradestatus='1'
    """)
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d>='{start}' order by d").fetchall()]
    t0s = days[::step][:rounds]
    res = []
    for i, t0 in enumerate(t0s):
        r = run_round(con, t0, top_n=top_n)
        res.append(r)
        print(f"[{i+1}/{len(t0s)}] {t0}: net={r.get('net')} win={r.get('win')} excess={r.get('excess')} beat={r.get('beat')} mode={r.get('mode')}", flush=True)
    ok = [r for r in res if "skip" not in r]
    agg = {"rounds":len(res),"valid":len(ok),
           "avg_net":float(np.mean([r["net"] for r in ok])) if ok else None,
           "win_rate_avg":float(np.mean([r["win"] for r in ok])) if ok else None,
           "beat_rate":float(np.mean([r["beat"] for r in ok])) if ok else None,
           "excess_avg":float(np.mean([r["excess"] for r in ok])) if ok else None,
           "cum":float(np.prod([1+r["net"] for r in ok])-1) if ok else None}
    json.dump({"agg":agg,"rounds":res}, open(f"{T1}/test2016_v3.json","w"), ensure_ascii=False, indent=1)
    print("AGG:", json.dumps(agg, ensure_ascii=False))

if __name__=="__main__":
    main()
