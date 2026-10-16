#!/usr/bin/env python3
"""Review same-day premarket and midday picks against one close quote batch."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import signal_hub as H


def load(name):
    path = os.path.join(H.DATA_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return None


def normalise_date(value):
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def normalise_code(code):
    text = str(code or "").strip().lower()
    market = text[:2] if text[:2] in ("sh", "sz", "bj") else ""
    bare = text[2:] if market else text
    if bare.isdigit() and len(bare) < 6:
        bare = bare.zfill(6)
    if not market and bare:
        market = ("sh" if bare.startswith(("6", "5", "9")) else
                  "bj" if bare.startswith(("4", "8")) else "sz")
    return bare, market + bare


def parse_local_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CST)
    return stamp.astimezone(CST)


def valid_stage(payload, stage, trading_date):
    if not isinstance(payload, dict) or normalise_date(payload.get("date")) != trading_date:
        return False, "missing or wrong-date artifact"
    if payload.get("session") != stage:
        return False, f"session marker is not {stage}"
    if stage == "midday" and payload.get("status") not in ("ok", "partial"):
        return False, "midday artifact status is not ok"
    stamp = parse_local_time(payload.get("ts") or payload.get("generated_at"))
    if stamp is None or stamp.date().isoformat() != trading_date:
        return False, "missing or invalid timestamp"
    if stage == "premarket" and (stamp.hour, stamp.minute) >= (9, 15):
        return False, "premarket was generated at or after 09:15"
    if stage == "midday" and not ((11, 30) <= (stamp.hour, stamp.minute) < (13, 0)):
        return False, "midday artifact is outside 11:30-13:00"
    return True, "ok"


def close_window(now):
    if now.weekday() >= 5:
        return False, "market closed on weekend"
    if (now.hour, now.minute) < (15, 0):
        return False, "before the 15:00 close; close result is not available"
    return True, "at or after the 15:00 close"


def premarket_rows(payload):
    rows = []
    for tier in ("A_buy", "B_watch"):
        for row in ((payload or {}).get("tiers") or {}).get(tier) or []:
            if isinstance(row, dict) and row.get("code"):
                rows.append({**row, "bucket": tier})
    return rows


def midday_rows(payload):
    rows = []
    afternoon = (payload or {}).get("afternoon") or {}
    for bucket in ("keep", "watch", "drop", "no_quote", "new_strong"):
        for row in afternoon.get(bucket) or []:
            if isinstance(row, dict) and row.get("code"):
                rows.append({**row, "bucket": bucket})
    return rows


def quote_is_close(quote, trading_date):
    text = str((quote or {}).get("time") or "")
    if len(text) < 14 or not text[:14].isdigit() or text[:8] != trading_date.replace("-", ""):
        return False
    return (14, 55) <= (int(text[8:10]), int(text[10:12])) <= (15, 5)


def quote_lookup(raw):
    out = {}
    for key, quote in (raw or {}).items():
        if not isinstance(quote, dict):
            continue
        bare, prefixed = normalise_code(key)
        out[prefixed] = quote
    return out


def batch_quotes(codes, should_fetch, trading_date):
    requested = []
    for code in codes:
        _, prefixed = normalise_code(code)
        if prefixed and prefixed not in requested:
            requested.append(prefixed)
    raw = H.quotes(requested) if should_fetch and requested else {}
    lookup = quote_lookup(raw)
    result = {}
    for code in requested:
        bare, prefixed = normalise_code(code)
        quote = lookup.get(prefixed)
        result[prefixed] = (quote if quote and quote.get("price") and quote.get("chg_pct") is not None
                        and quote_is_close(quote, trading_date) else None)
    return result, requested


def enrich(rows, quotes):
    output = []
    for row in rows:
        bare, prefixed = normalise_code(row.get("code"))
        quote = quotes.get(prefixed)
        if not quote:
            output.append({**row, "code": bare, "status": "no_quote", "quote_time": None,
                           "quote_source": "signal_hub.quotes", "close": None, "chg_pct": None})
            continue
        output.append({**row, "code": bare, "status": "ok", "name": quote.get("name") or row.get("name"),
                       "close": quote.get("price"), "chg_pct": quote.get("chg_pct"),
                       "turnover": quote.get("turnover"), "high": quote.get("high"),
                       "low": quote.get("low"), "quote_time": quote.get("time"),
                       "quote_source": "signal_hub.quotes"})
    return output


def summarise(rows):
    requested = len(rows)
    quoted = [row for row in rows if row.get("status") == "ok" and row.get("chg_pct") is not None]
    missing = [row.get("code") for row in rows if row.get("status") != "ok" or row.get("chg_pct") is None]
    if not requested:
        return {"status": "insufficient", "reason": "stage has no valid candidates", "n_requested": 0,
                "n_quoted": 0, "n_missing": 0, "missing_codes": []}
    if not quoted:
        return {"status": "insufficient", "reason": "no close quotes for stage candidates", "n_requested": requested,
                "n_quoted": 0, "n_missing": len(missing), "missing_codes": missing}
    returns = [float(row["chg_pct"]) for row in quoted]
    ordered = sorted(returns)
    positive = sum(value > 0 for value in returns)
    return {
        "status": "ok" if len(quoted) == requested else "partial",
        "n_requested": requested,
        "n_quoted": len(quoted),
        "n_missing": len(missing),
        "missing_codes": missing,
        "n_up": positive,
        "hit_rate_pct": round(positive / len(returns) * 100, 1),
        "avg_ret_pct": round(sum(returns) / len(returns), 2),
        "median_ret_pct": round(ordered[(len(ordered) - 1) // 2], 2),
        "best": max(returns),
        "worst": min(returns),
        "avg_turnover_pct": round(sum(row.get("turnover") or 0 for row in quoted) / len(quoted), 2),
    }


def stage_view(rows):
    buckets = {}
    for row in rows:
        buckets.setdefault(row.get("bucket") or "unclassified", []).append(row)
    return {bucket: {"summary": summarise(items), "rows": sorted(items, key=lambda item: -(item.get("chg_pct") or -999))}
            for bucket, items in buckets.items()}


def legacy_summary(summary):
    """Keep archive_review's old field names without changing stage semantics."""
    return {**summary,
            "n": summary.get("n_quoted", 0),
            "n_up": summary.get("n_up", 0)}


def compatibility_fields(premarket_rows, midday_rows_value, premarket_summary, midday_summary):
    """Expose old archive keys as derived compatibility data, never old inputs."""
    pm_groups = {}
    for row in premarket_rows:
        pm_groups.setdefault(row.get("bucket") or "unclassified", []).append(row)
    return {
        "morning": {"summary": legacy_summary(premarket_summary),
                    "by_group": {group: legacy_summary(summarise(rows))
                                 for group, rows in pm_groups.items()},
                    "rows": premarket_rows},
        "afternoon": {"summary": legacy_summary(midday_summary),
                      "rows": midday_rows_value},
        "market": {"status": "insufficient", "reason": "market summary is not a principal stage input",
                   "phase": None, "phase_desc": None, "advance": None, "decline": None},
        "tradable_plans": {"summary": {"status": "insufficient",
                                         "reason": "entry-plan stage is not a principal input",
                                         "n": 0, "n_quoted": 0},
                           "n_stop_hit": None, "n_target_hit": None, "rows": []},
        "score_effectiveness": None,
        "historical_winrate": {},
        "rejected_review": {"n": sum(1 for row in midday_rows_value if row.get("bucket") == "drop"),
                            "rows": [row for row in midday_rows_value if row.get("bucket") == "drop"]},
    }


def self_test():
    assert normalise_date("20261016") == "2026-10-16"
    good = {"session": "premarket", "date": "2026-10-16", "ts": "2026-10-16T09:10:00+08:00"}
    assert valid_stage(good, "premarket", "2026-10-16")[0]
    assert not valid_stage({**good, "ts": "2026-10-16T09:16:00+08:00"}, "premarket", "2026-10-16")[0]
    insufficient = summarise([{"code": "600001", "status": "no_quote"}])
    assert insufficient["status"] == "insufficient"
    partial = summarise([{"code": "600001", "status": "ok", "chg_pct": 2.0}, {"code": "000001", "status": "no_quote"}])
    assert partial["status"] == "partial" and partial["hit_rate_pct"] == 100.0
    assert not quote_is_close({"time": "20261015150000"}, "2026-10-16")
    assert quote_is_close({"time": "20261016150000"}, "2026-10-16")
    assert not valid_stage({"session": "midday", "date": "2026-10-16", "ts": "2026-10-16T12:01:00+08:00", "status": "insufficient"}, "midday", "2026-10-16")[0]
    print("close_review self-test: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(H.DATA_DIR, "close_review.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    now = datetime.now(CST)
    trading_date = now.date().isoformat()
    premarket = load("premarket_picks.json")
    midday = load("midday_picks.json")
    pm_valid, pm_reason = valid_stage(premarket, "premarket", trading_date)
    md_valid, md_reason = valid_stage(midday, "midday", trading_date)
    pm_rows = premarket_rows(premarket) if pm_valid else []
    md_rows = midday_rows(midday) if md_valid else []
    union_codes = [row.get("code") for row in pm_rows + md_rows]
    after_close, close_reason = close_window(now)
    quotes, requested = batch_quotes(union_codes, after_close or args.dry_run, trading_date)
    pm_enriched = enrich(pm_rows, quotes)
    md_enriched = enrich(md_rows, quotes)
    pm_summary = summarise(pm_enriched)
    md_summary = summarise([row for row in md_enriched if row.get("bucket") == "keep"])
    if not pm_valid:
        pm_summary = {"status": "insufficient", "reason": pm_reason, "n_requested": 0, "n_quoted": 0,
                      "n_missing": 0, "missing_codes": []}
    if not md_valid:
        md_summary = {"status": "insufficient", "reason": md_reason, "n_requested": 0, "n_quoted": 0,
                      "n_missing": 0, "missing_codes": []}
    if not after_close and not args.dry_run:
        reason = close_reason
        pm_summary = {"status": "insufficient", "reason": reason, "n_requested": len(pm_rows),
                      "n_quoted": 0, "n_missing": len(pm_rows), "missing_codes": [row.get("code") for row in pm_rows]}
        md_summary = {"status": "insufficient", "reason": reason, "n_requested": len(md_rows),
                      "n_quoted": 0, "n_missing": len(md_rows), "missing_codes": [row.get("code") for row in md_rows]}
    previous = load("close_review.json") or {}
    archive_review = {
        "freshness": "archive_only; legacy fields were not used as principal inputs",
        "legacy_input_files": {name: load(name) is not None for name in (
            "morning_review.json", "afternoon_picks.json", "entry_plans.json")},
        "legacy_close_fields": {key: previous.get(key) for key in (
            "morning", "afternoon", "tradable_plans", "score_effectiveness", "rejected_review") if key in previous},
    }
    output = {
        "generated_at": now.isoformat(timespec="seconds"),
        "engine": "close_review v2 (same-day premarket + midday)",
        "date": trading_date,
        "status": ("partial" if (pm_summary["status"] != "ok" or md_summary["status"] != "ok")
                   else "ok") if (after_close or args.dry_run) and (pm_valid or md_valid)
                   else "insufficient",
        "market_window": {"status": "ok" if after_close else "insufficient", "reason": close_reason},
        "inputs": {
            "premarket": {"status": "ok" if pm_valid else "insufficient", "reason": pm_reason},
            "midday": {"status": "ok" if md_valid else "insufficient", "reason": md_reason},
        },
        "quote_request": {"n_requested": len(requested), "codes": requested, "source": "signal_hub.quotes",
                          "batch_calls": 1 if requested and (after_close or args.dry_run) else 0},
        "coverage": {"n_requested": len(pm_enriched) + len(md_enriched),
                     "n_quoted": sum(row.get("status") == "ok" for row in pm_enriched + md_enriched),
                     "n_missing": sum(row.get("status") != "ok" for row in pm_enriched + md_enriched),
                     "missing_codes": [row.get("code") for row in pm_enriched + md_enriched if row.get("status") != "ok"]},
        "premarket": {"summary": pm_summary, "rows": sorted(pm_enriched, key=lambda row: -(row.get("chg_pct") or -999))},
        "midday": {"summary": md_summary, "summary_scope": "keep only", "by_bucket": stage_view(md_enriched),
                   "rows": sorted(md_enriched, key=lambda row: -(row.get("chg_pct") or -999))},
        "archive_review": archive_review,
        "meta": {"principal_inputs": ["premarket_picks.json", "midday_picks.json"],
                 "missing_quote_policy": "status=no_quote; missing rows never contribute to a full-coverage claim",
                 "unsupported_stage_policy": "status=insufficient; no invented win rate"},
    }
    output.update(compatibility_fields(pm_enriched, md_enriched, pm_summary, md_summary))
    if args.dry_run:
        print(json.dumps(output, ensure_ascii=False, indent=1)[:10000])
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=1)
    print(f"[close] status={output['status']} premarket={pm_summary['status']} midday={md_summary['status']}")
    print(f"[close] quote coverage={output['coverage']['n_quoted']}/{output['coverage']['n_requested']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
