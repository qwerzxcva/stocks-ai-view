#!/usr/bin/env python3
"""s09_boll_band.py — S9 BOLL 波段高抛低吸策略（独立实现，策略规则来自用户提供）。

规则（底仓 2-3 成不动，滚动仓位做波段）：
1. 高抛（sell_high）：触及/破 BOLL 上轨 + KDJ_J>100 + RSI6>80 + 放量滞涨
   （量比>1.5 而当日涨幅<1%：放量不涨=派发迹象）。
2. 低吸（buy_low）：回踩 BOLL 中轨/下轨 + KDJ_J<10 + RSI6<30 + 止跌阳
   （收阳或长下影）。
3. 无信号 → hold（底仓持有）。

数据降级：换手率无流通股本→量比代理活跃度；主力资金/五档/分时无→不使用，列入 degraded。

统一接口：evaluate(code, kline, context) -> {action, score, position_pct, reasons,
  stop_loss, take_profit, indicators, strategy, degraded}
  action ∈ {buy_low, sell_high, hold, watch}
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

STRATEGY = "s09_boll_band"
BASE_POSITION = 25.0  # 底仓 2-3 成，取中值 2.5 成


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded = [
        "换手率缺失（无流通股本），用5日量比代理活跃度",
        "主力资金/五档盘口/分时缺失，未参与判定",
    ]
    o = kline.get("open", [])
    c = kline.get("close", [])
    h = kline.get("high", [])
    l = kline.get("low", [])
    vol = kline.get("vol", [])
    date = kline.get("date", [])
    n = len(c)
    min_bars = 30
    if n < min_bars:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": [f"数据不足({n}<{min_bars}根)"], "stop_loss": None,
                "take_profit": None, "indicators": {}, "strategy": STRATEGY,
                "degraded": degraded}

    bl = ind.boll(c, 20, 2.0)
    kd = ind.kdj(h, l, c)
    rsi6 = ind.rsi(c, 6)
    vr = ind.vol_ratio(vol, 5)
    chg = ind.pct_chg(c)
    i = n - 1
    price = c[i]
    b_up, b_mid, b_low = bl["up"][i], bl["mid"][i], bl["low"][i]
    j_now, r_now = kd["j"][i], rsi6[i]
    vr_now = vr[i]
    chg_now = chg[i]
    reasons = []
    score = 50.0
    action = "hold"
    position_pct = BASE_POSITION  # 底仓不动

    if b_up is None or b_low is None:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": ["BOLL 指标不可用"], "stop_loss": None, "take_profit": None,
                "indicators": {}, "date": date[i] if date else None,
                "strategy": STRATEGY, "degraded": degraded}

    # ---- 高抛 ----
    touch_up = h[i] >= b_up * 0.995 or price >= b_up
    j_hot = j_now is not None and j_now > 100
    rsi_hot = r_now is not None and r_now > 80
    stagnation = (vr_now is not None and vr_now > 1.5
                  and chg_now is not None and chg_now < 1.0)
    if touch_up and j_hot and rsi_hot and stagnation:
        reasons.append(f"触及BOLL上轨({b_up:.2f})+J={j_now:.0f}>100+RSI6={r_now:.0f}>80"
                       f"+放量滞涨(量比{vr_now:.1f}涨幅{chg_now:.1f}%)，高抛")
        action = "sell_high"
        score = 85.0
        position_pct = BASE_POSITION  # 高抛只减滚动仓，底仓保留
    elif touch_up and (j_hot or rsi_hot):
        reasons.append(f"触及上轨({b_up:.2f})且{'J>100' if j_hot else 'RSI6>80'}，"
                       "但放量滞涨/双超买不齐备，部分高抛或观望")
        action = "watch"
        score = 60.0

    # ---- 低吸 ----
    touch_mid = l[i] <= b_mid * 1.01
    touch_low = l[i] <= b_low * 1.01
    j_cold = j_now is not None and j_now < 10
    rsi_cold = r_now is not None and r_now < 30
    lower_shadow = (min(o[i], c[i]) - l[i]) / price * 100.0 if price > 0 else 0.0
    stop_fall_yang = c[i] >= o[i] or lower_shadow > 1.5

    if action in ("hold", "watch"):
        if touch_low and j_cold and rsi_cold and stop_fall_yang:
            reasons.append(f"回踩BOLL下轨({b_low:.2f})+J={j_now:.0f}<10+RSI6={r_now:.0f}<30"
                           f"+止跌{'阳' if c[i] >= o[i] else '下影'}，低吸")
            action = "buy_low"
            score = 85.0
            position_pct = min(BASE_POSITION + 25.0, 50.0)  # 底仓+滚动仓
        elif touch_mid and j_cold and rsi_cold and stop_fall_yang:
            reasons.append(f"回踩BOLL中轨({b_mid:.2f})+J={j_now:.0f}<10+RSI6={r_now:.0f}<30"
                           "+止跌，中轨低吸（强度弱于下轨）")
            action = "buy_low"
            score = 70.0
            position_pct = min(BASE_POSITION + 15.0, 40.0)
        elif (touch_mid or touch_low) and (j_cold or rsi_cold):
            reasons.append(f"回踩{'下轨' if touch_low else '中轨'}但超卖/止跌不齐备"
                           f"(J={j_now:.0f} RSI={r_now:.0f})，观察")
            action = "watch"
            score = 55.0

    if not reasons:
        pos_in_band = (price - b_low) / (b_up - b_low) * 100.0 if b_up > b_low else 50.0
        reasons.append(f"带内运行(位置{pos_in_band:.0f}%)，无顶底信号，底仓持有")
        action = "hold"

    stop_loss = round(b_low * 0.97, 4)  # 跌破下轨3%止损
    take_profit = round(b_up, 4) if action == "buy_low" else (
        round(b_mid, 4) if action == "sell_high" else None)

    return {
        "action": action, "score": round(score, 1),
        "position_pct": position_pct,
        "reasons": reasons,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "indicators": {
            "boll_up": round(b_up, 4), "boll_mid": round(b_mid, 4),
            "boll_low": round(b_low, 4),
            "kdj_j": round(j_now, 2) if j_now is not None else None,
            "rsi6": round(r_now, 2) if r_now is not None else None,
            "vol_ratio": round(vr_now, 2) if vr_now is not None else None,
            "chg_pct": round(chg_now, 2) if chg_now is not None else None,
            "band_position_pct": round((price - b_low) / (b_up - b_low) * 100.0, 1) if b_up > b_low else None,
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
