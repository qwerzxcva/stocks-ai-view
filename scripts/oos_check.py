#!/usr/bin/env python3
"""oos_check.py — 校准表的时间序列样本外（OOS）稳健性检验（只读，不写盘）。

## 为什么需要它

`docs/books/ENGINE-OPTIMIZATION.md` 第五节第 11 条点出的最根本缺陷：五张校准表
全部来自同一套切点，**没有任何一张做过时间序列切分的样本外检验**。表内的 t 值只
说明「在全样本上相关」，不说明「在未来仍然成立」；而第一节已经算出纯噪声下
`|t|≥1.5` 的期望假阳性就有 15.5 条。所以「这条 edge 是不是噪声」在只读表内 t 时
无法回答，必须切开时间轴再算一遍。

## 做法

不引入新口径、不重算权重，只把 `signal_validator.validate()` 的同一套遍历按
**切点在时间轴上的位置**分成两段：

    每只票的 cut 序列按先后排序 → 前 `--split`（默认 70%）进 in-sample（IS）
                                   → 后 30% 进 out-of-sample（OOS）

两段各自统计 `n / avg_fwd / t / win_rate`，再按标签对齐，回答三件事：

1. OOS 方向是否与 IS 一致（同向 / 反号）；
2. OOS 的 t 是否仍过 `--min-t`（与写表门槛一致，默认 1.5）；
3. 「表内显著、表外消失」的比例 —— 这是判定一张表可信度的核心数字。

## 口径边界

- 这是**时间序列 OOS**，不是跨股票 OOS；它检验「同一批股票的后期是否还成立」，
  不检验「换一批股票是否成立」（后者需要按 code 分组，另做）。
- 样本区间是 `kline_source` 本地缓存（770 只 × 400 根，2025-02~2026-10）；
  OOS 段只占 30%，样本量约为 IS 的 0.4 倍，`t` 天然会掉——**因此判据看「同向 +
  是否仍过门槛」，不看「t 有没有变小」**。
- 只读：不写 `data/*.json`，不改任何表。

## 用法

    python3 scripts/oos_check.py --layer reversal
    python3 scripts/oos_check.py --layer reversal --split 0.6 --min-t 1.5
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signal_validator as SV  # noqa: E402
from kline_source import list_codes, load_bars  # noqa: E402

LAYERS = {
    "reversal": ("reversal_engine", "_tag_fn"),
    "limit": ("limit_structure", "_tag_fn"),
    "orderflow": ("orderflow_proxy", "_tag_fn"),
}


def _tag_fn_of(layer):
    mod_name, fn_name = LAYERS[layer]
    mod = __import__(mod_name)
    return getattr(mod, fn_name)


def collect(tag_fn, samples, horizon=5, min_history=80, cut_step=2,
            split=0.7, combo=True, max_tags_per_bar=3):
    """按切点在时间轴的位置，把前向收益分流进 IS / OOS 两个桶。"""
    is_b, oos_b = {}, {}
    for _code, bars in samples:
        n = len(bars)
        if n < min_history + horizon + 1:
            continue
        cuts = list(range(min_history, n - horizon, max(1, cut_step)))
        if len(cuts) < 2:
            continue
        k = max(1, int(len(cuts) * split))
        for idx, cut in enumerate(cuts):
            view = bars[:cut]
            fwd = SV.pct(bars[cut + horizon - 1]["close"], view[-1]["close"])
            try:
                tags = tag_fn(view) or []
            except Exception:
                continue  # 单点异常不污染统计，与 validate() 同口径
            bucket = is_b if idx < k else oos_b
            for t in set(tags):
                bucket.setdefault(t, []).append(fwd)
            if combo and len(tags) >= 2:
                pri = list(dict.fromkeys(tags))[:max_tags_per_bar]
                for i in range(len(pri)):
                    for j in range(i + 1, len(pri)):
                        bucket.setdefault(f"{pri[i]} & {pri[j]}", []).append(fwd)
    return is_b, oos_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", default="reversal", choices=sorted(LAYERS))
    ap.add_argument("--sample", type=int, default=200, help="取样只数")
    ap.add_argument("--split", type=float, default=0.7, help="前多少比例做 in-sample")
    ap.add_argument("--min-n", type=int, default=100, help="与写表门槛一致")
    ap.add_argument("--min-t", type=float, default=1.5, help="与写表门槛一致")
    a = ap.parse_args()

    tag_fn = _tag_fn_of(a.layer)
    codes = list_codes()[: a.sample]
    samples = [(c, load_bars(c, limit=400)) for c in codes]
    samples = [(c, b) for c, b in samples if len(b) >= 120]
    if not samples:
        print("没有可用样本（kline_source 缓存为空？）")
        return 1

    is_b, oos_b = collect(tag_fn, samples, split=a.split, min_history=80, cut_step=2)

    rows = []
    for k, xs in is_b.items():
        if len(xs) < a.min_n:
            continue
        si = SV.stats(xs)
        so = SV.stats(oos_b.get(k) or [])
        if not si:
            continue
        rows.append((k, si, so))
    rows.sort(key=lambda r: -abs(r[1]["t"]))

    print(f"层={a.layer}  样本={len(samples)} 只  split={a.split}  "
          f"min_n={a.min_n}  min_t={a.min_t}")
    print()
    print(f"{'标签':46s} {'IS n':>6s} {'IS t':>6s} {'IS fwd':>7s} "
          f"{'OOS n':>6s} {'OOS t':>6s} {'OOS fwd':>7s}  判定")
    print("-" * 112)

    n_sig = 0          # IS 显著的标签数
    n_stable = 0       # IS 显著且 OOS 同向、仍过门槛
    n_vanished = 0     # IS 显著但 OOS 消失（|t|<1 或反号）
    n_flip = 0         # IS 显著但 OOS 反号

    for k, si, so in rows:
        is_sig = abs(si["t"]) >= a.min_t
        if so and so["n"] >= a.min_n:
            same_dir = (si["avg"] > 0) == (so["avg"] > 0)
            oos_sig = abs(so["t"]) >= a.min_t
            if is_sig:
                n_sig += 1
                if not same_dir:
                    n_flip += 1
                    verdict = "反号"
                elif oos_sig:
                    n_stable += 1
                    verdict = "稳健"
                elif abs(so["t"]) < 1.0:
                    n_vanished += 1
                    verdict = "消失"
                else:
                    verdict = "减弱"
            else:
                verdict = "IS 不显著"
            oos_cols = f"{so['n']:6d} {so['t']:6.2f} {so['avg']:7.3f}"
        else:
            if is_sig:
                n_sig += 1
                n_vanished += 1
            verdict = "OOS 样本不足"
            oos_cols = f"{'-':>6s} {'-':>6s} {'-':>7s}"
        print(f"{k[:46]:46s} {si['n']:6d} {si['t']:6.2f} {si['avg']:7.3f} "
              f"{oos_cols}  {verdict}")

    print()
    print(f"IS 显著（|t|>={a.min_t}）标签：{n_sig}")
    if n_sig:
        print(f"  其中 OOS 同向且仍过门槛（稳健）：{n_stable}  ({n_stable / n_sig:.0%})")
        print(f"  其中 OOS 反号：{n_flip}  ({n_flip / n_sig:.0%})")
        print(f"  其中 OOS 消失（|t|<1）/ 样本不足：{n_vanished}  ({n_vanished / n_sig:.0%})")
        print()
        print("判读：稳健率 < 50% 说明该层多数 edge 不能外推，只宜作理由、不宜单独加分。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
