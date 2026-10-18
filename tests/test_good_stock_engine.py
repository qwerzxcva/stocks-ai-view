import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from good_stock_engine import score_stock


NOW = "2026-10-18T10:00:00+00:00"


def complete_sample(**overrides):
    record = {
        "code": "600519",
        "quote": {"price": 100.0, "asof": "2026-10-18T09:30:00+00:00"},
        "fundamentals": {
            "eps": 8.0,
            "net_income": 1000.0,
            "operating_cash_flow": 1200.0,
            "debt_to_equity": 0.4,
            "pe": 15.0,
            "pb": 1.5,
            "period_end": "2026-06-30",
        },
        "valuation": {"valuation_margin": 0.8},
        "technical": {"trend_score": 0.8, "location_score": 0.7, "volume_score": 0.8},
        "empirical_edge": {"edge_score": 0.8, "n_samples": 1000},
        "adjustment": {"status": "verified", "safe_for_long_term": True},
        "provenance": {"source": "fixture", "confidence": "observed",
                         "snapshot_ts": "2026-10-18T09:30:00+00:00"},
    }
    record.update(overrides)
    return record


class GoodStockEngineTests(unittest.TestCase):
    def test_complete_high_score_is_eligible_and_explainable(self):
        result = score_stock(complete_sample(), now=NOW)
        self.assertEqual(result["decision"], "eligible")
        self.assertEqual(result["status"], "eligible")
        self.assertGreaterEqual(result["score"], 65)
        self.assertLessEqual(result["score"], 100)
        self.assertIn(result["grade"], {"A", "B", "C", "D", "F"})
        self.assertTrue(result["reasons"])
        # 缺 DPS 只记风险不拦合格; 其余风险必须为空
        self.assertTrue(all("dividend" in r for r in result["risks"]))
        self.assertEqual(result["missing_fields"], [])
        self.assertTrue(result["provenance"]["freshness_ok"])
        self.assertTrue(result["provenance"]["adjustment_safe_for_long_term"])
        self.assertEqual(result["asof"], "2026-10-18T09:30:00+00:00")

    def test_missing_financial_evidence_is_insufficient_and_scores_zero(self):
        record = complete_sample(fundamentals={"pe": 10.0, "pb": 1.0})
        result = score_stock(record, now=NOW)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertEqual(result["score"], 0)
        self.assertIn("fundamentals.eps", result["missing_fields"])
        self.assertIn("fundamentals.operating_cash_flow", result["missing_fields"])
        self.assertNotIn("positive EPS", result["reasons"])

    def test_price_pe_pb_only_cannot_be_eligible_or_derive_eps(self):
        result = score_stock({
            "code": "000001",
            "quote": {"price": 10.0, "asof": "2026-10-18T09:30:00+00:00"},
            "fundamentals": {"pe": 8.0, "pb": 0.8},
            "adjustment": {"safe_for_long_term": True},
            "technical": {},
            "empirical_edge": {},
        }, now=NOW)
        self.assertNotEqual(result["decision"], "eligible")
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertIn("fundamentals.eps", result["missing_fields"])
        self.assertNotIn("positive EPS", result["reasons"])

    def test_failed_adjustment_blocks_long_term_indicators(self):
        result = score_stock(complete_sample(adjustment={"status": "unsafe", "safe_for_long_term": False}), now=NOW)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertIn("technical.long_term_adjustment_verified", result["missing_fields"])
        self.assertIn("adjustment provenance is unsafe", " ".join(result["risks"]))
        self.assertNotIn("trend evidence", result["reasons"])
        self.assertNotIn("location evidence", result["reasons"])

    def test_stale_snapshot_is_insufficient(self):
        result = score_stock(
            complete_sample(quote={"price": 100.0, "asof": "2026-10-16T09:30:00+00:00"}),
            now=NOW,
            max_age_hours=24,
        )
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertFalse(result["provenance"]["freshness_ok"])
        self.assertIn("stale", " ".join(result["risks"]))

    def test_old_provenance_snapshot_cannot_be_hidden_by_fresh_quote(self):
        result = score_stock(complete_sample(provenance={"source": "fixture", "snapshot_ts": "2026-10-16T09:30:00+00:00"}), now=NOW)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertFalse(result["provenance"]["freshness_ok"])

    def test_negative_earnings_are_rejected_and_not_valued(self):
        fundamentals = complete_sample()["fundamentals"].copy()
        fundamentals.update({"eps": -1.0, "net_income": -100.0})
        result = score_stock(complete_sample(fundamentals=fundamentals), now=NOW)
        self.assertEqual(result["decision"], "reject")
        self.assertIn("negative or zero EPS", " ".join(result["risks"]))
        self.assertNotIn("valuation margin is backed by EPS, PE and PB", result["reasons"])

    def test_score_is_clamped_to_zero_and_hundred(self):
        low = complete_sample(
            fundamentals={"eps": -1, "net_income": -1, "operating_cash_flow": -1, "debt_to_equity": 999, "period_end": "2026-06-30"},
            technical={"trend_score": -1, "location_score": -1, "volume_score": -1},
            empirical_edge={"edge_score": -1, "n_samples": 1000},
        )
        self.assertEqual(score_stock(low, now=NOW)["score"], 0)
        high = complete_sample(
            fundamentals={"eps": 10, "net_income": 10, "operating_cash_flow": 10, "debt_to_equity": 0, "pe": 1, "pb": 0.1, "period_end": "2026-06-30"},
            valuation={"valuation_margin": 1},
            technical={"trend_score": 1, "location_score": 1, "volume_score": 1},
            empirical_edge={"edge_score": 1, "n_samples": 10000},
        )
        self.assertEqual(score_stock(high, now=NOW)["score"], 100)

    def test_source_conflict_rejects_even_with_complete_values(self):
        result = score_stock(complete_sample(provenance={"source": "fixture", "source_conflict": True, "snapshot_ts": "2026-10-18T09:30:00+00:00"}), now=NOW)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(result["provenance"]["source_conflict"])
        self.assertIn("source conflict", " ".join(result["risks"]))

    def test_empirical_edge_requires_sample_and_ignores_bull_resonance(self):
        result = score_stock(complete_sample(empirical_edge={"bull_resonance": 99}), now=NOW)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertIn("empirical_edge.n_samples", result["missing_fields"])
        self.assertIn("bull resonance is not treated as empirical evidence", " ".join(result["risks"]))

    def test_small_empirical_edge_is_not_eligible(self):
        result = score_stock(complete_sample(empirical_edge={"edge_pp": 10, "n_samples": 99}), now=NOW)
        self.assertNotEqual(result["decision"], "eligible")
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertIn("sample is too small", " ".join(result["risks"]))

    def test_complete_but_low_score_is_watch(self):
        result = score_stock(complete_sample(
            fundamentals={"eps": 1, "net_income": 1, "operating_cash_flow": 1, "debt_to_equity": 10, "pe": 100, "pb": 10, "period_end": "2026-06-30"},
            valuation={"valuation_margin": -1},
            technical={"trend_score": 0, "location_score": 0, "volume_score": 0},
            empirical_edge={"edge_score": 0, "n_samples": 1000},
        ), now=NOW)
        self.assertEqual(result["decision"], "watch")

    def test_fundamental_values_without_report_period_are_insufficient(self):
        record = complete_sample()
        record["fundamentals"].pop("period_end", None)
        result = score_stock(record, now=NOW)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertIn("fundamentals.report_period", result["missing_fields"])

    def test_missing_dps_only_affects_dividend_score_not_decision(self):
        result = score_stock(complete_sample(
            fundamentals={"eps": 8, "net_income": 1000, "operating_cash_flow": 1200,
                          "debt_to_equity": 0.4, "pe": 15, "pb": 1.5,
                          "period_end": "2026-06-30"},
        ), now=NOW)
        self.assertEqual(result["decision"], "eligible")
        self.assertIn("dividend per share evidence is missing", result["risks"])

    def test_positive_dps_gives_dividend_points(self):
        result = score_stock(complete_sample(
            fundamentals={"eps": 8, "net_income": 1000, "operating_cash_flow": 1200,
                          "debt_to_equity": 0.4, "pe": 15, "pb": 1.5,
                          "period_end": "2026-06-30",
                          "dps": 2.0},
        ), now=NOW)
        self.assertEqual(result["decision"], "eligible")
        self.assertTrue(any("dividend per share" in r for r in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
