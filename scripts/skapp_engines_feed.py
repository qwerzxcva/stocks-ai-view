#!/usr/bin/env python3
"""
skapp_engines_feed.py — 新引擎产出聚合 → data/engines_feed.json

供 skapp_app_data.py 后续接入, 本脚本独立可运行。
读取 data/engines/*.json (10 个新引擎默认落盘), 聚合为单一 JSON:
  {ts, regime, cycle, anomalies, price_levels, orchestrator_top,
   llm_verdict, feedback, degraded[]}
缺文件/解析失败的键置 null 并记入 degraded。

CLI: --out (默认 data/engines_feed.json; '--out -' 输出 stdout)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
ENGINES_DIR = os.path.join(DATA_DIR, "engines")
DEFAULT_OUT = os.path.join(DATA_DIR, "engines_feed.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _file(name):
    return os.path.join(ENGINES_DIR, name)


def build_feed():
    feed = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
            "source_dir": os.path.relpath(ENGINES_DIR, DATA_DIR)}
    degraded = []

    # regime: regime_router 全量 (regime/phase/路由表/门控系数)
    regime = _load(_file("regime_router.json"))
    if regime:
        feed["regime"] = {
            "engine": regime.get("engine"),
            "data_asof": regime.get("data_asof"),
            "regime": regime.get("regime"),
            "regime_score": regime.get("regime_score"),
            "confidence": regime.get("confidence"),
            "phase": regime.get("phase"),
            "phase_degraded": regime.get("phase_degraded"),
            "gates": regime.get("regime_gate") or regime.get("gates"),
            "routing": regime.get("routing") or regime.get("module_routing"),
            "h_group_hard_cap": regime.get("h_group_hard_cap"),
            "degraded": regime.get("degraded"),
        }
    else:
        feed["regime"] = None
        degraded.append("regime_router.json missing/unreadable")

    # cycle: cycle_grid_tester 网格/关口统计
    cyc = _load(_file("cycle_grid_tester.json"))
    if cyc:
        feed["cycle"] = {
            "engine": cyc.get("engine"),
            "data_asof": cyc.get("data_asof"),
            "hypothesis_test": cyc.get("grid_hypothesis_test") or cyc.get("hypothesis_test"),
            "focus_level_stats": cyc.get("focus_level_stats"),
            "swing_amplitude_stats": cyc.get("swing_amplitude_stats"),
            "round_level_support_resistance":
                cyc.get("round_level_support_resistance"),
        }
    else:
        feed["cycle"] = None
        degraded.append("cycle_grid_tester.json missing/unreadable")

    # anomalies: market_anomaly_engine 当日异动列表 + 条件胜率
    ano = _load(_file("market_anomaly_engine.json"))
    if ano:
        feed["anomalies"] = {
            "engine": ano.get("engine"),
            "data_asof": ano.get("data_asof"),
            "asof": ano.get("asof"),
            "events": ano.get("anomalies") or ano.get("events"),
            "event_counts_in_window": ano.get("event_counts_in_window"),
            "conditional_winrate": ano.get("hist_winrate_all_types") or ano.get("conditional_winrate"),
            "degraded": ano.get("degraded"),
        }
    else:
        feed["anomalies"] = None
        degraded.append("market_anomaly_engine.json missing/unreadable")

    # price_levels: price_level_engine_<code>.json 逐个聚合 code → levels
    price_levels = {}
    for fp in sorted(glob.glob(_file("price_level_engine*.json"))):
        d = _load(fp)
        if not d:
            degraded.append("%s unreadable" % os.path.basename(fp))
            continue
        code = d.get("code") or os.path.basename(fp)
        price_levels[code] = {
            "data_asof": d.get("data_asof"),
            "is_index": d.get("is_index"),
            "levels": d.get("levels"),
            "validate": d.get("validate"),
        }
    feed["price_levels"] = price_levels or None
    if not price_levels:
        degraded.append("no price_level_engine*.json found")

    # orchestrator_top: central_orchestrator 终审榜单
    orch = _load(_file("central_orchestrator.json"))
    if orch:
        feed["orchestrator_top"] = {
            "engine": orch.get("engine"),
            "asof": orch.get("asof"),
            "rounds": orch.get("rounds"),
            "top": orch.get("top"),
            "llm": orch.get("llm"),
            "degraded": orch.get("degraded"),
        }
    else:
        feed["orchestrator_top"] = None
        degraded.append("central_orchestrator.json missing/unreadable")

    # llm_verdict: llm_pick_arbiter 裁决结果
    arb = _load(_file("llm_pick_arbiter.json"))
    if arb:
        feed["llm_verdict"] = {
            "engine": arb.get("engine"),
            "model_chain": arb.get("model_chain"),
            "verdicts": arb.get("verdicts") or arb.get("picks") or arb.get("adjustments"),
            "data_asof": arb.get("data_asof"),
            "meta": arb.get("meta"),
        }
    else:
        feed["llm_verdict"] = None
        degraded.append("llm_pick_arbiter.json missing/unreadable")

    # feedback: feedback_updater 权重版本与摘要
    fb = _load(_file("feedback_updater.json"))
    if fb:
        feed["feedback"] = {
            "engine": fb.get("engine"),
            "version": fb.get("version"),
            "updated": fb.get("updated"),
            "converged": fb.get("converged"),
            "weights": fb.get("weights") or fb.get("weights_after"),
            "summary": {k: fb.get(k) for k in
                        ("reason", "settled_records", "lookback",
                         "cold_start", "sources")
                        if fb.get(k) is not None},
            "degraded": fb.get("degraded"),
        }
    else:
        feed["feedback"] = None
        degraded.append("feedback_updater.json missing/unreadable")

    # 附加: 个股层两引擎候选 (tomorrow/morning), 便于软件端直接取用
    for key, fname in (("tomorrow_candidates", "tomorrow_candidate_engine.json"),
                       ("morning_picks", "morning_pick_engine.json")):
        d = _load(_file(fname))
        if d:
            feed[key] = {
                "engine": d.get("engine"),
                "data_asof": d.get("data_asof") or d.get("date"),
                "trade_date": d.get("trade_date"),
                "picks": d.get("candidates") or d.get("picks"),
                "degraded": d.get("degraded"),
            }
        else:
            feed[key] = None
            degraded.append("%s missing/unreadable" % fname)

    feed["degraded"] = degraded or None
    return feed


def main():
    ap = argparse.ArgumentParser(description="新引擎产出聚合 → engines_feed.json (供 skapp_app_data.py 后续接入, 独立可运行)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"输出路径 (默认 {DEFAULT_OUT}; '--out -' 输出 stdout)")
    args = ap.parse_args()

    feed = build_feed()
    text = json.dumps(feed, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("->", args.out, "(non_null_keys=%d degraded=%d)" %
              (len([k for k, v in feed.items() if v is not None]),
               len(feed["degraded"] or [])))
    else:
        print(text)


if __name__ == "__main__":
    main()
