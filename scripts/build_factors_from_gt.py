#!/usr/bin/env python3
"""build_factors_from_gt.py — 从基准行情 CSV 提取后复权因子表 (2026-10-17)。

基准 CSV (用户提供的准确数据, tests/fixtures/*_gt.csv) 带 hfq_factor 列,
这是唯一与我们的引擎口径完全一致 (茅台 hfq 与腾讯 hfq 在 2014 差 6.9%)
的因子来源。产出 data/adjusted/factors/{code}.json:
  {"code":..., "source":..., "factors": {date: hfq_factor}}

load_klines 读到因子表时: qfq = raw × factor / factor_latest。
后续扩充: 用 cnfin/分红送配接口对全市场建因子表 (下轮)。

用法:
  python3 build_factors_from_gt.py tests/fixtures/sh600519_gt.csv
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(ROOT, "data")

FACTORS_DIR = os.path.join(DATA_DIR, "adjusted", "factors")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+", help="基准行情 CSV (含 hfq_factor 列)")
    args = ap.parse_args()

    os.makedirs(FACTORS_DIR, exist_ok=True)
    for path in args.csv:
        factors = {}
        code = None
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "").strip()
                if not d:
                    continue
                if code is None:
                    raw_code = (row.get("code") or "").strip()
                    if raw_code:
                        code = raw_code.lower().replace(".sh", "").replace(".sz", "")
                        code = ("sh" if "sh" in raw_code.lower()
                                else "sz") + code
                try:
                    factors[d] = float(row["hfq_factor"])
                except (ValueError, KeyError):
                    continue
        if not factors or not code:
            # 行情 CSV 无 code 列: 从文件名推断 (sh600519_gt.csv)
            base = os.path.basename(path).split("_")[0]
            code = base if base[:2] in ("sh", "sz") else None
            if not code or not factors:
                print(f"SKIP {path}: 无法确定 code 或无因子数据", file=sys.stderr)
                continue
        out = {
            "code": code,
            "source": os.path.basename(path),
            "n": len(factors),
            "factors": dict(sorted(factors.items())),
        }
        dest = os.path.join(FACTORS_DIR, f"{code}.json")
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        first = min(factors); last = max(factors)
        print(f"{code}: {len(factors)} 个因子 ({first} ~ {last}, "
              f"latest={factors[last]}) -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
