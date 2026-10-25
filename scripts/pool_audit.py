#!/usr/bin/env python3
"""审计多池真实字段、覆盖、时间戳与交易时段新鲜度。"""
import argparse
import json
import os
from datetime import datetime, time, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CST = timezone(timedelta(hours=8))

# field 是页面和产物中实际存在的业务字段；count_field 指定真正的股票/条目池。
POOLS = {
    "risk": ("decision.json", "avoid", "daily"),
    "cycle": ("cycle_pool_v2.json", "stocks", "weekly"),
    "recommend": ("ai_pick_v4.json", "picks", "daily"),
    "hot": ("hot_news.json", "items", "daily"),
    "low": ("lowpool.json", "strong", "daily"),
    "limitup": ("ban_pick.json", "picks", "daily"),
    "ticks": ("auction_engine.json", "stocks", "intraday"),
    "blocktrade": ("enrich.json", "blocktrade", "intraday"),
    "lhb": ("enrich.json", "lhb", "intraday"),
    "auction": ("auction_engine.json", "stocks", "intraday"),
    "sector": ("sector_pool.json", "tier2", "daily"),
    "money_divergence": ("flow_divergence.json", "buy_side", "intraday"),
}

# Calendar-day thresholds are intentionally looser outside the A-share session.
THRESHOLDS = {"intraday": 3.0, "daily": 30.0, "weekly": 8 * 24.0}


def load(path):
    try:
        with open(os.path.join(DATA, path), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return None


def parse_ts(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=CST) if parsed.tzinfo is None else parsed.astimezone(CST)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:len(fmt)], fmt).replace(tzinfo=CST)
        except ValueError:
            pass
    return None


def timestamp_for(name, data):
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    # cycle_pool_v2's authoritative timestamp is meta.ts; all other known
    # current artifacts use top-level ts/date or their explicit sub-meta date.
    for source in (meta, data):
        for key in ("ts", "timestamp", "generated", "rebuilt_at", "date", "data_date"):
            if isinstance(source.get(key), str):
                return source[key]
    if name == "enrich.json" and isinstance(data.get("enrich_meta"), dict):
        dates = [data["enrich_meta"].get(key) for key in ("lhb_date", "blocktrade_date")]
        dates = [item for item in dates if isinstance(item, str)]
        return max(dates) if dates else None
    return None


def trading_age_hours(start, end):
    """Only count elapsed weekday market-session time for intraday outputs."""
    if end <= start:
        return (end - start).total_seconds() / 3600
    elapsed = 0.0
    day = start.astimezone(CST).date()
    last = end.astimezone(CST).date()
    while day <= last:
        if day.weekday() < 5:
            for opening, closing in ((time(9, 15), time(11, 30)),
                                     (time(13, 0), time(15, 5))):
                session_start = datetime.combine(day, opening, CST)
                session_end = datetime.combine(day, closing, CST)
                elapsed += max(0.0, (min(end, session_end) - max(start, session_start)).total_seconds())
        day += timedelta(days=1)
    return elapsed / 3600


def threshold_for(kind, now):
    return THRESHOLDS[kind]


def record_count(value):
    return len(value) if isinstance(value, (list, dict)) else 0


def status_for(data, value, timestamp, kind, now):
    if data is None:
        return "missing"
    if value is None:
        return "missing_field"
    parsed = parse_ts(timestamp)
    if parsed is None:
        return "no_timestamp"
    age = (now - parsed).total_seconds() / 3600
    if age < -0.05:
        return "future_timestamp"
    effective_age = trading_age_hours(parsed, now) if kind == "intraday" else age
    if effective_age > threshold_for(kind, now):
        return "stale"
    return "empty" if record_count(value) == 0 else "fresh"


def source_for(data):
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    return data.get("source") or meta.get("source") or data.get("engine")


def audit_pool(name, spec, now):
    filename, field, kind = spec
    data = load(filename)
    value = data.get(field) if isinstance(data, dict) else None
    timestamp = timestamp_for(filename, data)
    parsed = parse_ts(timestamp)
    age = round((now - parsed).total_seconds() / 3600, 2) if parsed else None
    status = status_for(data, value, timestamp, kind, now)
    if name == "cycle" and isinstance(data, dict):
        stocks_meta = data.get("stocks_meta") or {}
        if stocks_meta.get("status") == "insufficient_data" or stocks_meta.get("n") == 0:
            status = "insufficient_data"
    effective_age = trading_age_hours(parsed, now) if parsed and kind == "intraday" else age
    return {
        "file": filename,
        "field": field,
        "frequency": kind,
        "count": record_count(value),
        "ts": timestamp,
        "age_hours": age,
        "effective_age_hours": round(effective_age, 2) if effective_age is not None else None,
        "threshold_hours": threshold_for(kind, now),
        "status": status,
        "empty": value is not None and record_count(value) == 0,
        "stale": status == "stale",
        "source": source_for(data),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(DATA, "pool_audit.json"))
    args = parser.parse_args()
    now = datetime.now(CST)
    pools = {name: audit_pool(name, spec, now) for name, spec in POOLS.items()}
    stale = [name for name, row in pools.items() if row["status"] == "stale"]
    empty = [name for name, row in pools.items() if row["empty"]]
    insufficient = [name for name, row in pools.items() if row["status"] in ("missing", "missing_field", "no_timestamp", "future_timestamp", "insufficient_data")]
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "pool_audit.py",
        "status": "insufficient" if insufficient else ("partial" if stale or empty else "fresh"),
        "stale_pools": stale,
        "empty_pools": empty,
        "insufficient_pools": insufficient,
        "pools": pools,
        "meta": {
            "purpose": "按实际业务字段审计多池覆盖、空池、时间戳和时效",
            "session_rule": "intraday 仅累计工作日 09:15-11:30、13:00-15:05 的交易时段时间，阈值3小时；daily/weekly按日历年龄",
            "field_corrections": {"cycle": "cycle_pool_v2.stocks + cycle_pool_v2.stocks_meta.n + cycle_pool_v2.meta.ts", "risk": "decision.avoid", "hot": "hot_news.items"},
        },
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"[pool_audit] {payload['status']} stale={stale} empty={empty} insufficient={insufficient}")
    return 0


if __name__ == "__main__":
    main()
