import copy
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import engine_spec
import engine_v5


def vote(indicator, direction, strength=1.0):
    return {"indicator": indicator, "vote": direction, "strength": strength, "why": "test"}


class FamilyAggregationTest(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "aggregation": dict(engine_spec.AGGREGATION, kind="family_weighted_vote"),
            "engines": [
                {"weight_key": "A", "family": "momentum"},
                {"weight_key": "B", "family": "momentum"},
                {"weight_key": "C", "family": "trend"},
            ],
        }
        self.weights = dict.fromkeys("ABC", 1.0)

    def aggregate(self, votes, weights=None):
        return engine_v5.aggregate_votes(votes, self.weights if weights is None else weights, self.spec)

    def test_cancellation_and_duplicate_first_wins(self):
        votes = [vote("A", 1), vote("B", -1), vote("C", 1)]
        result = self.aggregate(votes)
        self.assertEqual((75.0, 0.5, 1.0), (result["score"], result["raw"], result["resonance"]))
        self.assertEqual((2, 1, 0), tuple(result[k] for k in ("bull", "bear", "neutral")))
        self.assertEqual((1, 0, 1), tuple(result[k] for k in ("familyBull", "familyBear", "familyNeutral")))
        self.assertEqual(result, self.aggregate(votes + [vote("A", 1)]))
        self.assertEqual(result, self.aggregate(votes + [vote("A", -1)]))

    def test_opposing_families_and_legacy_replay(self):
        votes = [vote("A", 1), vote("B", 1), vote("C", -1)]
        result = self.aggregate(votes)
        self.assertEqual((50.0, 0.0, 0.5), (result["score"], result["raw"], result["resonance"]))
        self.assertEqual((2, 1), (result["bull"], result["bear"]))
        self.assertEqual((1, 1, 0), tuple(result[k] for k in ("familyBull", "familyBear", "familyNeutral")))
        self.spec["aggregation"]["kind"] = "weighted_vote"
        # 历史公式：50 + 50/3 * (0.6 + 0.4 * 2/3) = 64.444...。
        self.assertEqual(64.4, self.aggregate(votes)["score"])
        self.assertEqual(72.5, self.aggregate(votes + [vote("A", 1)])["score"])

    def test_neutral_denominator_and_direction_tolerance(self):
        result = self.aggregate([vote("A", 1), vote("B", -1), vote("C", 0)])
        self.assertEqual((50.0, 0.0, 2), (result["score"], result["resonance"], result["familyNeutral"]))
        result = self.aggregate([vote("A", 1, 1e-13)])
        self.assertEqual((0, 1), (result["familyBull"], result["familyNeutral"]))

    def test_family_weight_is_max_member_weight(self):
        result = self.aggregate([vote("A", 1), vote("B", -1), vote("C", 1)], {"A": 3, "B": 1, "C": 2})
        self.assertEqual((85.0, 0.7), (result["score"], result["raw"]))

    def test_external_empty_and_unmapped_are_independent(self):
        for family in ("external", ""):
            with self.subTest(family=family):
                self.spec["engines"] = [{"weight_key": key, "family": family} for key in "AB"]
                result = self.aggregate([vote("A", 1), vote("B", 1), vote("C", -1)])
                self.assertEqual((64.4, 2, 1), (result["score"], result["familyBull"], result["familyBear"]))

    def test_invalid_votes_and_weights_are_dropped(self):
        for kind in ("weighted_vote", "family_weighted_vote"):
            self.spec["aggregation"]["kind"] = kind
            for bad in (math.nan, math.inf, -1, 0, "bad", None):
                with self.subTest(kind=kind, weight=bad):
                    result = self.aggregate([vote("A", 1), vote("C", -1)], {"A": bad, "C": 1})
                    self.assertEqual((0.0, 0, 1), (result["score"], result["bull"], result["bear"]))
            for bad_vote in (vote("A", 2), vote("A", math.nan), vote("A", 1, math.inf),
                             vote("A", 1, -0.1), vote("A", 1, 1.1), vote("A", 1, None)):
                with self.subTest(kind=kind, vote=bad_vote):
                    self.assertEqual(0.0, engine_v5.aggregate_votes([bad_vote, vote("C", -1)], self.weights, self.spec)["score"])
        self.assertIsNone(self.aggregate([]))
        self.assertIsNone(self.aggregate([vote("A", 1)], {"A": 0}))

    def test_invalid_first_duplicate_still_wins(self):
        self.assertIsNone(self.aggregate([vote("A", 1, math.nan), vote("A", 1)]))

    def test_missing_weight_evidence_and_no_mutation(self):
        votes = [vote("A", 1)]
        before = copy.deepcopy((votes, self.spec))
        result = self.aggregate(votes, {})
        self.assertEqual((100.0, ["A"]), (result["score"], result["missing_weight_keys"]))
        self.assertEqual(before, (votes, self.spec))

    def test_unknown_kind_rejected_even_without_votes(self):
        self.spec["aggregation"]["kind"] = "unknown"
        with self.assertRaises(ValueError):
            self.aggregate([])

    def test_score_stock_uses_shared_aggregation(self):
        votes = [vote("RSI", 1), vote("MACD", -1), vote("SuperTrend", 1)]
        weights = {v["indicator"]: 1 for v in votes}
        with patch.object(engine_v5, "build_votes", return_value=votes):
            result = engine_v5.score_stock([], [], [], [], weights)
        self.assertEqual(75.0, result["score"])
        self.assertEqual(result, engine_v5.aggregate_votes(votes, weights))

    def test_spec_version_registry_and_default_provenance(self):
        with patch.object(engine_spec, "load_weights", return_value={"RSI": 1.2, "flow": 0.2}):
            spec = engine_spec.build_spec()
        self.assertEqual("3.3.0", spec["spec_version"])
        self.assertEqual("family_weighted_vote", spec["aggregation"]["kind"])
        engines = {e["weight_key"]: e for e in spec["engines"]}
        self.assertEqual("momentum", engines["momentum_overheat"]["family"])
        for key in ("flow", "tape", "book", "InDTPool"):
            self.assertIn(key, engines)
        self.assertEqual(sorted(set(engines) - {"RSI", "flow"}), spec["default_weight_keys"])
        self.assertTrue(set(engines).issubset(spec["weights"]))
        self.assertEqual(0.2, spec["weights"]["flow"])
        with patch.object(engine_spec, "load_weights", return_value={}):
            spec = engine_spec.build_spec()
        self.assertEqual("equal", spec["weights_source"])
        self.assertEqual(sorted(engines), spec["default_weight_keys"])


if __name__ == "__main__":
    unittest.main()
