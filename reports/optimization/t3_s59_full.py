#!/usr/bin/env python3
"""t3_s59_full.py — T3 续：S5/S9 熊市防御策略 bear 段全市场验证（无前视）。

在 t3_bear_defense.py（100 只冒烟，无基准）基础上补齐：
  1. 全主板股票池（sh60*/sz00*），全历史（指数 2023-06-16 → 2026-10-23，812 根）；
  2. 基准：同期全市场主板等权 T+5 收益（主分片逐日涨幅表 → 前向收益，无未来函数）；
  3. 子场景分解：bear 段细分为 阴跌（高波除外）/ 恐慌（60日波动率≥全历史85分位）/
     筑底（bear 段最后 15 个连续交易日），均只用 T 日及之前信息；
  4. 分年度（信号日所在自然年）胜率/超额。

性能：策略 evaluate 每调用重算全历史指标，全市场逐时点直接调不可行。
  本驱动用「精确必要条件预筛」：先按全数组指标（因果计算，slice[i]==full[i]）
  筛出必过信号条件（J<0/RSI<30/下轨触碰/60日峰值回撤>5%），仅对命中日调官方
  evaluate 核实。1 只×1 日 smoke 实测命中率极低（S5 0.7%、S9 1.9% 于 bear 段）。
  KDJ/RSI/BOLL/量比 为因果指标（只含 i 及之前数据），预筛不改变信号集合。

口径：T 日收盘买入 → T+5/T+10 收盘；胜=ret>0；超额 pp=策略均值-基准均值（百分点）；
Wilson 95% CI 对胜率（CI 下限 > 基准胜率 判稳健）。

用法：
  python3 t3_s59_full.py --smoke 100           # 前100只冒烟
  python3 t3_s59_full.py --out t3_s59_full.json  # 全市场
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STRAT = os.path.normpath(os.path.join(HERE, "..", "..", "scripts", "engines", "strategies"))
ENG = os.path.normpath(os.path.join(STRAT, ".."))
for p in (STRAT, ENG):
    if p not in sys.path:
        sys.path.insert(0, p)

import bt_driver as bt  # noqa: E402
import _indicators as ind  # noqa: E402
import regime_router as rr  # noqa: E402

BUY_ACTIONS = {"buy", "buy_low"}
HORIZONS = (5, 10)
H = max(HORIZONS)


def load_mainboard_codes(data_dir):
    codes = set()
    bulk = os.path.join(data_dir, "bulk")
    for shard in ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "60"]:
        path = os.path.join(bulk, f"day_qfq_{shard}.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                c = line[:8]
                if c.startswith(("sh60", "sz00")):
                    codes.add(c)
    return sorted(codes)


def bear_subscenes(bars, regimes):
    """对每根指数 bar 判定 bear 子场景（无前视，只用 <=i 信息）。
    返回 {date: 'panic'|'bottom'|'grind'|None}。
      panic  : bear 且 60日已实现波动率 ≥ 全历史（至i）该波动率序列的 85 分位
      bottom : bear 段最后一个连续 bear 区间的最后 15 个交易日
      grind  : 其余 bear 日（阴跌/弱震荡）
    """
    closes = [b["close"] for b in bars]
    dates = [b["date"] for b in bars]
    n = len(closes)
    vols = [None] * n
    for j in range(60, n):
        rs = [(closes[k] / closes[k - 1] - 1) for k in range(j - 59, j + 1)]
        m = sum(rs) / len(rs)
        vols[j] = (sum((r - m) ** 2 for r in rs) / (len(rs) - 1)) ** 0.5 * 100
    out = {}
    for i in range(n):
        if regimes.get(dates[i]) != "bear":
            continue
        v = vols[i]
        if v is not None and i >= 120:
            hist = sorted(x for x in vols[60:i + 1] if x is not None)
            pct = sum(1 for x in hist if x <= v) / len(hist)
            if pct >= 0.85:
                out[dates[i]] = "panic"
                continue
        out[dates[i]] = "grind"
    bear_seq = [d for d in dates if regimes.get(d) == "bear"]
    if bear_seq:
        # 最后一个连续 bear 段（中间若被非 bear 打断则取末段）
        seg_end = len(bear_seq) - 1
        seg_start = seg_end
        idx = {d: k for k, d in enumerate(dates)}
        while seg_start > 0:
            gap = idx[bear_seq[seg_start]] - idx[bear_seq[seg_start - 1]]
            if gap > 4:  # 间隔超过 4 个指数交易日视为断段
                break
            gap_dates = dates[idx[bear_seq[seg_start - 1]] + 1: idx[bear_seq[seg_start]]]
            if any(regimes.get(g) != "bear" for g in gap_dates):  # 中间有非 bear 日 → 断段
                break
            seg_start -= 1
        for d in bear_seq[max(seg_start, len(bear_seq) - 15):]:
            if out.get(d) == "grind":
                out[d] = "bottom"
    return out


def build_equal_weight_bench(kl):
    """全市场主板等权基准：{date: {ret5/ret10 和、胜数、家数}}。
    对每只股票每个可前推日 d（收盘买入）：ret_h = close[i+h]/close[i]-1。"""
    acc = {}
    for code, k in kl.items():
        closes = k["close"]
        dates = k["date"]
        n = len(closes)
        for i in range(n - H):
            d = dates[i]
            a = acc.get(d)
            if a is None:
                a = acc[d] = {"s5": 0.0, "s10": 0.0, "w5": 0, "w10": 0, "n": 0}
            r5 = closes[i + 5] / closes[i] - 1
            r10 = closes[i + 10] / closes[i] - 1
            a["s5"] += r5
            a["s10"] += r10
            a["w5"] += 1 if r5 > 0 else 0
            a["w10"] += 1 if r10 > 0 else 0
            a["n"] += 1
    return acc


def wilson(wins, n, z=1.96):
    if n == 0:
        return [None, None]
    p = wins / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def stats(rows, key):
    """rows: list of dict with 'ret5','ret10','bret5','bret10','win5','bwin5' per signal."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    rets = [r[key] for r in rows]
    wins = sum(1 for r in rets if r > 0)
    avg = sum(rets) / n
    bk = "b" + key
    brets = [r[bk] for r in rows if r.get(bk) is not None]
    bavg = sum(brets) / len(brets) if brets else None
    bwin = None
    bw = [r["bwin_" + key] for r in rows if r.get("bwin_" + key) is not None]
    if bw:
        bwin = sum(bw) / len(bw)
    out = {
        "n": n,
        "win_rate": round(wins / n, 4),
        "wilson95": wilson(wins, n),
        "avg_ret_pct": round(avg, 3),
        "bench_avg_pct": round(bavg, 3) if bavg is not None else None,
        "excess_pp": round(avg - bavg, 3) if bavg is not None else None,
        "bench_win_rate": round(bwin, 4) if bwin is not None else None,
        "win_excess_pp": round((wins / n - bwin) * 100, 2) if bwin is not None else None,
    }
    return out


def prescreen_candidates(k, bear_dates, sname):
    """用全数组因果指标做必要条件预筛，返回候选索引 list。"""
    c = k["close"]
    h = k["high"]
    l = k["low"]
    o = k["open"]
    v = k["vol"]
    n = len(c)
    bl = ind.boll(c, 20, 2.0)
    kd = ind.kdj(h, l, c)
    rsi6 = ind.rsi(c, 6)
    bl_low, bl_mid = bl["low"], bl["mid"]
    j, r = kd["j"], rsi6
    # 60 日滚动峰值（因果）
    peak60 = [None] * n
    for i in range(n):
        s = max(0, i - 59)
        peak60[i] = max(c[s:i + 1])
    cand = []
    for i in range(bt.WARMUP, n - H):
        if k["date"][i] not in bear_dates:
            continue
        if sname.startswith("s05"):
            dd = (peak60[i] - c[i]) / peak60[i] * 100 if peak60[i] else 0
            ok = ((bl_low[i] is not None and l[i] <= bl_low[i] * 1.01)
                  and (j[i] is not None and j[i] < 0)) or \
                 ((j[i] is not None and j[i] < 0) and dd > 5.0)
        else:  # s09
            ok = ((l[i] <= (bl_mid[i] or 0) * 1.01 or l[i] <= (bl_low[i] or 0) * 1.01)
                  and (j[i] is not None and j[i] < 10)
                  and (r[i] is not None and r[i] < 30)
                  and (c[i] >= o[i] or ((min(o[i], c[i]) - l[i]) / c[i] * 100 if c[i] > 0 else 0) > 1.5))
        if ok:
            cand.append(i)
    return cand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help=">0: 前 N 只冒烟")
    ap.add_argument("--only", default=None, help="s05,s09")
    ap.add_argument("--data", default=bt.DATA_DIR)
    ap.add_argument("--out", default=os.path.join(HERE, "t3_s59_full.json"))
    args = ap.parse_args()

    t0 = time.time()
    codes = load_mainboard_codes(args.data)
    if args.smoke:
        codes = codes[: args.smoke]
    print(f"[t3s59] 主板池 {len(codes)} 只", flush=True)

    kl = bt.load_pool_klines(codes)
    kl = {c: v for c, v in kl.items() if len(v["close"]) >= bt.WARMUP + 30}
    print(f"[t3s59] 有效K线 {len(kl)} 只  ({time.time()-t0:.0f}s)", flush=True)

    bars = bt.read_index_txt("sh000001")
    regimes = {}
    for i in range(len(bars)):
        rg, _s, _c, _e = rr.judge_regime(bars, i)
        regimes[bars[i]["date"]] = rg
    bear_dates = {d for d, r in regimes.items() if r == "bear"}
    subscenes = bear_subscenes(bars, regimes)
    n_panic = sum(1 for v in subscenes.values() if v == "panic")
    n_bottom = sum(1 for v in subscenes.values() if v == "bottom")
    n_grind = sum(1 for v in subscenes.values() if v == "grind")
    print(f"[t3s59] bear 日 {len(bear_dates)}（阴跌{n_grind}/恐慌{n_panic}/筑底{n_bottom}）"
          f" 区间 {bars[0]['date']}..{bars[-1]['date']}  ({time.time()-t0:.0f}s)", flush=True)

    print(f"[t3s59] 构建全市场等权基准…", flush=True)
    bench = build_equal_weight_bench(kl)
    print(f"[t3s59] 基准就绪 {len(bench)} 日  ({time.time()-t0:.0f}s)", flush=True)

    strats = ["s05_drawdown_cushion", "s09_boll_band"]
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        strats = [s for s in strats if any(s.startswith(w) for w in want)]

    result = {"meta": {
        "title": "T3续 S5/S9 bear段全市场验证（无前视+全市场等权基准）",
        "pool": len(kl), "bear_days": len(bear_dates),
        "subscene_days": {"grind": n_grind, "panic": n_panic, "bottom": n_bottom},
        "index_range": [bars[0]["date"], bars[-1]["date"]],
        "horizons": list(HORIZONS), "bench": "全市场主板等权",
        "prescreen": "精确必要条件预筛（因果指标 full[i]==slice[i]），命中日调官方evaluate核实",
    }, "strategies": {}}

    for sname in strats:
        mod = importlib.import_module(sname)
        t1 = time.time()
        signals = []
        n_cand = 0
        for code, k in kl.items():
            closes = k["close"]
            dates = k["date"]
            cand = prescreen_candidates(k, bear_dates, sname)
            n_cand += len(cand)
            for i in cand:
                sub = {kk: vv[: i + 1] for kk, vv in k.items()}
                ctx = {"regime": "bear", "market_score": None, "_nolookahead": True}
                try:
                    res = mod.evaluate(code, sub, ctx)
                except Exception:
                    continue
                if res.get("action") not in BUY_ACTIONS:
                    continue
                d = dates[i]
                b = bench.get(d)
                row = {"code": code, "date": d,
                       "year": d[:4], "scene": subscenes.get(d),
                       "ret5": (closes[i + 5] / closes[i] - 1) * 100,
                       "ret10": (closes[i + 10] / closes[i] - 1) * 100}
                if b and b["n"] > 0:
                    row["bret5"] = b["s5"] / b["n"] * 100
                    row["bret10"] = b["s10"] / b["n"] * 100
                    row["bwin_ret5"] = b["w5"] / b["n"]
                    row["bwin_ret10"] = b["w10"] / b["n"]
                signals.append(row)
        print(f"[t3s59] {sname}: 候选{n_cand} → 信号{len(signals)}  ({time.time()-t1:.0f}s)",
              flush=True)

        per = {}
        for hz in HORIZONS:
            rk = f"ret{hz}"
            rows = [s for s in signals if s.get(rk) is not None]
            blk = stats(rows, rk)
            # 子场景
            by_scene = {}
            for sc in ("grind", "panic", "bottom"):
                seg = [s for s in rows if s["scene"] == sc]
                if seg:
                    by_scene[sc] = stats(seg, rk)
            # 分年度
            by_year = {}
            for y in sorted({s["year"] for s in rows}):
                seg = [s for s in rows if s["year"] == y]
                by_year[y] = stats(seg, rk)
            blk["by_scene"] = by_scene
            blk["by_year"] = by_year
            per[f"T+{hz}"] = blk
        result["strategies"][sname] = {"n_signals": len(signals), "horizons": per}

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    print(f"[t3s59] -> {args.out}  总耗时 {time.time()-t0:.0f}s", flush=True)

    # 终端摘要
    for sname, sd in result["strategies"].items():
        for hz, blk in sd["horizons"].items():
            print(f"{sname} {hz}: n={blk['n']} 胜率={blk.get('win_rate')} "
                  f"超额={blk.get('excess_pp')}pp CI={blk.get('wilson95')} "
                  f"基准胜率={blk.get('bench_win_rate')}", flush=True)


if __name__ == "__main__":
    main()
