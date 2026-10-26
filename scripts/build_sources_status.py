#!/usr/bin/env python3
"""build_sources_status.py — 生成 data/api/sources.json（数据源台账）。

回答三个问题，字段全部来自真实注册表与真实执行器清单：
  - 登记了多少源（total）
  - 多少源真的接进了采集器（with_fetcher）
  - 多少源可用但没人执行（usable_without_fetcher）
  - 哪些被判定 blocked/unauth（不可用，不绕过）

用法: python3 scripts/build_sources_status.py [--out data/api/sources.json]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CST = timezone(timedelta(hours=8))
UsABLE = ("ok", "degraded")


def _group_of(src):
    return str(src.get("freq") or "other")[:24]


def build():
    import sources_registry as sr
    try:
        import collect_all_sources as cas
        fetchers = {name for group in cas.FETCHERS.values() for name, _ in group}
    except Exception as exc:  # 采集器导入失败时如实降级，不谎报
        print("[sources] 采集器导入失败: %s" % type(exc).__name__, file=sys.stderr)
        fetchers = set()

    items = []
    for src in sr.SOURCES:
        sid = src.get("id")
        status = src.get("status") or "missing"
        items.append({
            "id": sid,
            "name": src.get("name") or sid,
            "group": _group_of(src),
            "status": status,
            "has_fetcher": sid in fetchers,
            "tested_at": (src.get("tested_at") or src.get("last_check")
                          or src.get("checked_at")),
            "note": str(src.get("notes") or src.get("note") or "")[:300],
        })

    usable = [i for i in items if i["status"] in UsABLE]
    merged = [i for i in items if i["has_fetcher"]]
    live = [i for i in usable if i["has_fetcher"]]
    gap = [i for i in usable if not i["has_fetcher"]]
    blocked = [i for i in items if i["status"] in ("blocked", "unauth")]

    groups = {}
    for item in items:
        g = item["group"] or "other"
        stat = groups.setdefault(g, {"total": 0, "with_fetcher": 0, "usable": 0})
        stat["total"] += 1
        stat["with_fetcher"] += 1 if item["has_fetcher"] else 0
        stat["usable"] += 1 if item["status"] in UsABLE else 0

    return {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "total": len(items),
        "usable": len(usable),
        "with_fetcher": len(merged),
        "live": len(live),
        "usable_without_fetcher": len(gap),
        "blocked_or_unauth": len(blocked),
        "groups": groups,
        "merged_ids": sorted(i["id"] for i in merged),
        "gap_ids": sorted(i["id"] for i in gap),
        "blocked_ids": sorted(i["id"] for i in blocked),
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/api/sources.json")
    args = ap.parse_args()
    payload = build()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("[sources] total=%d usable=%d with_fetcher=%d live=%d gap=%d blocked=%d -> %s" % (
        payload["total"], payload["usable"], payload["with_fetcher"],
        payload["live"], payload["usable_without_fetcher"],
        payload["blocked_or_unauth"], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
