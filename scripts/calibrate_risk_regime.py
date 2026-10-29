#!/usr/bin/env python3
"""calibrate_risk_regime.py — riskBudget / regime 的回测校准（真实指数数据，非经验值）。

## 要回答的问题

`risk_budget.py` 里的 `vol_target_annual=0.18 / max_pos=0.6 / regime_cap(bull .9/range .6/bear .3)`
是**经验值**，从没跑过回测。上一轮因此拒绝接线：未经校准的仓位公式替换实盘仓位，
比不接更糟。本脚本就是那道前置关卡——

1. 用 `data/index_kline.json` 里 5 个指数的真实日线（500 根，2024-10 ~ 2026-10）做回测：
   - B&H 满仓 vs 固定 0.6 vs 波动率目标 vs 波动率目标 × 状态上限
   - 交易成本按 5bp × |Δ仓位| 计（不计成本的回测会把换手当免费午餐）
2. 对 (vol_target, max_pos, 快慢均线) 做网格搜索，**按时间切 3 折**：
   前两折选参、最后一折只做样本外评估，避免用全样本挑参数再自夸
3. 输出 `data/risk_regime_params.json`：校准后的参数 + 逐折指标 + 接线结论

## 判据（先写死，不事后找理由）

接线条件（全部满足才 wire）：

- 样本外 Sharpe ≥ B&H 的 Sharpe，且
- 样本外最大回撤 ≤ B&H 最大回撤 × 0.8，且
- 样本外 3 折里至少 2 折 Sharpe 不低于固定 0.6 基线

任一条不满足 → `decision=not_wired`，并把差多少写清楚。数据不足（主指数 < 250 根）直接拒绝输出参数。

    python3 scripts/calibrate_risk_regime.py            # 校准并写参数
    python3 scripts/calibrate_risk_regime.py --report   # 只打印指标
"""
import argparse
import json

from numcmp import deep_diff
import math
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
VERSION = "risk_regime_calibration_v1"

PRIMARY = "sh000300"
COST_BPS = 5.0
MIN_BARS = 250
VOL_WINDOW = 60
FOLDS = 3


def jload(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def index_series(idx_doc):
    out = {}
    for code, bars in (idx_doc.get("indices") or {}).items():
        rows = bars if isinstance(bars, list) else bars.get("bars") or []
        rows = sorted(rows, key=lambda b: b["date"])
        if len(rows) < MIN_BARS:
            continue
        out[code] = rows
    return out


def daily_returns(closes):
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]


def rolling_vol(rets, win=VOL_WINDOW):
    """t 日收盘后可得的历史波动（用 t-win..t-1 的收益，避免未来函数）。"""
    out = [None] * (len(rets) + 1)
    for t in range(len(rets) + 1):
        if t < win:
            continue
        seg = rets[t - win:t]
        out[t] = statistics.pstdev(seg) * math.sqrt(252) if len(seg) > 1 else None
    return out


def ma(closes, n):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= n:
            out[i] = statistics.fmean(closes[i + 1 - n:i + 1])
    return out


def metrics(pos, rets, cost_bps=COST_BPS):
    """pos[t] 是 t 日**收盘后**定的仓位，从 t+1 起生效。"""
    eq = 1.0
    peak = 1.0
    dd = 0.0
    strat = []
    turn = 0.0
    for t in range(1, len(rets) + 1):
        p = pos[t - 1]
        r = p * rets[t - 1] - cost_bps / 10000.0 * abs(pos[t - 1] - pos[t - 2]) if t >= 2 else p * rets[t - 1]
        strat.append(r)
        eq *= (1 + r)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
        if t >= 2:
            turn += abs(pos[t - 1] - pos[t - 2])
    if not strat:
        return {}
    n = len(strat)
    mu = statistics.fmean(strat)
    sd = statistics.pstdev(strat) if n > 1 else 0.0
    cagr = eq ** (252.0 / n) - 1 if eq > 0 else -1.0
    return {
        "n_days": n,
        "total_return": round(eq - 1, 4),
        "cagr": round(cagr, 4),
        "vol_annual": round(sd * math.sqrt(252), 4),
        "sharpe": round(mu / sd * math.sqrt(252), 3) if sd > 0 else 0.0,
        "max_drawdown": round(dd, 4),
        "calmar": round(cagr / abs(dd), 2) if dd < 0 else None,
        "avg_exposure": round(statistics.fmean([p for p in pos[:-1]]), 3),
        "turnover_annual": round(turn / n * 252, 2),
    }


def regime_of(closes, fast, slow):
    """状态判定：快均线在上且慢均线上行 → bull；反之 bear；其余 range。"""
    f = ma(closes, fast)
    s = ma(closes, slow)
    out = []
    for i in range(len(closes)):
        if f[i] is None or s[i] is None or i < slow + 5:
            out.append("range")
            continue
        slope = s[i] - s[i - 5]
        if f[i] > s[i] and slope > 0:
            out.append("bull")
        elif f[i] < s[i] and slope < 0:
            out.append("bear")
        else:
            out.append("range")
    return out


def apply_dwell(regimes, dwell):
    """最短停留：状态切换须连续 dwell 日确认，防止状态抖动把成本吃光。"""
    if dwell <= 1:
        return regimes[:]
    out = regimes[:]
    cur = regimes[0]
    pending = None
    streak = 0
    for i in range(1, len(regimes)):
        want = regimes[i]
        if want == cur:
            pending, streak = None, 0
            out[i] = cur
            continue
        if want == pending:
            streak += 1
        else:
            pending, streak = want, 1
        if streak >= dwell:
            cur = want
            pending, streak = None, 0
        out[i] = cur
    return out


def positions(closes, rets, vol_target, max_pos, fast, slow, dwell, regime_cap):
    vol = rolling_vol(rets)
    reg = apply_dwell(regime_of(closes, fast, slow), dwell)
    pos = []
    for t in range(len(rets) + 1):
        v = vol[t] or 0.25
        vol_scale = max(0.2, min(1.0, vol_target / v if v > 0 else 1.0))
        cap = (regime_cap or {}).get(reg[t], 0.6) if regime_cap else 1.0
        # t 日的状态/波动只能用于 t 日收盘后定仓，收益从 t+1 起计
        pos.append(min(max_pos, cap * vol_scale))
    return pos, reg


def splits(n, folds):
    size = n // folds
    return [(i * size, n if i == folds - 1 else (i + 1) * size) for i in range(folds)]


def slice_pos(pos, rets, a, b):
    return pos[a:b], rets[a:b]


def evaluate(idx_series, params, regime_cap):
    """逐指数回测，返回每个指数的指标。"""
    out = {}
    for code, rows in idx_series.items():
        closes = [r["close"] for r in rows]
        rets = daily_returns(closes)
        pos, _ = positions(closes, rets, params["vol_target_annual"], params["max_pos"],
                            params["ma_fast"], params["ma_slow"], params["dwell"], regime_cap)
        out[code] = metrics(pos, rets)
    return out


def baselines(idx_series):
    out = {}
    for code, rows in idx_series.items():
        closes = [r["close"] for r in rows]
        rets = daily_returns(closes)
        bh = metrics([1.0] * len(closes), rets, cost_bps=0.0)
        fixed = metrics([0.6] * len(closes), rets, cost_bps=0.0)
        out[code] = {"buy_hold": bh, "fixed_60": fixed}
    return out


def grid():
    for vol_target in (0.12, 0.15, 0.18, 0.22, 0.25):
        for max_pos in (0.5, 0.6, 0.8):
            for fast, slow in ((20, 60), (30, 90)):
                for dwell in (1, 3, 5):
                    yield {"vol_target_annual": vol_target, "max_pos": max_pos,
                           "ma_fast": fast, "ma_slow": slow, "dwell": dwell}


def calib(idx_series, regime_cap):
    """前 FOLDS-1 折选参，最后一折样本外。"""
    primary = idx_series[PRIMARY]
    closes = [r["close"] for r in primary]
    rets = daily_returns(closes)
    n = len(rets) + 1
    bounds = splits(n, FOLDS)
    train_end = bounds[-1][0]

    best = None
    scored = []
    for p in grid():
        pos, _ = positions(closes, rets, p["vol_target_annual"], p["max_pos"],
                            p["ma_fast"], p["ma_slow"], p["dwell"], regime_cap)
        m = metrics(pos[:train_end], rets[:train_end - 1])
        if not m:
            continue
        scored.append((m["sharpe"], p, m))
        if best is None or m["sharpe"] > best[0]:
            best = (m["sharpe"], p, m)
    scored.sort(key=lambda x: -x[0])
    chosen = best[1]

    pos_all, reg_all = positions(closes, rets, chosen["vol_target_annual"], chosen["max_pos"],
                                 chosen["ma_fast"], chosen["ma_slow"], chosen["dwell"], regime_cap)
    oos_pos = pos_all[train_end - 1:]
    oos_rets = rets[train_end - 1:]
    oos = metrics(oos_pos, oos_rets)
    bh_oos = metrics([1.0] * len(oos_pos), oos_rets, cost_bps=0.0)
    fixed_oos = metrics([0.6] * len(oos_pos), oos_rets, cost_bps=0.0)

    per_fold = []
    for a, b in bounds:
        seg_pos = pos_all[a:b]
        seg_rets = rets[a:b - 1]
        if not seg_rets:
            continue
        per_fold.append({
            "days": b - a,
            "strategy": metrics(seg_pos, seg_rets),
            "fixed_60": metrics([0.6] * len(seg_pos), seg_rets, cost_bps=0.0),
            "buy_hold": metrics([1.0] * len(seg_pos), seg_rets, cost_bps=0.0),
        })

    state_counts = {s: reg_all.count(s) for s in ("bull", "range", "bear")}
    return {
        "chosen": chosen,
        "train_metrics": best[2],
        "oos_metrics": oos,
        "oos_buy_hold": bh_oos,
        "oos_fixed_60": fixed_oos,
        "per_fold": per_fold,
        "state_days": state_counts,
        "grid_top5": [{"sharpe": round(s, 3), **{k: v for k, v in p.items()}} for s, p, _ in scored[:5]],
        "train_end_index": train_end,
    }


def decide(cal):
    oos, bh, fixed = cal["oos_metrics"], cal["oos_buy_hold"], cal["oos_fixed_60"]
    folds = cal["per_fold"]
    c1 = oos["sharpe"] >= bh["sharpe"]
    c2 = abs(oos["max_drawdown"]) <= abs(bh["max_drawdown"]) * 0.8
    c3_fold = sum(1 for f in folds if f["strategy"]["sharpe"] >= f["fixed_60"]["sharpe"])
    c3 = c3_fold >= 2
    return {
        "wire": bool(c1 and c2 and c3),
        "criteria": {
            "oos_sharpe_ge_bh": {"pass": c1, "oos": oos["sharpe"], "bh": bh["sharpe"]},
            "oos_maxdd_le_80pct_bh": {"pass": c2,
                                      "oos": oos["max_drawdown"], "bh": bh["max_drawdown"],
                                      "threshold": round(abs(bh["max_drawdown"]) * 0.8, 4)},
            "folds_beating_fixed60": {"pass": c3, "count": c3_fold, "need": 2, "total": len(folds),
                                      "per_fold": [{"strategy": f["strategy"]["sharpe"],
                                                    "fixed_60": f["fixed_60"]["sharpe"]} for f in folds]},
        },
    }


def main():
    ap = argparse.ArgumentParser(description="riskBudget / regime 回测校准")
    ap.add_argument("--out", default=os.path.join(DATA, "risk_regime_params.json"))
    ap.add_argument("--report", action="store_true", help="只打印，不写文件")
    ap.add_argument("--verify", help="重算并比对已提交的校准参数")
    args = ap.parse_args()

    idx_doc = jload("index_kline.json")
    if not idx_doc:
        print("[calib] 缺 data/index_kline.json，拒绝输出参数", file=sys.stderr)
        return 2
    series = index_series(idx_doc)
    if PRIMARY not in series:
        print("[calib] 主指数 %s 数据不足 %d 根，拒绝输出参数" % (PRIMARY, MIN_BARS), file=sys.stderr)
        return 2

    current_cap = {"bull": 0.9, "range": 0.6, "bear": 0.3}
    cal = calib(series, current_cap)
    base = baselines(series)
    decision = decide(cal)

    span = {"start": series[PRIMARY][0]["date"], "end": series[PRIMARY][-1]["date"],
            "bars": len(series[PRIMARY]), "indices": sorted(series.keys())}
    doc = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": VERSION,
        "spec_version": "3.0.0",
        "data": {**span, "primary": PRIMARY, "cost_bps": COST_BPS,
                 "vol_window": VOL_WINDOW, "folds": FOLDS},
        "regime_cap": current_cap,
        "params": cal["chosen"],
        "calibration": {
            "train": cal["train_metrics"],
            "oos": cal["oos_metrics"],
            "oos_buy_hold": cal["oos_buy_hold"],
            "oos_fixed_60": cal["oos_fixed_60"],
            "per_fold": cal["per_fold"],
            "state_days": cal["state_days"],
            "grid_top5": cal["grid_top5"],
        },
        "baselines_all_indices": base,
        "strategy_all_indices": evaluate(series, cal["chosen"], current_cap),
        "decision": decision,
        "note": "pos 由 t 日收盘后的状态/波动决定，收益从 t+1 计；成本 5bp×|Δpos|；"
                "参数量由前 2 折选、最后 1 折样本外评估",
    }
    if args.verify:
        if not os.path.exists(args.verify):
            print("[calib] 缺少 %s" % args.verify)
            return 1
        with open(args.verify, encoding="utf-8") as fh:
            old = json.load(fh)
        drift = []
        d = deep_diff(old.get("params"), cal["chosen"], "params")
        if d:
            drift.append("params(%s)" % d)
        if (old.get("decision") or {}).get("wire") != decision["wire"]:
            drift.append("decision.wire(已提交 %s / 重算 %s)"
                         % ((old.get("decision") or {}).get("wire"), decision["wire"]))
        d = deep_diff(old.get("regime_cap"), current_cap, "regime_cap")
        if d:
            drift.append(d)
        if drift:
            print("[calib] DRIFT %s —— 参数须重新校准后才能进规格" % "; ".join(drift))
            return 1
        print("[calib] OK 校准参数与重算一致（wire=%s）" % decision["wire"])
        return 0

    if not args.report:
        os.makedirs(DATA, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)

    print("[calib] 数据 %s ~ %s，%d 根，%d 指数" % (span["start"], span["end"], span["bars"], len(span["indices"])))
    print("[calib] 选中参数 %s" % json.dumps(cal["chosen"], ensure_ascii=False))
    print("[calib] 训练 %s" % json.dumps(cal["train_metrics"], ensure_ascii=False))
    print("[calib] 样本外 %s" % json.dumps(cal["oos_metrics"], ensure_ascii=False))
    print("[calib] B&H 样本外 %s" % json.dumps(cal["oos_buy_hold"], ensure_ascii=False))
    print("[calib] 固定0.6 样本外 %s" % json.dumps(cal["oos_fixed_60"], ensure_ascii=False))
    print("[calib] 分折 %s" % json.dumps([{k: (f[k] if k == "days" else f[k]["sharpe"]) for k in ("days", "strategy", "fixed_60", "buy_hold")} for f in cal["per_fold"]], ensure_ascii=False))
    print("[calib] 状态天数 %s" % json.dumps(cal["state_days"], ensure_ascii=False))
    print("[calib] 接线判定 wire=%s %s" % (decision["wire"], json.dumps(decision["criteria"], ensure_ascii=False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
