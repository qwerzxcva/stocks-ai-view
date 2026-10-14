#!/usr/bin/env python3
"""sync_reviews.py — publish the review artefacts to the data warehouse.

The dashboard reads review data from jiegengxiangjianxue1/db-market/market/,
but the CI runner produces it inside ai-view/data/. This copies the review
files across so the site sees them.

Only the review/session files are handled; the market-wide and per-stock data
is published by stocks-db/scripts/sync_repos.py.

Usage:
    python3 sync_reviews.py            # push the files that exist
    python3 sync_reviews.py --dry-run  # list what would be pushed
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MARKET_REPO = "jiegengxiangjianxue1/db-market"
DEST_DIR = "market"

# review + session artefacts only. universe.json belongs to the market repo's
# master/ tree (published by stocks-db/scripts/sync_repos.py); duplicating it
# under market/ would add 1.4MB of redundancy.
FILES = [
    "close_review.json",
    "stop_analysis.json",
    "engine_weights.json",
    "review_history.jsonl",
    "morning_review.json",
    "afternoon_picks.json",
    "entry_plans.json",
    "review_today.json",
    "today_picks.json",
    "overseas_impact.json",
    "overseas_live.json",
]


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def token():
    t = os.environ.get("GH_TOKEN") or ""
    if not t:
        log("GH_TOKEN is not set; cannot publish")
    return t


def api(method, path, body=None):
    url = f"https://api.github.com/repos/{MARKET_REPO}/contents/{path}"
    data = json.dumps(body).encode() if body is not None else None
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: token {token()}",
           "-H", "Accept: application/vnd.github+json"]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", data.decode()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return {}


def publish(name, local_path, dry=False):
    with open(local_path, "rb") as f:
        body = f.read()
    b64 = base64.b64encode(body).decode()
    dest = f"{DEST_DIR}/{name}"
    if dry:
        log(f"  would publish {name} ({len(body)} bytes) -> {dest}")
        return True
    cur = api("GET", dest)
    # skip when the remote already has identical content
    if cur.get("content"):
        try:
            if base64.b64decode(cur["content"]) == body:
                return True
        except Exception:
            pass
    payload = {"message": f"review: {name} {datetime.now(CST):%Y%m%d_%H%M}",
               "content": b64, "branch": "main"}
    if cur.get("sha"):
        payload["sha"] = cur["sha"]
    r = api("PUT", dest, payload)
    if r.get("content"):
        log(f"  published {name} ({len(body)} bytes)")
        return True
    log(f"  FAILED {name}: {str(r)[:180]}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not token():
        return 1

    ok = miss = 0
    for name in FILES:
        p = os.path.join(DATA_DIR, name)
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            log(f"  skip {name} (missing or empty)")
            miss += 1
            continue
        if publish(name, p, a.dry_run):
            ok += 1
    log(f"published {ok}, skipped {miss}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
