#!/usr/bin/env python3
"""s03_turnover_relay.py — S3 高换手接力策略（独立实现，策略规则来自用户提供）。激进档。

规则：
1. 换手率 15-25% 且量比 > 1.5：
   - K线无流通股本，换手率不可直接计算；context moneyflow latest.txt 含
     turnover_pct（真实换手率）时优先使用，并按日期对齐（快照晚于决策日拒绝使用，防前视）。
   - moneyflow 缺失/过期时降级为「量比区间 + 成交放大代理」：量比>1.5 且当日量
     处于近 20 日分位 > 70% 视为高换手活跃，并在 degraded 标注。
2. 压力位放量整理站稳：近 20 日压力位（前高），近 3 日在压力位 ±3% 区间内整理
   且收盘站上压力位（>=压力位*0.99），整理期均量 >= 前 20 日均量（放量整理）。
3. 主力净流入为正：context moneyflow mainnet > 0；缺则降级标注（不加减分）。
4. 行业催化：context 板块催化（context['sector_hot'] 为板块名列表），与个股行业
   （moneyflow 记录 industry 字段，若无则跳过）匹配则加分；数据源缺失降级。
5. 卖出：
   - 死亡换手：换手率 > 35%（moneyflow 真实值；缺失时降级为「天量收阴」：
     量比>3 且当日收阴）。
   - 放量滞涨：量比 > 2 但涨幅 < 1%（资金分歧）。
   - 板块退潮：context sector_hot 榜中该股行业跌出前列（缺失降级跳过）。
6. 仓位 5 成（固定）。

无前视：evaluate 只使用 kline 序列最后一根及之前的数据；moneyflow 快照日期
晚于 kline 最后交易日时拒绝使用并降级。

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

STRATEGY = "s03_turnover_relay"
POSITION_PCT = 50.0  # 仓位 5 成（固定）


def _moneyflow_usable(context: dict, code: str, last_date: Optional[str]) -> Optional[dict]:
    """moneyflow 快照可用性：存在、且快照日期不晚于 kline 最后日期（防前视）。"""
    mf = ctx_mod.moneyflow_for(context, _loader.normalize_code(code))
    if mf is None:
        return None
    snap = mf.get("date")
    if last_date and snap and snap > last_date:
        return None  # 快照晚于决策日 → 前视，拒绝
    return mf


def _vol_pctile(vol: list, i: int, win: int = 20) -> Optional[float]:
    """当日量在近 win 日（含当日）中的分位（0-100）。"""
    if i < win - 1:
        return None
    seg = vol[i - win + 1:i + 1]
    cnt = sum(1 for v in seg if v <= vol[i])
    return cnt / len(seg) * 100.0


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
    degraded: list = []
    close = kline.get("close", [])
    high = kline.get("high", [])
    vol = kline.get("vol", [])
    date = kline.get("date", [])
    n = len(close)
    min_bars = 40
    if n < min_bars:
        return {"action": "hold", "score": 0, "position_pct": 0,
                "reasons": [f"数据不足({n}<{min_bars}根)"], "stop_loss": None,
                "take_profit": None, "indicators": {}, "strategy": STRATEGY,
                "degraded": degraded}

    i = n - 1
    price = close[i]
    last_date = date[i] if date else None
    chg = ind.pct_chg(close)
    vr5 = ind.vol_ratio(vol, 5)     # 标准量比：当日量/近5日均量
    vr20 = ind.vol_ratio(vol, 20)   # 近似换手活跃度（_indicators 口径）
    ema5 = ind.ema(close, 5)
    reasons: list = []
    score = 40.0

    mf = _moneyflow_usable(context, code, last_date)
    if mf is None:
        degraded.append(
            "换手率/主力净流入：moneyflow 缺失或快照晚于决策日（防前视），"
            "换手率降级为量比区间+成交放大代理，主力净流入因子跳过")
    turnover = mf.get("turnover_pct") if mf else None
    mainnet = mf.get("mainnet") if mf else None
    v5 = vr5[i]
    pctile20 = _vol_pctile(vol, i, 20)

    # ---- 规则 5：卖出先行 ----
    # 5a. 死亡换手 >35%
    if turnover is not None and turnover > 35.0:
        reasons.append(f"换手率{turnover:.1f}%>35%，死亡换手，清仓")
        return {"action": "sell", "score": 10, "position_pct": 0,
                "reasons": reasons, "stop_loss": None, "take_profit": None,
                "indicators": {"turnover_pct": turnover},
                "date": last_date, "strategy": STRATEGY, "degraded": degraded}
    if turnover is None and v5 is not None and v5 > 3.0 \
            and chg[i] is not None and chg[i] < 0:
        reasons.append(f"量比{v5:.2f}>3 且收阴（{chg[i]:.2f}%），天量收阴代理死亡换手，清仓")
        degraded.append("死亡换手：无真实换手率，降级为天量(量比>3)+收阴代理")
        return {"action": "sell", "score": 15, "position_pct": 0,
                "reasons": reasons, "stop_loss": None, "take_profit": None,
                "indicators": {"vol_ratio5": round(v5, 2)},
                "date": last_date, "strategy": STRATEGY, "degraded": degraded}
    # 5b. 放量滞涨：量比>2 且涨幅<1%
    if v5 is not None and v5 > 2.0 and chg[i] is not None and chg[i] < 1.0:
        reasons.append(f"量比{v5:.2f}>2 但涨幅仅{chg[i]:.2f}%，放量滞涨，资金分歧，卖出")
        return {"action": "sell", "score": 25, "position_pct": 0,
                "reasons": reasons,
                "stop_loss": round(ema5[i], 4) if ema5[i] else None,
                "take_profit": None,
                "indicators": {"vol_ratio5": round(v5, 2), "chg_pct": round(chg[i], 2)},
                "date": last_date, "strategy": STRATEGY, "degraded": degraded}
    # 5c. 板块退潮：context sector_hot 列表存在且该股行业不在其中
    sector_hot = context.get("sector_hot")
    my_industry = mf.get("industry") if mf else None
    if sector_hot is not None and my_industry:
        if my_industry not in sector_hot:
            reasons.append(f"板块退潮：行业[{my_industry}]不在热点榜，卖出")
            return {"action": "sell", "score": 25, "position_pct": 0,
                    "reasons": reasons,
                    "stop_loss": round(ema5[i], 4) if ema5[i] else None,
                    "take_profit": None,
                    "indicators": {"industry": my_industry},
                    "date": last_date, "strategy": STRATEGY, "degraded": degraded}
    else:
        degraded.append("板块退潮：context 无 sector_hot/行业字段，退潮判定跳过")

    # ---- 规则 1：换手 15-25% 且量比>1.5 ----
    turnover_ok = None
    if turnover is not None:
        turnover_ok = 15.0 <= turnover <= 25.0
        if turnover_ok:
            reasons.append(f"真实换手率{turnover:.1f}% 在15-25%接力区间")
            score += 20
        else:
            reasons.append(f"换手率{turnover:.1f}% 不在15-25%区间")
    else:
        # 降级：量比>1.5 且 20 日量分位>70%
        turnover_ok = (v5 is not None and v5 > 1.5
                       and pctile20 is not None and pctile20 > 70.0)
        if turnover_ok:
            reasons.append(f"量比{v5:.2f}>1.5 且量能20日分位{pctile20:.0f}%>70%，"
                           "高换手活跃（代理，换手率无流通股本）")
            score += 12
        else:
            reasons.append(f"量比{v5 if v5 is None else round(v5, 2)} / 量分位"
                           f"{pctile20 if pctile20 is None else round(pctile20, 0)}%，"
                           "换手活跃度不足（代理口径）")

    # ---- 规则 2：压力位放量整理站稳 ----
    press = None
    if i >= 20:
        press = max(high[i - 20:i])  # 近20日前高（不含今日）
    consolidate_ok = False
    if press is not None and i >= 3:
        seg_close = close[i - 2:i + 1]
        in_zone = all(abs(c / press - 1.0) <= 0.03 for c in seg_close)
        stand = price >= press * 0.99
        avg3 = sum(vol[i - 2:i + 1]) / 3.0
        pre = vol[max(0, i - 22):i - 2]
        base20 = sum(pre) / max(1, len(pre))
        vol_expand = base20 > 0 and avg3 >= base20
        consolidate_ok = in_zone and stand and vol_expand
        if consolidate_ok:
            reasons.append(f"压力位{press:.2f}放量整理站稳（3日±3%内，收盘≥压力位99%，"
                           f"整理期均量{avg3:.0f}≥前20日均量{base20:.0f}）")
            score += 20
        elif stand and vol_expand:
            reasons.append(f"收盘站上压力位{press:.2f}且放量，但整理形态不完整")
            score += 8
        else:
            reasons.append(f"未在压力位{press:.2f}放量整理站稳")
    else:
        reasons.append("数据窗口不足，压力位无法计算")

    # ---- 规则 3：主力净流入为正 ----
    if mainnet is not None:
        if mainnet > 0:
            reasons.append(f"主力净流入{mainnet:.0f}>0，资金配合")
            score += 15
        else:
            reasons.append(f"主力净流入{mainnet:.0f}为负，接力资金缺席")
            score -= 10
    else:
        reasons.append("主力净流入缺失（降级跳过）")

    # ---- 规则 4：行业催化 ----
    if sector_hot is not None and my_industry:
        if my_industry in sector_hot:
            reasons.append(f"行业[{my_industry}]在热点板块榜，催化成立")
            score += 10
    else:
        degraded.append("行业催化：context 无 sector_hot 板块榜（moneyflow latest.txt "
                        "无行业字段），催化因子跳过")

    # ---- 汇总 ----
    score = max(0.0, min(100.0, score))
    if turnover_ok and consolidate_ok and score >= 60:
        action, pos = "buy", POSITION_PCT
        reasons.append("高换手接力买点成立，仓位 5 成")
    else:
        action, pos = "watch", 0.0
        reasons.append("核心条件（换手区间+压力位站稳）不齐或评分不足，观望")

    stop_loss = round(press * 0.97, 4) if press else (round(ema5[i], 4) if ema5[i] else None)
    take_profit = round(price * 1.15, 4)
    return {
        "action": action, "score": round(score, 1), "position_pct": pos,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "turnover_pct": turnover,
            "vol_ratio5": round(v5, 2) if v5 is not None else None,
            "vol_ratio20_proxy": round(vr20[i], 2) if vr20[i] is not None else None,
            "vol_pctile20": round(pctile20, 1) if pctile20 is not None else None,
            "pressure_20d": round(press, 4) if press else None,
            "consolidate_stand": consolidate_ok,
            "mainnet": mainnet,
            "chg_pct": round(chg[i], 2) if chg[i] is not None else None,
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
