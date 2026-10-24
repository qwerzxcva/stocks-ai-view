#!/usr/bin/env python3
"""macc_shard.py — M-ACC 批A regime×策略交叉表：代码分区驱动（无前视）。

复用 bt_driver 的 _loader / _context / regime_router 无前视管线；
全主板代码按排序切 N 段（--part k/N），段内串行 evaluate
（与 bt_driver 完全同口径 T 收盘→T+5/T+10，信号只用 T 及之前数据）。
产出：{strategy: {regime: [signal,...]}} → reports/accuracy/macc_part_<k>.json。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))  # reports/accuracy
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
STRAT = os.path.join(ROOT, "scripts", "engines", "strategies")
ENG = os.path.join(ROOT, "scripts", "engines")
for p in (STRAT, ENG):
    if p not in sys.path:
        sys.path.insert(0, p)

import bt_driver as bt  # noqa: E402 复用无前视管线
import _loader  # noqa: E402
import _context as ctx_mod  # noqa: E402

BUY_ACTIONS = bt.BUY_ACTIONS
HORIZONS = bt.HORIZONS
WARMUP = bt.WARMUP
STRATEGIES = bt.STRATEGIES


def load_mainboard_codes(data_dir):
    """全主板股票代码（sh60/sz00），复用 T3 口径：合并全部主分片取并集。"""
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
                if c.startswith("sh60") or c.startswith("sz00"):
                    codes.add(c)
    return sorted(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", type=int, required=True, help="第几段（0-based）")
    ap.add_argument("--parts", type=int, required=True, help="总段数")
    ap.add_argument("--stride", type=int, default=1, help="段内抽样步长")
    ap.add_argument("--out", required=True)
    ap.add_argument("--strategies", default=None)
    ap.add_argument("--codes", default=None, help="冒烟：直接指定代码，逗号分隔（忽略 part/stride）")
    args = ap.parse_args()

    t0 = time.time()
    if args.codes:
        codes = args.codes.split(",")
    else:
        all_codes = load_mainboard_codes(bt.DATA_DIR)
        # 代码互斥分区：排序后切片，段间不重叠
        chunk = [c for i, c in enumerate(all_codes) if i % args.parts == args.part]
        # 段内步长抽样（覆盖全号段，行业自然分散）
        codes = chunk[::args.stride] if args.stride > 1 else chunk
    print(f"[macc p{args.part}/{args.parts}] 池 {len(codes)} 只（{time.time()-t0:.0f}s）", flush=True)

    klines = bt.load_pool_klines(codes)
    klines = {c: v for c, v in klines.items() if len(v["close"]) >= WARMUP + 20}
    print(f"[macc p{args.part}] 有效K线 {len(klines)} 只（{time.time()-t0:.0f}s）", flush=True)

    regimes, n_idx = bt.build_index_regimes()
    ctx_mod._build_dt_chg_table(bt.DATA_DIR)  # 一次构建全市场主板跌幅表（dt_count 数据源）
    print(f"[macc p{args.part}] regime判定完成 sh000001 {n_idx} 根（{time.time()-t0:.0f}s）", flush=True)

    # 基准：全体主板等权（dt_chg 表逐日等权复利；M-ACC 全市场口径，比 bt_driver 的 sh000300 更贴合全市场）
    dt_table = ctx_mod._DT_CHG_TABLE or {}
    bench_daily = {d: (sum(chgs) / len(chgs) if chgs else None) for d, chgs in dt_table.items()}
    bench_dates = sorted(d for d, v in bench_daily.items() if v is not None)
    bench_pos = {d: i for i, d in enumerate(bench_dates)}
    bench_nav = {}
    nav = 1.0
    for d in bench_dates:
        nav *= (1 + bench_daily[d])
        bench_nav[d] = nav

    strategies = (args.strategies.split(",") if args.strategies else STRATEGIES)
    mods = {}
    for s in strategies:
        try:
            mods[s] = importlib.import_module(s)
        except Exception as e:
            print(f"[macc p{args.part}] {s} 导入失败: {e}", flush=True)
    result = {s: {} for s in mods}

    for sname, mod in mods.items():
        ts = time.time()
        n_sig = 0
        for code, kl in klines.items():
            dates = kl["date"]
            closes = kl["close"]
            n = len(closes)
            end = n - max(HORIZONS)
            for i in range(WARMUP, end):
                d = dates[i]
                rg = regimes.get(d)
                if rg is None:
                    continue
                sub = {k: v[: i + 1] for k, v in kl.items()}
                ctx = dict(bt.context_asof(d))
                ctx["regime"] = rg
                try:
                    r = mod.evaluate(code, sub, ctx)
                except Exception:
                    continue
                if r.get("action") not in BUY_ACTIONS:
                    continue
                r5 = (closes[i + 5] / closes[i] - 1) * 100
                r10 = (closes[i + 10] / closes[i] - 1) * 100
                j = bench_pos.get(d)
                b5 = b10 = None
                if j is not None and j + 5 < len(bench_dates):
                    b5 = (bench_nav[bench_dates[j + 5]] / bench_nav[d] - 1) * 100
                if j is not None and j + 10 < len(bench_dates):
                    b10 = (bench_nav[bench_dates[j + 10]] / bench_nav[d] - 1) * 100
                cell = result[sname].setdefault(rg, [])
                cell.append({"date": d, "code": code,
                             "ret5": round(r5, 4), "ret10": round(r10, 4),
                             "bench5": round(b5, 4) if b5 is not None else None,
                             "bench10": round(b10, 4) if b10 is not None else None})
                n_sig += 1
        print(f"[macc p{args.part}] {sname}: n={n_sig}（{time.time()-ts:.0f}s）", flush=True)

    payload = {"meta": {"part": args.part, "parts": args.parts,
                        "pool": sorted(klines.keys()), "stride": args.stride,
                        "note": "M-ACC批A regime×策略交叉表分区；口径=bt_driver无前视（T收盘→T+5/T+10），"
                                "基准=全体主板等权（dt_chg表逐日等权复利），regime=judge_regime逐日判定",
                        "strategies": list(mods)},
               "signals": result}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"[macc p{args.part}] 写出 {args.out}（总 {time.time()-t0:.0f}s）", flush=True)


if __name__ == "__main__":
    main()
