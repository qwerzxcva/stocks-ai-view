#!/usr/bin/env python3
"""
xu_factor.py — 干支五行「玄学因子」对照实验

移植自 GitCode stock_analysis/metaphysics/（ganzhi.py）。

⚠️ 这是一个**对照实验（negative control）**，不是可用因子。
GitCode 原版实测 IC = -0.008，即在统计上无预测力（|IC| < 0.02 通常视为噪声）。
保留它的唯一目的是：给真正的因子提供一个「随机基准」，用来检验
其他因子的 IC 是否只是运气。

本脚本产出的任何数字都**不得**进入引擎权重、选股池或任何实盘决策。

用法:
  python3 scripts/xu_factor.py
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")

# 天干地支
GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
GAN_WX = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
          "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
ZHI_WX = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
          "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
WX_IDX = {"木": 0, "火": 1, "土": 2, "金": 3, "水": 4}


def ganzhi_of_day(y: int, m: int, d: int) -> tuple[str, str]:
    """公历 → 日干支（用儒略日推算法，与原版一致）。"""
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524
    gan = GAN[(jd + 9) % 10]
    zhi = ZHI[(jd + 1) % 12]
    return gan, zhi


def wx_vector(gan: str, zhi: str) -> list[float]:
    """干支 → 五行 one-hot（木火土金水）。"""
    v = [0.0] * 5
    v[WX_IDX[GAN_WX[gan]]] += 1.0
    v[WX_IDX[ZHI_WX[zhi]]] += 1.0
    return v


def load_closes(bulk_dir: str, limit_shards: int = 0) -> dict:
    """{code: [(date_str, close), ...]} 按日期升序。"""
    paths = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    if limit_shards:
        paths = paths[:limit_shards]
    series: dict = {}
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                try:
                    series.setdefault(parts[0], []).append((parts[1], float(parts[3])))
                except ValueError:
                    continue
    for c in series:
        series[c].sort(key=lambda x: x[0])
    return series


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关（IC 口径）。"""
    n = len(xs)
    if n < 10:
        return None

    def rank(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=os.path.join(DATA, "bulk"))
    ap.add_argument("--out", default=os.path.join(DATA, "xu_factor_results.json"))
    ap.add_argument("--limit-shards", type=int, default=0)
    ap.add_argument("--max-symbols", type=int, default=800)
    args = ap.parse_args()

    if not os.path.isdir(args.bulk):
        print(f"找不到 bulk 目录 {args.bulk}", file=sys.stderr)
        return 1

    print("加载日线...")
    closes = load_closes(args.bulk, args.limit_shards)
    syms = sorted(closes.keys())[:args.max_symbols]
    print(f"股票 {len(syms)} 只（上限 {args.max_symbols}）")

    # 每个交易日：横截面 = 各股当日收益 vs 当日干支五行强度
    by_day: dict = {}
    for s in syms:
        ser = closes[s]
        for i in range(1, len(ser)):
            d, c = ser[i]
            prev = ser[i - 1][1]
            if prev <= 0:
                continue
            by_day.setdefault(d, []).append((s, c / prev - 1))

    ic_list = []
    n_cs = 0
    for d in sorted(by_day):
        rows = by_day[d]
        if len(rows) < 30:
            continue
        try:
            y, m, dd = (int(x) for x in d.split("-"))
        except ValueError:
            continue
        gan, zhi = ganzhi_of_day(y, m, dd)
        wv = wx_vector(gan, zhi)
        # 因子值 = 五行强度在该股上的投影（用代码数字做伪映射，与原版同构）
        xs, ys = [], []
        for s, r in rows:
            digits = [int(ch) for ch in s if ch.isdigit()]
            seed = (sum(digits) % 5) if digits else 0
            xs.append(wv[seed])
            ys.append(r)
        ic = spearman(xs, ys)
        if ic is not None:
            ic_list.append(ic)
            n_cs += 1

    if not ic_list:
        print("无有效横截面，样本不足", file=sys.stderr)
        return 1

    mean_ic = statistics.mean(ic_list)
    std_ic = statistics.stdev(ic_list) if len(ic_list) > 1 else 0.0
    ir = mean_ic / std_ic if std_ic else 0.0
    positive_rate = sum(1 for x in ic_list if x > 0) / len(ic_list) * 100

    verdict = "无效因子（噪声）" if abs(mean_ic) < 0.02 else "需进一步检验"

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "xu_factor",
        "status": "CONTROL_EXPERIMENT_DO_NOT_USE",
        "n_symbols": len(syms),
        "n_cross_sections": n_cs,
        "mean_ic": round(mean_ic, 5),
        "std_ic": round(std_ic, 5),
        "ir": round(ir, 4),
        "positive_rate_pct": round(positive_rate, 2),
        "verdict": verdict,
        "reference": {"gitcode_ic": -0.008, "note": "GitCode 原版实测 IC"},
        "warning": "对照实验：|IC|<0.02 视为无预测力，禁止纳入引擎权重或实盘决策",
        "provenance": {"source": "bulk/day_qfq_*.csv", "origin": "GitCode stock_analysis/metaphysics/"},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n横截面 {n_cs} 个，mean IC = {mean_ic:.5f}，IR = {ir:.3f}")
    print(f"判定: {verdict}")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
