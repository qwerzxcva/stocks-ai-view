"""hive_stat_factor.py — E4 统计因子域 (横截面排名, 无预测)
策略1 short_reversal: 5日跌幅最深的 20% 次周反转 (A股月频反转结构)
策略2 low_vol_shelter: 20日波动最低前 20% (低波异象 IC-0.041)
策略3 liquidity_prem: 成交额 top30% 且非巨幅波动 (小盘流动性溢价代理)
策略4 seasonal_edge: 月历边际 (月初/月末/节假日) — 调制而非选票
输出: 每策略独立 rank, 引擎内取均值
"""
import pandas as pd
import numpy as np


def run(ctx):
    px = ctx["px"]
    last_d = px.d.max()
    last = px[px.d == last_d].set_index("sym")
    g = px.groupby("sym")
    # 5日收益
    c5 = g["c"].apply(lambda s: s.iloc[-1] / s.iloc[-6] - 1 if len(s) >= 6 else np.nan)
    # 20日波动
    vol20 = g["c"].apply(lambda s: s.pct_change().iloc[-20:].std() if len(s) >= 21 else np.nan)
    # 20日均额
    amt20 = g.apply(lambda t: (t.c * t.v).iloc[-20:].mean() if len(t) >= 20 else np.nan)

    df = pd.DataFrame({"ret5": c5, "vol20": vol20, "amt20": amt20}).dropna()
    # S1 短反转: ret5 最低 30% 给分 (超跌)
    df["s_rev"] = 1 - df.ret5.rank(pct=True)
    df.loc[df.ret5 > -0.02, "s_rev"] = df["s_rev"] * 0.3   # 跌得不够深的不给满分
    # S2 低波
    df["s_lv"] = 1 - df.vol20.rank(pct=True)
    # S3 流动性: amt20 高的 70 分位以上
    df["s_liq"] = np.where(df.amt20 >= df.amt20.quantile(0.7), 0.7, 0.3)
    # S4 月历: 当月第N交易日 (月初效应)
    dd = pd.Timestamp(last_d)
    df["s_season"] = 0.5 + (0.2 if dd.day <= 5 else 0) - (0.2 if dd.day >= 24 else 0)

    df["score"] = df.s_rev * 0.4 + df.s_lv * 0.3 + df.s_liq * 0.2 + df.s_season * 0.1
    df = df.sort_values("score", ascending=False)
    sig = []
    for sym, r in df.head(200).iterrows():
        why = []
        if r.s_rev > 0.7: why.append("超跌反转")
        if r.s_lv > 0.7: why.append("低波")
        if r.s_liq > 0.6: why.append("流动性")
        sig.append({"sym": sym, "score": round(float(r.score), 3), "conf": 0.6,
                    "strategy": "stat_mix", "why": "+".join(why) or "统计中性"})
    return {"engine": "E4", "domain": "stat_factor", "signals": sig,
            "meta": {"n": len(sig)}}
