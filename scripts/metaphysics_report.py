#!/usr/bin/env python3
"""
metaphysics_report.py — 全市场股票名称字辈分组统计（玄学对照实验产物）

扫描 data/data/stock_names.json 中的中文简称，按 name_matching 的字辈组
定义归类，输出 data/name_groups.json。

⚠️ 对照实验产物：字辈分组无预测力，仅供审计与可解释性展示，
   禁止纳入引擎权重、选股池或实盘决策。

用法:
  python3 scripts/metaphysics_report.py
  python3 scripts/metaphysics_report.py --out data/name_groups.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import name_matching  # noqa: E402

CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")


def build_report(names: dict[str, str]) -> dict:
    groups: dict[str, dict] = {
        key: {"label": g["label"], "stocks": []}
        for key, g in name_matching.NAME_GROUPS.items()
    }
    classified = 0
    multi_group = 0
    for code, name in sorted(names.items()):
        hits = name_matching.match_groups(name)
        if not hits:
            continue
        classified += 1
        if len(hits) > 1:
            multi_group += 1
        for key, chars in hits.items():
            groups[key]["stocks"].append({
                "code": code,
                "name": name.strip(),
                "chars": chars,
            })

    for g in groups.values():
        g["count"] = len(g["stocks"])
        g["stocks"].sort(key=lambda s: s["code"])

    return {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "metaphysics_report",
        "status": "CONTROL_EXPERIMENT_DO_NOT_USE",
        "n_names": len(names),
        "n_classified": classified,
        "n_multi_group": multi_group,
        "coverage_pct": round(classified / len(names) * 100, 2) if names else 0.0,
        "groups": groups,
        "group_defs": {
            k: {"label": v["label"], "chars": v["chars"],
                "pinyin": sorted(v["pinyin"]), "radicals": sorted(v["radicals"])}
            for k, v in name_matching.NAME_GROUPS.items()
        },
        "warning": "玄学对照实验：字辈分组无统计预测力，禁止用于实盘决策",
        "provenance": {"source": "data/data/stock_names.json",
                       "matcher": "scripts/name_matching.py"},
    }


def print_summary(rep: dict) -> None:
    print(f"中文名 {rep['n_names']} 条，命中字辈 {rep['n_classified']} "
          f"（{rep['coverage_pct']}%），跨组 {rep['n_multi_group']}")
    for key, g in rep["groups"].items():
        examples = "、".join(s["name"] for s in g["stocks"][:6])
        print(f"  {g['label']}({key}): {g['count']:4d} 只  例: {examples}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA, "name_groups.json"))
    ap.add_argument("--names", default=None, help="自定义中文名表路径")
    args = ap.parse_args()

    names = name_matching.load_cn_names(args.names)
    if not names:
        print("找不到含中文名的 stock_names.json", file=sys.stderr)
        return 1

    rep = build_report(names)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print_summary(rep)
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
