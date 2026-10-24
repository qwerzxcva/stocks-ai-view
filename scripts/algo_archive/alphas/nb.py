# -*- coding: utf-8 -*-
"""高斯朴素贝叶斯（不同归纳偏置：特征条件独立）。"""
from __future__ import annotations

import math


class GaussianNB:
    def fit(self, X, y):
        n = len(X); d = len(X[0]) if n else 0
        self.d = d
        self.cls = {}
        for c in (0, 1):
            rows = [X[i] for i in range(n) if y[i] == c]
            nc = len(rows)
            mu = [0.0] * d
            for r in rows:
                for j in range(d):
                    mu[j] += r[j]
            mu = [m / max(1, nc) for m in mu]
            var = [0.0] * d
            for r in rows:
                for j in range(d):
                    e = r[j] - mu[j]; var[j] += e * e
            var = [v / max(1, nc) + 1e-6 for v in var]
            self.cls[c] = (math.log(max(nc, 1) / n), mu, var)
        return self

    def predict_proba(self, X):
        lp0, mu0, v0 = self.cls[0]
        lp1, mu1, v1 = self.cls[1]
        d = self.d
        # 预计算常数项
        c0 = lp0 - 0.5 * sum(math.log(2 * math.pi * v0[j]) for j in range(d))
        c1 = lp1 - 0.5 * sum(math.log(2 * math.pi * v1[j]) for j in range(d))
        out = []
        for x in X:
            l0 = c0 - 0.5 * sum((x[j] - mu0[j]) ** 2 / v0[j] for j in range(d))
            l1 = c1 - 0.5 * sum((x[j] - mu1[j]) ** 2 / v1[j] for j in range(d))
            z = l1 - l0
            z = max(-35.0, min(35.0, z))
            out.append(1.0 / (1.0 + math.exp(-z)))
        return out