#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/market_phase.py ═══
# 上游 commit: 656e3275f404 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (补 __main__ 冒烟入口, 原为纯库无 main 导致直接运行静默退出)
"""
市场阶段机 (参考 tick-stock-panel market_phase 移植, 本地轻量版)
  输入: 每日 {date, 首板数, 2板+数, 最高连板, 晋级率, 封板率}
  输出: ice/ignite/rally/climax/ebb/repair 六阶段
  数据源: 本地日线全量(自算涨停) 或 t13涨停池
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PHASES = {"ice": "冰点", "ignite": "启动", "rally": "主升", "climax": "高潮", "ebb": "退潮", "repair": "修复"}

# 阈值(TSP原版, 基于A股全历史分位数)
CLIMAX_GE2, CLIMAX_FB = 50, 220
RALLY_H, RALLY_G2, RALLY_PR = 7, 15, 0.23
RALLY_PR_ALT, RALLY_G2_ALT, RALLY_H_ALT = 0.30, 12, 5
EBB_PR, EBB_PR_STRICT, EBB_SEAL = 0.15, 0.13, 0.57
EBB_RECENT_G2, EBB_RECENT_H = 12, 6
IGNITE_G2_DELTA, IGNITE_G2, IGNITE_PR = 3, 8, 0.20
IGNITE_H_DELTA, IGNITE_H, IGNITE_PR_SOFT = 1, 5, 0.19
ICE_H, ICE_G2, ICE_FB = 4, 6, 24
PROMO_MIN_POOL = 10
EMA_A = 1.0 / 3.0
CONFIRM = 2


def ema(vals, alpha=EMA_A):
    out, cur = [], None
    first = None
    for v in vals:
        if v is None:
            out.append(cur if cur is not None else None)
            continue
        cur = v if cur is None else cur + alpha * (v - cur)
        out.append(cur)
        if first is None:
            first = len(out) - 1
    if first:
        for i in range(first):
            out[i] = out[first]
    return out


def classify(daily: list[dict]) -> list[str]:
    """daily: 按日期升序 [{date, first_board, ge2, height, promo, seal}]"""
    H = ema([d.get("height") for d in daily])
    FB = ema([d.get("first_board") for d in daily])
    G2 = ema([d.get("ge2") for d in daily])
    PR = ema([d.get("promo") for d in daily])
    SR = ema([d.get("seal") for d in daily])

    def raw(i):
        h, fb, g2, pr, sr = H[i], FB[i], G2[i], PR[i], SR[i]
        g2p = G2[max(0, i - 5)]; hp = H[max(0, i - 5)]
        if g2 is not None and (g2 >= CLIMAX_GE2 or (fb is not None and fb >= CLIMAX_FB)):
            return "climax"
        if h is not None and h >= RALLY_H and g2 >= RALLY_G2 and pr is not None and pr >= RALLY_PR:
            return "rally"
        if pr is not None and pr >= RALLY_PR_ALT and g2 >= RALLY_G2_ALT and h is not None and h >= RALLY_H_ALT:
            return "rally"
        if h is not None and h <= ICE_H and g2 <= ICE_G2 and fb is not None and fb <= ICE_FB:
            return "ice"
        from_high = (g2p is not None and g2p >= EBB_RECENT_G2) or (hp is not None and hp >= EBB_RECENT_H)
        if from_high and pr is not None and pr <= EBB_PR and g2 < g2p:
            return "ebb"
        if pr is not None and pr <= EBB_PR_STRICT and sr is not None and sr <= EBB_SEAL:
            return "ebb"
        if g2p is not None and g2 - g2p >= IGNITE_G2_DELTA and g2 >= IGNITE_G2 and pr is not None and pr >= IGNITE_PR:
            return "ignite"
        if hp is not None and h - hp >= IGNITE_H_DELTA and h >= IGNITE_H and pr is not None and pr >= IGNITE_PR_SOFT:
            return "ignite"
        return "repair"

    labels, cur, pend, run = [], None, None, 0
    for i in range(len(daily)):
        r = raw(i)
        if cur is None:
            cur = r; labels.append(r); continue
        if r == cur:
            labels.append(cur); pend, run = None, 0; continue
        if r == pend:
            run += 1
        else:
            pend, run = r, 1
        if run >= CONFIRM:
            cur = r; labels.append(r); pend, run = None, 0
        else:
            labels.append(cur)
    return labels


def from_daily_klines(kline_getter, dates):
    """从日线序列算每日梯队: kline_getter(date)->{symbol: (close, prev_close)}"""
    out = []
    prev_zt = {}  # symbol -> 连板数(昨日)
    for dt in dates:
        px = kline_getter(dt)
        fb = ge2 = 0
        height = 0
        today_zt = {}
        for sym, (c, pc) in px.items():
            if pc and c >= round(pc * 1.1, 2) - 0.001:
                n = prev_zt.get(sym, 0) + 1
                today_zt[sym] = n
                height = max(height, n)
                if n == 1:
                    fb += 1
                else:
                    ge2 += 1
        promo_ok = sum(1 for s, n in today_zt.items() if prev_zt.get(s, 0) + 1 == n and prev_zt.get(s, 0) >= 1)
        pool = sum(1 for n in prev_zt.values() if n >= 1)
        out.append({"date": dt, "first_board": fb, "ge2": ge2, "height": height,
                    "promo": (promo_ok / pool) if pool >= PROMO_MIN_POOL else None, "seal": None})
        prev_zt = today_zt
    return out


if __name__ == "__main__":
    # 冒烟: 纯算法库, 用一组合成梯队跑通 classify
    ap = argparse.ArgumentParser(description="市场阶段机 (算法库冒烟)")
    ap.add_argument("--out", default=None, help="可选: 落盘 JSON")
    args = ap.parse_args()

    demo = []
    for i, (fb, ge2, h, pr) in enumerate([(180, 20, 3, 0.18), (60, 4, 2, 0.12), (30, 3, 1, 0.10),
                                          (220, 40, 5, 0.28), (150, 18, 4, 0.25), (40, 2, 1, 0.08),
                                          (12, 1, 1, 0.05), (25, 2, 1, 0.09), (80, 10, 3, 0.20)]):
        demo.append({"date": f"2026-10-0{i+1}", "first_board": fb, "ge2": ge2,
                     "height": h, "promo": pr, "seal": 0.5})
    labels = classify(demo)
    print("阶段序列:", list(zip([d["date"] for d in demo], labels)))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                       "daily": demo, "phases": labels,
                       "labels": PHASES}, f, ensure_ascii=False, indent=1)
        print("->", args.out)
