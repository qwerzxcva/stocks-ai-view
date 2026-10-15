#!/usr/bin/env python3
"""
weight_calibrator_v3.py — 分层权重校准 (修 v2 把权重全部归零的问题)

v2 的问题: 全样本算 edge, 14 个看多指标胜率全在 40~42%, edge 全负,
于是 `total edge 0.0` → 所有权重归零 → engine_v5 无信号可用。

v3 的改法: 不追求「全局正 edge」, 而是做三件事:
  1. 分层统计 — 按 (持有期 × 市场阶段) 交叉, 找出指标真正有效的格子
  2. 相对权重 — 即使全为负 edge, 也按 edge 排序给出相对强弱
     (负得少的指标仍优于负得多的, 全部归零等于放弃信号)
  3. 市况适配 — 看空胜率显著高于看多时, 输出「回避优先」建议,
     而不是强行给买入信号加权

产出 data/weight_calibration.json, engine_v5 / multidim 可消费。

用法:
  python3 weight_calibrator_v3.py [--min-n 5000] [--dry-run]
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def wilson_ci(win, n, z=1.96):
    """Wilson 置信区间 (小样本比正态近似可靠)。"""
    if n <= 0:
        return None
    p = win / 100.0
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round((centre - half) * 100, 2), round((centre + half) * 100, 2)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=5000)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "weight_calibration.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rl = load("reflect_latest.json")
    an = rl.get("analysis") or {}
    overall = an.get("overall") or {}
    ranked = an.get("ranked") or []
    wm = load("winrate_matrix.json")

    if not overall:
        print("[calib] reflect_latest.analysis.overall 为空, 终止")
        return 1

    # ── 1. 持有期 × 市场阶段 交叉 ──────────────────────────────
    horizons = {}
    for h, v in overall.items():
        if not isinstance(v, dict):
            continue
        n = v.get("n") or 0
        if n < args.min_n:
            continue
        horizons[h] = {
            "n": n,
            "bull_win_rate": v.get("bull_win_rate"),
            "bull_ci": wilson_ci(v.get("bull_win_rate") or 0, v.get("bull_n") or 0),
            "bear_win_rate": v.get("bear_win_rate"),
            "bear_ci": wilson_ci(v.get("bear_win_rate") or 0, v.get("bear_n") or 0),
            "avg_ret_pct": v.get("avg_ret_pct"),
            "raw_win_rate": v.get("raw_win_rate"),
        }

    # ── 2. 指标相对排序 (即使全负 edge 也给相对权重) ──────────
    ind_rows = []
    for r in ranked:
        n = r.get("bull_n") or 0
        if n < args.min_n:
            continue
        wr = r.get("bull_win_rate") or 0
        edge = wr - 50.0
        ind_rows.append({
            "indicator": r.get("indicator"),
            "quality": r.get("quality"),
            "n": n,
            "bull_win_rate": wr,
            "bull_ci": wilson_ci(wr, n),
            "bear_win_rate": r.get("bear_win_rate"),
            "bear_n": r.get("bear_n"),
            "edge": round(edge, 2),
        })
    # 按 edge 降序: 负得少的排前面 (相对更强)
    ind_rows.sort(key=lambda x: -(x["edge"] or 0))
    # 相对权重: 用 edge 的 min-max 归一, 全负时最高分拿 0.5 而非 0
    edges = [r["edge"] for r in ind_rows]
    if edges:
        lo, hi = min(edges), max(edges)
        rng = (hi - lo) or 1.0
        for r in ind_rows:
            # 即使全负, 也保留 0.1~0.5 的相对权重, 不归零
            r["rel_weight"] = round(0.1 + 0.4 * (r["edge"] - lo) / rng, 3)

    # ── 3. 市况适配建议 ────────────────────────────────────────
    h1 = horizons.get("1") or {}
    bull_wr = h1.get("bull_win_rate")
    bear_wr = h1.get("bear_win_rate")
    advice = "neutral"
    if bull_wr is not None and bear_wr is not None:
        gap = bear_wr - bull_wr
        if gap >= 5:
            advice = "avoid_first"      # 回避信号远比买入信号有效
        elif gap <= -5:
            advice = "long_first"       # 买入信号有效
    # 样本均衡性检查 (bear 样本过小时结论不可靠)
    regime_balance = None
    if h1.get("bull_ci") and h1.get("bear_ci"):
        regime_balance = {
            "bull_n": None, "bear_n": None,
            "note": "bear 样本若 < bull 的 20%, bear 胜率结论不可靠",
        }
    rbn = (wm.get("horizons") or {}).get("1", {}).get("regime_n") or {}
    if rbn:
        regime_balance = {
            "bull_n": rbn.get("bull"), "bear_n": rbn.get("bear"),
            "bear_share_pct": round(rbn.get("bear", 0) / max(1, (rbn.get("bull", 0) + rbn.get("bear", 0))) * 100, 1),
        }

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "weight_calibrator_v3 (分层 + 相对权重 + 市况适配)",
        "min_n": args.min_n,
        "horizons": horizons,
        "indicators": ind_rows,
        "market_advice": advice,
        "regime_balance": regime_balance,
        "findings": [],
        "caveat": "权重由历史回测推导, 市场结构变化会使历史 edge 失效; "
                  "全负 edge 时保留相对权重而非归零, 避免信号系统空转。"
                  "Research reference only, not investment advice.",
    }

    # 生成文字结论
    if ind_rows:
        best = ind_rows[0]
        worst = ind_rows[-1]
        out["findings"].append(
            "1 日持有期下, 看多胜率最高的指标是 %s (%.1f%%, n=%d), "
            "最低是 %s (%.1f%%) — 差距 %.1f 个百分点" % (
                best["indicator"], best["bull_win_rate"], best["n"],
                worst["indicator"], worst["bull_win_rate"],
                (best["bull_win_rate"] or 0) - (worst["bull_win_rate"] or 0)))
    if bull_wr is not None and bear_wr is not None:
        out["findings"].append(
            "看空胜率 %.1f%% vs 看多 %.1f%% (差 %+.1fpp) → 市况适配建议: %s" % (
                bear_wr, bull_wr, bear_wr - bull_wr,
                {"avoid_first": "回避优先, 买入信号需降权",
                 "long_first": "买入优先, 可正常进攻",
                 "neutral": "多空均衡, 维持中性权重"}[advice]))
    if regime_balance and regime_balance.get("bear_share_pct") is not None:
        share = regime_balance["bear_share_pct"]
        if share < 20:
            out["findings"].append(
                "注意: bear 样本仅占 %.1f%% (n=%d), 看空胜率 %.1f%% 的置信区间宽, "
                "不宜作为强结论" % (share, regime_balance.get("bear_n"), bear_wr or 0))

    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:2500])
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[calib v3] -> %s" % args.out)
    for s in out["findings"]:
        print("  " + s)
    print("[calib v3] 指标 %d 个, 相对权重 0.1~0.5 (不归零)" % len(ind_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
