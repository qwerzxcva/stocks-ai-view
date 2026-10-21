#!/usr/bin/env python3
"""Audit cloud universe, stock bundles, apply outputs, and engine coverage.

The audit is deliberately read-only. It uses the cloud warehouse catalog and
GitHub repository trees as evidence, then inspects local engine artifacts when
available. Missing evidence is reported as ``insufficient_data``; no score is
invented for a security that has no verified engine output.

Examples:
  python3 scripts/coverage_audit.py
  python3 scripts/coverage_audit.py --json-out /tmp/coverage_report.json
  python3 scripts/coverage_audit.py --offline --master data/universe.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))
DEFAULT_MASTER_URL = (
    "https://raw.githubusercontent.com/jiegengxiangjianxue1/db-market/main/"
    "master/universe.json"
)
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/jiegengxiangjianxue1/db-market/main/"
    "_index.json"
)
DEFAULT_TREE_REPOS = {
    "00": "jiegengxiangjianxue1/db-stocks-a",
    "30": "jiegengxiangjianxue2/db-stocks-b",
    "60": "jiegengxiangjianxue2/db-stocks-c",
    "68": "jiegengxiangjianxue3/db-stocks-d",
    "15": "jiegengxiangjianxue3/db-stocks-d",
    "51": "jiegengxiangjianxue3/db-stocks-d",
    "50": "jiegengxiangjianxue3/db-stocks-d",
    "52": "jiegengxiangjianxue3/db-stocks-d",
    "56": "jiegengxiangjianxue3/db-stocks-d",
    "58": "jiegengxiangjianxue3/db-stocks-d",
    "20": "jiegengxiangjianxue3/db-stocks-d",
}
DEFAULT_BUNDLE_REPOS = {
    "00": "jiegengxiangjianxue1/db-stocks-a",
    "30": "jiegengxiangjianxue2/db-stocks-b",
    "60": "jiegengxiangjianxue2/db-stocks-c",
    "68": "jiegengxiangjianxue3/db-stocks-d",
    "15": "jiegengxiangjianxue3/db-stocks-d",
    "51": "jiegengxiangjianxue3/db-stocks-d",
    "50": "jiegengxiangjianxue3/db-stocks-d",
    "52": "jiegengxiangjianxue3/db-stocks-d",
    "56": "jiegengxiangjianxue3/db-stocks-d",
    "58": "jiegengxiangjianxue3/db-stocks-d",
    "20": "jiegengxiangjianxue3/db-stocks-d",
}
ENGINE_FILES = (
    "engine_v5.json",
    "multidim.json",
    "ai_picks.json",
    "ai_pick_v4.json",
    "lowpool.json",
    "cycle_pool_v2.json",
    "sector_pool.json",
    "flow_divergence.json",
    "decision.json",
    "ban_pick.json",
)


def _http_json(url: str, timeout: int = 30) -> tuple[Any | None, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "stocks-ai-view-coverage-audit/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")), {"url": url, "status": "ok", "bytes": len(raw)}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return None, {"url": url, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _bare(code: Any) -> str | None:
    text = re.sub(r"\D", "", str(code or ""))
    return text.zfill(6) if len(text) <= 6 and text else None


def _codes_from_master(payload: Any) -> set[str]:
    stocks = payload.get("stocks") if isinstance(payload, dict) else None
    if isinstance(stocks, dict):
        return {code for code in (_bare(x) for x in stocks) if code}
    if isinstance(stocks, list):
        return {code for code in (_bare(x.get("code") if isinstance(x, dict) else x) for x in stocks) if code}
    codes = payload.get("codes") if isinstance(payload, dict) else None
    return {code for code in (_bare(x) for x in codes or []) if code}


def _master_shards(payload: Any) -> dict[str, set[str]]:
    stocks = payload.get("stocks") if isinstance(payload, dict) else {}
    out: dict[str, set[str]] = {}
    if isinstance(stocks, dict):
        for raw, item in stocks.items():
            code = _bare(raw)
            if not code:
                continue
            shard = str((item or {}).get("shard") or code[:2]) if isinstance(item, dict) else code[:2]
            out.setdefault(shard, set()).add(code)
    return out


def _github_tree(repo: str, branch: str = "main") -> tuple[list[str], dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    payload, evidence = _http_json(url, timeout=60)
    if isinstance(payload, dict):
        paths = [str(row.get("path")) for row in payload.get("tree", []) if isinstance(row, dict) and row.get("path")]
        evidence["truncated"] = bool(payload.get("truncated"))
        evidence["path_count"] = len(paths)
        return paths, evidence

    # GitHub's unauthenticated API is rate-limited in collection containers.
    # A shallow, blob-filtered clone still gives an independently verifiable
    # tree without downloading stock payloads.
    tmp = tempfile.mkdtemp(prefix="coverage-tree-")
    clone_url = f"https://github.com/{repo}.git"
    try:
        clone = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--depth=1", "--branch", branch, clone_url, tmp],
            capture_output=True, text=True, timeout=240,
        )
        if clone.returncode != 0:
            return [], {**evidence, "fallback": "git_clone", "status": "error", "error": clone.stderr.strip()[-500:]}
        listing = subprocess.run(
            ["git", "-C", tmp, "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=120,
        )
        paths = [line for line in listing.stdout.splitlines() if line]
        return paths, {"url": clone_url, "status": "ok", "source": "git_ls_tree", "path_count": len(paths), "truncated": False}
    except (OSError, subprocess.SubprocessError) as exc:
        return [], {**evidence, "fallback": "git_clone", "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bundle_codes(paths: Iterable[str]) -> set[str]:
    out = set()
    for path in paths:
        match = re.search(r"(?:^|/)(\d{6})/stock\.json$", path)
        if match:
            out.add(match.group(1))
    return out


def _apply_codes(data: Any) -> set[str]:
    if not isinstance(data, dict):
        return set()
    values: list[Any] = []
    for key in ("top", "table", "results", "picks", "strong", "watch_list", "buy_side", "sell_side", "avoid"):
        value = data.get(key)
        if isinstance(value, list):
            values.extend(value)
    out = set()
    for row in values:
        raw = row.get("code") if isinstance(row, dict) else row
        code = _bare(raw)
        if code:
            out.add(code)
    return out


def _artifact_names(data_dir: Path) -> list[str]:
    names = set(ENGINE_FILES)
    if data_dir.exists():
        names.update(path.name for path in data_dir.glob("*.json"))
    return sorted(names)


def _engine_row_count(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in ("n_scored", "n_scanned", "total_filtered", "total_securities"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    for key in ("table", "results", "top", "picks", "strong"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _engine_status(data: Any, path: Path) -> dict[str, Any]:
    if data is None:
        return {"status": "missing", "file": path.name, "row_count": None,
                "reported_scan_count": None, "codes": []}
    if not isinstance(data, dict):
        return {"status": "insufficient_data", "file": path.name, "row_count": None,
                "reported_scan_count": None, "reported_scored_count": None,
                "code_evidence_count": 0, "code_evidence_coverage_pct": 0.0,
                "codes": [], "score_evidence": False,
                "note": "JSON is not a code-keyed object; no score inferred"}
    codes = sorted(_apply_codes(data))
    row_count = _engine_row_count(data)
    status = "available" if row_count is not None or codes else "insufficient_data"
    evidence_pct = (len(codes) / row_count * 100) if row_count and codes else 0.0
    return {
        "status": status,
        "file": path.name,
        "row_count": row_count,
        "reported_scan_count": data.get("n_scanned") if isinstance(data.get("n_scanned"), int) else row_count,
        "reported_scored_count": data.get("n_scored") if isinstance(data.get("n_scored"), int) else None,
        "code_evidence_count": len(codes),
        "code_evidence_coverage_pct": round(evidence_pct, 4),
        "codes": codes,
        "score_evidence": bool(codes),
        "note": "reported counts are kept separate from code-keyed evidence; no score inferred for absent codes",
    }


def _coverage_row(covered: set[str], universe: set[str], reason: str) -> dict[str, Any]:
    missing = universe - covered
    return {
        "universe_count": len(universe),
        "covered_count": len(covered & universe),
        "missing_count": len(missing),
        "coverage_pct": round(len(covered & universe) / len(universe) * 100, 4) if universe else None,
        "missing_codes_sample": sorted(missing)[:50],
        "missing_reason": reason if missing else None,
        "status": "complete" if not missing else "partial",
    }


def audit(
    *,
    master: Any | None = None,
    index: Any | None = None,
    trees: dict[str, list[str]] | None = None,
    local_data_dir: Path | None = None,
    tree_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a report from supplied evidence; network collection is in main()."""
    master = master if master is not None else {}
    universe = _codes_from_master(master)
    expected_by_shard = _master_shards(master)
    trees = trees or {}
    tree_evidence = tree_evidence or {}
    bundle_by_shard: dict[str, set[str]] = {}
    for shard, paths in trees.items():
        bundle_by_shard[shard] = _bundle_codes(paths)

    bundle_codes = set().union(*bundle_by_shard.values()) if bundle_by_shard else set()
    bundle_missing = universe - bundle_codes
    shard_rows = {}
    for shard, expected in sorted(expected_by_shard.items()):
        actual = bundle_by_shard.get(shard, set())
        evidence = tree_evidence.get(shard, {})
        reason = "no db-client shard route"
        if shard in DEFAULT_TREE_REPOS:
            reason = "remote tree unavailable"
        if evidence.get("status") == "ok" and not actual and not trees.get(shard):
            reason = "tree available but no stock.json entries (bundle path absent or unbuilt)"
        elif evidence.get("status") == "ok" and expected - actual:
            reason = "master codes exist but stock.json is absent from shard tree"
        shard_rows[shard] = {
            "expected_count": len(expected),
            "stock_bundle_count": len(actual & expected),
            "extra_bundle_count": len(actual - expected),
            "missing_count": len(expected - actual),
            "missing_codes_sample": sorted(expected - actual)[:20],
            "reason": reason if expected - actual else None,
            "evidence": evidence,
        }

    local_data_dir = local_data_dir or ROOT / "data"
    engines = {}
    engine_union: set[str] = set()
    pool_union: set[str] = set()
    artifact_names = _artifact_names(local_data_dir)
    for name in artifact_names:
        data, error = _load_json(local_data_dir / name)
        row = _engine_status(data, local_data_dir / name)
        if error:
            row["load_error"] = error
        row["universe_coverage"] = _coverage_row(set(row["codes"]), universe, "artifact has no verifiable code rows")
        engines[name] = row
        engine_union.update(row["codes"])
        if any(token in name for token in ("pick", "pool", "decision", "ban_", "flow_divergence")):
            pool_union.update(row["codes"])

    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "status": "insufficient_data" if not universe else ("partial" if bundle_missing or not bundle_codes else "complete"),
        "universe": {
            "master_count": len(universe),
            "master_source": "cloud master/universe.json" if master else "missing",
            "shard_count": len(expected_by_shard),
            "codes_by_shard": {k: len(v) for k, v in sorted(expected_by_shard.items())},
        },
        "stock_bundle": {
            "coverage": _coverage_row(bundle_codes, universe, "master universe code has no verified <shard>/<code>/stock.json"),
            "by_shard": shard_rows,
            "repos": {shard: DEFAULT_BUNDLE_REPOS.get(shard) for shard in sorted(expected_by_shard)},
            "apply_contract": {
                "path": "<shard>/<code>/stock.json",
                "source": "db-client.js stockBundle routing contract",
                "verified_from_tree": bool(bundle_codes),
                "missing_is_not_scored": True,
            },
            "apply_coverage": _coverage_row(bundle_codes, universe, "no verified stock_bundle payload at the db-client apply path"),
        },
        "engines": engines,
        "artifact_inventory": {
            "candidate_json_count": len(artifact_names),
            "candidate_json_files": artifact_names,
            "code_keyed_artifact_count": sum(1 for row in engines.values() if row.get("code_evidence_count", 0)),
            "note": "Top-level data JSON artifacts are inventoried; reported scan counts remain separate from code-keyed evidence.",
        },
        "engine_coverage": {
            "all_engine_code_union": _coverage_row(engine_union, universe, "local artifact does not contain a verifiable code row"),
            "pool_code_union": _coverage_row(pool_union, universe, "pool artifact has no verifiable code row"),
            "score_claim_allowed": bool(universe) and engine_union >= universe,
            "score_claim_note": "Only codes present in an engine artifact are eligible for score claims; absent codes remain insufficient_data.",
        },
        "limitations": [
            "Engine artifacts are local snapshots; this audit does not treat historical win rate as current accuracy.",
            "A Git tree proves path presence, not payload freshness or semantic field completeness.",
            "No remote apply output is assumed when it is not exposed as a verifiable code-keyed artifact.",
        ],
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    master, master_ev = (None, {"status": "skipped"})
    index, index_ev = (None, {"status": "skipped"})
    trees: dict[str, list[str]] = {}
    tree_ev: dict[str, dict[str, Any]] = {}
    if args.master:
        master, error = _load_json(Path(args.master))
        master_ev = {"url": args.master, "status": "ok" if master is not None else "error", "error": error}
    elif args.offline:
        local_master = ROOT / "data" / "universe.json"
        master, error = _load_json(local_master)
        master_ev = {"path": str(local_master), "status": "ok" if master is not None else "error", "error": error}
    else:
        master, master_ev = _http_json(DEFAULT_MASTER_URL)
    if not args.offline:
        index, index_ev = _http_json(DEFAULT_INDEX_URL)
    if not args.offline:
        repo_cache: dict[str, tuple[list[str], dict[str, Any]]] = {}
        for shard, repo in DEFAULT_TREE_REPOS.items():
            if repo not in repo_cache:
                repo_cache[repo] = _github_tree(repo)
            paths, evidence = repo_cache[repo]
            trees[shard] = paths
            tree_ev[shard] = {**evidence, "repo": repo}
    report = audit(master=master, index=index, trees=trees, local_data_dir=Path(args.data_dir), tree_evidence=tree_ev)
    report["evidence"] = {"master": master_ev, "index": index_ev, "trees": tree_ev}
    report["index_summary"] = {
        key: index.get(key) for key in ("generated_at", "n_securities_master", "n_stocks_built", "n_shards")
    } if isinstance(index, dict) else None
    if isinstance(index, dict):
        report["index_discrepancies"] = {
            "master_minus_index_master": report["universe"]["master_count"] - int(index.get("n_securities_master") or 0),
            "master_minus_index_built": report["universe"]["master_count"] - int(index.get("n_stocks_built") or 0),
            "index_master_minus_built": int(index.get("n_securities_master") or 0) - int(index.get("n_stocks_built") or 0),
        }
    else:
        report["index_discrepancies"] = None
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", help="write report JSON to this path")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="local engine artifact directory")
    parser.add_argument("--master", help="local or HTTP master universe JSON")
    parser.add_argument("--offline", action="store_true", help="do not make network requests")
    args = parser.parse_args()
    report = collect(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] != "insufficient_data" else 2


if __name__ == "__main__":
    raise SystemExit(main())
