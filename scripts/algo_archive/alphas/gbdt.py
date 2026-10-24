# -*- coding: utf-8 -*-
"""梯度提升决策树（stump 版，纯标准库）——非线性、特征交互，与线性/贝叶斯不同范式。"""
from __future__ import annotations

import math
import random


def _sig(z):
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


class GBDT:
    def __init__(self, n_trees=12, feat_frac=0.15, lr=0.2, min_leaf=30, seed=1):
        self.n_trees, self.feat_frac, self.lr = n_trees, feat_frac, lr
        self.min_leaf, self.seed = min_leaf, seed

    def fit(self, X, y):
        n = len(X); d = len(X[0]) if n else 0
        p = sum(y) / n
        self.z0 = math.log(p / (1 - p)) if 0 < p < 1 else 0.0
        mu = [0.0] * d
        for x in X:
            for j in range(d):
                mu[j] += x[j]
        mu = [m / n for m in mu]
        sd = [0.0] * d
        for x in X:
            for j in range(d):
                e = x[j] - mu[j]; sd[j] += e * e
        sd = [math.sqrt(v / n) + 1e-9 for v in sd]
        rng = random.Random(self.seed)
        nfeat = max(3, int(d * self.feat_frac))
        z = [self.z0] * n
        self.trees = []
        for _ in range(self.n_trees):
            g = [_sig(z[i]) - y[i] for i in range(n)]
            g2 = [gi * gi for gi in g]
            G = sum(g); G2 = sum(g2)
            best = {"sse": 1e18}
            for j in rng.sample(range(d), nfeat):
                for c in (-1.0, -0.5, 0.0, 0.5, 1.0):
                    th = mu[j] + c * sd[j]
                    nl = 0; gl = 0.0; gl2 = 0.0
                    for i in range(n):
                        if X[i][j] <= th:
                            nl += 1; gl += g[i]; gl2 += g2[i]
                    nr = n - nl
                    if nl < self.min_leaf or nr < self.min_leaf:
                        continue
                    gr = G - gl; gr2 = G2 - gl2
                    sse = (gl2 - gl * gl / nl) + (gr2 - gr * gr / nr)
                    if sse < best["sse"]:
                        best = {"sse": sse, "j": j, "th": th, "vl": gl / nl, "vr": gr / nr}
            if "j" not in best:
                break
            self.trees.append(best)
            j, th, vl, vr = best["j"], best["th"], best["vl"], best["vr"]
            for i in range(n):
                z[i] += self.lr * (vl if X[i][j] <= th else vr)
        return self

    def predict_proba(self, X):
        trees = self.trees; lr = self.lr; z0 = self.z0
        out = []
        for x in X:
            z = z0
            for t in trees:
                z += lr * (t["vl"] if x[t["j"]] <= t["th"] else t["vr"])
            out.append(_sig(z))
        return out