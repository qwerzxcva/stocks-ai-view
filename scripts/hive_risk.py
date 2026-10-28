"""hive_risk.py — E8 风险域 (一票否决, 权重最高)
策略1 crowding_veto: 拥挤度 (近5日涨停/巨振票否决追高)
策略2 drawdown_guard: 近20日跌幅>25% 的票 (接刀风险) 否决
策略3 st_liquidity_veto: ST/流动性枯竭 (20日均额<3000万) 否决
"""
import pandas as pd


def run(ctx):
    px = ctx["px"]
    last_d = px.d.max()
    last = px[px.d == last_d].set_index("sym")
    g = px.groupby("sym")
    sig = []
    # 名称判断 ST 需要行情快照, 这里用保守规则: 20日均额 + 回撤 + 拥挤
    amt20 = g.apply(lambda t: (t.c * t.v).iloc[-20:].mean() if len(t) >= 20 else 0)
    ret20 = g["c"].apply(lambda s: s.iloc[-1] / s.iloc[-21] - 1 if len(s) >= 21 else 0)
    # 拥挤: 近5日涨幅 > 20%
    ret5 = g["c"].apply(lambda s: s.iloc[-1] / s.iloc[-6] - 1 if len(s) >= 6 else 0)

    n = 0
    for sym in set(amt20.index):
        vetoes = []
        if amt20.get(sym, 0) < 3e7:
            vetoes.append("流动性枯竭")
        if ret20.get(sym, 0) < -0.25:
            vetoes.append("20日深跌25%")
        if ret5.get(sym, 0) > 0.20:
            vetoes.append("5日涨20%拥挤")
        if vetoes:
            sig.append({"sym": sym, "score": 0, "conf": 1.0, "veto": True,
                        "strategy": "risk_veto", "why": "+".join(vetoes)})
            n += 1
    return {"engine": "E8", "domain": "risk", "signals": sig,
            "meta": {"n_veto": n}}
