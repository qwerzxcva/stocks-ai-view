#!/usr/bin/env python3
"""apply_rank_to_pools.py — 给现有池子加横截面分位排名 (zvt QuantileScorer 落地)

对以下池子的 picks/top 列表加 rank_pct / rank_no / quantile_tier 字段:
  - data/ban_pick.json      打板池 (p_zt_next/exp_ret_next)
  - data/engine_v5.json     17 指标共振 (top)
  - data/multidim.json      多维引擎 (top)
  - data/lowpool.json       低位池 (strong/watch_list)

不改原分数字段, 只新增排名字段 (additive-only, 符合项目 json_output_additive_only 约定)。

用法:
  python3 apply_rank_to_pools.py            # dry-run
  python3 apply_rank_to_pools.py --write    # 写入
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rank_score import composite_rank  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")

# 文件 → (列表字段, 打分因子, 权重, 方向)
TARGETS = {
    "ban_pick.json": {
        "list_key": "picks",
        "factors": ["p_zt_next", "exp_ret_next", "seal_strength"],
        "weights": {"p_zt_next": 0.5, "exp_ret_next": 0.3, "seal_strength": 0.2},
        "higher": {"p_zt_next": True, "exp_ret_next": True, "seal_strength": True},
        "name_key": "name",
    },
    "engine_v5.json": {
        "list_key": "top",
        "factors": ["score"],
        "weights": {"score": 1.0},
        "higher": {"score": True},
        "name_key": "name",
    },
    "multidim.json": {
        "list_key": "top",
        "factors": ["score"],
        "weights": {"score": 1.0},
        "higher": {"score": True},
        "name_key": "name",
    },
}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and v is not None else None


def process(fname, spec, write=False):
    p = os.path.join(DATA, fname)
    if not os.path.exists(p):
        print(f"  [skip] {fname} 不存在")
        return None
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    rows = d.get(spec["list_key"]) or []
    if not rows:
        print(f"  [skip] {fname}.{spec['list_key']} 为空")
        return None

    # 只保留有打分因子的行
    factors = [k for k in spec["factors"]
               if any(_num(r.get(k)) is not None for r in rows)]
    if not factors:
        print(f"  [skip] {fname} 无可用因子 {spec['factors']}")
        return None
    weights = {k: spec["weights"].get(k, 1.0 / len(factors)) for k in factors}
    higher = {k: spec["higher"].get(k, True) for k in factors}

    n_before = len(rows)
    composite_rank(rows, factors, weights=weights, higher_is_better=higher,
                   out_key="rank_composite")

    # 摘要
    top = sorted(rows, key=lambda r: r.get("rank_no") or 999)[:5]
    print(f"  ✅ {fname}.{spec['list_key']}: {n_before} 只, 因子={factors}")
    for r in top:
        nm = r.get(spec.get("name_key")) or r.get("code")
        print(f"      #{r.get('rank_no')} {nm} comp={r.get('rank_composite')} "
              f"tier={r.get('quantile_tier')}")

    if write:
        d[spec["list_key"]] = rows
        d.setdefault("meta", {})
        d["meta"]["rank_added_at"] = json.dumps({}) and __import__("datetime").datetime.now(
            __import__("datetime").timezone(
                __import__("datetime").timedelta(hours=8))).isoformat(timespec="seconds")
        d["meta"]["rank_method"] = "rank_score.composite_rank (zvt QuantileScorer)"
        d["meta"]["rank_factors"] = factors
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"     [写入] {p}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    print("=== 横截面排名落地 ===")
    for fname, spec in TARGETS.items():
        process(fname, spec, args.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
