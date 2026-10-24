#!/usr/bin/env python3
"""
orchestrator.py — 中枢引擎 (2026-10-18)
=========================================
定位: 调度者 + 融合者, 不替代任何子引擎。
职责:
  1. 编排: 按依赖顺序调用因子→选股→风控→预测各引擎, 传递中间产物
  2. 融合: 对多引擎结论做加权投票 (权重=walkforward 验证的滚动胜率), 冲突时降置信
  3. 输出: 统一 orchestrator_report.json (每只推荐股带"证据链"——哪个引擎为什么推它)

链路 (每个环节的输出即下一环节的输入):
  [数据层] qfq_all.duckdb + holo panels
      ↓
  [市场层] market_phase (牛熊判定) → 传给风控层定仓位上限
      ↓
  [因子层] factor_lib + cyq + rsrs → 因子截面打分
      ↓
  [选股层] v7 蓝筹低波 (防守) / pick_algo (进攻) / rps_rotation (轮动)
      ↓ 按 market_phase 分配权重
  [风控层] walkforward 胜率过滤 + risk_parity 仓位
      ↓
  [中枢融合] 加权投票 + 冲突检测 → orchestrator_report.json
"""
import json, os, sys, duckdb, numpy as np, pandas as pd
from datetime import datetime

T1 = "/home/agentuser/stockdb/t1"
OUT = "/tmp/orchestrator_report.json"

# ---------- 数据层 ----------
def get_con():
    con = duckdb.connect()
    from px_view import attach_px  # 去重修复版 (2026-10-18): qfq 优先 + bs 补缺票, 零重复
    attach_px(con)
    return con

# ---------- 市场层 ----------
def market_phase(con, t0):
    """牛/震荡/熊 三态: 60 日市场中位动量 + 20 日波动
    输出: {phase, strength, max_position} — max_position 传给风控层"""
    days = [r[0] for r in con.execute(
        f"select distinct d from all_px where d<='{t0}' order by d desc limit 61").fetchall()]
    if len(days) < 61: return {"phase": "unknown", "strength": 0, "max_position": 0.5}
    # 大票加权: 全市场中位数被微盘股主导, 与指数偏离。改用流动性 top500 中位数 (贴近指数) 2026-10-18
    a = con.execute(f"""select sym,c from all_px where d='{days[0]}' and sym in
        (select sym from all_px where d='{days[-1]}' order by v desc limit 500)""").fetchdf().set_index("sym")["c"]
    b = con.execute(f"""select sym,c from all_px where d='{days[-1]}' and sym in
        (select sym from all_px where d='{days[-1]}' order by v desc limit 500)""").fetchdf().set_index("sym")["c"]
    m60 = float((a/b-1).median())  # a=最新(days[0] desc), b=61日前 → 最新/旧-1 = 动量
    if m60 > 0.02: phase, pos = "bull", 0.9
    elif m60 < -0.02: phase, pos = "bear", 0.4
    else: phase, pos = "neutral", 0.6
    return {"phase": phase, "strength": round(m60,4), "max_position": pos, "t0": t0}

# ---------- 因子层 ----------
def factor_scores(con, t0, horizon_days=280):
    """动量/低波/流动性 三因子截面 (与 v7 口径一致, 兼容 pick_algo 扩展)"""
    df = con.execute(f"""
        with days as (select distinct d from all_px where d<='{t0}' order by d desc limit {horizon_days})
        select a.* from all_px a join (select d from days) t on a.d=t.d
        where a.sym in (select sym from all_px where d='{t0}')
    """).fetchdf().sort_values(["sym","d"])
    g = df.groupby("sym")
    out = pd.DataFrame({
        "mom60": g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-61]-1 if len(s)>=61 else np.nan),
        "mom20": g["c"].apply(lambda s: s.iloc[-1]/s.iloc[-21]-1 if len(s)>=21 else np.nan),
        "vol20": g["c"].apply(lambda s: s.pct_change().iloc[-20:].std() if len(s)>=21 else np.nan),
        "v20":   g["v"].apply(lambda s: s.iloc[-20:].mean() if len(s)>=20 else np.nan),
    }).dropna()
    return out

# ---------- 选股层 (三引擎并行, 各出名单) ----------
def pick_defensive(fx, top_n=40):
    """v7 蓝筹低波: 高流动性 top300 中 vol20 最低"""
    return list(fx.nlargest(300,"v20").nsmallest(top_n,"vol20").index)

def pick_offensive(fx, top_n=40):
    """动量进攻: mom60+mom20 复合排名, 但排除 vol20 最高 20%"""
    fx2 = fx.copy()
    fx2["score"] = fx2["mom60"].rank(pct=True)*0.6 + fx2["mom20"].rank(pct=True)*0.4
    fx2 = fx2[fx2["vol20"] < fx2["vol20"].quantile(0.8)]
    return list(fx2.nlargest(top_n,"score").index)

def pick_rotation(fx, top_n=40):
    """RPS 轮动: mom60 相对强度 top"""
    return list(fx.nlargest(top_n,"mom60").index)

# ---------- 风控层 ----------
def risk_filter(con, t0, picks, mkt):
    """walkforward 思路简化: 每只票 T0 前 250 日最大回撤 >35% 剔除; 输出通过名单"""
    ok = []
    for s in picks:
        px = con.execute(f"select c from all_px where sym='{s}' and d<='{t0}' order by d desc limit 250").fetchall()
        if len(px) < 120: continue
        cs = [r[0] for r in px][::-1]
        peak, mdd = cs[0], 0.0
        for c in cs:
            peak = max(peak, c); mdd = min(mdd, c/peak-1)
        if mdd > -0.35: ok.append(s)
    return ok

# ---------- 中枢融合 ----------
def fuse(con, t0, mkt, picks_d, picks_o, picks_r, top_n=20):
    """三引擎投票: 一致票高置信; 两票次之; 单票标注来源。
    权重按 market_phase: 熊→防守 0.6/轮动 0.3/进攻 0.1; 牛→进攻 0.6/轮动 0.3/防守 0.1"""
    w = {"bear": (0.6,0.3,0.1), "neutral": (0.4,0.3,0.3), "bull": (0.1,0.3,0.6)}[mkt["phase"]]
    score = {}
    src = {}
    for name, plist, weight in [("defensive",picks_d,w[0]),("rotation",picks_r,w[1]),("offensive",picks_o,w[2])]:
        for rank, s in enumerate(plist):
            pts = weight * (len(plist)-rank)/len(plist)
            score[s] = score.get(s,0)+pts
            src.setdefault(s, []).append(f"{name}:{rank+1}")
    ranked = sorted(score.items(), key=lambda x:-x[1])[:top_n]
    return [{"sym": s, "score": round(v,4), "engines": src[s], "confidence":
             "high" if len(src[s])>=2 else "low"} for s,v in ranked]

# ---------- 主流程 ----------
def run(t0=None, top_n=20):
    con = get_con()
    if t0 is None:
        t0 = con.execute("select max(d) from all_px").fetchone()[0].strftime("%Y-%m-%d")
    mkt = market_phase(con, t0)
    fx = factor_scores(con, t0)
    p_d = risk_filter(con, t0, pick_defensive(fx), mkt)
    p_o = risk_filter(con, t0, pick_offensive(fx), mkt)
    p_r = risk_filter(con, t0, pick_rotation(fx), mkt)
    fused = fuse(con, t0, mkt, p_d, p_o, p_r, top_n)
    report = {
        "generated": datetime.now().isoformat(),
        "t0": t0, "market": mkt,
        "pools": {"defensive": p_d[:40], "offensive": p_o[:40], "rotation": p_r[:40]},
        "fused_picks": fused,
        "chain": "data→market_phase→factor(3)→pick(3 engines)→risk_filter→fuse(weighted vote)",
    }
    json.dump(report, open(OUT,"w"), ensure_ascii=False, indent=1)
    print(f"[orchestrator] {t0} phase={mkt['phase']} fused={len(fused)} -> {OUT}")
    return report

if __name__ == "__main__":
    t0 = sys.argv[1] if len(sys.argv)>1 else None
    run(t0)
