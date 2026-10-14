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
import shutil
import subprocess
import sys
import time
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


def api(method, path, body=None, retries=3):
    """Contents API call with rate-limit backoff.

    A burst of GET+PUT pairs trips GitHub's secondary rate limit, and the
    failure surfaces as a confusing "sha wasn't supplied" on the PUT because
    the GET returned an error body carrying no sha. Detect the limit
    explicitly and back off rather than reporting that misleading error."""
    url = f"https://api.github.com/repos/{MARKET_REPO}/contents/{path}"
    data = json.dumps(body).encode() if body is not None else None
    out = {}
    for attempt in range(retries):
        cmd = ["curl", "-s", "X", method, url,
               "-H", f"Authorization: token {token()}",
               "-H", "Accept: application/vnd.github+json"]
        cmd[2] = "-X"
        if data:
            cmd += ["-H", "Content-Type: application/json", "-d", data.decode()]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        try:
            out = json.loads(r.stdout) if r.stdout.strip() else {}
        except Exception:
            out = {}
        msg = str(out.get("message") or "").lower()
        if "rate limit" in msg or "abuse" in msg:
            wait = 30 * (attempt + 1)
            log(f"  rate limited on {method} {path}; waiting {wait}s")
            time.sleep(wait)
            continue
        return out
    return out


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
    """Delegate to stocks-db/scripts/sync_repos.py.

    Publishing through the contents API looks cheaper (no clone) but a burst
    of GET+PUT pairs trips GitHub's secondary rate limit, and the resulting
    error is a misleading "sha wasn't supplied" on the PUT. A git push has no
    such limit and already handles the shard routing, so use it.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # the warehouse builder lives in the sibling repo; run it directly so the
    # review files it publishes are the ones just produced here
    here = os.path.dirname(os.path.abspath(__file__))
    sync = os.path.join(os.path.dirname(here), "..", "stocks-db", "scripts",
                        "sync_repos.py")
    sync = os.path.abspath(sync)
    if not os.path.exists(sync):
        log(f"{sync} not found; cannot publish")
        return 1
    # mirror the review artefacts into the warehouse's data/market so the
    # warehouse push picks them up
    wh = os.path.join(os.path.dirname(sync), "..", "data", "market")
    wh = os.path.abspath(wh)
    copied = 0
    for name in FILES:
        src = os.path.join(DATA_DIR, name)
        if os.path.exists(src) and os.path.getsize(src) > 0:
            shutil.copy2(src, os.path.join(wh, name))
            copied += 1
    log(f"staged {copied} review files into the warehouse")
    if copied == 0:
        log("nothing to publish")
        return 1
    if a.dry_run:
        log(f"would push the warehouse ({copied} files staged)")
        return 0
    env = dict(os.environ)
    r = subprocess.run([sys.executable, sync, "--push"],
                       capture_output=True, text=True, env=env, timeout=1800)
    tail = (r.stdout or "").strip().split("\n")[-8:]
    for line in tail:
        log(f"  {line}")
    if r.returncode != 0:
        log(f"warehouse push failed (exit {r.returncode})")
        return 1
    log("published via git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
