#!/usr/bin/env python3
"""run_chain_local.py — run the engine chain locally, same order as the workflow.

Purpose: verify the chain end to end before relying on Actions. Each engine's
failure is recorded but does not stop the chain, matching the workflow's
`|| echo skipped` behaviour, so this reports the same picture Actions would.

Usage:
    python3 run_chain_local.py            # full chain
    python3 run_chain_local.py --only 5   # one tier
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, "data")

TIERS = {
    0: [
        ["valuation_engine.py", "--inject"],
        ["phase_runner.py", "--days", "90", "--out", "data/market_phase.json"],
    ],
    1: [
        ["engine_v5.py", "--out", "data/engine_v5.json", "--limit", "500"],
        ["ai_pick_v4.py", "--out", "data/ai_pick_v4.json"],
        ["rps_rotation.py", "--top", "30", "--out", "data/rps_rotation.json"],
    ],
    2: [
        ["multidim_engine.py", "--out", "data/multidim.json", "--limit", "300"],
        ["decision_engine.py", "--out", "data/decision.json"],
        ["ban_prob.py", "--out", "data/ban_prob.json"],
        ["ban_pick.py", "--out", "data/ban_pick.json"],
        ["patterns.py", "--out", "data/patterns_smoke.json"],
        ["strategy_card.py"],
        ["engine_fusion.py"],
    ],
    3: [
        ["kline_sim.py", "--source", "predictions", "--out", "data/kline_sim.json",
         "--days", "1,3,5,10", "--max-signals", "3000"],
        ["reflect_engine.py", "--pred-dir", "data/predictions",
         "--horizons", "1,3,5,10", "--out", "data/reflect_latest.json"],
        ["winrate_matrix.py", "--out", "data/winrate_matrix.json"],
        ["walkforward.py", "--train-days", "120", "--test-days", "20",
         "--step-days", "20", "--horizon", "5", "--out", "data/walkforward.json"],
    ],
    4: [
        ["factor_lib.py", "--out", "data/factor_smoke.json"],
        ["flow_divergence.py", "--out", "data/flow_divergence.json"],
        ["sample_balancer.py", "--in", "data/ban_prob.json",
         "--out", "data/ban_prob_balanced.json", "--write-back"],
        ["loss_attribution.py"],
        ["pick_tracker.py", "stats"],
    ],
    5: [
        ["weight_calibrator_v2.py"],
        ["edge_miner.py"],
        ["self_reflect.py", "--out", "data/self_reflect.json"],
    ],
    6: [
        ["numeric_validator.py", "--out", "data/validation_report.json"],
        ["scenario_engine.py"],
        ["signal_auditor.py"],
        ["risk_engine.py"],
    ],
    7: [
        ["portfolio_optimizer.py"],
        ["anomaly_detector.py"],
    ],
    8: [
        ["morning_review.py"],
        ["review_yesterday.py"],
        ["entry_planner.py"],
        ["afternoon_picks.py"],
        ["stop_analysis.py"],
        ["build_today_picks.py", "--force"],
        ["close_review.py"],
        ["archive_review.py"],
    ],
}


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, action="append")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tiers = sorted(a.only or TIERS)
    env = dict(os.environ)
    env.setdefault("STOCKS_KLINE_BULK",
                   "/root/workspace/stocks-data-kline/data/bulk")

    ok = fail = 0
    t_all = time.time()
    for t in tiers:
        if t not in TIERS:
            log(f"tier {t} not defined")
            continue
        log(f"=== tier {t} ===")
        for cmd in TIERS[t]:
            script = os.path.join(HERE, cmd[0])
            if not os.path.exists(script):
                log(f"  MISSING {cmd[0]}")
                fail += 1
                continue
            if a.dry_run:
                log(f"  would run {' '.join(cmd)}")
                continue
            t0 = time.time()
            r = subprocess.run([sys.executable, script] + cmd[1:],
                               cwd=REPO, capture_output=True, text=True,
                               env=env, timeout=600)
            sec = time.time() - t0
            if r.returncode == 0:
                ok += 1
                log(f"  OK   {cmd[0]:28s} {sec:5.1f}s")
            else:
                fail += 1
                tail = (r.stderr or r.stdout).strip().split("\n")[-1][:100]
                log(f"  FAIL {cmd[0]:28s} {sec:5.1f}s  {tail}")

    if a.dry_run:
        return 0
    log(f"=== done: {ok} ok, {fail} fail, {time.time()-t_all:.0f}s total ===")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
