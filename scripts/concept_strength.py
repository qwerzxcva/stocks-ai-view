#!/usr/bin/env python3
"""Concept and sector strength from data already on disk.

The rank uses only observed fields:
  sector grade and seven-dimension score from sector_engine,
  same-day limit-up count, and main-force net flow.
Missing flow is left missing. It is never treated as zero confirmation.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")


def load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def flow_map(payload):
    rows = payload.get("sector_flow") or []
    return {str(row.get("name") or ""): row for row in rows if row.get("name")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(DATA, "concept_strength.json"))
    args = parser.parse_args()
    sectors = load("sector_engine.json")
    themes = load("hot_themes.json")
    flows = flow_map(load("moneyflow.json"))
    graded = {row.get("sector"): row for row in sectors.get("sectors") or []}

    ranked = []
    for row in sectors.get("sectors") or []:
        name = row.get("sector")
        flow = flows.get(name) or {}
        net = flow.get("main_net")
        ranked.append({
            "name": name,
            "kind": "sector",
            "grade": row.get("grade"),
            "score": row.get("total"),
            "change_pct": row.get("avg_chg_1d"),
            "limit_up_count": row.get("n_zt") or 0,
            "main_net": net,
            "main_net_ratio": flow.get("main_net_ratio"),
            "flow_confirmed": net is not None,
            "leader": row.get("leader"),
            "source": "sector_engine+moneyflow",
        })
    for theme in themes.get("themes") or []:
        name = theme.get("name")
        if name in graded:
            continue
        ranked.append({
            "name": name,
            "kind": "theme",
            "grade": None,
            "score": None,
            "change_pct": None,
            "limit_up_count": 0,
            "main_net": None,
            "main_net_ratio": None,
            "flow_confirmed": False,
            "leader": None,
            "news_count": theme.get("count") or 0,
            "stock_count": len(theme.get("stocks") or []),
            "source": "hot_themes",
        })

    ranked.sort(key=lambda row: (-(row.get("score") or -1), -(row.get("news_count") or 0), row["name"] or ""))
    payload = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "concept_strength v1",
        "n": len(ranked),
        "n_flow_confirmed": sum(row["flow_confirmed"] for row in ranked),
        "items": ranked,
        "meta": {"limitation": "Theme rows have news counts only; they are not price-confirmed."},
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"[concept_strength] {len(ranked)} rows, flow confirmed {payload['n_flow_confirmed']} -> {args.out}")


if __name__ == "__main__":
    main()
