#!/usr/bin/env python3
"""s04_intraday_breakout.py — S4 分时突破策略（独立实现，策略规则来自用户提供）。激进档。

⚠️ 本策略原生依赖分时/盘口数据，K线（日线）口径下为降级实现，docstring 如实标注：
1. 早盘横盘 > 1h 且波幅 < 2%：无分时数据 → 降级为「近 5 日低波幅近似」：
   近 5 日日内振幅 (high-low)/prev_close 均值 < 2.5%（日线波幅系统性高于分时，
   阈值放宽至 2.5%），视为持续缩量窄幅整理后的蓄势。
2. 买 1-5 大单推升：无盘口数据 → 降级为「量价行为代理」：收阳且收盘位于当日
   振幅上半区（收盘在 (high+low)/2 之上），代理买方占优推升；盘口判定关闭并标注。
3. 放量突破：量比 > 3（当日量/近5日均量）且收盘突破近 20 日高点平台。
4. 涨幅 3-6% 未涨停：当日涨幅 ∈ [3%, 6%]，且未触涨停（主板<9.8%/创业科创<19.8%）。
5. 卖出：
   - 量能衰退：次日量比 < 1（突破后量能不济）→ 卖出；
   - 获利 3% 保本损：持仓浮盈>=3% 后回落抹平 → 保本卖出。日线无持仓成本，
     降级为「突破确认日（最近一根满足规则的 K 线）收盘价为成本，浮盈曾>=3% 且
     当前回落 <= 成本 → 保本卖出」。
6. 仓位 4-5 成（评分>=70 → 5 成，60-70 → 4 成）。

无前视：evaluate 只使用 kline 序列最后一根及之前的数据。

统一接口：evaluate(code, kline, context) -> {action, score, position_pct, reasons,
  stop_loss, take_profit, indicators, strategy, degraded}
CLI：--code --asof --out（--asof 严格无前视，slice_asof 截断）
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

STRATEGY = "s04_intraday_breakout"

_DEGRADED_BASE = [
    "早盘横盘>1h波幅<2%：无分时数据，降级为近5日低波幅（日内振幅均值<2.5%）近似",
    "买1-5大单推升：无盘口数据，降级为收阳+收盘位于当日振幅上半区的量价代理",
    "获利3%保本损：日线无持仓成本，降级为以最近突破确认日收盘为成本近似",
]


def _board(code: str) -> str:
    c = _loader.normalize_code(code)
    if c.startswith("bj"):
        return "bse"
    if c.startswith(("sz30", "sh68")):
        return "star"
    return "main"


def _amp_pct(high: list, low: list, close: list, i: int) -> Optional[float]:
    """日内振幅 % = (high-low)/prev_close。"""
    if i < 1 or close[i - 1] <= 0:
        return None
    return (high[i] - low[i]) / close[i - 1] * 100.0


def _is_breakout_day(kline: dict, k: int, vr: list, chg: list,
                     limit_thr: float) -> bool:
    """第 k 根是否满足放量突破（量比>3 且收盘突破近20日高点 且涨幅3-6%未涨停）。"""
    close, high = kline["close"], kline["high"]
    if k < 20 or vr[k] is None or chg[k] is None:
        return False
    platform = max(high[k - 20:k])
    return (vr[k] > 3.0 and close[k] > platform
            and 3.0 <= chg[k] <= 6.0 and chg[k] < limit_thr)


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded: list = list(_DEGRADED_BASE)
    close = kline.get("close", [])
    high = kline.get("high", [])
    low = kline.get("low", [])
    vol = kline.get("vol", [])
    date = kline.get("date", [])
    n = len(close)
    min_bars = 30
    if n < min_bars:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": [f"数据不足({n}<{min_bars}根)"], "stop_loss": None,
                "take_profit": None, "indicators": {}, "strategy": STRATEGY,
                "degraded": degraded}

    i = n - 1
    price = close[i]
    last_date = date[i] if date else None
    chg = ind.pct_chg(close)
    vr = ind.vol_ratio(vol, 5)
    limit_thr = {"main": 9.8, "star": 19.8, "bse": 29.8}[_board(code)]
    reasons: list = []
    score = 40.0

    v = vr[i]
    platform = max(high[i - 20:i]) if i >= 20 else None

    # ---- 卖出先行 ----
    # 找最近一次突破确认日（成本基准）
    cost_idx = None
    for k in range(i, max(i - 15, 19), -1):
        if _is_breakout_day(kline, k, vr, chg, limit_thr):
            cost_idx = k
            break

    if cost_idx is not None and cost_idx < i:
        cost = close[cost_idx]
        peak = max(close[cost_idx:i + 1])
        profit_peak = (peak / cost - 1.0) * 100.0
        profit_now = (price / cost - 1.0) * 100.0
        # 5a. 获利3%保本损：曾浮盈>=3% 且当前回落到成本及以下
        if profit_peak >= 3.0 and price <= cost:
            reasons.append(f"突破日{cost_idx and date[cost_idx]}成本{cost:.2f}，"
                           f"浮盈峰值{profit_peak:.1f}%≥3%，现回落至{profit_now:.1f}%，保本卖出")
            return {"action": "sell", "score": 20, "position_pct": 0,
                    "reasons": reasons, "stop_loss": round(cost, 4),
                    "take_profit": None,
                    "indicators": {"cost": round(cost, 4),
                                   "profit_peak_pct": round(profit_peak, 2)},
                    "date": last_date, "strategy": STRATEGY, "degraded": degraded}
        # 5b. 量能衰退：突破次日及之后量比<1
        if v is not None and v < 1.0 and profit_now < 3.0:
            reasons.append(f"突破后量比{v:.2f}<1，量能衰退，卖出")
            return {"action": "sell", "score": 25, "position_pct": 0,
                    "reasons": reasons,
                    "stop_loss": round(cost, 4), "take_profit": None,
                    "indicators": {"vol_ratio5": round(v, 2)},
                    "date": last_date, "strategy": STRATEGY, "degraded": degraded}
        reasons.append(f"持仓中（突破日收盘{cost:.2f}为成本，浮盈{profit_now:.1f}%），持有")
        return {"action": "hold", "score": 55, "position_pct": 0,
                "reasons": reasons,
                "stop_loss": round(cost, 4),
                "take_profit": round(cost * 1.10, 4),
                "indicators": {"cost": round(cost, 4),
                               "profit_now_pct": round(profit_now, 2),
                               "vol_ratio5": round(v, 2) if v is not None else None},
                "date": last_date, "strategy": STRATEGY, "degraded": degraded}

    # ---- 买点判定（当日） ----
    # 1. 早盘横盘降级：近5日低波幅
    amps = [_amp_pct(high, low, close, k) for k in range(max(1, i - 4), i + 1)]
    amps = [a for a in amps if a is not None]
    amp5 = sum(amps) / len(amps) if amps else None
    low_vol_ok = amp5 is not None and amp5 < 2.5
    if low_vol_ok:
        reasons.append(f"近5日日内振幅均值{amp5:.2f}%<2.5%，低波幅蓄势（分时横盘降级）")
        score += 15
    else:
        reasons.append(f"近5日振幅均值{amp5 if amp5 is None else round(amp5, 2)}%，"
                       "非低波幅（降级口径）")

    # 2. 买1-5大单推升降级：收阳+收盘在振幅上半区
    mid = (high[i] + low[i]) / 2.0
    push_ok = chg[i] is not None and chg[i] > 0 and price >= mid
    if push_ok:
        reasons.append("收阳且收盘位于当日振幅上半区，买方占优（盘口大单降级代理）")
        score += 10
    else:
        reasons.append("未现买方占优特征（降级口径）")

    # 3. 放量突破：量比>3 且收盘破20日平台
    breakout_vol = v is not None and v > 3.0
    breakout_price = platform is not None and price > platform
    if breakout_vol and breakout_price:
        reasons.append(f"量比{v:.2f}>3 放量突破20日平台{platform:.2f}")
        score += 25
    else:
        reasons.append(f"量比{v if v is None else round(v, 2)} / 平台"
                       f"{platform if platform is None else round(platform, 2)}，"
                       "放量突破不成立")

    # 4. 涨幅3-6%未涨停
    chg_ok = chg[i] is not None and 3.0 <= chg[i] <= 6.0 and chg[i] < limit_thr
    if chg_ok:
        reasons.append(f"涨幅{chg[i]:.2f}% 在3-6%区间且未涨停（阈值{limit_thr}%）")
        score += 15
    elif chg[i] is not None:
        reasons.append(f"涨幅{chg[i]:.2f}% 不在3-6%区间或已触涨停")

    # ---- 汇总：仓位 4-5 成 ----
    score = max(0.0, min(100.0, score))
    if breakout_vol and breakout_price and chg_ok and score >= 70:
        action, pos = "buy", 50.0
        reasons.append("分时突破信号强，仓位 5 成")
    elif breakout_vol and breakout_price and chg_ok and score >= 60:
        action, pos = "buy", 40.0
        reasons.append("分时突破信号中等，仓位 4 成")
    else:
        action, pos = "watch", 0.0
        reasons.append("核心突破条件不齐，观望")

    stop_loss = round(platform, 4) if platform else None
    take_profit = round(price * 1.10, 4)
    return {
        "action": action, "score": round(score, 1), "position_pct": pos,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "vol_ratio5": round(v, 2) if v is not None else None,
            "chg_pct": round(chg[i], 2) if chg[i] is not None else None,
            "amp5_avg_pct": round(amp5, 2) if amp5 is not None else None,
            "platform_20d": round(platform, 4) if platform else None,
            "close_in_upper_half": push_ok,
            "limit_threshold": limit_thr,
        },
        "date": last_date, "strategy": STRATEGY, "degraded": degraded,
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
