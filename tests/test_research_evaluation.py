"""Boundary tests use synthetic in-memory bars, never production data."""
import csv
import io
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import research_evaluation as r


def rows(n=185):
    start = date(2020, 1, 1)
    return [dict(date=(start + timedelta(days=i)).isoformat(), open_price="10",
                 high_price="11", low_price="9", close_price="10", volume="100",
                 turnover="1000", hfq_factor="1", source="cnfin") for i in range(n)]


def parse(data):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(data[0]))
    writer.writeheader()
    writer.writerows(data)
    return r.parse_csv(stream.getvalue())


class ResearchEvaluationTest(unittest.TestCase):
    def test_illegal_duplicate_and_decreasing_dates(self):
        for bad in ("2020-02-30", "20200102", "2020-1-02", "2020-01-01", "2019-12-31"):
            data = rows(2)
            data[1]["date"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(data)

    def test_required_numeric_fields_rejected(self):
        for field, value in (("volume", "0"), ("turnover", "nan"), ("hfq_factor", "0"),
                             ("high_price", "9"), ("low_price", "11"),
                             ("close_price", ""), ("open_price", "inf")):
            data = rows(2)
            data[0][field] = value
            bars, info = parse(data)
            with self.subTest(field=field):
                self.assertIsNone(bars[0])
                self.assertEqual(1, info["rejected"])

    def test_source_gap_breaks_history_and_future(self):
        data = rows(250)
        data[120]["source"] = "api"
        bars, info = parse(data)
        self.assertEqual({"api": 1}, info["rejected_reasons"])
        self.assertIsNone(bars[120])
        self.assertIsNone(r.forward_label(bars, 119, 22, 0))
        with patch.object(r, "build_votes", return_value=[]):
            signals = r.build_signals("A", bars)
        self.assertEqual([data[119]["date"]] + [x["date"] for x in data[240:]],
                         [s["date"] for s in signals])

    def test_all_ohlc_adjustment_and_volume_preserved(self):
        data = rows(2)
        data[0]["hfq_factor"] = "2"
        data[1]["hfq_factor"] = "4"
        bars, _ = parse(data)
        adjusted = r.adjusted_window(bars, 4)
        self.assertEqual([5, 10], adjusted["open"])
        self.assertEqual([5.5, 11], adjusted["high"])
        self.assertEqual([4.5, 9], adjusted["low"])
        self.assertEqual([5, 10], adjusted["close"])
        self.assertEqual([100, 100], adjusted["volume"])

    def test_entry_cost_and_insufficient_horizon(self):
        bars, _ = parse(rows(24))
        self.assertIsNone(r.forward_label(bars, 1, 60, 0))
        label = r.forward_label(bars, 1, 22, 30)
        self.assertAlmostEqual(10 * .997 / (10 * 1.003) - 1, label["net_return"])
        self.assertTrue(label["recoup"])
        self.assertEqual(bars[23].date, label["label_end"])
        self.assertEqual(bars[2].date, label["entry_date"])
        self.assertIsNone(r.forward_label(bars, 2, 22, 0))
        with self.assertRaises(ValueError):
            r.forward_label(bars, 1, 22, -1)

    def test_entry_is_next_open_and_high_covers_both_costs(self):
        data = rows(23)
        for row in data[1:]:
            row.update(open_price="20", high_price="20", close_price="20", low_price="19")
        bars, _ = parse(data)
        self.assertTrue(r.forward_label(bars, 0, 22, 0)["recoup"])
        self.assertFalse(r.forward_label(bars, 0, 22, 10)["recoup"])
        self.assertEqual(20, r.forward_label(bars, 0, 22, 0)["entry_price"])

    def test_signal_independent_of_future_prices_and_external_disabled(self):
        data = rows(130)
        bars, _ = parse(data)
        before = r.build_signals("A", bars)[0]
        for row in data[120:]:
            row.update(open_price="100", high_price="110", low_price="90", close_price="100",
                       hfq_factor="10")
        changed, _ = parse(data)
        self.assertEqual(before, r.build_signals("A", changed)[0])
        with patch.object(r, "build_votes", return_value=[]) as votes:
            r.build_signals("A", bars[:120])
        args = votes.call_args.args
        self.assertEqual((None, ""), args[-2:])
        self.assertEqual(120, len(args[0]))

    def test_global_date_purge_and_calendar_embargo(self):
        samples = [dict(date=d, label_end=end, code=code) for d, end in
                   (("2020-01-01", "2020-01-02"), ("2020-01-03", "2020-01-09"),
                    ("2020-01-06", "2020-01-07"), ("2020-01-10", "2020-01-11"),
                    ("2020-01-20", "2020-01-21")) for code in ("A", "B")]
        folds = r.purged_walk_forward(samples, validation_size=1, min_train=1, embargo_days=2)
        fold = next(f for f in folds if f["validation_start"] == "2020-01-10")
        self.assertEqual({"2020-01-01", "2020-01-06"}, {s["date"] for s in fold["train"]})
        for f in folds:
            self.assertFalse({s["date"] for s in f["train"]} & {s["date"] for s in f["validation"]})
            for s in f["train"]:
                self.assertLess(date.fromisoformat(s["label_end"]),
                                date.fromisoformat(f["validation_start"]) - timedelta(days=2))
                self.assertLess(s["date"], f["validation_start"])

    def test_validation_summary_keeps_cost_and_model_cohorts_separate(self):
        signals = [{"code": "A", "date": "2020-01-02", "positive": {
            "family_weighted_vote": True, "weighted_vote": False}}]
        samples = [{"code": "A", "date": "2020-01-02", "horizon": 22, "labels": {
            "0": {"recoup": True, "net_return": .1},
            "10": {"recoup": False, "net_return": .09}}}]
        summary = r.validation_summary(samples, signals)
        family = [row for row in summary if row["horizon"] == 22 and row["cohort"] == "family_weighted_vote"]
        self.assertEqual([1, 1], [row["n"] for row in family])
        self.assertEqual([.1, .09], [row["mean_net_return"] for row in family])
        legacy = [row for row in summary if row["cohort"] == "weighted_vote"]
        self.assertTrue(all(row["n"] == 0 and row["recoup_rate"] is None for row in legacy))
        self.assertEqual([], r.validation_summary([], signals))

    def test_tied_spearman_and_coverage_and_zero_variance(self):
        self.assertAlmostEqual(1, r.spearman([1, 1, 2], [2, 2, 3]))
        self.assertIsNone(r.spearman([1, 1], [2, 3]))
        self.assertIsNone(r.icir([.2, .2]))
        self.assertIsNone(r.icir([.2]))
        self.assertAlmostEqual(0, r.icir([-.2, .2]))
        small = r.cross_sectional_ic([("A", 1, 2), ("B", 2, 3)])
        self.assertEqual(2, small["coverage"])
        self.assertIsNone(small["ic"])

    def test_filtered_sources_cannot_create_research_windows(self):
        data = rows(130)
        for i, row in enumerate(data):
            row["source"] = "cnfin" if i >= 124 else ("api" if i % 2 else "csv_legacy")
        bars, info = parse(data)
        self.assertEqual(6, info["accepted"])
        self.assertEqual({"csv_legacy": 62, "api": 62}, info["rejected_reasons"])
        report = r.evaluate({"A": (bars, info)})
        self.assertEqual("insufficient", report["status"])
        self.assertEqual(0, report["sample_count"])
        self.assertIn("120 contiguous valid cnfin", report["reason"])

    def test_family_correlations_need_30_same_code_past_dates(self):
        bars, _ = parse(rows(148))
        signals = r.build_signals("A", bars)
        pairs = r.family_correlations(signals)
        self.assertTrue(pairs)
        self.assertTrue(all(p["pearson"] is None for p in pairs))
        self.assertTrue(all(p["n"] < 30 for p in pairs))
        self.assertTrue(all(p["code"] == "A" for p in pairs))
        with self.assertRaises(ValueError):
            r.evaluate({"A": (bars, {})}, min_ic_stocks=1)

    def test_fixed_benchmarks_accept_original_mixed_sources(self):
        expected = {"sh600519": 6096, "sz002242": 2626}
        for code, count in expected.items():
            path = r.ROOT / "tests" / "fixtures" / "research" / f"{code}_benchmark.csv"
            bars, info = r.load_input(code, path, approved_benchmarks=True)
            self.assertEqual(r.APPROVED_BENCHMARKS[code], info["sha256"])
            self.assertEqual(count, info["row_count"])
            self.assertEqual(count, sum(info["source_counts"].values()))
            self.assertTrue(info["benchmark_approved"])
            self.assertGreater(info["accepted"], 120)
            self.assertGreater(info["source_counts"].get("api", 0)
                               + info["source_counts"].get("csv_legacy", 0), 0)
            self.assertEqual(count, len(bars))

    def test_tampered_benchmark_and_unknown_code_rejected(self):
        path = r.ROOT / "tests" / "fixtures" / "research" / "sh600519_benchmark.csv"
        payload = path.read_bytes()
        with patch.object(Path, "read_bytes", return_value=payload + b"\n"):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                r.load_input("sh600519", path, approved_benchmarks=True)
        with self.assertRaisesRegex(ValueError, "Unknown input code"):
            r.load_input("unknown", path, approved_benchmarks=True)
        with self.assertRaisesRegex(ValueError, "designated fixture"):
            r.load_input("sz002242", path, approved_benchmarks=True)
        with self.assertRaisesRegex(ValueError, "designated fixture"):
            r.load_input("sh600519", path)

    def test_unknown_benchmark_source_rejected_and_numeric_rules_unchanged(self):
        data = rows(2)
        data[0]["source"] = "unapproved_vendor"
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)
        with self.assertRaisesRegex(ValueError, "Unknown source"):
            r.parse_csv(stream.getvalue(), approved_benchmark=True)
        data[0]["source"] = "api"
        data[0]["high_price"] = "1"
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)
        bars, info = r.parse_csv(stream.getvalue(), approved_benchmark=True)
        self.assertIsNone(bars[0])
        self.assertEqual({"invalid_ohlcv_factor": 1}, info["rejected_reasons"])

    def test_report_costs_not_independent_and_insufficient_scopes(self):
        bars, info = parse(rows(145))
        report = r.evaluate({"A": (bars, info)}, cost_bps=10)
        self.assertEqual(26 * 2, report["sample_count"])
        self.assertEqual([0.0, 10.0, 30.0], report["cost_bps_per_side"])
        self.assertTrue(all(x["ic"] is None for x in report["daily_ic"]))
        for key in ("marketwide_evidence", "real_holdings", "capacity"):
            self.assertEqual("insufficient", report[key]["status"])
        self.assertEqual("equal_weights", report["weight_policy"])
        self.assertEqual("3.3.0", report["spec_version"])
        self.assertIsNone(next(s for s in report["samples"] if s["horizon"] == 60)["labels"]["10"])
        self.assertTrue(all(w == 1 for w in report["weights"].values()))


if __name__ == "__main__":
    unittest.main()
