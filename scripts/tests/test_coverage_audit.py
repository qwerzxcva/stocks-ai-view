import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import coverage_audit


def _master(codes):
    return {
        "stocks": {
            code: {"name": code, "shard": code[:2]}
            for code in codes
        }
    }


def test_bundle_coverage_distinguishes_missing_stock_json():
    master = _master(["000001", "300001", "688001"])
    trees = {
        "00": ["00/000001/stock.json"],
        "30": ["30/300001/manifest.json"],
        "68": [],
    }
    evidence = {
        "00": {"status": "ok"},
        "30": {"status": "ok"},
        "68": {"status": "ok"},
    }
    report = coverage_audit.audit(master=master, trees=trees, tree_evidence=evidence, local_data_dir=Path("/tmp/does-not-exist"))
    bundle = report["stock_bundle"]["coverage"]
    assert bundle["universe_count"] == 3
    assert bundle["covered_count"] == 1
    assert bundle["missing_count"] == 2
    assert report["stock_bundle"]["by_shard"]["30"]["reason"] == "master codes exist but stock.json is absent from shard tree"
    assert report["stock_bundle"]["by_shard"]["68"]["reason"] == "tree available but no stock.json entries (bundle path absent or unbuilt)"


def test_engine_absence_never_creates_score_or_coverage():
    master = _master(["000001", "000002"])
    report = coverage_audit.audit(
        master=master,
        trees={"00": ["00/000001/stock.json"]},
        tree_evidence={"00": {"status": "ok"}},
        local_data_dir=Path("/tmp/does-not-exist"),
    )
    assert report["engine_coverage"]["all_engine_code_union"]["covered_count"] == 0
    assert report["engine_coverage"]["all_engine_code_union"]["status"] == "partial"
    assert report["engine_coverage"]["score_claim_allowed"] is False
    assert all(row["status"] == "missing" for row in report["engines"].values())


def test_engine_codes_are_covered_only_when_present_in_artifact(tmp_path):
    (tmp_path / "engine_v5.json").write_text(
        '{"n_scanned": 2, "n_scored": 1, "top": [{"code": "000001", "score": 71.2}]}',
        encoding="utf-8",
    )
    master = _master(["000001", "000002"])
    report = coverage_audit.audit(
        master=master,
        trees={"00": ["00/000001/stock.json"]},
        tree_evidence={"00": {"status": "ok"}},
        local_data_dir=tmp_path,
    )
    row = report["engines"]["engine_v5.json"]
    assert row["codes"] == ["000001"]
    assert row["universe_coverage"]["covered_count"] == 1
    assert row["universe_coverage"]["missing_count"] == 1
    assert "000002" in row["universe_coverage"]["missing_codes_sample"]
