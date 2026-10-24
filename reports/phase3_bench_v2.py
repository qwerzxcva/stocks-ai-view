#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 基线复现 v2: reflect_engine 口径 + 修正版全市场基准。

与 v1 (phase3_baseline_repro.py) 的差异:
  - 胜率计算完全不变 (reflect 同口径, 结果已复现, 不重算)。
  - 基准改为「主流域」: 分片 00-09/20/30/60/68 (4162 只, 单一复权基准,
    覆盖至 2026-10-16), 剔除 expand/extra/recent/stockdb 辅助片
    (复权基准与主片不一致: sh600519 2024-06-11 主片 1434.66 vs expand 1569.00;
    sz000021 主片 14.96 vs stockdb 15.50)。
输出: reports/phase3_bench_v2.json
"""
import glob
import json
import os
import sys
from collections import defaultdict

BULK = "/root/workspace/stocks-data-kline/data/bulk"
MAIN_SHARDS = [f"day_qfq_{s}.csv" for s in
               ("00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
                "20", "30", "60", "68")]
HORIZONS = (1, 3, 5, 10)

# ── 主分片加载 (reflect_engine.load_klines 同解析) ──
kl = {}
for fn in MAIN_SHARDS:
    p = os.path.join(BULK, fn)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            code, rest = line.split("|", 1)
            parts = rest.split("|") if "|" in rest else rest.split(",")
            if len(parts) < 3:
                continue
            try:
                kl.setdefault(code, []).append((parts[0], float(parts[2])))
            except ValueError:
                continue
for c in kl:
    kl[c].sort()
print(f"[v2] 主流域 {len(kl)} 只", flush=True)

# ── 基准: {h: {exit_date: [up, total]}} ──
bench = {h: {} for h in HORIZONS}
for code, bars in kl.items():
    n = len(bars)
    for h in HORIZONS:
        bh = bench[h]
        for i in range(n - h):
            entry = bars[i][1]
            if entry <= 0:
                continue
            cell = bh.get(bars[i + h][0])
            up = 1 if bars[i + h][1] > entry else 0
            if cell is None:
                bh[bars[i + h][0]] = [up, 1]
            else:
                cell[0] += up
                cell[1] += 1

# ── 复用 v1 的分段结果, 按 exit_date 重新聚合基准 ──
# 需要样本的 (pred_date, code, horizon) -> exit_date; 重新摊一遍预测文件 (轻量, 只取 close)
sys.path.insert(0, "/root/workspace/stocks-ai-view/scripts")
from reflect_engine import load_klines as load_all  # noqa: E402

allk = load_all(BULK)   # reflect 原口径 (含全部片, first-wins)
kidx = {c: {b[0]: i for i, b in enumerate(v)} for c, v in allk.items()}
pred_files = sorted(glob.glob(
    "/root/workspace/stocks-ai-view/data/predictions/engine_v5*.json"))

bagg = {h: defaultdict(lambda: [0, 0]) for h in HORIZONS}
for pf in pred_files:
    try:
        d = json.load(open(pf, encoding="utf-8"))
    except Exception:
        continue
    top = d.get("top") or []
    pred_date = top[0].get("date") if top else d.get("ts", "")[:10]
    if not pred_date:
        continue
    for r in top:
        code = r.get("code")
        bars = allk.get(code)
        if not bars:
            continue
        idx = kidx[code]
        if pred_date in idx:
            i = idx[pred_date]
        else:
            cands = [k for k in idx if k >= pred_date]
            if not cands:
                continue
            i = idx[min(cands)]
        bull, bear = int(r.get("bull") or 0), int(r.get("bear") or 0)
        if bull > bear:
            direction = "bull"
        elif bear > bull:
            direction = "bear"
        else:
            direction = "neutral"
        year = pred_date[:4]
        # MA250 regime 与 v1 一致, 从 raw json 里复用会复杂, 直接重算指数映射
        for h in HORIZONS:
            j = i + h
            if j >= len(bars) or bars[i][2] <= 0:
                continue
            xd = bars[j][0]
            bc = bench[h].get(xd)
            if bc is None:
                continue
            for key in ("overall", f"dir_{direction}", f"year_{year}"):
                cell = bagg[h][key]
                cell[0] += bc[0]
                cell[1] += bc[1]

out = {"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
       "universe": {"main_shards": MAIN_SHARDS, "n_codes": len(kl)},
       "benchmark": {}}
for h in HORIZONS:
    out["benchmark"][str(h)] = {
        k: {"n_pairs": v[1], "pct": round(v[0] / v[1] * 100, 2)}
        for k, v in sorted(bagg[h].items())}
with open("/root/workspace/stocks-ai-view/reports/phase3_bench_v2.json",
          "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("[v2] -> reports/phase3_bench_v2.json", flush=True)
for h in HORIZONS:
    b = out["benchmark"][str(h)]
    print(f"h={h}d overall基准={b['overall']['pct']}% "
          f"bull={b['dir_bull']['pct']}% bear={b['dir_bear']['pct']}%", flush=True)
