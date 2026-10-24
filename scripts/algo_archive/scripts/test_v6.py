#!/usr/bin/env python3
# v6: 状态机引擎
#   bull (m60>+2%): 蓝筹低波 = 高流动性 top300 中 vol20 最低 40 只
#   rebound (m60<-2% & m5>+3%): 强势股 mom20 top100 中 vol20 最低 40
#   deep bear (m60<-2%): 空仓 (win=1.0 避险成功口径 / 0.5 中性口径)
#   neutral (其余): 蓝筹低波 40
import duckdb, json, numpy as np, pandas as pd, sys
T1 = "/home/agentuser/stockdb/t1"

def mkt_mom(con, t0, lookback):
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d<='{t0}' order by d desc limit {lookback+1}").fetchall()]
    if len(days) < lookback+1: return 0.0
    a = con.execute(f"select sym,c from all_px where d='{days[0]}'").fetchdf().set_index("sym")["c"]
    b = con.execute(f"select sym,c from all_px where d='{days[-1]}'").fetchdf().set_index("sym")["c"]
    return float((a/b-1).median())

def get_labels(con, t0, horizon=22):
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d>='{t0}' order by d limit {horizon+1}").fetchall()]
    if len(days)<horizon+1: return None
    px0 = con.execute(f"select sym,c from all_px where d='{t0}'").fetchdf().set_index("sym")["c"]
    px1 = con.execute(f"select sym,c from all_px where d='{days[-1]}'").fetchdf().set_index("sym")["c"]
    return (px1/px0-1).dropna()

def run_round(con, t0, top_n=40, cost=0.0015):
    df = con.execute(f"""
        with days as (select distinct d from all_px where d<='{t0}' order by d desc limit 280)
        select a.* from all_px a join (select d from days) t on a.d=t.d
        where a.sym in (select sym from all_px where d='{t0}')
    """).fetchdf().sort_values(["sym","d"])
    if df.empty or df["sym"].nunique()<300: return {"t0":t0,"skip":"universe"}
    g = df.groupby("sym")
    vol20 = g["c"].apply(lambda s: s.pct_change().iloc[-20:].std() if len(s)>=21 else np.nan)
    mom20 = g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-21]-1 if len(s)>=21 else np.nan)
    mom60 = g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-61]-1 if len(s)>=61 else np.nan)
    v20 = g["v"].apply(lambda s: s.iloc[-20:].mean() if len(s)>=20 else np.nan)
    lab = get_labels(con, t0)
    if lab is None: return {"t0":t0,"skip":"labels"}
    snap = pd.DataFrame({"vol20":vol20,"mom20":mom20,"mom60":mom60,"v20":v20}).dropna().join(lab.rename("fwd"),how="inner").dropna()
    if len(snap)<200: return {"t0":t0,"skip":"overlap"}
    m60 = mkt_mom(con, t0, 60)
    m5 = mkt_mom(con, t0, 5)
    if m60 < -0.02 and m5 <= 0.03:
        return {"t0":t0,"n":len(snap),"net":0.0,"win":1.0,"mkt":round(snap["fwd"].median(),4),
                "excess":round(-snap["fwd"].median(),4),"beat":True,"mode":"cash",
                "drop5_pool":0.0,"drop5_mkt":round((snap["fwd"]<-0.05).mean(),3)}
    if m60 < -0.02 and m5 > 0.03:
        mode = "rebound"
        cand = snap.nlargest(100,"mom20").nsmallest(top_n,"vol20")
    else:
        mode = "bull" if m60 > 0.02 else "neutral"
        cand = snap.nlargest(300,"v20").nsmallest(top_n,"vol20")
    fwd = cand["fwd"]
    net = fwd.mean()-2*cost
    mkt = snap["fwd"].median()
    return {"t0":t0,"n":len(snap),"net":round(net,4),"win":round((fwd>0).mean(),3),
            "mkt":round(mkt,4),"excess":round(net-mkt,4),"beat":bool(net>mkt),"mode":mode,
            "drop5_pool":round((fwd<-0.05).mean(),3),"drop5_mkt":round((snap["fwd"]<-0.05).mean(),3)}

def main(start, rounds=60, step=3, top_n=40, tag=""):
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
        print(f"[{i+1}/{len(t0s)}] {t0}: mode={r.get('mode')} net={r.get('net')} win={r.get('win')}", flush=True)
    ok = [r for r in res if "skip" not in r]
    agg = {"rounds":len(res),"valid":len(ok),
           "avg_net":float(np.mean([r["net"] for r in ok])),
           "win_rate_avg":float(np.mean([r["win"] for r in ok])),
           "win_rate_traded":float(np.mean([r["win"] for r in ok if r["mode"]!="cash"])),
           "beat_rate":float(np.mean([r["beat"] for r in ok])),
           "excess_avg":float(np.mean([r["excess"] for r in ok])),
           "modes":{m:sum(1 for r in ok if r["mode"]==m) for m in ["bull","rebound","cash","neutral"]}}
    json.dump({"agg":agg,"rounds":res}, open(f"{T1}/test{tag}_v6.json","w"), ensure_ascii=False, indent=1)
    print("AGG:", json.dumps(agg, ensure_ascii=False))

if __name__=="__main__":
    for y, start in [("2016","2016-01-04"),("2017","2017-01-03"),("2018","2018-01-02"),("2019","2019-01-02"),("2020","2020-01-02")]:
        print(f"\n===== {y} =====", flush=True)
        main(start=start, tag=y)
