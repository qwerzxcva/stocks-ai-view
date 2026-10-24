#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 基线复现: reflect_engine 同口径全量前向验证 (一次性脚本)。

口径完全对齐 scripts/reflect_engine.py:
  - 样本: data/predictions/engine_v5_*.json 全部 top 记录
  - 收益: T 日收盘 -> T+N 日收盘 (verify_prediction, reflect_engine.py:176-191)
  - 命中: 看多(bull>bear)且上涨 / 看空(bear>bull)且下跌 / 中性剔除
  - CI: Wilson 95% (reflect_engine.py:33)
新增 (reflect 未做的): 基准=同期全体股票同向上涨比例 (同 N 个交易日窗口),
分年度胜率, 以及 MA250 牛熊分段 (指数收盘 vs MA250, 补足历史相位快照只有 1 条的缺口)。
输出: reports/phase3_baseline_raw.json
"""
import glob
import json
import math
import os
import sys
from datetime import datetime

HERE = "/root/workspace/stocks-ai-view/scripts"
sys.path.insert(0, HERE)
from reflect_engine import (  # noqa: E402
    load_klines, load_phase_snapshots, match_phase_for, wilson_ci)

PRED_DIR = "/root/workspace/stocks-ai-view/data/predictions"
BULK_DIR = "/root/workspace/stocks-data-kline/data/bulk"
INDEX_FILE = "/root/workspace/stocks-data-kline/data/index/history/sh000001.txt"
OUT = "/root/workspace/stocks-ai-view/reports/phase3_baseline_raw.json"
HORIZONS = (1, 3, 5, 10)


def load_index_ma250(path, ma_n=250):
    """指数日线 -> {date: regime('bull'/'bear')}, 收盘 vs MA250 (不足250根记 None)。"""
    bars = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = line.split("|")
            try:
                bars.append((parts[1], float(parts[3])))  # date, close
            except (ValueError, IndexError):
                continue
    bars.sort()
    regime = {}
    closes = [c for _, c in bars]
    for i, (d, c) in enumerate(bars):
        if i + 1 < ma_n:
            regime[d] = None
        else:
            ma = sum(closes[i - ma_n + 1:i + 1]) / ma_n
            regime[d] = "bull" if c >= ma else "bear"
    return regime


def pct(x, nd=2):
    return round(x, nd) if x is not None else None


def stat_block(hits, n):
    if not n:
        return {"n": 0}
    wr = hits / n * 100
    ci = wilson_ci(hits, n)
    return {"n": n, "win_rate": round(wr, 2),
            "wilson_ci": [ci[0], ci[1]]}


def main():
    t0 = datetime.now()
    print("[p3] 加载指数 MA250 regime ...", flush=True)
    idx_regime = load_index_ma250(INDEX_FILE)
    idx_dates = sorted(idx_regime)
    print(f"[p3] 指数 {len(idx_dates)} 根 ({idx_dates[0]}~{idx_dates[-1]})", flush=True)

    print("[p3] 加载 bulk K线 ...", flush=True)
    klines = load_klines(BULK_DIR)
    print(f"[p3] K线 {len(klines)} 只", flush=True)

    print("[p3] 加载历史相位快照 ...", flush=True)
    phase_map, phase_sources = load_phase_snapshots(
        "/root/workspace/stocks-ai-view/data")
    print(f"[p3] 相位快照 {len(phase_map)} 条", flush=True)

    # 全市场每日上涨比例基准: {horizon: {exit_date: [n_up, n_total]}}
    bench = {h: {} for h in HORIZONS}
    print("[p3] 计算全市场基准 (同期同窗口上涨比例) ...", flush=True)
    for code, bars in klines.items():
        dates = [b[0] for b in bars]
        closes = [b[2] for b in bars]
        n = len(bars)
        for h in HORIZONS:
            bh = bench[h]
            for i in range(n - h):
                entry = closes[i]
                if entry <= 0:
                    continue
                xd = dates[i + h]
                up = 1 if closes[i + h] > entry else 0
                cell = bh.get(xd)
                if cell is None:
                    bh[xd] = [up, 1]
                else:
                    cell[0] += up
                    cell[1] += 1
    print("[p3] 基准完成", flush=True)

    # 样本: 逐预测文件, reflect_engine 同口径, 但方向/命中/基准对齐到样本级
    pred_files = sorted(glob.glob(os.path.join(PRED_DIR, "engine_v5*.json")))
    print(f"[p3] 预测文件 {len(pred_files)} 个, 逐文件验证 ...", flush=True)
    kidx = {c: {b[0]: i for i, b in enumerate(v)} for c, v in klines.items()}

    # agg[horizon][seg_key] = [hits, n]; seg_key: overall/bull_dir/bear_dir/phase_*/ma250_*/year_*
    agg = {h: {} for h in HORIZONS}
    # bench_agg[horizon][seg_key] = [up, total]  (与样本同 exit_date 的全市场基准)
    bagg = {h: {} for h in HORIZONS}
    dropped = {"no_kline": 0, "no_bars_at_pred": 0, "no_forward": 0,
               "bad_price": 0, "neutral": 0}
    n_records = 0
    pred_dates = []

    def bump(d, h, key, hit, bcell):
        cell = d[h].get(key)
        if cell is None:
            d[h][key] = [hit, 1]
        else:
            cell[0] += hit
            cell[1] += 1
        if bcell is not None:
            bc = bagg[h].get(key)
            if bc is None:
                bagg[h][key] = [bcell[0], bcell[1]]
            else:
                bc[0] += bcell[0]
                bc[1] += bcell[1]

    for fi, pf in enumerate(pred_files):
        if fi % 100 == 0:
            print(f"[p3] {fi}/{len(pred_files)} ...", flush=True)
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
        year = pred_date[:4]
        for r in top:
            code = r.get("code")
            bars = klines.get(code)
            if not bars:
                dropped["no_kline"] += 1
                continue
            idx = kidx[code]
            if pred_date in idx:
                i = idx[pred_date]
            else:
                cands = [k for k in idx if k >= pred_date]
                if not cands:
                    dropped["no_bars_at_pred"] += 1
                    continue
                i = idx[min(cands)]
            n_records += 1
            bull, bear = int(r.get("bull") or 0), int(r.get("bear") or 0)
            if bull > bear:
                direction = "bull"
            elif bear > bull:
                direction = "bear"
            else:
                direction = "neutral"
            # MA250 regime: 取 pred_date 当日或之前最近一个指数交易日
            jx = None
            lo, hi = 0, len(idx_dates) - 1
            # 二分: 最大的 <= pred_date 的指数日期
            ans = -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if idx_dates[mid] <= pred_date:
                    ans = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if ans >= 0:
                jx = idx_regime[idx_dates[ans]]
            for h in HORIZONS:
                j = i + h
                if j >= len(bars):
                    if h == HORIZONS[-1]:
                        dropped["no_forward"] += 1
                    continue
                entry = bars[i][2]
                exit_ = bars[j][2]
                if entry <= 0:
                    dropped["bad_price"] += 1
                    continue
                up = exit_ > entry
                if direction == "neutral":
                    dropped["neutral"] += 1
                    # 中性仍计入 raw? 与 reflect 一致: overall n 含全部 valid,
                    # 但命中只按方向计。reflect 的 raw_win_rate 分母含中性,
                    # 而 bull/bear 分开。此处为复现 raw_win_rate, 中性计入
                    # overall 的 n, 命中=上涨。
                    bcell = bench[h].get(bars[j][0])
                    bump(agg, h, "overall", 1 if up else 0, bcell)
                    continue
                hit = up if direction == "bull" else (not up)
                if direction == "bull" and not up and exit_ == entry:
                    hit = False  # 平盘: 看多未命中
                if direction == "bear" and exit_ == entry:
                    hit = False  # 平盘: 看空未命中
                xd = bars[j][0]
                bcell = bench[h].get(xd)
                bump(agg, h, "overall", 1 if up else 0, bcell)
                bump(agg, h, f"dir_{direction}", 1 if hit else 0, bcell)
                bump(agg, h, f"phase_{ph_label}", 1 if hit else 0, bcell)
                if jx is not None:
                    bump(agg, h, f"ma250_{jx}", 1 if hit else 0, bcell)
                bump(agg, h, f"year_{year}", 1 if hit else 0, bcell)

    print(f"[p3] 验证完成 n_records={n_records} dropped={dropped}", flush=True)

    def seg_pack(h, key):
        cell = agg[h].get(key)
        if not cell:
            return None
        out = stat_block(cell[0], cell[1])
        bc = bagg[h].get(key)
        if bc and bc[1]:
            br = bc[0] / bc[1] * 100
            out["benchmark_pct"] = round(br, 2)
            out["excess_pp"] = round(out["win_rate"] - br, 2)
            out["benchmark_note"] = "同期全体股票同窗口(T收盘→T+N收盘)上涨比例, 按样本exit_date加权"
        return out

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "engine": "phase3_baseline_repro.py (复现 reflect_engine.py 口径)",
        "windows": {
            "pred_dates": [min(pred_dates), max(pred_dates)] if pred_dates else None,
            "n_pred_files": len(pred_files),
            "n_records": n_records,
        },
        "method": {
            "return": "T 收盘 → T+N 收盘 (reflect_engine.verify_prediction:176-191)",
            "hit": "看多(bull>bear)且上涨 / 看空(bear>bull)且下跌; 平盘不算命中; 中性只计入 overall raw 口径",
            "ci": "Wilson 95% (reflect_engine.wilson_ci:33)",
            "ma250_regime": "上证综指收盘 vs MA250 (sh000001.txt, 至2026-10-09); >=MA250=bull 否则 bear; pred_date 取当日或之前最近指数交易日",
            "phase_snapshot": f"latest.json/market_now.json 历史相位快照 ({len(phase_map)} 条, 来源 {phase_sources}); 除最新一天外全部代理为最新标签, 参考价值极低",
        },
        "dropped": dropped,
        "horizons": {},
    }
    for h in HORIZONS:
        hz = {
            "overall_raw": seg_pack(h, "overall"),
            "dir_bull": seg_pack(h, "dir_bull"),
            "dir_bear": seg_pack(h, "dir_bear"),
            "ma250": {k.replace("ma250_", ""): seg_pack(h, k)
                      for k in agg[h] if k.startswith("ma250_")},
            "phase_snapshot": {k.replace("phase_", ""): seg_pack(h, k)
                               for k in agg[h] if k.startswith("phase_")},
            "by_year": {k.replace("year_", ""): seg_pack(h, k)
                        for k in agg[h] if k.startswith("year_")},
        }
        result["horizons"][str(h)] = hz

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"[p3] -> {OUT}  耗时 {datetime.now()-t0}", flush=True)
    # 控制台摘要
    for h in HORIZONS:
        hz = result["horizons"][str(h)]
        o, b, s = hz["overall_raw"], hz["dir_bull"], hz["dir_bear"]
        print(f"h={h}d raw={o and o['win_rate']}%(n={o and o['n']}) "
              f"bull_dir={b and b['win_rate']}%(n={b and b['n']}) "
              f"bear_dir={s and s['win_rate']}%(n={s and s['n']})", flush=True)


if __name__ == "__main__":
    main()
