#!/usr/bin/env python3
"""Per-security K-line coverage and full-window closing-price averages."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import glob
import gzip
import json
import math
import os

from decision_contract import normalize_code
from paths import BULK

WINDOWS = (20, 60, 120, 252)
CST = timezone(timedelta(hours=8))


def load_universe(path):
    universe = set()
    if not os.path.exists(path):
        return universe
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            code = line.split("|", 1)[0].strip()
            try:
                digits, exchange = normalize_code(code)
            except ValueError:
                continue
            universe.add(exchange + digits)
    return universe


def _files(bulk):
    paths = sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv")) +
                   glob.glob(os.path.join(bulk, "day_qfq_*.csv.gz")))
    plain = {path for path in paths if path.endswith(".csv")}
    return [path for path in paths if not path.endswith(".gz") or path[:-3] not in plain]


def _rows(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace("|", ",").split(",")
            if len(parts) < 4:
                continue
            try:
                digits, exchange = normalize_code(parts[0])
                day = date.fromisoformat(parts[1].strip())
                close = float(parts[3])
                if not math.isfinite(close) or close <= 0:
                    continue
            except (ValueError, OverflowError):
                continue
            yield exchange + digits, day.isoformat(), close


def build_features(bulk=BULK, codes=None, universe_path=None):
    universe_path = universe_path or os.path.join(bulk, "universe.csv")
    universe = load_universe(universe_path)
    duplicates = defaultdict(int)
    conflicts = defaultdict(int)
    sources = defaultdict(set)
    files = _files(bulk)
    if codes is not None:
        requested = set(exchange + code for code, exchange in map(normalize_code, codes))
    else:
        requested = set(universe)
        if not requested:
            for path in files:
                for symbol, _, _ in _rows(path):
                    requested.add(symbol)
    by_code = defaultdict(dict)
    for path in files:
        filename = os.path.basename(path)
        for symbol, day, close in _rows(path):
            if symbol not in requested:
                continue
            existing = by_code[symbol].get(day)
            sources[symbol].add(filename)
            if existing is not None:
                duplicates[symbol] += 1
                if not math.isclose(existing[0], close, rel_tol=1e-9, abs_tol=1e-9):
                    conflicts[symbol] += 1
                if filename.startswith("day_qfq_recent.csv") and not existing[1].startswith("day_qfq_recent.csv"):
                    by_code[symbol][day] = (close, filename)
            else:
                by_code[symbol][day] = (close, filename)
    features = {}
    for symbol in sorted(requested):
        days = sorted(by_code[symbol])
        closes = [by_code[symbol][day][0] for day in days[-max(WINDOWS):]]
        features[symbol] = {
            "code": symbol[2:], "exchange": symbol[:2], "first_date": days[0] if days else None,
            "last_date": days[-1] if days else None, "length": len(days),
            "ma20": round(sum(closes[-20:]) / 20, 4) if len(days) >= 20 else None,
            "ma60": round(sum(closes[-60:]) / 60, 4) if len(days) >= 60 else None,
            "ma120": round(sum(closes[-120:]) / 120, 4) if len(days) >= 120 else None,
            "ma252": round(sum(closes[-252:]) / 252, 4) if len(days) >= 252 else None,
            "duplicate_rows": duplicates[symbol], "conflicting_rows": conflicts[symbol],
            "provenance": sorted(sources[symbol]), "in_universe": symbol in universe,
            "coverage": {f"ma{n}": len(days) >= n for n in WINDOWS},
        }
    return {
        "generated_at": datetime.now(CST).isoformat(),
        "source": "local day_qfq_*.csv[.gz] and universe.csv", "catalog_count": len(universe),
        "universe_count": len(universe),
        "requested_count": len(requested), "covered_count": sum(bool(by_code[c]) for c in requested),
        "universe_covered_count": sum(bool(by_code[c]) for c in requested & universe),
        "file_count": len(files), "duplicate_rows": sum(duplicates.values()),
        "conflicting_rows": sum(conflicts.values()), "features": features,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk", default=BULK)
    parser.add_argument("--codes", help="comma-separated subset; default full universe")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = build_features(args.bulk, args.codes.split(",") if args.codes else None)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
