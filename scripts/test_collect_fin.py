"""Public Eastmoney response fixtures; no network or repository data writes."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import collect_fin as fin

NOW = datetime(2026, 11, 12, 12, tzinfo=fin.CST)


def financial(**changes):
    row = {"SECURITY_CODE": "600519", "REPORTDATE": "2026-09-30 00:00:00",
           "NOTICE_DATE": "2026-10-30 00:00:00", "UPDATE_DATE": "2026-10-31 00:00:00",
           "BASIC_EPS": 3.2, "ASSIGNDSCRPT": None}
    return dict(row, **changes)


def dividend(**changes):
    row = {"SECURITY_CODE": "600519", "REPORT_DATE": "2026-09-30 00:00:00",
           "ASSIGN_PROGRESS": "实施分配", "PRETAX_BONUS_RMB": 10,
           "EX_DIVIDEND_DATE": "2026-11-01 00:00:00",
           "PLAN_NOTICE_DATE": "2026-10-30 00:00:00", "PUBLISH_DATE": "2026-10-31 00:00:00"}
    return dict(row, **changes)


class FakeHTTP:
    def __init__(self, financial_rows=None, dividends=None, broken=None):
        self.rows = {"RPT_LICO_FN_CPD": financial_rows or [], "RPT_SHAREBONUS_DET": dividends or []}
        self.broken = broken
        self.calls = []

    def __call__(self, request, timeout):
        query = parse_qs(urlparse(request.full_url).query)
        name = query["reportName"][0]
        page = int(query["pageNumber"][0])
        size = int(query["pageSize"][0])
        self.calls.append((name, page, query))
        fields = query["sortColumns"][0].split(",")
        rows = sorted(self.rows[name], key=lambda row: tuple(str(row.get(field) or "") for field in fields),
                      reverse=True)
        payload = {"success": True, "result": {"count": len(rows),
                   "pages": (len(rows) + size - 1) // size,
                   "data": rows[(page - 1) * size:page * size]}}
        if self.broken:
            self.broken(payload, name, page)
        return io.BytesIO(json.dumps(payload).encode())


class CollectFinTest(unittest.TestCase):
    def fetch(self, rows=None, dividends=None, broken=None, page_size=500):
        fake = FakeHTTP(rows or [financial()], dividends, broken)
        with patch.object(fin.urllib.request, "urlopen", fake), patch.object(fin.time, "sleep"):
            result = fin.fetch_fin_batch(["sh600519"], retries=1, now=NOW, page_size=page_size)
        return result, fake

    def test_latest_eps_missing_dps_is_not_backfilled(self):
        result, fake = self.fetch([financial(), financial(REPORTDATE="2025-12-31 00:00:00")],
                                  [dividend(REPORT_DATE="2025-12-31 00:00:00")])
        rows = result["sh600519"]
        latest = max(rows, key=lambda row: row["report_date"])
        self.assertEqual(latest["eps"], 3.2)
        self.assertIsNone(latest["dps"])
        self.assertEqual(latest["symbol"], "600519")
        self.assertEqual(latest["ts"], NOW.isoformat(timespec="seconds"))
        self.assertIn("datacenter-web.eastmoney.com", latest["data_evidence"]["eps"]["source"])
        self.assertIn("REPORTDATE", fake.calls[0][2]["filter"][0])

    def test_complete_pagination_for_both_reports(self):
        rows = [financial(), financial(REPORTDATE="2026-06-30 00:00:00")]
        bonuses = [dividend(), dividend(EX_DIVIDEND_DATE="2026-11-02 00:00:00")]
        result, fake = self.fetch(rows, bonuses, page_size=1)
        latest = max(result["sh600519"], key=lambda row: row["report_date"])
        self.assertEqual(latest["dps"], 2)
        self.assertEqual(len(fake.calls), 4)

    def test_incomplete_or_changed_pagination_rejected(self):
        for field in ("data", "count", "pages"):
            def broken(payload, name, page):
                if name == "RPT_LICO_FN_CPD":
                    payload["result"].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.fetch(broken=broken)
        def missing(payload, name, page):
            if name == "RPT_LICO_FN_CPD" and page == 2:
                payload["result"]["data"] = []
        with self.assertRaises(ValueError):
            self.fetch([financial(), financial(REPORTDATE="2026-06-30")], broken=missing, page_size=1)

    def test_invalid_identity_dates_and_eps(self):
        changes = [{"SECURITY_CODE": "000001"}, {"REPORTDATE": "2027-03-31"},
                   {"NOTICE_DATE": "bad"}, {"UPDATE_DATE": "2027-01-01"},
                   {"BASIC_EPS": None}, {"BASIC_EPS": True}, {"BASIC_EPS": float("inf")}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.fetch([financial(**change)])

    def test_explicit_zero_and_conflict(self):
        result, _ = self.fetch([financial(ASSIGNDSCRPT="不分配不转增")])
        self.assertEqual(result["sh600519"][0]["dps"], 0)
        result, _ = self.fetch([financial(ASSIGNDSCRPT="不分配不转增")], [dividend()])
        self.assertIsNone(result["sh600519"][0]["dps"])
        self.assertIn("conflict", result["sh600519"][0]["dps_reason"])

    def test_preliminary_future_crossperiod_and_null_not_actual(self):
        for change in ({"ASSIGN_PROGRESS": "预披露"}, {"EX_DIVIDEND_DATE": "2026-12-01"},
                       {"REPORT_DATE": "2026-06-30"}, {"PRETAX_BONUS_RMB": None},
                       {"PRETAX_BONUS_RMB": True}):
            with self.subTest(change=change):
                result, _ = self.fetch(dividends=[dividend(**change)])
                self.assertIsNone(result["sh600519"][0]["dps"])

    def test_duplicate_events_dedupe_and_conflicts_reject(self):
        result, _ = self.fetch(dividends=[dividend(), dividend()])
        self.assertEqual(result["sh600519"][0]["dps"], 1)
        result, _ = self.fetch(dividends=[dividend(), dividend(PRETAX_BONUS_RMB=20)])
        self.assertIsNone(result["sh600519"][0]["dps"])
        self.assertIn("conflict", result["sh600519"][0]["dps_reason"])
        with self.assertRaises(ValueError):
            self.fetch([financial(), financial(BASIC_EPS=4)])

    def test_overlapping_pages_cannot_claim_complete_collection(self):
        for name in ("RPT_LICO_FN_CPD", "RPT_SHAREBONUS_DET"):
            def overlap(payload, report_name, page):
                if report_name == name and page == 2:
                    payload["result"]["data"] = [financial() if name == "RPT_LICO_FN_CPD"
                                                   else dividend(EX_DIVIDEND_DATE="2026-11-02")]
            with self.subTest(report=name), self.assertRaises(ValueError):
                self.fetch([financial(), financial(REPORTDATE="2026-06-30")],
                           [dividend(), dividend(EX_DIVIDEND_DATE="2026-11-02")],
                           broken=overlap, page_size=1)

    def test_dividend_chronology_conflicts_stay_missing(self):
        for change in ({"EX_DIVIDEND_DATE": "2026-08-01"},
                       {"PLAN_NOTICE_DATE": "2026-09-01"},
                       {"PUBLISH_DATE": "2026-11-02"}):
            with self.subTest(change=change):
                result, _ = self.fetch(dividends=[dividend(**change)])
                self.assertIsNone(result["sh600519"][0]["dps"])
                self.assertIsNotNone(result["sh600519"][0]["dps_reason"])

    def test_partial_universe_is_not_a_successful_collection(self):
        result, _ = self.fetch()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fin, "FIN_DIR", directory), \
                patch.object(fin, "fetch_fin_batch", return_value=result):
            self.assertEqual(fin.main(["--codes", "sh600519,sz000001"]), 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_dividend_page_failure_cannot_sum_partial(self):
        def broken(payload, name, page):
            if name == "RPT_SHAREBONUS_DET" and page == 2:
                payload["result"]["data"] = []
        with self.assertRaises(ValueError):
            self.fetch(dividends=[dividend(), dividend(EX_DIVIDEND_DATE="2026-11-02")],
                       broken=broken, page_size=1)

    def test_revision_preserves_old_collection_time_and_empty_file(self):
        result, _ = self.fetch()
        row = result["sh600519"][0]
        with tempfile.TemporaryDirectory() as directory:
            old = dict(row, report_date="2026-06-30", ts="2026-07-30T12:00:00+08:00")
            fin.save_stock(directory, "sh600519", [old, row], now=NOW)
            revised = dict(row, eps=4)
            fin.save_stock(directory, "sh600519", [revised], now=NOW)
            path = Path(directory) / "sh600519.json"
            document = json.loads(path.read_text())
            self.assertEqual(document["symbol"], "600519")
            self.assertEqual(document["reports"][0]["ts"], old["ts"])
            self.assertEqual(document["reports"][-1]["eps"], 4)
            before = path.read_bytes()
            fin.save_stock(directory, "sh600519", [], now=NOW)
            self.assertEqual(before, path.read_bytes())

    def test_legacy_datetime_period_is_revised_without_duplicates(self):
        result, _ = self.fetch()
        row = result["sh600519"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sh600519.json"
            path.write_text(json.dumps({"code": "sh600519", "reports": [
                {"report_date": "2026-09-30 00:00:00", "eps": 1},
                {"report_date": "2026-06-30 00:00:00", "eps": 2}]}))
            fin.save_stock(directory, "sh600519", [row], now=NOW)
            reports = json.loads(path.read_text())["reports"]
            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[-1]["eps"], 3.2)
            self.assertNotIn("ts", reports[0])
            self.assertTrue(reports[0]["stale"]["is_stale"])

    def test_zero_universe_and_limit(self):
        with patch.object(fin, "get_universe", return_value=[]):
            self.assertEqual(fin.main([]), 1)
        result, _ = self.fetch()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fin, "FIN_DIR", directory), \
                patch.object(fin, "get_universe", return_value=["sh600519", "sz000001"]), \
                patch.object(fin, "fetch_fin_batch", return_value=result) as fetch:
            self.assertEqual(fin.main(["--limit", "1"]), 0)
            self.assertEqual(fetch.call_args.args[0], ["sh600519"])


if __name__ == "__main__":
    unittest.main()
