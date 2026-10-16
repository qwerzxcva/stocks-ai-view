#!/usr/bin/env python3
"""Produce the 11:30-13:00 midday revision from a valid premarket call."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signal_hub as H
DATA_DIR = H.DATA_DIR


def load(name):
    path = os.path.join(DATA_DIR, name)
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


def validate_premarket(payload, trading_date, now=None):
    """Return a status dict; never treat an invalid artifact as current."""
    now = now or datetime.now(CST)
    if not isinstance(payload, dict):
        return {"status": "insufficient", "reason": "premarket_picks.json missing or invalid"}
    if payload.get("session") != "premarket":
        return {"status": "insufficient", "reason": "premarket session marker missing"}
    if normalise_date(payload.get("date")) != trading_date:
        return {"status": "insufficient", "reason": "premarket artifact is not for today", "artifact_date": payload.get("date")}
    stamp = parse_local_time(payload.get("ts"))
    if stamp is None:
        return {"status": "insufficient", "reason": "premarket timestamp missing or invalid"}
    if stamp.date().isoformat() != trading_date or (stamp.hour, stamp.minute) >= (9, 15):
        return {"status": "insufficient", "reason": "premarket was not generated before 09:15", "generated_at": stamp.isoformat()}
    if stamp > now:
        return {"status": "insufficient", "reason": "premarket timestamp is in the future", "generated_at": stamp.isoformat()}
    return {"status": "ok", "generated_at": stamp.isoformat(), "date": trading_date}


def midday_window(now):
    if now.weekday() >= 5:
        return False, "market closed on weekend"
    current = (now.hour, now.minute)
    if current < (11, 30) or current >= (13, 0):
        return False, "outside the 11:30-13:00 midday window"
    return True, "within the 11:30-13:00 midday window"


def candidate_rows(premarket):
    rows = []
    tiers = (premarket or {}).get("tiers") or {}
    for tier in ("A_buy", "B_watch"):
        for item in tiers.get(tier) or []:
            if isinstance(item, dict) and item.get("code"):
                rows.append({**item, "tier": tier})
    return rows


def quote_is_current(quote, trading_date, now):
    text = str((quote or {}).get("time") or "")
    if len(text) < 14 or not text[:14].isdigit() or text[:8] != trading_date.replace("-", ""):
        return False
    quote_hm = (int(text[8:10]), int(text[10:12]))
    return (9, 30) <= quote_hm <= (11, 30) and quote_hm <= (now.hour, now.minute)


def quote_lookup(quotes):
    out = {}
    for key, quote in (quotes or {}).items():
        if not isinstance(quote, dict):
            continue
        bare, prefixed = normalise_code(key)
        out[prefixed] = quote
    return out


def fetch_candidate_quotes(rows, should_fetch, trading_date, now):
    """Call signal_hub.quotes exactly once for the complete A/B candidate set."""
    codes = []
    for row in rows:
        bare, prefixed = normalise_code(row.get("code"))
        if bare and prefixed not in codes:
            codes.append(prefixed)
    raw = H.quotes(codes) if should_fetch and codes else {}
    lookup = quote_lookup(raw)
    result = {}
    for row in rows:
        bare, prefixed = normalise_code(row.get("code"))
        quote = lookup.get(prefixed)
        if not quote or not quote.get("price") or not quote_is_current(quote, trading_date, now):
            result[prefixed] = {"status": "no_quote", "quote_source": "signal_hub.quotes",
                            "quote_time": (quote or {}).get("time")}
            continue
        result[prefixed] = {
            "status": "ok",
            "price": quote.get("price"),
            "prev_close": quote.get("prev_close"),
            "chg_pct": quote.get("chg_pct"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "turnover": quote.get("turnover"),
            "quote_time": quote.get("time"),
            "quote_source": "signal_hub.quotes",
        }
    return result, codes


def verdict(row, quote):
    if quote.get("status") != "ok":
        return "no_quote"
    price = quote.get("price")
    entry = (row.get("entry_zone") or [None, None])[0]
    target = row.get("target")
    stop = row.get("stop")
    chg = quote.get("chg_pct")
    if chg is not None and chg >= 9.8:
        return "limit_up_unbuyable"
    if target is not None and price >= target:
        return "target_hit"
    if stop is not None and price <= stop:
        return "stopped_out"
    if entry is not None and price > entry * 1.02:
        return "in_profit"
    if entry is not None and price < entry * 0.98:
        return "underwater"
    return "flat"


def verify_premarket(premarket, quotes, trading_date):
    details = []
    for row in candidate_rows(premarket):
        bare, prefixed = normalise_code(row.get("code"))
        quote = quotes.get(prefixed) or {"status": "no_quote", "quote_source": "signal_hub.quotes", "quote_time": None}
        details.append({
            "code": bare,
            "name": row.get("name"),
            "tier": row.get("tier"),
            "score": row.get("score"),
            "entry_zone": row.get("entry_zone"),
            "stop": row.get("stop"),
            "target": row.get("target"),
            "status": quote.get("status", "no_quote"),
            "price": quote.get("price"),
            "chg_pct": quote.get("chg_pct"),
            "quote_time": quote.get("quote_time"),
            "quote_source": quote.get("quote_source", "signal_hub.quotes"),
            "am_price": quote.get("price"), "am_chg_pct": quote.get("chg_pct"),
            "limit_up": verdict(row, quote) == "limit_up_unbuyable",
            "verdict": verdict(row, quote),
        })
    counts = {}
    for item in details:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    quoted = sum(1 for item in details if item["status"] == "ok")
    return {
        "status": "ok" if details and quoted == len(details) else ("partial" if quoted else "insufficient"),
        "date": trading_date,
        "n_checked": len(details),
        "n_quoted": quoted,
        "n_no_quote": len(details) - quoted,
        "verdicts": counts,
        "detail": details,
        "gate_was": (premarket.get("gate") or {}).get("state"),
        "global_score_was": (premarket.get("global") or {}).get("score"),
    }


def market_review(now, trading_date):
    """Use only a same-date intraday snapshot; never use post-close market_now."""
    snapshot = load("market_now.json") or {}
    if not isinstance(snapshot, dict) or normalise_date(snapshot.get("date") or snapshot.get("date_belongs_to")) != trading_date:
        return {"status": "insufficient", "reason": "no same-date market snapshot"}
    if snapshot.get("session_state") == "post_close" or snapshot.get("is_live") is False:
        return {"status": "insufficient", "reason": "market_now is post-close or explicitly non-live"}
    stamp = parse_local_time(snapshot.get("ts") or snapshot.get("collected_at"))
    if (stamp is None or stamp.date().isoformat() != trading_date or stamp > now or
            (stamp.hour, stamp.minute) > (11, 30)):
        return {"status": "insufficient", "reason": "market snapshot is not a morning snapshot"}
    market = snapshot.get("market") or {}
    return {"status": "ok", "source": "market_now.json", "ts": stamp.isoformat(), "market": {
        "advance": market.get("advance"), "decline": market.get("decline"),
        "limit_up": market.get("limit_up"), "limit_down": market.get("limit_down"),
        "broken": market.get("broken"), "seal_rate": market.get("seal_rate"),
        "turnover_yi": (market.get("turnover") or {}).get("total_yi"),
    }}


def afternoon_list(premarket, verification):
    by_code = {normalise_code(item["code"])[1]: item for item in verification.get("detail") or []}
    keep, drop, watch, no_quote = [], [], [], []
    for row in candidate_rows(premarket):
        bare, prefixed = normalise_code(row.get("code"))
        check = by_code.get(prefixed) or {"status": "no_quote", "verdict": "no_quote"}
        item = dict(row)
        item.update({"status": check.get("status", "no_quote"), "price": check.get("price"),
                     "am_price": check.get("price"), "chg_pct": check.get("chg_pct"),
                     "quote_time": check.get("quote_time"),
                     "quote_source": check.get("quote_source"), "am_verdict": check.get("verdict")})
        if check.get("status") != "ok":
            item["why_moved"] = "no quote; excluded from keep list"
            no_quote.append(item)
        elif check.get("verdict") == "limit_up_unbuyable":
            item["why_moved"] = "morning limit-up; not buyable in the afternoon"
            watch.append(item)
        elif check.get("verdict") == "stopped_out":
            item["why_moved"] = "morning price breached the planned stop"
            drop.append(item)
        elif check.get("verdict") == "in_profit" and (check.get("chg_pct") or 0) > 6:
            item["why_moved"] = "already up more than 6%; wait for a pullback"
            watch.append(item)
        else:
            keep.append(item)
    return {"keep": keep[:12], "drop": drop[:8], "watch": watch[:8], "no_quote": no_quote,
            "new_strong": [], "new_strong_status": "insufficient: no non-stale intraday source used"}


def self_test():
    fixed = datetime(2026, 10, 16, 11, 40, tzinfo=CST)
    good = {"session": "premarket", "date": "2026-10-16", "ts": "2026-10-16T09:10:00+08:00"}
    late = {**good, "ts": "2026-10-16T09:15:00+08:00"}
    assert validate_premarket(good, "2026-10-16", fixed)["status"] == "ok"
    assert validate_premarket(late, "2026-10-16", fixed)["status"] == "insufficient"
    assert not quote_is_current({"time": "20261015113000"}, "2026-10-16", fixed)
    assert not midday_window(datetime(2026, 10, 16, 15, 1, tzinfo=CST))[0]
    assert normalise_code("sh600001") == ("600001", "sh600001")
    assert normalise_code("600001")[0] == "600001"
    rows = [{"code": "600001"}, {"code": "sz000001"}]
    original = H
    class Fake:
        @staticmethod
        def quotes(codes):
            assert codes == ["sh600001", "sz000001"]
            return {"sh600001": {"price": 10, "chg_pct": 1, "time": "20261016113000"}}
    globals()["H"] = Fake
    try:
        result, _ = fetch_candidate_quotes(rows, True, "2026-10-16", fixed)
        assert result["sh600001"]["status"] == "ok"
        assert result["sz000001"]["status"] == "no_quote"
    finally:
        globals()["H"] = original
    print("midday_picks self-test: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(DATA_DIR, "midday_picks.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    now = datetime.now(CST)
    trading_date = now.date().isoformat()
    premarket = load("premarket_picks.json")
    validation = validate_premarket(premarket, trading_date, now)
    in_window, window_reason = midday_window(now)
    rows = candidate_rows(premarket) if validation["status"] == "ok" else []
    quotes, requested_codes = fetch_candidate_quotes(rows, in_window or args.dry_run, trading_date, now)
    verification = verify_premarket(premarket or {}, quotes, trading_date)
    if validation["status"] != "ok":
        verification = {"status": "insufficient", "date": trading_date, "n_checked": 0, "n_quoted": 0,
                        "n_no_quote": 0, "verdicts": {}, "detail": [],
                        "reason": validation["reason"]}
    elif not in_window and not args.dry_run:
        verification["status"] = "insufficient"
        verification["reason"] = window_reason
    output = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "midday_picks v2 (validated premarket + one batch quote)",
        "date": trading_date,
        "session": "midday",
        "status": (verification["status"] if validation["status"] == "ok" and
                   (in_window or args.dry_run) else "insufficient"),
        "market_window": {"status": "ok" if in_window else "insufficient", "reason": window_reason},
        "premarket_validation": validation,
        "morning_review": market_review(now, trading_date),
        "premarket_verification": verification,
        "afternoon": (afternoon_list(premarket, verification)
                      if validation["status"] == "ok" and (in_window or args.dry_run)
                      else {"keep": [], "drop": [], "watch": [], "no_quote": [], "new_strong": [],
                            "status": "insufficient", "reason": validation.get("reason") or window_reason}),
        "quote_request": {"n_requested": len(requested_codes), "codes": requested_codes,
                          "source": "signal_hub.quotes", "batch_calls": 1 if requested_codes and (in_window or args.dry_run) else 0},
        "gate": (premarket or {}).get("gate") if validation["status"] == "ok" else None,
        "meta": {"flow": "same-date premarket before 09:15 -> one A/B quote batch -> midday revision",
                 "no_quote_policy": "no_quote is excluded from keep; no_data is never emitted",
                 "market_now_policy": "post-close or stale snapshots are not used as morning data"},
    }
    if args.dry_run:
        print(json.dumps(output, ensure_ascii=False, indent=1)[:8000])
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=1)
    print(f"[midday] status={output['status']} premarket={validation['status']} window={window_reason}")
    print(f"[midday] quote coverage={verification.get('n_quoted', 0)}/{verification.get('n_checked', 0)} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
