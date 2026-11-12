#!/usr/bin/env python3
"""Collect the latest eight completed A-share reporting periods from public Eastmoney.

Cloud output: data/fin/<prefixed-code>.json. EPS and actual DPS are bound to
one code and one reporting period; missing dividends remain explicitly null.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")
FIN_DIR = os.path.join(DATA, "fin")
API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
STALE_HOURS = 48


def get_universe() -> list[str]:
    path = os.path.join(DATA, "lp_syms.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            codes = json.load(stream)
        if codes:
            a_shares = [code for code in codes
                        if not isinstance(code, str) or not re.fullmatch(r"(?:sz200|sh900)\d{3}", code)]
            if len(a_shares) != len(codes):
                print(f"excluded {len(codes) - len(a_shares)} B-share symbols outside A-share scope")
            return a_shares
    bulk = os.path.join(DATA, "bulk")
    return sorted({name[8:-4] for name in os.listdir(bulk)
                   if name.startswith("day_qfq_") and name.endswith(".csv")}) if os.path.isdir(bulk) else []


def normalize_code(code: str) -> tuple[str, str]:
    if not isinstance(code, str) or not re.fullmatch(r"(?:(?:sh|sz|bj))?\d{6}", code):
        raise ValueError("invalid stock code")
    bare = code[-6:]
    prefix = "sh" if bare.startswith("6") else "sz" if bare.startswith(("0", "3")) else "bj"
    if len(code) == 8 and code[:2] != prefix:
        raise ValueError("stock prefix conflicts with identity")
    return prefix + bare, bare


def source_date(value: object, now: datetime) -> str:
    if not isinstance(value, str):
        raise ValueError("missing source date")
    parsed = datetime.fromisoformat(value)
    parsed = parsed.replace(tzinfo=CST) if parsed.tzinfo is None else parsed.astimezone(CST)
    if parsed > now:
        raise ValueError("future source date")
    return parsed.date().isoformat()


def finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def report_start(now: datetime) -> str:
    # The current quarter is incomplete; include the preceding eight quarter ends.
    year, quarter = divmod(now.year * 4 + (now.month - 1) // 3 - 8, 4)
    month = (quarter + 1) * 3
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def fetch_pages(name: str, date_field: str, bare: list[str], now: datetime,
                retries: int, delay: float, page_size: int) -> tuple[list[dict], str]:
    quoted = ",".join(f'"{code}"' for code in bare)
    sort_fields = [date_field, "SECURITY_CODE"]
    if name == "RPT_SHAREBONUS_DET":
        sort_fields += ["EX_DIVIDEND_DATE", "PLAN_NOTICE_DATE", "PUBLISH_DATE"]
    params = {"reportName": name, "columns": "ALL",
              "filter": f'(SECURITY_CODE in ({quoted}))({date_field}>=\'{report_start(now)}\')',
              "pageSize": str(page_size), "sortColumns": ",".join(sort_fields),
              "sortTypes": ",".join("-1" for _ in sort_fields)}
    rows = []
    page_keys = set()
    previous_order = None
    expected = None
    page = 1
    source_url = ""
    while True:
        params["pageNumber"] = str(page)
        url = API + "?" + urllib.parse.urlencode(params)
        if not source_url:
            source_url = url
        payload = None
        for attempt in range(retries):
            time.sleep(delay)
            try:
                request = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                if attempt + 1 == retries:
                    raise
                time.sleep(2 ** attempt)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError(f"{name}: unsuccessful response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"{name}: missing result")
        count, pages, data = result.get("count"), result.get("pages"), result.get("data")
        if (type(count) is not int or type(pages) is not int or count < 0 or pages < 0
                or not isinstance(data, list)):
            raise ValueError(f"{name}: incomplete pagination metadata")
        if pages != (count + page_size - 1) // page_size:
            raise ValueError(f"{name}: count/pages mismatch")
        if expected is None:
            expected = (count, pages)
        if expected != (count, pages):
            raise ValueError(f"{name}: pagination changed during collection")
        wanted = min(page_size, max(0, count - (page - 1) * page_size))
        if len(data) != wanted or not all(isinstance(row, dict) for row in data):
            raise ValueError(f"{name}: missing or malformed page")
        current_keys = set()
        for row in data:
            order = tuple(str(row.get(field) or "") for field in sort_fields)
            event_key = order + ((str(row.get("ASSIGN_PROGRESS") or ""),)
                                 if name == "RPT_SHAREBONUS_DET" else ())
            if event_key in page_keys:
                raise ValueError(f"{name}: overlapping pages")
            if previous_order is not None and order > previous_order:
                raise ValueError(f"{name}: unstable pagination order")
            previous_order = order
            current_keys.add(event_key)
        page_keys.update(current_keys)
        rows.extend(data)
        if page >= pages:
            break
        page += 1
    if len(rows) != expected[0]:
        raise ValueError(f"{name}: truncated result")
    return rows, source_url


def stale_marker(timestamp: object, now: datetime) -> dict:
    try:
        parsed = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        age = (now - parsed).total_seconds() / 3600 if parsed and parsed.tzinfo else None
    except ValueError:
        age = None
    return {"age_hours": round(age, 3) if age is not None else None,
            "threshold_hours": STALE_HOURS,
            "is_stale": age is None or age < 0 or age > STALE_HOURS}


def fetch_fin_batch(codes: list[str], retries: int = 3, *, now: datetime | None = None,
                    sleep: float = 0.7, page_size: int = 500) -> dict[str, list[dict]]:
    if not codes:
        return {}
    if not 1 <= len(codes) <= 100 or not 1 <= page_size <= 500 or retries < 1 or sleep < 0.7:
        raise ValueError("invalid batch, page size, retries or request interval")
    now = now or datetime.now(CST)
    identities = dict(normalize_code(code)[::-1] for code in codes)
    bare = list(identities)
    financials, fin_url = fetch_pages("RPT_LICO_FN_CPD", "REPORTDATE", bare, now, retries, sleep, page_size)
    dividends, div_url = fetch_pages("RPT_SHAREBONUS_DET", "REPORT_DATE", bare, now, retries, sleep, page_size)
    events: dict[tuple[str, str], dict[str, dict]] = {}
    incomplete: dict[tuple[str, str], str] = {}
    for row in dividends:
        code = row.get("SECURITY_CODE")
        if code not in identities:
            raise ValueError("dividend identity outside requested whitelist")
        period = source_date(row.get("REPORT_DATE"), now)
        key = (code, period)
        if row.get("ASSIGN_PROGRESS") != "实施分配":
            continue
        try:
            ex_date = source_date(row.get("EX_DIVIDEND_DATE"), now)
            notice = source_date(row.get("PLAN_NOTICE_DATE"), now)
            published = source_date(row.get("PUBLISH_DATE"), now)
            if min(ex_date, notice, published) < period or ex_date < max(notice, published):
                raise ValueError("dividend chronology contradicts report period")
            amount = finite_number(row.get("PRETAX_BONUS_RMB"))
            if amount is None or amount < 0:
                raise ValueError("missing actual dividend amount")
        except ValueError:
            incomplete[key] = "invalid_or_future_implemented_dividend"
            continue
        event = {"ex_date": ex_date, "plan_notice_date": notice, "publish_date": published,
                 "pretax_per_10": amount}
        by_date = events.setdefault(key, {})
        if ex_date in by_date and by_date[ex_date] != event:
            incomplete[key] = "dividend_event_conflict"
        by_date[ex_date] = event
    output: dict[str, list[dict]] = {}
    seen: dict[tuple[str, str], dict] = {}
    for row in financials:
        code = row.get("SECURITY_CODE")
        if code not in identities:
            raise ValueError("financial identity outside requested whitelist")
        period = source_date(row.get("REPORTDATE"), now)
        if period[5:] not in ("03-31", "06-30", "09-30", "12-31") or period < report_start(now):
            raise ValueError("report outside declared eight-period window")
        announce = source_date(row.get("NOTICE_DATE"), now)
        update = source_date(row.get("UPDATE_DATE"), now)
        if announce < period or update < period:
            raise ValueError("source publication precedes report period")
        eps = finite_number(row.get("BASIC_EPS"))
        if eps is None:
            raise ValueError("missing or invalid EPS")
        key = (code, period)
        if key in seen:
            if seen[key] != row:
                raise ValueError("conflicting duplicate financial report")
            continue
        seen[key] = row
        actual = list(events.get(key, {}).values())
        zero = row.get("ASSIGNDSCRPT") == "不分配不转增"
        reason = incomplete.get(key)
        dps = None
        if zero and actual:
            reason = "explicit_zero_actual_dividend_conflict"
        elif not reason and actual:
            dps = sum(event["pretax_per_10"] for event in actual) / 10
            if not math.isfinite(dps):
                dps, reason = None, "invalid_dividend_sum"
        elif not reason and zero:
            dps = 0.0
        elif not reason:
            reason = "no_implemented_dividend_for_report_period"
        ts = now.isoformat(timespec="seconds")
        def evidence(source, field, unit, value, missing_reason=None):
            return {"source": source, "field": field, "report_date": period, "symbol": code,
                    "unit": unit, "confidence": "verified_public" if value is not None else "insufficient",
                    "snapshot_ts": ts, "reason": missing_reason}
        output.setdefault(identities[code], []).append({
            "symbol": code, "report_date": period, "announce_date": announce, "update_date": update,
            "eps": eps, "dps": dps, "dps_reason": reason, "dividend_events": actual,
            "deduct_eps": finite_number(row.get("DEDUCT_BASIC_EPS")),
            "revenue": finite_number(row.get("TOTAL_OPERATE_INCOME")),
            "net_profit": finite_number(row.get("PARENT_NETPROFIT")),
            "ts": ts, "collectedAt": ts, "stale": stale_marker(ts, now),
            "data_evidence": {
                "eps": evidence(fin_url, "BASIC_EPS", "CNY/share", eps),
                "dps": evidence(fin_url if zero and not actual else div_url,
                                "ASSIGNDSCRPT" if zero and not actual else "PRETAX_BONUS_RMB/10",
                                "CNY/share", dps, reason)}})
    return output


def save_stock(fin_dir: str, code: str, rows: list[dict], *, now: datetime | None = None):
    if not rows:
        return
    now = now or datetime.now(CST)
    code, bare = normalize_code(code)
    path = os.path.join(fin_dir, f"{code}.json")
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            existing = json.load(stream).get("reports", [])
    # Replacement by period captures revisions without renewing untouched report timestamps.
    merged = {}
    for row in existing:
        if row.get("symbol") not in (None, bare):
            raise ValueError("existing report identity conflicts with destination")
        # Legacy rows used an Eastmoney datetime; normalize the period, never their collection time.
        period = datetime.fromisoformat(row["report_date"]).date().isoformat()
        merged[period] = dict(row, report_date=period)
    for row in rows:
        if row.get("symbol") != bare:
            raise ValueError("cannot save a report with mismatched identity")
        merged[row["report_date"]] = dict(row)
    reports = sorted(merged.values(), key=lambda row: row["report_date"])
    for row in reports:
        row["stale"] = stale_marker(row.get("ts"), now)
    ts = now.isoformat(timespec="seconds")
    output = {"code": code, "symbol": bare, "source": API, "n": len(reports), "ts": ts,
              "collectedAt": ts, "stale": stale_marker(ts, now),
              "coverage": {"mode": "recent_eight_completed_report_periods", "from": report_start(now),
                           "retained_older_reports": True}, "reports": reports}
    os.makedirs(fin_dir, exist_ok=True)
    # The temporary file lives beside the destination so replacement is atomic.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=fin_dir, delete=False) as stream:
        temporary = stream.name
        try:
            json.dump(output, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, ValueError, TypeError):
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    except OSError:
        os.unlink(temporary)
        raise


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--codes", help="Comma-separated prefixed or bare codes for cloud smoke runs")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.7)
    args = parser.parse_args(argv)
    if args.limit < 0 or not 1 <= args.batch <= 100 or not math.isfinite(args.sleep) or args.sleep < 0.7:
        parser.error("limit >= 0, batch 1..100 and sleep >= 0.7 required")
    try:
        requested = args.codes.split(",") if args.codes is not None else get_universe()
        universe = list(dict.fromkeys(normalize_code(code.strip())[0] for code in requested))
    except (OSError, ValueError, TypeError) as error:
        print(f"universe failed: {error}", file=sys.stderr)
        return 1
    if args.limit:
        universe = universe[:args.limit]
    if not universe:
        print("empty universe; no collection performed", file=sys.stderr)
        return 1
    written = 0
    failed = False
    for offset in range(0, len(universe), args.batch):
        batch = universe[offset:offset + args.batch]
        try:
            result = fetch_fin_batch(batch, sleep=args.sleep)
            missing = sorted(code for code in batch if not result.get(code))
            unexpected = sorted(set(result) - set(batch))
            if missing or unexpected:
                raise ValueError(f"unverified batch coverage: missing={missing}, unexpected={unexpected}")
            for code, rows in result.items():
                if rows:
                    save_stock(FIN_DIR, code, rows)
                    written += 1
        except (OSError, ValueError, TypeError) as error:
            failed = True
            print(f"batch {offset // args.batch + 1} failed: {error}", file=sys.stderr)
    print(f"collected {written}/{len(universe)} stocks; recent eight completed report periods")
    return 1 if failed or written == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
