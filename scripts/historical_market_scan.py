#!/usr/bin/env python3
"""Scan the full security master using dated K-line and fund-flow histories."""
import argparse
import glob
import gzip
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_v5
from paths import BULK, AI_VIEW_DATA

CST = timezone(timedelta(hours=8))


def load_master(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["stocks"]


def load_bars(directory, master):
    histories = defaultdict(dict)
    conflicts = 0
    conflict_codes = set()
    for path in sorted(glob.glob(os.path.join(directory, "day_qfq_*.csv*"))):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line or line.startswith("#") or "|" not in line:
                    continue
                symbol, _, tail = line.strip().partition("|")
                code = symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol
                info = master.get(code)
                if not info or symbol[:2] != info.get("market"):
                    continue
                parts = tail.split(",") if "," in tail else tail.split("|")
                if len(parts) < 6:
                    continue
                try:
                    row = [parts[0]] + [float(value) for value in parts[1:6]]
                    datetime.strptime(row[0], "%Y-%m-%d")
                except (ValueError, TypeError):
                    continue
                previous = histories[code].get(row[0])
                if previous is not None and previous != row:
                    conflicts += 1
                    conflict_codes.add(code)
                histories[code][row[0]] = row
    return histories, conflicts, conflict_codes


def load_flow(root, info, code):
    shard = str(info.get("shard") or "")
    candidates = (os.path.join(root, "stocks", shard, code, "money", "fflow.json"),
                  os.path.join(root, shard, code, "money", "fflow.json"))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if str(payload.get("code")) != code:
                return {}, "wrong_code"
            rows = payload.get("rows") or []
            by_date = {}
            for row in rows:
                date = str(row.get("date") or "")[:10]
                value = row.get("main_net_inflow")
                if value is not None:
                    datetime.strptime(date, "%Y-%m-%d")
                    by_date[date] = float(value)
            return by_date, None if by_date else "empty_flow"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}, "invalid_flow"
    return {}, "missing_flow"


def scan(master, histories, flow_root, asof, min_bars=30, min_flow=20, conflict_codes=frozenset()):
    reasons = Counter()
    candidates = []
    for code, info in sorted(master.items()):
        if not str(info.get("asset_type") or "").startswith("stock_"):
            continue
        if code in conflict_codes:
            reasons["conflicting_kline"] += 1
            continue
        bars = [row for date, row in sorted(histories.get(code, {}).items()) if date <= asof]
        if not bars:
            reasons["missing_kline"] += 1
            continue
        if bars[-1][0] != asof:
            reasons["stale_kline"] += 1
            continue
        if len(bars) < min_bars:
            reasons["short_kline"] += 1
            continue
        flow, error = load_flow(flow_root, info, code)
        if error:
            reasons[error] += 1
            continue
        if asof not in flow:
            reasons["stale_flow"] += 1
            continue
        aligned = [flow[row[0]] for row in bars[-min_flow:] if row[0] in flow]
        if len(aligned) < min_flow:
            reasons["short_aligned_flow"] += 1
            continue
        closes, highs, lows, volumes = ([row[index] for row in bars] for index in (2, 3, 4, 5))
        score = engine_v5.score_stock(closes, highs, lows, volumes, code_bare=code)
        if score is None:
            reasons["no_signal"] += 1
            continue
        candidates.append({"code": code, "exchange": info.get("market"), "name": info.get("name"),
                           "asof": asof, "score": score["score"], "resonance": score["resonance"],
                           "last_close": closes[-1], "main_net_1d": aligned[-1],
                           "main_net_3d": sum(aligned[-3:]), "kline_bars": len(bars),
                           "flow_aligned_bars": len(aligned), "kline_first_date": bars[0][0],
                           "kline_last_date": bars[-1][0], "flow_source": "historical_fflow",
                           "action": "observe"})
    candidates.sort(key=lambda row: (-row["score"], -row["main_net_3d"], row["code"]))
    return candidates, dict(reasons)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True)
    parser.add_argument("--bulk", default=BULK)
    parser.add_argument("--flow-root", required=True)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "historical_scan.json"))
    parser.add_argument("--min-coverage", type=float, default=0.8)
    args = parser.parse_args()
    master = load_master(args.master)
    histories, conflicts, conflict_codes = load_bars(args.bulk, master)
    asof = args.asof
    if asof:
        datetime.strptime(asof, "%Y-%m-%d")
    else:
        last_dates = Counter(max(history) for history in histories.values() if history)
        if not last_dates:
            raise SystemExit("no dated K-line histories")
        asof = last_dates.most_common(1)[0][0]
    candidates, reasons = scan(master, histories, args.flow_root, asof,
                               conflict_codes=conflict_codes)
    universe = sum(str(row.get("asset_type") or "").startswith("stock_") for row in master.values())
    coverage = len(candidates) / universe if universe else 0
    payload = {"schema_version": 1, "ts": datetime.now(CST).isoformat(timespec="seconds"),
               "asof": asof, "status": "ready" if coverage >= args.min_coverage else "insufficient",
               "universe": universe, "n_kline": sum(bool(histories.get(code)) for code in master),
               "n_aligned": len(candidates), "coverage": round(coverage, 4),
               "kline_conflicts": conflicts, "n_conflict_stocks": len(conflict_codes), "rejected": reasons,
               "ranked_observations": candidates[:100] if coverage >= args.min_coverage else [],
               "source": {"kline": args.bulk, "flow": args.flow_root, "master": args.master}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    print(f"[historical_scan] {payload['status']} {len(candidates)}/{universe} coverage={coverage:.2%} conflicts={conflicts} -> {args.out}")
    return 0 if payload["status"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
