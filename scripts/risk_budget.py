#!/usr/bin/env python3
"""risk_budget.py — 风险预算与仓位优化（v1）。

## 为什么不是 decision_engine 里那段仓位树

仓位此前写死在 `decision_engine.py` 的市场环境分支里（bear 0.3 / range 0.6 / bull 0.9），
`decision.py` 又有一份凯利实现，两处互不引用。本引擎把「该拿多少仓」独立出来，输入是
**已经算好的市场状态**（engine_fusion.E + market_phase.phase），输出是一个可核对的预算数：
波动率目标 + 半凯利 + 三道硬闸，任何一道不满足就降档，且把降档原因写进 gates。

## 口径

    vol_scale  = clamp(vol_target_annual / realized_vol_annual, 0.2, 1.0)
    kelly_half = 0.5 * max(0, W - (1-W)/R)          # W/R 来自 pick_log；缺失用 0.45/2.0
    base       = min(max_pos, min(kelly_half, regime_cap) * vol_scale)
    gates      : fusion 分歧(E 与 state 不符) → ×0.5；phase ∈ {ice,ebb} → ×0.5；
                 drawdown_guard 触发 → 0
    budget     = base 经过 gates 后的最终上限（占总资金比例）

realized_vol 用全市场指数日收益的 60 日标准差年化（无指数数据则用成分股均值，仍缺失则退回 0.25）。
"""
import argparse
import glob
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
SPEC = os.path.join(DATA, "engine_spec.json")
VERSION = "risk_budget_v1"
DEFAULT_PARAMS = {"vol_target_annual": 0.18, "max_pos": 0.6, "half_kelly": 0.5,
                  "drawdown_guard": -0.15, "default_vol": 0.25,
                  "regime_cap": {"bull": 0.9, "range": 0.6, "bear": 0.3},
                  "phase_damp": ["ice", "ebb"]}
PHASE_CAP = {"rally": 0.9, "ignite": 0.8, "repair": 0.6, "climax": 0.5, "ebb": 0.3, "ice": 0.2}


def load_params():
    try:
        with open(SPEC, encoding="utf-8") as f:
            spec = json.load(f)
        for p in spec.get("products") or []:
            if p.get("id") == "risk_budget":
                return {**DEFAULT_PARAMS, **(p.get("params") or {})}, spec.get("spec_version")
    except Exception:
        pass
    return DEFAULT_PARAMS, None


def jload(name):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def realized_vol(params):
    """用 bulk 里前 200 只主板股的日收益均值序列估年化波动。"""
    bulk = os.path.normpath(os.path.join(HERE, "..", "..", "stocks-data-kline", "data", "bulk"))
    files = sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv")))[:3]
    series = {}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 3:
                    continue
                try:
                    series.setdefault(p[0], []).append(float(p[2]))
                except ValueError:
                    continue
                if len(series) > 200:
                    break
        if len(series) > 200:
            break
    rets_per_day = []
    for closes in series.values():
        tail = closes[-61:]
        if len(tail) < 40:
            continue
        rets_per_day.append([tail[i] / tail[i - 1] - 1 for i in range(1, len(tail)) if tail[i - 1] > 0])
    if not rets_per_day:
        return params["default_vol"], "default"
    n = min(len(r) for r in rets_per_day)
    idx = [statistics.fmean(r[-n:][i] for r in rets_per_day if len(r) >= n) for i in range(n)]
    daily = statistics.pstdev(idx) if len(idx) > 1 else 0.0
    return (daily * math.sqrt(252) or params["default_vol"]), "market_index_proxy"


def kelly_from_log(params):
    """半凯利。账本不可用时返回 (默认, 'default')。"""
    try:
        import ledger_store  # 可选依赖
        con = ledger_store.connect("pick_log")
        if con is None or ledger_store.has_rows(con) == 0:
            raise RuntimeError("empty")
        row = con.execute("""SELECT COUNT(*),
            AVG(CASE WHEN verdict LIKE '止盈%' THEN 1.0 ELSE 0.0 END),
            AVG(CASE WHEN verdict LIKE '止盈%' THEN ABS(ret_open_close) END),
            AVG(CASE WHEN verdict LIKE '止损%' THEN ABS(ret_open_close) END)
            FROM pick_log WHERE settled=true""").fetchone()
        con.close()
        n, w, aw, al = row
        if not n or not w:
            raise RuntimeError("no settled")
        R = (aw or 0.02) / (al or 0.02)
        return max(0.0, params["half_kelly"] * (w - (1 - w) / R)), "pick_log(n=%d)" % n
    except Exception:
        W, R = 0.45, 2.0
        return max(0.0, params["half_kelly"] * (W - (1 - W) / R)), "default(W=0.45,R=2.0)"


def main():
    ap = argparse.ArgumentParser(description="风险预算与仓位优化")
    ap.add_argument("--out", default=os.path.join(DATA, "risk_budget.json"))
    args = ap.parse_args()

    params, spec_ver = load_params()
    gates = {}
    fusion = jload("engine_fusion.json")
    phase_doc = jload("market_phase.json") or {}
    decision = jload("decision.json") or {}

    E = (fusion or {}).get("E", 0.0)
    state = (fusion or {}).get("state", "中性观望")
    agree = (fusion or {}).get("agreement", 0.0)
    # 与 engine_fusion 同一判据：只有 phase_runner 写的规范格式（有 dates/source）才认 phase。
    # market_phase.py 的 demo 输出没有 phase 键且内容是合成梯队，拿它调仓等于用假数据定价。
    if phase_doc.get("phase"):
        phase = phase_doc["phase"]
    elif phase_doc.get("dates") and phase_doc.get("phases"):
        phase = phase_doc["phases"][-1]
    else:
        phase = None
    regime = decision.get("market_regime") or ("bull" if E > 30 else "bear" if E < -30 else "range")

    vol, vol_src = realized_vol(params)
    vol_scale = max(0.2, min(1.0, params["vol_target_annual"] / vol if vol else 1.0))
    kelly, kelly_src = kelly_from_log(params)

    regime_cap = params["regime_cap"].get(regime, 0.6)
    base = min(params["max_pos"], min(kelly, regime_cap) * vol_scale)
    gates["regime_cap"] = regime_cap
    gates["vol_scale"] = round(vol_scale, 3)

    if agree and agree < 0.6:
        base *= 0.5
        gates["fusion_disagreement"] = "agreement<0.6 ×0.5"
    if phase is None:
        gates["phase_unavailable"] = "market_phase.json 非 phase_runner 格式，phase 闸门跳过"
        base = min(base, 0.5)
    if phase in params["phase_damp"]:
        base *= 0.5
        gates["phase_damp"] = "%s ×0.5" % phase
    if phase in PHASE_CAP:
        base = min(base, PHASE_CAP[phase])
        gates["phase_cap"] = PHASE_CAP[phase]
    if (fusion or {}).get("missing_inputs"):
        gates["missing_inputs"] = (fusion or {})["missing_inputs"][:3]
        base = min(base, 0.5)
    dd = (decision.get("evidence") or {}).get("drawdown") if isinstance(decision.get("evidence"), dict) else None
    if dd is not None and dd <= params["drawdown_guard"]:
        base = 0.0
        gates["drawdown_guard"] = "DD=%.3f<=%.3f → 空仓" % (dd, params["drawdown_guard"])

    budget = round(max(0.0, min(params["max_pos"], base)), 3)
    out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
           "engine": VERSION, "spec_version": spec_ver,
           "E": E, "fusion_state": state, "regime": regime, "phase": phase,
           "budget": budget,
           "kelly": {"half": round(kelly, 3), "source": kelly_src},
           "vol_target": {"annual_target": params["vol_target_annual"],
                          "realized": round(vol, 4), "scale": round(vol_scale, 3),
                          "source": vol_src},
           "gates": gates,
           "meta": {"formula": "budget=min(max_pos, min(kelly,regime_cap)*vol_scale) 经 gates 降档",
                    "cap_source": "engine_spec.PRODUCTS[risk_budget].params",
                    "note": "只在市场状态可信时给仓位；missing_inputs 存在时上限压到 0.5"}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[risk_budget] E={E} state={state} phase={phase} → budget={budget:.1%} "
          f"(kelly={kelly:.2f} vol_scale={vol_scale:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
