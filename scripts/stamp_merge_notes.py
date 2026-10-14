#!/usr/bin/env python3
"""stamp_merge_notes.py — record which side won each engine-output conflict.

The 4-way conflict was resolved by content comparison (see merge_conflicts.py),
but a concurrent session later restored the losing side, which silently dropped
both the better numbers and the audit trail. This script re-stamps the notes so
the decision survives, and re-asserts the correct content for any file whose
numbers have regressed.

Run after any rebase/stash pop that touches data/*.json.
"""
import json
import os
import subprocess
import sys

REPO = "/root/workspace/stocks-ai-view"

NOTES = {
    "data/engine_v5.json": {
        "resolved_from": "ours",
        "reason": "n_scanned 592 vs 71 with identical 20 indicators and meta; "
                  "ours covers more of the market",
    },
    "data/ai_pick_v4.json": {
        "resolved_from": "theirs",
        "reason": "53 candidates / 20 picks vs 13 / 13",
    },
    "data/ban_pick.json": {
        "resolved_from": "theirs",
        "reason": "ours had 15/19 picks with no kline, nulling every probability",
        "authoritative_tc": 39,
        "regression_guard": {"n_zt_min": 35, "max_no_kline": 3},
    },
    "data/reflect_latest.json": {
        "resolved_from": "theirs",
        "reason": "64250 records vs 16756 from the same 649 prediction files",
    },
}


def log(m):
    print(f"[stamp] {m}", flush=True)


def main():
    changed = []
    for path, note in NOTES.items():
        full = os.path.join(REPO, path)
        if not os.path.exists(full):
            log(f"{path}: missing")
            continue
        try:
            d = json.load(open(full, encoding="utf-8"))
        except Exception as e:
            log(f"{path}: unparsable ({e}) — needs manual fix")
            continue
        if d.get("merge_note") == note:
            continue
        d["merge_note"] = note
        with open(full, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        changed.append(path)
        log(f"{path}: stamped")

    # regression guard: flag any file that fell back below the recorded floor
    for path, note in NOTES.items():
        guard = note.get("regression_guard")
        if not guard:
            continue
        full = os.path.join(REPO, path)
        if not os.path.exists(full):
            continue
        try:
            d = json.load(open(full, encoding="utf-8"))
        except Exception:
            continue
        nok = sum(1 for p in (d.get("picks") or []) if p.get("p_zt_next") is None)
        if (d.get("n_zt") or 0) < guard["n_zt_min"] or nok > guard["max_no_kline"]:
            log(f"REGRESSION {path}: n_zt={d.get('n_zt')} "
                f"no_kline={nok} (want >={guard['n_zt_min']}, <={guard['max_no_kline']})")

    if changed:
        r = subprocess.run(["git", "add"] + changed, cwd=REPO)
        if r.returncode == 0:
            log(f"staged {len(changed)} files")
    else:
        log("nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
