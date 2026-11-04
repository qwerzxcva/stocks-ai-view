import unittest
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import engine_v5


class CentralWeightContractTest(unittest.TestCase):
    def score(self, votes, weights):
        with patch.object(engine_v5, "build_votes", return_value=votes):
            return engine_v5.score_stock([], [], [], [], weights=weights)

    def test_collector_stocks_schema_supplies_real_three_day_series(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        days = [(now-timedelta(days=i)).date().isoformat() for i in range(3)]
        document = {"ts": now.isoformat(), "stocks": {"600519": {
            "code": "600519", "source": "dc_stock_flow", "daily_series": [
                {"date": day, "main_net": value} for day, value in zip(days, [10000000, 20000000, 30000000])
            ]}}}
        with tempfile.TemporaryDirectory() as root:
            Path(root, "moneyflow.json").write_text(json.dumps(document))
            vote = engine_v5.capital_flow_consistency("600519", root)
            self.assertIsNotNone(vote)
            self.assertEqual(1, vote[0])
            self.assertIn("6000", vote[2])
            document["stocks"]["600519"]["daily_series"] = document["stocks"]["600519"]["daily_series"][:1]
            Path(root, "moneyflow.json").write_text(json.dumps(document))
            self.assertIsNone(engine_v5.capital_flow_consistency("600519", root))

    def test_zero_weight_is_excluded_from_score_counts_and_votes(self):
        votes = [
            {"indicator": "on", "vote": 1, "strength": 1.0},
            {"indicator": "off", "vote": -1, "strength": 1.0},
        ]
        result = self.score(votes, {"on": 1.0, "off": 0.0})
        self.assertEqual(100.0, result["score"])
        self.assertEqual(1, result["bull"])
        self.assertEqual(0, result["bear"])
        self.assertEqual([votes[0]], result["votes"])

    def test_all_disabled_returns_no_score_not_neutral_fifty(self):
        votes = [{"indicator": "off", "vote": -1, "strength": 1.0}]
        self.assertIsNone(self.score(votes, {"off": 0.0}))

    def test_invalid_weight_is_not_directional_evidence(self):
        votes = [{"indicator": "off", "vote": 1, "strength": 1.0}]
        self.assertIsNone(self.score(votes, {"off": float("nan")}))
        self.assertIsNone(self.score(votes, {"off": -1.0}))


if __name__ == "__main__":
    unittest.main()
