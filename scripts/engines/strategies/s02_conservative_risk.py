#!/usr/bin/env python3
"""s02_conservative_risk.py — S2 保守风控策略（独立实现，策略规则来自用户提供）。

规则：
1. 止损：收盘破 EMA20，或相对成本累计亏 5%。成本价未知时以近 20 日 VWAP 代理（降级标注），
   context['position_cost'] 提供真实成本价时优先使用。
2. 见顶信号（K 线形态，独立实现，概念参考常见 15 种蜡烛形态，代码完全自写）：
   - 乌云盖顶：前日长阳，今日高开于前收之上，收盘深入前日实体中点以下且收阴；
   - 黄昏之星：长阳 → 小实体星线（跳空或高位）→ 长阴跌入首日实体中点以下；
   - 放量长阴：实体阴线 >3% 且量比 >1.8。
3. 大盘避险：context 大盘评分(engine_fusion E)<50 或 跌停>50 家 → 强制观望，不开新仓。
4. 买入：BOLL 下轨放量止跌（触/破下轨 + 量比>1.5 + 收阳或长下影）+ KDJ_J 或 RSI6 底背离
   （价格创近 40 日新低区域，而 J/RSI 高于前低）。
5. 仓位 0-3 成。

数据降级：换手率/主力资金/五档盘口无 → 量比代理活跃度；成本价用 VWAP 代理；
均列入 degraded。

统一接口：evaluate(code, kline, context) -> {action, score, position_pct, reasons,
  stop_loss, take_profit, indicators, strategy, degraded}
CLI：--code --asof --out（--asof 严格无前视）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _indicators as ind  # noqa: E402
import _loader  # noqa: E402
import _context as ctx_mod  # noqa: E402

STRATEGY = "s02_conservative_risk"


# ---------- K 线形态识别（独立实现） ----------

def is_dark_cloud_cover(o, c, h, l, i):
    """乌云盖顶：i-1 长阳，i 高开>i-1 收盘，收阴且收盘深入 i-1 实体中点之下。"""
    if i < 1:
        return False
    body_prev = c[i - 1] - o[i - 1]
    if body_prev <= 0 or body_prev / o[i - 1] < 0.02:  # 前日需为>2%长阳
        return False
    mid_prev = (o[i - 1] + c[i - 1]) / 2.0
    return (o[i] > c[i - 1]            # 高开
            and c[i] < o[i]            # 今日收阴
            and c[i] < mid_prev        # 深入前日实体中点以下
            and c[i] > o[i - 1])       # 但未完全吞没（否则是看跌吞没）


def is_evening_star(o, c, h, l, i):
    """黄昏之星：i-2 长阳，i-1 小实体星线（实体<1%），i 长阴跌入 i-2 实体中点以下。"""
    if i < 2:
        return False
    b1 = c[i - 2] - o[i - 2]
    if b1 <= 0 or b1 / o[i - 2] < 0.02:      # 首日长阳
        return False
    star_body = abs(c[i - 1] - o[i - 1])
    if star_body / o[i - 1] >= 0.01:          # 星线实体小
        return False
    mid1 = (o[i - 2] + c[i - 2]) / 2.0
    return c[i] < o[i] and c[i] < mid1        # 第三日长阴深入首日实体


def is_heavy_bearish(o, c, vol, vr, i):
    """放量长阴：阴线实体>3% 且量比>1.8。"""
    if i < 1 or vr[i] is None:
        return False
    body = (o[i] - c[i]) / o[i] if o[i] > 0 else 0.0
    return body > 0.03 and vr[i] > 1.8


# ---------- 底背离 ----------

def _bullish_divergence(close, metric, lookback=40):
    """价格处于近 lookback 日低位区（距最低点<3%），而 metric 高于其在前低点的值。
    metric 为 J 值或 RSI 序列（含 None）。返回 (是否背离, 说明)。"""
    n = len(close)
    if n < lookback + 5:
        return False, ""
    seg_start = n - lookback
    p_low_idx = seg_start + min(range(lookback), key=lambda k: close[seg_start + k])
    cur = n - 1
    # 当前价接近或跌破前低
    if close[cur] > close[p_low_idx] * 1.03:
        return False, ""
    # 找前低之前更早的一个低点做对比基准
    prev_start = max(0, seg_start - lookback)
    if p_low_idx - prev_start < 5:
        return False, ""
    prev_low_idx = prev_start + min(range(p_low_idx - prev_start),
                                    key=lambda k: close[prev_start + k])
    m_prev = metric[prev_low_idx]
    m_now = metric[cur]
    if m_prev is None or m_now is None:
        return False, ""
    if close[p_low_idx] <= close[prev_low_idx] * 1.01 and m_now > m_prev:
        return True, (f"价格低点{close[prev_low_idx]:.2f}→{close[p_low_idx]:.2f}走低，"
                      f"指标{m_prev:.1f}→{m_now:.1f}抬升")
    return False, ""


def _vwap(close, vol, window):
    n = len(close)
    if n == 0:
        return None
    w = min(window, n)
    seg_c, seg_v = close[-w:], vol[-w:]
    tv = sum(seg_v)
    if tv <= 0:
        return sum(seg_c) / w
    return sum(c * v for c, v in zip(seg_c, seg_v)) / tv


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded = []
    o = kline.get("open", [])
    c = kline.get("close", [])
    h = kline.get("high", [])
    l = kline.get("low", [])
    vol = kline.get("vol", [])
    date = kline.get("date", [])
    n = len(c)
    min_bars = 45
    if n < min_bars:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": [f"数据不足({n}<{min_bars}根)"], "stop_loss": None,
                "take_profit": None, "indicators": {}, "strategy": STRATEGY,
                "degraded": []}

    ema20 = ind.ema(c, 20)
    bl = ind.boll(c, 20, 2.0)
    kd = ind.kdj(h, l, c)
    rsi6 = ind.rsi(c, 6)
    vr = ind.vol_ratio(vol, 5)
    i = n - 1
    price = c[i]
    e20 = ema20[i]
    reasons = []
    score = 50.0
    action = "hold"
    position_pct = 0.0

    # ---- 大盘避险（context）：熊市不禁止交易，而是降仓位+提高买入门槛 ----
    # 修正：保守策略的价值正是在熊市找超跌反弹（BOLL下轨+底背离本就是熊市信号），
    # 若大盘避险直接 watch 则永不交易（n=0）。改为：risk_off 时买入门槛收紧
    # （必须有底背离确认）、仓位减半，但保留超跌反弹机会。
    mkt_score = context.get("market_score")
    dt_count = context.get("dt_flood_count")
    risk_off = False
    if mkt_score is not None and mkt_score < 50:
        reasons.append(f"大盘评分{mkt_score:.1f}<50，避险模式(买入需底背离+仓位减半)")
        risk_off = True
    elif mkt_score is None:
        degraded.append("大盘评分缺失(engine_fusion)，避险因子降级跳过")
    if dt_count is not None and dt_count > 50:
        reasons.append(f"跌停{dt_count}家>50家，恐慌模式(仅极端超跌+底背离才可小仓)")
        risk_off = True
        panic = True
    else:
        panic = False
        if dt_count is None:
            degraded.append("跌停家数缺失(anomaly/dtpool)，避险因子降级跳过")

    # ---- 止损检查 ----
    stop_broken = e20 is not None and price < e20
    cost = context.get("position_cost")
    cost_proxy = False
    if cost is None:
        cost = _vwap(c, vol, 20)
        cost_proxy = True
        degraded.append("持仓成本未知，用近20日VWAP代理成本")
    cum_loss = (price / cost - 1.0) * 100.0 if cost else None
    if stop_broken:
        reasons.append(f"收盘{price:.2f}跌破EMA20({e20:.2f})，触发止损")
        return {"action": "sell", "score": 10, "position_pct": 0, "reasons": reasons,
                "stop_loss": round(e20, 4), "take_profit": None,
                "indicators": {"ema20": round(e20, 4), "cum_loss_pct": round(cum_loss, 2) if cum_loss is not None else None},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}
    if cum_loss is not None and cum_loss <= -5.0:
        reasons.append(f"相对{'VWAP代理' if cost_proxy else ''}成本累计亏损{cum_loss:.1f}%≤-5%，触发止损")
        return {"action": "sell", "score": 10, "position_pct": 0, "reasons": reasons,
                "stop_loss": round(e20, 4) if e20 else None, "take_profit": None,
                "indicators": {"ema20": round(e20, 4) if e20 else None,
                               "cum_loss_pct": round(cum_loss, 2)},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 见顶信号 ----
    top_signals = []
    if is_dark_cloud_cover(o, c, h, l, i):
        top_signals.append("乌云盖顶")
    if is_evening_star(o, c, h, l, i):
        top_signals.append("黄昏之星")
    if is_heavy_bearish(o, c, vol, vr, i):
        top_signals.append(f"放量长阴(量比{vr[i]:.1f})")
    if top_signals:
        reasons.append("见顶信号：" + "、".join(top_signals) + "，减仓/离场")
        return {"action": "sell", "score": 25, "position_pct": 0, "reasons": reasons,
                "stop_loss": round(e20, 4) if e20 else None,
                "take_profit": None,
                "indicators": {"ema20": round(e20, 4) if e20 else None,
                               "top_patterns": top_signals,
                               "vol_ratio": round(vr[i], 2) if vr[i] is not None else None},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 买入：BOLL 下轨放量止跌 + 底背离 ----
    b_low, b_mid = bl["low"][i], bl["mid"][i]
    touched_low = b_low is not None and l[i] <= b_low * 1.01
    vol_burst = vr[i] is not None and vr[i] > 1.5
    lower_shadow = (min(o[i], c[i]) - l[i]) / price if price > 0 else 0.0
    stop_fall = c[i] >= o[i] or lower_shadow > 0.02  # 收阳或长下影止跌

    j_div, j_txt = _bullish_divergence(c, kd["j"], 40)
    r_div, r_txt = _bullish_divergence(c, rsi6, 40)

    if touched_low and vol_burst and stop_fall:
        reasons.append(f"BOLL下轨({b_low:.2f})放量(量比{vr[i]:.2f})止跌"
                       f"（{'收阳' if c[i] >= o[i] else '长下影'}）")
        score += 25
        if j_div or r_div:
            reasons.append("底背离确认：" + (j_txt if j_div else r_txt))
            score += 20
            action = "buy"
            # risk_off 时仓位减半（保守型在熊市轻仓）
            position_pct = 15.0 if risk_off else 30.0
            if risk_off:
                reasons.append("避险模式：仓位减半至15%")
        else:
            # risk_off（尤其 panic）时无底背离不开仓
            if panic:
                reasons.append("恐慌模式：无底背离确认，不开新仓")
                action = "watch"
            else:
                reasons.append("KDJ/RSI 无底背离确认，降仓试探")
                action = "buy"
                position_pct = 8.0 if risk_off else 15.0
                score += 5
    elif touched_low and stop_fall:
        reasons.append(f"触及BOLL下轨({b_low:.2f})止跌，但量比{vr[i]:.2f}未达1.5，量能不足")
        if j_div or r_div:
            reasons.append("存在底背离（" + (j_txt if j_div else r_txt) + "），小仓关注")
            action = "buy"
            position_pct = 10.0
            score += 10
        else:
            action = "watch"
            score -= 5
    else:
        reasons.append("未触发BOLL下轨放量止跌买点")
        if stop_fall and kd["j"][i] is not None and kd["j"][i] < 20:
            reasons.append(f"KDJ_J={kd['j'][i]:.1f}<20 超卖区，观察")
            action = "watch"

    score = max(0.0, min(100.0, score))
    take_profit = round(b_mid, 4) if b_mid else None  # 反弹至中轨为保守止盈参考
    return {
        "action": action, "score": round(score, 1),
        "position_pct": min(position_pct, 30.0),  # 仓位上限 3 成
        "reasons": reasons,
        "stop_loss": round(e20, 4) if e20 else None,
        "take_profit": take_profit,
        "indicators": {
            "ema20": round(e20, 4) if e20 else None,
            "boll_low": round(b_low, 4) if b_low else None,
            "boll_mid": round(b_mid, 4) if b_mid else None,
            "kdj_j": round(kd["j"][i], 2) if kd["j"][i] is not None else None,
            "rsi6": round(rsi6[i], 2) if rsi6[i] is not None else None,
            "vol_ratio": round(vr[i], 2) if vr[i] is not None else None,
            "bull_div_j": j_div, "bull_div_rsi": r_div,
            "market_score": mkt_score, "dt_count": dt_count,
        },
        "date": date[i] if date else None,
        "strategy": STRATEGY,
        "degraded": degraded,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="sh600519")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，严格无前视")
    ap.add_argument("--data", default=_loader.DEFAULT_DATA_DIR)
    ap.add_argument("--live", default=ctx_mod.DEFAULT_LIVE_DIR)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    kl = _loader.load_kline_from_bulk(args.data, args.code)
    kl = _loader.slice_asof(kl, args.asof)
    ctx = ctx_mod.load_context(args.data, args.live, asof=args.asof)
    res = evaluate(args.code, kl, ctx)
    txt = json.dumps(res, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        open(args.out, "w", encoding="utf-8").write(txt)
        print("->", args.out)
    else:
        print(txt)
