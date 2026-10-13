#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/walkforward.py ═══
# 上游 commit: 4866b80e246a · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
Walk-forward 滚动前推验证 (参考 tick-stock-panel backtest/walkforward)
用法: python3 walkforward.py --train-days 120 --test-days 20 --step-days 20
核心: 训练窗选参数 -> 紧邻测试窗样本外验证 -> 滚动. IS/OOS 退化比=过拟合信号.
"""
from __future__ import annotations
import argparse
import random
from datetime import date, timedelta
import pandas as pd


class Fold:
    def __init__(self, index, train_start, train_end, test_start, test_end):
        self.index, self.train_start, self.train_end = index, train_start, train_end
        self.test_start, self.test_end = test_start, test_end


def generate_folds(start, end, train_days, test_days, step_days):
    if min(train_days, test_days, step_days) <= 0:
        raise ValueError("窗口参数必须为正")
    folds, i = [], 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end + timedelta(days=1)   # 隔断前视泄漏
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start += timedelta(days=step_days)
    return folds


def bootstrap_win_ci(returns, n_boot=1000, win_threshold=0.0, seed=42, alpha=0.05):
    """Bootstrap 胜率 95% 置信区间: 对收益序列做 n_boot 次有放回重采样,
    每次算胜率(收益>win_threshold 的比例), 返回 (胜率点估计, ci_low, ci_high, 基准胜率阈值)。
    参考: 蒙特卡洛bootstrap对样本外收益的重采样置信区间 (pybroker式稳健检验)。"""
    r = [x for x in returns if x is not None and not pd.isna(x)]
    if not r:
        return {"win_rate": None, "ci_low": None, "ci_high": None, "n": 0}
    rng = random.Random(seed)
    n = len(r)
    point = sum(1 for x in r if x > win_threshold) / n
    wins = []
    for _ in range(n_boot):
        s = [r[rng.randrange(n)] for _ in range(n)]
        wins.append(sum(1 for x in s if x > win_threshold) / n)
    wins.sort()
    lo_i = int((alpha / 2) * n_boot)
    hi_i = min(n_boot - 1, int((1 - alpha / 2) * n_boot))
    return {"win_rate": round(point, 4), "ci_low": round(wins[lo_i], 4),
            "ci_high": round(wins[hi_i], 4), "n": n, "n_boot": n_boot}


def run_rolling_validation(df, score_fn, param_grid=None, n_windows=6, test_days=20):
    """pybroker式滚动 train/test 防过拟合验证:
    将时间轴切成 n_windows 段, 每段 train=前序全部 / test=紧邻20日(扩张窗 walk-forward),
    检验 OOS 收益是否随窗口扩张保持为正且稳定(标准差小)。无 param_grid 时用 score_fn 默认参数。"""
    dates = sorted(df["date"].unique())
    d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    total = (d1 - d0).days
    if total <= test_days * n_windows:
        raise ValueError("数据长度不足")
    results = []
    for w in range(1, n_windows + 1):
        tr_end = d0 + timedelta(days=int(total * w / n_windows))
        te_start = tr_end + timedelta(days=1)
        te_end = min(te_start + timedelta(days=test_days), d1)
        tr = df[(df.date >= d0.isoformat()) & (df.date <= tr_end.isoformat())]
        te = df[(df.date >= te_start.isoformat()) & (df.date <= te_end.isoformat())]
        if te.empty:
            continue
        if param_grid:
            best_p, best_v = None, -1e9
            for p in param_grid:
                sc = score_fn(tr, p)
                v = sum(sc.values()) / max(len(sc), 1)
                if v > best_v:
                    best_p, best_v = p, v
            sc_oos = score_fn(te, best_p)
        else:
            best_p = None
            sc_oos = score_fn(te)
        oos = sum(sc_oos.values()) / max(len(sc_oos), 1) if sc_oos else 0.0
        results.append({"window": w, "train_end": tr_end.isoformat(),
                        "test": f"{te_start}~{te_end}", "params": best_p, "oos": round(oos, 4)})
    oos_vals = [r["oos"] for r in results]
    mean = sum(oos_vals) / max(len(oos_vals), 1)
    std = (sum((x - mean) ** 2 for x in oos_vals) / max(len(oos_vals), 1)) ** 0.5
    return {"windows": results, "oos_mean": round(mean, 4), "oos_std": round(std, 4),
            "oos_positive_ratio": round(sum(1 for x in oos_vals if x > 0) / max(len(oos_vals), 1), 4),
            "verdict": "稳定" if std <= max(abs(mean), 0.01) and mean > 0 else "过拟合/不稳定风险"}


def run_walkforward(df, score_fn, param_grid, train_days=120, test_days=20, step_days=20,
                    return_detail=False):
    """
    df: 日线DataFrame(多票); score_fn(df_train, params) -> {symbol: score}
        pick_fn 在测试窗内按score排序取TopN, 返回测试窗实际收益(次日开盘买入口径由调用方保证)
    返回: folds明细 + OOS拼接收益 + IS/OOS退化比
    """
    dates = sorted(df["date"].unique())
    d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    folds = generate_folds(d0, d1, train_days, test_days, step_days)
    results = []
    for f in folds:
        tr = df[(df.date >= f.train_start.isoformat()) & (df.date <= f.train_end.isoformat())]
        te = df[(df.date >= f.test_start.isoformat()) & (df.date <= f.test_end.isoformat())]
        # 网格搜索: 训练窗内逐参数打分
        best_p, best_v = None, -1e9
        for p in param_grid:
            scores = score_fn(tr, p)
            v = sum(scores.values()) / max(len(scores), 1)
            if v > best_v:
                best_p, best_v = p, v
        scores_oos = score_fn(te, best_p)
        oos = sum(scores_oos.values()) / max(len(scores_oos), 1) if scores_oos else 0.0
        results.append({"fold": f.index, "params": best_p, "is": round(best_v, 4),
                        "oos": round(oos, 4), "degradation": round(best_v - oos, 4)})
    avg_is = sum(r["is"] for r in results) / max(len(results), 1)
    avg_oos = sum(r["oos"] for r in results) / max(len(results), 1)
    return {"folds": results, "avg_is": round(avg_is, 4), "avg_oos": round(avg_oos, 4),
            "verdict": "过拟合风险高" if avg_oos < avg_is * 0.4 else "可接受"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-days", type=int, default=120)
    ap.add_argument("--test-days", type=int, default=20)
    ap.add_argument("--step-days", type=int, default=20)
    a = ap.parse_args()
    folds = generate_folds(date(2024, 1, 1), date(2026, 10, 11), a.train_days, a.test_days, a.step_days)
    print(f"folds: {len(folds)}")
    for f in folds[-3:]:
        print(f"  fold{f.index}: train {f.train_start}~{f.train_end} test {f.test_start}~{f.test_end}")
