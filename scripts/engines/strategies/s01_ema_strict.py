#!/usr/bin/env python3
"""s01_ema_strict.py — S1 EMA 趋势严进策略（不追高）。

策略规则来自用户提供，独立实现。保守型（熊市/震荡市优先）。

四大规则：
1. 严进不追高：乖离率 (现价-EMA5)/EMA5*100%。<2% 最佳买点，2-5% 可小仓，>5% 严禁追高判观望。
2. 趋势顺势：多头排列 EMA5>EMA10>EMA20 为必须条件；空头排列坚决不碰；均线发散上行优于粘合。
3. 效率优先（筹码）：90%集中度<15% 表筹码集中；获利盘 70-90% 警惕回吐；现价高于均本 5-15% 健康。
4. 买点偏好：最佳=缩量回踩 EMA5 获支撑；次优=回踩 EMA10 获支撑；跌破 EMA20 观望。

数据降级说明：
  - 筹码集中度/获利比例/平均成本：K线无筹码分布数据，用「近 N 日成交量加权均价(VWAP)」
    近似平均成本，用「现价相对近 60 日成本分布的分位」近似获利盘。docstring 标注，context
    若提供真实筹码数据则优先使用。

统一接口：evaluate(code, kline, context) -> dict
  kline: {'date':[..], 'open':[..], 'close':[..], 'high':[..], 'low':[..], 'vol':[..]} 升序
  返回 {action, score, position_pct, reasons, stop_loss, take_profit, indicators}
"""
from __future__ import annotations

import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _indicators as ind  # noqa: E402


def _vwap(close, vol, window):
    """成交量加权均价（近似平均成本）。"""
    n = len(close)
    if n == 0:
        return None
    w = min(window, n)
    seg_c = close[-w:]
    seg_v = vol[-w:]
    tv = sum(seg_v)
    if tv <= 0:
        return sum(seg_c) / w
    return sum(c * v for c, v in zip(seg_c, seg_v)) / tv


def _profit_ratio(close, vol, window=60):
    """获利比例近似：现价高于近 window 日 VWAP 成本线的成交量占比。
    返回 0-100。K线无真实筹码，这是降级近似。"""
    n = len(close)
    if n < 5:
        return None
    w = min(window, n)
    cur = close[-1]
    profit_vol = 0.0
    total_vol = 0.0
    for i in range(n - w, n):
        v = vol[i]
        total_vol += v
        if close[i] <= cur:  # 该日买入成本低于现价 → 获利
            profit_vol += v
    if total_vol <= 0:
        return None
    return profit_vol / total_vol * 100.0


def _concentration(close, vol, window=60):
    """筹码集中度近似：用成交量加权价格的变异系数反推。
    真实 90% 集中度需筹码分布，此处用价格离散度代理：值越小越集中。返回近似百分比。"""
    n = len(close)
    if n < 10:
        return None
    w = min(window, n)
    seg = close[-w:]
    m = sum(seg) / w
    if m <= 0:
        return None
    var = sum((x - m) ** 2 for x in seg) / w
    cv = (var ** 0.5) / m * 100.0  # 变异系数%
    return cv  # 越小越集中，粗略对应「集中度」的反面；<15 视为较集中


def evaluate(code: str, kline: dict, context: Optional[dict] = None) -> dict:
    context = context or {}
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
                "take_profit": None, "indicators": {}, "strategy": "s01_ema_strict"}

    ema5 = ind.ema(close, 5)
    ema10 = ind.ema(close, 10)
    ema20 = ind.ema(close, 20)
    bias5 = ind.bias(close, ema5)
    vr = ind.vol_ratio(vol, 5)

    i = n - 1  # 最新一根
    price = close[i]
    e5, e10, e20 = ema5[i], ema10[i], ema20[i]
    b5 = bias5[i]

    reasons = []
    score = 50.0
    action = "hold"
    position_pct = 0.0

    # ---- 规则 2：趋势多头必须 ----
    bullish = (e5 is not None and e10 is not None and e20 is not None
               and e5 > e10 > e20)
    if not bullish:
        reasons.append("非多头排列(EMA5>EMA10>EMA20不满足)，坚决不碰")
        return {"action": "avoid", "score": 20, "position_pct": 0,
                "reasons": reasons, "stop_loss": e20, "take_profit": None,
                "indicators": {"ema5": e5, "ema10": e10, "ema20": e20, "bias5": b5},
                "strategy": "s01_ema_strict"}
    reasons.append("多头排列 EMA5>EMA10>EMA20 成立")
    score += 15

    # 均线发散上行优于粘合：间距是否扩大
    if i >= 5 and all(x is not None for x in (ema5[i], ema10[i], ema5[i-5], ema10[i-5])):
        spread_now = (ema5[i] - ema10[i]) / ema10[i] * 100  # type: ignore[operator]
        spread_prev = (ema5[i-5] - ema10[i-5]) / ema10[i-5] * 100  # type: ignore[operator]
        if spread_now > spread_prev:
            reasons.append(f"均线发散上行(间距{spread_prev:.2f}%→{spread_now:.2f}%)")
            score += 8
        else:
            reasons.append("均线粘合/收敛，趋势强度一般")

    # ---- 规则 1：严进不追高（乖离率硬门槛）----
    if b5 is None:
        reasons.append("乖离率无法计算")
    elif b5 > 5.0:
        reasons.append(f"乖离率{b5:.2f}%>5%，严禁追高，判观望")
        return {"action": "watch", "score": 35, "position_pct": 0,
                "reasons": reasons, "stop_loss": e20, "take_profit": None,
                "indicators": {"ema5": e5, "ema10": e10, "ema20": e20, "bias5": b5},
                "strategy": "s01_ema_strict"}
    elif b5 < 2.0:
        reasons.append(f"乖离率{b5:.2f}%<2%，最佳买点区间")
        score += 20
        position_pct = 50.0
        action = "buy"
    else:
        reasons.append(f"乖离率{b5:.2f}% 在2-5%，可小仓介入")
        score += 10
        position_pct = 25.0
        action = "buy"

    # ---- 规则 4：买点偏好（回踩支撑）----
    # 缩量回踩 EMA5：收盘接近 EMA5 且当日量 < 5 日均量
    near_ema5 = e5 is not None and abs(price - e5) / e5 < 0.015
    near_ema10 = e10 is not None and abs(price - e10) / e10 < 0.02
    shrink = vr[i] is not None and vr[i] < 1.0  # 量比<1 = 缩量
    broke_ema20 = e20 is not None and price < e20

    if broke_ema20:
        reasons.append("已跌破 EMA20，观望")
        action = "watch"
        position_pct = 0.0
        score -= 20
    elif near_ema5 and shrink:
        reasons.append("缩量回踩 EMA5 获支撑（最佳买点）")
        score += 12
    elif near_ema10:
        reasons.append("回踩 EMA10 获支撑（次优买点）")
        score += 6

    # ---- 规则 3：筹码效率（降级近似）----
    conc = _concentration(close, vol, 60)
    pratio = _profit_ratio(close, vol, 60)
    avg_cost = _vwap(close, vol, 60)
    if conc is not None:
        if conc < 15.0:
            reasons.append(f"筹码较集中(离散度{conc:.1f}%≈集中度<15%)")
            score += 5
        else:
            reasons.append(f"筹码较分散(离散度{conc:.1f}%)")
    if pratio is not None:
        if 70.0 <= pratio <= 90.0:
            reasons.append(f"获利盘{pratio:.0f}% 在70-90%区间，警惕获利回吐")
            score -= 8
        elif pratio > 90.0:
            reasons.append(f"获利盘{pratio:.0f}%>90%，回吐风险高")
            score -= 12
    if avg_cost is not None and avg_cost > 0:
        above = (price / avg_cost - 1.0) * 100.0
        if 5.0 <= above <= 15.0:
            reasons.append(f"现价高于均本{above:.1f}%，处于5-15%健康区")
            score += 5
        elif above > 15.0:
            reasons.append(f"现价高于均本{above:.1f}%>15%，偏离过大")
            score -= 5

    score = max(0.0, min(100.0, score))
    stop_loss = e20  # 生命线止损
    take_profit = None
    if action == "buy" and e5 is not None:
        take_profit = round(e5 * 1.05, 4)  # 乖离到 5% 止盈参考

    return {
        "action": action,
        "score": round(score, 1),
        "position_pct": position_pct,
        "reasons": reasons,
        "stop_loss": round(stop_loss, 4) if stop_loss else None,
        "take_profit": take_profit,
        "indicators": {
            "ema5": round(e5, 4) if e5 else None,
            "ema10": round(e10, 4) if e10 else None,
            "ema20": round(e20, 4) if e20 else None,
            "bias5_pct": round(b5, 2) if b5 is not None else None,
            "vol_ratio": round(vr[i], 2) if vr[i] is not None else None,
            "concentration_proxy": round(conc, 2) if conc is not None else None,
            "profit_ratio_pct": round(pratio, 1) if pratio is not None else None,
            "avg_cost": round(avg_cost, 4) if avg_cost else None,
        },
        "date": date[i] if date else None,
        "strategy": "s01_ema_strict",
        "degraded": ["筹码集中度/获利盘/平均成本为K线降级近似"],
    }


# 分片角色：全历史主分片（行数多，复权基准）vs 增量分片（行数少，补最新）。
# 复权基准不一致修正（R1 发现）：同一股票跨分片价格基准不同，规则=主分片优先，
# 增量分片仅用于补充主分片缺失的最新日期，且价格不与前复权历史混排直接比较——
# 策略只用序列内相对关系（EMA/乖离），混排时以主分片为基准段，增量段接在其后。
_HISTORY_SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
                   "20", "30", "68"]
_INCREMENTAL_SHARDS = ["60", "recent", "extra"]


def _read_shard(path: str, target: str) -> dict:
    """读单个分片里某股票的全部行，返回 {date: (o,c,h,l,v)}。"""
    rows = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith(target):
                    p = line.rstrip("\n").split("|")
                    if len(p) >= 7:
                        try:
                            rows[p[1]] = (float(p[2]), float(p[3]), float(p[4]),
                                          float(p[5]), float(p[6]))
                        except ValueError:
                            continue
    except FileNotFoundError:
        pass
    return rows


def load_kline_from_bulk(data_dir: str, code: str) -> dict:
    """从 bulk 分片读取单只股票 K 线，主分片优先 + 增量补最新 + 按日期去重排序。
    code 如 sh600519 或 600519。返回升序 K 线。"""
    import glob
    if not code.startswith(("sh", "sz", "bj")):
        pref = "sh" if code.startswith(("60", "68")) else (
            "sz" if code.startswith(("00", "30")) else "bj")
        code = pref + code
    target = code + "|"
    bulk = os.path.join(data_dir, "bulk")

    # 1) 主分片：收集全历史（取行数最多的那个分片作为复权基准）
    hist_rows: dict = {}
    for shard in _HISTORY_SHARDS:
        rows = _read_shard(os.path.join(bulk, f"day_qfq_{shard}.csv"), target)
        if len(rows) > len(hist_rows):
            hist_rows = rows

    # 2) 增量分片：只补主分片没有的更新日期
    if hist_rows:
        latest_hist = max(hist_rows.keys())
        for shard in _INCREMENTAL_SHARDS:
            for path in glob.glob(os.path.join(bulk, f"day_qfq_{shard}*.csv")):
                rows = _read_shard(path, target)
                for d, v in rows.items():
                    if d > latest_hist and d not in hist_rows:
                        hist_rows[d] = v
    else:
        # 主分片没有，兜底全部增量分片
        for path in glob.glob(os.path.join(bulk, "day_qfq_*.csv")):
            hist_rows.update(_read_shard(path, target))

    dates = sorted(hist_rows.keys())
    return {
        "date": dates,
        "open": [hist_rows[d][0] for d in dates],
        "close": [hist_rows[d][1] for d in dates],
        "high": [hist_rows[d][2] for d in dates],
        "low": [hist_rows[d][3] for d in dates],
        "vol": [hist_rows[d][4] for d in dates],
    }


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600519")
    # HERE=scripts/engines/strategies，仓库根需上三级
    ap.add_argument("--data", default=os.path.normpath(os.path.join(HERE, "..", "..", "..", "data")))
    ap.add_argument("--live", default=None)
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，严格无前视")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    import _loader  # noqa: E402
    import _context as ctx_mod  # noqa: E402
    kl = load_kline_from_bulk(args.data, args.code)
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
