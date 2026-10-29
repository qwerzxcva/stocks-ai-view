#!/usr/bin/env python3
"""engines_v2.py — flow / tape / rotation 三引擎的**云端参考实现**与黄金向量生成器。

## 为什么要有这个文件

`flow` / `tape` / `rotation` 最早是在端上（Kotlin）写出来的，云端没有对应实现，
于是它们拿不到黄金向量、进不了一致性执法：端上改了公式没有任何东西会红。
一个只在单侧存在的引擎，本质上和「写死在界面里的经验值」没有区别。

本文件按端上实现逐字段移植成 Python，作为**参考实现**：

    flow      engine-algo/.../FlowEngine.kt      （横截面分位 + 三档置信 + 流动性折算）
    tape      engine-algo/.../TapeEngine.kt      （收盘位置/量价背离/影线/缺口/5日位置）
    rotation  engine-algo/.../RotationEngine.kt  （板块强度/5日动量/广度/额占比 → 轮动）

`--golden` 用确定性夹具（不依赖网络与随机数）算出期望值写进
`data/parity/golden_v2.json`；端上 `EngineV2ParityTest` 用同一批夹具复算并逐字段比对。
夹具里的大样本用**整数算术生成器**（`flow_synth_v1` / `rotation_synth_v1`），
两端各自实现同一段整数运算，浮点结果逐位相同，JSON 里只存生成器参数不存几百行数据。

## 用法

    python3 scripts/engines_v2.py --golden data/parity/golden_v2.json
    python3 scripts/engines_v2.py --selftest      # 只跑内置夹具，打印关键输出

## 口径

参数全部与端上 `Params` 默认值一致；任何一侧改参数都必须同时改这里与 Kotlin 默认值，
并重新生成 golden——否则 parity 测试会红，这正是它存在的意义。
"""
import argparse
import json

from numcmp import deep_diff
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SPEC_VERSION = "3.1.0"

# ── 参数（与端上 Params 默认值逐项一致）────────────────────────────
FLOW_P = {
    "pctSd": 5.0, "tanhK": 1.5, "turnoverFloor": 0.5, "turnoverHigh": 20.0,
    "lowTurnWeight": 0.3, "highTurnWeight": 0.5, "volRatioK": 0.5, "volRatioCap": 2.0,
    "wCross": 0.6, "wAbs": 0.4, "gatedWCross": 0.3, "minPeerN": 300,
    "limitAmountRatio": 0.5, "limitWeight": 0.2, "mainUnit": 0.05,
    "bullThreshold": 65.0, "bearThreshold": 35.0,
    "bullBreadthGate": 0.85, "bearBreadthGate": 0.15, "minCloudN": 30,
    "minAmountYuan": 1.0e7,
}

TAPE_P = {
    "ampK": 3.0, "divergePerVr": 1.0, "divergeScale": 2.0,
    "wBuy": 12.0, "wDiverge": 8.0, "wShadow": 6.0, "wGap": 6.0, "wPos": 6.0,
    "bullThreshold": 62.0, "bearThreshold": 38.0,
    "gapMinPct": 1.0, "shadowLong": 0.5, "abnormalGapPct": 11.0,
    "posBars": 5, "minBars": 2, "minAmountYuan": 1.0e7,
    "openMinute": 570, "closeAuctionMinute": 897, "closeConfidence": 0.8,
    "minUsedWeight": 10,
}

ROT_P = {
    "wStrength": 0.35, "wMom5": 0.25, "wBreadth": 0.20, "wAmt": 0.20,
    "minSectors": 8, "minMembers": 5, "dispGate": 1.2,
    "attackE": 30.0, "defenseE": -30.0, "bullRot": 0.35, "bearRot": -0.35,
    "breadthPanic": 0.15, "limitHotShare": 0.02, "minCoverage": 0.75,
    "minUsableWeight": 0.40, "topN": 3, "mom5Days": 5,
}

TAPE_FULL_WEIGHT = 38.0


# ── 通用工具 ──────────────────────────────────────────────────────
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def java_round2(x):
    """Java 的 `Math.round(x*100)/100`：四舍五入到分（0.5 一律进位）。"""
    return math.floor(x * 100.0 + 0.5) / 100.0


def limit_cap(code, name=""):
    base = 10.0
    if code[:3] in ("300", "301", "688", "689"):
        base = 20.0
    elif code[:1] in ("8", "4") or code[:2] == "92":
        base = 30.0
    if base == 10.0 and "ST" in (name or "").upper():
        return 5.0
    return base


def at_limit_up(row):
    cap = limit_cap(row["code"], row.get("name"))
    prev = row.get("prevClose")
    px = row.get("price")
    if prev is not None and px is not None and px > 0:
        return px >= java_round2(prev * (1 + cap / 100.0)) - 0.005
    pct = row.get("pctChg")
    if pct is None:
        return False
    return pct >= cap - max(cap * 0.035, 0.2)


def at_limit_down(row):
    cap = limit_cap(row["code"], row.get("name"))
    prev = row.get("prevClose")
    px = row.get("price")
    if prev is not None and px is not None and px > 0:
        return px <= java_round2(prev * (1 - cap / 100.0)) + 0.005
    pct = row.get("pctChg")
    if pct is None:
        return False
    return pct <= -(cap - max(cap * 0.035, 0.2))


def tradable(row):
    return (row.get("price") or 0.0) > 0.0 and (row.get("volumeLots") or 0.0) > 0.0


def percentile_of(sorted_vals, v):
    """分位（0-100）：小于 + 半个等于，浮点按 1e-9 容差判等（与端上二分实现等价）。"""
    if not sorted_vals:
        return float("nan")
    eps = 1e-9
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < v - eps:
            lo = mid + 1
        else:
            hi = mid
    first = lo
    lo2, hi2 = first, len(sorted_vals)
    while lo2 < hi2:
        mid = (lo2 + hi2) // 2
        if sorted_vals[mid] <= v + eps:
            lo2 = mid + 1
        else:
            hi2 = mid
    equal = lo2 - first
    return 100.0 * (first + 0.5 * equal) / len(sorted_vals)


# ── 波动率目标闸门（端上 VolTargetGate 的同一条公式）────────────────
VOL_P = {"volTargetAnnual": 0.12, "window": 60, "floor": 0.2, "maxPos": 0.8}


def vol_target_compute(daily_returns, p=None):
    """realized = stdev(近 window 日) × √252；scale = clamp(target/realized, floor, 1.0)。

    `min_need = max(20, window//2)`：样本不够时 scale 置 1.0 且标 insufficient，
    不拿中性值冒充——端上 `VolTargetGate` 必须与这行一致。
    """
    p = p or VOL_P
    usable = [r for r in daily_returns if r is not None and math.isfinite(r)]
    min_need = max(20, p["window"] // 2)
    if len(usable) < min_need:
        return {"scale": 1.0, "realizedVol": None, "sampleDays": len(usable),
                "status": "insufficient"}
    win = usable[-p["window"]:]
    mean = sum(win) / len(win)
    var = sum((x - mean) ** 2 for x in win) / len(win)
    realized = math.sqrt(var) * math.sqrt(252.0)
    if not realized > 0.0:
        return {"scale": 1.0, "realizedVol": realized, "sampleDays": len(win), "status": "ok"}
    scale = max(p["floor"], min(1.0, p["volTargetAnnual"] / realized))
    return {"scale": scale, "realizedVol": realized, "sampleDays": len(win), "status": "ok"}


# ── flow ────────────────────────────────────────────────────────
def _flow_cell(row, amount5d, turn_scale, p):
    amount = row.get("amount")
    pct = row.get("pctChg")
    turn = row.get("turnoverRate")
    if amount is None or pct is None or turn is None:
        return None
    if amount <= 0.0:
        return None
    if abs(pct) > limit_cap(row["code"], row.get("name")) + 0.5:
        return None
    t = math.tanh(clamp(pct / p["pctSd"], -3.0, 3.0) / p["tanhK"])
    turn_adj = turn * turn_scale
    if turn_adj < p["turnoverFloor"]:
        liq = p["lowTurnWeight"]
    elif turn_adj > p["turnoverHigh"]:
        liq = p["highTurnWeight"]
    else:
        liq = 1.0
    if amount < p["minAmountYuan"]:
        liq *= p["lowTurnWeight"]
    vr = row.get("volRatio")
    bounded = clamp((vr - 1.0) / 2.0, -1.0, 1.0) if vr is not None else 0.0
    vr_f = 1.0 + p["volRatioK"] * bounded
    locked = (at_limit_up(row) or at_limit_down(row)) and amount5d is not None \
        and amount5d > 0 and amount < p["limitAmountRatio"] * amount5d
    if locked:
        liq *= p["limitWeight"]
    return {
        "code": row["code"],
        "netProxy": amount * t / 3.0 * liq * vr_f,
        "fsiAbs": clamp(50.0 + 50.0 * math.tanh(3.0 * t), 0.0, 100.0),
        "liq": liq,
        "vrF": vr_f,
        "limitLocked": locked,
    }


def flow_screen(rows, p=None, amount5d=None, cloud=None, breadth=None, turn_scale=1.0):
    p = p or FLOW_P
    amount5d = amount5d or {}
    cloud = cloud or {}
    suspended = {r["code"] for r in rows if not tradable(r)}
    cells, anomalies = [], {}
    for r in rows:
        if not tradable(r):
            continue
        c = _flow_cell(r, amount5d.get(r["code"]), turn_scale, p)
        if c is None:
            anomalies[r["code"]] = "输入缺字段或除权异常"
            continue
        cells.append(c)
    by_code = {c["code"]: c for c in cells}
    net_sorted = sorted(c["netProxy"] for c in cells)
    n = len(net_sorted)
    use_cross = n >= p["minPeerN"]
    gated = breadth is not None and (breadth > p["bullBreadthGate"] or breadth < p["bearBreadthGate"])
    w1 = 0.0 if not use_cross else (p["gatedWCross"] if gated else p["wCross"])
    w2 = 1.0 - w1
    cloud_ok = len(cloud) >= p["minCloudN"]

    out = []
    for r in rows:
        code = r["code"]
        if code in suspended:
            out.append({"code": code, "score": None, "signal": "insufficient", "tier": "none",
                        "netProxyYuan": None, "fsiPct": None, "fsiAbs": None, "pctMain": None,
                        "m5Ratio": None, "scoreDelta": None, "liquidityWeight": 0.0,
                        "volRatioFactor": 1.0, "confidence": 0.0,
                        "reasons": ["停牌/无成交，不进横截面"]})
            continue
        if code in anomalies:
            out.append({"code": code, "score": None, "signal": "insufficient", "tier": "none",
                        "netProxyYuan": None, "fsiPct": None, "fsiAbs": None, "pctMain": None,
                        "m5Ratio": None, "scoreDelta": None, "liquidityWeight": 0.0,
                        "volRatioFactor": 1.0, "confidence": 0.0, "reasons": [anomalies[code]]})
            continue
        c = by_code.get(code)
        if c is None:
            out.append({"code": code, "score": None, "signal": "insufficient", "tier": "none",
                        "netProxyYuan": None, "fsiPct": None, "fsiAbs": None, "pctMain": None,
                        "m5Ratio": None, "scoreDelta": None, "liquidityWeight": 0.0,
                        "volRatioFactor": 1.0, "confidence": 0.0, "reasons": ["代理量缺失"]})
            continue
        fsi_pct = percentile_of(net_sorted, c["netProxy"]) if use_cross else None
        local = w1 * (fsi_pct if use_cross else c["fsiAbs"]) + w2 * c["fsiAbs"]
        amount = r.get("amount")
        main = None
        if r.get("mainNet") is not None and amount is not None and amount > 0:
            main = r["mainNet"] / amount
        cf = cloud.get(code)
        pct_main = cf["pctMain"] if cf else main
        m5 = cf.get("m5Ratio") if cf else None
        m1 = 50.0 + 50.0 * math.tanh(pct_main / p["mainUnit"]) if pct_main is not None else None
        m5s = 50.0 + 50.0 * math.tanh(m5 / p["mainUnit"]) if m5 is not None else None
        reasons = []
        if gated:
            reasons.append("普涨/普跌日，横截面分位失效")
        if not use_cross:
            reasons.append("横截面样本 %d < %d，只出绝对强度" % (n, p["minPeerN"]))
        if c["limitLocked"]:
            reasons.append("封板且成交额不足，代理量已压权")
        if cf is not None and not cloud_ok:
            reasons.append("云端序列样本不足")
        if m1 is not None and m5s is not None and cloud_ok:
            score = 0.40 * local + 0.30 * m1 + 0.30 * m5s
            tier, conf = "cloud", 0.95
        elif m1 is not None:
            score = 0.65 * local + 0.35 * m1
            tier, conf = "local_main", 0.85
        else:
            score = local
            tier = "local" if use_cross else "local_abs"
            conf = 0.70 if use_cross else 0.42
        s = clamp(score, 0.0, 100.0)
        out.append({
            "code": code, "score": s,
            "signal": "bullish" if s >= p["bullThreshold"] else ("bearish" if s <= p["bearThreshold"] else "neutral"),
            "tier": tier, "netProxyYuan": c["netProxy"], "fsiPct": fsi_pct, "fsiAbs": c["fsiAbs"],
            "pctMain": pct_main, "m5Ratio": m5,
            "scoreDelta": (s - local) if tier == "cloud" else None,
            "liquidityWeight": c["liq"], "volRatioFactor": c["vrF"], "confidence": conf,
            "reasons": reasons,
        })
    return out


# ── tape ────────────────────────────────────────────────────────
def cst_minute(now_ms):
    dt = datetime.fromtimestamp(now_ms / 1000.0, CST)
    return dt.hour * 60 + dt.minute


def _tape_empty(status, reasons, used_weight):
    return {"score": None, "signal": "insufficient", "buyProxy": None, "divergence": None,
            "upperShadow": None, "lowerShadow": None, "gapPct": None, "gapFilled": None,
            "closePos5": None, "confidence": 0.0, "usedWeight": used_weight,
            "status": status, "reasons": reasons}


def tape_compute(bars, row=None, now_ms=0, p=None):
    p = p or TAPE_P
    minute = cst_minute(now_ms)
    if minute < p["openMinute"]:
        return _tape_empty("pre_open", ["开盘前 open/prevClose 未定，缺口项不可算"], 0)
    last = bars[-1] if bars else None
    open_ = (row or {}).get("open") if row else None
    open_ = open_ if open_ is not None else (last["open"] if last else None)
    high = (row or {}).get("high") if row else None
    high = high if high is not None else (last["high"] if last else None)
    low = (row or {}).get("low") if row else None
    low = low if low is not None else (last["low"] if last else None)
    close = (row or {}).get("price") if row else None
    close = close if close is not None else (last["close"] if last else None)
    if open_ is None or high is None or low is None or close is None:
        return _tape_empty("insufficient", ["缺 open/high/low/close，结构分项全不可算"], 0)
    if row is None and len(bars) < p["minBars"]:
        return _tape_empty("insufficient", ["日 K 少于 %d 根且无实时行" % p["minBars"]], 0)

    reasons = []
    prev_bar = bars[-2] if len(bars) >= 2 else None
    pct = row.get("pctChg") if row else None
    if pct is None and prev_bar is not None and (prev_bar["close"] or 0) > 0:
        pct = (close / prev_bar["close"] - 1.0) * 100.0
    amplitude = row.get("amplitude") if row else None
    if amplitude is None and prev_bar is not None and (prev_bar["close"] or 0) > 0:
        amplitude = (high - low) / prev_bar["close"] * 100.0
    vol_ratio = row.get("volRatio") if row else None

    weight_sum = 0
    weighted = 0.0

    def add(w, s):
        nonlocal weight_sum, weighted
        weight_sum += int(w)
        weighted += w * clamp(s, -1.0, 1.0)

    rng = high - low
    cpr = clamp((close - low) / rng, 0.0, 1.0) if rng > 1e-9 else None
    if cpr is not None and amplitude is not None:
        shrink = clamp(amplitude / p["ampK"], 0.0, 1.0)
        buy_proxy = clamp(0.5 + (cpr - 0.5) * shrink, 0.0, 1.0)
        add(p["wBuy"], 2.0 * buy_proxy - 1.0)
        if buy_proxy >= 0.65:
            reasons.append("收在区间上沿且振幅够：主动买占优")
        if buy_proxy <= 0.35:
            reasons.append("收在区间下沿：主动卖占优")
    else:
        buy_proxy = None
        reasons.append("一字板/无振幅：内外盘代理不可算")

    divergence = None
    if pct is not None and vol_ratio is not None:
        e = clamp((pct - p["divergePerVr"] * (vol_ratio - 1.0)) / p["divergeScale"], -2.0, 2.0)
        divergence = e
        add(p["wDiverge"], e / 2.0)
        if vol_ratio >= 2.0 and abs(pct) <= 1.0:
            reasons.append("放量滞涨/滞跌")
        if vol_ratio >= 1.5 and pct < 0:
            reasons.append("放量下跌")

    if rng > 1e-9:
        upper = clamp((high - max(open_, close)) / rng, 0.0, 1.0)
        lower = clamp((min(open_, close) - low) / rng, 0.0, 1.0)
        add(p["wShadow"], lower - upper)
        if upper >= p["shadowLong"]:
            reasons.append("长上影：上方抛压")
        if lower >= p["shadowLong"]:
            reasons.append("长下影：下方承接")
    else:
        upper = lower = None

    prev_close = (row.get("prevClose") if row else None) or (prev_bar["close"] if prev_bar else None)
    gap_pct = gap_filled = None
    gap_item, gap_usable = 0.0, False
    if prev_close is not None and prev_close > 0 and row is not None:
        g = (open_ - prev_close) / prev_close * 100.0
        gap_pct = g
        cap = limit_cap(row["code"], row.get("name"))
        if abs(g) > cap + 0.5:
            gap_filled = None
            reasons.append("缺口超涨跌停上限，判除权/复牌，缺口项不计")
        else:
            prev_high = prev_bar["high"] if prev_bar else None
            prev_low = prev_bar["low"] if prev_bar else None
            up_gap = g >= p["gapMinPct"] and prev_high is not None and open_ > prev_high
            down_gap = g <= -p["gapMinPct"] and prev_low is not None and open_ < prev_low
            if up_gap:
                gap_item = -0.5 if close < prev_high else 1.0
                gap_filled = close < prev_high
            elif down_gap:
                gap_item = -1.0
                gap_filled = False
            gap_usable = gap_filled is not None or up_gap or down_gap
    if gap_usable:
        add(p["wGap"], gap_item)

    close_pos5 = None
    if len(bars) >= p["posBars"]:
        win = bars[-p["posBars"]:]
        hi5 = max(b["high"] for b in win)
        lo5 = min(b["low"] for b in win)
        if hi5 - lo5 > 1e-9:
            close_pos5 = clamp((close - lo5) / (hi5 - lo5), 0.0, 1.0)
            add(p["wPos"], 2.0 * close_pos5 - 1.0)
    else:
        reasons.append("日 K 不足 %d 根，收盘位置不计" % p["posBars"])

    if weight_sum < p["minUsedWeight"]:
        return _tape_empty("insufficient", reasons + ["可用权重 %d/38 过低" % weight_sum], weight_sum)

    score = 50.0 + TAPE_FULL_WEIGHT * (weighted / weight_sum)
    amount = row.get("amount") if row else None
    if amount is not None and amount < p["minAmountYuan"]:
        reasons.append("成交额过低，结构项噪声主导，不给分")
        return _tape_empty("illiquid", reasons, weight_sum)
    conf = p["closeConfidence"] if minute >= p["closeAuctionMinute"] else 1.0
    score = clamp(score, 0.0, 100.0)
    return {
        "score": score,
        "signal": "bullish" if score >= p["bullThreshold"] else ("bearish" if score <= p["bearThreshold"] else "neutral"),
        "buyProxy": buy_proxy, "divergence": divergence, "upperShadow": upper, "lowerShadow": lower,
        "gapPct": gap_pct, "gapFilled": gap_filled, "closePos5": close_pos5,
        "confidence": conf, "usedWeight": weight_sum, "status": "ok", "reasons": reasons,
    }


# ── rotation ────────────────────────────────────────────────────
def _rot_empty(status, reasons, coverage, mom_cov):
    return {"marketState": "unknown", "e": None, "marketRot": None, "dispersion": None,
            "strongest": [], "weakest": [], "ranked": [], "coverage": coverage,
            "momCoverage": mom_cov, "confidence": 0.0, "status": status, "reasons": reasons}


def _pctile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    idx = int((len(sorted_vals) - 1) * q)
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return sorted_vals[idx]


def rotation_compute(sectors, mom5=None, amt_delta=None, market_e=None, breadth=None, p=None):
    p = p or ROT_P
    rows = (sectors or {}).get("rows") or []
    coverage = (sectors or {}).get("coverage", 0.0)
    if not rows:
        return _rot_empty("insufficient", ["板块聚合不可得"], 0.0, 0.0)
    if len(rows) < p["minSectors"]:
        return _rot_empty("insufficient", ["板块数 %d < %d" % (len(rows), p["minSectors"])], coverage, 0.0)
    total_heat = sum(r["heat"] for r in rows)
    reasons = []
    if coverage < p["minCoverage"]:
        reasons.append("行业映射覆盖不足，热度加总失真")
    mapped = sum(r["n"] for r in rows)
    limit_hot = mapped > 0 and sum(r["limitUp"] for r in rows) / mapped > p["limitHotShare"]
    if limit_hot:
        reasons.append("涨停占比偏高，广度权重降低")

    def strength_of(r):
        return r.get("strength") if r.get("strength") is not None else r.get("chg1d")

    def breadth_of(r):
        d = r["up"] + r["down"]
        return None if d <= 0 else (r["up"] - r["down"]) / d

    strength_sorted = sorted(v for v in (strength_of(r) for r in rows) if v is not None)
    mom_sorted = sorted(v for v in ((mom5 or {}).get(r["name"]) for r in rows) if v is not None)
    amt_sorted = sorted(v for v in ((amt_delta or {}).get(r["name"]) for r in rows) if v is not None)
    mom_cov = sum(1 for r in rows if (mom5 or {}).get(r["name"]) is not None and
                  math.isfinite((mom5 or {}).get(r["name"]))) / len(rows)

    def pct2(v):
        return clamp(v / 50.0 - 1.0, -1.0, 1.0)

    ranked = []
    for r in rows:
        w_s = p["wStrength"]
        w_b = 0.10 if limit_hot else p["wBreadth"]
        w_m = p["wMom5"]
        w_a = p["wAmt"]
        if limit_hot:
            w_s += p["wBreadth"] - 0.10
        if mom_cov < 0.30:
            w_m = 0.0
        if total_heat <= 0.0 or amt_delta is None:
            w_a = 0.0
        s_val = strength_of(r)
        m_val = (mom5 or {}).get(r["name"])
        a_val = (amt_delta or {}).get(r["name"])
        br = breadth_of(r)
        acc = 0.0
        used = 0.0
        if s_val is not None and strength_sorted:
            acc += w_s * pct2(percentile_of(strength_sorted, s_val))
            used += w_s
        if m_val is not None and math.isfinite(m_val) and mom_sorted and w_m > 0.0:
            acc += w_m * pct2(percentile_of(mom_sorted, m_val))
            used += w_m
        if br is not None:
            acc += w_b * br
            used += w_b
        if a_val is not None and math.isfinite(a_val) and amt_sorted and w_a > 0.0:
            acc += w_a * pct2(percentile_of(amt_sorted, a_val))
            used += w_a
        ranked.append({
            "name": r["name"], "rot": (acc / used) if used > 0.0 else float("nan"),
            "strength": s_val, "mom5": m_val, "breadth": br, "amtDeltaPp": a_val,
            "n": r["n"], "lowN": r["n"] < p["minMembers"], "usedWeight": used,
        })
    ranked.sort(key=lambda x: x["rot"] if math.isfinite(x["rot"]) else -9.0, reverse=True)

    pool = [x for x in ranked if math.isfinite(x["rot"]) and x["usedWeight"] >= p["minUsableWeight"]]
    if not pool:
        return _rot_empty("insufficient", reasons + ["无板块权重达标"], coverage, mom_cov)
    rot_sorted = sorted(x["rot"] for x in pool)
    market_rot = _pctile(rot_sorted, 0.5)
    disp = _pctile(rot_sorted, 0.90) - _pctile(rot_sorted, 0.10)
    tradable_secs = [x for x in pool if not x["lowN"]]
    strongest = tradable_secs[: p["topN"]]
    weakest = list(reversed(tradable_secs))[: p["topN"]]
    if market_e is None:
        state = "unknown"
    elif market_e > p["attackE"] and disp < p["dispGate"]:
        state = "attack"
    elif market_e < p["defenseE"]:
        state = "defense"
    else:
        state = "balanced"
    if market_e is not None and market_e > p["attackE"] and disp >= p["dispGate"]:
        reasons.append("进攻但结构分化，降级均衡")
    if breadth is not None and breadth < p["breadthPanic"]:
        state = "defense"
        reasons.append("广度极弱，强制防守")
    if coverage < p["minCoverage"]:
        state = "unknown"
    if state == "unknown":
        conf = 0.30
    elif len(pool) < p["minSectors"]:
        conf = 0.50
    elif mom_cov < 0.30:
        conf = 0.65
    else:
        conf = 0.85
    return {
        "marketState": state, "e": market_e, "marketRot": market_rot, "dispersion": disp,
        "strongest": strongest, "weakest": weakest, "ranked": ranked,
        "coverage": coverage, "momCoverage": mom_cov, "confidence": conf,
        "status": "degraded" if state == "unknown" else "ok", "reasons": reasons,
    }


# ── 确定性夹具生成器（两端同算法，JSON 只存参数）────────────────────
FLOW_SYNTH = {"n": 320, "codeBase": 600000, "amountBase": 1.0e7, "amountStep": 1.0e5,
              "turnDiv": 10.0, "pctMod": 400, "cloudN": 30}


def flow_synth_rows(s):
    """`flow_synth_v1`：纯整数运算生成 n 行，两端浮点结果逐位相同。"""
    rows = []
    for i in range(s["n"]):
        code = "%06d" % (s["codeBase"] + i)
        pct = ((i * 37) % s["pctMod"]) / 10.0 - 20.0
        amount = s["amountBase"] + ((i * 7919) % 500) * s["amountStep"]
        turn = ((i * 13) % 300) / s["turnDiv"]
        vr = 0.5 + ((i * 17) % 40) / 10.0
        main_net = amount * (((i * 23) % 21) - 10) / 100.0
        prev_close = 10.0 + (i % 50) * 0.1
        price = prev_close * (1 + pct / 100.0)
        rows.append({
            "code": code, "name": "S%03d" % i, "price": price, "pctChg": pct,
            "volumeLots": 1000.0 + (i * 3) % 500, "amount": amount,
            "amplitude": abs(pct) + 1.0, "turnoverRate": turn, "volRatio": vr,
            "mainNet": main_net, "prevClose": prev_close,
            "high": max(price, prev_close), "low": min(price, prev_close), "open": prev_close,
        })
    return rows


def flow_synth_cloud(s):
    cloud = {}
    for i in range(s["cloudN"]):
        code = "%06d" % (s["codeBase"] + i)
        cloud[code] = {"pctMain": ((i * 11) % 41 - 20) / 200.0, "m5Ratio": ((i * 7) % 31 - 15) / 200.0}
    return cloud


ROT_SYNTH = {"n": 14, "codeBase": 0, "membersBase": 8, "membersStep": 3}


def rotation_synth_sectors(s):
    rows = []
    names = ["板块%02d" % i for i in range(s["n"])]
    for i, name in enumerate(names):
        n = s["membersBase"] + (i * s["membersStep"]) % 17
        up = (i * 5) % (n + 1)
        down = n - up
        rows.append({
            "name": name, "n": n,
            "chg1d": ((i * 13) % 90) / 10.0 - 4.0,
            "heat": 1.0e9 + ((i * 37) % 200) * 1.0e8,
            "up": up, "down": down, "limitUp": (i * 3) % 5,
            "limitDown": 0,
            "strength": ((i * 29) % 100),
        })
    mom5 = {r["name"]: ((i * 17) % 60) / 10.0 - 3.0 for i, r in enumerate(rows)}
    amt = {r["name"]: ((i * 23) % 40) / 10.0 - 2.0 for i, r in enumerate(rows)}
    return rows, mom5, amt


def build_golden():
    """生成三引擎黄金向量：小样本显式给出，大样本给生成器参数。"""
    # ── flow 小样本：覆盖停牌 / 缺字段 / 除权异常 / 封板压权 / mainNet 档 ──
    flow_rows = [
        {"code": "600001", "name": "A", "price": 11.0, "pctChg": 3.0, "volumeLots": 5000.0,
         "amount": 5.0e7, "amplitude": 5.0, "turnoverRate": 3.0, "volRatio": 1.6,
         "mainNet": 5.0e6, "prevClose": 10.68, "high": 11.2, "low": 10.5, "open": 10.7},
        {"code": "600002", "name": "B", "price": 0.0, "pctChg": 0.0, "volumeLots": 0.0,
         "amount": 0.0, "amplitude": 0.0, "turnoverRate": 0.0, "volRatio": None,
         "mainNet": None, "prevClose": 10.0, "high": 10.0, "low": 10.0, "open": 10.0},
        {"code": "600003", "name": "C", "price": 12.0, "pctChg": 12.5, "volumeLots": 800.0,
         "amount": 3.0e7, "amplitude": 12.0, "turnoverRate": 6.0, "volRatio": 2.0,
         "mainNet": None, "prevClose": 10.66, "high": 12.0, "low": 10.6, "open": 10.7},
        {"code": "600004", "name": "D", "price": 9.0, "pctChg": -5.0, "volumeLots": 200.0,
         "amount": 5.0e6, "amplitude": 6.0, "turnoverRate": 0.2, "volRatio": 0.8,
         "mainNet": -1.0e6, "prevClose": 9.47, "high": 9.5, "low": 8.9, "open": 9.4},
        {"code": "600005", "name": "E", "price": 22.0, "pctChg": 10.0, "volumeLots": 120.0,
         "amount": 2.0e6, "amplitude": 1.0, "turnoverRate": 1.0, "volRatio": 3.0,
         "mainNet": 1.0e6, "prevClose": 20.0, "high": 22.0, "low": 21.9, "open": 22.0},
    ]
    flow_amount5d = {"600005": 2.0e7, "600001": 4.0e7}

    small = flow_screen(flow_rows, amount5d=flow_amount5d, breadth=0.55)
    big_rows = flow_synth_rows(FLOW_SYNTH)
    big = flow_screen(big_rows, amount5d={}, cloud=flow_synth_cloud(FLOW_SYNTH), breadth=0.90)
    big_defense = flow_screen(flow_synth_rows(FLOW_SYNTH), amount5d={}, breadth=0.10)

    # ── flow 三档单点样本：small 只覆盖 local_main/none，local / local_abs / cloud 单独给 ──
    # 小样本走不到横截面档，所以这两例显式改小 minPeerN/minCloudN；参数随用例一起存进夹具，
    # 端上按同一份参数构造 FlowEngine.Params，避免「默认值将来漂了，用例静默改义」。
    tier_rows = [
        {"code": "600020", "name": "L1", "price": 11.0, "pctChg": 3.0, "volumeLots": 5000.0,
         "amount": 5.0e7, "amplitude": 5.0, "turnoverRate": 3.0, "volRatio": 1.6,
         "mainNet": None, "prevClose": 10.68, "high": 11.2, "low": 10.5, "open": 10.7},
        {"code": "600021", "name": "L2", "price": 9.0, "pctChg": -5.0, "volumeLots": 200.0,
         "amount": 5.0e6, "amplitude": 6.0, "turnoverRate": 0.2, "volRatio": 0.8,
         "mainNet": None, "prevClose": 9.47, "high": 9.5, "low": 8.9, "open": 9.4},
        {"code": "600022", "name": "L3", "price": 10.2, "pctChg": 0.5, "volumeLots": 3000.0,
         "amount": 3.0e7, "amplitude": 2.0, "turnoverRate": 2.0, "volRatio": 1.0,
         "mainNet": None, "prevClose": 10.15, "high": 10.3, "low": 10.1, "open": 10.15},
    ]
    tier_rows_with_main = [
        dict(r, mainNet=(1.0e6 if i == 0 else (-1.0e6 if i == 1 else 0.0)))
        for i, r in enumerate(tier_rows)
    ]
    tier_cloud = {r["code"]: {"pctMain": 0.02 + i * 0.01, "m5Ratio": 0.03 - i * 0.02}
                  for i, r in enumerate(tier_rows)}
    flow_local_p = dict(FLOW_P, minPeerN=2)
    flow_cloud_p = dict(FLOW_P, minCloudN=2)
    tier_local = flow_screen(tier_rows, p=flow_local_p, breadth=0.55)
    tier_local_abs = flow_screen(tier_rows[:1], breadth=None)
    tier_cloud_res = flow_screen(tier_rows_with_main, p=flow_cloud_p, cloud=tier_cloud, breadth=0.55)

    # ── tape 样本 ──
    bars6 = [
        {"open": 10.0, "close": 10.2, "high": 10.3, "low": 9.9, "vol": 1000.0},
        {"open": 10.2, "close": 10.1, "high": 10.4, "low": 10.0, "vol": 1100.0},
        {"open": 10.1, "close": 10.5, "high": 10.6, "low": 10.0, "vol": 1500.0},
        {"open": 10.5, "close": 10.4, "high": 10.7, "low": 10.3, "vol": 1200.0},
        {"open": 10.4, "close": 10.8, "high": 10.9, "low": 10.35, "vol": 1800.0},
        {"open": 11.0, "close": 11.2, "high": 11.3, "low": 10.9, "vol": 2000.0},
    ]
    tape_row = {"code": "600010", "name": "T", "price": 11.2, "pctChg": 2.5, "volumeLots": 2000.0,
                "amount": 3.0e7, "amplitude": 3.6, "turnoverRate": 4.0, "volRatio": 1.8,
                "mainNet": 2.0e6, "prevClose": 10.93, "high": 11.3, "low": 10.9, "open": 11.0}
    flat_bar = {"open": 10.0, "close": 11.0, "high": 11.0, "low": 11.0, "vol": 500.0}
    moment = int(datetime(2026, 10, 29, 10, 30, tzinfo=CST).timestamp() * 1000)
    pre_open = int(datetime(2026, 10, 29, 9, 10, tzinfo=CST).timestamp() * 1000)
    auction = int(datetime(2026, 10, 29, 15, 2, tzinfo=CST).timestamp() * 1000)
    low_amt_row = dict(tape_row, amount=5.0e6)
    bars_bear = [
        {"open": 11.5, "close": 11.3, "high": 11.6, "low": 11.2, "vol": 1200.0},
        {"open": 11.3, "close": 11.1, "high": 11.4, "low": 11.0, "vol": 1300.0},
        {"open": 11.1, "close": 10.9, "high": 11.2, "low": 10.8, "vol": 1400.0},
        {"open": 10.9, "close": 10.7, "high": 11.0, "low": 10.6, "vol": 1500.0},
        {"open": 10.7, "close": 10.5, "high": 10.8, "low": 10.45, "vol": 1600.0},
        {"open": 10.1, "close": 10.12, "high": 10.6, "low": 10.05, "vol": 2600.0},
    ]
    tape_bear_row = {"code": "600011", "name": "TB", "price": 10.12, "pctChg": -3.62,
                     "volumeLots": 2600.0, "amount": 3.0e7, "amplitude": 5.24,
                     "turnoverRate": 5.0, "volRatio": 2.5, "mainNet": -3.0e6,
                     "prevClose": 10.5, "high": 10.6, "low": 10.05, "open": 10.1}

    tape_cases = [
        {"id": "tape_normal", "bars": bars6, "row": tape_row, "now_ms": moment},
        {"id": "tape_pre_open", "bars": bars6, "row": tape_row, "now_ms": pre_open},
        {"id": "tape_one_word", "bars": bars6[:-1] + [flat_bar],
         "row": dict(tape_row, price=11.0, high=11.0, low=11.0, open=11.0, amount=3.0e7),
         "now_ms": moment},
        {"id": "tape_illiquid", "bars": bars6, "row": low_amt_row, "now_ms": moment},
        {"id": "tape_auction", "bars": bars6, "row": tape_row, "now_ms": auction},
        # 弱档（bearish）样本：此前 5 个场景全是 bullish 或缺数据，端上弱档渲染一直没被走过。
        # 构造：五连阴 + 向下跳空未回补 + 收在当日与 5 日区间下沿 + 放量下跌 + 长上影。
        {"id": "tape_bearish", "bars": bars_bear, "row": tape_bear_row, "now_ms": moment},
    ]

    for c in tape_cases:
        c["expect"] = tape_compute(c["bars"], c["row"], c["now_ms"])

    # ── rotation 样本 ──
    rot_rows, mom5, amt = rotation_synth_sectors(ROT_SYNTH)
    sectors_ok = {"rows": rot_rows, "coverage": 0.92}
    sectors_low = {"rows": rot_rows, "coverage": 0.60}
    two_sectors = {"rows": rot_rows[:2], "coverage": 0.92}
    rot_cases = [
        {"id": "rotation_attack", "sectors": sectors_ok, "mom5": mom5, "amt": amt,
         "market_e": 45.0, "breadth": 0.62},
        {"id": "rotation_defense", "sectors": sectors_ok, "mom5": mom5, "amt": amt,
         "market_e": -48.0, "breadth": 0.30},
        {"id": "rotation_panic", "sectors": sectors_ok, "mom5": mom5, "amt": amt,
         "market_e": 50.0, "breadth": 0.10},
        {"id": "rotation_low_coverage", "sectors": sectors_low, "mom5": mom5, "amt": amt,
         "market_e": 40.0, "breadth": 0.55},
        {"id": "rotation_too_few", "sectors": two_sectors, "mom5": mom5, "amt": amt,
         "market_e": 40.0, "breadth": 0.55},
    ]
    for c in rot_cases:
        c["expect"] = rotation_compute(c["sectors"], c["mom5"], c["amt"], c["market_e"], c["breadth"])

    vol_cases = [
        {"id": "vol_calm", "returns": [0.002, -0.001, 0.0015, -0.0005, 0.001] * 20},
        {"id": "vol_storm", "returns": [0.03, -0.028, 0.021, -0.035, 0.026] * 20},
        {"id": "vol_insufficient", "returns": [0.01, -0.01, 0.02]},
        {"id": "vol_flat", "returns": [0.0] * 40},
    ]
    for c in vol_cases:
        c["expect"] = vol_target_compute(c["returns"])

    return {
        "spec_version": SPEC_VERSION,
        "engine": "v2",
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "stocks-ai-view/scripts/engines_v2.py",
        "note": "端上 FlowEngine/TapeEngine/RotationEngine 必须逐字段复现；"
                "score 容差 1e-6，其余枚举/档位严格相等",
        "params": {"flow": FLOW_P, "tape": TAPE_P, "rotation": ROT_P},
        "flow": {
            "small": {"rows": flow_rows, "amount5d": flow_amount5d, "breadth": 0.55,
                      "cloud": {}, "turn_scale": 1.0, "expect": small},
            "synth": {"generator": "flow_synth_v1", "params": FLOW_SYNTH,
                      "breadth": 0.90, "cloud": "flow_synth_cloud_v1", "turn_scale": 1.0,
                      "expect": big},
            "synth_bear": {"generator": "flow_synth_v1", "params": FLOW_SYNTH,
                           "breadth": 0.10, "cloud": {}, "turn_scale": 1.0, "expect": big_defense},
            "tiers": {
                "local": {"rows": tier_rows, "params": flow_local_p, "breadth": 0.55,
                          "cloud": {}, "turn_scale": 1.0, "expect": tier_local},
                "local_abs": {"rows": tier_rows[:1], "params": FLOW_P, "breadth": None,
                              "cloud": {}, "turn_scale": 1.0, "expect": tier_local_abs},
                "cloud": {"rows": tier_rows_with_main, "params": flow_cloud_p, "breadth": 0.55,
                          "cloud": tier_cloud, "turn_scale": 1.0, "expect": tier_cloud_res},
            },
        },
        "tape": {"cases": tape_cases},
        "rotation": {"generator": "rotation_synth_v1", "params": ROT_SYNTH, "cases": rot_cases},
        "vol_target": {
            "params": VOL_P,
            "note": "参数由 scripts/calibrate_risk_regime.py 在 5 指数 500 交易日上校准；"
                    "cases 里的 returns 是端上「存档全市场平均涨幅」的同形序列",
            "cases": vol_cases,
        },
    }


def verify(path):
    """重算并比对已提交的黄金向量：返回 (ok, 说明)。

    只比引擎输出（flow/tape/rotation/vol_target）与 params，不比 generated_at——
    时间戳每跑一次都变，拿它当不一致会让这道闸门永远红。数值用 _deep_diff 带容差比。
    """
    if not os.path.exists(path):
        return False, "缺少 %s" % path
    with open(path, encoding="utf-8") as fh:
        old = json.load(fh)
    new = build_golden()
    problems = []
    for section in ("flow", "tape", "rotation", "vol_target"):
        d = deep_diff(old.get(section), new.get(section), section)
        if d:
            problems.append(d)
    d = deep_diff(old.get("params") or {}, new.get("params"), "params")
    if d:
        problems.append(d)
    if problems:
        return False, "漂移段落：%s（参考实现或参数改过，需重生成并同步端上夹具）" % "; ".join(problems[:3])
    return True, "黄金向量与参考实现一致（flow/tape/rotation/vol_target + params）"


def main():
    ap = argparse.ArgumentParser(description="flow/tape/rotation 云端参考实现与黄金向量")
    ap.add_argument("--golden", help="写出 golden_v2.json 的路径")
    ap.add_argument("--verify", help="重算并比对已有黄金向量")
    ap.add_argument("--selftest", action="store_true", help="只跑内置夹具并打印摘要")
    args = ap.parse_args()
    if args.verify:
        ok, msg = verify(args.verify)
        print("[engines_v2] %s %s" % ("OK" if ok else "DRIFT", msg))
        return 0 if ok else 1
    golden = build_golden()
    if args.selftest:
        f = golden["flow"]["small"]["expect"]
        print("flow small:", [(x["code"], None if x["score"] is None else round(x["score"], 3),
                               x["tier"]) for x in f])
        for c in golden["tape"]["cases"]:
            e = c["expect"]
            print("tape", c["id"], None if e["score"] is None else round(e["score"], 3),
                  e["status"], e["usedWeight"])
        for c in golden["rotation"]["cases"]:
            e = c["expect"]
            print("rotation", c["id"], e["marketState"],
                  None if e["marketRot"] is None else round(e["marketRot"], 4),
                  None if e["dispersion"] is None else round(e["dispersion"], 4), e["status"])
        for c in golden["vol_target"]["cases"]:
            e = c["expect"]
            print("vol", c["id"], round(e["scale"], 4), e["status"],
                  None if e["realizedVol"] is None else round(e["realizedVol"], 4))
        return 0
    if args.golden:
        os.makedirs(os.path.dirname(os.path.abspath(args.golden)) or ".", exist_ok=True)
        with open(args.golden, "w", encoding="utf-8") as fh:
            json.dump(golden, fh, ensure_ascii=False)
            fh.write("\n")
        print("golden v2 -> %s (flow cases=%d, tape=%d, rotation=%d, %.1f KB)" % (
            args.golden, 3, len(golden["tape"]["cases"]), len(golden["rotation"]["cases"]),
            os.path.getsize(args.golden) / 1024.0))
        return 0
    print(json.dumps({k: len(json.dumps(golden[k])) for k in ("flow", "tape", "rotation")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
