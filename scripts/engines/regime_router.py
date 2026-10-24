#!/usr/bin/env python3
"""
regime_router.py — 中枢唯一 regime 判定入口 (新增引擎, 不改任何既有文件)

用途:
  1. 从指数日线独立判定 market regime: bull / bear / range (带置信度与证据);
  2. 复用 market_phase.py 六阶段规则 (ice/ignite/rally/climax/ebb/repair) 给出周期位置,
     梯队数据优先取 stocks-data-live/data/emotion/daily.jsonl, 不足时从全 A 日线分片自算涨停梯队
     (与 market_phase.py:117 from_daily_klines 同口径, 但日线无炸板数据, seal 恒 None → 降级标注);
  3. 输出 8 大模块组 (A-H) 的路由表: 并行/串联 + 权重档 (重/中/轻), H 组硬上限 0.02 写死;
  4. regime_gate 系数 bull=1.0 / range=0.8 / bear=0.5 为默认初始值, CLI --gate 可覆盖。

数据来源 (全部本地真实文件, 只用 T 日及之前数据, 无前视):
  - 指数日线: stocks-data-kline/data/index/history/sh000001.txt (| 分隔: code|date|open|close|high|low|vol)
  - 全 A 日线分片: stocks-data-kline/data/bulk/day_qfq_*.csv (同格式)
  - 情绪快照: stocks-data-live/data/emotion/daily.jsonl (zt/dt/炸板率, 仅少量行, 缺则降级)

规则来源 (未 import 以免路径副作用, 逐行复刻):
  - 六阶段规则: scripts/market_phase.py:21 (PHASES), :24-36 (阈值), :48-91 (classify), :117 (梯队自算)
  - bull/bear/range 直觉参考: scripts/decision_engine.py:58 market_regime (其依赖 market_now 实时快照,
    本引擎改为纯历史指数日线可复算版本, 规则独立实现)

已知降级/限制:
  - 本地指数数据 asof 2026-10-09, 分片 asof 2026-10-16, 滞后于当前日期 (2026-10-23), 输出带 data_asof。
  - 无分钟线/封单数据 → market_phase 的 seal(封板率) 恒 None, 对应 EBB 严格分支自动跳过。
  - emotion/daily.jsonl 仅 6 行, 梯队不足 PROMO_MIN_POOL 时 promo=None, 阶段判定退化为索引趋势代理。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KLINE = os.path.expanduser(os.environ.get("KLINE_DIR", "~/workspace/stocks-data-kline/data"))
LIVE = os.path.expanduser(os.environ.get("LIVE_DIR", "~/workspace/stocks-data-live/data"))
INDEX_TXT = os.path.join(KLINE, "index", "history", "sh000001.txt")
EMOTION_JSONL = os.path.join(LIVE, "emotion", "daily.jsonl")
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "regime_router.json"))

PHASES = {"ice": "冰点", "ignite": "启动", "rally": "主升",
          "climax": "高潮", "ebb": "退潮", "repair": "修复"}

# ── 六阶段阈值, 逐行复刻自 scripts/market_phase.py:24-36 (TSP 原版, A股全历史分位数) ──
CLIMAX_GE2, CLIMAX_FB = 50, 220
RALLY_H, RALLY_G2, RALLY_PR = 7, 15, 0.23
RALLY_PR_ALT, RALLY_G2_ALT, RALLY_H_ALT = 0.30, 12, 5
EBB_PR, EBB_PR_STRICT, EBB_SEAL = 0.15, 0.13, 0.57
EBB_RECENT_G2, EBB_RECENT_H = 12, 6
IGNITE_G2_DELTA, IGNITE_G2, IGNITE_PR = 3, 8, 0.20
IGNITE_H_DELTA, IGNITE_H, IGNITE_PR_SOFT = 1, 5, 0.19
ICE_H, ICE_G2, ICE_FB = 4, 6, 24
PROMO_MIN_POOL = 10
EMA_A = 1.0 / 3.0
CONFIRM = 2

H_GROUP_HARD_CAP = 0.02  # H 组(另类) 权重硬上限, 写死

# ── 路由表: regime -> 模块组 -> (模式, 权重档) ──
# 模块组: A 大盘 / B 个股 / C 异动 / D 消息 / E 数据 / F 资金 / G 行业 / H 另类
ROUTING = {
    "bull": {
        "A": {"mode": "parallel", "tier": "中", "note": "大盘趋势确认, 与 B 并联"},
        "B": {"mode": "parallel", "tier": "重", "note": "个股引擎主输出"},
        "C": {"mode": "parallel", "tier": "中", "note": "异动追共振"},
        "D": {"mode": "serial_after_B", "tier": "轻", "note": "消息作二次过滤"},
        "E": {"mode": "serial_before_all", "tier": "中", "note": "数据质检前置门禁"},
        "F": {"mode": "parallel", "tier": "中", "note": "资金流确认进攻"},
        "G": {"mode": "parallel", "tier": "重", "note": "行业主线共振加分"},
        "H": {"mode": "serial_after_B", "tier": "轻", "note": "另类信号仅参考"},
    },
    "range": {
        "A": {"mode": "parallel", "tier": "重", "note": "震荡市大盘网格权重上调"},
        "B": {"mode": "parallel", "tier": "中", "note": "个股降档, 高抛低吸"},
        "C": {"mode": "parallel", "tier": "重", "note": "异动是震荡市主要alpha"},
        "D": {"mode": "parallel", "tier": "中", "note": "消息驱动占比上升"},
        "E": {"mode": "serial_before_all", "tier": "中", "note": "数据质检前置门禁"},
        "F": {"mode": "parallel", "tier": "中", "note": "资金流做T参考"},
        "G": {"mode": "parallel", "tier": "中", "note": "轮动加快, 行业降档"},
        "H": {"mode": "serial_after_C", "tier": "轻", "note": "另类信号仅参考"},
    },
    "bear": {
        "A": {"mode": "serial_before_all", "tier": "重", "note": "大盘风控一票否决前置"},
        "B": {"mode": "serial_after_A", "tier": "轻", "note": "个股引擎降级为观察名单"},
        "C": {"mode": "parallel", "tier": "中", "note": "只报风险型异动(跌停潮/龙头大跌)"},
        "D": {"mode": "parallel", "tier": "中", "note": "利空消息权重上调"},
        "E": {"mode": "serial_before_all", "tier": "重", "note": "弱市数据质量更需严防"},
        "F": {"mode": "parallel", "tier": "重", "note": "资金流是 bear 撤离核心信号"},
        "G": {"mode": "parallel", "tier": "轻", "note": "行业仅保留防御性观察"},
        "H": {"mode": "serial_after_A", "tier": "轻", "note": "另类信号仅参考"},
    },
}

DEFAULT_GATES = {"bull": 1.0, "range": 0.8, "bear": 0.5}


# ═══ 数据读取 ═══
def read_kline_file(path):
    """code|date|open|close|high|low|vol -> [{date,open,close,high,low,vol}] 升序"""
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("|")
            if len(p) < 7:
                continue
            try:
                rows.append({"code": p[0], "date": p[1], "open": float(p[2]),
                             "close": float(p[3]), "high": float(p[4]),
                             "low": float(p[5]), "vol": float(p[6])})
            except ValueError:
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def sma(vals, i, n):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def ema(vals, alpha=EMA_A):
    """复刻 market_phase.py:39-55"""
    out, cur = [], None
    first = None
    for v in vals:
        if v is None:
            out.append(cur if cur is not None else None)
            continue
        cur = v if cur is None else cur + alpha * (v - cur)
        out.append(cur)
        if first is None:
            first = len(out) - 1
    if first:
        for i in range(first):
            out[i] = out[first]
    return out


def classify_phase(daily):
    """复刻 market_phase.py:58-104 classify: daily 升序 [{date,first_board,ge2,height,promo,seal}]"""
    H = ema([d.get("height") for d in daily])
    FB = ema([d.get("first_board") for d in daily])
    G2 = ema([d.get("ge2") for d in daily])
    PR = ema([d.get("promo") for d in daily])
    SR = ema([d.get("seal") for d in daily])

    def raw(i):
        h, fb, g2, pr, sr = H[i], FB[i], G2[i], PR[i], SR[i]
        g2p = G2[max(0, i - 5)]
        hp = H[max(0, i - 5)]
        if g2 is not None and (g2 >= CLIMAX_GE2 or (fb is not None and fb >= CLIMAX_FB)):
            return "climax"
        if h is not None and h >= RALLY_H and g2 is not None and g2 >= RALLY_G2 \
                and pr is not None and pr >= RALLY_PR:
            return "rally"
        if pr is not None and pr >= RALLY_PR_ALT and g2 is not None and g2 >= RALLY_G2_ALT \
                and h is not None and h >= RALLY_H_ALT:
            return "rally"
        if h is not None and h <= ICE_H and g2 is not None and g2 <= ICE_G2 \
                and fb is not None and fb <= ICE_FB:
            return "ice"
        from_high = (g2p is not None and g2p >= EBB_RECENT_G2) or (hp is not None and hp >= EBB_RECENT_H)
        if from_high and pr is not None and pr <= EBB_PR and g2 is not None and g2 < g2p:
            return "ebb"
        if pr is not None and pr <= EBB_PR_STRICT and sr is not None and sr <= EBB_SEAL:
            return "ebb"
        if g2p is not None and g2 is not None and g2 - g2p >= IGNITE_G2_DELTA \
                and g2 >= IGNITE_G2 and pr is not None and pr >= IGNITE_PR:
            return "ignite"
        if hp is not None and h is not None and h - hp >= IGNITE_H_DELTA \
                and h >= IGNITE_H and pr is not None and pr >= IGNITE_PR_SOFT:
            return "ignite"
        return "repair"

    labels, cur, pend, run = [], None, None, 0
    for i in range(len(daily)):
        r = raw(i)
        if cur is None:
            cur = r
            labels.append(r)
            continue
        if r == cur:
            labels.append(cur)
            pend, run = None, 0
            continue
        if r == pend:
            run += 1
        else:
            pend, run = r, 1
        if run >= CONFIRM:
            cur = r
            labels.append(r)
            pend, run = None, 0
        else:
            labels.append(cur)
    return labels


# ═══ 梯队数据: emotion 快照优先, 不足则分片自算 (market_phase.py:117 同口径) ═══
def ladder_from_emotion(asof):
    out = []
    if not os.path.exists(EMOTION_JSONL):
        return out
    with open(EMOTION_JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("date", "") > asof:
                continue  # 无前视
            zt = d.get("zt") or 0
            ladder = d.get("ladder", "")
            ge2 = 0
            try:
                parts = [int(x) for x in ladder.split(":") if x]
                ge2 = sum(parts[1:]) if len(parts) > 1 else 0
            except ValueError:
                pass
            fb = max(0, zt - ge2)
            seal = None
            if d.get("zb_rate") is not None:
                seal = round(1 - d["zb_rate"] / 100.0, 4)
            out.append({"date": d["date"], "first_board": fb, "ge2": ge2,
                        "height": d.get("max_lb") or 0, "promo": None, "seal": seal})
    out.sort(key=lambda r: r["date"])
    return out


def ladder_from_shards(asof, tail_days):
    """从全 A 分片自算最近 tail_days 个交易日的涨停梯队 (10% 主板口径, 降级近似)。"""
    hist = {}  # code -> {date: close}
    dates = set()
    for fp in glob.glob(os.path.join(KLINE, "bulk", "day_qfq_*.csv")):
        with open(fp, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 7 or p[1] > asof:
                    continue
                hist.setdefault(p[0], {})[p[1]] = float(p[3])
                dates.add(p[1])
    dates = sorted(dates)[-tail_days:]
    date_set = set(dates)
    out, prev_zt = [], {}
    for dt in dates:
        fb = ge2 = height = 0
        today_zt = {}
        for sym, series in hist.items():
            c = series.get(dt)
            if c is None:
                continue
            prior = [d for d in sorted(series) if d < dt]
            if not prior:
                continue
            pc = series[prior[-1]]
            if pc and c >= round(pc * 1.1, 2) - 0.001:
                n = prev_zt.get(sym, 0) + 1
                today_zt[sym] = n
                height = max(height, n)
                if n == 1:
                    fb += 1
                else:
                    ge2 += 1
        promo_ok = sum(1 for s, n in today_zt.items() if prev_zt.get(s, 0) >= 1)
        pool = sum(1 for n in prev_zt.values() if n >= 1)
        out.append({"date": dt, "first_board": fb, "ge2": ge2, "height": height,
                    "promo": (promo_ok / pool) if pool >= PROMO_MIN_POOL else None,
                    "seal": None})
        prev_zt = today_zt
    return out


# ═══ regime 判定 (纯指数日线, 可复算) ═══
def judge_regime(bars, i):
    """bars[:i+1] 判定, 只用 T 日及之前。返回 (regime, score, evidence)"""
    closes = [b["close"] for b in bars]
    ma20 = sma(closes, i, 20)
    ma60 = sma(closes, i, 60)
    ma250 = sma(closes, i, 250)
    c = closes[i]

    def slope(n, lag=5):
        a = sma(closes, i, n)
        b = sma(closes, i - lag, n) if i - lag >= n - 1 else None
        if a is None or b is None or b == 0:
            return None
        return (a - b) / b * 100  # 5日斜率 %

    s20, s60, s250 = slope(20), slope(60), slope(250)

    win = closes[max(0, i - 250):i + 1]
    hi = max(win)
    dd = (c - hi) / hi * 100  # 距 250 日高点回撤 %, <=0

    rets = [(closes[j] / closes[j - 1] - 1) for j in range(max(1, i - 59), i + 1)]
    vol60 = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5 * 100 \
        if len(rets) > 1 else None
    # 波动率分位: 用全历史 60 日滚动波动率的分位
    volq = None
    if vol60 is not None and len(closes) > 120:
        hist_vols = []
        for j in range(60, len(closes)):
            rs = [(closes[k] / closes[k - 1] - 1) for k in range(j - 59, j + 1)]
            m = sum(rs) / len(rs)
            hist_vols.append((sum((r - m) ** 2 for r in rs) / (len(rs) - 1)) ** 0.5 * 100)
        hist_vols.sort()
        volq = sum(1 for v in hist_vols if v <= vol60) / len(hist_vols)

    score = 0.0
    ev = {}
    if ma20 is not None:
        pts = 1.0 if c > ma20 else -1.0
        score += pts
        ev["close_vs_ma20"] = {"value": round((c / ma20 - 1) * 100, 2), "points": pts}
    if ma60 is not None:
        pts = 1.0 if c > ma60 else -1.0
        score += pts
        ev["close_vs_ma60"] = {"value": round((c / ma60 - 1) * 100, 2), "points": pts}
    if ma250 is not None:
        pts = 1.0 if c > ma250 else -1.0
        score += pts
        ev["close_vs_ma250"] = {"value": round((c / ma250 - 1) * 100, 2), "points": pts}
    if s20 is not None:
        pts = 0.5 if s20 > 0 else -0.5
        score += pts
        ev["ma20_slope_5d_pct"] = {"value": round(s20, 3), "points": pts}
    if s60 is not None:
        pts = 0.5 if s60 > 0 else -0.5
        score += pts
        ev["ma60_slope_5d_pct"] = {"value": round(s60, 3), "points": pts}
    if s250 is not None:
        pts = 0.5 if s250 > 0 else -0.5
        score += pts
        ev["ma250_slope_5d_pct"] = {"value": round(s250, 3), "points": pts}
    pts = 1.0 if dd >= -8 else (0.0 if dd >= -15 else -1.0)
    score += pts
    ev["drawdown_from_250d_high_pct"] = {"value": round(dd, 2), "points": pts}

    MAX_SCORE = 5.5
    if score >= 2.0:
        regime = "bull"
    elif score <= -2.0:
        regime = "bear"
    else:
        regime = "range"
    confidence = round(min(1.0, abs(score) / MAX_SCORE + 0.3), 3)
    if volq is not None:
        ev["vol60_percentile"] = {"value": round(volq, 3),
                                  "note": "60日已实现波动率的全历史分位"}
    return regime, score, confidence, ev


def main():
    ap = argparse.ArgumentParser(description="中枢 regime 路由引擎 (bull/bear/range + 六阶段 + 模块路由表)")
    ap.add_argument("--asof", default=None, help="判定截止日 YYYY-MM-DD, 默认数据最新日")
    ap.add_argument("--index", default=INDEX_TXT, help="指数日线文件路径")
    ap.add_argument("--gate", default=None,
                    help="覆盖 regime_gate, 如 'bull=1.0,range=0.8,bear=0.5'")
    ap.add_argument("--phase-lookback", type=int, default=120,
                    help="分片自算梯队的回看交易日数 (emotion 快照不足时启用, 默认 120; 0=跳过)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 文件, 默认 DEFAULT_OUT ('--out -' 输出 stdout)")
    ap.add_argument("--explain", action="store_true", help="输出判定依据明细")
    args = ap.parse_args()

    gates = dict(DEFAULT_GATES)
    if args.gate:
        for kv in args.gate.split(","):
            k, _, v = kv.partition("=")
            if k.strip() in gates:
                gates[k.strip()] = float(v)

    bars = read_kline_file(args.index)
    if not bars:
        print(json.dumps({"error": f"no data in {args.index}"}), file=sys.stderr)
        sys.exit(2)
    asof = args.asof or bars[-1]["date"]
    bars = [b for b in bars if b["date"] <= asof]  # 无前视
    if not bars:
        print(json.dumps({"error": f"no bar <= {asof}"}), file=sys.stderr)
        sys.exit(2)
    i = len(bars) - 1
    data_asof_index = bars[-1]["date"]

    regime, score, conf, ev = judge_regime(bars, i)

    # ── 周期位置: emotion 快照 + 分片自算梯队 ──
    ladder = ladder_from_emotion(asof)
    ladder_src = "emotion/daily.jsonl"
    degraded = []
    need = 40  # EMA 预热所需最少天数
    if len(ladder) < need and args.phase_lookback > 0:
        calc = ladder_from_shards(asof, args.phase_lookback)
        known = {d["date"] for d in ladder}
        merged = calc + [d for d in ladder if d["date"] not in known]
        merged.sort(key=lambda r: r["date"])
        # emotion 行回填 promo/seal (快照无 promo, seal 更可信)
        emap = {d["date"]: d for d in ladder}
        for d in merged:
            if d["date"] in emap:
                if emap[d["date"]]["seal"] is not None:
                    d["seal"] = emap[d["date"]]["seal"]
                d["emotion_zt"] = True
        ladder = merged
        ladder_src = "bulk shards self-computed limit-up ladder + emotion overlay"
        degraded.append("seal 无炸板历史, 日线口径恒 None (emotion 快照覆盖期除外)")
        degraded.append("涨停口径统一按 10% 主板近似, ST/科创/创业 20cm 未区分")
    labels = classify_phase(ladder) if ladder else []
    phase = labels[-1] if labels else None
    phase_since = None
    if labels:
        j = len(labels) - 1
        while j > 0 and labels[j - 1] == phase:
            j -= 1
        phase_since = ladder[j]["date"]

    routing = {}
    for grp, cfg in ROUTING[regime].items():
        tier_w = {"重": 1.0, "中": 0.6, "轻": 0.3}[cfg["tier"]]
        w = tier_w * gates[regime]
        if grp == "H":
            w = min(w, H_GROUP_HARD_CAP)
        routing[grp] = {**cfg, "weight": round(w, 4)}

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "engine": "regime_router v1 (新增引擎, 不改既有文件)",
        "asof": asof,
        "data_asof": {"index": data_asof_index,
                      "note": "本地数据滞后于当前日期 2026-10-23, 为已知限制"},
        "regime": regime,
        "regime_score": round(score, 2),
        "confidence": conf,
        "phase": phase,
        "phase_cn": PHASES.get(phase),
        "phase_since": phase_since,
        "phase_source": ladder_src,
        "regime_gate": gates,
        "gate_applied": gates[regime],
        "routing": routing,
        "h_group_hard_cap": H_GROUP_HARD_CAP,
        "degraded": degraded or None,
    }
    if args.explain:
        result["evidence"] = ev
        result["ladder_tail"] = ladder[-10:]
        result["rule_sources"] = {
            "phase_rules": "scripts/market_phase.py:21-104 (逐行复刻, 未 import)",
            "regime_reference": "scripts/decision_engine.py:58 (口径改为纯历史指数日线)",
        }

    txt = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print("->", args.out, file=sys.stderr)
    else:
        print(txt)


if __name__ == "__main__":
    main()
