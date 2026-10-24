#!/usr/bin/env python3
# 2016 假设测试 v2: 时点训练 (rolling rankIC 权重) — 只用 T0 前数据
import duckdb, json, numpy as np, pandas as pd, sys
sys.path.insert(0, "/home/agentuser/stockdb/t1")

T1 = "/home/agentuser/stockdb/t1"
FACTORS = ["mom5","mom20","mom60","ma20_bias","vol20","volr","pos60","amp20"]

def get_snap(con, t0):
    df = con.execute(f"""
        with days as (
            select distinct d from all_px where d <= '{t0}' order by d desc limit 280
        )
        select a.* from all_px a
        join (select d from days) t on a.d = t.d
        where a.sym in (select sym from all_px where d = '{t0}')
    """).fetchdf()
    if df.empty: return None
    df = df.sort_values(["sym","d"])
    g = df.groupby("sym")
    out = pd.DataFrame({"sym": g.tail(1)["sym"].values, "d": g.tail(1)["d"].values}, index=g.tail(1)["sym"].values)
    c = df.groupby("sym")["c"]
    last = g.tail(1).set_index("sym")
    out.index = out["sym"]
    def shift_ret(s, n):
        return s / s.shift(n) - 1
    # 向量化因子: 每票最后行
    grp = df.groupby("sym")
    c20 = grp["c"].apply(lambda s: s.iloc[-1]/s.iloc[-21]-1 if len(s)>=21 else np.nan)
    c5  = grp["c"].apply(lambda s: s.iloc[-1]/s.iloc[-6]-1 if len(s)>=6 else np.nan)
    c60 = grp["c"].apply(lambda s: s.iloc[-1]/s.iloc[-61]-1 if len(s)>=61 else np.nan)
    ma20 = grp["c"].apply(lambda s: s.iloc[-1]/s.rolling(20).mean().iloc[-1]-1 if len(s)>=20 else np.nan)
    vol20 = grp["c"].apply(lambda s: s.pct_change().iloc[-20:].std() if len(s)>=21 else np.nan)
    v5 = grp["v"].apply(lambda s: s.iloc[-1]/s.iloc[-5:].mean()-1 if len(s)>=5 else np.nan)
    hi60 = grp["h"].apply(lambda s: s.iloc[-60:].max() if len(s)>=60 else np.nan)
    lo60 = grp["l"].apply(lambda s: s.iloc[-60:].min() if len(s)>=60 else np.nan)
    pos60 = (last["c"] - lo60)/(hi60-lo60+1e-9)
    amp = vol20*np.sqrt(252)
    snap = pd.DataFrame({
        "mom5": c5, "mom20": c20, "mom60": c60, "ma20_bias": ma20,
        "vol20": vol20, "volr": v5, "pos60": pos60, "amp20": amp,
    })
    snap["d"] = t0
    return snap.dropna()

def get_labels(con, t0, horizon=22):
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d >= '{t0}' order by d limit {horizon+1}").fetchall()]
    if len(days) < horizon+1: return None
    end = days[-1]
    px0 = con.execute(f"select sym, c from all_px where d='{t0}'").fetchdf().set_index("sym")["c"]
    px1 = con.execute(f"select sym, c from all_px where d='{end}'").fetchdf().set_index("sym")["c"]
    return (px1/px0-1).dropna()

def train_weights(con, t0, train_days=500, n_sub=8):
    """在 [t0-train_days, t0) 内做 n_sub 个子截面 rankIC → 因子权重 = 正IC占比"""
    days = [r[0] for r in con.execute(
        f"select distinct d from all_px where d < '{t0}' order by d desc limit {train_days}").fetchall()]
    days = sorted(days)
    if len(days) < 300: return None
    step = max(1, len(days)//(n_sub+1))
    sample_days = [days[-(i*step)-1] for i in range(1, n_sub+1)]
    ic_pos = {f: 0 for f in FACTORS}
    ic_all = {f: [] for f in FACTORS}
    for sd in sample_days:
        snap = get_snap(con, sd)
        if snap is None or len(snap) < 200: continue
        lab = get_labels(con, sd)
        if lab is None or len(lab) < 100: continue
        m = snap.join(lab.rename("y"), how="inner").dropna()
        if len(m) < 150: continue
        for f in FACTORS:
            if f == "amp20": continue
            ic = m[f].rank(pct=True).corr(m["y"].rank(pct=True))
            ic_all[f].append(ic)
    if not any(len(v)>=3 for v in ic_all.values()): return None
    w = {}
    for f in FACTORS[:-1]:
        arr = ic_all[f]
        if len(arr) < 3: w[f] = 0.0
        else:
            mean_ic = np.mean(arr)
            # 低值做多: 若 mean_ic>0 → 该因子高值对应高收益 → 我们要低值 → 权重 = -mean_ic 方向
            w[f] = -mean_ic
    # amp20 与 vol20 共线, 权重给 0
    w["amp20"] = 0.0
    s = sum(abs(v) for v in w.values()) or 1.0
    return {k: v/s for k, v in w.items()}

def run_round(con, t0, top_n=50, cost=0.0015):
    snap = get_snap(con, t0)
    if snap is None or len(snap) < 300: return {"t0": t0, "skip": "universe"}
    lab = get_labels(con, t0)
    if lab is None or len(lab) < 100: return {"t0": t0, "skip": "labels"}
    df = snap.join(lab.rename("fwd"), how="inner").dropna()
    if len(df) < 200: return {"t0": t0, "skip": "overlap"}
    w = train_weights(con, t0)
    if w is None:
        # 回退: 默认低波低位置
        w = {"mom5":0,"mom20":0,"mom60":0.2,"ma20_bias":0.15,"vol20":0.25,"volr":0.1,"pos60":0.3,"amp20":0}
    score = sum(w[f]*df[f].rank(pct=True) for f in FACTORS)
    df["score"] = score
    picks = df.nsmallest(top_n, "score")
    fwd = picks["fwd"]
    net = fwd.mean() - 2*cost
    mkt = df["fwd"].median()
    return {"t0": t0, "n": len(df), "net": round(net,4), "win": round((fwd>0).mean(),3),
            "mkt": round(mkt,4), "excess": round(net-mkt,4), "beat": bool(net>mkt),
            "drop5_pool": round((fwd<-0.05).mean(),3), "drop5_mkt": round((df["fwd"]<-0.05).mean(),3),
            "w": {k: round(v,2) for k,v in w.items() if abs(v)>0.01}}

def main(start="2016-01-04", rounds=60, step=3, top_n=50):
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
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d >= '{start}' order by d").fetchall()]
    t0s = days[::step][:rounds]
    res = []
    for i, t0 in enumerate(t0s):
        r = run_round(con, t0, top_n=top_n)
        res.append(r)
        print(f"[{i+1}/{len(t0s)}] {t0}: net={r.get('net')} win={r.get('win')} excess={r.get('excess')} beat={r.get('beat')}", flush=True)
    ok = [r for r in res if "skip" not in r]
    agg = {"rounds": len(res), "valid": len(ok),
           "avg_net": float(np.mean([r["net"] for r in ok])) if ok else None,
           "win_rate_avg": float(np.mean([r["win"] for r in ok])) if ok else None,
           "beat_rate": float(np.mean([r["beat"] for r in ok])) if ok else None,
           "excess_avg": float(np.mean([r["excess"] for r in ok])) if ok else None}
    json.dump({"agg": agg, "rounds": res}, open(f"{T1}/test2016_v2.json","w"), ensure_ascii=False, indent=1)
    print("AGG:", json.dumps(agg, ensure_ascii=False))

if __name__ == "__main__":
    main()
