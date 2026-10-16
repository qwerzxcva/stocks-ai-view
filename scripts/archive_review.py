#!/usr/bin/env python3
"""archive_review.py — append today's close review to the history archive.

Every close review is compressed to one row and appended to
data/review_history.jsonl in the cloud market repo, so the dashboard can show
a table of past reviews without re-reading every day's full payload.

The row keeps only what a history table needs: date, the headline numbers, the
per-group breakdown, the score-effectiveness check and the market regime.
Full detail stays in close_review.json for that single day.

Usage:
    python3 archive_review.py            # append today
    python3 archive_review.py --rebuild  # rebuild from all close_review files
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# token comes from the environment only; a hardcoded fallback is both a
# security problem and gets the push rejected by secret scanning
GH_TOKEN = os.environ.get("GH_TOKEN") or ""
MARKET_REPO = "jiegengxiangjianxue1/db-market"
ARCHIVE_NAME = "review_history.jsonl"


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def valid_premarket(p, review_date):
    if not isinstance(p, dict) or p.get("session") != "premarket" or p.get("date") != review_date:
        return False
    try:
        stamp = datetime.fromisoformat(str(p.get("ts", "")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=CST)
        stamp = stamp.astimezone(CST)
        return stamp.date().isoformat() == review_date and (stamp.hour, stamp.minute) < (9, 15)
    except (TypeError, ValueError):
        return False


def row_from_review(r, premarket=None):
    """Compress one close_review into a history-table row."""
    ms = r.get("morning", {}).get("summary") or {}
    groups = r.get("morning", {}).get("by_group") or {}
    # the honest number: exclude the limit-up group, which cannot be bought
    rows = [x for x in (r.get("morning", {}).get("rows") or [])
            if x.get("group") != "limit_up"]
    n_ex = len(rows)
    up_ex = sum(1 for x in rows if (x.get("chg_pct") or 0) > 0)
    sc = r.get("score_effectiveness") or {}
    tp = (r.get("tradable_plans") or {})
    row = {
        "date": r.get("date"),
        "generated_at": r.get("generated_at"),
        "market_phase": (r.get("market") or {}).get("phase"),
        "n_picks": ms.get("n"),
        # headline: including limit-up (what the raw numbers say)
        "hit_rate_all_pct": ms.get("hit_rate_pct"),
        "avg_ret_all_pct": ms.get("avg_ret_pct"),
        # honest: excluding limit-up (what was actually buyable)
        "n_buyable": n_ex,
        "hit_rate_buyable_pct": round(up_ex / n_ex * 100, 1) if n_ex else None,
        "avg_ret_buyable_pct": round(
            sum(x.get("chg_pct") or 0 for x in rows) / n_ex, 2) if n_ex else None,
        "by_group": {g: {"n": s.get("n"), "hit_rate_pct": s.get("hit_rate_pct"),
                         "avg_ret_pct": s.get("avg_ret_pct")}
                     for g, s in groups.items()},
        "score_top_avg": sc.get("top_half_avg"),
        "score_bottom_avg": sc.get("bottom_half_avg"),
        "score_edge_pct": (round(sc["top_half_avg"] - sc["bottom_half_avg"], 2)
                           if sc.get("top_half_avg") is not None
                           and sc.get("bottom_half_avg") is not None else None),
        "stop_hit": tp.get("n_stop_hit"),
        "target_hit": tp.get("n_target_hit"),
        "n_rejected": (r.get("rejected_review") or {}).get("n"),
    }
    for field in ("mainlines", "themes"):
        if r.get(field):
            row[field] = r[field]
    if valid_premarket(premarket, r.get("date")):
        row["premarket"] = {
            "date": premarket["date"],
            "ts": premarket["ts"],
            "session": "premarket",
            "gate": premarket.get("gate"),
            "tiers": premarket.get("tiers"),
            "outlook": premarket.get("outlook"),
        }
    return row


def api(method, path, body=None):
    """GitHub contents API. Cloning the market repo costs minutes because it
    carries every dataset; a single-file read/write is far cheaper."""
    url = f"https://api.github.com/repos/{MARKET_REPO}/contents/{path}"
    data = json.dumps(body).encode() if body is not None else None
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: token {GH_TOKEN}",
           "-H", "Accept: application/vnd.github+json"]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", data.decode()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return {}


def fetch_archive():
    """Current archive rows, or [] when the file does not exist yet."""
    r = api("GET", f"market/{ARCHIVE_NAME}")
    if not r.get("content"):
        return []
    import base64
    text = base64.b64decode(r["content"]).decode("utf-8", "ignore")
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def push_archive(rows, msg):
    import base64
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows)
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    cur = api("GET", f"market/{ARCHIVE_NAME}")
    body = {"message": msg, "content": b64, "branch": "main"}
    if cur.get("sha"):
        body["sha"] = cur["sha"]
    r = api("PUT", f"market/{ARCHIVE_NAME}", body)
    if r.get("content"):
        log(f"archive pushed ({len(rows)} rows)")
        return True
    log(f"push failed: {str(r)[:200]}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild from every close_review_*.json present")
    ap.add_argument("--local-only", action="store_true",
                    help="write the local copy without pushing")
    a = ap.parse_args()
    if not GH_TOKEN and not a.local_only:
        log("GH_TOKEN is not set; writing the local copy only")
        a.local_only = True

    rows = []
    if a.rebuild:
        for f in sorted(glob.glob(os.path.join(DATA_DIR, "close_review_*.json"))):
            try:
                rows.append(row_from_review(json.load(open(f, encoding="utf-8"))))
            except Exception:
                pass
        log(f"rebuilt from {len(rows)} files")
    else:
        r = load("close_review.json")
        if not r:
            log("close_review.json missing; run close_review.py first")
            return 1
        rows = [row_from_review(r, load("premarket_picks.json"))]

    stage = tempfile.mkdtemp(prefix="revarch_")
    try:
        existing = fetch_archive()
        by_date = {x.get("date"): x for x in existing}
        for x in rows:
            if x.get("date"):
                previous = by_date.get(x["date"]) or {}
                for field in ("premarket", "mainlines", "themes"):
                    if field not in previous:
                        continue
                    if field == "premarket":
                        if not valid_premarket(x.get(field), x["date"]):
                            x.pop(field, None)
                            if valid_premarket(previous[field], x["date"]):
                                x[field] = previous[field]
                    elif field not in x:
                        x[field] = previous[field]
                by_date[x["date"]] = x
        merged = [by_date[d] for d in sorted(by_date) if d]
        text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in merged)
        # keep a local copy so the dashboard can render without an extra fetch
        local = os.path.join(DATA_DIR, ARCHIVE_NAME)
        with open(local, "w", encoding="utf-8") as f:
            f.write(text)
        log(f"archive rows: {len(merged)} (was {len(existing)})")

        if not a.local_only:
            d = rows[-1].get("date") if rows else "n/a"
            push_archive(merged, f"review: {d} close review archived")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
