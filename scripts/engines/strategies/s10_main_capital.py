#!/usr/bin/env python3
"""s10_main_capital.py — S10 主力强攻策略（独立实现，策略规则来自用户提供）。平衡档。

规则：
1. 主力净流入为正 且 净特大单 > 净小单流出：
   - context moneyflow latest.txt 只有 mainnet（主力净额）与 netrate，无分单
     （特大单/大单/中单/小单）数据 → 「净特大单>净小单流出」不可直接验证，
     降级为「量价行为代理大单」：大阳/放量上攻且收盘接近日内高位（主力吸筹特征），
     并在 degraded 标注。context 若提供 order_split（{code:{super_net, small_net}}）
     则优先使用真实分单。
2. 板块涨幅前 10%：context 无板块榜 → 降级标注（预留 sector_rank.rank_pct<=10 判定）。
3. 主动买 > 主动卖：无逐笔成交数据 → 降级为「收盘价位于当日振幅上半区 +
   收阳」代理主动买入占优，并标注。
4. 卖出：
   - 价涨主力净流出背离：今日收阳（或近2日涨）但 mainnet<0 → 卖出；
   - 主力净流入断崖转负：netrate < -5% 视为断崖（无历史序列，降级为当日阈值），
     资金流缺失时降级为「日线趋势走坏：收盘跌破 EMA10」→ 卖出。
5. 仓位 5 成（固定）。

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

STRATEGY = "s10_main_capital"


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded: list = []
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

    code_norm = _loader.normalize_code(code)
    ema10 = ind.ema(close, 10)
    vr = ind.vol_ratio(vol, 5)
    chg = ind.pct_chg(close)

    i = n - 1
    price = close[i]
    e10 = ema10[i]
    reasons = []
    score = 40.0

    mf = ctx_mod.moneyflow_for(context, code_norm)
    mainnet = mf.get("mainnet") if mf else None
    netrate = mf.get("netrate_pct") if mf else None

    # ---- 规则 4：卖出先行 ----
    if mf is not None:
        # 价涨主力净流出背离
        if chg[i] is not None and chg[i] > 0 and mainnet is not None and mainnet < 0:
            reasons.append(f"价涨({chg[i]:.2f}%)但主力净流出({mainnet:.0f})，背离，卖出")
            return {"action": "sell", "score": 20, "position_pct": 0,
                    "reasons": reasons, "stop_loss": round(e10, 4) if e10 else None,
                    "take_profit": None,
                    "indicators": {"mainnet": mainnet, "netrate_pct": netrate},
                    "date": date[i] if date else None, "strategy": STRATEGY,
                    "degraded": degraded}
        # 断崖转负
        if netrate is not None and netrate < -5.0:
            reasons.append(f"主力净率{netrate:.1f}%<-5%，净流入断崖转负，卖出")
            return {"action": "sell", "score": 15, "position_pct": 0,
                    "reasons": reasons, "stop_loss": round(e10, 4) if e10 else None,
                    "take_profit": None,
                    "indicators": {"mainnet": mainnet, "netrate_pct": netrate},
                    "date": date[i] if date else None, "strategy": STRATEGY,
                    "degraded": degraded}
    else:
        degraded.append("moneyflow: 主力资金数据缺失，卖出条件降级为日线趋势（收盘破EMA10）")
        if e10 is not None and price < e10:
            reasons.append(f"资金流缺失，日线趋势降级判定：收盘破EMA10({e10:.2f})，卖出")
            return {"action": "sell", "score": 20, "position_pct": 0,
                    "reasons": reasons, "stop_loss": round(e10, 4),
                    "take_profit": None,
                    "indicators": {"ema10": round(e10, 4)},
                    "date": date[i] if date else None, "strategy": STRATEGY,
                    "degraded": degraded}

    # ---- 规则 1：主力净流入正 + 分单/代理 ----
    split = (context.get("order_split") or {}).get(code_norm)
    inflow_ok = False
    if mf is not None and mainnet is not None and mainnet > 0:
        reasons.append(f"主力净流入为正({mainnet:.0f})")
        score += 15
        if split:
            sup, sml = split.get("super_net"), split.get("small_net")
            if sup is not None and sml is not None and sup > abs(min(sml, 0.0)):
                reasons.append("净特大单>净小单流出（真实分单），强攻特征")
                score += 15
                inflow_ok = True
            else:
                reasons.append("分单不满足 净特大单>净小单流出")
        else:
            degraded.append("分单（特大单/小单）数据缺失，降级为量价行为代理大单")
            # 量价代理：放量大阳且收盘接近日内高位
            rng = high[i] - low[i]
            close_pos = (price - low[i]) / rng if rng > 0 else 0.5
            big_yang = chg[i] is not None and chg[i] >= 2.0
            volup = vr[i] is not None and vr[i] >= 1.5
            if big_yang and volup and close_pos >= 0.7:
                reasons.append(f"量价代理大单：涨{chg[i]:.2f}%+量比{vr[i]:.2f}+收盘在振幅"
                               f"{close_pos * 100:.0f}%分位（吸筹特征）")
                score += 12
                inflow_ok = True
            else:
                reasons.append("量价代理未确认大单吸筹特征")
    else:
        if mf is None:
            reasons.append("主力资金数据缺失，主力净流入条件不可验，降级观望")
        else:
            reasons.append(f"主力净流入非正(mainnet={mainnet})，强攻前提不成立")
        return {"action": "watch", "score": round(max(0.0, min(100.0, score - 10)), 1),
                "position_pct": 0, "reasons": reasons,
                "stop_loss": round(e10, 4) if e10 else None, "take_profit": None,
                "indicators": {"mainnet": mainnet, "netrate_pct": netrate},
                "date": date[i] if date else None, "strategy": STRATEGY,
                "degraded": degraded}

    # ---- 规则 2：板块涨幅前 10%（降级/预留）----
    sector = (context.get("sector_rank") or {}).get(code_norm)
    if sector is None:
        degraded.append("板块/行业涨幅榜：context 无板块数据源，「板块前10%」因子降级跳过")
        reasons.append("板块榜数据缺失，该因子跳过（不加分）")
    else:
        rank_pct = sector.get("rank_pct")
        if rank_pct is not None and rank_pct <= 10.0:
            reasons.append(f"板块涨幅前{rank_pct:.1f}%（前10%）")
            score += 12
        else:
            reasons.append(f"板块排名不足前10%({rank_pct})")
            score -= 5

    # ---- 规则 3：主动买 > 主动卖（降级）----
    degraded.append("主动买/主动卖：无逐笔成交数据，降级为收盘位于振幅上半区+收阳代理")
    rng = high[i] - low[i]
    close_pos = (price - low[i]) / rng if rng > 0 else 0.5
    if chg[i] is not None and chg[i] > 0 and close_pos >= 0.5:
        reasons.append(f"主动买代理成立：收阳且收盘在振幅{close_pos * 100:.0f}%分位")
        score += 10
    else:
        reasons.append("主动买代理不成立（收阴或收盘偏弱）")
        score -= 5

    # ---- 汇总：仓位固定 5 成 ----
    score = max(0.0, min(100.0, score))
    if inflow_ok and score >= 65:
        action, position_pct = "buy", 50.0
        reasons.append("主力强攻确认，仓位 5 成")
    else:
        action, position_pct = "watch", 0.0
        reasons.append("强攻确认不足，观望")

    stop_loss = round(e10, 4) if e10 else None
    take_profit = None
    if action == "buy" and i >= 1:
        # 止盈参考：近 20 日前高
        ph = max(high[max(0, i - 20):i])
        take_profit = round(ph, 4) if ph > price else None
    return {
        "action": action, "score": round(score, 1), "position_pct": position_pct,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "ema10": round(e10, 4) if e10 else None,
            "vol_ratio": round(vr[i], 2) if vr[i] is not None else None,
            "chg_pct": round(chg[i], 2) if chg[i] is not None else None,
            "close_pos_in_range": round(close_pos, 2),
            "mainnet": mainnet, "netrate_pct": netrate,
            "moneyflow_date": (mf or {}).get("date"),
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
