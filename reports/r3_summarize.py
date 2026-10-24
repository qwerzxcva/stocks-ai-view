#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R3 汇总: 读 r3_intermediate/eval.json + manifest.json, 产出
reports/phase3_integrated_raw.json。

统计: 整体/分 fold/分 regime 的 T+1、T+5 胜率/超额(pp)/Wilson 95% CI/均值超额;
等权 top10 组合(按 asof 日平均日收益复利)累计收益/最大回撤/夏普 vs 基准。
"""
import json
import math
import os
from collections import defaultdict

AIV = os.path.expanduser("~/workspace/stocks-ai-view")
INT = os.path.join(AIV, "reports", "r3_intermediate")
OUT = os.path.join(AIV, "reports", "phase3_integrated_raw.json")


def wilson(hits, n, z=1.96):
    if n == 0:
        return [None, None]
    p = hits / n
    den = 1 + z * z / n
    ctr = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((ctr - rad) / den * 100, 2), round((ctr + rad) / den * 100, 2)]


def bucket_stats(rows, h, bench):
    """rows: picks 子集; h: 1|5。返回胜率/超额/CI/均值超额。"""
    key = f"ret_t{h}"
    vals, bvals, excess = [], [], []
    hits = bhits = 0
    n = 0
    for p in rows:
        r = p.get(key)
        if r is None:
            continue
        b = bench.get(f"{p['asof']}|{h}")
        if not b:
            continue
        n += 1
        vals.append(r)
        bvals.append(b["mean_ret"])
        excess.append(r - b["mean_ret"])
        hits += r > 0
        bhits += b["win_rate"]
    if not n:
        return None
    wr = hits / n * 100
    bwr = bhits / n
    return {
        "n": n,
        "win_rate": round(wr, 2),
        "wilson_ci": wilson(hits, n),
        "bench_win_rate": round(bwr, 2),
        "excess_pp": round(wr - bwr, 2),
        "mean_ret": round(sum(vals) / n, 3),
        "bench_mean_ret": round(sum(bvals) / n, 3),
        "mean_excess_pp": round(sum(excess) / n, 3),
    }


def portfolio(rows_by_day, h, bench):
    """等权 top10 按日复利 (每个 asof 日的组合 h 日收益=当日 picks 均值)。"""
    key = f"ret_t{h}"
    curve, bcurve, dd = 1.0, 1.0, 0.0
    peak = bpeak = 1.0
    daily, bdaily = [], []
    days = sorted(rows_by_day)
    for d in days:
        rets = [p[key] for p in rows_by_day[d] if p.get(key) is not None]
        if not rets:
            continue
        r = sum(rets) / len(rets) / 100
        b = (bench.get(f"{d}|{h}") or {}).get("mean_ret")
        curve *= 1 + r
        daily.append(r)
        if b is not None:
            bcurve *= 1 + b / 100
            bdaily.append(b / 100)
        peak = max(peak, curve)
        bpeak = max(bpeak, bcurve)
        dd = min(dd, curve / peak - 1)
        bdd = min(bpeak and (bcurve / bpeak - 1) or 0, 0)
    n = len(daily)
    if not n:
        return None

    def sharpe(rs):
        if len(rs) < 2:
            return None
        m = sum(rs) / len(rs)
        v = sum((x - m) ** 2 for x in rs) / (len(rs) - 1)
        return round(m / math.sqrt(v) * math.sqrt(250 / h), 2) if v > 0 else None

    # 组合最大回撤重算
    c, pk, mdd = 1.0, 1.0, 0.0
    for r in daily:
        c *= 1 + r
        pk = max(pk, c)
        mdd = min(mdd, c / pk - 1)
    bc, bpk, bmdd = 1.0, 1.0, 0.0
    for r in bdaily:
        bc *= 1 + r
        bpk = max(bpk, bc)
        bmdd = min(bmdd, bc / bpk - 1)
    return {
        "n_periods": n,
        "cum_ret_pct": round((math.prod(1 + r for r in daily) - 1) * 100, 2),
        "bench_cum_ret_pct": round((math.prod(1 + r for r in bdaily) - 1) * 100,
                                   2) if bdaily else None,
        "max_drawdown_pct": round(mdd * 100, 2),
        "bench_max_drawdown_pct": round(bmdd * 100, 2) if bdaily else None,
        "sharpe": sharpe(daily),
        "bench_sharpe": sharpe(bdaily),
    }


def main():
    ev = json.load(open(os.path.join(INT, "eval.json"), encoding="utf-8"))
    man = json.load(open(os.path.join(INT, "manifest.json"), encoding="utf-8"))
    picks = ev["picks"]
    bench = ev["bench"]

    by_fold = defaultdict(list)
    by_regime = defaultdict(list)
    by_day = defaultdict(list)
    for p in picks:
        by_fold[p["fold"]].append(p)
        by_regime[p.get("regime") or "unknown"].append(p)
        by_day[p["asof"]].append(p)

    out = {
        "engine": "r3_summarize.py — Phase3 R3 集成回测汇总",
        "convention": ev["convention"],
        "data_quality": ev["data_quality"],
        "dropped": ev["dropped"],
        "folds": {y: {k: v for k, v in f.items() if k != "asofs_done"}
                  for y, f in man["folds"].items()},
        "llm_mode": {},
        "overall": {},
        "by_fold": {},
        "by_regime": {},
        "portfolio": {},
    }
    # llm 模式统计
    llm_modes = defaultdict(int)
    for p in picks:
        llm_modes[p.get("llm") or "none"] += 1
    out["llm_mode"] = dict(llm_modes)

    for h in (1, 5):
        out["overall"][f"t{h}"] = bucket_stats(picks, h, bench)
    for y, rows in sorted(by_fold.items()):
        out["by_fold"][y] = {f"t{h}": bucket_stats(rows, h, bench)
                             for h in (1, 5)}
    for rg, rows in sorted(by_regime.items()):
        out["by_regime"][rg] = {f"t{h}": bucket_stats(rows, h, bench)
                                for h in (1, 5)}
    for h in (1, 5):
        out["portfolio"][f"t{h}"] = portfolio(by_day, h, bench)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("->", OUT)
    for h in (1, 5):
        o = out["overall"][f"t{h}"]
        print(f"T+{h}: n={o['n']} win={o['win_rate']}% bench={o['bench_win_rate']}% "
              f"excess={o['excess_pp']}pp CI={o['wilson_ci']} "
              f"mean_excess={o['mean_excess_pp']}pp")


if __name__ == "__main__":
    main()
