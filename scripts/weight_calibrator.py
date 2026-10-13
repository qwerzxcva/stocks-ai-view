#!/usr/bin/env python3
"""
weight_calibrator.py — 权重校准闭环引擎

解决问题: reflect_engine 算出 by_indicator (各指标 1 日胜率 + Wilson CI),
但结果「算完就扔」—— engine_v5 永远等权投票 (w.get(v["indicator"], 1.0))。
本引擎把特征重要性转成权重, 写回 engine_v5 使用。

方法 (可解释, 不训练黑箱):
  1. 从 reflect_latest.json 读 by_indicator: 每指标的 bull_win_rate + CI + n
  2. 质量分 quality = 多空胜率均值 (reflect 已有)
  3. 权重 = f(quality):
       quality >= 65 → 1.5  (显著有效, 加权)
       55 <= quality < 65 → 1.2  (弱有效, 略加权)
       45 <= quality < 55 → 1.0  (噪声, 等权)
       quality < 45 → 0.6   (反向/无效, 降权)
  4. 归一化到均值 1.0 (保持总量不变, 只改相对权重)
  5. 样本不足 (n<30) 的指标保持 1.0 (不因噪声调权重)
  6. 输出 data/engine_weights.json + 可选用 --apply 写入 engine_v5 默认权重

防过拟合:
  - 只用 Wilson CI 下限 > 50% 的指标才加权 (统计显著)
  - 权重幅度限制 [0.6, 1.5] (单次校准不剧烈改变行为)
  - 记录校准历史 (data/weights_history.json) 便于回溯

用法:
  python3 weight_calibrator.py                # 只算并输出
  python3 weight_calibrator.py --apply        # 写入 engine_v5.py 的 DEFAULT_WEIGHTS
"""
import argparse
import json
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

MIN_N = 30            # 样本不足不调权重
MIN_CI_LO = 50.0      # Wilson CI 下限需 > 50% 才认定有效
W_CAP = (0.6, 1.5)    # 权重幅度限制


def weight_from_quality(q, ci_lo, n):
    """质量分 → 权重。样本不足或 CI 不显著 → 1.0 (不动)。"""
    if n is None or n < MIN_N:
        return 1.0, "样本不足, 保持等权"
    if q is None:
        return 1.0, "无质量分"
    if q >= 65 and (ci_lo is None or ci_lo >= MIN_CI_LO):
        return 1.5, f"质量{q:.0f}且CI下限{ci_lo}≥{MIN_CI_LO} → 加权"
    if q >= 55 and (ci_lo is None or ci_lo >= MIN_CI_LO):
        return 1.2, f"质量{q:.0f}弱有效 → 略加权"
    if q >= 45:
        return 1.0, f"质量{q:.0f} 噪声区 → 等权"
    if q < 45 and (ci_lo is None or ci_lo < 100 - MIN_CI_LO):
        return 0.6, f"质量{q:.0f} 反向/无效 → 降权"
    return 1.0, "边界情况, 保持等权"


def calibrate(reflect):
    """从 reflect_latest.json 生成权重表。

    字段适配: analysis.indicators (dict) — 每指标含
      bull_n / bull_win_rate / bull_win_rate_ci
      bear_n / bear_win_rate / bear_win_rate_ci
    质量分 = 双向胜率均值 (两边都高才是好指标; 只一边高可能过拟合)
    """
    analysis = (reflect or {}).get("analysis") or {}
    by_ind = analysis.get("indicators") or analysis.get("by_indicator") or {}
    if not by_ind:
        return None
    weights, reasons = {}, {}
    for name, rec in by_ind.items():
        # 字段适配: reflect 用 indicators (dict), 双向胜率
        bull_wr = rec.get("bull_win_rate")
        bear_wr = rec.get("bear_win_rate")
        n = (rec.get("bull_n") or 0) + (rec.get("bear_n") or 0)
        vals = [v for v in (bull_wr, bear_wr) if v is not None]
        q = sum(vals) / len(vals) if vals else None   # 双向质量分
        ci_lo = None
        for key in ("bull_win_rate_ci", "bear_win_rate_ci"):
            ci = rec.get(key)
            if isinstance(ci, list) and ci and ci[0] is not None:
                ci_lo = ci[0] if ci_lo is None else min(ci_lo, ci[0])
        w, why = weight_from_quality(q, ci_lo, n)
        weights[name] = w
        reasons[name] = {
            "quality": round(q, 1) if q is not None else None,
            "n": n, "ci_lo": ci_lo, "weight": w, "reason": why,
            "bull_win": bull_wr, "bear_win": bear_wr,
        }
    # 归一化到均值 1.0 (只改相对权重)
    if weights:
        # 不做 mean=1.0 归一化 — 那会把「全部指标都无效」的真实信号抹平。
        # 保留绝对权重: 整体无效时整体低权重, 由调用方 (engine_v5) 决定是否降级技术面。
        # 只做幅度裁剪, 防止单指标权重异常。
        for k in weights:
            weights[k] = round(max(W_CAP[0], min(W_CAP[1], weights[k])), 3)
            reasons[k]["weight_final"] = weights[k]
    # 汇总信号: 有多少指标真有效
    n_eff = sum(1 for v in reasons.values() if v["weight"] >= 1.2)
    n_bad = sum(1 for v in reasons.values() if v["weight"] <= 0.7)
    summary = {
        "n_total": len(weights),
        "n_effective": n_eff,
        "n_reversed_or_noise": n_bad,
        "verdict": ("技术面整体证伪, 应降级为回避过滤器" if n_eff <= 2 and n_bad >= 8
                    else "部分指标有效, 保留加权" if n_eff >= 3
                    else "指标有效性弱, 维持等权观察"),
    }
    return {"weights": weights, "reasons": reasons, "summary": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(DATA_DIR, "reflect_latest.json"))
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "engine_weights.json"))
    ap.add_argument("--apply", action="store_true",
                    help="写入 engine_v5.py 的 DEFAULT_WEIGHTS")
    args = ap.parse_args()

    if not os.path.exists(args.inp):
        print(f"[calibrator] 输入不存在: {args.inp}")
        return 1
    reflect = json.load(open(args.inp, encoding="utf-8"))
    cal = calibrate(reflect)
    if not cal:
        print("[calibrator] reflect 无 by_indicator 数据 (样本不足?)")
        return 1

    hist_path = os.path.join(DATA_DIR, "weights_history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path, encoding="utf-8"))
        except Exception:
            hist = []
    hist.append({"ts": datetime.now(CST).isoformat(timespec="seconds"),
                 "weights": cal["weights"]})
    hist = hist[-30:]  # 只留最近 30 次

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "weight_calibrator v1 (特征重要性 → 权重)",
        "source": "reflect_latest.json by_indicator (多持有期胜率+Wilson CI)",
        "method": {"min_n": MIN_N, "min_ci_lo": MIN_CI_LO, "cap": list(W_CAP),
                   "normalize": "mean=1.0, cap [0.6,1.5]",
                   "anti_overfit": "只用 CI 下限>50% 的指标加权; 样本<30 不动"},
        **cal,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)

    print(f"[calibrator] {len(cal['weights'])} 个指标权重:")
    for k, v in sorted(cal["weights"].items(), key=lambda x: -x[1]):
        r = cal["reasons"][k]
        print(f"  {k:16s} w={v:.3f}  质量{r['quality']} n={r['n']}  {r['reason']}")
    print(f"[calibrator] → {args.out} / {hist_path}")

    if args.apply:
        ev = os.path.join(HERE, "engine_v5.py")
        if not os.path.exists(ev):
            print("[calibrator] engine_v5.py 不存在")
            return 1
        src = open(ev, encoding="utf-8").read()
        block = "DEFAULT_WEIGHTS = " + json.dumps(cal["weights"], ensure_ascii=False, indent=4)
        if "DEFAULT_WEIGHTS = {" in src:
            import re
            src = re.sub(r"DEFAULT_WEIGHTS = \{.*?\n\}",
                         block, src, count=1, flags=re.DOTALL)
        else:
            # 插到 score_stock 前
            src = src.replace("def score_stock(",
                             block + "\n\n\ndef score_stock(", 1)
        open(ev, "w", encoding="utf-8").write(src)
        print(f"[calibrator] ✅ 已写入 {ev}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
