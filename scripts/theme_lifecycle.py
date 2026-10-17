#!/usr/bin/env python3
"""Theme lifecycle from already collected theme snapshots.

A theme is not a one-day news count. This engine keeps one observation per
theme per day, then classifies the observable stage:

  emerging   first appearance, or quiet for more than five trading observations
  expanding  mention count is rising and the theme is still young
  climax     three or more rising observations, or a one-day doubling
  fading     count has fallen for two observations
  dormant    no fresh observation for three days

The history file is the database-facing record. The JSON is the page-facing
snapshot and never invents a stock code when the source theme has none.
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


def save(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)


def parse_day(value):
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def stage_of(history, today):
    if len(history) >= 2 and history[-1]["count"] < history[-2]["count"] and (
            len(history) < 3 or history[-2]["count"] <= history[-3]["count"]):
        stage = "fading"
    elif len(history) >= 3 and all(
            history[-i]["count"] > history[-i - 1]["count"] for i in (1, 2)):
        stage = "climax"
    elif len(history) >= 2 and history[-1]["count"] >= history[-2]["count"] * 2 and history[-1]["count"] >= 6:
        stage = "climax"
    elif len(history) >= 2 and history[-1]["count"] > history[-2]["count"]:
        stage = "expanding"
    elif len(history) == 1:
        stage = "emerging"
    else:
        stage = "dormant"
    if len(history) == 1 or (today - history[-2]["day"]).days > 5:
        if stage not in ("climax", "fading"):
            stage = "emerging"
    return stage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--themes", default=os.path.join(DATA, "hot_themes.json"))
    parser.add_argument("--history", default=os.path.join(DATA, "theme_lifecycle_history.json"))
    parser.add_argument("--out", default=os.path.join(DATA, "theme_lifecycle.json"))
    args = parser.parse_args()

    themes = load(args.themes)
    observed = parse_day(themes.get("ts")) or datetime.now(CST).date()
    history_doc = load(args.history)
    history = history_doc.get("themes") or {}

    for theme in themes.get("themes") or []:
        name = str(theme.get("name") or "").strip()
        if not name:
            continue
        rows = history.setdefault(name, [])
        row = {
            "date": observed.isoformat(),
            "count": int(theme.get("count") or 0),
            "direction": theme.get("direction") or "neutral",
            "stock_count": len(theme.get("stocks") or []),
        }
        if rows and rows[-1].get("date") == row["date"]:
            rows[-1] = row
        else:
            rows.append(row)
        del rows[:-30]

    classified = []
    for name, rows in history.items():
        parsed = []
        for row in rows:
            day = parse_day(row.get("date"))
            if day:
                parsed.append({"day": day, "count": int(row.get("count") or 0),
                               "direction": row.get("direction") or "neutral",
                               "stock_count": int(row.get("stock_count") or 0)})
        if not parsed:
            continue
        age = (observed - parsed[-1]["day"]).days
        stage = "dormant" if age >= 3 else stage_of(parsed, observed)
        counts = [item["count"] for item in parsed]
        classified.append({
            "name": name,
            "stage": stage,
            "age_days": age,
            "observations": len(parsed),
            "latest_count": counts[-1],
            "peak_count": max(counts),
            "direction": parsed[-1]["direction"],
            "stock_count": parsed[-1]["stock_count"],
            "count_path": counts[-7:],
            "first_seen": parsed[0]["day"].isoformat(),
            "last_seen": parsed[-1]["day"].isoformat(),
        })

    order = {"climax": 0, "expanding": 1, "emerging": 2, "fading": 3, "dormant": 4}
    classified.sort(key=lambda item: (order[item["stage"]], -item["latest_count"], item["name"]))
    payload = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "as_of": observed.isoformat(),
        "engine": "theme_lifecycle v1",
        "n_themes": len(classified),
        "stages": {name: sum(item["stage"] == name for item in classified) for name in order},
        "themes": classified,
        "meta": {
            "source": "hot_themes.json observations",
            "provenance": {"source": "hot_themes", "confidence": 0.7,
                           "snapshot_ts": themes.get("ts")},
            "limitation": "One observation per publishing day; intraday count changes do not create a new lifecycle step.",
        },
    }
    save(args.history, {"themes": history})
    save(args.out, payload)
    print(f"[theme_lifecycle] themes={len(classified)} stages={payload['stages']} -> {args.out}")


if __name__ == "__main__":
    main()
