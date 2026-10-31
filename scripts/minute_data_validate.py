#!/usr/bin/env python3
"""
minute_data_validate.py — 分钟 parquet 质检 (只读数据、只写报告)

判定阈值全部写死在下方常量里 (仅单日股票数绝对下限可用 --min-symbols 覆盖), 报告逐日给出
「完整日 / 部分日 / 碎片日」三档结论。

检查项:
  1. 每票每日根数: 中位数 < MIN_BARS_COMPLETE(200) 判 fragmented;
     中位达标但单票根数不足的占比 > PARTIAL_RATIO, 或 missing_rate > PARTIAL_RATIO,
     判 partial (个体缺根, 中位数掩盖不了)。
     完整 A 股交易日应为 234~238 根 (09:31~11:30 + 13:01~15:00)。
  2. 时间戳域: 必须落在 [09:31, 15:00]; (symbol,date,hhmm) 不得重复。
  3. OHLC 一致性: high >= max(open,close,low), low <= min(open,close,high)。
  4. amount 空值率 > 1% 告警。
  5. 覆盖股票数 < 当日子集(单日最大股票数基准) × 90% 告警;
     且单日股票数 < MIN_SYMBOLS_ABS(3000, 可用 --min-symbols 覆盖) 直接判 failed
     — 相对基准测不出「只拉到 5 只票」, 必须有绝对下限。

退出码: 0 全部通过 / 1 存在 fragmented 或 partial 日、覆盖不足或校验失败 /
        2 没有可读的分钟文件。
缺字段或无法计算的指标一律写 null, 不臆造数值。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

# ─── 判定阈值 (硬编码) ───────────────────────────────────────────
MIN_BARS_COMPLETE = 200          # 每票每日中位根数低于此值 -> 碎片日
EXPECTED_BARS_FULL = 236         # 完整日中位基准 (实测 234~238)
MIN_SYMBOLS_ABS = 3000           # 单日股票数绝对下限 (相对基准测不出小得可怜的拉取)
PARTIAL_RATIO = 0.05             # 单票缺根占比 / missing_rate 的 partial 阀值
SESSION_FIRST = "09:31"
SESSION_LAST = "15:00"
AMOUNT_NULL_WARN = 0.01          # amount 空值率告警线
MIN_COVERAGE_RATIO = 0.90        # 单日股票数 / 当日子集基准 的下限
OHLC_TOL = 1e-6                  # 浮点价格比较容差

DEFAULT_DIR = os.path.expanduser("~/workspace/stocks-data-kline/data/minute/minute1")
DEFAULT_REPORT = os.path.expanduser(
    "~/workspace/stocks-data-kline/data/minute/minute_validate_report.json")
COLUMNS = ["symbol", "date", "hhmm", "open", "close", "high", "low", "volume", "amount"]


def collect_files(targets: list[str]) -> tuple[list[str], list[str]]:
    """返回 (存在的 parquet 文件, 缺失路径)。"""
    found: list[str] = []
    missing: list[str] = []
    for t in targets:
        t = os.path.abspath(os.path.expanduser(t))
        if os.path.isdir(t):
            hits = sorted(glob.glob(os.path.join(t, "*.parquet")))
            if hits:
                found.extend(hits)
            else:
                missing.append(t)
        elif os.path.isfile(t):
            found.append(t)
        else:
            missing.append(t)
    return found, missing


def load(files: list[str]) -> Any:
    import pandas as pd
    import pyarrow.parquet as pq

    frames = []
    for p in files:
        table = pq.read_table(p)
        names = set(table.column_names)
        lack = [c for c in ("symbol", "date", "hhmm") if c not in names]
        if lack:
            raise ValueError(f"{p} 缺必需列 {lack}")
        frames.append(table.to_pandas())
    return pd.concat(frames, ignore_index=True)


def analyse(df: Any, min_symbols_abs: int = MIN_SYMBOLS_ABS) -> dict[str, Any]:
    res: dict[str, Any] = {}
    df = df.copy()
    df["date"] = df["date"].astype(str)
    df["hhmm"] = df["hhmm"].astype(str)

    # ── 时间戳域 / 重复 ──
    hhmm_min = str(df["hhmm"].min())
    hhmm_max = str(df["hhmm"].max())
    res["timestamp_domain"] = {
        "min": hhmm_min,
        "max": hhmm_max,
        "expected": [SESSION_FIRST, SESSION_LAST],
        "out_of_range_rows": int(((df["hhmm"] < SESSION_FIRST)
                                  | (df["hhmm"] > SESSION_LAST)).sum()),
        "ok": hhmm_min >= SESSION_FIRST and hhmm_max <= SESSION_LAST,
    }
    dup = int(df.duplicated(subset=["symbol", "date", "hhmm"]).sum())
    res["duplicates"] = {"rows": dup, "ok": dup == 0}

    # ── OHLC ──
    need = [c for c in ("open", "close", "high", "low") if c in df.columns]
    if len(need) == 4:
        hi_break = (df["high"] < df[["open", "close", "low"]].max(axis=1) - OHLC_TOL)
        lo_break = (df["low"] > df[["open", "close", "high"]].min(axis=1) + OHLC_TOL)
        res["ohlc"] = {
            "high_below_others_rows": int(hi_break.sum()),
            "low_above_others_rows": int(lo_break.sum()),
            "ok": int(hi_break.sum()) == 0 and int(lo_break.sum()) == 0,
        }
    else:
        res["ohlc"] = {"ok": None, "reason": "insufficient: 缺 OHLC 列"}

    # ── amount 空值 ──
    if "amount" in df.columns:
        null_rate = float(df["amount"].isna().mean())
        res["amount_null"] = {"null_rate": round(null_rate, 6),
                              "warn_threshold": AMOUNT_NULL_WARN,
                              "ok": null_rate <= AMOUNT_NULL_WARN}
    else:
        res["amount_null"] = {"null_rate": None, "ok": None,
                              "reason": "insufficient: 无 amount 列"}

    # ── 逐日根数 / 覆盖 ──
    per_sym_day = df.groupby(["date", "symbol"], sort=False).size()
    bars = per_sym_day.groupby(level=0).agg(["median", "min", "max", "size"])
    rows = df.groupby("date", sort=True).size()
    ref_symbols = int(bars["size"].max()) if len(bars) else 0

    per_day: list[dict[str, Any]] = []
    for date in sorted(rows.index):
        b = bars.loc[date]
        n_sym = int(b["size"])
        med = float(b["median"])
        inc = int((per_sym_day.loc[date] < MIN_BARS_COMPLETE).sum())
        missing_rate = (round(1 - int(rows.loc[date]) / (n_sym * EXPECTED_BARS_FULL), 6)
                        if n_sym else None)
        complete = med >= MIN_BARS_COMPLETE
        partial = bool(complete and n_sym and (
            inc / n_sym > PARTIAL_RATIO
            or (missing_rate is not None and missing_rate > PARTIAL_RATIO)))
        per_day.append({
            "date": str(date),
            "rows": int(rows.loc[date]),
            "symbols": n_sym,
            "bars_per_sym_median": med,
            "bars_per_sym_min": int(b["min"]),
            "bars_per_sym_max": int(b["max"]),
            "expected_bars": EXPECTED_BARS_FULL,
            "missing_rate": missing_rate,
            "incomplete_symbol_days": inc,
            "incomplete_ratio": round(inc / n_sym, 4) if n_sym else None,
            "classification": ("fragmented" if not complete
                               else "partial" if partial else "complete"),
            "coverage_ratio": round(n_sym / ref_symbols, 4) if ref_symbols else None,
            "coverage_warn": bool(ref_symbols and n_sym < ref_symbols * MIN_COVERAGE_RATIO),
            "below_abs_min": bool(n_sym < min_symbols_abs),
        })

    res["coverage"] = {
        "reference_symbols": ref_symbols,
        "reference_def": "全数据集单日最大股票数(当日子集基准)",
        "abs_min_symbols": min_symbols_abs,
        "min_ratio": MIN_COVERAGE_RATIO,
        "warn_days": [d["date"] for d in per_day if d["coverage_warn"]],
        "under_abs_min_days": [d["date"] for d in per_day if d["below_abs_min"]],
        "ok": not any(d["coverage_warn"] or d["below_abs_min"] for d in per_day),
    }
    res["per_day"] = per_day
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="分钟 parquet 质检 (只读, 只写报告)")
    ap.add_argument("paths", nargs="*", help=f"parquet 文件或目录 (默认 {DEFAULT_DIR})")
    ap.add_argument("--report", default=DEFAULT_REPORT, help="报告 JSON 路径")
    ap.add_argument("--min-symbols", type=int, default=MIN_SYMBOLS_ABS,
                    help=f"单日股票数绝对下限 (默认 {MIN_SYMBOLS_ABS})")
    args = ap.parse_args()

    targets = args.paths or [DEFAULT_DIR]
    files, missing = collect_files(targets)
    if not files:
        print(f"✗ 没有可读的分钟 parquet: {targets}", file=sys.stderr)
        return 2

    try:
        df = load(files)
    except ImportError as exc:
        print(f"✗ 需要 pyarrow + pandas 才能质检: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"✗ 读取失败: {exc}", file=sys.stderr)
        return 2

    res = analyse(df, args.min_symbols)
    preds = res["per_day"]
    complete_days = [d["date"] for d in preds if d["classification"] == "complete"]
    partial_days = [d["date"] for d in preds if d["classification"] == "partial"]
    fragmented_days = [d["date"] for d in preds if d["classification"] == "fragmented"]
    hard_ok = all(bool(res[k].get("ok")) for k in
                  ("timestamp_domain", "duplicates", "ohlc", "amount_null", "coverage"))
    status = ("ok" if hard_ok and not fragmented_days and not partial_days
              else "failed")

    report = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "minute_data_validate",
        "source_files": files,
        "missing_targets": missing,
        "thresholds": {
            "min_bars_complete": MIN_BARS_COMPLETE,
            "expected_bars_full": EXPECTED_BARS_FULL,
            "session": [SESSION_FIRST, SESSION_LAST],
            "amount_null_warn": AMOUNT_NULL_WARN,
            "min_coverage_ratio": MIN_COVERAGE_RATIO,
            "min_symbols_abs": args.min_symbols,
            "partial_ratio": PARTIAL_RATIO,
        },
        "summary": {
            "status": status,
            "days": len(preds),
            "rows": int(len(df)),
            "symbols": int(df["symbol"].nunique()),
            "complete_days": complete_days,
            "partial_days": partial_days,
            "fragmented_days": fragmented_days,
        },
        "checks": {k: res[k] for k in ("timestamp_domain", "duplicates", "ohlc",
                                      "amount_null", "coverage")},
        "per_day": preds,
    }

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"→ 报告 {args.report}")

    print(f"文件 {len(files)} 份, 行数 {len(df):,}, 股票 {report['summary']['symbols']}, "
          f"交易日 {len(preds)}")
    print(f"完整日 {len(complete_days)} 天: {', '.join(complete_days) or '(无)'}")
    print(f"部分日 {len(partial_days)} 天: {', '.join(partial_days) or '(无)'}")
    print(f"碎片日 {len(fragmented_days)} 天: {', '.join(fragmented_days) or '(无)'}")
    for d in preds:
        flag = {"complete": "OK ", "partial": "部分", "fragmented": "碎片"}[
            d["classification"]]
        print(f"  {flag} {d['date']}  rows={d['rows']:>7,} symbols={d['symbols']:>5} "
              f"bars/sym median={d['bars_per_sym_median']:.0f} "
              f"min={d['bars_per_sym_min']} max={d['bars_per_sym_max']} "
              f"incomplete_sym={d['incomplete_symbol_days']} "
              f"coverage={d['coverage_ratio']} below_abs={d['below_abs_min']}")
    for k in ("timestamp_domain", "duplicates", "ohlc", "amount_null"):
        print(f"  {k}: ok={report['checks'][k]['ok']}  {report['checks'][k]}")

    if not hard_ok:
        return 1
    return 1 if (fragmented_days or partial_days) else 0


if __name__ == "__main__":
    sys.exit(main())
