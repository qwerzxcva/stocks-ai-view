import gzip
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import decision_contract
import horizon_features


def test_normalize_exchange_disambiguates_index():
    assert decision_contract.normalize_code("000937") == ("000937", "sz")
    assert decision_contract.normalize_code("sh000937") == ("000937", "sh")


def test_decision_quotes_batch_and_status(monkeypatch):
    calls = []

    def fake_quotes(codes, refresh=False):
        calls.append((codes, refresh))
        return {"sz000937": {"price": 4.5, "prev_close": 4.4, "time": "2026-10-17 10:00:00"}}

    monkeypatch.setattr(decision_contract.signal_hub, "quotes", fake_quotes)
    now = datetime.fromisoformat("2026-10-17T10:01:00+08:00")
    result = decision_contract.decision_quotes(
        ["000937", "sh000937"], now=now, max_age_seconds=300
    )
    assert calls == [(["sz000937", "sh000937"], True)]
    assert result["sz000937"].status == "ok"
    assert result["sz000937"].price == 4.5
    assert result["sh000937"].status == "missing"
    assert result["sz000937"].snapshot_id


def test_decision_quote_time_only(monkeypatch):
    monkeypatch.setattr(
        decision_contract.signal_hub,
        "quotes",
        lambda codes, refresh=False: {
            "sz000937": {"price": 4.5, "prev_close": 4.4, "time": "10:00:00"}
        },
    )
    result = decision_contract.decision_quotes(
        ["sz000937"], now=datetime.fromisoformat("2026-10-16T10:01:00+08:00")
    )
    assert result["sz000937"].status == "ok"
    assert result["sz000937"].ts == "2026-10-16T10:00:00+08:00"


def test_decision_quote_stale(monkeypatch):
    monkeypatch.setattr(
        decision_contract.signal_hub,
        "quotes",
        lambda codes, refresh=False: {
            "sz000937": {"price": 4.5, "prev_close": 4.4, "time": "2026-10-17 09:00:00"}
        },
    )
    result = decision_contract.decision_quotes(
        ["sz000937"], now=datetime.fromisoformat("2026-10-17T10:00:00+08:00")
    )
    assert result["sz000937"].status == "stale"


def test_horizon_features_mixed_files_and_conflicts(tmp_path):
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    (bulk / "universe.csv").write_text(
        "# code|name|market\nsz000937|fixture|sz\nsh000937|index|sh\n", encoding="utf-8"
    )
    base = "\n".join(f"sz000937|2025-01-{day:02d}|1|{day}|1|1|1" for day in range(1, 21)) + "\n"
    (bulk / "day_qfq_00.csv").write_text(base, encoding="utf-8")
    overlay = "sz000937|2025-01-20,1,999,1,1,1\nsh000937|2025-01-01,1,2,1,1,1\n"
    with gzip.open(bulk / "day_qfq_recent.csv.gz", "wt", encoding="utf-8") as handle:
        handle.write(overlay)
    result = horizon_features.build_features(str(bulk))
    sz = result["features"]["sz000937"]
    sh = result["features"]["sh000937"]
    assert sz["length"] == 20
    assert sz["ma20"] == 59.45
    assert sz["ma60"] is None and sz["ma120"] is None and sz["ma252"] is None
    assert sz["conflicting_rows"] == 1
    assert "day_qfq_recent.csv.gz" in sz["provenance"]
    assert sh["length"] == 1 and sh["first_date"] == "2025-01-01"
    assert result["universe_count"] == 2
    assert result["universe_covered_count"] == 2


def test_horizon_codes_subset(tmp_path):
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    (bulk / "universe.csv").write_text("sz000937|fixture|sz\n", encoding="utf-8")
    (bulk / "day_qfq_00.csv").write_text(
        "sz000937|2025-01-01|1|10|1|1|1\n", encoding="utf-8"
    )
    result = horizon_features.build_features(str(bulk), codes=["000937"])
    assert result["requested_count"] == 1
    assert result["features"]["sz000937"]["ma20"] is None
