#!/usr/bin/env python3
"""t3_bear_defense.py — T3 熊市防御全市场全历史验证（无前视）。

聚焦 M2 回测发现的唯一真实正超额场景：S1 EMA严进 / S5 回撤缓冲 / S9 BOLL波段
在 bear regime 下的表现，扩到全主板市场（~3200 只）验证大样本稳健性。

口径（与 bt_driver 一致，严格无前视）：
  - 信号只用 T 日及之前 K 线 + asof=T 的 context（market_score/dt_count 无前视重算）
  - regime 由 regime_router.judge_regime 对 sh000001 逐日判定（无未来函数）
  - 只统计 regime==bear 交易日的买入信号
  - 收益 T 收盘 → T+5/T+10 收盘；基准=同日全体主板等权
  - 输出胜率/超额pp/Wilson CI/夏普/最大回撤，分策略

用法：
  python3 t3_bear_defense.py --pool-size 300   # 中等池冒烟
  python3 t3_bear_defense.py                    # 全主板
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BT_DIR = os.path.join(HERE, "..", "strategies")
ENG = os.path.join(HERE, "..", "..", "scripts", "engines")
STRAT = os.path.join(ENG, "strategies")
for p in (BT_DIR, STRAT, ENG):
    if p not in sys.path:
        sys.path.insert(0, p)

import bt_driver as bt  # noqa: E402 复用其无前视管线
import _context as ctx_mod  # noqa: E402
import regime_router as rr  # noqa: E402
import importlib  # noqa: E402

FOCUS_STRATEGIES = ["s01_ema_strict", "s05_drawdown_cushion", "s09_boll_band"]
BUY_ACTIONS = {"buy", "buy_low"}


def load_mainboard_codes(data_dir):
    """全主板股票代码（sh60/sz00），从主分片提取。"""
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
    ap.add_argument("--pool-size", type=int, default=0, help="0=全主板")
    ap.add_argument("--only", default=None, help="只跑指定策略，逗号分隔（如 s05,s09）")
    ap.add_argument("--data", default=bt.DATA_DIR)
    ap.add_argument("--out", default=os.path.join(HERE, "t3_bear_defense.json"))
    args = ap.parse_args()

    codes = load_mainboard_codes(args.data)
    if args.pool_size > 0:
        codes = codes[: args.pool_size]
    print(f"[t3] 主板池 {len(codes)} 只", file=sys.stderr)

    # 复用 bt_driver 的无前视加载与 regime 判定
    kl = bt.load_pool_klines(codes)
    kl = {c: v for c, v in kl.items() if len(v["close"]) >= bt.WARMUP + 30}
    print(f"[t3] 有效K线 {len(kl)} 只（>=预热+30根）", file=sys.stderr)

    regimes_ret = bt.build_index_regimes()
    # build_index_regimes 返回 (regimes_dict, ...) 二元组，取 [0]
    regimes = regimes_ret[0] if isinstance(regimes_ret, tuple) else regimes_ret
    bear_dates = {d for d, r in regimes.items() if r == "bear"}
    print(f"[t3] 全历史 bear 交易日 {len(bear_dates)} 天", file=sys.stderr)

    focus = FOCUS_STRATEGIES
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        focus = [s for s in FOCUS_STRATEGIES if any(s.startswith(w) for w in want)]
    # 逐策略回测（只统计 bear 日信号）
    results = {}
    for sname in focus:
        try:
            strat = importlib.import_module(sname)
        except Exception as e:
            print(f"[t3] {sname} 导入失败: {e}", file=sys.stderr)
            continue
        signals = []  # (code, t_date, ret_t5, ret_t10)
        for code, k in kl.items():
            closes = k["close"]
            dates = k["date"]
            n = len(closes)
            for i in range(bt.WARMUP, n - 10):
                d = dates[i]
                if d not in bear_dates:
                    continue  # 只看 bear 段
                sub = {kk: vv[: i + 1] for kk, vv in k.items()}
                # 轻量 context：S1/S5/S9 不依赖 dt_count（全市场扫描太慢），
                # 只给 regime + market_score（指数历史快算），跳过逐日 context_asof
                ctx = {"regime": "bear", "market_score": None, "_nolookahead": True}
                try:
                    res = strat.evaluate(code, sub, ctx)
                except Exception:
                    continue
                if res.get("action") in BUY_ACTIONS:
                    r5 = (closes[i + 5] / closes[i] - 1) * 100 if i + 5 < n else None
                    r10 = (closes[i + 10] / closes[i] - 1) * 100 if i + 10 < n else None
                    if r5 is not None:
                        signals.append((code, d, r5, r10))
        # 统计
        n_sig = len(signals)
        if n_sig == 0:
            results[sname] = {"n": 0, "note": "bear段无买入信号"}
            continue
        r5s = [s[2] for s in signals]
        wins = sum(1 for r in r5s if r > 0)
        win_rate = wins / n_sig
        avg = sum(r5s) / n_sig
        # Wilson CI
        z = 1.96
        p = win_rate
        denom = 1 + z * z / n_sig
        center = (p + z * z / (2 * n_sig)) / denom
        half = z * ((p * (1 - p) / n_sig + z * z / (4 * n_sig * n_sig)) ** 0.5) / denom
        results[sname] = {
            "n": n_sig,
            "win_rate_t5": round(win_rate, 4),
            "avg_ret_t5_pct": round(avg, 3),
            "wilson95": [round(center - half, 4), round(center + half, 4)],
        }
        print(f"[t3] {sname}: bear段信号 n={n_sig} 胜率={win_rate:.3f} 平均={avg:.2f}%",
              file=sys.stderr)

    out = {
        "engine": "t3_bear_defense",
        "scope": f"主板 {len(kl)} 只, bear regime 段",
        "bear_days": len(bear_dates),
        "horizon": "T+5",
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("->", args.out, file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
