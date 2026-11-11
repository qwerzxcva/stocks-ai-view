"""Isolated CSV loader regressions; never import the engine or real bulk data."""

import ast
import bisect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class LoadKlinesBulkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "scripts" / "engine_v5.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "load_klines")
        cls.loader_code = compile(ast.Module(body=[function], type_ignores=[]),
                                  str(source), "exec")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bulk = self.root / "bulk"
        self.bulk.mkdir()
        self.paths = ModuleType("paths")
        self.paths.AI_VIEW_DATA = str(self.root / "auxiliary")
        namespace = {"os": os, "json": json, "bisect": bisect,
                     "__file__": str(self.root / "engine_v5.py")}
        exec(self.loader_code, namespace)
        self.load = namespace["load_klines"]

    def write_shard(self, name, *rows):
        (self.bulk / name).write_text("\n".join(rows) + "\n", encoding="utf-8")

    def load_rows(self, limit=None):
        with patch.dict(sys.modules, {"paths": self.paths}):
            return self.load(str(self.bulk), limit=limit)

    def test_normal_no_limit_preserves_code_order_and_sorts_dates(self):
        self.write_shard("day_qfq_00.csv",
                         "sh600002|2026-11-11|2|3|4|1|5",
                         "sh600001|2026-11-10|1|1|1|1|1",
                         "sh600002|2026-10-29|1|1|1|1|1")
        rows = self.load_rows()
        self.assertEqual(list(rows), ["sh600002", "sh600001"])
        self.assertEqual([r[0] for r in rows["sh600002"]],
                         ["2026-10-29", "2026-11-11"])
        self.assertEqual(rows["sh600002"][-1], ["2026-11-11", 2, 3, 4, 1, 5])

    def test_limit_still_absorbs_recent_and_deduplicates(self):
        old = "sh600001|2026-10-29|1|1|1|1|1"
        self.write_shard("day_qfq_00.csv", old, old)
        self.write_shard("day_qfq_recent.csv", "sh600001|2026-11-11|1|1|1|1|1")
        rows = self.load_rows(limit=1)
        self.assertEqual([r[0] for r in rows["sh600001"]],
                         ["2026-10-29", "2026-11-11"])

    def test_identical_duplicates_within_and_across_shards(self):
        row = "sh600001|2026-10-29|1|1|1|1|1"
        self.write_shard("day_qfq_00.csv", row, row)
        self.write_shard("day_qfq_recent.csv", row)
        self.assertEqual(self.load_rows()["sh600001"], [["2026-10-29", 1, 1, 1, 1, 1]])

    def test_conflicting_ohlcv_is_rejected(self):
        original = "sh600001|2026-10-29|1|1|1|1|1"
        for column in range(5):
            for same_shard in (True, False):
                with self.subTest(column=column, same_shard=same_shard):
                    values = ["1"] * 5
                    values[column] = "2"
                    conflict = "sh600001|2026-10-29|" + "|".join(values)
                    self.write_shard("day_qfq_00.csv", original,
                                     conflict if same_shard else original)
                    self.write_shard("day_qfq_recent.csv", original if same_shard else conflict)
                    with self.assertRaisesRegex(ValueError, "sh600001.*2026-10-29"):
                        self.load_rows()

    def test_legacy_comma_and_pipe_rows_are_compatible(self):
        self.write_shard("day_qfq_00.csv", "# fixture", "", "invalid",
                         "sh600001|2026-10-29,1,2,3,4,5",
                         "sh600001|2026-10-29|1|2|3|4|5",
                         "sh600001|2026-11-11|2|3|4|5|6")
        self.assertEqual(self.load_rows()["sh600001"],
                         [["2026-10-29", 1, 2, 3, 4, 5], ["2026-11-11", 2, 3, 4, 5, 6]])

    def test_limit_does_not_admit_new_codes_in_any_shard(self):
        self.write_shard("day_qfq_00.csv", "sh600002|2026-10-29|1|1|1|1|1",
                         "sh600001|2026-10-29|1|1|1|1|1")
        self.write_shard("day_qfq_recent.csv", "sh600003|2026-11-11|1|1|1|1|1",
                         "sh600002|2026-11-11|2|2|2|2|2")
        rows = self.load_rows(limit=1)
        self.assertEqual(list(rows), ["sh600002"])
        self.assertEqual([r[0] for r in rows["sh600002"]],
                         ["2026-10-29", "2026-11-11"])


if __name__ == "__main__":
    unittest.main()
