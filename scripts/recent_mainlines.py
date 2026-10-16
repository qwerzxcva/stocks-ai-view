#!/usr/bin/env python3
"""recent_mainlines.py — 近30日主线统计引擎。

输入优先读取 data/review_history.jsonl 中每日 close_review 记录，并合并当前
mainline/hot_themes。没有足够历史时明确标注 sample_insufficient，不伪造统计。
"""
import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load_json(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_history(days):
    path = os.path.join(DATA, "review_history.jsonl")
    rows = []
    if not os.path.exists(path):
        return rows
    cutoff = datetime.now(CST).date() - timedelta(days=days)
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            row = json.loads(line)
            day = datetime.strptime(str(row.get("date"))[:10], "%Y-%m-%d").date()
            if day >= cutoff:
                rows.append(row)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(DATA, "recent_mainlines.json"))
    args = ap.parse_args()
    now = datetime.now(CST)
    history = read_history(args.days)
    current = load_json("mainline.json")
    themes = load_json("hot_themes.json")

    counts = Counter()
    directions = defaultdict(Counter)
    stocks = defaultdict(Counter)
    daily = []
    for row in history:
        names = []
        for group in (row.get("mainlines") or [], row.get("themes") or [], row.get("by_group") or {}):
            if isinstance(group, dict):
                names.extend(str(k) for k in group.keys())
            elif isinstance(group, list):
                for item in group:
                    if isinstance(item, str): names.append(item)
                    elif isinstance(item, dict) and item.get("name"): names.append(item["name"])
        names = list(dict.fromkeys(n for n in names if n))
        daily.append({"date": row.get("date"), "mainlines": names})
        for name in names: counts[name] += 1
    for item in (themes.get("themes") or []) + (current.get("mainlines") or []):
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            counts[name] += 1
            directions[name][item.get("direction", "neutral")] += 1
            for code in item.get("codes", item.get("stocks", []))[:30]: stocks[name][str(code)] += 1

    ranked = []
    for name, n in counts.most_common():
        direction = directions[name].most_common(1)[0][0] if directions[name] else "neutral"
        ranked.append({
            "name": name,
            "days_present": n,
            "presence_pct": round(n / max(1, len(history)) * 100, 1),
            "direction": direction,
            "stable": n >= 3,
            "representative_stocks": [x for x, _ in stocks[name].most_common(10)],
            "evidence": "review_history.jsonl + current mainline/hot_themes",
        })
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "recent_mainlines.py",
        "window_days": args.days,
        "history_days_available": len(history),
        "status": "ok" if len(history) >= 3 else "sample_insufficient",
        "mainlines": ranked[:30],
        "daily": daily,
        "current": current.get("mainlines", []),
        "meta": {
            "purpose": "把单日今日主线合并为近30日稳定主线，避免一天热点被误判为主线",
            "minimum_history": 3,
            "caveat": "历史样本不足时只展示当前主线，不给稳定性结论",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f: json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[recent_mainlines] history={len(history)} status={payload['status']} themes={len(ranked)} -> {args.out}")


if __name__ == "__main__": main()
