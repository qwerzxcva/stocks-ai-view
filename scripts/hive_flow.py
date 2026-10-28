"""hive_flow.py — E2 资金流域 (量/额, 不看价格形态)
策略1 volume_anomaly: 量比异动 (今日量 vs 20日均量的突变)
策略2 moneyflow_state: 量价方向一致性 (放量上涨=吸筹, 放量下跌=出货)
策略3 turnover_regime: 换手温和区 (1-7% 活跃但不过热)
"""
import pandas as pd


def run(ctx):
    px = ctx["px"]
    g = px.groupby("sym")
    last = px[px.d == px.d.max()].set_index("sym")
    v20 = g["v"].apply(lambda s: s.iloc[-21:-1].mean() if len(s) >= 21 else None)
    v5 = g["v"].apply(lambda s: s.iloc[-6:-1].mean() if len(s) >= 6 else None)
    sig = []
    for sym, r in last.iterrows():
        v20s, v5s = v20.get(sym), v5.get(sym)
        if not v20s or v20s <= 0:
            continue
        vr_today = r["v"] / v20s
        vr_5d = v5s / v20s if v5s else 1.0
        ret = r["c"] / r["o"] - 1 if r["o"] else 0
        score, why = 0.0, []
        # S1 量比异动: 温和放量 1.5-3x (巨量>5x 往往是出货)
        if 1.5 <= vr_today <= 3.0:
            score += 0.3; why.append(f"量比{vr_today:.1f}")
        # S2 量价一致: 放量+收阳
        if vr_today >= 1.3 and ret > 0.01:
            score += 0.3; why.append("放量收阳")
        if vr_today >= 1.3 and ret < -0.01:
            score -= 0.2; why.append("放量收阴(警惕)")
        # S3 换手 regime: 近5日持续活跃
        if 1.1 <= vr_5d <= 2.5:
            score += 0.2; why.append(f"5日持续放量{vr_5d:.1f}")
        if score > 0.25:
            sig.append({"sym": sym, "score": min(max(score, 0), 1.0), "conf": 0.75,
                        "strategy": "flow_mix", "why": "+".join(why)})
    sig.sort(key=lambda x: -x["score"])
    return {"engine": "E2", "domain": "flow", "signals": sig,
            "meta": {"n": len(sig)}}
