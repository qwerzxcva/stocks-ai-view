#!/usr/bin/env python3
"""Immutable, timestamped decision quotes from the shared signal hub."""
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

import signal_hub

CST = timezone(timedelta(hours=8))
CODE_RE = re.compile(r"^(?:(sh|sz|bj))?(\d{6})$", re.I)


def normalize_code(value):
    match = CODE_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"invalid six-digit security code: {value!r}")
    exchange, code = match.groups()
    exchange = (exchange or ("sh" if code.startswith(("5", "6")) else
                             "bj" if code.startswith(("4", "8")) else "sz")).lower()
    return code, exchange


def parse_quote_time(value, reference_date=None):
    raw = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{14}", raw):
            stamp = datetime.strptime(raw, "%Y%m%d%H%M%S")
        elif re.fullmatch(r"\d{12}", raw):
            stamp = datetime.strptime(raw, "%Y%m%d%H%M")
        elif re.fullmatch(r"\d{8}", raw):
            stamp = datetime.strptime(raw, "%Y%m%d")
        elif re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", raw) and reference_date is not None:
            parts = [int(part) for part in raw.split(":")]
            stamp = datetime.combine(reference_date, datetime.min.time()).replace(
                hour=parts[0], minute=parts[1], second=parts[2] if len(parts) == 3 else 0
            )
        else:
            stamp = datetime.fromisoformat(raw)
        return stamp.replace(tzinfo=CST) if stamp.tzinfo is None else stamp.astimezone(CST)
    except ValueError:
        return None


def quote_session(ts):
    clock = ts.hour * 60 + ts.minute
    if ts.weekday() >= 5 or clock >= 15 * 60 + 5:
        return "closed"
    if clock < 9 * 60 + 15:
        return "preopen"
    if clock < 9 * 60 + 30:
        return "auction"
    if clock < 11 * 60 + 30:
        return "morning"
    if clock < 13 * 60:
        return "lunch"
    if clock < 15 * 60:
        return "afternoon"
    return "close"


@dataclass(frozen=True)
class DecisionQuote:
    code: str
    exchange: str
    status: str
    price: float | None
    prev_close: float | None
    ts: str | None
    source: str
    snapshot_id: str | None
    session: str | None


def decision_quotes(codes, *, now=None, max_age_seconds=300, refresh=True):
    """Batch lookup; absent, invalid and outdated provider stamps never masquerade as live."""
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be nonnegative")
    now = now or datetime.now(CST)
    now = now.replace(tzinfo=CST) if now.tzinfo is None else now.astimezone(CST)
    symbols = dict.fromkeys(f"{exchange}{code}" for code, exchange in map(normalize_code, codes))
    fetched = signal_hub.quotes(list(symbols), refresh=refresh) if symbols else {}
    results = {}
    for symbol in symbols:
        exchange, code = symbol[:2], symbol[2:]
        raw = fetched.get(symbol)
        status, price, prev_close, stamp, digest, session = "missing", None, None, None, None, None
        if raw is not None:
            try:
                price = float(raw["price"])
                prev_close = float(raw["prev_close"])
            except (KeyError, TypeError, ValueError):
                status = "invalid"
            else:
                ts = parse_quote_time(raw.get("time"), now.date())
                if not math.isfinite(price) or not math.isfinite(prev_close) or price <= 0 or prev_close <= 0:
                    status = "invalid"
                elif ts is None:
                    status = "missing_timestamp"
                else:
                    stamp = ts.isoformat()
                    session = quote_session(ts)
                    payload = json.dumps([symbol, stamp, price, prev_close], separators=(",", ":"))
                    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
                    age = (now - ts).total_seconds()
                    status = "ok" if -60 <= age <= max_age_seconds else "stale"
        results[symbol] = DecisionQuote(code, exchange, status, price, prev_close,
                                        stamp, "tencent.qt.gtimg.cn/signal_hub", digest, session)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", required=True, help="comma-separated security codes")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    args = parser.parse_args()
    results = decision_quotes(args.codes.split(","), max_age_seconds=args.max_age_seconds)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"generated_at": datetime.now(CST).isoformat(), "quotes":
                   {key: asdict(value) for key, value in results.items()}},
                  handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
