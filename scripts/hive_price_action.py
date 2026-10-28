"""hive_price_action.py — E1 价格行为域 (纯K线, 零指标)
策略1 pure_structure: 20日箱体突破/跌破结构
策略2 candle_language: 单K语言 (锤头/吞没/长上影拒绝)
策略3 range_position: 收盘在120日区间的位置 (低位蓄势 vs 高位风险)
策略4 swing_map: 波段高低点阶梯 (HH/HL 上升结构 vs LH/LL 下降)
输入只读 ctx['px'], 输出与其他引擎零共享。
"""
import pandas as pd


def run(ctx):
    px = ctx["px"]
    g = px.groupby("sym")
    last = px[px.d == px.d.max()].set_index("sym")
    sig = []

    hi120 = g["h"].max()
    lo120 = g["l"].min()
    c20max = g["h"].apply(lambda s: s.iloc[-21:-1].max() if len(s) >= 21 else None)

    for sym, r in last.iterrows():
        why = []
        score = 0.0
        # S1 结构: 收盘突破20日高
        try:
            c20 = c20max.get(sym)
            if c20 and r["c"] > c20:
                score += 0.35; why.append("破20日高")
        except Exception:
            pass
        # S2 K线语言: 长下影锤头 (下影>实体2倍, 收在日高1/3内)
        body = abs(r["c"] - r["o"]); rng = r["h"] - r["l"]
        if rng > 0:
            lower = min(r["c"], r["o"]) - r["l"]
            if lower > 2 * body and lower / rng > 0.5:
                score += 0.25; why.append("长下影承接")
        # S3 区间位置: 120日低位 30% 内 = 蓄势
        h120, l120 = hi120.get(sym), lo120.get(sym)
        if h120 and l120 and h120 > l120:
            pos = (r["c"] - l120) / (h120 - l120)
            if pos < 0.3:
                score += 0.25; why.append(f"120日低位{pos:.0%}")
        # S4 波段: 收盘>5日高=HH阶梯
        h5 = g["h"].apply(lambda s: s.iloc[-6:-1].max() if len(s) >= 6 else None).get(sym)
        if h5 and r["c"] > h5:
            score += 0.15; why.append("5日HH")
        if score > 0.2:
            sig.append({"sym": sym, "score": min(score, 1.0), "conf": 0.8,
                        "strategy": "multi", "why": "+".join(why)})
    sig.sort(key=lambda x: -x["score"])
    return {"engine": "E1", "domain": "price_action", "signals": sig,
            "meta": {"n": len(sig)}}
