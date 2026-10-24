#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 基线复现 final: 单遍流式, 低内存。

一条流完成两件事, 避免常驻双份全市场 K 线 (16GB 容器, 上次双载被 OOM):
  Pass A: 主分片 (00-09,20,30,60,68, 4019只, 单一复权基准) → 基准表
          {h: {exit_date: [up,total]}} 与 全市场K线(仅 close) —— 约 1.2GB
  Pass B: 辅助分片 (expand/extra/recent/stockdb) 流式补齐主分片缺失的
          (code,date), 只对预测样本出现的代码保留 → reflect 口径样本视图
  然后: 656 个预测快照逐条验证 (T收盘→T+N收盘, 方向命中), 分段聚合,
        基准按样本 exit_date 从主分片基准表加权。

口径与 scripts/reflect_engine.py 完全对齐 (T收盘→T+N收盘, Wilson 95% CI),
唯二修正:
  1. (code,date) 多片重复 → 主分片优先 (reflect 原实现是文件序 first-wins,
     辅助片的异基准复权价会插进序列中段, 使跨缝收益失真);
  2. 基准只取主分片 (辅助片复权基准不一致, 无脑并入会把每日上涨比例
     刷到 ~80% 的物理不可能值)。
输出: reports/phase3_baseline_raw.json (分段汇总, 无逐样本明细)
"""
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/root/workspace/stocks-ai-view/scripts")
from reflect_engine import wilson_ci, load_phase_snapshots, match_phase_for  # noqa

BULK = "/root/workspace/stocks-data-kline/data/bulk"
PRED_DIR = "/root/workspace/stocks-ai-view/data/predictions"
INDEX_FILE = "/root/workspace/stocks-data-kline/data/index/history/sh000001.txt"
OUT = "/root/workspace/stocks-ai-view/reports/phase3_baseline_raw.json"
MAIN = tuple(f"day_qfq_{s}.csv" for s in
             ("00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
              "20", "30", "60", "68"))
AUX = ("day_qfq_expand.csv", "day_qfq_extra.csv",
       "day_qfq_recent.csv", "day_qfq_stockdb.csv")
HORIZONS = (1, 3, 5, 10)
t0 = datetime.now()


def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#") or "|" not in line:
        return None
    code, rest = line.split("|", 1)
    parts = rest.split("|") if "|" in rest else rest.split(",")
    if len(parts) < 3:
        return None
    try:
        return code, parts[0], float(parts[2])
    except ValueError:
        return None


# ── 预测样本的代码集合 (轻量预扫) ──
pred_files = sorted(glob.glob(os.path.join(PRED_DIR, "engine_v5*.json")))
sample_codes = set()
for pf in pred_files:
    try:
        d = json.load(open(pf, encoding="utf-8"))
    except Exception:
        continue
    for r in (d.get("top") or []):
        c = r.get("code")
        if c:
            sample_codes.add(c)
print(f"[f] 预测文件 {len(pred_files)} 个, 样本代码 {len(sample_codes)} 只",
      flush=True)

# ── Pass A: 主分片 → 全市场K线(主基准) + 基准表 ──
kl = {}            # code -> sorted [(date, close)]  主流域 (也是样本主体)
bench = {h: {} for h in HORIZONS}
for fn in MAIN:
    with open(os.path.join(BULK, fn), encoding="utf-8", errors="replace") as f:
        for line in f:
            pr = parse_line(line)
            if not pr:
                continue
            code, d, c = pr
            kl.setdefault(code, []).append((d, c))
dup_in_main = 0
for code, bars in kl.items():
    bars.sort()
    # 主片内部同 (code,date) 去重 (同值重复为主, first-wins)
    out = []
    seen = set()
    for d, c in bars:
        if d in seen:
            dup_in_main += 1
            continue
        seen.add(d)
        out.append((d, c))
    kl[code] = out
    n = len(out)
    for h in HORIZONS:
        bh = bench[h]
        for i in range(n - h):
            e = out[i][1]
            if e <= 0:
                continue
            cell = bh.get(out[i + h][0])
            up = 1 if out[i + h][1] > e else 0
            if cell is None:
                bh[out[i + h][0]] = [up, 1]
            else:
                cell[0] += up
                cell[1] += 1
print(f"[f] 主流域 {len(kl)} 只, 主片内重复 {dup_in_main} 行, 基准表完成 "
      f"({datetime.now()-t0})", flush=True)

# ── Pass B: 辅助片流式补齐主片缺失的样本代码 (code,date) ──
aux_add = 0
aux_conflict_skip = 0
for fn in AUX:
    p = os.path.join(BULK, fn)
    if not os.path.exists(p):
        continue
    touched = {}
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            pr = parse_line(line)
            if not pr:
                continue
            code, d, c = pr
            if code not in sample_codes:
                continue
            touched.setdefault(code, {})[d] = c
    for code, dd in touched.items():
        if code not in kl:
            kl[code] = sorted(dd.items())
            aux_add += len(dd)
            continue
        have = {x[0] for x in kl[code]}
        new = [(d, c) for d, c in dd.items() if d not in have]
        aux_conflict_skip += len(dd) - len(new)
        if new:
            kl[code] = sorted(kl[code] + new)
            aux_add += len(new)
print(f"[f] 辅助片补齐 {aux_add} 行 (跳过与主片冲突 {aux_conflict_skip} 行) "
      f"({datetime.now()-t0})", flush=True)

kidx = {c: {d: i for i, (d, _) in enumerate(v)} for c, v in kl.items()}

# ── 指数 MA250 regime ──
ibars = []
with open(INDEX_FILE, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = line.split("|")
        try:
            ibars.append((parts[1], float(parts[3])))
        except (ValueError, IndexError):
            continue
ibars.sort()
idx_dates = [d for d, _ in ibars]
idx_regime = {}
ic = [c for _, c in ibars]
for i, (d, c) in enumerate(ibars):
    idx_regime[d] = None if i + 1 < 250 else (
        "bull" if c >= sum(ic[i - 249:i + 1]) / 250 else "bear")

def regime_at(pred_date):
    lo, hi, ans = 0, len(idx_dates) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if idx_dates[mid] <= pred_date:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return idx_regime[idx_dates[ans]] if ans >= 0 else None

phase_map, phase_sources = load_phase_snapshots(
    "/root/workspace/stocks-ai-view/data")

# ── 样本验证与分段聚合 ──
agg = {h: defaultdict(lambda: [0, 0]) for h in HORIZONS}   # [hits, n]
bagg = {h: defaultdict(lambda: [0, 0]) for h in HORIZONS}  # 基准 [up, total]
dropped = defaultdict(int)
n_records = 0
pred_dates = []

for fi, pf in enumerate(pred_files):
    if fi % 100 == 0:
        print(f"[f] {fi}/{len(pred_files)} ({datetime.now()-t0})", flush=True)
    try:
        d = json.load(open(pf, encoding="utf-8"))
    except Exception:
        continue
    top = d.get("top") or []
    pred_date = top[0].get("date") if top else d.get("ts", "")[:10]
    if not pred_date:
        continue
    pred_dates.append(pred_date)
    ph_label, _ = match_phase_for(pred_date, phase_map)
    jx = regime_at(pred_date)
    year = pred_date[:4]
    for r in top:
        code = r.get("code")
        bars = kl.get(code)
        if not bars:
            dropped["no_kline"] += 1
            continue
        idx = kidx[code]
        if pred_date in idx:
            i = idx[pred_date]
        else:
            lo_i = None
            for dd, _ in bars:
                if dd >= pred_date:
                    lo_i = idx[dd]
                    break
            if lo_i is None:
                dropped["no_bars_at_pred"] += 1
                continue
            i = lo_i
        n_records += 1
        bull, bear = int(r.get("bull") or 0), int(r.get("bear") or 0)
        direction = ("bull" if bull > bear else
                     "bear" if bear > bull else "neutral")
        for h in HORIZONS:
            j = i + h
            if j >= len(bars):
                dropped[f"no_forward_{h}d"] += 1
                continue
            entry, exit_ = bars[i][1], bars[j][1]
            if entry <= 0:
                dropped["bad_price"] += 1
                continue
            up = exit_ > entry
            xd = bars[j][0]
            bcell = bench[h].get(xd)
            keys = ["overall", f"dir_{direction}", f"phase_{ph_label}",
                    f"year_{year}"] + ([f"ma250_{jx}"] if jx else [])
            for k in keys:
                hit = up if k == "overall" else (
                    up if direction == "bull" else
                    (not up if direction == "bear" else up))
                cell = agg[h][k]
                cell[0] += 1 if hit else 0
                cell[1] += 1
                if bcell is not None:
                    bc = bagg[h][k]
                    bc[0] += bcell[0]
                    bc[1] += bcell[1]


def pack(h, k):
    cell = agg[h].get(k)
    if not cell or not cell[1]:
        return None
    ci = wilson_ci(cell[0], cell[1])
    out = {"n": cell[1], "win_rate": round(cell[0] / cell[1] * 100, 2),
           "wilson_ci": [ci[0], ci[1]]}
    bc = bagg[h].get(k)
    if bc and bc[1]:
        br = bc[0] / bc[1] * 100
        out["benchmark_pct"] = round(br, 2)
        out["excess_pp"] = round(out["win_rate"] - br, 2)
    return out


result = {
    "ts": datetime.now().isoformat(timespec="seconds"),
    "engine": "phase3_baseline_final.py (reflect_engine 口径复现 + 修正基准)",
    "windows": {
        "pred_dates": [min(pred_dates), max(pred_dates)] if pred_dates else None,
        "n_pred_files": len(pred_files),
        "n_records": n_records,
        "kline_max_date": max(b[0] for v in kl.values() for b in v[-1:]),
        "index_max_date": idx_dates[-1],
    },
    "method": {
        "return": "T 收盘 → T+N 收盘 (reflect_engine.verify_prediction:176-191)",
        "hit": "看多(bull>bear)且上涨 / 看空(bear>bull)且下跌; 平盘不命中; "
               "overall=全样本上涨占比 (reflect raw_win_rate 口径)",
        "ci": "Wilson 95% (reflect_engine.wilson_ci:33)",
        "dedup": "(code,date) 多片重复 → 主分片(00-09/20/30/60/68)优先, "
                 "辅助片(expand/extra/recent/stockdb)仅补主片缺失; "
                 "修正 reflect 原实现的文件序 first-wins 混基准缺陷",
        "benchmark": "同期全体主分片股票同窗口(T收盘→T+N收盘)上涨比例, "
                     "按样本 exit_date 加权",
        "ma250_regime": "上证综指收盘 vs MA250 (sh000001.txt, 至 "
                        f"{idx_dates[-1]}); >=MA250=bull 否则 bear",
        "phase_snapshot": f"latest.json/market_now.json 相位快照 "
                          f"({len(phase_map)} 条, 来源 {phase_sources}); "
                          "仅 1 条快照, 全部样本被代理为同一标签, 无分段价值",
    },
    "data_quality": {
        "main_shard_codes": len(kl),
        "aux_rows_merged": aux_add,
        "aux_conflict_rows_skipped": aux_conflict_skip,
        "dup_within_main_shards": dup_in_main,
        "cross_shard_basis_conflict_example":
            "sh600519 2024-06-11 close: 主片(01/07) 1434.661 vs "
            "expand 1569.000 (差 9.1%); sz000021 同日 主片(00) 14.960 "
            "vs stockdb 15.500; 1日收益差 0.9pp",
    },
    "dropped": dict(dropped),
    "horizons": {},
}
for h in HORIZONS:
    hz = {"overall_raw": pack(h, "overall"),
          "dir_bull": pack(h, "dir_bull"),
          "dir_bear": pack(h, "dir_bear"),
          "ma250": {k.replace("ma250_", ""): pack(h, k)
                    for k in agg[h] if k.startswith("ma250_")},
          "phase_snapshot": {k.replace("phase_", ""): pack(h, k)
                             for k in agg[h] if k.startswith("phase_")},
          "by_year": {k.replace("year_", ""): pack(h, k)
                      for k in agg[h] if k.startswith("year_")}}
    result["horizons"][str(h)] = hz

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print(f"[f] -> {OUT} 总耗时 {datetime.now()-t0}", flush=True)
for h in HORIZONS:
    hz = result["horizons"][str(h)]
    o, b, s = hz["overall_raw"], hz["dir_bull"], hz["dir_bear"]
    print(f"h={h}d raw={o['win_rate']}%(n={o['n']},基准{o.get('benchmark_pct')}%) "
          f"bull={b['win_rate']}%(n={b['n']}) bear={s['win_rate']}%"
          f"(n={s['n']})", flush=True)
