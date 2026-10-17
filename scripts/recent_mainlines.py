#!/usr/bin/env python3
"""Build recent mainline statistics from paired trading-day records.

History rows are close-review archives. The current premarket snapshot is
accepted only when its timestamp is on the same date and before 09:15 CST;
an evening regeneration remains visible as rejected metadata, never as a
premarket prediction.
"""
import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
PREMARKET_CUTOFF = (9, 15)
WINDOW_SIZES = (7, 30, 365)
MIN_SAMPLE_DAYS = 3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load_json(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, TypeError, json.JSONDecodeError):
        return {}


def parse_date(value):
    if value is None:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_timestamp(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)
        return parsed.astimezone(CST)
    except (TypeError, ValueError):
        return None


def read_history():
    """Read one close-review row per date, retaining the latest timestamp."""
    path = os.path.join(DATA, "review_history.jsonl")
    if not os.path.exists(path):
        return {}
    by_date = {}
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = parse_date(row.get("date"))
            if day is None:
                continue
            stamp = parse_timestamp(row.get("generated_at"))
            previous = by_date.get(day)
            previous_stamp = parse_timestamp(previous.get("generated_at")) if previous else None
            if previous is None or (stamp and (previous_stamp is None or stamp > previous_stamp)):
                if previous and valid_premarket(previous.get("premarket")) and not valid_premarket(row.get("premarket")):
                    row["premarket"] = previous["premarket"]
                by_date[day] = row
            elif valid_premarket(row.get("premarket")) and not valid_premarket(previous.get("premarket")):
                previous["premarket"] = row["premarket"]
    return by_date


def read_trading_dates(extra_dates):
    """Use observed index sessions as the calendar; fall back to source dates."""
    dates = set()
    index_data = load_json("index_kline.json")
    indices = index_data.get("indices") if isinstance(index_data, dict) else {}
    if isinstance(indices, dict):
        sessions = indices.get("sh000001") or next(iter(indices.values()), [])
        if isinstance(sessions, list):
            dates.update(day for day in (parse_date(x.get("date")) for x in sessions)
                         if day is not None)
    dates.update(x for x in extra_dates if x is not None)
    return sorted(day for day in dates if day <= datetime.now(CST).date())


def source_theme_items(row):
    """Extract explicitly named themes, never recommendation group names."""
    items = []
    for field in ("mainlines", "themes"):
        value = row.get(field) if isinstance(row, dict) else None
        if isinstance(value, dict):
            value = [{"name": name} for name in value]
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append({"name": item.strip()})
            elif isinstance(item, dict) and str(item.get("name", "")).strip():
                items.append(item)
    return items


def theme_names(row):
    return list(dict.fromkeys(item["name"] for item in source_theme_items(row)))


def theme_stats(rows):
    counts = Counter()
    directions = defaultdict(Counter)
    stocks = defaultdict(Counter)
    for row in rows:
        day_names = set()
        for item in source_theme_items(row):
            name = item["name"]
            day_names.add(name)
            direction = item.get("direction")
            if direction:
                directions[name][str(direction)] += 1
            codes = item.get("codes", item.get("stocks", []))
            if isinstance(codes, list):
                for code in codes[:30]:
                    if isinstance(code, dict):
                        code = code.get("code")
                    if code is not None:
                        stocks[name][str(code)] += 1
        for name in day_names:
            counts[name] += 1
    denominator = len(rows)
    ranked = []
    for name, days_present in counts.most_common():
        direction = directions[name].most_common(1)[0][0] if directions[name] else "neutral"
        ranked.append({
            "name": name,
            "days_present": days_present,
            "presence_pct": round(days_present / denominator * 100, 1) if denominator else 0.0,
            "direction": direction,
            "stable": days_present >= MIN_SAMPLE_DAYS,
            "representative_stocks": [x for x, _ in stocks[name].most_common(10)],
            "evidence": "review_history.jsonl explicit mainlines/themes",
        })
    return ranked


def aggregate_metrics(rows):
    n_buyable = sum((row.get("n_buyable") or 0) for row in rows)
    hit_pairs = [(row.get("hit_rate_buyable_pct"), row.get("n_buyable") or 0)
                 for row in rows if row.get("hit_rate_buyable_pct") is not None]
    avg_pairs = [(row.get("avg_ret_buyable_pct"), row.get("n_buyable") or 0)
                 for row in rows if row.get("avg_ret_buyable_pct") is not None]
    metrics = {"n_buyable": n_buyable}
    if hit_pairs:
        hit_denominator = sum(weight for _, weight in hit_pairs)
        metrics["hit_rate_buyable_pct"] = round(
            sum(value * weight for value, weight in hit_pairs) / hit_denominator, 1
        ) if hit_denominator else None
    else:
        metrics["hit_rate_buyable_pct"] = None
    if avg_pairs:
        avg_denominator = sum(weight for _, weight in avg_pairs)
        metrics["avg_ret_buyable_pct"] = round(
            sum(value * weight for value, weight in avg_pairs) / avg_denominator, 2
        ) if avg_denominator else None
    else:
        metrics["avg_ret_buyable_pct"] = None
    return metrics


def valid_premarket(snapshot):
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("session") != "premarket":
        return False
    day = parse_date(snapshot.get("date"))
    stamp = parse_timestamp(snapshot.get("ts") or snapshot.get("generated_at"))
    return bool(day and stamp and stamp.date() == day and
                (stamp.hour, stamp.minute) < PREMARKET_CUTOFF)


def daily_row(day, history_row, snapshot):
    close_time = history_row.get("generated_at") if history_row else None
    archived = history_row.get("premarket") if history_row else None
    if valid_premarket(archived) and parse_date(archived.get("date")) == day:
        candidate = archived
        candidate_source = "data/review_history.jsonl"
    else:
        candidate = snapshot
        candidate_source = "data/premarket_picks.json"
    snapshot_day = parse_date(candidate.get("date")) if isinstance(candidate, dict) else None
    snapshot_time = (candidate.get("ts") or candidate.get("generated_at")) \
        if isinstance(candidate, dict) else None
    same_snapshot_day = snapshot_day == day
    premarket_ok = same_snapshot_day and valid_premarket(candidate)
    close = ({
        "date": day.isoformat(),
        "source": "data/review_history.jsonl",
        "time": close_time,
        "summary": {
            "n_picks": history_row.get("n_picks"),
            "hit_rate_all_pct": history_row.get("hit_rate_all_pct"),
            "avg_ret_all_pct": history_row.get("avg_ret_all_pct"),
            "n_buyable": history_row.get("n_buyable"),
            "hit_rate_buyable_pct": history_row.get("hit_rate_buyable_pct"),
            "avg_ret_buyable_pct": history_row.get("avg_ret_buyable_pct"),
        },
        "market_phase": history_row.get("market_phase"),
    } if history_row else None)
    premarket = ({
        "date": day.isoformat(),
        "source": candidate_source,
        "time": snapshot_time,
        "session": "premarket",
        "gate": candidate.get("gate"),
        "tiers": candidate.get("tiers"),
        "outlook": candidate.get("outlook"),
    } if premarket_ok else None)
    if not same_snapshot_day:
        premarket_status = "missing"
    elif premarket_ok:
        premarket_status = "available"
    else:
        premarket_status = "rejected_not_before_09:15"
    return {
        "date": day.isoformat(),
        "premarket": premarket,
        "close": close,
        "source": {
            "premarket": candidate_source if same_snapshot_day else None,
            "close": "data/review_history.jsonl" if history_row else None,
        },
        "actual_time": {
            "premarket": snapshot_time if same_snapshot_day else None,
            "close": close_time,
        },
        "premarket_status": premarket_status,
        "mainlines": theme_names(history_row) if history_row else [],
    }


def build_window(requested_days, trading_dates, history_by_date, premarket):
    selected_dates = set(trading_dates[-requested_days:])
    rows = [history_by_date[day] for day in sorted(selected_dates) if day in history_by_date]
    paired = [history_by_date[day] for day in sorted(selected_dates)
              if day in history_by_date and daily_row(day, history_by_date[day], premarket)["premarket"]]
    return {
        "requested_days": requested_days,
        "observed_days": len(rows),
        "paired_days": len(paired),
        "status": "ok" if len(paired) >= requested_days else "sample_insufficient",
        "metrics": aggregate_metrics(rows),
        "paired_metrics": aggregate_metrics(paired) if paired else None,
        "themes": theme_stats(rows),
    }


def market_summary(index_doc):
    """Observed Shanghai Composite close and volume, not a stock-pick review."""
    rows = ((index_doc or {}).get("indices") or {}).get("sh000001") or []
    out = {}
    previous = None
    for row in rows:
        close = row.get("close")
        if close is None:
            continue
        change = None if previous in (None, 0) else round((close / previous - 1) * 100, 2)
        out[row.get("date")] = {
            "index": "sh000001",
            "close": close,
            "change_pct": change,
            "volume": row.get("vol"),
            "source": "data/index_kline.json",
            "scope": "大A指数总结，不含个股推荐验证",
        }
        previous = close
    ordered = list(out)
    for index, day in enumerate(ordered):
        window = ordered[max(0, index - 19):index + 1]
        if len(window) < 20:
            continue
        ma20 = sum(out[item]["close"] for item in window) / 20
        close = out[day]["close"]
        out[day]["ma20"] = round(ma20, 2)
        out[day]["bias_ma20_pct"] = round((close / ma20 - 1) * 100, 2)
        change = out[day].get("change_pct")
        if change is None:
            label = "数据不足"
        elif close < ma20 and change < 0:
            label = "指数跌破MA20且当日走弱"
        elif close < ma20:
            label = "指数仍在MA20下方"
        elif change > 0:
            label = "指数站上MA20且当日走强"
        else:
            label = "指数站上MA20但当日回落"
        out[day]["market_mainline"] = label
    return out


def build_payload(args):
    now = datetime.now(CST)
    history_by_date = read_history()
    current = load_json("mainline.json")
    premarket = load_json("premarket_picks.json")
    market_by_date = market_summary(load_json("index_kline.json"))
    premarket_day = parse_date(premarket.get("date")) if valid_premarket(premarket) else None
    trading_dates = read_trading_dates(set(history_by_date) | {premarket_day})
    trading_dates = [day for day in trading_dates if day is not None]

    legacy_dates = set(trading_dates[-args.days:])
    legacy_rows = [history_by_date[day] for day in sorted(legacy_dates) if day in history_by_date]
    ranked = theme_stats(legacy_rows)
    daily = []
    for day in sorted(legacy_dates):
        row = daily_row(day, history_by_date.get(day), premarket)
        row["market_summary"] = market_by_date.get(day.isoformat())
        daily.append(row)
    market_rows = [row for row in daily if row.get("market_summary")]
    changes = [row["market_summary"]["change_pct"] for row in market_rows
               if row["market_summary"].get("change_pct") is not None]
    observed_days = len(legacy_rows)
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "recent_mainlines.py",
        "window_days": args.days,
        "history_days_available": len(history_by_date),
        "status": "ok" if sum(bool(row["premarket"] and row["close"]) for row in daily) >= args.days else "sample_insufficient",
        "mainlines": ranked[:30],
        "daily": daily,
        "current": current.get("mainlines", []) if isinstance(current, dict) else [],
        "market_window": {
            "index": "sh000001",
            "days": len(market_rows),
            "up_days": sum(value > 0 for value in changes),
            "down_days": sum(value < 0 for value in changes),
            "avg_change_pct": round(sum(changes) / len(changes), 3) if changes else None,
            "cumulative_change_pct": round(sum(changes), 2) if changes else None,
            "source": "data/index_kline.json",
            "scope": "大A指数表现；不是选股推荐的胜率",
        },
        "windows": {
            str(size): build_window(size, trading_dates, history_by_date, premarket)
            for size in WINDOW_SIZES
        },
        "meta": {
            "purpose": "按交易日配对盘前预测与盘后总结，统计稳定主线",
            "minimum_history": MIN_SAMPLE_DAYS,
            "caveat": "未凑齐请求交易日标注 sample_insufficient；只统计显式 mainlines/themes，不把 by_group 当主题",
            "premarket_cutoff": "09:15 CST",
            "trading_calendar_source": "data/index_kline.json observed sessions; archived close and valid premarket dates supplement missing sessions",
            "metrics_scope": "metrics use archived close reviews; paired_metrics and status use only verified same-day premarket+close pairs",
        },
    }
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(DATA, "recent_mainlines.json"))
    args = ap.parse_args()
    payload = build_payload(args)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[recent_mainlines] history={payload['history_days_available']} status={payload['status']} themes={len(payload['mainlines'])} -> {args.out}")


if __name__ == "__main__":
    main()
