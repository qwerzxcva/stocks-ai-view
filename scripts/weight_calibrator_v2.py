#!/usr/bin/env python3
"""weight_calibrator_v2.py — derive engine weights from realised outcomes.

The original calibrator wrote a DEFAULT_WEIGHTS constant into engine_v5.py that
nothing reads, and its --apply touched a protected file. This version does not
modify any engine; it writes a weights file that engine_v5 can optionally load
through the documented --weights path, and it reports what it changed.

What the data says (64250 records, 17 indicators):
  every bull signal wins 42-45%, while several bear signals win 47-60%.
  So equal weighting is wrong twice over: the indicators are near-identical in
  bull power, and the bear side carries more information than the bull side.

Method:
  1. read reflect_latest.json for each indicator's bull/bear win rate and n
  2. weight_bull = max(0, win_rate - 0.5) scaled, only for indicators whose
     sample clears a floor (n >= 5000) so noise is not promoted
  3. weight_bear = same on the bear side
  4. normalise each side to sum 1
  5. write data/engine_weights.json with the provenance of every number

engine_v5.py can consume it via --weights data/engine_weights.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MIN_N = 5000
DISCLAIMER = ("Weights derived from historical records. Past performance does "
              "not imply future results.")


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    p = os.path.join(DATA_DIR, "reflect_latest.json")
    if not os.path.exists(p):
        log("reflect_latest.json missing; run reflect_engine first")
        return 1
    rl = json.load(open(p, encoding="utf-8"))
    ind = ((rl.get("analysis") or {}).get("indicators") or {})
    if not ind:
        log("no indicator stats")
        return 1

    bull, bear, skipped = [], [], []
    for name, v in ind.items():
        bn = v.get("bull_n") or 0
        ben = v.get("bear_n") or 0
        bw = v.get("bull_win_rate")
        bew = v.get("bear_win_rate")
        if bn < a.min_n or bw is None:
            skipped.append({"indicator": name, "reason": f"bull n={bn} < {a.min_n}"})
            continue
        bull.append({"indicator": name, "n": bn, "win_rate": bw,
                     "edge": round(bw - 50, 2)})
        if ben >= a.min_n and bew is not None:
            bear.append({"indicator": name, "n": ben, "win_rate": bew,
                         "edge": round(bew - 50, 2)})

    if not bull:
        log("no indicator cleared the sample floor")
        return 1

    def normalise(rows, side):
        # only a positive edge earns weight; a negative edge contributes 0
        for r in rows:
            r["raw"] = max(0.0, r["edge"])
        total = sum(r["raw"] for r in rows) or 1.0
        for r in rows:
            r["weight"] = round(r["raw"] / total, 4)
        rows.sort(key=lambda r: -r["weight"])
        log(f"{side}: {len(rows)} indicators, "
            f"total edge {round(sum(r['raw'] for r in rows), 2)}")
        return rows

    bull = normalise(bull, "bull")
    bear = normalise(bear, "bear")

    print()
    print("=== bull weights (top 8) ===")
    for r in bull[:8]:
        print(f"  {r['indicator']:22s} n={r['n']:>6} win={r['win_rate']}% "
              f"edge={r['edge']:+.2f} w={r['weight']:.4f}")
    print()
    print("=== bear weights (top 8) ===")
    for r in bear[:8]:
        print(f"  {r['indicator']:22s} n={r['n']:>6} win={r['win_rate']}% "
              f"edge={r['edge']:+.2f} w={r['weight']:.4f}")
    if skipped:
        print()
        print(f"skipped (n < {a.min_n}): "
              + ", ".join(s["indicator"] for s in skipped))

    # does the bear side actually carry more information?
    bull_edge = sum(r["raw"] for r in bull) / len(bull)
    bear_edge = sum(r["raw"] for r in bear) / len(bear) if bear else 0
    print()
    print(f"mean bull edge {bull_edge:.2f} vs mean bear edge {bear_edge:.2f}"
          f"  -> bear side {'stronger' if bear_edge > bull_edge else 'weaker'}")

    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "weight_calibrator_v2",
        "source": "reflect_latest.json",
        "n_records": rl.get("n_records"),
        "min_n": a.min_n,
        "bull": bull, "bear": bear, "skipped": skipped,
        "mean_bull_edge": round(bull_edge, 2),
        "mean_bear_edge": round(bear_edge, 2),
        "usage": ("engine_v5.py --weights data/engine_weights.json; the engine "
                  "still runs with equal weights when the file is absent"),
        "disclaimer": DISCLAIMER,
    }
    if a.dry_run:
        return 0
    path = os.path.join(DATA_DIR, "engine_weights.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
