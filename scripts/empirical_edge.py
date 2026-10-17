#!/usr/bin/env python3
"""empirical_edge.py — 实测胜率 → 有符号边际, 校准 engine_v5 投票。

背景 (2026-10-17 reflect_latest.json, n≈6.4万):
  17 个指标的 +1 票 (看多) 次日实现胜率全部 43–45% (Wilson CI 不含 50),
  显著低于随机 —— 样本期内技术共振做多是负边际; 同时 -1 票 (看空) 的
  实际下跌比例 54–60% (RSI 57%, UO 59.5%, Pivot/Donchian 56.1%), 即看空
  票有真实实现度, 应当用作回避过滤器。self_reflect.py 早已诊断此问题,
  本模块把诊断落成可消费的数字。

证据来源: data/reflect_latest.json (reflect_engine.py 产出), 字段语义见
  reflect_engine.py: bull_win_rate = P(T+h 涨 | +1票),
                    bear_win_rate = P(T+h 跌 | -1票)。

做多边际 (百分点, 正=有利于买入):
  e(+1) = bull_win_rate - 50        (实测看多票的上涨偏差)
  e(-1) = 50 - bear_win_rate        (实测看空票的反向偏差, 看空实现度越高越负)

显著性门槛: 该侧 n >= MIN_N 且 Wilson CI 不含 50 才采用, 否则记 0
  (无信息, 不反向猜测, 防止小样本噪声进生产链路)。
reflect 缺失/不可读时 load_edge_table 返回 None, 调用方维持原行为。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MIN_N = 500

# engine_v5 与 reflect 的指标名逐字一致 (2026-10-17 核对 17/17),
# 若未来 engine 改名, 这里兜底别名。
ALIASES = {
    "KDJ": "KDJ(RSV)",
    "BOLL": "BOLL/Keltner",
    "Donchian": "Donchian20",
}


def load_edge_table(path=None):
    """读 reflect_latest.json, 产出 {指标: {e_bull, e_bear, n_bull, n_bear}}。

    仅保留显著边际; 无显著证据的侧记 0.0。返回 None 表示无证据可用。
    """
    p = path or os.path.join(DATA_DIR, "reflect_latest.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    inds = ((d.get("analysis") or {}).get("indicators")) or {}
    if not inds:
        return None
    table = {}
    for name, rec in inds.items():
        def _side(wr_key, n_key, ci_key, sign):
            wr = rec.get(wr_key)
            n = rec.get(n_key) or 0
            ci = rec.get(ci_key) or [None, None]
            if wr is None or n < MIN_N or ci[0] is None:
                return 0.0, n
            lo, hi = ci
            if lo <= 50 <= hi:          # CI 含 50 → 与随机无异
                return 0.0, n
            return sign * (wr - 50), n
        e_bull, n_bull = _side("bull_win_rate", "bull_n", "bull_win_rate_ci", +1)
        e_bear, n_bear = _side("bear_win_rate", "bear_n", "bear_win_rate_ci", -1)
        table[name] = {"e_bull": round(e_bull, 2), "e_bear": round(e_bear, 2),
                       "n_bull": n_bull, "n_bear": n_bear}
    for alias, canon in ALIASES.items():
        if alias in inds and canon not in table:
            table[alias] = table[canon]
    return {"ts": d.get("ts"), "indicators": table}


def long_edge(table, indicator, vote):
    """单票对「次日上涨」的边际 (pp)。vote ∈ {-1,0,+1}。"""
    if not table or not vote:
        return 0.0
    rec = table["indicators"].get(indicator)
    if not rec:
        return 0.0
    return rec["e_bull"] if vote > 0 else rec["e_bear"]


def calibrate(votes, table):
    """把 engine_v5 的一组投票换算成期望上涨概率偏差。

    按 engine_v5 的口径以 strength 加权, 只在有方向的票上平均:
      edge_pp = Σ strength·e_i(vote) / Σ strength   (方向票)
    返回 {"edge_pp": pp (正=正期望), "dir_weight": 方向票总权重};
    全部无方向票时 edge_pp 为 0.0 且 dir_weight 为 0。
    """
    num = den = 0.0
    for v in votes or []:
        vote = v.get("vote") or 0
        if not vote:
            continue
        w = float(v.get("strength") or 1.0)
        num += w * long_edge(table, v.get("indicator"), vote)
        den += w
    if not den:
        return {"edge_pp": 0.0, "dir_weight": 0.0}
    return {"edge_pp": round(num / den, 2), "dir_weight": round(den, 2)}


def vote_weights(votes):
    """方向票的 strength 加权多空权重。"""
    bull_w = bear_w = 0.0
    for v in votes or []:
        vote = v.get("vote") or 0
        if not vote:
            continue
        w = float(v.get("strength") or 1.0)
        if vote < 0:
            bear_w += w
        else:
            bull_w += w
    return round(bull_w, 2), round(bear_w, 2)


def bear_aligned(votes, table=None, min_ratio=3):
    """看空对齐: 方向票 strength 加权后 -1 占绝对多数 (>= 3:1)。

    用途: 回避过滤器 —— reflect 实测 -1 票下跌实现度 54-60%, 这类票次日
    下跌概率显著。table 保留在签名里以便调用方统一传参, 判定本身只看权重。
    """
    bull_w, bear_w = vote_weights(votes)
    if not bear_w:
        return False, 0.0
    if bear_w < min_ratio * max(bull_w, 0.01):
        return False, round(bear_w, 2)
    return True, round(bear_w, 2)


def main():
    t = load_edge_table()
    if not t:
        print("no edge table (reflect_latest.json missing/empty)")
        return 0
    print(f"edge table @ {t['ts']}")
    rows = sorted(t["indicators"].items(),
                  key=lambda kv: -(kv[1]["e_bear"]))
    for name, r in rows:
        print(f"  {name:16s} e_bull={r['e_bull']:+6.2f}pp (n={r['n_bull']:>6}) "
              f"e_bear={r['e_bear']:+6.2f}pp (n={r['n_bear']:>6})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
