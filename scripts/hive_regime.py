"""hive_regime.py — E3 市场状态域 (只看全市场聚合, 不看个股)
策略1 phase_machine: 20日市场动量+波动 → 冰点/修复/发酵/亢奋 相位机
策略2 breadth_pulse: 涨跌家数比 (今日 c>o 占比)
策略3 limitup_ecology: 涨停/跌停生态 (ret>9.5% 计数)
输出: signals(域内空, 不选股) + meta.max_position 全局仓位
"""
import pandas as pd


def run(ctx):
    px = ctx["px"]
    days = sorted(px.d.unique())
    last = px[px.d == days[-1]].set_index("sym")
    # S1 相位机: 全市场等权20日动量 + 20日日收益波动
    mkt = px.groupby("d").apply(lambda t: (t.c.sum() / t.o.sum() - 1)).dropna()
    m20 = float(mkt.iloc[-20:].sum())
    vol20 = float(mkt.iloc[-20:].std())
    if m20 > 0.03 and vol20 < mkt.std():
        phase, pos = "发酵", 0.7
    elif m20 > 0.01:
        phase, pos = "修复", 0.55
    elif m20 < -0.03:
        phase, pos = "冰点", 0.3
    else:
        phase, pos = "震荡", 0.45
    # S2 广度
    adv = float((last.c > last.o).mean())
    # S3 涨停生态
    ret_day = last.c / last.o - 1
    zt = float((ret_day > 0.095).mean() * 1000)   # ‰
    # 综合仓位微调
    if adv > 0.55: pos = min(pos + 0.1, 0.9)
    if adv < 0.35: pos = max(pos - 0.1, 0.2)
    return {"engine": "E3", "domain": "regime", "signals": [],
            "meta": {"phase": phase, "m20": round(m20, 4), "advance_ratio": round(adv, 3),
                     "limitup_permille": round(zt, 1), "max_position": pos,
                     "note": "全局仓位与相位, 不选个股"}}
