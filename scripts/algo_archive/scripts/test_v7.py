#!/usr/bin/env python3
# v7: 全程持仓引擎 (不空仓), 用户中性口径:
#   win = 持有期内曾回本 (c >= 买入价), 即"亏了之后涨回来没亏本"
#   持仓 = 蓝筹低波 40 只 (高流动性 top300 中 vol20 最低)
#   每轮 T0 买入, 跟踪 22 交易日; 报告 recoup22 / recoup60 / 期末净收益 / vs 市场
import duckdb, json, numpy as np, pandas as pd, sys
T1 = "/home/agentuser/stockdb/t1"
COST = 0.003  # 双边合计

def get_con():
    con = duckdb.connect()
    from px_view import attach_px
    attach_px(con)
    con.execute("create table px_mat as select * from all_px")  # 物化加速 (2026-10-18)
    con.execute("drop view all_px")
    con.execute("create view all_px as select * from px_mat")
    return con

def pick(con, t0, top_n=40):
    hist = con.execute(f"""
        with d20 as (select distinct d from all_px where d<='{t0}' order by d desc limit 21),
        ret as (select sym, d, c, c/lag(c) over (partition by sym order by d)-1 r
                from all_px where d in (select d from d20)),
        agg as (select ret.sym, avg(ret.c) v20, stddev_pop(ret.r) vol, count(*) n
                from ret group by ret.sym)
        select sym, v20, vol from agg where n>=20
    """).fetchdf().dropna()
    return hist.nlargest(300,"v20").nsmallest(top_n,"vol")["sym"].tolist()

def round_stats(con, t0, top_n=40):
    t0px = con.execute(f"select sym, c from all_px where d='{t0}'").fetchdf().set_index("sym")["c"]
    blue = [s for s in pick(con, t0, top_n) if s in t0px.index]
    rec22 = rec60 = 0; finals = []; mkts = []
    # 市场基准 (全票中位 22 日收益)
    d22 = con.execute(f"select distinct d from all_px where d>'{t0}' order by d limit 22").fetchall()
    if len(d22) < 22: return None
    end22 = d22[21][0]
    mkt = con.execute(f"""
        with p0 as (select sym,c from all_px where d='{t0}'),
        p1 as (select sym,c from all_px where d='{end22}')
        select median(p1.c/p0.c-1) m from p1 join p0 using(sym)
    """).fetchone()[0]
    for s in blue:
        c0 = t0px[s]
        px = con.execute(f"select d,c from all_px where sym='{s}' and d>'{t0}' order by d limit 60").fetchall()
        if len(px) < 22: continue
        rec = None
        for i,(d,c) in enumerate(px[:22],1):
            if c >= c0 and rec is None: rec = i
        if rec: rec22 += 1
        rec60_any = any(c >= c0 for d,c in px)
        if rec60_any: rec60 += 1
        finals.append(px[21][1]/c0-1-COST)  # 22 日期末净收益
    n = len(finals)
    if n < 30: return None
    return {"t0": t0, "n": n,
            "recoup22": round(rec22/n,3), "recoup60": round(rec60/n,3),
            "final_mean": round(float(np.mean(finals)),4),
            "final_win": round(float(np.mean([f>=0 for f in finals])),3),
            "mkt": round(float(mkt),4),
            "beat": bool(np.mean(finals) > mkt)}

def main(tag, start, rounds=60, step=3, top_n=40):
    con = get_con()
    days = [r[0] for r in con.execute(f"select distinct d from all_px where d>='{start}' order by d").fetchall()]
    t0s = days[::step][:rounds]
    res = []
    for i, t0 in enumerate(t0s):
        r = round_stats(con, t0, top_n)
        if r is None:
            print(f"[{i+1}/{len(t0s)}] {t0}: skip", flush=True); continue
        res.append(r)
        print(f"[{i+1}/{len(t0s)}] {t0}: recoup22={r['recoup22']:.0%} recoup60={r['recoup60']:.0%} final={r['final_mean']:+.3f} finalwin={r['final_win']:.0%} beat={r['beat']}", flush=True)
    agg = {"rounds": len(res),
           "recoup22_mean": round(float(np.mean([r["recoup22"] for r in res])),4),
           "recoup60_mean": round(float(np.mean([r["recoup60"] for r in res])),4),
           "final_mean": round(float(np.mean([r["final_mean"] for r in res])),4),
           "final_win_mean": round(float(np.mean([r["final_win"] for r in res])),4),
           "beat_rate": round(float(np.mean([r["beat"] for r in res])),4),
           "round_recoup22_ge50": sum(1 for r in res if r["recoup22"]>=0.5),
           "round_recoup60_ge50": sum(1 for r in res if r["recoup60"]>=0.5)}
    json.dump({"agg": agg, "rounds": res}, open(f"{T1}/test{tag}_v7.json","w"), ensure_ascii=False, indent=1, default=str)
    print("AGG:", json.dumps(agg, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    for y, start in [("2016","2016-01-04"),("2017","2017-01-03"),("2018","2018-01-02"),("2019","2019-01-02"),("2020","2020-01-02")]:
        print(f"===== {y} =====", flush=True)
        main(y, start)
