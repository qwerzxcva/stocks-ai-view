#!/usr/bin/env python3
"""s06_strong_pullback.py — S6 强势回踩策略（独立实现，策略规则来自用户提供）。平衡档。

规则：
1. 强势前提：近 5 日内出现过涨停（consecutive_limit_up）或 5 日累计涨幅 > 20%。
2. 今日缩量 > 30%：当日量 < 近 5 日均量的 70%（vol_ratio < 0.7）。
3. 回踩 EMA5/EMA10 获支撑：最低价触及/略破均线但收盘收回均线上方（±2% 区间）。
   - 五档护盘（盘口大单托底）：K 线无盘口数据，无法验证 → 降级标注，仅以收盘收回为据。
4. 15 分钟底分形 + MACD 绿柱缩短：无 15 分钟数据 → 降级为
   「日线底分型（昨日最低价为三日最低、今日低点抬高）+ 日线 MACD hist 负值拐头（绿柱缩短）」
   并在 degraded 标注。
5. 卖出：次日不能创新高（收盘低于前高）→ 清仓信号；收盘跌破 EMA10 → 止损。
6. 仓位 6-8 成（平衡档激进腿）。

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

STRATEGY = "s06_strong_pullback"


def _board(code: str) -> str:
    c = _loader.normalize_code(code)
    if c.startswith("bj"):
        return "bse"
    if c.startswith(("sz30", "sh68")):
        return "star"
    return "main"


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded: list = [
        "五档护盘：K线无盘口数据，无法验证盘口大单托底，仅以收盘收回均线为据",
        "15分钟底分形/MACD：无分钟数据，降级为日线底分型+日线MACD hist拐头",
    ]
    close = kline.get("close", [])
    high = kline.get("high", [])
    low = kline.get("low", [])
    vol = kline.get("vol", [])
    date = kline.get("date", [])
    n = len(close)
    min_bars = 40
    if n < min_bars:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": [f"数据不足({n}<{min_bars}根)"], "stop_loss": None,
                "take_profit": None, "indicators": {}, "strategy": STRATEGY,
                "degraded": degraded}

    ema5 = ind.ema(close, 5)
    ema10 = ind.ema(close, 10)
    vr = ind.vol_ratio(vol, 5)
    chg = ind.pct_chg(close)
    lu = ind.consecutive_limit_up(close, _board(code))
    md = ind.macd(close)

    i = n - 1
    price = close[i]
    e5, e10 = ema5[i], ema10[i]
    reasons = []
    score = 40.0

    # ---- 规则 5：卖出/止损先行判定 ----
    prior_high = max(high[max(0, i - 10):i]) if i >= 1 else None  # 近10日前高（不含今日）
    if e10 is not None and price < e10:
        reasons.append(f"收盘{price:.2f}跌破EMA10({e10:.2f})，触发止损，清仓")
        return {"action": "sell", "score": 15, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e10, 4), "take_profit": None,
                "indicators": {"ema5": round(e5, 4) if e5 else None,
                               "ema10": round(e10, 4)},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}
    if prior_high and i >= 1 and high[i] < prior_high and high[i - 1] < prior_high \
            and chg[i] is not None and chg[i] < 0:
        # 连续两日高点不能收复前高且今日收阴 = 次日不破前高 → 清仓信号
        reasons.append(f"次日不破前高({prior_high:.2f})且收阴，按规则清仓")
        return {"action": "sell", "score": 25, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e10, 4) if e10 else None,
                "take_profit": None,
                "indicators": {"prior_high": round(prior_high, 4)},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 规则 1：强势前提（5日内涨停 或 5日累计涨>20%）----
    win = 5
    had_limit = max(lu[max(0, i - win + 1):i + 1]) >= 1
    base = close[i - win] if i >= win and close[i - win] > 0 else None
    cum5 = (close[i] / base - 1.0) * 100.0 if base else None
    strong = had_limit or (cum5 is not None and cum5 > 20.0)
    if not strong:
        reasons.append("近5日无涨停且累计涨幅未超20%，强势前提不成立")
        return {"action": "watch", "score": 30, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e10, 4) if e10 else None,
                "take_profit": None,
                "indicators": {"cum5_pct": round(cum5, 2) if cum5 is not None else None,
                               "had_limit_up": had_limit},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}
    if had_limit:
        reasons.append("近5日内出现涨停，强势前提成立")
        score += 15
    if cum5 is not None and cum5 > 20.0:
        reasons.append(f"近5日累计涨幅{cum5:.1f}%>20%，强势")
        score += 10

    # ---- 规则 2：今日缩量 > 30% ----
    v = vr[i]
    if v is None:
        reasons.append("量比无法计算")
    elif v < 0.7:
        reasons.append(f"今日量比{v:.2f}<0.7，缩量超30%，洗盘特征")
        score += 15
    else:
        reasons.append(f"量比{v:.2f} 缩量不足30%，回踩质量存疑")
        score -= 5

    # ---- 规则 3：回踩 EMA5/EMA10 获支撑 ----
    def _touched(ma):
        return ma is not None and ma > 0 and low[i] <= ma * 1.01 and price >= ma * 0.995

    back_ema5 = _touched(e5)
    back_ema10 = _touched(e10)
    if back_ema5:
        reasons.append(f"回踩EMA5({e5:.2f})获支撑并收回（盘口护盘无数据，降级收盘判定）")
        score += 15
    elif back_ema10:
        reasons.append(f"回踩EMA10({e10:.2f})获支撑并收回（盘口护盘无数据，降级收盘判定）")
        score += 10
    else:
        reasons.append("未有效回踩EMA5/EMA10，买点不成立")
        return {"action": "watch", "score": round(max(0.0, min(100.0, score)), 1),
                "position_pct": 0, "reasons": reasons,
                "stop_loss": round(e10, 4) if e10 else None, "take_profit": None,
                "indicators": {"ema5": round(e5, 4) if e5 else None,
                               "ema10": round(e10, 4) if e10 else None,
                               "vol_ratio": round(v, 2) if v is not None else None},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 规则 4：底分型 + MACD 绿柱缩短（日线降级版）----
    fractal = False
    if i >= 2:
        # 日线底分型：昨日最低价为三日最低（中柱低于两侧），今日低点抬高
        fractal = (low[i - 1] <= low[i - 2] and low[i - 1] <= low[i]
                   and low[i] > low[i - 1])
    hist = md["hist"]
    hist_turn = (i >= 2 and hist[i] is not None and hist[i - 1] is not None
                 and hist[i] < 0 and hist[i] > hist[i - 1])
    if fractal:
        reasons.append("日线底分型出现（15分钟底分形降级替代）")
        score += 8
    if hist_turn:
        reasons.append(f"MACD绿柱缩短(hist {hist[i-1]:.3f}→{hist[i]:.3f})，动能修复")
        score += 8
    if not fractal and not hist_turn:
        reasons.append("底分型与MACD拐头均未出现，动能确认不足")
        score -= 5

    # ---- 汇总：仓位 6-8 成 ----
    score = max(0.0, min(100.0, score))
    if score >= 75:
        action, position_pct = "buy", 80.0
        reasons.append("信号强，仓位 8 成")
    elif score >= 60:
        action, position_pct = "buy", 60.0
        reasons.append("信号中等，仓位 6 成")
    else:
        action, position_pct = "watch", 0.0
        reasons.append("综合评分不足，观望")

    stop_loss = round(e10, 4) if e10 else None
    take_profit = round(prior_high, 4) if prior_high else None
    return {
        "action": action, "score": round(score, 1), "position_pct": position_pct,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "ema5": round(e5, 4) if e5 else None,
            "ema10": round(e10, 4) if e10 else None,
            "vol_ratio": round(v, 2) if v is not None else None,
            "cum5_pct": round(cum5, 2) if cum5 is not None else None,
            "had_limit_up_5d": had_limit,
            "macd_hist": round(hist[i], 4) if hist[i] is not None else None,
            "hist_turning": hist_turn, "daily_fractal": fractal,
            "prior_high_10d": round(prior_high, 4) if prior_high else None,
        },
        "date": date[i] if date else None, "strategy": STRATEGY,
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
