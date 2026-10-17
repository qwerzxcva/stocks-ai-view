#!/usr/bin/env python3
"""Write one versioned review snapshot per run.

archive_review keeps one compressed row per day. This keeps the full close
review payload, because a later rerun must not erase the earlier version.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", default=os.path.join(DATA, "close_review.json"))
    parser.add_argument("--index", default=os.path.join(DATA, "review_versions.json"))
    parser.add_argument("--dir", default=os.path.join(DATA, "review_versions"))
    args = parser.parse_args()
    review = load(args.review)
    if not review:
        raise SystemExit("close_review.json is missing; no version written")
    now = datetime.now(CST)
    day = str(review.get("date") or now.date().isoformat())
    version = now.strftime("%Y%m%dT%H%M%S")
    os.makedirs(args.dir, exist_ok=True)
    target = os.path.join(args.dir, f"{day}_{version}.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(review, handle, ensure_ascii=False, indent=1)
    index = load(args.index)
    rows = index.get("versions") or []
    rows.append({
        "date": day,
        "version": version,
        "generated_at": review.get("generated_at"),
        "status": review.get("status"),
        "file": os.path.relpath(target, ROOT),
        "bytes": os.path.getsize(target),
    })
    payload = {"ts": now.isoformat(timespec="seconds"), "engine": "review_versions v1",
               "n": len(rows), "versions": rows[-60:]}
    with open(args.index, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"[review_versions] {target}")


if __name__ == "__main__":
    main()
