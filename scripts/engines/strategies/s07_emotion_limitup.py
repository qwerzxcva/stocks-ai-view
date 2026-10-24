#!/usr/bin/env python3
"""s07_emotion_limitup.py — S7 情绪打板策略（独立实现，策略规则来自用户提供）。激进档。

规则：
1. 涨幅 > 7% 冲板放量：当日涨幅>7% 且量比>2（冲板未封/封板均为打板语境）。
2. 换手 > 15% 且量比 > 2、资金流入前 50：
   - 换手率：K线无流通股本；context moneyflow 有 turnover_pct 时优先使用
     （快照晚于决策日拒绝，防前视），缺失降级为「量比>2 且量能20日分位>70%」代理。
   - 资金流入前 50：moneyflow latest.txt 仅 151 行非全市场快照，无法构建全市场
     净额排名 → 降级为「主力净流入为正（mainnet>0）」代理，并在 degraded 标注；
     context 若提供 fund_rank_top50（list[code]）则优先真实判定。
3. 大盘涨家 > 跌家、无跌停潮：
   - 涨/跌家数：context.market_anomaly 无涨/跌家数字段（只有事件计数）→ 降级，
     用 zt_flood>0（涨停潮事件）作为涨家占优弱代理；context 若提供
     adv_dec（{up, down}）则优先真实判定。
   - 跌停潮：context.dt_flood_count（跌停池精确计数优先，否则 anomaly dt_flood
     事件计数）；>0 视为有跌停潮 → 不开新仓。
4. 封板概率增强：context.ban_prob（连板实证概率，按当前连板数取 zt_next_rate）
   与 context.limitup_ml（涨停 ML lift）作为加分项；缺失降级跳过。
5. 卖出：
   - 封板失败：冲板（盘中最高触及涨停价）但收盘未涨停（炸板/未封）→ 卖出；
   - 次日不拉升：昨日涨停今日涨幅 < 2% → 卖出（连板情绪断档）。
     日线口径无「次日」实时判定，降级为「最近涨停日的下一交易日涨幅<2%」。
6. 仓位 5-8 成（score>=80 → 8 成，65-80 → 6 成，55-65 → 5 成）。

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

STRATEGY = "s07_emotion_limitup"


def _board(code: str) -> str:
    c = _loader.normalize_code(code)
    if c.startswith("bj"):
        return "bse"
    if c.startswith(("sz30", "sh68")):
        return "star"
    return "main"


def _vol_pctile(vol: list, i: int, win: int = 20) -> Optional[float]:
    if i < win - 1:
        return None
    seg = vol[i - win + 1:i + 1]
    return sum(1 for v in seg if v <= vol[i]) / len(seg) * 100.0


def _moneyflow_usable(context: dict, code: str, last_date: Optional[str]) -> Optional[dict]:
    mf = ctx_mod.moneyflow_for(context, _loader.normalize_code(code))
    if mf is None:
        return None
    snap = mf.get("date")
    if last_date and snap and snap > last_date:
        return None
    return mf


def _ban_prob_for(context: dict, lianban: int) -> Optional[float]:
    """ban_prob.json 按连板数取次日涨停概率（zt_next_rate%）。"""
    bp = context.get("ban_prob") or {}
    by_lb = ((bp.get("base") or {}).get("by_lianban") or {})
    name = {1: "首板", 2: "2连板", 3: "3连板"}.get(lianban)
    if name is None:
        name = "4连板+" if lianban >= 4 else None
    # 兼容 key 变体
    for key in ([name] if name else []) + [f"{lianban}连板", str(lianban)]:
        if key and key in by_lb:
            rate = by_lb[key].get("zt_next_rate")
            return float(rate) if rate is not None else None
    return None


def _limitup_ml_lift(context: dict) -> Optional[float]:
    """limitup_ml_results.json picks 最高 lift 值。"""
    ml = context.get("limitup_ml") or {}
    picks = ml.get("picks") or {}
    lifts = [float(v["lift"]) for v in picks.values()
             if isinstance(v, dict) and v.get("lift") is not None]
    return max(lifts) if lifts else None


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

    i = n - 1
    price = close[i]
    last_date = date[i] if date else None
    chg = ind.pct_chg(close)
    vr = ind.vol_ratio(vol, 5)
    lu = ind.consecutive_limit_up(close, _board(code))
    limit_thr = {"main": 9.8, "star": 19.8, "bse": 29.8}[_board(code)]
    reasons: list = []
    score = 40.0

    v = vr[i]
    mf = _moneyflow_usable(context, code, last_date)
    if mf is None:
        degraded.append("换手率/主力净流入：moneyflow 缺失或快照晚于决策日（防前视），"
                        "换手率降级为量比+量能分位代理")
    turnover = mf.get("turnover_pct") if mf else None
    mainnet = mf.get("mainnet") if mf else None
    pctile20 = _vol_pctile(vol, i, 20)

    # ---- 规则 5：卖出先行 ----
    # 5a. 封板失败：盘中最高涨幅触涨停但收盘未涨停（炸板）
    if i >= 1 and close[i - 1] > 0:
        high_chg = (high[i] / close[i - 1] - 1.0) * 100.0
        if high_chg >= limit_thr and (chg[i] is not None and chg[i] < limit_thr):
            reasons.append(f"盘中触板(+{high_chg:.1f}%)但收盘仅{chg[i]:.2f}%，封板失败（炸板），卖出")
            return {"action": "sell", "score": 15, "position_pct": 0,
                    "reasons": reasons, "stop_loss": None, "take_profit": None,
                    "indicators": {"high_chg_pct": round(high_chg, 2),
                                   "chg_pct": round(chg[i], 2)},
                    "date": last_date, "strategy": STRATEGY, "degraded": degraded}
    # 5b. 次日不拉升：昨日涨停、今日涨幅<2%
    if i >= 1 and lu[i - 1] >= 1 and lu[i] == 0 \
            and chg[i] is not None and chg[i] < 2.0:
        reasons.append(f"昨日{lu[i-1]}连板，今日涨幅{chg[i]:.2f}%<2%，次日不拉升，卖出")
        return {"action": "sell", "score": 20, "position_pct": 0,
                "reasons": reasons, "stop_loss": None, "take_profit": None,
                "indicators": {"prev_lianban": lu[i - 1], "chg_pct": round(chg[i], 2)},
                "date": last_date, "strategy": STRATEGY, "degraded": degraded}

    # ---- 规则 3：大盘环境 ----
    # dt_flood_count 为全局最新快照（无 asof 对齐），与决策日错位时降级为参考并标注，
    # 不做硬否决；硬否决仅在快照日期与决策日一致时启用。
    dt_flood = context.get("dt_flood_count")
    dt_pool_date = context.get("dt_pool_date")
    dt_aligned = dt_pool_date is not None and last_date is not None \
        and dt_pool_date <= last_date
    if dt_flood is not None and dt_flood > 0:
        if dt_aligned and dt_pool_date == last_date:
            reasons.append(f"跌停潮（dt_flood_count={dt_flood}，{dt_pool_date} 当日），"
                           "情绪冰点，不开新仓")
            return {"action": "watch", "score": 20, "position_pct": 0,
                    "reasons": reasons, "stop_loss": None, "take_profit": None,
                    "indicators": {"dt_flood_count": dt_flood},
                    "date": last_date, "strategy": STRATEGY, "degraded": degraded}
        degraded.append(
            f"跌停潮判定：dt_flood_count={dt_flood} 为 {dt_pool_date} 全局快照，"
            f"与决策日 {last_date} 错位（无 asof 对齐能力），降级为参考、不硬否决")
        reasons.append(f"跌停潮快照{dt_flood}（{dt_pool_date}）与决策日错位，仅参考")
        score -= 5
    adv_dec = context.get("adv_dec")  # {"up":.., "down":..} 可选真实涨跌家数
    ec = context.get("market_event_counts") or {}
    if adv_dec and adv_dec.get("up") is not None:
        if adv_dec["up"] > (adv_dec.get("down") or 0):
            reasons.append(f"涨家{adv_dec['up']}>跌家{adv_dec.get('down')}，情绪面配合")
            score += 10
        else:
            reasons.append(f"涨家{adv_dec['up']}≤跌家{adv_dec.get('down')}，情绪偏弱")
            score -= 10
    else:
        degraded.append("涨/跌家数：market_anomaly 无涨跌家数字段，降级为 zt_flood 事件弱代理")
        if ec.get("zt_flood", 0) > 0:
            reasons.append(f"涨停潮事件{ec['zt_flood']}起，涨家占优（弱代理）")
            score += 6
        else:
            reasons.append("无涨停潮事件，涨家占优无法确认（弱代理中性）")

    # ---- 规则 1：涨幅>7% 冲板放量 ----
    surge_ok = chg[i] is not None and chg[i] > 7.0 and v is not None and v > 2.0
    if surge_ok:
        reasons.append(f"涨幅{chg[i]:.2f}%>7% 且量比{v:.2f}>2，冲板放量")
        score += 25
    else:
        reasons.append(f"涨幅{chg[i] if chg[i] is None else round(chg[i], 2)}% / 量比"
                       f"{v if v is None else round(v, 2)}，冲板放量不成立")

    # ---- 规则 2：换手>15% 量比>2 资金流入前50 ----
    if turnover is not None:
        if turnover > 15.0:
            reasons.append(f"真实换手率{turnover:.1f}%>15%，情绪活跃")
            score += 12
        else:
            reasons.append(f"换手率{turnover:.1f}%≤15%")
    else:
        if v is not None and v > 2.0 and pctile20 is not None and pctile20 > 70.0:
            reasons.append(f"量比{v:.2f}>2 且量能分位{pctile20:.0f}%>70%，高换手（代理）")
            score += 8
        else:
            reasons.append("换手活跃度不足（代理口径）")

    fund_top50 = context.get("fund_rank_top50")  # list[code] 可选真实榜
    code_norm = _loader.normalize_code(code)
    if fund_top50 is not None:
        if code_norm in fund_top50:
            reasons.append("资金流入前50（真实榜）")
            score += 10
        else:
            reasons.append("不在资金流入前50榜")
    else:
        degraded.append("资金流入前50：moneyflow 非全市场快照（仅151行），无法构建全市场"
                        "排名，降级为主力净流入为正代理")
        if mainnet is not None and mainnet > 0:
            reasons.append(f"主力净流入{mainnet:.0f}>0（前50代理）")
            score += 6
        elif mainnet is not None:
            reasons.append(f"主力净流入{mainnet:.0f}≤0（前50代理不满足）")
        else:
            reasons.append("主力净流入缺失（前50代理跳过）")

    # ---- 规则 4：ban_prob + limitup_ml 增强 ----
    lb_now = lu[i] if lu[i] >= 1 else (lu[i - 1] + 1 if i >= 1 and lu[i - 1] >= 1 else 1)
    ban_rate = _ban_prob_for(context, lb_now)
    if ban_rate is not None:
        reasons.append(f"ban_prob：{lb_now}板次日涨停概率{ban_rate:.1f}%（实证）")
        if ban_rate >= 20.0:
            score += 10
        elif ban_rate >= 5.0:
            score += 4
        else:
            score -= 5
    else:
        degraded.append("ban_prob：缺失或该连板档无样本，封板概率增强跳过")
    ml_lift = _limitup_ml_lift(context)
    if ml_lift is not None:
        reasons.append(f"limitup_ml 最高 lift={ml_lift:.2f}")
        if ml_lift >= 1.3:
            score += 6
        elif ml_lift >= 1.0:
            score += 3
    else:
        degraded.append("limitup_ml：缺失，ML 增强跳过")

    # ---- 汇总：仓位 5-8 成 ----
    score = max(0.0, min(100.0, score))
    if surge_ok and score >= 80:
        action, pos = "buy", 80.0
        reasons.append("打板信号强，仓位 8 成")
    elif surge_ok and score >= 65:
        action, pos = "buy", 60.0
        reasons.append("打板信号中等，仓位 6 成")
    elif surge_ok and score >= 55:
        action, pos = "buy", 50.0
        reasons.append("打板信号偏弱，仓位 5 成")
    else:
        action, pos = "watch", 0.0
        reasons.append("冲板放量核心条件不成立或评分不足，观望")

    prev_low = low[i - 1] if i >= 1 else None
    stop_loss = round(prev_low, 4) if prev_low else None
    take_profit = round(price * 1.10, 4)
    return {
        "action": action, "score": round(score, 1), "position_pct": pos,
        "reasons": reasons, "stop_loss": stop_loss, "take_profit": take_profit,
        "indicators": {
            "chg_pct": round(chg[i], 2) if chg[i] is not None else None,
            "vol_ratio5": round(v, 2) if v is not None else None,
            "turnover_pct": turnover,
            "vol_pctile20": round(pctile20, 1) if pctile20 is not None else None,
            "lianban": lu[i],
            "dt_flood_count": dt_flood,
            "dt_pool_date": dt_pool_date,
            "ban_prob_zt_next_pct": ban_rate,
            "limitup_ml_lift": ml_lift,
            "mainnet": mainnet,
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
