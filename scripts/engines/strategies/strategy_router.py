#!/usr/bin/env python3
"""strategy_router.py — 策略路由器（独立实现，策略规则来自用户提供）。

按大盘 regime 路由到对应档位策略池：
  bear  → 保守档 [S1, S2, S5, S9]
  range → 平衡档 [S6, S8, S10]
  bull  → 激进档 [S3, S4, S7]

regime 来源（优先级）：
  1. --regime 参数显式覆盖（bear/range/bull）；
  2. context.regime（_context 加载 regime_router 输出，即 data/engines/regime_router.json）。

同档多策略并行 evaluate，按 score 加权共识：
  consensus_score = Σ(sig.score × sig.position_pct) / Σ(sig.position_pct)（position_pct=0
  的信号退化为等权 score 平均）；任一策略 action=sell 时共识 action=sell（风控优先）。

输出统一推股列表（--codes 多股时按共识 score 降序）：
  {code, consensus_score, consensus_action, signals: {strategy: 各策略信号明细},
   regime, tier, asof}

CLI：--code（单股）或 --codes 多只（逗号分隔）/ --asof / --regime（覆盖）/ --out。
无前视：--asof 通过 slice_asof 截断后传给各策略。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _loader  # noqa: E402
import _context as ctx_mod  # noqa: E402

# 档位路由表
TIER_MAP = {
    "bear": ("保守档", ["s01_ema_strict", "s02_conservative_risk",
                        "s05_drawdown_cushion", "s09_boll_band"]),
    "range": ("平衡档", ["s06_strong_pullback", "s08_multifactor_resonance",
                         "s10_main_capital"]),
    "bull": ("激进档", ["s03_turnover_relay", "s07_emotion_limitup"]),
}

_MODULES: dict = {}


def _load_strategy_module(name: str):
    """按模块名动态 import 策略（sys.path 方式同 s01），失败返回 None。"""
    if name in _MODULES:
        return _MODULES[name]
    mod = None
    try:
        mod = __import__(name)
    except Exception as exc:  # noqa: BLE001 —— 单策略加载失败不拖垮路由
        print(f"[router] 策略模块 {name} 加载失败: {exc}", file=sys.stderr)
    _MODULES[name] = mod
    return mod


def _consensus(signals: dict) -> dict:
    """score 加权共识。position_pct>0 的信号按仓位加权，否则等权兜底。"""
    items = [(s, r) for s, r in signals.items() if isinstance(r, dict)]
    if not items:
        return {"consensus_action": "hold", "consensus_score": 0.0}
    if any(r.get("action") == "sell" for _, r in items):
        action = "sell"  # 风控优先：任一卖出即卖出
    elif any(r.get("action") == "buy" for _, r in items):
        action = "buy"
    elif any(r.get("action") == "watch" for _, r in items):
        action = "watch"
    else:
        action = "hold"
    num, den = 0.0, 0.0
    for _, r in items:
        w = float(r.get("position_pct") or 0.0)
        s = float(r.get("score") or 0.0)
        if w > 0:
            num += s * w
            den += w
    if den == 0.0:  # 全部无仓位 → 等权
        den = float(len(items))
        num = sum(float(r.get("score") or 0.0) for _, r in items)
    return {"consensus_action": action,
            "consensus_score": round(num / den, 1) if den else 0.0}


def route_one(code: str, regime: str, asof: str, data_dir: str,
              live_dir: str, context: dict) -> dict:
    """单股：加载 K线 → 按 regime 档位并行 evaluate → 共识聚合。"""
    tier, strategies = TIER_MAP[regime]
    kl = _loader.load_kline_from_bulk(data_dir, code)
    kl = _loader.slice_asof(kl, asof)
    signals: dict = {}
    for name in strategies:
        mod = _load_strategy_module(name)
        if mod is None:
            signals[name] = {"error": "模块加载失败", "action": "hold",
                             "score": 0, "position_pct": 0}
            continue
        try:
            signals[name] = mod.evaluate(code, kl, context)
        except Exception as exc:  # noqa: BLE001
            signals[name] = {"error": f"evaluate 异常: {exc}", "action": "hold",
                             "score": 0, "position_pct": 0}
    cons = _consensus(signals)
    return {
        "code": _loader.normalize_code(code),
        "consensus_score": cons["consensus_score"],
        "consensus_action": cons["consensus_action"],
        "signals": signals,
        "regime": regime, "tier": tier,
        "asof": asof or (kl["date"][-1] if kl.get("date") else None),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None, help="单股，如 sh600519")
    ap.add_argument("--codes", default=None, help="多股，逗号分隔")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，严格无前视")
    ap.add_argument("--regime", default=None,
                    choices=["bear", "range", "bull"], help="覆盖 context.regime")
    ap.add_argument("--data", default=_loader.DEFAULT_DATA_DIR)
    ap.add_argument("--live", default=ctx_mod.DEFAULT_LIVE_DIR)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.code:
        codes = [args.code]
    else:
        ap.error("需要 --code 或 --codes")

    context = ctx_mod.load_context(args.data, args.live, asof=args.asof)
    regime = args.regime or context.get("regime")
    if regime not in TIER_MAP:
        print(json.dumps({
            "error": f"regime 不可用: {regime!r}（context.regime="
                     f"{context.get('regime')!r}），请用 --regime bear/range/bull 覆盖",
            "regime": regime, "results": [],
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    results = [route_one(c, regime, args.asof, args.data, args.live, context)
               for c in codes]
    # 统一推股列表：共识 score 降序
    results.sort(key=lambda r: r["consensus_score"], reverse=True)
    out = {
        "regime": regime,
        "regime_source": "--regime 覆盖" if args.regime else "context.regime(regime_router)",
        "tier": TIER_MAP[regime][0],
        "strategies": TIER_MAP[regime][1],
        "asof": args.asof,
        "results": results,
    }
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        open(args.out, "w", encoding="utf-8").write(txt)
        print("->", args.out)
    else:
        print(txt)


if __name__ == "__main__":
    main()
