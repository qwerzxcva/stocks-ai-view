#!/usr/bin/env python3
"""
rank_score.py — 横截面分位/排名打分 (借鉴 zvtvz/zvt 的 QuantileScorer/RankScorer)

问题 (2026-10-13):
  engine_v5/ai_screener 的 score 是绝对分 (0-100), 无法回答「在全市场排第几」。
  绝对分随市场整体波动 (普涨日所有股票分都高), top-N 选取不稳定。

设计 (借鉴 zvt):
  - QuantileScorer: 因子值在全市场当日分位中所处档位 (7 档: 0/0.1/0.3/0.5/0.7/0.9/1.0)
  - RankScorer:   score 的 pct rank (0-1), 1 = 全市场最高
  - 多因子聚合:  filter 双轨 (AND/OR) + score 均值

用法:
  from rank_score import quantile_score, rank_score, composite_rank

  # 单因子分位
  scores = quantile_score([10, 20, 30, 40, 50])   # → [0, 0.25, 0.5, 0.75, 1.0]

  # 多因子横截面排名
  stocks = [{"code": "600519", "f_score": 85, "f_mom": 0.12, "f_rsi": 45}, ...]
  out = composite_rank(stocks, factor_keys=["f_score", "f_mom", "f_rsi"],
                       weights={"f_score": 0.5, "f_mom": 0.3, "f_rsi": 0.2})
  # → 每只股票加 rank_pct / quantile_tier / composite_pct
"""
import math


# ─── 单因子横截面分位 ─────────────────────────────────────────
def quantile_score(values):
    """values → 每个值在横截面中的分位 (0-1)。

    并列值取相同分位 (平均秩法)。
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    # 平均秩 (处理并列)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    # rank 0..n-1 → pct 0..1
    return [r / (n - 1) for r in ranks]


def quantile_tier(pct):
    """分位 → 7 档标签 (zvt QuantileScorer 口径)。"""
    tiers = [(0.0, "极弱"), (0.1, "弱"), (0.3, "偏弱"), (0.5, "中性"),
             (0.7, "偏强"), (0.9, "强"), (1.01, "极强")]
    for th, label in tiers:
        if pct < th:
            return label
    return "极强"


def rank_score(values):
    """rank_pct: 1.0 = 横截面最高。等同 quantile_score (保留别名以对齐 zvt 命名)。"""
    return quantile_score(values)


# ─── 多因子横截面聚合 ─────────────────────────────────────────
def composite_rank(stocks, factor_keys, weights=None, higher_is_better=None,
                   out_key="composite_pct"):
    """给 stocks 批量加横截面分位字段。

    stocks: list of dict, 每只股票含 factor_keys 里的数值字段
    factor_keys: 参与聚合的因子字段名
    weights: {factor: weight} 缺省等权
    higher_is_better: {factor: bool} 缺省 True; False 的因子会先取负 (如 RSI 过高反而危险)

    返回: 原 list (原地加字段):
      {factor}_pct        每个因子的横截面分位
      {out_key}           加权聚合分位 (0-1)
      rank_pct            {out_key} 的排名分位
      quantile_tier       7 档标签
      rank_no             横截面名次 (1 = 最好)
    """
    n = len(stocks)
    if n == 0:
        return stocks
    weights = weights or {k: 1.0 / len(factor_keys) for k in factor_keys}
    higher = higher_is_better or {k: True for k in factor_keys}

    # 每因子算分位
    factor_pcts = {}
    for k in factor_keys:
        vals = []
        for s in stocks:
            v = s.get(k)
            vals.append(float(v) if isinstance(v, (int, float)) and v is not None
                        else float("nan"))
        # NaN 用中位数填充 (缺因子不惩罚)
        clean = [v for v in vals if not math.isnan(v)]
        med = sorted(clean)[len(clean) // 2] if clean else 0.0
        vals = [med if math.isnan(v) else v for v in vals]
        if not higher.get(k, True):
            vals = [-v for v in vals]
        pcts = quantile_score(vals)
        factor_pcts[k] = pcts
        for s, p in zip(stocks, pcts):
            s[f"{k}_pct"] = round(p, 4)

    # 加权聚合
    for i, s in enumerate(stocks):
        comp = sum(factor_pcts[k][i] * weights.get(k, 0) for k in factor_keys)
        s[out_key] = round(comp, 4)

    # 聚合分的排名分位
    comps = [s[out_key] for s in stocks]
    ranks = quantile_score(comps)
    order = sorted(range(n), key=lambda i: -comps[i])
    rank_no = [0] * n
    for pos, idx in enumerate(order, 1):
        rank_no[idx] = pos
    for s, r, rn in zip(stocks, ranks, rank_no):
        s["rank_pct"] = round(r, 4)
        s["rank_no"] = rn
        s["quantile_tier"] = quantile_tier(r)
    return stocks


# ─── 与现有 TSP v3 综合分的衔接 ──────────────────────────────
def add_rank_to_picks(picks, score_key="score", out_dir=None):
    """给 ai_picks/picks 列表加横截面排名 (最小侵入)。

    picks: [{code, score, ...}, ...]  score 为绝对分 (0-100)
    返回: 同 list, 加 rank_pct / rank_no / quantile_tier
    """
    return composite_rank(picks, [score_key],
                          weights={score_key: 1.0},
                          higher_is_better={score_key: True},
                          out_key="score_pct")


if __name__ == "__main__":
    # 自测
    vals = [10, 20, 30, 40, 50]
    print("quantile:", quantile_score(vals))
    print("并列:", quantile_score([5, 5, 5, 10]))

    demo = [
        {"code": "A", "score": 85, "mom": 0.12, "rsi": 45},
        {"code": "B", "score": 72, "mom": 0.05, "rsi": 62},
        {"code": "C", "score": 90, "mom": -0.03, "rsi": 30},
        {"code": "D", "score": 60, "mom": 0.20, "rsi": 70},
    ]
    composite_rank(demo, ["score", "mom", "rsi"],
                   weights={"score": 0.5, "mom": 0.3, "rsi": 0.2},
                   higher_is_better={"score": True, "mom": True, "rsi": False})
    print()
    for s in sorted(demo, key=lambda x: x["rank_no"]):
        print(f"  {s['code']} score={s['score']} mom={s['mom']} rsi={s['rsi']} "
              f"→ comp={s['composite_pct']} rank_no={s['rank_no']} "
              f"tier={s['quantile_tier']}")
