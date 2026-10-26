#!/usr/bin/env python3
"""build_market_breadth.py -- accumulate a daily A-share market-breadth history.

Why this exists:
  data/market_now.json is rewritten on every run (the collectors refresh it
  every few minutes), so the daily limit-up / limit-down / turnover snapshot
  is never kept. Nothing can be looked back at or compared day over day.
  This script turns the single overwritten snapshot into an append-only,
  committed daily series.

Input (only source, no network here):
  <out-dir>/market_now.json  produced by scripts/build_market_now.py

Outputs:
  <out-dir>/market_breadth_history.json   last 120 trading days, JSON array
  <out-dir>/api/market_breadth.json       app bundle: latest + 60 + coverage

Design rules:
  * stdlib only -- the CI runner is small, numpy/pandas are not installed
  * idempotent -- the same date is updated in place, never appended twice;
    when the numbers did not change the output bytes are unchanged too
  * missing numbers stay null and are named in "notes"; nothing is invented
  * one input file, no new network collector

Usage:
  python3 scripts/build_market_breadth.py
  python3 scripts/build_market_breadth.py --date 2026-10-26
  python3 scripts/build_market_breadth.py --out-dir data --dry-run
"""
import argparse
import json
import os
import sys
import time
from datetime import date as _date, datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

HISTORY_LIMIT = 120          # days kept in market_breadth_history.json
API_HISTORY_LIMIT = 60       # days embedded in data/api/market_breadth.json
HISTORY_NAME = "market_breadth_history.json"
API_RELPATH = os.path.join("api", "market_breadth.json")

# Fixed wording required by the app contract. Kept verbatim (a data value,
# not a code comment).
CALIBER = ("涨停=主板10%/双创20%/ST5%/北交所30%；封板率=涨停/(涨停+炸板)")

# Caveat that travels with the data: market_now.json derives limit_up from a
# >= +10% histogram bucket sum, which only approximates the per-board rules
# above: it counts ChiNext/STAR names that are up 10-19% but not at their own
# limit, and 5% ST limit-ups never show up in that histogram at all.
CALIBER_CAVEAT = ("limit_up/limit_down are the eastmoney limit-up/limit-down pool "
                  "counts (pool_zt_count/pool_dt_count) and follow the per-board rules "
                  "in 'caliber'. limit_up_hist/limit_down_hist keep the older >=+10% / "
                  "<=-10% histogram-bucket sum for cross-checking only: that flat "
                  "threshold over-counts ChiNext/STAR names up 10-19% without a sealed "
                  "limit and never sees 5% ST limit-ups (2026-10-26: hist 77 vs pool 55; "
                  "2026-10-23: 171 vs 82). Caliber switched on 2026-10-27, so the series "
                  "steps down at that date - a caliber change, not a market move.")

# Fields compared to decide whether a same-date record really changed.
VALUE_KEYS = ("limit_up", "limit_down", "broken", "seal_rate_pct",
              "advance", "decline", "flat", "up_pct", "amount_yi",
              "limit_up_hist", "limit_down_hist")

# Intraday marker: a record carrying it is an unsettled snapshot.
INTRADAY_NOTE = "intraday_snapshot_not_settled"

# Backfilled-from-pool-history marker. Such a record must never overwrite a
# primary market_now record of the same day (see merge_record).
BACKFILL_SOURCE = "eastmoney_pool_history"
BACKFILL_NOTE = "backfilled_from_pool_history"
BACKFILL_GAP_NOTE = "no_breadth_no_amount_no_hist"

# Keys an index row may use to carry an amount (100M CNY). "turnover" is NOT in
# this list on purpose: in market_now.json it is a turnover *rate* (%), not an
# amount, so summing it would fabricate a meaningless number.
AMOUNT_KEYS = ("amount_yi", "amount", "turnover_yi", "amt_yi")


# --------------------------------------------------------------------------- #
# small value helpers
# --------------------------------------------------------------------------- #
def as_int(value):
    """Coerce to int, or None. bool/int/float accepted, garbage -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def as_float(value):
    """Coerce to float, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return None if out != out or out in (float("inf"), float("-inf")) else out
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def normalize_date(value):
    """Accept 20261026 / 2026-10-26 / ISO timestamp -> 'YYYY-MM-DD' or None."""
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        text = "%s-%s-%s" % (text[:4], text[4:6], text[6:])
    elif "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    try:
        return _date.fromisoformat(text).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# record extraction
# --------------------------------------------------------------------------- #
def resolve_amount_yi(market, notes):
    """Shanghai+Shenzhen amount traded in 100M CNY, or None.

    Priority:
      1. market.turnover.total_yi            -- authoritative, already summed
      2. sum(market.turnover.parts.values()) -- same number, different shape
      3. amount key on the sh000001 / sz399001 index rows (some producers
         attach it there); never the "turnover" rate field
    """
    turnover = market.get("turnover")
    if isinstance(turnover, dict):
        total = as_float(turnover.get("total_yi"))
        if total is not None and total > 0:
            return round(total, 1)
        parts = turnover.get("parts")
        if isinstance(parts, dict):
            values = [as_float(v) for v in parts.values()]
            values = [v for v in values if v is not None and v > 0]
            if values:
                notes.append("amount_from_parts_sum")
                return round(sum(values), 1)

    indices = market.get("indices")
    if isinstance(indices, list):
        total = 0.0
        found = False
        for row in indices:
            if not isinstance(row, dict):
                continue
            if row.get("code") not in ("sh000001", "sz399001"):
                continue
            for key in AMOUNT_KEYS:
                value = as_float(row.get(key))
                if value is not None and value > 0:
                    total += value
                    found = True
                    break
        if found:
            notes.append("amount_from_index_rows")
            return round(total, 1)
        if any(isinstance(r, dict) and r.get("turnover") is not None for r in indices):
            notes.append("amount_unavailable_indices_turnover_is_rate_pct")
            return None

    notes.append("amount_unavailable")
    return None


def build_record(market_now, date_override=None):
    """Turn one market_now.json dict into one breadth record (or None)."""
    notes = []

    market = market_now.get("market")
    if not isinstance(market, dict):
        market = {}
        notes.append("missing_market_block")

    recorded_at = datetime.now(CST).isoformat(timespec="seconds")

    is_live = market_now.get("is_live")
    session_state = market_now.get("session_state")
    if is_live is True or session_state == "trading":
        notes.append(INTRADAY_NOTE)

    # -- date ------------------------------------------------------------- #
    if date_override:
        day = normalize_date(date_override)
        if day is None:
            return None, "--date %r is not a YYYY-MM-DD date" % date_override
        notes.append("date_from_argument")
    else:
        day = normalize_date(market_now.get("date"))
        if day is None:
            day = normalize_date(market_now.get("ts"))
            if day is not None:
                notes.append("date_from_ts")
    if day is None:
        return None, "no usable date in market_now.json"

    # -- breadth counters -------------------------------------------------- #
    # 口径优先级（2026-10-26 修正）：
    #   1) 东财涨停/跌停池家数 pool_zt_count / pool_dt_count —— 按分板块涨跌幅规则统计，是真实口径；
    #   2) 兜底才用 ±10% 直方图桶和 (limit_up/limit_down) —— 平价阈值，会把双创 10~19% 未涨停算进来、
    #      又漏掉 ST 5% 涨停，实测同一天 77 vs 55。两者都留在记录里，谁也不能冒充谁。
    # 直方图对照口径（2026-10-27）：market_now.json 新格式把池口径写进 limit_up、直方图挪到
    # limit_up_hist；旧格式里 limit_up 本身就是直方图值，靠"是否存在 hist 键"区分，避免读串。
    if ("limit_up_hist" in market) or ("limit_down_hist" in market):
        limit_up_hist = as_int(market.get("limit_up_hist"))
        limit_down_hist = as_int(market.get("limit_down_hist"))
    else:
        limit_up_hist = as_int(market.get("limit_up"))
        limit_down_hist = as_int(market.get("limit_down"))
    limit_up = as_int(market.get("pool_zt_count"))
    limit_down = as_int(market.get("pool_dt_count"))
    if limit_up is not None:
        notes.append("limit_up_from_pool_zt_count")
    else:
        # 池口径缺失时不再拿直方图顶替（那会把 77 冒充 55）；宁可显示缺失，让上游修池数据
        notes.append("limit_up_missing_no_pool_count")
    if limit_down is not None:
        notes.append("limit_down_from_pool_dt_count")
    else:
        notes.append("limit_down_missing_no_pool_count")
    broken = as_int(market.get("broken"))
    advance = as_int(market.get("advance"))
    decline = as_int(market.get("decline"))
    flat = as_int(market.get("flat"))

    for name, value in (("limit_up", limit_up), ("limit_down", limit_down),
                        ("broken", broken), ("advance", advance),
                        ("decline", decline), ("flat", flat)):
        if value is None:
            notes.append("missing_" + name)

    # -- seal rate = limit_up / (limit_up + broken) ------------------------ #
    seal_rate = None
    if limit_up is not None and broken is not None and (limit_up + broken) > 0:
        seal_rate = round(limit_up / float(limit_up + broken) * 100.0, 1)
    else:
        fallback = as_float(market.get("zt_rate"))
        if fallback is not None:
            seal_rate = round(fallback, 1)
            notes.append("seal_rate_from_zt_rate")
        else:
            notes.append("missing_seal_rate_pct")

    # -- up ratio ---------------------------------------------------------- #
    up_pct = as_float(market.get("up_pct"))
    if up_pct is None:
        total = sum(v for v in (advance, decline, flat) if v is not None)
        if total > 0 and advance is not None:
            up_pct = round(advance / float(total) * 100.0, 1)
            notes.append("up_pct_computed")
        else:
            notes.append("missing_up_pct")

    amount_yi = resolve_amount_yi(market, notes)

    # -- suspicious equality: keep the numbers, shout about them ----------- #
    if limit_up is not None and limit_down is not None \
            and limit_up == limit_down and limit_up > 0:
        notes.append("suspect_same_updown")
        print("::warning::market_breadth %s: limit_up == limit_down == %d "
              "(suspect up/down caliber; value left untouched)"
              % (day, limit_up), file=sys.stderr)

    # -- histogram vs pool divergence (caliber cross-check, informational) -- #
    if limit_up_hist is not None and limit_up is not None and limit_up > 0:
        if abs(limit_up_hist - limit_up) / float(limit_up) > 0.10:
            notes.append("limit_up_vs_hist_divergence")

    record = {
        "date": day,
        "limit_up": limit_up,
        "limit_down": limit_down,
        # 平价阈值口径（±10% 直方图桶和），仅作对照证据，不作为对外主值
        "limit_up_hist": limit_up_hist,
        "limit_down_hist": limit_down_hist,
        "broken": broken,
        "seal_rate_pct": seal_rate,
        "advance": advance,
        "decline": decline,
        "flat": flat,
        "up_pct": up_pct,
        "amount_yi": amount_yi,
        "source": "market_now",
        "recorded_at": recorded_at,
        "notes": notes,
        "revisions": 0,
    }
    return record, None


# --------------------------------------------------------------------------- #
# backfill from eastmoney pool history
# --------------------------------------------------------------------------- #
def _fetch_pool(kind, date_str):
    """Delegate to collect_market_extras.fetch_pool (single source of the pool
    parsing rules: count comes from data.tc, because data.pool may be empty for
    historical days while tc still carries the real number)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from collect_market_extras import fetch_pool
    return fetch_pool(kind, date_str, retries=3)


def fetch_pool_counts(day):
    """ZT/DT/ZB counts for one historical day, or None when the pool API has
    nothing for it (window is roughly the last month and slides forward)."""
    compact = day.replace("-", "")
    counts = {}
    for kind, key in (("ZT", "limit_up"), ("DT", "limit_down"), ("ZB", "broken")):
        try:
            counts[key] = int(_fetch_pool(kind, compact).get("count") or 0)
        except Exception as exc:
            print("[market_breadth] pool %s %s failed: %s" % (kind, day, exc),
                  file=sys.stderr)
            return None
    if not any(counts.values()):
        return None          # outside the window, or a day with no pool data at all
    return counts


def build_backfill_record(day, counts):
    """History-only record. Breadth counters and amount are unknown for past
    days, so they stay None instead of being invented."""
    limit_up = counts.get("limit_up")
    broken = counts.get("broken")
    seal_rate = None
    if limit_up is not None and broken is not None and (limit_up + broken) > 0:
        seal_rate = round(limit_up / float(limit_up + broken) * 100.0, 1)
    return {
        "date": day,
        "limit_up": limit_up,
        "limit_down": counts.get("limit_down"),
        "broken": broken,
        "seal_rate_pct": seal_rate,
        "advance": None,
        "decline": None,
        "flat": None,
        "up_pct": None,
        "amount_yi": None,
        "limit_up_hist": None,
        "limit_down_hist": None,
        "source": BACKFILL_SOURCE,
        "recorded_at": datetime.now(CST).isoformat(timespec="seconds"),
        "notes": [BACKFILL_NOTE, BACKFILL_GAP_NOTE],
        "revisions": 0,
    }


def trading_days(start, end):
    """Weekdays between two dates. Public holidays are not modelled: the pool
    API answers tc=0 for them, which fetch_pool_counts turns into a skip."""
    try:
        first = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return []
    out = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def build_api_payload(items):
    """The api bundle. updated_at is the newest record's timestamp (not this
    run's) so an unchanged rerun stays byte-identical."""
    revisions_total = sum(as_int(r.get("revisions")) or 0 for r in items)
    return {
        "latest": items[-1] if items else None,
        "history": items[-API_HISTORY_LIMIT:],
        "coverage": coverage_of(items),
        "caliber": CALIBER,
        "caliber_caveat": CALIBER_CAVEAT,
        "updated_at": items[-1].get("recorded_at") if items else None,
        "revisions_total": revisions_total,
    }


# --------------------------------------------------------------------------- #
# history file handling
# --------------------------------------------------------------------------- #
def load_history(path):
    """Read the history array. Missing/broken file -> empty list."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        print("[market_breadth] history unreadable (%s); starting fresh" % exc,
              file=sys.stderr)
        return []
    if isinstance(data, dict):                       # tolerate {"items": [...]}
        data = data.get("items") or data.get("history") or []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and normalize_date(r.get("date"))]


def same_values(old, new):
    return all(old.get(k) == new.get(k) for k in VALUE_KEYS)


def merge_record(items, record):
    """Insert or update one record. Returns (items, action)."""
    day = record["date"]
    previous = next((r for r in items if normalize_date(r.get("date")) == day), None)

    if previous is not None:
        prev_source = previous.get("source") or ""
        if prev_source and prev_source != BACKFILL_SOURCE and record.get("source") == BACKFILL_SOURCE:
            # A backfilled row only carries limit counts; it must never overwrite
            # a primary market_now row that also has breadth/amount.
            return items, "skipped_backfill_over_primary"

    if previous is None:
        items.append(record)
        return items, "append"

    previous_settled = INTRADAY_NOTE not in (previous.get("notes") or [])
    if previous_settled and INTRADAY_NOTE in record["notes"] and not same_values(previous, record):
        # A settled record must not be dragged backwards by a later unsettled
        # snapshot of the same date (pre-open runs re-read the previous day).
        return items, "skipped_unsettled_overwrite"

    if same_values(previous, record):
        return items, "unchanged"

    record["revisions"] = as_int(previous.get("revisions")) or 0
    record["revisions"] += 1
    record["recorded_at"] = record["recorded_at"] or previous.get("recorded_at")
    items = [record if normalize_date(r.get("date")) == day else r for r in items]
    return items, "update"


def trim_and_sort(items, limit=HISTORY_LIMIT):
    items = sorted(items, key=lambda r: normalize_date(r.get("date")))
    if len(items) > limit:
        items = items[-limit:]
    for item in items:
        item["date"] = normalize_date(item.get("date"))
        item["revisions"] = as_int(item.get("revisions")) or 0
        if not isinstance(item.get("notes"), list):
            item["notes"] = []
    return items


def coverage_of(items):
    """days / first / last / missing (weekday gaps, holidays included)."""
    dates = [r["date"] for r in items]
    if not dates:
        return {"days": 0, "first": None, "last": None, "missing": [],
                "missing_method": "weekday-gap; A-share holidays also show up here"}
    known = set(dates)
    first, last = dates[0], dates[-1]
    missing = []
    cursor = _date.fromisoformat(first)
    stop = _date.fromisoformat(last)
    while cursor <= stop:
        if cursor.weekday() < 5 and cursor.isoformat() not in known:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return {
        "days": len(dates),
        "first": first,
        "last": last,
        "missing": missing,
        "missing_method": "weekday-gap; A-share holidays also show up here",
    }


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)

    parser = argparse.ArgumentParser(
        description="Accumulate daily market breadth from market_now.json")
    parser.add_argument("--date", default=None,
                        help="record under this date (YYYY-MM-DD) instead of "
                             "the date carried by market_now.json")
    parser.add_argument("--out-dir", default="data",
                        help="directory holding market_now.json and receiving "
                             "the outputs (default: data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change, write nothing")
    parser.add_argument("--backfill-from", default=None,
                        help="backfill pool counts from this day (YYYY-MM-DD); "
                             "run it right after the pool window opens, the window "
                             "slides forward and older days are lost for good")
    parser.add_argument("--backfill-to", default=None,
                        help="last day to backfill (default: today)")
    args = parser.parse_args(argv)

    out_dir = args.out_dir
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(repo_root, out_dir)
    source_path = os.path.join(out_dir, "market_now.json")
    history_path = os.path.join(out_dir, HISTORY_NAME)
    api_path = os.path.join(out_dir, API_RELPATH)

    # -- one-off backfill from pool history (does not need market_now.json) -- #
    if args.backfill_from:
        end = args.backfill_to or datetime.now(CST).date().isoformat()
        days = trading_days(args.backfill_from, end)
        if not days:
            print("[market_breadth] bad backfill range %s..%s" % (args.backfill_from, end),
                  file=sys.stderr, flush=True)
            return 2
        items = load_history(history_path)
        before = len(items)
        added = updated = skipped = nodata = primary_kept = 0
        for day in days:
            counts = fetch_pool_counts(day)
            if counts is None:
                nodata += 1
                print("[market_breadth] backfill %s -> no pool data (skip)" % day, flush=True)
                continue
            record = build_backfill_record(day, counts)
            items, action = merge_record(items, record)
            if action == "append":
                added += 1
            elif action == "update":
                updated += 1
            elif action == "skipped_backfill_over_primary":
                primary_kept += 1
            else:
                skipped += 1
            print("[market_breadth] backfill %s zt=%s dt=%s zb=%s -> %s"
                  % (day, counts["limit_up"], counts["limit_down"], counts["broken"], action),
                  flush=True)
            time.sleep(0.8)   # pool API: stay ~1 QPS
        items = trim_and_sort(items)
        print("[market_breadth] backfill done: appended=%d updated=%d unchanged=%d "
              "no_data=%d primary_kept=%d days=%d->%d"
              % (added, updated, skipped, nodata, primary_kept, before, len(items)), flush=True)
        if args.dry_run:
            print("[market_breadth] dry-run: nothing written", flush=True)
            return 0
        write_json(history_path, items)
        write_json(api_path, build_api_payload(items))
        print("[market_breadth] wrote %s and %s" % (history_path, api_path), flush=True)
        return 0

    if not os.path.exists(source_path):
        print("[market_breadth] %s not found -> no-op (nothing to record)"
              % source_path, flush=True)
        return 0

    try:
        with open(source_path, encoding="utf-8") as handle:
            market_now = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print("[market_breadth] cannot read %s (%s) -> no-op"
              % (source_path, exc), file=sys.stderr, flush=True)
        return 0
    if not isinstance(market_now, dict):
        print("[market_breadth] unexpected market_now shape -> no-op",
              file=sys.stderr, flush=True)
        return 0

    record, error = build_record(market_now, args.date)
    if record is None:
        print("[market_breadth] %s -> no-op" % error, file=sys.stderr, flush=True)
        return 0

    items = load_history(history_path)
    before = len(items)
    items, action = merge_record(items, record)
    items = trim_and_sort(items)

    api_payload = build_api_payload(items)

    print("[market_breadth] date=%s limit_up=%s limit_down=%s broken=%s "
          "seal_rate=%s advance=%s decline=%s amount_yi=%s action=%s "
          "days=%d->%d notes=%s"
          % (record["date"], record["limit_up"], record["limit_down"],
             record["broken"], record["seal_rate_pct"], record["advance"],
             record["decline"], record["amount_yi"], action, before,
             len(items), ",".join(record["notes"]) or "-"), flush=True)

    if args.dry_run:
        print("[market_breadth] dry-run: %s and %s NOT written"
              % (history_path, api_path), flush=True)
        return 0

    # The history file is the array itself (contract: "a JSON array of the
    # most recent 120 trading days"); updated_at/revisions_total live inside
    # each record and in the api bundle.
    write_json(history_path, items)
    write_json(api_path, api_payload)
    print("[market_breadth] wrote %s (%d records) and %s"
          % (history_path, len(items), api_path), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
