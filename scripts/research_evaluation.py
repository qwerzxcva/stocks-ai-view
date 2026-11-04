#!/usr/bin/env python3
"""Historical two-stock research audit; no fitting, trading, or warehouse writes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from engine_spec import AGGREGATION, SPEC_VERSION, VOTE_ENGINES
from engine_v5 import aggregate_votes, build_votes

ROOT = Path(__file__).resolve().parents[1]
HORIZONS = (22, 60)
# Only the two byte-identical user attachments have benchmark provenance approval.
APPROVED_BENCHMARKS = {
    "sh600519": "b1a39e978bc37ae2a8fa3027acefaa6355f2fb145bdb5fbc07462a2261012400",
    "sz002242": "4baf5881b6a571486737c593da15e9dede3b41f564905981fddfa07df98c8535",
}
PRICE_FIELDS = ("open", "high", "low", "close")
REQUIRED = {"date", "source", "volume", "turnover", "hfq_factor"} | {
    f"{field}_price" for field in PRICE_FIELDS
}


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    factor: float
    source: str = "synthetic_fixture"


def iso_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Not a strict ISO date: {value!r}")
    return date.fromisoformat(value)


def parse_csv(text: str, *, approved_benchmark: bool = False) -> tuple[list[Bar | None], dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not REQUIRED.issubset(reader.fieldnames or []):
        raise ValueError("CSV is missing required columns")
    bars: list[Bar | None] = []
    rejected: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    previous: date | None = None
    dates: list[str] = []
    accepted_dates: list[str] = []
    source_by_date: dict[str, str] = {}
    for row in reader:
        current = iso_date(row["date"])
        if previous is not None and current <= previous:
            raise ValueError("CSV dates must be strictly increasing, without duplicates")
        previous = current
        dates.append(row["date"])
        source = row.get("source") or "missing_source"
        source_counts[source] += 1
        source_by_date[row["date"]] = source
        if approved_benchmark and source not in {"cnfin", "api", "csv_legacy"}:
            raise ValueError("Unknown source in approved benchmark")
        if source != "cnfin" and not approved_benchmark:
            rejected[source] += 1
            bars.append(None)  # A source gap must not splice distant sessions together.
            continue
        try:
            prices = [float(row[f"{key}_price"]) for key in PRICE_FIELDS]
            volume, turnover, factor = (float(row[key]) for key in
                                        ("volume", "turnover", "hfq_factor"))
            op, high, low, close = prices
            valid = (all(math.isfinite(x) and x > 0 for x in prices + [volume, factor])
                     and math.isfinite(turnover) and turnover >= 0
                     and high >= max(op, close) and low <= min(op, close))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            rejected["invalid_ohlcv_factor"] += 1
            bars.append(None)
            continue
        bars.append(Bar(row["date"], op, high, low, close, volume, turnover, factor, source))
        accepted_dates.append(row["date"])
    return bars, {
        "row_count": len(bars), "accepted": len(accepted_dates),
        "rejected": sum(rejected.values()), "rejected_reasons": dict(rejected),
        "source_counts": dict(source_counts), "source_by_date": source_by_date,
        "date_range": [dates[0], dates[-1]] if dates else None,
        "accepted_date_range": [accepted_dates[0], accepted_dates[-1]] if accepted_dates else None,
        "source_policy": ("fixed code+SHA256 approved benchmark; original mixed source labels preserved"
                          if approved_benchmark else "row-level cnfin only; every rejected row breaks continuity"),
    }


def load_input(code: str, path: Path, *, approved_benchmarks: bool = False) -> tuple[list[Bar | None], dict[str, Any]]:
    if code not in APPROVED_BENCHMARKS:
        raise ValueError("Unknown input code")
    path = path.resolve()
    expected_path = (ROOT / "tests" / "fixtures" / "research" / f"{code}_benchmark.csv"
                     if approved_benchmarks else ROOT / "tests" / "fixtures" / f"{code}_gt.csv")
    if path != expected_path:
        raise ValueError("Only the designated fixture path for this mode is supported")
    payload = path.read_bytes()
    sha256 = hashlib.sha256(payload).hexdigest()
    if approved_benchmarks and sha256 != APPROVED_BENCHMARKS[code]:
        raise ValueError("Benchmark SHA256 does not match fixed approval for this code")
    bars, info = parse_csv(payload.decode("utf-8-sig"), approved_benchmark=approved_benchmarks)
    info.update(path=str(path.relative_to(ROOT)), sha256=sha256,
                benchmark_approved=approved_benchmarks,
                provenance=("user attachment; fixed code/SHA256 approval; original source labels retained"
                            if approved_benchmarks else "row-level cnfin evidence"))
    return bars, info


def adjusted_window(bars: list[Bar], anchor: float) -> dict[str, list[float]]:
    result = {key: [getattr(bar, key) * (bar.factor / anchor) for bar in bars]
              for key in PRICE_FIELDS}
    if any(not math.isfinite(x) or x <= 0 for values in result.values() for x in values):
        raise ValueError("Nonfinite or nonpositive adjusted price")
    result["volume"] = [bar.volume for bar in bars]
    return result


def build_signals(code: str, bars: list[Bar | None]) -> list[dict[str, Any]]:
    engines = {e["weight_key"] for e in VOTE_ENGINES}
    weights = {key: 1.0 for key in engines}
    signals = []
    history: list[Bar] = []
    for index, bar in enumerate(bars):
        if bar is None:
            history = []
            continue
        history.append(bar)
        history = history[-120:]
        if len(history) != 120:
            continue
        adjusted = adjusted_window(history, bar.factor)
        votes = build_votes(adjusted["close"], adjusted["high"], adjusted["low"],
                            adjusted["volume"], None, "")
        votes_by_key = {v["indicator"]: v["vote"] * v["strength"] for v in votes}
        scores = {key: votes_by_key.get(key) for key in sorted(engines)}
        positive = {key: value is not None and value > 0 for key, value in scores.items()}
        for kind in ("family_weighted_vote", "weighted_vote"):
            spec = {"engines": VOTE_ENGINES, "aggregation": dict(AGGREGATION, kind=kind)}
            aggregate = aggregate_votes(votes, weights, spec)
            scores[kind] = aggregate["score"] if aggregate else None
            positive[kind] = aggregate is not None and aggregate["score"] >= AGGREGATION["signal_bullish"]
        signals.append({"code": code, "date": bar.date, "index": index,
                        "scores": scores, "positive": positive})
    return signals


def forward_label(bars: list[Bar | None], index: int, horizon: int,
                  cost_bps: float) -> dict[str, Any] | None:
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError("Cost must be finite, in [0, 10000) bps per side")
    if horizon <= 0 or index < 0 or index >= len(bars):
        raise ValueError("Invalid signal index or horizon")
    signal = bars[index]
    future = bars[index + 1:index + 1 + horizon]
    if signal is None or len(future) != horizon or any(bar is None for bar in future):
        return None
    observed = [bar for bar in future if bar is not None]
    adjusted = adjusted_window(observed, signal.factor)
    entry = adjusted["open"][0]
    cost = cost_bps / 10000
    paid = entry * (1 + cost)
    net = adjusted["close"][-1] * (1 - cost) / paid - 1
    if not math.isfinite(net):
        raise ValueError("Nonfinite realized return")
    return {"entry_date": observed[0].date, "label_end": observed[-1].date,
            "entry_price": entry,
            "recoup": max(adjusted["high"]) * (1 - cost) >= paid, "net_return": net}


def purged_walk_forward(samples: list[dict[str, Any]], validation_size: int,
                        min_train: int, embargo_days: int) -> list[dict[str, Any]]:
    """Fixed-rule audit, not fitting. Sizes count globally grouped signal dates.

    Calendar embargo applies after the training label's last observation: require
    label_end < validation_start - embargo_days. Future samples cannot train.
    """
    if validation_size < 1 or min_train < 1 or embargo_days < 0:
        raise ValueError("Invalid walk-forward parameters")
    for sample in samples:
        iso_date(sample["date"])
        if sample.get("label_end"):
            iso_date(sample["label_end"])
    dates = sorted({s["date"] for s in samples})
    folds = []
    for start in range(min_train, len(dates), validation_size):
        validation_dates = set(dates[start:start + validation_size])
        validation_start = dates[start]
        boundary = iso_date(validation_start) - timedelta(days=embargo_days)
        train = [s for s in samples if s["date"] < validation_start
                 and s.get("label_end") and iso_date(s["label_end"]) < boundary]
        train_dates = {s["date"] for s in train}
        folds.append({"validation_start": validation_start,
                      "validation_end": max(validation_dates),
                      "status": "ok" if len(train_dates) >= min_train else "insufficient",
                      "train": train,
                      "validation": [s for s in samples if s["date"] in validation_dates]})
    return folds


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    xx, yy = sum(v * v for v in dx), sum(v * v for v in dy)
    if xx == 0 or yy == 0:
        return None
    value = sum(a * b for a, b in zip(dx, dy)) / math.sqrt(xx * yy)
    return max(-1.0, min(1.0, value)) if math.isfinite(value) else None


def average_ranks(values: list[float]) -> list[float]:
    ranks = [0.0] * len(values)
    ordered = sorted(range(len(values)), key=values.__getitem__)
    for _, group in itertools.groupby(enumerate(ordered), key=lambda pair: values[pair[1]]):
        tied = list(group)
        rank = statistics.mean(position + 1 for position, _ in tied)
        for _, index in tied:
            ranks[index] = rank
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    return pearson(average_ranks(x), average_ranks(y))


def cross_sectional_ic(observations: list[tuple[str, float, float]], min_stocks: int = 20) -> dict[str, Any]:
    if min_stocks < 2:
        raise ValueError("IC requires at least two distinct stocks")
    by_code = {code: (score, realized) for code, score, realized in observations}
    values = list(by_code.values())
    return {"coverage": len(values), "minimum_stocks": min_stocks,
            "ic": spearman([v[0] for v in values], [v[1] for v in values])
            if len(values) >= min_stocks else None,
            "reason": "below_minimum_stocks" if len(values) < min_stocks
            else ("zero_variance" if spearman([v[0] for v in values], [v[1] for v in values]) is None else None)}


def icir(values: list[float | None]) -> float | None:
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return None
    sd = statistics.stdev(valid)
    return statistics.mean(valid) / sd if sd > 0 else None


def family_correlations(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: dict[str, list[str]] = defaultdict(list)
    for engine in VOTE_ENGINES:
        family = engine.get("family")
        if family and family != "external":
            families[family].append(engine["weight_key"])
    result = []
    for code in sorted({s["code"] for s in signals}):
        history = [s for s in signals if s["code"] == code]
        for family, keys in families.items():
            for left, right in itertools.combinations(keys, 2):
                common = [(s["scores"][left], s["scores"][right]) for s in history
                          if s["scores"][left] is not None and s["scores"][right] is not None]
                n = len(common)
                result.append({"code": code, "family": family, "left": left, "right": right,
                               "n": n, "as_of": history[-1]["date"] if history else None,
                               "pearson": pearson([p[0] for p in common], [p[1] for p in common])
                               if n >= 30 else None,
                               "reason": "diagnostic_only_not_independence_proof" if n >= 30
                               else "fewer_than_30_common_past_dates"})
    return result


def cost_key(value: float) -> str:
    return f"{value:g}"


def validation_summary(samples: list[dict[str, Any]], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(s["code"], s["date"]): s for s in signals}
    groups: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        signal = by_key[(sample["code"], sample["date"])]
        for cost, label in sample["labels"].items():
            if label is None:
                continue
            for model in ("all_valid_signals", "family_weighted_vote", "weighted_vote"):
                if model == "all_valid_signals" or signal["positive"][model]:
                    groups[(sample["horizon"], cost, model)].append(label)
    result = []
    costs = sorted({cost for sample in samples for cost in sample["labels"]})
    for horizon in HORIZONS:
        for cost in costs:
            for model in ("all_valid_signals", "family_weighted_vote", "weighted_vote"):
                labels = groups[(horizon, cost, model)]
                result.append({"horizon": horizon, "cost_bps_per_side": float(cost), "cohort": model,
                               "n": len(labels),
                               "recoup_rate": statistics.mean(x["recoup"] for x in labels) if labels else None,
                               "mean_net_return": statistics.mean(x["net_return"] for x in labels) if labels else None})
    return result


def evaluate(inputs: dict[str, tuple[list[Bar | None], dict[str, Any]]], cost_bps: float = 0,
             min_ic_stocks: int = 20, validation_size: int = 60, min_train: int = 120,
             embargo_days: int = 5) -> dict[str, Any]:
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError("Cost must be finite, in [0, 10000) bps per side")
    if min_ic_stocks < 20:
        raise ValueError("This research report requires at least 20 stocks for daily market IC")
    costs = sorted({0.0, 10.0, 30.0, float(cost_bps)})
    signals = []
    samples = []
    grouped: dict[tuple[str, int], list[tuple[dict, dict]]] = defaultdict(list)
    for code, (bars, _) in sorted(inputs.items()):
        current = build_signals(code, bars)
        signals.extend(current)
        for signal in current:
            for horizon in HORIZONS:
                labels = {cost_key(c): forward_label(bars, signal["index"], horizon, c) for c in costs}
                default = labels[cost_key(cost_bps)]
                sample = {"code": code, "date": signal["date"], "horizon": horizon,
                          "label_end": default["label_end"] if default else None, "labels": labels}
                samples.append(sample)
                grouped[(signal["date"], horizon)].append((signal, sample))
    engines = sorted({e["weight_key"] for e in VOTE_ENGINES} | {"family_weighted_vote", "weighted_vote"})
    daily_metrics = {}
    daily_ic = []
    ic_values: dict[tuple[str, int], list[float | None]] = defaultdict(list)
    for (day, horizon), members in sorted(grouped.items()):
        metrics = {}
        for engine in engines:
            valid = [(sig, sample) for sig, sample in members if sig["scores"][engine] is not None]
            obs = [(sig["code"], sig["scores"][engine], sample["labels"][cost_key(cost_bps)]["net_return"])
                   for sig, sample in valid if sample["labels"][cost_key(cost_bps)] is not None]
            ic = cross_sectional_ic(obs, min_ic_stocks)
            daily_ic.append({"date": day, "horizon": horizon, "engine": engine, **ic})
            ic_values[(engine, horizon)].append(ic["ic"])
            sensitivities = {}
            for c in costs:
                labels = [sample["labels"][cost_key(c)] for _, sample in valid
                          if sample["labels"][cost_key(c)] is not None]
                bullish = [sample["labels"][cost_key(c)] for sig, sample in valid
                           if sig["positive"][engine] and sample["labels"][cost_key(c)] is not None]
                sensitivities[cost_key(c)] = {"n": len(labels), "positive_n": len(bullish),
                    "recoup_rate": statistics.mean(label["recoup"] for label in labels) if labels else None,
                    "mean_net_return": statistics.mean(label["net_return"] for label in labels) if labels else None,
                    "positive_recoup_rate": statistics.mean(label["recoup"] for label in bullish) if bullish else None,
                    "positive_mean_net_return": statistics.mean(label["net_return"] for label in bullish) if bullish else None}
            metrics[engine] = {"signal_coverage": len(valid), "costs": sensitivities}
        daily_metrics.setdefault(day, {})[str(horizon)] = metrics
    folds = purged_walk_forward(samples, validation_size, min_train, embargo_days)
    fold_audit = [{"validation_start": f["validation_start"], "validation_end": f["validation_end"],
                   "status": f["status"], "train_count": len(f["train"]),
                   "train_date_count": len({s["date"] for s in f["train"]}),
                   "max_train_label_end": max((s["label_end"] for s in f["train"]), default=None),
                   "validation_count": len(f["validation"]),
                   "validation_metrics": validation_summary(f["validation"], signals) if f["status"] == "ok" else [],
                   "metrics_status": "fixed_rule_validation" if f["status"] == "ok" else "insufficient_train_dates"}
                  for f in folds]
    accepted_dates = [b.date for bars, _ in inputs.values() for b in bars if b is not None]
    return {
        "status": "history_only" if any(s["label_end"] for s in samples) else "insufficient",
        "reason": None if any(s["label_end"] for s in samples) else
        "No complete 22/60-session label after 120 contiguous valid cnfin bars or fixed approved benchmark bars; source gaps are not spliced",
        "snapshot": "immutable input-file SHA256 and accepted date ranges in sources; no live snapshot",
        "scope": {"codes": sorted(inputs), "description": "two-stock fixtures, not marketwide or a 300/40 blue-chip model"},
        "as_of": max(accepted_dates, default=None), "history_only": True,
        "freshness": "historical fixtures; not live or latest market data",
        "spec_version": SPEC_VERSION, "weight_policy": "equal_weights",
        "weights": {e["weight_key"]: 1.0 for e in VOTE_ENGINES},
        "sources": {code: info for code, (_, info) in sorted(inputs.items())},
        "adjustment": "all OHLC * row hfq_factor / signal-date hfq_factor; raw volume unchanged",
        "entry_rule": "next observed trading-row open; rejected rows break both history and labels",
        "horizon_rule": "22/60 observations including entry day; incomplete or gapped label is null",
        "recoup_rule": "max adjusted future high * (1-cost) >= adjusted entry open * (1+cost)",
        "net_return_rule": "last adjusted close * (1-cost) / (entry adjusted open * (1+cost)) - 1",
        "positive_rule": "single-engine vote*strength > 0; aggregate score >= spec signal_bullish",
        "cost_bps_per_side": costs, "primary_cost_bps": cost_bps,
        "cost_caveat": "hypothetical symmetric fees, not measured slippage or capacity evidence",
        "sample_count": len(samples), "sample_unit": "code/signal_date/horizon; costs are sensitivity variants, not independent samples",
        "overlap_caveat": "overlapping horizons and correlated stocks/engines are not independent trials",
        "evaluation_caveat": "retrospective fixed-rule audit, not an untouched model-selection holdout; cohort sizes differ; no portfolio simulation or improvement significance claim",
        "validation_summary": validation_summary([s for f in folds if f["status"] == "ok" for s in f["validation"]], signals),
        "signals": signals, "samples": samples, "daily_metrics": daily_metrics, "daily_ic": daily_ic,
        "icir": [{"engine": engine, "horizon": horizon, "icir": icir(values),
                  "valid_days": sum(v is not None for v in values), "total_days": len(values),
                  "definition": "daily IC mean / sample standard deviation; not annualized"}
                 for (engine, horizon), values in sorted(ic_values.items())],
        "family_correlations": family_correlations(signals),
        "walk_forward": {"mode": "fixed-rule audit only, no training, fitting or parameter selection",
                         "validation_size_dates": validation_size, "min_train_dates": min_train,
                         "embargo_calendar_days": embargo_days,
                         "purge_rule": "training label_end < validation_start - embargo calendar days",
                         "folds": fold_audit},
        "marketwide_evidence": {"status": "insufficient", "reason": "only two stock fixtures; IC normally needs at least 20 stocks"},
        "real_holdings": {"status": "insufficient", "reason": "no actual holdings or portfolio weights supplied"},
        "capacity": {"status": "insufficient", "reason": "no executions, market impact or measured slippage"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", metavar="CODE=PATH")
    parser.add_argument("--approved-benchmarks", action="store_true",
                        help="Use only fixed code+SHA256 approved user attachments; no custom approvals")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=0)
    parser.add_argument("--min-ic-stocks", type=int, default=20)
    parser.add_argument("--validation-size", type=int, default=60)
    parser.add_argument("--min-train", type=int, default=120)
    parser.add_argument("--embargo-days", type=int, default=5)
    args = parser.parse_args()
    inputs = {}
    fixture_dir = ROOT / "tests" / "fixtures"
    specs = args.input or [
        f"{code}={fixture_dir / 'research' / (code + '_benchmark.csv')}" if args.approved_benchmarks
        else f"{code}={fixture_dir / (code + '_gt.csv')}" for code in APPROVED_BENCHMARKS]
    try:
        for item in specs:
            code, raw_path = item.split("=", 1)
            path = Path(raw_path).resolve()
            if code in inputs:
                raise ValueError("Duplicate input code")
            inputs[code] = load_input(code, path, approved_benchmarks=args.approved_benchmarks)
        report = evaluate(inputs, args.cost_bps, args.min_ic_stocks, args.validation_size,
                          args.min_train, args.embargo_days)
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        output = args.output.resolve()
        if (output == ROOT or output.is_relative_to(ROOT / "data")
                or output.is_relative_to(ROOT / "tests" / "fixtures")):
            raise ValueError("Cannot overwrite warehouse data or source fixtures")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": report["status"], "as_of": report["as_of"],
                      "sample_count": report["sample_count"], "output": str(output)}, allow_nan=False))


if __name__ == "__main__":
    main()
