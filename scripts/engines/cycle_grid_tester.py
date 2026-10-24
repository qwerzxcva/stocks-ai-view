#!/usr/bin/env python3
"""
cycle_grid_tester.py — 「100 点网格 / 底部 3500」用户假设的统计检验 (新增引擎, 不改任何既有文件)

用途:
  对上证综指全历史做纯描述性+推断统计, 检验用户假设:
    H0: 分形顶/谷落在 100 点整数网格 ±band 点带内的比例不高于随机水平;
    H1: 显著高于随机 (二项检验 + Monte Carlo 1000 次随机平移网格对照)。
  另输出:
    - 条件概率: 每个百点关口 (含 3500±30) 历史上作为支撑/阻力的命中统计;
    - 顶-谷递进结构: 最近 4 次顶谷交替的幅度序列与「100 点整数递进」描述性统计。

数据来源: stocks-data-kline/data/index/history/sh000001.txt
  (| 分隔: code|date|open|close|high|low|vol; 802 行, 2023-06-16 ~ 2026-10-09)

措辞纪律 (红线): 只报统计结果, 不确认也不否定用户判断, 不预判指数会到 3500。
无前视: 全部统计基于 asof 及之前数据; 未来走向预测仅使用历史条件频率。

已知限制:
  - 样本仅 802 个交易日 (~3.3 年), 顶/谷样本量有限, 检验功效有限, 报告中如实标注。
  - 分形顶/谷依赖 window 参数, 结果对参数敏感性在输出中给出窗口扫描。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime

KLINE = os.path.expanduser(os.environ.get("KLINE_DIR", "~/workspace/stocks-data-kline/data"))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "cycle_grid_tester.json"))


def read_index(path):
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("|")
            if len(p) < 7:
                continue
            try:
                rows.append({"date": p[1], "open": float(p[2]), "close": float(p[3]),
                             "high": float(p[4]), "low": float(p[5])})
            except ValueError:
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def fractal_pivots(bars, window):
    """分形顶/谷: i 为顶当 high[i] 为 ±window 内最高; 谷取 low[i] 最低。端点不计。"""
    tops, bottoms = [], []
    n = len(bars)
    for i in range(window, n - window):
        hi = bars[i]["high"]
        lo = bars[i]["low"]
        if all(hi >= bars[j]["high"] for j in range(i - window, i + window + 1) if j != i):
            tops.append({"date": bars[i]["date"], "price": hi, "i": i})
        if all(lo <= bars[j]["low"] for j in range(i - window, i + window + 1) if j != i):
            bottoms.append({"date": bars[i]["date"], "price": lo, "i": i})
    return tops, bottoms


def on_grid(price, grid, band, offset=0.0):
    r = (price - offset) % grid
    return min(r, grid - r) <= band


def binom_pvalue_ge(k, n, p0):
    """P[X >= k], X~Bin(n,p0), 精确上尾"""
    if n <= 0:
        return None
    s = 0.0
    for x in range(k, n + 1):
        s += math.comb(n, x) * (p0 ** x) * ((1 - p0) ** (n - x))
    return min(1.0, s)


def monte_carlo_shift(prices, grid, band, n_sim=1000, seed=42):
    """随机平移网格 offset~U[0,grid), 统计命中数 >= 实际命中数的比例 (经验 p)"""
    rng = random.Random(seed)
    actual = sum(1 for p in prices if on_grid(p, grid, band))
    ge = 0
    for _ in range(n_sim):
        off = rng.uniform(0, grid)
        c = sum(1 for p in prices if on_grid(p, grid, band, off))
        if c >= actual:
            ge += 1
    return actual, (ge + 1) / (n_sim + 1)  # +1 修正, 避免 p=0


def round_levels(lo, hi, grid):
    start = math.ceil(lo / grid) * grid
    return [int(start + k * grid) for k in range(int((hi - start) / grid) + 1)]


def support_resistance_hits(bars, levels, band, pivots_idx):
    """对每个关口: 统计其 ±band 带内出现的分形顶(阻力命中)/谷(支撑命中)次数"""
    out = []
    tops, bottoms = pivots_idx
    for lv in levels:
        t = [p for p in tops if abs(p["price"] - lv) <= band]
        b = [p for p in bottoms if abs(p["price"] - lv) <= band]
        out.append({"level": lv,
                    "resistance_hits": len(t), "support_hits": len(b),
                    "resistance_dates": [p["date"] for p in t],
                    "support_dates": [p["date"] for p in b]})
    return out


def alternation_sequence(tops, bottoms, last_n=4):
    """顶谷交替序列: 合并排序后取相邻异类对, 输出最近 last_n 段幅度"""
    pts = [(t["i"], "top", t["price"], t["date"]) for t in tops] + \
          [(b["i"], "bottom", b["price"], b["date"]) for b in bottoms]
    pts.sort()
    seq = []
    for i in range(1, len(pts)):
        if pts[i][1] != pts[i - 1][1]:
            seq.append({"from": {"type": pts[i - 1][1], "date": pts[i - 1][3], "price": pts[i - 1][2]},
                        "to": {"type": pts[i][1], "date": pts[i][3], "price": pts[i][2]},
                        "amplitude": round(pts[i][2] - pts[i - 1][2], 2)})
    return seq[-last_n:], seq


def main():
    ap = argparse.ArgumentParser(description="「100 点网格/底部 3500」假设统计检验 (只报统计, 不做方向预判)")
    ap.add_argument("--index", default=os.path.join(KLINE, "index", "history", "sh000001.txt"))
    ap.add_argument("--grid", type=float, default=100.0, help="网格间距, 默认 100 点")
    ap.add_argument("--band", type=float, default=30.0, help="网格带半宽, 默认 ±30 点")
    ap.add_argument("--window", type=int, default=5, help="分形窗口 ±N 日, 默认 5")
    ap.add_argument("--asof", default=None, help="只用该日及之前数据, 默认数据最新日")
    ap.add_argument("--sims", type=int, default=1000, help="Monte Carlo 随机平移次数")
    ap.add_argument("--focus", type=float, default=3500.0, help="关注关口, 默认 3500")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 文件, 默认 DEFAULT_OUT ('--out -' 输出 stdout)")
    args = ap.parse_args()

    bars = read_index(args.index)
    if not bars:
        print(json.dumps({"error": f"no data in {args.index}"}))
        raise SystemExit(2)
    asof = args.asof or bars[-1]["date"]
    bars = [b for b in bars if b["date"] <= asof]
    data_asof = bars[-1]["date"]

    tops, bottoms = fractal_pivots(bars, args.window)
    pivots = tops + bottoms
    prices = [p["price"] for p in pivots]
    n = len(prices)

    # ── H0 检验 ──
    cover = min(1.0, 2 * args.band / args.grid)  # 网格带覆盖比例 = 随机命中概率上界
    k = sum(1 for p in prices if on_grid(p, args.grid, args.band))
    p_binom = binom_pvalue_ge(k, n, cover)
    actual, p_mc = monte_carlo_shift(prices, args.grid, args.band, args.sims)

    # ── 窗口敏感性 ──
    win_scan = []
    for w in (3, 5, 8, 10):
        t2, b2 = fractal_pivots(bars, w)
        ps = [p["price"] for p in t2 + b2]
        kk = sum(1 for p in ps if on_grid(p, args.grid, args.band))
        win_scan.append({"window": w, "n_pivots": len(ps), "hits": kk,
                         "hit_rate": round(kk / max(1, len(ps)), 4),
                         "random_expectation": round(cover, 4)})

    # ── 百点关口支撑/阻力 ──
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    levels = round_levels(lo - args.grid, hi + args.grid, args.grid)
    sr = support_resistance_hits(bars, levels, args.band, (tops, bottoms))
    focus_stats = next((r for r in sr if abs(r["level"] - args.focus) < args.grid / 2), None)

    # ── 顶谷递进 ──
    recent4, full_seq = alternation_sequence(tops, bottoms, 4)
    amps = [abs(s["amplitude"]) for s in full_seq]
    amp_stat = None
    if amps:
        amps_s = sorted(amps)
        med = amps_s[len(amps_s) // 2] if len(amps_s) % 2 else \
            (amps_s[len(amps_s) // 2 - 1] + amps_s[len(amps_s) // 2]) / 2
        near_grid = sum(1 for a in amps if abs(a % args.grid) <= args.band
                        or abs(a % args.grid - args.grid) <= args.band)
        amp_stat = {"n_swings": len(amps), "mean": round(sum(amps) / len(amps), 1),
                    "median": round(med, 1), "min": round(min(amps), 1), "max": round(max(amps), 1),
                    "pct_amplitude_near_grid_multiple": round(near_grid / len(amps), 4)}

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "engine": "cycle_grid_tester v1 (新增引擎, 不改既有文件)",
        "asof": asof,
        "data_asof": {"index": data_asof, "n_bars": len(bars),
                      "note": "样本 2023-06 起仅约 3.3 年, 检验功效有限; 本地数据滞后于 2026-10-23 为已知限制"},
        "params": {"grid": args.grid, "band": args.band, "window": args.window,
                   "sims": args.sims, "focus": args.focus},
        "pivot_counts": {"tops": len(tops), "bottoms": len(bottoms), "total": n},
        "grid_hypothesis_test": {
            "H0": f"顶/谷落在 {args.grid:.0f} 点网格 ±{args.band:.0f} 带内的比例不高于随机",
            "hits": k, "n": n, "hit_rate": round(k / max(1, n), 4),
            "random_expectation_rate": round(cover, 4),
            "binom_pvalue_upper_tail": round(p_binom, 4) if p_binom is not None else None,
            "monte_carlo_shift_pvalue": round(p_mc, 4),
            "reading": "p<0.05 时网格聚集显著高于随机; 否则统计上不能拒绝「与随机无异」。"
                       "本引擎只报统计量, 不据此确认或否定任何主观判断。",
        },
        "window_sensitivity": win_scan,
        "round_level_support_resistance": sr,
        "focus_level_stats": focus_stats,
        "focus_reading": (f"{args.focus:.0f}±{args.band:.0f} 区间历史命中见 focus_level_stats; "
                          "「是否构成支撑」仅为历史频率描述, 不含对未来点位的预判。"),
        "recent_top_bottom_alternation": recent4,
        "swing_amplitude_stats": amp_stat,
        "wording_discipline": "本报告只陈述统计结果; 不确认、不否定、不预测用户关于网格/点位的判断。",
    }

    txt = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print("->", args.out)
    else:
        print(txt)


if __name__ == "__main__":
    main()
