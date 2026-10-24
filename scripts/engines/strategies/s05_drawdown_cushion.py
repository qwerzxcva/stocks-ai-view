#!/usr/bin/env python3
"""s05_drawdown_cushion.py — S5 回撤承接策略（独立实现，策略规则来自用户提供）。

规则：
1. 买点：跌至/穿 BOLL 下轨 + KDJ_J < 0（深度超卖）。
2. 价格与 EMA20 偏离近期峰值过大：峰值回撤 = (近60日最高收盘-现价)/峰值，越大越超卖。
3. 压盘吸货识别：定义「卖盘大但价不跌」。无五档盘口 → 用「长下影线 + 缩量」代理
   （下影>2% 且量比<0.8：下方承接强、抛压被消化），并标注降级。context moneyflow
   提供真实主力净流入时优先参考。
4. 5 分钟底部抬升：无分时数据 → 降级为日线「近 3 日最低价逐日抬升」并标注。
5. 卖出：反弹至 BOLL 中轨；或 RSI6 修复回 50；或买入逻辑成立后 2 日未脱离成本平仓
   （成本以近 20 日 VWAP 代理，context['position_cost'] 优先）。
6. 仓位 2-3 成。

数据降级：换手率无流通股本→量比代理；主力资金→moneyflow 或降级；五档/分时无→
K线量价代理；均列入 degraded。

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

STRATEGY = "s05_drawdown_cushion"


def _vwap(close, vol, window):
    n = len(close)
    if n == 0:
        return None
    w = min(window, n)
    seg_c, seg_v = close[-w:], vol[-w:]
    tv = sum(seg_v)
    if tv <= 0:
        return sum(seg_c) / w
    return sum(x * v for x, v in zip(seg_c, seg_v)) / tv


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
    reasons = []
    score = 50.0
    action = "hold"
    position_pct = 0.0

    b_low, b_mid = bl["low"][i], bl["mid"][i]
    j_now = kd["j"][i]
    r_now = rsi6[i]

    # ---- 卖出判定（优先，持仓管理视角）----
    cost = context.get("position_cost")
    cost_proxy = False
    if cost is None:
        cost = _vwap(c, vol, 20)
        cost_proxy = True
    if cost_proxy:
        degraded.append("持仓成本未知，用近20日VWAP代理成本")
    profit_pct = (price / cost - 1.0) * 100.0 if cost else None

    rebound_mid = b_mid is not None and price >= b_mid * 0.995
    rsi_fixed = r_now is not None and r_now >= 50.0
    if rebound_mid:
        reasons.append(f"反弹至BOLL中轨({b_mid:.2f})，兑现卖点")
        return {"action": "sell", "score": 30, "position_pct": 0, "reasons": reasons,
                "stop_loss": round(b_low, 4) if b_low else None,
                "take_profit": round(b_mid, 4),
                "indicators": {"boll_mid": round(b_mid, 4), "rsi6": round(r_now, 2) if r_now else None},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}
    if rsi_fixed and profit_pct is not None and profit_pct > 0:
        reasons.append(f"RSI6={r_now:.1f}修复回50上方且浮盈{profit_pct:.1f}%，修复卖点")
        return {"action": "sell", "score": 30, "position_pct": 0, "reasons": reasons,
                "stop_loss": round(b_low, 4) if b_low else None,
                "take_profit": round(price, 4),
                "indicators": {"rsi6": round(r_now, 2), "profit_pct": round(profit_pct, 2)},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}
    # 2 日未脱离成本：近 2 日收盘均在成本 ±1% 内且处于浅套状态 → 平仓
    if cost and n >= 3:
        stuck = all(abs(c[k] / cost - 1.0) < 0.01 for k in (n - 1, n - 2))
        if stuck and profit_pct is not None and -1.0 <= profit_pct < 1.0 and rsi_fixed is False:
            reasons.append(f"2日未脱离成本区({cost:.2f}±1%)，平仓")
            return {"action": "sell", "score": 35, "position_pct": 0, "reasons": reasons,
                    "stop_loss": round(b_low, 4) if b_low else None, "take_profit": None,
                    "indicators": {"cost_proxy": round(cost, 4)},
                    "date": date[i] if date else None, "strategy": STRATEGY,
                    "degraded": degraded}

    # ---- 买入判定 ----
    # 1) 跌至/穿 BOLL 下轨 + J<0
    at_low = b_low is not None and l[i] <= b_low * 1.01
    deep_j = j_now is not None and j_now < 0
    if at_low:
        reasons.append(f"最低价{l[i]:.2f}跌至/穿BOLL下轨({b_low:.2f})")
        score += 15
    if deep_j:
        reasons.append(f"KDJ_J={j_now:.1f}<0 深度超卖")
        score += 15

    # 2) 峰值回撤（与 EMA20 偏离度一并看）
    peak = max(c[max(0, n - 60):n])
    dd_pct = (peak - price) / peak * 100.0 if peak > 0 else 0.0
    bias20 = (price / ema20[i] - 1.0) * 100.0 if ema20[i] else None
    if dd_pct > 10.0:
        reasons.append(f"距近60日峰值回撤{dd_pct:.1f}%>10%，超卖")
        score += 10
    elif dd_pct > 5.0:
        reasons.append(f"距近60日峰值回撤{dd_pct:.1f}%")
        score += 5
    if bias20 is not None and bias20 < -8.0:
        reasons.append(f"偏离EMA20达{bias20:.1f}%，偏离近期峰值过深")
        score += 5

    # 3) 压盘吸货：长下影 + 缩量代理（无五档盘口，降级）
    lower_shadow = (min(o[i], c[i]) - l[i]) / price * 100.0 if price > 0 else 0.0
    shrink = vr[i] is not None and vr[i] < 0.8
    absorb = lower_shadow > 2.0 and shrink
    mf = ctx_mod.moneyflow_for(context, code) if context else None
    if absorb:
        reasons.append(f"压盘吸货迹象：下影{lower_shadow:.1f}%+量比{vr[i]:.2f}缩量（盘口缺失，K线代理）")
        score += 10
        degraded.append("五档盘口缺失，压盘吸货用「长下影+缩量」K线代理")
    if mf is not None:
        if mf.get("mainnet", 0) > 0:
            reasons.append(f"主力净流入{mf['mainnet']:.0f}（moneyflow {mf['date']}），承接佐证")
            score += 5
        else:
            reasons.append(f"主力净流出{mf['mainnet']:.0f}，承接存疑")
            score -= 5
    else:
        degraded.append("主力资金数据缺失（moneyflow 无该股记录）")

    # 4) 5分钟底部抬升 → 日线降级：近 3 日最低价逐日抬升
    bottom_up = n >= 3 and l[n - 1] > l[n - 2] > l[n - 3]
    if bottom_up:
        reasons.append(f"底部抬升：近3日最低价{l[n-3]:.2f}→{l[n-2]:.2f}→{l[n-1]:.2f}逐日抬升（日线代理5分钟）")
        score += 10
    degraded.append("分时数据缺失，5分钟底部抬升降级为日线最低价抬升")

    # 汇总买/观
    if at_low and deep_j and (absorb or bottom_up):
        action = "buy"
        position_pct = 30.0
    elif at_low and deep_j:
        action = "buy"
        position_pct = 20.0
        reasons.append("核心超卖成立但承接/抬升证据不足，取2成仓")
    elif at_low or (deep_j and dd_pct > 8.0):
        action = "watch"
        reasons.append("部分超卖条件成立，等待 J<0 与下轨共振")
    else:
        reasons.append("未进入超卖承接区")

    score = max(0.0, min(100.0, score))
    if action == "hold":
        action = "watch"
    return {
        "action": action, "score": round(score, 1),
        "position_pct": min(position_pct, 30.0),
        "reasons": reasons,
        "stop_loss": round(b_low * 0.98, 4) if b_low else None,  # 下轨下方2%防守
        "take_profit": round(b_mid, 4) if b_mid else None,        # 第一目标中轨
        "indicators": {
            "ema20": round(ema20[i], 4) if ema20[i] else None,
            "boll_low": round(b_low, 4) if b_low else None,
            "boll_mid": round(b_mid, 4) if b_mid else None,
            "kdj_j": round(j_now, 2) if j_now is not None else None,
            "rsi6": round(r_now, 2) if r_now is not None else None,
            "vol_ratio": round(vr[i], 2) if vr[i] is not None else None,
            "peak_drawdown_pct": round(dd_pct, 2),
            "bias20_pct": round(bias20, 2) if bias20 is not None else None,
            "lower_shadow_pct": round(lower_shadow, 2),
            "bottom_up": bottom_up, "absorb_proxy": absorb,
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
