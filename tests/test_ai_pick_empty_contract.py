import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ai_pick_v4


class EmptyPickContractTest(unittest.TestCase):
    def test_no_candidates_keeps_uncomputed_fields_explicitly_null(self):
        feature = {
            "date": "2026-11-04", "is_zt_today": False,
            "ret": 0.0, "pos240": 0.2, "vratio": 2.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "picks.json"
            with patch.object(sys, "argv", ["ai_pick_v4", "--out", str(output)]), \
                    patch.object(ai_pick_v4, "load_bulk_klines", return_value={"600000": []}), \
                    patch.object(ai_pick_v4, "compute_features", return_value=feature):
                self.assertEqual(0, ai_pick_v4.main())
            result = json.loads(output.read_text())
        self.assertEqual([], result["picks"])
        self.assertEqual(0, result["n_candidates"])
        self.assertEqual(1, result["n_scanned"])
        self.assertEqual("2026-11-04", result["data_date"])
        for key in ("global", "adapt", "cycle_note"):
            self.assertIn(key, result)
            self.assertIsNone(result[key])


if __name__ == "__main__":
    unittest.main()
