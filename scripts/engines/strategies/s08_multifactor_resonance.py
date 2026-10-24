#!/usr/bin/env python3
"""s08_multifactor_resonance.py — S8 多因子共振策略（独立实现，策略规则来自用户提供）。平衡档。

规则：
1. 大盘环境：context.market_score（engine_fusion E 值）> 80 视为普涨环境；
   缺失或 <80 时不开新仓（降级标注）。
2. 板块强度：所属板块涨幅前 15% 且指数在 5 日线上 —— context 无板块/行业
   数据源 → 降级标注，用个股自身趋势（收盘在 EMA5 上）作为弱代理并注明。
3. 技术共振：KDJ 低位金叉（K<30 上穿 D）或中位发散（30<=K<=70 且 K>D 且
   间距扩大）+ RSI6 处于 50-70 强势区。
4. 资金面：主力净流入连续 2 日为正或今日主力净额占成交额 > 5%
   （context moneyflow 只有当日快照 latest.txt，无历史序列 → 「连续2日」无法
   验证，降级为当日 mainnet>0 或 netrate>5%，并在 degraded 标注）。
5. 卖出：次日不破前高 / KDJ 顶背离（价格新高 J 未新高）/ 收盘破 3 日线（EMA3）。
6. 仓位 3-5 成。

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

STRATEGY = "s08_multifactor_resonance"


def _top_divergence(high, j_arr, i, lookback=40):
    """KDJ 顶背离：价格创近 lookback 日新高，但 J 未超该窗口内前高对应的 J。"""
    if i < lookback:
        return False
    win_h = high[i - lookback + 1:i + 1]
    if high[i] < max(win_h):
        return False  # 今日非新高
    # 找窗口内此前高点
    prev_peak = max(high[i - lookback + 1:i])
    prev_idx = None
    for k in range(i - lookback + 1, i):
        if high[k] == prev_peak:
            prev_idx = k
    if prev_idx is None or j_arr[i] is None or j_arr[prev_idx] is None:
        return False
    return j_arr[i] < j_arr[prev_idx]


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded: list = []
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

    code_norm = _loader.normalize_code(code)
    ema3 = ind.ema(close, 3)
    ema5 = ind.ema(close, 5)
    kd = ind.kdj(high, low, close)
    rsi6 = ind.rsi(close, 6)

    i = n - 1
    price = close[i]
    e3, e5 = ema3[i], ema5[i]
    K, D, J = kd["k"][i], kd["d"][i], kd["j"][i]
    r6 = rsi6[i]
    reasons = []
    score = 40.0

    # ---- 规则 5：卖出先行 ----
    prior_high = max(high[max(0, i - 10):i]) if i >= 1 else None
    if e3 is not None and price < e3:
        reasons.append(f"收盘{price:.2f}破3日线EMA3({e3:.2f})，卖出")
        return {"action": "sell", "score": 20, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e3, 4), "take_profit": None,
                "indicators": {"ema3": round(e3, 4)}, "date": date[i] if date else None,
                "strategy": STRATEGY, "degraded": degraded}
    if _top_divergence(high, kd["j"], i):
        reasons.append("KDJ 顶背离（价新高 J 未新高），卖出")
        return {"action": "sell", "score": 25, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e3, 4) if e3 else None,
                "take_profit": None,
                "indicators": {"kdj_j": round(J, 2) if J is not None else None},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 规则 1：大盘评分 > 80 普涨 ----
    mkt = context.get("market_score")
    if mkt is None:
        degraded.append("market_score: engine_fusion 不可用，大盘环境因子降级为中性")
        reasons.append("大盘评分缺失，环境因子降级中性（不加扣分）")
    elif mkt > 80:
        reasons.append(f"大盘评分{mkt:.1f}>80，普涨环境")
        score += 15
    else:
        reasons.append(f"大盘评分{mkt:.1f}≤80，非普涨环境，不开新仓")
        return {"action": "watch", "score": 30, "position_pct": 0,
                "reasons": reasons, "stop_loss": round(e3, 4) if e3 else None,
                "take_profit": None,
                "indicators": {"market_score": mkt},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 规则 2：板块强度（降级）----
    sector = context.get("sector_rank") or context.get("industry_rank")
    if sector is None:
        degraded.append("板块/行业涨幅榜：context 无板块数据源，降级为个股自身在EMA5上弱代理")
        if e5 is not None and price > e5:
            reasons.append("板块数据缺失，以个股收盘在EMA5上作弱代理通过")
            score += 8
        else:
            reasons.append("板块数据缺失且个股在EMA5下，代理不通过")
            score -= 8
    else:
        # 预留：若有板块榜数据，要求前 15%
        rank_pct = (sector.get(code_norm) or {}).get("rank_pct")
        above5 = (sector.get(code_norm) or {}).get("above_ma5")
        if rank_pct is not None and rank_pct <= 15.0 and above5:
            reasons.append(f"板块涨幅前{rank_pct:.1f}%且在5日线上")
            score += 12
        else:
            reasons.append("板块强度不足（未进前15%或破5日线）")
            score -= 8

    # ---- 规则 3：KDJ 共振 + RSI6 50-70 ----
    kdj_ok = False
    if K is not None and D is not None and i >= 1 \
            and kd["k"][i - 1] is not None and kd["d"][i - 1] is not None:
        golden = kd["k"][i - 1] <= kd["d"][i - 1] and K > D
        if golden and K < 30:
            reasons.append(f"KDJ低位金叉(K={K:.1f}<30 上穿D={D:.1f})")
            score += 15
            kdj_ok = True
        elif 30 <= K <= 70 and K > D:
            spread_now = K - D
            spread_prev = kd["k"][i - 1] - kd["d"][i - 1]
            if spread_now > spread_prev:
                reasons.append(f"KDJ中位发散(K={K:.1f}>D={D:.1f}，间距扩大)")
                score += 12
                kdj_ok = True
            else:
                reasons.append("KDJ 中位但未发散")
        else:
            reasons.append(f"KDJ无共振(K={K:.1f}, D={D:.1f})")
    else:
        reasons.append("KDJ 数据不足")
    if r6 is not None and 50.0 <= r6 <= 70.0:
        reasons.append(f"RSI6={r6:.1f} 在50-70强势区")
        score += 10
    elif r6 is not None:
        reasons.append(f"RSI6={r6:.1f} 不在50-70区间，动能不配合")
        score -= 5

    # ---- 规则 4：资金面（降级）----
    mf = ctx_mod.moneyflow_for(context, code_norm)
    if mf is None:
        degraded.append("moneyflow: 主力资金数据缺失，资金面因子降级为量价代理")
        # 量价代理：连续2日收阳且今日量>=昨日
        if i >= 2 and close[i] > close[i - 1] and close[i - 1] > close[i - 2] \
                and vol[i] >= vol[i - 1]:
            reasons.append("资金流缺失，量价代理：连续2日收阳且放量，视同资金流入")
            score += 8
        else:
            reasons.append("资金流缺失，量价代理未确认资金流入")
    else:
        mainnet = mf.get("mainnet")
        netrate = mf.get("netrate_pct")
        if mainnet is not None and mainnet > 0:
            reasons.append(f"当日主力净流入为正({mainnet:.0f}，仅当日快照，连续2日不可验)")
            score += 10
        elif netrate is not None and netrate > 5.0:
            reasons.append(f"主力净率{netrate:.1f}%>5%")
            score += 10
        else:
            reasons.append(f"主力资金不配合(mainnet={mainnet}, netrate={netrate})")
            score -= 5
        degraded.append("moneyflow 仅当日快照，「连续2日为正」降级为当日判定")

    # ---- 汇总：仓位 3-5 成 ----
    score = max(0.0, min(100.0, score))
    if kdj_ok and score >= 75:
        action, position_pct = "buy", 50.0
        reasons.append("多因子共振强，仓位 5 成")
    elif kdj_ok and score >= 60:
        action, position_pct = "buy", 30.0
        reasons.append("共振一般，仓位 3 成")
    else:
        action, position_pct = "watch", 0.0
        reasons.append("共振不足，观望")

    stop_loss = round(e3, 4) if e3 else None
    take_profit = round(prior_high, 4) if prior_high else None
    return {
        "action": action, "score": round(score, 1), "position_pct": position_pct,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "ema3": round(e3, 4) if e3 else None,
            "ema5": round(e5, 4) if e5 else None,
            "kdj_k": round(K, 2) if K is not None else None,
            "kdj_d": round(D, 2) if D is not None else None,
            "kdj_j": round(J, 2) if J is not None else None,
            "rsi6": round(r6, 2) if r6 is not None else None,
            "market_score": mkt,
            "moneyflow": mf,
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
