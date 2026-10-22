#!/usr/bin/env python3
"""
xu_factor_v2.py — 玄学因子全维度对照实验（干支/五行生克/建除十二神/字辈谐音）

移植自 GitCode stock_analysis/metaphysics/（ganzhi.py 完整版），并叠加本仓
name_matching.py 的字辈谐音维度。

⚠️ 对照实验（negative control）：所有维度均为命理规则编码，无任何已验证的
预测机制。GitCode 原版实测 IC = -0.008（噪声）。任何产出不得进入引擎权重、
选股池或实盘决策。

维度:
  D1 建除吉凶      建/除/满/平/定/执/破/危/成/收/开/闭 → 吉日+1 凶日-1
  D2 日干五行      甲乙木/丙丁火/戊己土/庚辛金/壬癸水
  D3 日支五行      子亥水/寅卯木/巳午火/申酉金/辰戌丑未土
  D4 日-股五行生克  日生股 +1.0 / 同 +0.3 / 股生日(泄) -0.5 / 日克股 -1.0 / 股克日 -0.3
  D5 字辈谐音      股票简称落入羊/金/水/木/土字辈组 → +1 / 无 → 0

用法:
  python3 scripts/xu_factor_v2.py --bulk data/bulk --out data/xu_factor_v2.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from name_matching import classify_name, load_cn_names

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
GAN_WX = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
          "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
ZHI_WX = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
          "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
WX = ["木", "火", "土", "金", "水"]
JIANCHU = ["建", "除", "满", "平", "定", "执", "破", "危", "成", "收", "开", "闭"]
JC_GOOD = {1, 4, 5, 7, 8, 10}


def jdn(y: int, m: int, d: int) -> int:
    a = (14 - m) // 12
    y2 = y + 4800 - a
    m2 = m + 12 * a - 3
    return d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - y2 // 100 + y2 // 400 - 32045


def ganzhi_index(y: int, m: int, d: int) -> int:
    return (jdn(y, m, d) + 49) % 60


_BOUNDS = [(1, 6, 1), (2, 4, 2), (3, 6, 3), (4, 5, 4), (5, 6, 5), (6, 6, 6),
           (7, 7, 7), (8, 8, 8), (9, 8, 9), (10, 8, 10), (11, 7, 11), (12, 7, 0)]


def month_branch(m: int, d: int) -> int:
    b = 0
    for bm, bd, br in _BOUNDS:
        if (m, d) >= (bm, bd):
            b = br
        else:
            break
    return b


def day_meta(y: int, m: int, d: int) -> dict:
    gi = ganzhi_index(y, m, d)
    gan, zhi = GAN[gi % 10], ZHI[gi % 12]
    jc = (ZHI.index(zhi) - month_branch(m, d)) % 12
    return {
        "gan": gan, "zhi": zhi,
        "gan_wx": GAN_WX[gan], "zhi_wx": ZHI_WX[zhi],
        "jianchu": JIANCHU[jc], "good": 1 if jc in JC_GOOD else 0,
    }


def sym_wx(code: str) -> str:
    return WX[sum(ord(c) for c in code) % 5]


def rel_score(day_wx: str, s_wx: str) -> float:
    i, j = WX.index(day_wx), WX.index(s_wx)
    if i == j:
        return 0.3
    if (i + 1) % 5 == j:
        return 1.0
    if (j + 1) % 5 == i:
        return -0.5
    if (i + 2) % 5 == j:
        return -1.0
    return -0.3


def xu_score(y: int, m: int, d: int, code: str, name: str | None) -> dict:
    dm = day_meta(y, m, d)
    dim1 = 1.0 if dm["good"] else -1.0
    dim4 = rel_score(dm["gan_wx"], sym_wx(code))
    dim5 = 1.0 if (name and classify_name(name)) else 0.0
    return {
        "jianchu": dim1,
        "gan_wx": dm["gan_wx"], "zhi_wx": dm["zhi_wx"],
        "shengke": dim4,
        "zibei": dim5,
        "total": dim1 + dim4 + dim5,
        "meta": dm,
    }


def load_closes(bulk_dir: str, limit_shards: int = 0) -> dict:
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


def pearson(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=os.path.join(DATA, "bulk"))
    ap.add_argument("--out", default=os.path.join(DATA, "xu_factor_v2.json"))
    ap.add_argument("--limit-shards", type=int, default=0)
    ap.add_argument("--max-symbols", type=int, default=1000)
    args = ap.parse_args()

    names = load_cn_names()
    closes = load_closes(args.bulk, args.limit_shards)
    syms = sorted(closes.keys())[: args.max_symbols]
    print(f"股票 {len(syms)} 只, 中文名表 {len(names)} 条")

    # 逐日横截面：每维度的 IC
    dims = ["jianchu", "shengke", "zibei", "total"]
    ic_by_dim = {d: [] for d in dims}
    by_day: dict = {}
    for s in syms:
        ser = closes[s]
        for i in range(1, len(ser)):
            by_day.setdefault(ser[i][0], []).append((s, ser[i][1] / ser[i - 1][1] - 1))

    for d in sorted(by_day):
        rows = by_day[d]
        if len(rows) < 30:
            continue
        try:
            y, m, dd = (int(x) for x in d.split("-"))
        except ValueError:
            continue
        xs = {k: [] for k in dims}
        ys = []
        for code, r in rows:
            nm = names.get(code) or names.get(code[2:])
            sc = xu_score(y, m, dd, code, nm)
            for k in dims:
                xs[k].append(sc[k])
            ys.append(r)
        for k in dims:
            ic = pearson(xs[k], ys)
            if ic is not None:
                ic_by_dim[k].append(ic)

    result = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
              "engine": "xu_factor_v2",
              "status": "CONTROL_EXPERIMENT_DO_NOT_USE",
              "n_symbols": len(syms),
              "dimensions": {}}
    for k in dims:
        ics = ic_by_dim[k]
        if not ics:
            result["dimensions"][k] = {"mean_ic": None, "n": 0}
            continue
        m_ic = statistics.mean(ics)
        sd_ic = statistics.stdev(ics) if len(ics) > 1 else 0.0
        result["dimensions"][k] = {
            "mean_ic": round(m_ic, 5),
            "std_ic": round(sd_ic, 5),
            "ir": round(m_ic / sd_ic, 4) if sd_ic else 0,
            "positive_rate_pct": round(sum(1 for x in ics if x > 0) / len(ics) * 100, 2),
            "n_cross_sections": len(ics),
            "verdict": "无效因子（噪声）" if abs(m_ic) < 0.02 else "需进一步检验",
        }
        print(f"{k:10s} IC={m_ic:+.5f} IR={result['dimensions'][k]['ir']:+.3f} "
              f"n={len(ics)} → {result['dimensions'][k]['verdict']}")

    for k in dims:
        if result["dimensions"][k]["n_cross_sections"] == 0:
            result["dimensions"][k]["note"] = "该维度在样本日内无变异（所有样本同值），IC 无法计算"
            result["dimensions"][k]["verdict"] = "无法评估（无变异）"

    result["reference"] = {"gitcode_ic": -0.008, "note": "GitCode 原版单维 IC"}
    result["warning"] = "对照实验：|IC|<0.02 视为无预测力，禁止纳入引擎权重或实盘决策"
    result["provenance"] = {"source": "bulk/day_qfq_*.csv + data/data/stock_names.json",
                            "origin": "GitCode stock_analysis/metaphysics/ + name_matching.py"}
    json.dump(result, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
