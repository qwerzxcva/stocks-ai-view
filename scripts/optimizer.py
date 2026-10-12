#!/usr/bin/env python3
"""
optimizer.py — 策略参数优化器 (借鉴 StockSharp.Strategies.Optimization)

来源: github.com/StockSharp/StockSharp  Algo.Strategies/Optimization (10871 star)
  - BruteForceOptimizer   全网格遍历
  - GeneticOptimizer      遗传算法 (选择/交叉/变异/精英保留)
  - WalkForwardOptimizer   滚动前推 (训练集调参 → 测试集验证, 防过拟合)
  - FitnessFormulaProvider 适应度公式 (如 "PnL * SharpeRatio"), 可配置

我们用它做什么:
  低位池/打板池的评分权重目前是手工设定的常数 (位置40+金叉20+RSI15...),
  没有依据。用本模块在历史K线上搜索最优权重组合, 并用 WalkForward 检验是否过拟合。

安全边界:
  - 只输出「研究候选」, 不自动改线上权重 (改权重需人工确认)
  - 过拟合风险在 meta 里显式标注 (样本外收益 / 参数敏感度)

用法:
  python3 optimizer.py --demo                # 内置演示
  python3 optimizer.py --space '{"lookback":[20,60,120]}' --bars bars.json
"""
import argparse
import itertools
import json
import math
import random
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


# ─── 适应度公式 (借鉴 FitnessFormulaProvider 的思路, 用安全 eval) ──
class FitnessFormula:
    """把公式字符串编译成函数, 可用变量:

      pnl        区间累计收益 %
      sharpe     夏普比率 (日收益, 年化 252)
      max_dd     最大回撤 % (正数表示跌幅)
      win_rate   胜率 %
      trades     交易次数
      profit_dd  收益回撤比 (pnl/max_dd)
      expectancy 期望收益 (avg_win*win% - avg_loss*loss%)

    示例公式: "pnl * sharpe / max(max_dd, 1)", "pnl * 0.6 + sharpe * 40 - max_dd * 0.4"
    """

    ALLOWED = {"max": max, "min": min, "abs": abs, "sqrt": math.sqrt, "log": math.log}

    def __init__(self, formula=None):
        self.formula = formula or "pnl * sharpe / max(max_dd, 1)"
        self.vars = ["pnl", "sharpe", "max_dd", "win_rate", "trades",
                     "profit_dd", "expectancy"]

    def __call__(self, stats):
        env = dict(self.ALLOWED)
        env.update({k: float(stats.get(k, 0) or 0) for k in self.vars})
        try:
            return float(eval(self.formula, {"__builtins__": {}}, env))
        except Exception:
            return float("-inf")

    def __repr__(self):
        return f"FitnessFormula({self.formula!r})"


# ─── 回测统计 (单标的, 日线, 简单持仓模型) ────────────────────────
def backtest(bars, weights, cost_pct=0.03, max_hold=20):
    """用给定权重跑一次打分选股回测。

    weights: dict, 打分权重 (如 {"position":40, "golden":20, "rsi":15, "shrink":15, "mild":10})
    bars:    [[date,o,c,h,l,v], ...] 升序
    cost_pct: 单边交易成本 %

    模型 (简化, 与 build_lowpool 口径一致):
      - 每日给最后 lookback 根打分, 分数 >= threshold 且不在持仓则买入
      - 持有 max_hold 日或分数 < exit_threshold 则卖出
      - 全仓单标的, 不计复利细节
    """
    if len(bars) < 30:
        return None
    close = [b[2] for b in bars]
    high = [b[3] for b in bars]
    low = [b[4] for b in bars]
    vol = [b[5] for b in bars]

    def score_at(i, lookback):
        if i < lookback:
            return 0.0
        seg = close[i - lookback + 1:i + 1]
        lo, hi = min(seg), max(seg)
        pos = (close[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        s = 0.0
        # 位置分
        s += weights.get("position", 40) * max(0.0, (25 - pos) / 25)
        # 金叉
        ma5 = sum(close[i - 4:i + 1]) / 5
        ma20 = sum(close[i - 19:i + 1]) / 20 if i >= 19 else ma5
        if ma5 > ma20:
            s += weights.get("golden", 20)
        # RSI 修复
        if i >= 15:
            g = l = 0.0
            for j in range(i - 13, i + 1):
                d = close[j] - close[j - 1]
                if d > 0:
                    g += d
                else:
                    l -= d
            ag, al = g / 14, l / 14
            rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            if 40 <= rsi <= 55:
                s += weights.get("rsi", 15)
            elif rsi < 30:
                s += weights.get("rsi", 15) * 0.5
        # 缩量止跌
        if i >= 25:
            v5 = sum(vol[i - 4:i + 1]) / 5
            v20 = sum(vol[i - 25:i - 5]) / 20
            if v20 > 0 and v5 < v20 * 0.7:
                s += weights.get("shrink", 15)
        # 温和上涨
        chg = (close[i] - close[i - 1]) / close[i - 1] * 100 if close[i - 1] else 0
        if 0 <= chg <= 5:
            s += weights.get("mild", 10)
        return s

    lookback = int(weights.get("lookback", 120))
    # warmup 不能超过段长的 60%, 否则短测试段会一次都不交易 (WalkForward 曾因此全 0)
    lookback = max(20, min(lookback, int(len(bars) * 0.6)))
    thr = float(weights.get("threshold", 60))
    exit_thr = float(weights.get("exit_threshold", 40))

    cash, shares, trades, wins = 1.0, 0.0, 0, 0
    equity, peak, max_dd = [1.0], 1.0, 0.0
    hold = 0
    entry_price = 0.0

    for i in range(lookback, len(close)):
        s = score_at(i, lookback)
        px = close[i]

        if shares > 0:
            hold += 1
            if s < exit_thr or hold >= max_hold:
                proceeds = shares * px * (1 - cost_pct / 100)
                if proceeds > entry_price * shares:
                    wins += 1
                cash = proceeds
                shares, hold = 0.0, 0
        else:
            if s >= thr:
                shares = cash / (px * (1 + cost_pct / 100))
                entry_price = px
                cash, hold = 0.0, 0
                trades += 1

        eq = cash + shares * px
        equity.append(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

    if shares > 0:   # 强制平仓
        proceeds = shares * close[-1] * (1 - cost_pct / 100)
        if proceeds > entry_price * shares:
            wins += 1
        cash = proceeds

    # 统计
    rets = [(equity[i] - equity[i - 1]) / equity[i - 1] for i in range(1, len(equity))]
    pnl = (cash - 1.0) * 100
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        sharpe = (mu / sd * math.sqrt(252)) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    win_rate = (wins / trades * 100) if trades else 0.0
    profit_dd = pnl / max_dd if max_dd > 0 else 0.0

    # 期望收益
    win_rets = [r for r in rets if r > 0]
    loss_rets = [r for r in rets if r < 0]
    avg_win = sum(win_rets) / len(win_rets) * 100 if win_rets else 0.0
    avg_loss = abs(sum(loss_rets) / len(loss_rets)) * 100 if loss_rets else 0.0
    expectancy = avg_win * (len(win_rets) / len(rets)) - avg_loss * (len(loss_rets) / len(rets)) if rets else 0.0

    return {
        "pnl": round(pnl, 2), "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2), "win_rate": round(win_rate, 1),
        "trades": trades, "profit_dd": round(profit_dd, 3),
        "expectancy": round(expectancy, 3),
        "final_equity": round(cash, 4),
    }


# ─── 三种优化器 ──────────────────────────────────────────────────
def brute_force(bars, space, fitness, top_n=10, max_combos=2000, min_trades=5):
    """全网格遍历 (借鉴 BruteForceOptimizer)。

    min_trades: 最低交易次数门槛, 避免 trades=4/win=100% 这类小样本过拟合排到前面。
    """
    keys = list(space.keys())
    combos = list(itertools.product(*[space[k] for k in keys]))
    if len(combos) > max_combos:
        random.seed(42)
        combos = random.sample(combos, max_combos)
    results = []
    for combo in combos:
        w = dict(zip(keys, combo))
        st = backtest(bars, w)
        if not st or st["trades"] < min_trades:
            continue
        st["fitness"] = round(fitness(st), 4)
        st["weights"] = w
        results.append(st)
    results.sort(key=lambda x: -x["fitness"])
    return results[:top_n]


def genetic(bars, space, fitness, pop=40, gens=15, top_n=10, seed=42):
    """遗传算法 (借鉴 GeneticOptimizer): 选择/交叉/变异/精英保留。"""
    rnd = random.Random(seed)
    keys = list(space.keys())
    bounds = {k: (min(space[k]), max(space[k])) for k in keys}
    discrete = {k: len(space[k]) > 1 and all(isinstance(v, int) for v in space[k]) for k in keys}

    def rand_gene():
        return {k: rnd.choice(space[k]) for k in keys}

    def crossover(a, b):
        return {k: (a[k] if rnd.random() < 0.5 else b[k]) for k in keys}

    def mutate(g):
        out = dict(g)
        for k in keys:
            if rnd.random() < 0.25:
                out[k] = rnd.choice(space[k])
        return out

    population = [rand_gene() for _ in range(pop)]
    scored = []
    for g in population:
        st = backtest(bars, g)
        if st and st["trades"] >= 3:
            st["fitness"] = round(fitness(st), 4)
            st["weights"] = g
            scored.append(st)

    for _ in range(gens):
        if len(scored) < 4:
            break
        scored.sort(key=lambda x: -x["fitness"])
        elite = scored[:max(2, pop // 5)]
        nxt = [s["weights"] for s in elite]
        while len(nxt) < pop:
            p1, p2 = rnd.choice(scored)["weights"], rnd.choice(scored)["weights"]
            nxt.append(mutate(crossover(p1, p2)))
        scored = []
        for g in nxt:
            st = backtest(bars, g)
            if st and st["trades"] >= 3:
                st["fitness"] = round(fitness(st), 4)
                st["weights"] = g
                scored.append(st)

    scored.sort(key=lambda x: -x["fitness"])
    return scored[:top_n]


def walk_forward(bars, space, fitness, folds=4, train_ratio=0.7, seed=42):
    """滚动前推 (借鉴 WalkForwardOptimizer): 每折 训练集调参 → 测试集验证。

    输出样本外 (out-of-sample) 统计, 用于判断是否过拟合。
    """
    n = len(bars)
    if n < 60:
        return None
    fold_size = n // folds
    results = []
    for f in range(folds):
        start = f * fold_size
        end = min(n, start + fold_size)
        seg = bars[start:end]
        cut = int(len(seg) * train_ratio)
        train, test = seg[:cut], seg[cut:]
        if len(train) < 40 or len(test) < 10:
            continue
        best = brute_force(train, space, fitness, top_n=1)
        if not best:
            # 训练段也无信号 (可能 lookback 太大或段太短), 跳过而不是记 0
            continue
        w = best[0]["weights"]
        st_tr = backtest(train, w)
        st_te = backtest(test, w)
        if not st_te or st_te["trades"] == 0:
            # 测试段无交易 → 该折对样本外统计无贡献, 明确跳过而非记 0%
            continue
        results.append({
            "fold": f + 1,
            "train_span": [train[0][0], train[-1][0]],
            "test_span": [test[0][0], test[-1][0]],
            "weights": w,
            "train_fitness": best[0]["fitness"],
            "train_pnl": st_tr["pnl"] if st_tr else None,
            "test_pnl": st_te["pnl"],
            "test_sharpe": st_te["sharpe"],
            "test_max_dd": st_te["max_dd"],
            "test_win_rate": st_te["win_rate"],
            "test_trades": st_te["trades"],
        })

    if not results:
        return None
    oos_pnl = [r["test_pnl"] for r in results]
    is_pnl = [r["train_pnl"] for r in results if r["train_pnl"] is not None]
    avg_oos = sum(oos_pnl) / len(oos_pnl)
    avg_is = sum(is_pnl) / len(is_pnl) if is_pnl else 0.0
    # 过拟合度量: 样本外/样本内 收益比 (<0.5 视为可能过拟合)
    ratio = avg_oos / avg_is if avg_is else 0.0
    return {
        "folds": results,
        "avg_oos_pnl": round(avg_oos, 2),
        "avg_is_pnl": round(avg_is, 2),
        "oos_is_ratio": round(ratio, 3),
        "overfit_risk": "高" if ratio < 0.5 else ("中" if ratio < 0.8 else "低"),
    }


# ─── demo ─────────────────────────────────────────────────────────
def _demo():
    import os
    BULK = "/root/workspace/stocks-data-kline/data/bulk"
    bars = {}
    for i in range(10):
        p = os.path.join(BULK, f"day_qfq_{i:02d}.csv")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    bars.setdefault(code, []).append(
                        [parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
    if not bars:
        print("无K线数据, 用合成数据演示")
        bars = {"syn": [[f"2026-{i//28+1:02d}-{i%28+1:02d}", 100 + i * 0.3,
                         100 + i * 0.3, 100 + i * 0.3 + 1, 100 + i * 0.3 - 1, 100000 + i * 100]
                        for i in range(120)]}

    code = max(bars, key=lambda k: len(bars[k]))
    data = sorted(bars[code], key=lambda b: b[0])
    print(f"=== optimizer 演示 (标的 {code}, {len(data)} 根K线) ===\n")

    space = {
        "position": [20, 30, 40, 50],
        "golden": [10, 20, 30],
        "rsi": [8, 15, 22],
        "shrink": [8, 15, 20],
        "mild": [5, 10, 15],
        "lookback": [60, 90, 120],
        "threshold": [50, 60, 70],
    }
    fit = FitnessFormula("pnl * sharpe / max(max_dd, 1)")
    print(f"适应度公式: {fit}")
    print(f"参数空间: {len(space)} 维, 组合数 "
          f"{math.prod(len(v) for v in space.values())}\n")

    print("--- 1. 暴力搜索 ---")
    bf = brute_force(data, space, fit, top_n=5)
    for r in bf:
        print(f"  fitness={r['fitness']:8.3f}  pnl={r['pnl']:+7.2f}%  "
              f"sharpe={r['sharpe']:6.3f}  dd={r['max_dd']:5.2f}%  "
              f"win={r['win_rate']:5.1f}%  trades={r['trades']}")
    if bf:
        print(f"  最优权重: {json.dumps(bf[0]['weights'], ensure_ascii=False)}")

    print("\n--- 2. 遗传算法 ---")
    ga = genetic(data, space, fit, pop=30, gens=10, top_n=5)
    for r in ga:
        print(f"  fitness={r['fitness']:8.3f}  pnl={r['pnl']:+7.2f}%  "
              f"sharpe={r['sharpe']:6.3f}  dd={r['max_dd']:5.2f}%  trades={r['trades']}")
    if ga:
        print(f"  最优权重: {json.dumps(ga[0]['weights'], ensure_ascii=False)}")

    print("\n--- 3. 滚动前推 (防过拟合检验) ---")
    wf = walk_forward(data, space, fit, folds=3)
    if wf:
        for f in wf["folds"]:
            print(f"  折{f['fold']}: 训练 {f['train_span'][0]}~{f['train_span'][1]} "
                  f"(fit={f['train_fitness']:.2f}, pnl={f['train_pnl']}%)  "
                  f"测试 {f['test_span'][0]}~{f['test_span'][1]} "
                  f"(pnl={f['test_pnl']:+.2f}%, sharpe={f['test_sharpe']})")
        print(f"  平均样本内 {wf['avg_is_pnl']}% vs 样本外 {wf['avg_oos_pnl']}%  "
              f"比值={wf['oos_is_ratio']}  过拟合风险: {wf['overfit_risk']}")

    print("\n注意: 以上为历史回测, 单标的全仓简化模型, 含单边0.03%成本。")
    print("      过拟合风险高的参数不应直接用于实盘。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    _demo()
