#!/usr/bin/env python3
"""Backfill stock-keyed daily fund-flow histories in bounded cloud batches."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from collect_moneyflow import fetch_fflow
from flow_divergence import fetch_sina_flow

CST = timezone(timedelta(hours=8))


def export_one(code, info, root, limit):
    market = info.get("market")
    if market not in ("sh", "sz", "bj"):
        return "unsupported_exchange"
    if market == "bj":
        rows, error = fetch_sina_flow(code, limit)
        rows = [{"time": row.get("date"), "main_net": row.get("main_net"),
                 "small_net": None, "medium_net": None, "large_net": None,
                 "xlarge_net": None} for row in rows]
        source = "sina_moneyflow"
    else:
        rows, error = fetch_fflow(code, klt=101, lmt=limit, retries=1)
        source = "eastmoney_fflow_daily"
    if not rows:
        return error or "source_empty"
    history = {}
    for row in rows:
        date = str(row.get("time") or "")[:10]
        if row.get("main_net") is None:
            continue
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        history[date] = {"date": date, "main_net_inflow": row["main_net"],
                         "small_net_inflow": row.get("small_net"),
                         "medium_net_inflow": row.get("medium_net"),
                         "large_net_inflow": row.get("large_net"),
                         "xlarge_net_inflow": row.get("xlarge_net")}
    if not history:
        return "invalid_history"
    destination = os.path.join(root, "stocks", info["shard"], code, "money", "fflow.json")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    payload = {"schema_version": 1, "code": code, "exchange": market,
               "source": source, "captured_at": datetime.now(CST).isoformat(timespec="seconds"),
               "first_date": min(history), "last_date": max(history), "n": len(history),
               "rows": [history[date] for date in sorted(history)]}
    temporary = destination + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, destination)
    return "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--history-limit", type=int, default=100000)
    args = parser.parse_args()
    if args.start < 0 or args.count < 1:
        parser.error("start must be nonnegative; count must be positive")
    with open(args.master, encoding="utf-8") as handle:
        master = json.load(handle)["stocks"]
    universe = sorted((code, info) for code, info in master.items()
                      if str(info.get("asset_type") or "").startswith("stock_"))
    counts = {}
    for code, info in universe[args.start:args.start + args.count]:
        status = export_one(code, info, args.out_root, args.history_limit)
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"start": args.start, "count": args.count, "universe": len(universe),
                      "next_start": min(len(universe), args.start + args.count), "statuses": counts},
                     ensure_ascii=False))
    return 0 if counts.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
