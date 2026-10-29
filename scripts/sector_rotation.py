#!/usr/bin/env python3
"""sector_rotation.py — 行业轮动增强（v1）。

## 与 rps_rotation.py 的区别

`rps_rotation` 是**个股**相对强度（rank pct 的 250/120/60/20 均值）。
它回答「谁强」，但不回答「强的是哪个行业、这个行业在扩散还是在退潮」。
板块层面的判断此前散在 `concept_strength.py` / `build_sectors_health.py` /
`recent_mainlines.py` 三处，阈值各不相同，且都不给统一 state。

本引擎只做一件事：把行业映射 + 个股动量聚合成**行业级 RPS 与状态**，
状态机定义写在这里，别处引用时不再自造阈值。

## 口径

    sector_ret_w = mean(成分股 ret_w)（w ∈ {5,20,60}）
    sector_rps   = rank_pct(sector_ret_20) * 100      # 横截面百分位
    breadth      = 成分股中 ret_5 > 0 的占比
    state        = bull   if rps >= state_bull and breadth >= 0.6
                   bear   if rps <= state_bear and breadth <= 0.4
                   neutral 其余
    leaders      = 成分股按 ret_5 排序取前 3
"""
import argparse
import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import limit_caliber as LC  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
SPEC = os.path.join(DATA, "engine_spec.json")
VERSION = "sector_rotation_v1"
DEFAULT_PARAMS = {"windows": [5, 20, 60], "state_bull": 60.0, "state_bear": 40.0,
                  "min_members": 3, "leaders": 3}


def load_params():
    try:
        with open(SPEC, encoding="utf-8") as f:
            spec = json.load(f)
        for p in spec.get("products") or []:
            if p.get("id") == "sector_rotation":
                return {**DEFAULT_PARAMS, **(p.get("params") or {})}, spec.get("spec_version")
    except Exception:
        pass
    return DEFAULT_PARAMS, None


def load_industry_map():
    """行业映射：优先 data/industry_map_em.json，回退 industry_stocks.json。"""
    for name in ("industry_map_em.json", "industry_stocks.json"):
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        m = {}
        if not isinstance(raw, dict):
            continue
        # shape A: industry_map_em.json → {"map": {code: industry}}
        if isinstance(raw.get("map"), dict):
            for c, sec in raw["map"].items():
                if isinstance(sec, str) and sec:
                    m[LC.bare(str(c))] = sec
            if m:
                return m
        # shape B: industry_stocks.json → {"industries": {industry: [codes]}}
        if isinstance(raw.get("industries"), dict):
            for sec, codes in raw["industries"].items():
                if isinstance(codes, list):
                    for c in codes:
                        m[LC.bare(str(c))] = sec
            if m:
                return m
    return {}


def read_closes(bulk_dir, max_codes=4200, tail=70):
    out = {}
    for path in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 7:
                    continue
                code = LC.bare(p[0])
                if not LC.is_main_board(code):
                    continue
                try:
                    out.setdefault(code, []).append((p[1], float(p[2])))
                except ValueError:
                    continue
        if len(out) >= max_codes:
            break
    return {c: r[-tail:] for c, r in out.items() if len(r) >= tail // 2}


def ret_n(rows, n):
    if len(rows) <= n:
        return None
    prev, last = rows[-1 - n][1], rows[-1][1]
    return (last / prev - 1) if prev > 0 else None


def main():
    ap = argparse.ArgumentParser(description="行业轮动增强")
    ap.add_argument("--bulk", default=None)
    ap.add_argument("--out", default=os.path.join(DATA, "sector_rotation.json"))
    ap.add_argument("--limit", type=int, default=4200)
    args = ap.parse_args()

    params, spec_ver = load_params()
    imap = load_industry_map()
    if not imap:
        print("[sector_rotation] 无行业映射，跳过（需要 data/industry_map_em.json）", file=sys.stderr)
        return 2
    bulk = args.bulk or os.path.normpath(os.path.join(HERE, "..", "..", "stocks-data-kline", "data", "bulk"))
    closes = read_closes(bulk, args.limit)

    per_sector = defaultdict(list)
    for code, rows in closes.items():
        sec = imap.get(code)
        if not sec:
            continue
        r5, r20, r60 = ret_n(rows, 5), ret_n(rows, 20), ret_n(rows, 60)
        if r5 is None or r20 is None:
            continue
        per_sector[sec].append({"code": code, "ret5": r5, "ret20": r20,
                                "ret60": r60 if r60 is not None else 0.0,
                                "price": rows[-1][1]})

    rows = []
    for sec, members in per_sector.items():
        if len(members) < params["min_members"]:
            continue
        rows.append({"sector": sec, "n": len(members),
                     "ret5": statistics.fmean(m["ret5"] for m in members),
                     "ret20": statistics.fmean(m["ret20"] for m in members),
                     "ret60": statistics.fmean(m["ret60"] for m in members),
                     "breadth": sum(1 for m in members if m["ret5"] > 0) / len(members),
                     "_members": members})
    if not rows:
        print("[sector_rotation] 无足够成分股的行业", file=sys.stderr)
        return 1

    order = sorted(range(len(rows)), key=lambda i: rows[i]["ret20"])
    for rank, i in enumerate(order):
        rows[i]["rps"] = round((rank + 1) / len(rows) * 100, 1)
    for r in rows:
        if r["rps"] >= params["state_bull"] and r["breadth"] >= 0.6:
            r["state"] = "bull"
        elif r["rps"] <= params["state_bear"] and r["breadth"] <= 0.4:
            r["state"] = "bear"
        else:
            r["state"] = "neutral"

    rows.sort(key=lambda r: -r["rps"])
    sectors, leaders = [], []
    for r in rows:
        top = sorted(r.pop("_members"), key=lambda m: -m["ret5"])[: params["leaders"]]
        sectors.append({k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in r.items()})
        leaders.append({"sector": r["sector"], "state": r["state"],
                        "codes": [m["code"] for m in top]})

    out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
           "engine": VERSION, "spec_version": spec_ver,
           "sectors": sectors, "leaders": leaders,
           "meta": {"params": params, "n_sectors": len(sectors),
                    "formula": "rps=rank_pct(sector_ret20)*100; state=首两个条件同时满足才给方向"}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    bull = [s["sector"] for s in sectors if s["state"] == "bull"]
    print(f"[sector_rotation] {len(sectors)} 个行业，强势 {len(bull)}: {bull[:5]} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
