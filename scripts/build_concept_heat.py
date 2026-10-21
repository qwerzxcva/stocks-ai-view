#!/usr/bin/env python3
"""Concept heat matrix: aggregate per-board daily bars into one compact JSON.

The frontend must not pull 100 x ~340KB of concept kline CSV. This script
reads the warehouse sector concept klines once and emits a matrix of
``concept x trading_day -> {chg_pct, close}`` for the trailing window, plus
the board catalogue (name, member count) so the UI can render rows without
extra requests.

Only observed data is emitted. A board with no bars in the window is dropped
from the matrix and listed under ``missing``; nothing is interpolated.

Usage:
  python3 scripts/build_concept_heat.py
  python3 scripts/build_concept_heat.py --days 40 --out data/concept_heat.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# db-market is the warehouse of record for sector dimensions; the local
# data/ copy only carries derived products.
DB_MARKET = "jiegengxiangjianxue1/db-market"
RAW = "https://raw.githubusercontent.com/%s/main/" % DB_MARKET
CONCEPT_INDEX = RAW + "sector/concept.json"
CONCEPT_KLINE = RAW + "sector/concept_kline/%s.csv"


def _get(url: str, timeout: int = 60) -> tuple[Any | None, str | None]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "stocks-ai-view-concept-heat/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace"), None
    except (OSError, urllib.error.URLError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _parse_bars(text: str) -> list[dict[str, Any]]:
    """Parse the '# comment\\ndate,open,close,high,low,volume' CSV shape."""
    rows: list[dict[str, Any]] = []
    # The warehouse CSV starts with a '# <bk> <name> ...' provenance line, so
    # drop it before handing the text to the csv reader (otherwise it becomes
    # the header row and every subsequent row fails to map).
    lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    for row in reader:
        if not row.get("date") or not row.get("close"):
            continue
        try:
            close = float(row["close"])
            prev_close = float(row.get("open") or row["close"])
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "date": row["date"].strip(),
                "close": close,
                # chg_pct is derived from the observed open/close of the same
                # bar only as a fallback; the real change uses the previous
                # bar close below.
                "open": prev_close,
            }
        )
    return rows


def build(days: int, top: int) -> dict[str, Any]:
    index_text, index_error = _get(CONCEPT_INDEX)
    if index_text is None:
        return {
            "status": "insufficient_data",
            "reason": "concept index unavailable",
            "evidence": index_error,
        }
    index = json.loads(index_text)
    boards = index.get("boards") or []
    if not boards:
        return {
            "status": "insufficient_data",
            "reason": "concept index has no boards",
            "evidence": CONCEPT_INDEX,
        }

    # Rank boards by market cap so the matrix keeps the most relevant rows
    # when the universe is larger than the requested display count.
    boards = sorted(
        boards, key=lambda b: float(b.get("total_mktcap") or 0), reverse=True
    )
    selected = boards[:top] if top else boards

    dates: list[str] = []
    matrix: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for board in selected:
        bk = board.get("bk")
        name = board.get("name") or bk
        if not bk:
            continue
        text, error = _get(CONCEPT_KLINE % bk)
        if text is None:
            errors.append({"bk": bk, "name": name, "error": error})
            missing.append({"bk": bk, "name": name, "reason": "kline fetch failed"})
            continue
        bars = _parse_bars(text)
        if len(bars) < 2:
            missing.append({"bk": bk, "name": name, "reason": "fewer than 2 bars"})
            continue
        window = bars[-(days + 1) :]
        cells = []
        for i in range(1, len(window)):
            prev_close = window[i - 1]["close"]
            close = window[i]["close"]
            if not prev_close:
                continue
            cells.append(
                {
                    "d": window[i]["date"],
                    "p": round((close / prev_close - 1) * 100, 2),
                }
            )
        if not cells:
            missing.append({"bk": bk, "name": name, "reason": "no computable change"})
            continue
        for cell in cells:
            if cell["d"] not in dates:
                dates.append(cell["d"])
        matrix.append(
            {
                "bk": bk,
                "name": name,
                "n_stocks": board.get("n_stocks"),
                "total_mktcap": board.get("total_mktcap"),
                "latest_close": window[-1]["close"],
                "cells": cells,
            }
        )

    dates.sort()
    if not dates:
        return {
            "status": "insufficient_data",
            "reason": "no board produced a usable window",
            "missing": missing,
            "errors": errors,
        }

    return {
        "status": "ok",
        "engine": "build_concept_heat.py",
        "source": "db-market sector/concept_kline (eastmoney daily board index)",
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "window_days": len(dates),
        "requested_days": days,
        "n_boards": len(matrix),
        "n_boards_requested": len(selected),
        "dates": dates,
        "boards": matrix,
        "missing": missing,
        "errors": errors,
        "caveat": (
            "格子数值为板块指数日涨跌幅%; 涨停/跌停家数需成分股日线, 本文件不含, "
            "故矩阵不显示家数以免用缺失数据伪造。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top", type=int, default=0, help="limit boards by market cap")
    parser.add_argument("--out", default=str(DATA / "concept_heat.json"))
    args = parser.parse_args()

    report = build(args.days, args.top)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)} bytes)")
    if report.get("status") != "ok":
        print(json.dumps(report, ensure_ascii=False)[:600])
        return 1
    print(
        "boards=%d dates=%d missing=%d"
        % (report["n_boards"], report["window_days"], len(report["missing"]))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
