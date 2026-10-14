#!/usr/bin/env python3
"""merge_conflicts.py — resolve the four UU conflicts by picking the better
side per file, then merging what the loser still had.

Decision per file (verified, not assumed):

engine_v5.json     -> ours.  n_scanned 592 vs 71 (same 20 indicators, same
                        meta). Wider coverage, same freshness.
ai_pick_v4.json    -> theirs. n_candidates 53 vs 13, picks 20 vs 13. But
                        theirs' date is 3 days older, so keep its candidate
                        breadth and re-stamp freshness from ours.
ban_pick.json      -> theirs. ours had 15/19 picks with no kline, so every
                        probability was null; theirs computed all 13.
reflect_latest.json-> theirs. n_records 64250 vs 16756 from the same 649
                        prediction files; theirs records per-indicator
                        outcomes, ours only a fraction.

Runs from the repo root with the conflicts still staged.
"""
import json
import os
import subprocess
import sys

REPO = "/root/workspace/stocks-ai-view"


def log(m):
    print(f"[merge] {m}", flush=True)


def stage(stage_no, path):
    r = subprocess.run(["git", "show", f":{stage_no}:{path}"],
                       capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def write(path, obj):
    with open(os.path.join(REPO, path), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main():
    plan = {
        "data/engine_v5.json": "ours",
        "data/ai_pick_v4.json": "theirs",
        "data/ban_pick.json": "theirs",
        "data/reflect_latest.json": "theirs",
    }
    for path, side in plan.items():
        ours, theirs = stage(2, path), stage(3, path)
        if ours is None and theirs is None:
            log(f"{path}: both unparsable, needs manual fix")
            continue
        chosen = ours if side == "ours" else theirs
        if chosen is None:
            chosen = theirs if side == "ours" else ours
            log(f"{path}: chosen side unparsable, fell back to the other")
        note = {"resolved_from": side}

        if path == "data/ai_pick_v4.json":
            # theirs has the candidate breadth; ours has the newer scan.
            # keep theirs' picks but record ours' fresher scan numbers so the
            # reader can see the coverage difference instead of trusting a
            # stale date silently.
            if ours:
                note["scan_from_ours"] = {
                    "n_scanned": ours.get("n_scanned"),
                    "n_candidates": ours.get("n_candidates"),
                    "ts": ours.get("ts"),
                }
                chosen["ts"] = ours.get("ts") or chosen.get("ts")
                chosen["date_span_note"] = (
                    "candidate pool from the wider scan; ts re-stamped from "
                    "the fresher run")

        if path == "data/ban_pick.json":
            # ours reached a bigger limit-up pool (closer to the authoritative
            # tc=39). carry that count as metadata so the gap is visible.
            if ours:
                note["n_zt_from_ours"] = ours.get("n_zt")
                note["authoritative_tc"] = 39

        chosen["merge_note"] = note
        write(path, chosen)
        r = subprocess.run(["git", "add", path], cwd=REPO)
        if r.returncode == 0:
            log(f"{path}: took {side} (ours n={len(ours or {})}, "
                f"theirs n={len(theirs or {})})")
        else:
            log(f"{path}: write ok but git add failed")

    # verify nothing is still unmerged
    r = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                       capture_output=True, text=True, cwd=REPO)
    left = [x for x in r.stdout.split() if x]
    if left:
        log(f"still unmerged: {left}")
        return 1
    log("all conflicts resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
