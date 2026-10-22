#!/usr/bin/env python3
"""
limitup_ml_train.py — 次日涨停模型训练（walk-forward + 逐日 precision@K）

移植自 GitCode stock_analysis/limitup2/train.py。纯标准库实现（逻辑回归 +
加权正例 + L2），不依赖 sklearn/numpy，便于在 CI 低配 runner 上跑。

用法:
  python3 scripts/limitup_ml_train.py --data data --out data/limitup_ml_results.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from array import array
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))

NF = 15
KS = (1, 5, 10, 20)


def load(data_dir: str):
    base = os.path.join(data_dir, "lp")

    def rd(suffix, ty):
        a = array(ty)
        a.frombytes(open(base + suffix, "rb").read())
        return a

    return (rd("_feat.f32", "f"), rd("_label.b", "b"), rd("_ret.f4", "f"),
            rd("_date.i4", "i"), rd("_sym.i4", "i"))


def scaler(rows, dim):
    mu = [0.0] * dim
    for r in rows:
        for k in range(dim):
            mu[k] += r[k]
    for k in range(dim):
        mu[k] /= len(rows)
    sd = [0.0] * dim
    for r in rows:
        for k in range(dim):
            sd[k] += (r[k] - mu[k]) ** 2
    for k in range(dim):
        sd[k] = math.sqrt(sd[k] / len(rows)) or 1.0
    return mu, sd


def fit(X, ys, dim, epochs=24, lr=0.8, l2=0.1, pos_w=5.0):
    w = [0.0] * dim
    b = 0.0
    m = len(X)
    for _ in range(epochs):
        idx = list(range(m))
        random.shuffle(idx)
        for ii in idx:
            x = X[ii]; y = ys[ii]
            s = b
            for k in range(dim):
                s += w[k] * x[k]
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, s))))
            g = (p - y) * (pos_w if y else 1.0)
            for k in range(dim):
                w[k] -= lr * (g * x[k] + l2 * w[k] / m)
            b -= lr * g
    return w, b


def score_i(w, b, mu, sd, feat, i):
    base = i * NF
    s = b
    for k in range(NF):
        s += w[k] * (feat[base + k] - mu[k]) / sd[k]
    return 1.0 / (1.0 + math.exp(-max(-30, min(30, s))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "..", "data"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "limitup_ml_results.json"))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    feat, lab, ret, date, sym = load(args.data)
    n = len(lab)
    if n == 0:
        print("无样本，先跑 limitup_ml_build.py", file=sys.stderr)
        return 1
    print(f"样本 {n:,}")

    byday: dict = {}
    for i in range(n):
        byday.setdefault(date[i], []).append(i)
    days = sorted(int(x) for x in byday)
    nd = len(days)
    rng = random.Random(args.seed)

    picks = {k: [0, 0] for k in KS}
    base_pos = base_tot = 0
    folds = args.folds
    start = int(nd * 0.6)
    span = nd - start
    step = max(1, span // folds)
    last = None  # (w, bias, mu, sd, train_rows, train_pos)

    for f in range(folds):
        a = int(start + f * step)
        b = int(nd if f == folds - 1 else min(nd, a + step))
        if a >= nd:
            break
        train_idx = [i for d in days[:a] for i in byday[d]]
        pos = [i for i in train_idx if lab[i] == 1]
        neg = [i for i in train_idx if lab[i] == 0]
        capn = min(len(neg), 150000)
        if len(neg) > capn:
            neg = rng.sample(neg, capn)
        tr = pos + neg
        rows = [feat[i * NF:(i + 1) * NF] for i in tr]
        mu, sd = scaler(rows, NF)
        X = [[(rows[j][k] - mu[k]) / sd[k] for k in range(NF)] for j in range(len(rows))]
        w, bias = fit(X, [lab[i] for i in tr], NF)
        last = (w, bias, mu, sd, len(tr), len(pos))

        for d in days[a:b]:
            idxs = byday[d]
            base_tot += len(idxs)
            base_pos += sum(1 for i in idxs if lab[i] == 1)
            sc = sorted(((score_i(w, bias, mu, sd, feat, i), i) for i in idxs), reverse=True)
            for K in KS:
                top = sc[:K]
                picks[K][1] += len(top)
                picks[K][0] += sum(1 for _, i in top if lab[i] == 1)
        print(f"  fold {f+1}: train {len(tr):,} (pos {len(pos):,}) test days {b-a}")

    base = base_pos / base_tot * 100 if base_tot else 0
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "limitup_ml_train",
        "n_samples": n,
        "base_rate_pct": round(base, 4),
        "picks": {},
        "provenance": {"source": "data/lp_* (bulk 派生)", "origin": "GitCode limitup2/train.py"},
    }
    print(f"\n样本外基准次日涨停率 {base:.3f}%")
    for K in KS:
        hit, tot = picks[K]
        p = hit / tot * 100 if tot else 0
        lift = p / base if base else 0
        out["picks"][str(K)] = {"precision_pct": round(p, 4), "lift": round(lift, 3),
                                "n_selected": tot, "n_hits": hit}
        print(f"TopK={K}: 命中率 {p:.3f}%  提升 {lift:.2f}x  (选取 {tot:,}, 命中 {hit:,})")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"→ {args.out}")

    if last:
        w, bias, mu, sd, tr_rows, tr_pos = last
        model = {
            "w": [round(x, 8) for x in w],
            "bias": round(bias, 8),
            "mu": [round(x, 8) for x in mu],
            "sd": [round(x, 8) for x in sd],
            "n_features": NF,
            "trained_at": datetime.now(CST).isoformat(timespec="seconds"),
            "train_rows": tr_rows,
            "train_pos": tr_pos,
        }
        model_path = os.path.join(os.path.dirname(args.out), "limitup_ml_model.json")
        json.dump(model, open(model_path, "w", encoding="utf-8"))
        print(f"→ {model_path} (最后一折参数, train_rows={tr_rows:,}, pos={tr_pos:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
