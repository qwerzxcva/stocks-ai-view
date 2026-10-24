# -*- coding: utf-8 -*-
"""规则专家引擎：金融知识库权重（文献先验）+ 线性得分。无需训练，可解释。"""
from __future__ import annotations

import math

from stock_analysis.kb.anomalies import WEIGHTS
from stock_analysis.engines.feature_names import NAMES


def _sig(z):
    return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z))))


class RuleExpert:
    def __init__(self):
        self.w = [WEIGHTS.get(n, 0.0) for n in NAMES]

    def fit(self, X, y):
        return self

    def predict_proba(self, X):
        w = self.w; d = len(w)
        out = []
        for x in X:
            z = 0.0
            for j in range(d):
                if w[j]:
                    z += w[j] * x[j]
            out.append(_sig(z))
        return out