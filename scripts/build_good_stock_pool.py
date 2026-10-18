#!/usr/bin/env python3
"""Build a deduplicated, provenance-aware good-stock pool."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

CST = timezone.utc
POOL_FILES = {
    "ai_picks": "ai_picks.json",
    "today_picks": "today_picks.json",
    "lowpool": "lowpool.json",
    "sector_pool": "sector_pool.json",
}
ENGINE_FUNCTIONS = ("score_stocks", "evaluate_batch", "score_batch", "run_batch", "score_many", "evaluate_many")


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text[:2] in {"sh", "sz", "bj"} else text


def _asof(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) == 8:
        text += "T00:00:00+00:00"
    elif text.isdigit() and len(text) >= 14:
        text = text[:8] + "T" + text[8:14] + "+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST).isoformat(timespec="seconds")


def _source_asof(data: Mapping[str, Any]) -> str | None:
    meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
    return _asof(data.get("ts") or data.get("updated_at") or meta.get("ts") or data.get("date"))


def _is_stale(asof: str | None, now: datetime, max_age_hours: float) -> bool:
    if not asof:
        return True
    try:
        point = datetime.fromisoformat(asof)
    except ValueError:
        return True
    return (now - point.astimezone(CST)).total_seconds() > max_age_hours * 3600


def _load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable:{type(exc).__name__}"
    if not isinstance(value, dict):
        return None, "invalid_root"
    return value, None


def _records(data: Mapping[str, Any], pool: str) -> Iterable[Any]:
    if pool == "ai_picks":
        yield from (data.get("picks") or [])
    elif pool == "today_picks":
        picks = data.get("picks") or {}
        if isinstance(picks, Mapping):
            for group in ("signals", "values", "limit_up", "avoid"):
                yield from (picks.get(group) or [])
    elif pool == "lowpool":
        yield from (data.get("strong") or [])
        yield from (data.get("watch_list") or data.get("watch") or [])
    elif pool == "sector_pool":
        for tier in ("tier1", "tier2", "tier3", "tier_down"):
            for item in data.get(tier) or []:
                yield dict(item, _sector_tier=tier) if isinstance(item, Mapping) else item


def _candidate(record: Mapping[str, Any], pool: str, asof: str | None) -> dict[str, Any] | None:
    code = _code(record.get("code"))
    if not code or not code.isdigit() or len(code) != 6:
        return None
    name = str(record.get("name") or "").strip()
    is_st = "st" in name.lower() or "*" in name or "ＳＴ" in name.upper()
    sector_code = str(record.get("code") or "")
    if pool == "sector_pool" and not sector_code.isdigit():
        return None
    return {
        "code": code,
        "name": name,
        "is_st": is_st,
        "risk_note": "ST/*ST marker retained; requires additional risk review" if is_st else None,
        "source_pools": [pool],
        "source_asof": asof,
        "source_fields": dict(record),
        "quote": {"price": record.get("price"), "asof": asof},
        "fundamentals": {
            key: record[key]
            for key in ("pe", "pb", "eps", "net_income", "operating_cash_flow", "debt_to_equity")
            if record.get(key) is not None
        },
        "valuation": {},
        "empirical_edge": {},
        "sector": record.get("sector"),
        "technical": {
            key: record[key]
            for key in ("trend_score", "location_score", "volume_score")
            if record.get(key) is not None
        },
        "provenance": {"source": [pool], "snapshot_ts": asof},
        "adjustment": {"status": record.get("adjustment_status")} if record.get("adjustment_status") else {},
    }


def _merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    target["source_pools"] = sorted(set(target["source_pools"] + incoming["source_pools"]))
    if not target.get("name") and incoming.get("name"):
        target["name"] = incoming["name"]
    target["is_st"] = bool(target.get("is_st") or incoming.get("is_st"))
    if incoming.get("input_status") == "stale":
        target["input_status"] = "stale"
    if target["is_st"]:
        target["risk_note"] = "ST/*ST marker retained; requires additional risk review"
    if incoming.get("source_asof") and incoming["source_asof"] != target.get("source_asof"):
        target.setdefault("source_asofs", []).append(incoming["source_asof"])
    target.setdefault("source_fields_by_pool", {})
    for pool in incoming["source_pools"]:
        target["source_fields_by_pool"][pool] = incoming["source_fields"]


def _engine_callable(engine: Any):
    for name in ENGINE_FUNCTIONS:
        fn = getattr(engine, name, None)
        if callable(fn):
            return fn
    instance = getattr(engine, "GoodStockEngine", None)
    if instance:
        obj = instance() if isinstance(instance, type) else instance
        for name in ENGINE_FUNCTIONS:
            fn = getattr(obj, name, None)
            if callable(fn):
                return fn
    return None


def invoke_engine(engine: Any, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Invoke the engine once for the complete candidate batch."""
    fn = _engine_callable(engine)
    if fn is None:
        return {c["code"]: {"status": "insufficient", "reason": "engine_unavailable"} for c in candidates}
    try:
        result = fn(candidates)
    except TypeError:
        result = fn(items=candidates)
    if isinstance(result, Mapping):
        if all(isinstance(v, Mapping) for v in result.values()):
            return {_code(k): dict(v) for k, v in result.items()}
        return {_code(result.get("code")): dict(result)} if result.get("code") else {}
    if isinstance(result, list):
        return {_code(x.get("code")): dict(x) for x in result if isinstance(x, Mapping) and _code(x.get("code"))}
    return {}


def _load_engine(path: Path | None):
    if path is None:
        path = Path(__file__).with_name("good_stock_engine.py")
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("good_stock_engine", path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_pool(data_dir: Path, engine: Any = None, *, asof: str | None = None,
               max_age_hours: float = 24.0) -> dict[str, Any]:
    now = datetime.fromisoformat(asof).astimezone(CST) if asof else datetime.now(CST)
    candidates: dict[str, dict[str, Any]] = {}
    source_status: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    sector_context: list[dict[str, Any]] = []

    for pool, filename in POOL_FILES.items():
        data, error = _load(data_dir / filename)
        if error:
            source_status[pool] = {"file": filename, "status": error, "asof": None}
            continue
        point = _source_asof(data or {})
        stale = _is_stale(point, now, max_age_hours)
        source_status[pool] = {"file": filename, "status": "stale" if stale else "ok", "asof": point}
        for record in _records(data or {}, pool):
            if not isinstance(record, Mapping):
                rejected.append({"code": None, "source_pool": pool, "status": "insufficient_data", "missing_fields": ["record.mapping"], "reason": "invalid_record"})
                continue
            if pool == "sector_pool":
                sector_context.append({"sector": record.get("sector"), "tier": record.get("_sector_tier"), "leader": record.get("leader"), "asof": point})
                leader = str(record.get("leader") or "").strip()
                matched = next((item for item in candidates.values() if leader and item.get("name") == leader), None)
                if matched:
                    matched["source_pools"] = sorted(set(matched["source_pools"] + [pool]))
                    matched.setdefault("source_fields_by_pool", {})[pool] = dict(record)
                    matched.setdefault("sector_context", []).append({"sector": record.get("sector"), "tier": record.get("_sector_tier")})
                    continue
            item = _candidate(record, pool, point)
            if item is None:
                if pool == "sector_pool":
                    rejected.append({"code": None, "name": record.get("leader"), "source_pool": pool,
                                     "status": "insufficient_data", "missing_fields": ["leader.stock_code"],
                                     "reason": "sector_record_has_no_stock_code"})
                continue
            if stale:
                item["input_status"] = "stale"
            if item["code"] in candidates:
                _merge(candidates[item["code"]], item)
            else:
                candidates[item["code"]] = item

    valuation, valuation_error = _load(data_dir / "valuation.json")
    valuation_asof = _source_asof(valuation or {}) if valuation else None
    if valuation_error:
        source_status["valuation"] = {"file": "valuation.json", "status": valuation_error, "asof": None}
    else:
        valuation_stale = _is_stale(valuation_asof, now, max_age_hours)
        source_status["valuation"] = {"file": "valuation.json", "status": "stale" if valuation_stale else "ok", "asof": valuation_asof}
        table = (valuation or {}).get("table") or {}
        for code, item in candidates.items():
            value = table.get("sh" + code) or table.get("sz" + code) or table.get("bj" + code)
            if value:
                item["valuation"] = dict(value)
                item["valuation_asof"] = valuation_asof
        for item in candidates.values():
            if _is_stale(valuation_asof, now, max_age_hours):
                item.setdefault("source_fields_by_pool", {})["valuation"] = {"status": "stale", "asof": valuation_asof}

    batch = list(candidates.values())
    results = invoke_engine(engine, batch) if engine is not None else invoke_engine(None, batch)
    eligible: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    reject: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = list(rejected)
    for item in batch:
        result = results.get(item["code"], {"decision": "insufficient_data", "missing_fields": ["engine.result"]})
        status = str(result.get("decision") or result.get("status") or ("eligible" if result.get("eligible") else "reject")).lower()
        enriched = dict(item)
        enriched["engine"] = {k: v for k, v in result.items() if k != "code"}
        enriched["provenance"] = {"source": item["source_pools"], "snapshot_ts": item.get("source_asof"), "asof": asof or _now()}
        if item.get("input_status") == "stale":
            status, result = "insufficient", {"reason": "stale_input"}
            enriched["engine"] = result
        enriched["status"] = status
        enriched["missing_fields"] = list(result.get("missing_fields") or [])
        if item.get("is_st"):
            enriched["risk_note"] = "ST/*ST marker retained; requires additional risk review"
        if status in {"eligible", "accepted", "pass", "good"}:
            enriched["status"] = "eligible"
            eligible.append(enriched)
        elif status in {"watch", "observe"}:
            enriched["status"] = "watch"
            watch.append(enriched)
        elif status in {"insufficient", "insufficient_data"}:
            enriched["status"] = "insufficient_data"
            insufficient.append(enriched)
        else:
            enriched["status"] = "reject"
            reject.append(enriched)

    return {
        "asof": asof or _now(),
        "generated_by": "build_good_stock_pool.py",
        "schema_version": 1,
        "source_status": source_status,
        "provenance": {"source": list(POOL_FILES) + ["valuation"], "asof": asof or _now()},
        "inputs": {"candidate_count": len(batch), "sector_context_count": len(sector_context), "sector_context": sector_context},
        "eligible": eligible,
        "watch": watch,
        "reject": reject,
        "insufficient": insufficient,
        "publish_list": ["data/good_stock_pool.json"],
        "protected_paths": ["scripts/build_good_stock_pool.py", "tests/test_good_stock_pool.py", ".github/workflows/good-stock-pool.yml"],
        "counts": {"eligible": len(eligible), "watch": len(watch), "reject": len(reject),
                   "insufficient": len(insufficient), "candidate_count": len(batch),
                   "eligible_count": len(eligible), "insufficient_count": len(insufficient)},
        "pool_type": "recommendation" if eligible else "analysis"
    }



def validate_payload(payload: Mapping[str, Any]) -> list[str]:
    """Return publish-blocking schema, count, and eligible-evidence errors."""
    required = {"schema_version", "asof", "source_status", "provenance", "eligible", "watch", "reject", "insufficient", "counts"}
    errors = [f"missing root field: {key}" for key in sorted(required.difference(payload))]
    if payload.get("schema_version") != 1:
        errors.append("unsupported schema_version")
    for key in ("eligible", "watch", "reject", "insufficient"):
        if key in payload and not isinstance(payload[key], list):
            errors.append(f"{key} must be a list")
    counts = payload.get("counts")
    if not isinstance(counts, Mapping):
        errors.append("counts must be an object")
    else:
        for key in ("eligible", "watch", "reject", "insufficient", "eligible_count", "insufficient_count", "candidate_count"):
            if key not in counts:
                errors.append(f"missing count: {key}")
        for key in ("eligible", "watch", "reject", "insufficient"):
            if key in counts and isinstance(payload.get(key), list) and counts[key] != len(payload[key]):
                errors.append(f"count mismatch: {key}")
        if isinstance(payload.get("eligible"), list) and counts.get("eligible_count") != len(payload["eligible"]):
            errors.append("count mismatch: eligible_count")
        if isinstance(payload.get("insufficient"), list) and counts.get("insufficient_count") != len(payload["insufficient"]):
            errors.append("count mismatch: insufficient_count")
        if counts.get("candidate_count") != sum(counts.get(key, 0) for key in ("eligible", "watch", "reject")) + counts.get("insufficient", 0) - sum(1 for row in payload.get("insufficient", []) if isinstance(row, Mapping) and row.get("code") is None):
            errors.append("count mismatch: candidate_count")
    if not isinstance(payload.get("source_status"), Mapping) or not payload["source_status"]:
        errors.append("source_status evidence is missing")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance.get("source") or not provenance.get("asof"):
        errors.append("provenance evidence is missing")
    for index, item in enumerate(payload.get("eligible", []) if isinstance(payload.get("eligible"), list) else []):
        if not isinstance(item, Mapping):
            errors.append(f"eligible[{index}] is not an object")
            continue
        if item.get("status") != "eligible":
            errors.append(f"eligible[{index}] has non-eligible status")
        engine = item.get("engine")
        if not isinstance(engine, Mapping) or engine.get("decision") != "eligible":
            errors.append(f"eligible[{index}] lacks eligible engine evidence")
        if item.get("missing_fields"):
            errors.append(f"eligible[{index}] has missing fields")
        item_provenance = item.get("provenance")
        if not isinstance(item_provenance, Mapping) or not item_provenance.get("source") or not item_provenance.get("asof"):
            errors.append(f"eligible[{index}] lacks provenance evidence")
    return errors


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parents[1] / "data")
    parser.add_argument("--out", type=Path, default=Path(__file__).parents[1] / "data" / "good_stock_pool.json")
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--asof")
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.out.exists() and not args.dry_run:
        parser.error(f"refusing to overwrite existing output: {args.out}")
    payload = build_pool(args.data_dir, _load_engine(args.engine), asof=args.asof, max_age_hours=args.max_age_hours)
    errors = validate_payload(payload)
    if errors:
        parser.error("publish gate failed: " + "; ".join(errors))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.dry_run:
        _atomic_write(args.out, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
