#!/usr/bin/env python3
"""stop_analysis.py — what a hard stop would have done to today's returns.

The signals group earned a +0.92% median but a -0.44% mean, which is the
signature of a few large losses dragging the average down. A hard stop is the
one lever that improves the mean without improving stock selection.

This module answers, with today's closed data, whether a stop would have
helped or hurt:
  - realised mean/median with and without the stop
  - how many picks the stop would have cut
  - how many of those cut picks later recovered above the stop price

Output: data/stop_analysis.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signal_hub as H

STOPS = [0.02, 0.025, 0.03, 0.04, 0.05]


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def load(name):
    p = os.path.join(H.DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ep = load("entry_plans.json") or {}
    plans = ep.get("plans") or []
    if not plans:
        log("no entry plans")
        return 1

    codes = [str(p["code"]) for p in plans]
    H.quotes(codes)
    log(f"plans: {len(codes)}")

    rows = []
    for p in plans:
        q = H.quotes([p["code"]]).get(H.prefixed(p["code"]))
        if not q or not q["prev_close"]:
            continue
        entry = (p.get("entry_ref") or
                 (sum(p["entry_zone"]) / 2 if p.get("entry_zone") else q["prev_close"]))
        # the day's path relative to the intended entry
        low_pct = (q["low"] - entry) / entry * 100
        high_pct = (q["high"] - entry) / entry * 100
        close_pct = (q["price"] - entry) / entry * 100
        rows.append({
            "code": p["code"], "name": q["name"], "group": p.get("group"),
            "entry_ref": round(entry, 2),
            "low_pct": round(low_pct, 2), "high_pct": round(high_pct, 2),
            "close_pct": round(close_pct, 2),
            "actual_chg_pct": round(q["chg_pct"], 2),
        })

    def stats(vals):
        n = len(vals)
        if not n:
            return None
        s = sorted(vals)
        return {"n": n, "mean": round(sum(vals) / n, 2),
                "median": round(s[n // 2], 2),
                "worst": round(min(vals), 2), "best": round(max(vals), 2)}

    base = [r["close_pct"] for r in rows]
    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "stop_analysis",
        "n_plans": len(rows),
        "no_stop": stats(base),
        "by_stop": {},
        "note": ("close_pct is measured from the intended entry, not from the "
                 "previous close, so it reflects what the plan would have "
                 "delivered. Single-day sample; not predictive."),
    }

    print()
    print(f"plans: {len(rows)}")
    print(f"no stop      mean={out['no_stop']['mean']:+.2f}% "
          f"median={out['no_stop']['median']:+.2f}% "
          f"worst={out['no_stop']['worst']:+.2f}%")
    print()
    print(" stop   n_cut  n_saved  mean    median  worst   recovered_above_stop")
    for s in STOPS:
        vals, cut, saved, recovered = [], 0, 0, 0
        for r in rows:
            if r["low_pct"] <= -s * 100:
                cut += 1
                vals.append(-s * 100)
                # would it have recovered above the stop by the close?
                if r["close_pct"] > -s * 100:
                    recovered += 1
            else:
                vals.append(r["close_pct"])
                if r["close_pct"] > 0:
                    saved += 1
        st = stats(vals)
        out["by_stop"][f"{int(s*1000)/10:.1f}%"] = {
            "n_cut": cut, "n_recovered_above_stop": recovered,
            "mean": st["mean"], "median": st["median"], "worst": st["worst"],
        }
        print(f" {s*100:4.1f}%   {cut:>4}  {saved:>6}  "
              f"{st['mean']:+.2f}%  {st['median']:+.2f}%  {st['worst']:+.2f}%  {recovered:>4}")

    print()
    print("worst 5 without a stop:")
    for r in sorted(rows, key=lambda x: x["close_pct"])[:5]:
        print(f"  {r['code']} {r['name']:10s} entry {r['entry_ref']:>8.2f} "
              f"low {r['low_pct']:+.2f}% close {r['close_pct']:+.2f}%")

    if a.dry_run:
        return 0
    path = os.path.join(H.DATA_DIR, "stop_analysis.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
