import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import build_good_stock_pool as pool


class FakeEngine:
    def __init__(self, results=None):
        self.calls = []
        self.results = results

    def evaluate_batch(self, candidates):
        self.calls.append(candidates)
        if self.results is not None:
            return self.results
        return [
            {"code": item["code"], "decision": {
                "600519": "eligible", "000001": "watch", "600000": "reject"
            }.get(item["code"], "insufficient_data"), "score": 0.8}
            for item in candidates
        ]


def fixture_data(root):
    root.mkdir(parents=True, exist_ok=True)
    samples = {
        "ai_picks.json": {"ts": "2026-10-18T00:00:00+00:00", "picks": [{"code": "sh600519", "name": "贵州茅台", "score": 81}]},
        "today_picks.json": {"ts": "2026-10-18T00:00:00+00:00", "picks": {"signals": [{"code": "600519", "name": "贵州茅台", "price": 1500}], "values": [{"code": "sz000001", "name": "平安银行", "score": 0.7}], "limit_up": [], "avoid": []}},
        "lowpool.json": {"ts": "2026-10-18T00:00:00+00:00", "strong": [{"code": "600519", "name": "贵州茅台"}], "watch_list": [{"code": "600000", "name": "*ST示例"}]},
        "sector_pool.json": {"date": "2026-10-18", "meta": {"ts": "2026-10-18T00:00:00+00:00"}, "tier1": [{"code": "pt01801743", "sector": "白酒", "leader": "未映射领导"}], "tier2": [], "tier3": [], "tier_down": []},
        "valuation.json": {"ts": "2026-10-18T00:00:00+00:00", "table": {"sh600519": {"price": 1500, "intrinsic_value": 1200, "dev_pct": 25, "confidence": 0.8}}},
    }
    for name, data in samples.items():
        (root / name).write_text(json.dumps(data), encoding="utf-8")
    return root


class BuildGoodStockPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = fixture_data(self.root / "inputs")

    def tearDown(self):
        self.temp.cleanup()

    def test_deduplicates_all_sources_and_calls_engine_once(self):
        engine = FakeEngine()
        result = pool.build_pool(self.data_dir, engine, asof="2026-10-18T01:00:00+00:00")
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual({item["code"] for item in engine.calls[0]}, {"600519", "000001", "600000"})
        self.assertEqual(result["counts"]["eligible_count"], 1)
        item = result["eligible"][0]
        self.assertEqual(item["source_pools"], ["ai_picks", "lowpool", "today_picks"])
        self.assertEqual(item["valuation"]["intrinsic_value"], 1200)
        self.assertEqual(result["inputs"]["sector_context_count"], 1)

    def test_separates_watch_reject_and_insufficient_and_retains_st(self):
        result = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        self.assertEqual([x["status"] for x in result["watch"]], ["watch"])
        self.assertEqual([x["status"] for x in result["reject"]], ["reject"])
        st = result["reject"][0]
        self.assertTrue(st["is_st"])
        self.assertIn("ST", st["risk_note"])
        self.assertEqual(sum(x.get("code") is not None for x in result["insufficient"]), 0)
        self.assertEqual(result["insufficient"][0]["missing_fields"], ["leader.stock_code"])

    def test_insufficient_data_lists_missing_fields(self):
        engine = FakeEngine([{"code": "600519", "decision": "insufficient_data", "missing_fields": ["quote.price", "fundamentals.eps"]},
                             {"code": "000001", "decision": "watch"},
                             {"code": "600000", "decision": "reject"}])
        result = pool.build_pool(self.data_dir, engine, asof="2026-10-18T01:00:00+00:00")
        item = next(x for x in result["insufficient"] if x.get("code") == "600519")
        self.assertEqual(item["missing_fields"], ["quote.price", "fundamentals.eps"])
        self.assertEqual(result["counts"]["insufficient_count"], 2)
        self.assertEqual(sum(x.get("code") is not None for x in result["insufficient"]), 1)
        self.assertEqual(result["pool_type"], "analysis")

    def test_stale_input_becomes_insufficient_data(self):
        result = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-20T00:00:00+00:00", max_age_hours=12)
        self.assertEqual(result["counts"]["eligible_count"], 0)
        self.assertTrue(all(x["status"] == "insufficient_data" for x in result["insufficient"] if x.get("code")))

    def test_sector_without_stock_code_is_context_not_candidate(self):
        result = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        row = next(x for x in result["insufficient"] if x["reason"] == "sector_record_has_no_stock_code")
        self.assertEqual(row["missing_fields"], ["leader.stock_code"])

    def test_output_schema_is_additive_and_has_publish_and_protection_lists(self):
        result = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        required_old_fields = {"asof", "generated_by", "schema_version", "source_status", "provenance", "inputs", "eligible", "insufficient", "counts"}
        self.assertTrue(required_old_fields.issubset(result))
        self.assertTrue({"watch", "reject", "publish_list", "protected_paths", "pool_type"}.issubset(result))
        self.assertEqual(result["publish_list"], ["data/good_stock_pool.json"])
        self.assertEqual(result["counts"]["eligible"], len(result["eligible"]))
        self.assertEqual(result["counts"]["insufficient"], len(result["insufficient"]))

    def test_publish_gate_rejects_invalid_schema_counts_and_eligible_evidence(self):
        payload = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        self.assertEqual(pool.validate_payload(payload), [])
        bad_schema = dict(payload, schema_version=99)
        self.assertIn("unsupported schema_version", pool.validate_payload(bad_schema))
        bad_count = dict(payload, counts=dict(payload["counts"], eligible_count=99))
        self.assertIn("count mismatch: eligible_count", pool.validate_payload(bad_count))
        bad_eligible = dict(payload, eligible=[dict(payload["eligible"][0], missing_fields=["fundamentals.eps"])])
        self.assertIn("eligible[0] has missing fields", pool.validate_payload(bad_eligible))
        bad_eligible["eligible"] = [dict(payload["eligible"][0], engine={"decision": "watch"})]
        self.assertIn("eligible[0] lacks eligible engine evidence", pool.validate_payload(bad_eligible))

    def test_atomic_write_replaces_destination_with_validated_payload(self):
        payload = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        out = self.root / "published.json"
        out.write_text("old", encoding="utf-8")
        pool._atomic_write(out, payload)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.root.glob(".published.json.tmp-*")), [])

    def test_dry_run_preserves_existing_output_and_overwrite_is_protected(self):
        out = self.root / "existing.json"
        original = '{"owned":true}'
        out.write_text(original, encoding="utf-8")
        self.assertEqual(pool.main(["--data-dir", str(self.data_dir), "--out", str(out), "--dry-run"]), 0)
        self.assertEqual(out.read_text(encoding="utf-8"), original)
        with self.assertRaises(SystemExit) as caught:
            pool.main(["--data-dir", str(self.data_dir), "--out", str(out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.read_text(encoding="utf-8"), original)

    def test_invalid_record_is_insufficient_data(self):
        (self.data_dir / "today_picks.json").write_text(json.dumps({"ts": "2026-10-18T00:00:00+00:00", "picks": {"signals": [1]}}), encoding="utf-8")
        result = pool.build_pool(self.data_dir, FakeEngine(), asof="2026-10-18T01:00:00+00:00")
        self.assertTrue(any(x["status"] == "insufficient_data" and x["reason"] == "invalid_record" for x in result["insufficient"]))


if __name__ == "__main__":
    unittest.main()
