#!/usr/bin/env python3
"""
sample_balancer.py — 样本均衡引擎 (经验贝叶斯收缩 + 层级先验)

解决问题: ban_prob 的 by_lianban 里 99.7% 样本集中在首板 (n=20081),
而 4连板 n=4、5连板 n=2、6连板 n=2 —— 这些「50%/100% 涨停率」是纯噪声。

方法 (可解释, 不用 sklearn):
  1. 经验贝叶斯收缩 (EB shrinkage):
       估计值 = (n × 原始率 + k × 先验率) / (n + k)
     k = 收缩强度 (默认 50, 即样本 <50 时大幅拉向先验)
  2. 层级先验: 高层板先验 = 低一层板的收缩后估计 (信息自上而下流动)
  3. 输出收缩后概率 + 可信度 (n 越大越信) + 是否 insufficient

为什么不用过采样/欠采样:
  过采样会造出假样本; 欠采样丢掉首板的 20081 个宝贵样本。
  收缩保留全部样本又抑制小样本噪声, 是分层概率估计的标准做法。

输入: data/ban_prob.json
输出: data/ban_prob_balanced.json + 回写原文件的 balanced 字段
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

K_DEFAULT = 50          # EB 收缩强度
MIN_SUFFICIENT = 100    # 样本充足阈值
# 层级顺序 (低→高), 先验自上而下流动
TIER_ORDER = ["首板", "2连板", "3连板", "4连板", "5连板", "6连板"]
# 更高板归入 6连板 桶 (与原 ban_prob 一致)


def shrink(rate, n, prior, k=K_DEFAULT):
    """EB 收缩: (n·rate + k·prior) / (n + k)"""
    if rate is None:
        return prior, 0.0
    if n is None or n <= 0:
        return prior, 0.0
    return (n * rate + k * prior) / (n + k), n / (n + k)


def balance(base, k=K_DEFAULT):
    """对 by_lianban 做层级 EB 收缩。"""
    lb = base.get("by_lianban") or {}
    if not lb:
        return None
    total = sum(v.get("n_samples", 0) for v in lb.values())

    # 全局先验: 全体涨停事件的加权平均 (作为最底层先验)
    def wavg(field):
        num = sum((v.get("n_samples", 0) * (v.get(field) or 0)) for v in lb.values())
        return num / total if total else 0.0

    out = {}
    prior_zt = wavg("zt_next_rate")
    prior_zb = wavg("zhaban_next_rate")
    prior_ret = wavg("ret_next_avg")

    # 按层级从低到高, 每层的先验 = 下一层的收缩后值 (信息自上而下)
    prev_zt, prev_zb, prev_ret = prior_zt, prior_zb, prior_ret
    for tier in TIER_ORDER:
        v = lb.get(tier)
        if v is None:
            continue
        n = v.get("n_samples", 0) or 0
        zt, c_zt = shrink(v.get("zt_next_rate"), n, prev_zt, k)
        zb, c_zb = shrink(v.get("zhaban_next_rate"), n, prev_zb, k)
        ret, c_ret = shrink(v.get("ret_next_avg"), n, prev_ret, k)
        # 期望收益 = 原始值收缩 (只对有样本的层)
        exp = (n * (v.get("ret_next_avg") or 0) + k * prev_ret) / (n + k) if n else prev_ret

        out[tier] = {
            "lianban": v.get("lianban"),
            "n_samples": n,
            "raw_zt_next_rate": v.get("zt_next_rate"),
            "bal_zt_next_rate": round(zt, 2),
            "raw_zhaban_next_rate": v.get("zhaban_next_rate"),
            "bal_zhaban_next_rate": round(zb, 2),
            "raw_ret_next_avg": v.get("ret_next_avg"),
            "bal_ret_next_avg": round(exp, 3),
            "confidence": round(c_zt, 3),          # n/(n+k): 样本越足越信原始值
            "sufficient": n >= MIN_SUFFICIENT,
            "prior_used": round(prev_zt, 2),
        }
        # 本层收缩后值成为下一层的先验
        prev_zt, prev_zb, prev_ret = zt, zb, exp

    return {
        "method": "empirical-Bayes shrinkage, k=%d, prior flows top-down" % k,
        "k": k,
        "min_sufficient": MIN_SUFFICIENT,
        "global_prior": {"zt_next_rate": round(prior_zt, 2),
                         "zhaban_next_rate": round(prior_zb, 2),
                         "ret_next_avg": round(prior_ret, 3)},
        "by_lianban": out,
        "sample_imbalance": {
            "total": total,
            "top_tier_share_pct": round(
                max((v.get("n_samples", 0) for v in lb.values()), default=0)
                / max(1, total) * 100, 1),
            "insufficient_tiers": [t for t, v in out.items() if not v["sufficient"]],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(DATA_DIR, "ban_prob.json"))
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "ban_prob_balanced.json"))
    ap.add_argument("--k", type=float, default=K_DEFAULT, help="EB 收缩强度")
    ap.add_argument("--write-back", action="store_true", help="回写原文件 balanced 字段")
    args = ap.parse_args()

    if not os.path.exists(args.inp):
        print(f"[balancer] 输入不存在: {args.inp}")
        return 1
    d = json.load(open(args.inp, encoding="utf-8"))
    bal = balance(d.get("base") or {}, args.k)
    if not bal:
        print("[balancer] 无 by_lianban 数据")
        return 1

    out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
           "engine": "sample_balancer v1 (EB shrinkage)",
           **bal}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if args.write_back:
        d["balanced"] = bal
        with open(args.inp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)

    print(f"[balancer] 样本失衡: {bal['sample_imbalance']['top_tier_share_pct']}% 集中在最大层")
    print(f"[balancer] insufficient 层: {bal['sample_imbalance']['insufficient_tiers']}")
    print(f"\n{'层级':<8}{'n':>7}{'原始涨停率':>12}{'收缩后':>10}{'置信':>8}  充足")
    for tier in TIER_ORDER:
        v = bal["by_lianban"].get(tier)
        if not v:
            continue
        print(f"{tier:<8}{v['n_samples']:>7}{str(v['raw_zt_next_rate']):>12}"
              f"{v['bal_zt_next_rate']:>10}{v['confidence']:>8.2f}  "
              f"{'✓' if v['sufficient'] else '✗ (用先验)'}")
    print(f"\n[balancer] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
