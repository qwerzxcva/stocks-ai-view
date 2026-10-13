#!/usr/bin/env python3
"""
decision_engine.py — 策略决策引擎 (多维因子 → 可执行决策卡)

输入:
  data/multidim.json       9维因子分 (multidim_engine.py 产出)
  data/market_now.json     市场环境 (涨跌停/涨跌家数/成交额)
  data/ban_pick.json       打板池 (可打/观察/回避)
  data/reflect_latest.json 历史胜率 (用于策略置信度)
  data/winrate_matrix.json 持有期×市场阶段胜率

输出 data/decision.json:
  {
    "ts": "...",
    "market_regime": "bull|bear|range",
    "position_advice": {"level": 0-1, "label": "空仓观察|轻仓试错|半仓参与|重仓进攻", "reason": "..."},
    "cards": [ {code,name,action,confidence,entry,stop,target,holding_days,reasons[],risk} ],
    "avoid": [ {code,name,reason} ],
    "strategy_summary": "...",
    "evidence": {"win_rate_1d":..., "sample":...}
  }

决策规则 (可解释, 每条都能追溯):
  1. 市场环境 → 总仓位上限 (bear≤30%, range≤60%, bull≤90%)
  2. 个股 action:
     buy  : 综合≥65 且 资金面≥60 且 高低位≥55 且 非高位套人
     watch: 综合≥55 或 (综合≥50 且 资金面≥70)
     avoid: 高位套人 或 (综合<45 且 资金面<40) 或 情绪面<30
  3. confidence: 综合分 × 历史1日胜率归一 × 共振度
  4. entry/stop/target: 基于 20日位 与 乖离 的保守估计
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


def load(name, default=None):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def market_regime(mn):
    """从 market_now 判断市场环境。"""
    m = (mn or {}).get("market") or {}
    adv, dec = m.get("advance", 0), m.get("decline", 0)
    tot = max(1, adv + dec)
    up_pct = m.get("up_pct", 50)
    zt = m.get("limit_up", 0)
    dt = m.get("limit_down", 0)
    net = (adv - dec) / tot * 100
    if net >= 8 and up_pct >= 58 and zt >= 50:
        return "bull", f"涨{adv}/跌{dec} 净{net:.0f}% 涨停{zt}"
    if net <= -8 or (dt >= 20 and up_pct <= 42):
        return "bear", f"涨{adv}/跌{dec} 净{net:.0f}% 跌停{dt}"
    return "range", f"涨{adv}/跌{dec} 净{net:.0f}% 涨停{zt}"


POSITION_CAP = {"bear": 0.3, "range": 0.6, "bull": 0.9}


def decide_stock(r, regime, win1d):
    """单股决策。返回 (action, confidence, reasons, risk)"""
    d = r.get("dims") or {}
    comp = r.get("composite", 0)
    cap = d.get("资金面", 50)
    pos = d.get("高低位", 50)
    fund = d.get("基本面", 50)
    sent = d.get("情绪面", 50)
    why = r.get("why") or {}
    pos_why = why.get("高低位", "")

    reasons, risk = [], ""
    action = "watch"

    high_trap = "高位套人" in pos_why
    low_absorb = "低位吸筹" in pos_why

    if high_trap:
        return "avoid", 0.0, [f"高位套人: {pos_why}"], "追高风险, 20日位>85%且乖离>15%"
    if comp < 45 and cap < 40:
        return "avoid", comp / 100 * 0.5, [f"综合{comp:.0f}偏低, 资金面{cap:.0f}弱"], "趋势与资金双弱"
    if sent < 30:
        return "avoid", 0.3, [f"情绪面{sent:.0f}过低, 逆市场情绪"], "市场情绪冰点, 不宜逆势"

    if comp >= 65 and cap >= 60 and pos >= 55:
        action = "buy"
        reasons.append(f"综合{comp:.0f} 资金{cap:.0f} 位置{pos:.0f} 三高共振")
    elif comp >= 55 or (comp >= 50 and cap >= 70):
        action = "buy" if regime != "bear" else "watch"
        reasons.append(f"综合{comp:.0f} 资金{cap:.0f}")
    else:
        action = "watch"
        reasons.append(f"综合{comp:.0f} 未达买入阈值(65)")

    if low_absorb:
        reasons.append("低位吸筹信号 (60日位<20%+主力流入+RSI<40)")
    if fund >= 70:
        reasons.append(f"基本面扎实 (PE/PB/市值: {why.get('基本面','')})")
    if regime == "bear":
        risk = "熊市环境, 降级为观察"
    if cap < 50:
        risk = (risk + " " if risk else "") + "资金面偏弱"

    # 置信度: 综合分 × 历史胜率 × 共振度
    resonance = r.get("resonance", 0.5)
    conf = comp / 100 * (win1d or 0.45) * (0.6 + 0.4 * resonance)
    return action, round(min(0.95, conf), 3), reasons, risk


def entry_stop_target(r):
    """保守的入场/止损/目标估计 (基于20日位与乖离)。"""
    why = (r.get("why") or {}).get("高低位", "")
    px = r.get("price") or 0
    if not px:
        return None, None, None
    if "低位" in why:
        return round(px * 1.005, 2), round(px * 0.93, 2), round(px * 1.12, 2)
    return round(px * 1.003, 2), round(px * 0.95, 2), round(px * 1.08, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "decision.json"))
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    md = load("multidim.json")
    mn = load("market_now.json")
    reflect = load("reflect_latest.json") or {}
    wm = load("winrate_matrix.json") or {}

    regime, regime_detail = market_regime(mn)
    ov = ((reflect.get("analysis") or {}).get("overall") or {})
    win1d = (ov.get("1") or {}).get("raw_win_rate")
    win1d = (win1d / 100) if win1d else None
    n1 = (ov.get("1") or {}).get("n") or 0

    top = (md or {}).get("top") or []
    cards, avoid = [], []
    for r in top:
        action, conf, reasons, risk = decide_stock(r, regime, win1d)
        entry, stop, target = entry_stop_target(r)
        rec = {
            "code": r.get("code"), "name": r.get("name"),
            "price": r.get("price"), "chg_pct": r.get("chg_pct"),
            "composite": r.get("composite"),
            "dims": r.get("dims"),
            "action": action, "confidence": conf,
            "entry": entry, "stop": stop, "target": target,
            "holding_days": 3 if regime != "bear" else 1,
            "reasons": reasons, "risk": risk,
            "why": r.get("why"),
        }
        if action == "avoid":
            avoid.append(rec)
        else:
            cards.append(rec)

    # 按 置信度 排序, buy 优先
    cards.sort(key=lambda x: (-(x["action"] == "buy"), -x["confidence"]))
    cards = cards[: args.top]

    pos_cap = POSITION_CAP[regime]
    n_buy = sum(1 for c in cards if c["action"] == "buy")
    # 建议仓位 = min(环境上限, 买入信号数×单票10%)
    level = min(pos_cap, n_buy * 0.10)
    # 标签按「建议仓位 / 环境上限」的比例, 避免 range 环境也叫重仓
    ratio = level / pos_cap if pos_cap else 0
    label = ("空仓观察" if level < 0.1 else
             "轻仓试错" if ratio < 0.35 else
             "半仓参与" if ratio < 0.7 else
             "顺势加仓")

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "decision v1 (9维因子 → 可执行决策卡)",
        "market_regime": regime,
        "regime_detail": regime_detail,
        "position_advice": {
            "level": round(level, 2),
            "label": label,
            "cap": pos_cap,
            "reason": f"市场环境={regime} ({regime_detail}), 建议总仓位≤{pos_cap*100:.0f}%",
        },
        "cards": cards,
        "avoid": avoid[:8],
        "strategy_summary": (
            f"{regime}市场下, {len(cards)}只候选 ({n_buy}只买入信号)。"
            f"历史1日胜率{(win1d or 0)*100:.1f}% (n={n1})。"
            f"建议{label}, 单票止损3-7%, 持有{3 if regime!='bear' else 1}日。" if cards else "无合格候选"),
        "evidence": {
            "win_rate_1d": round(win1d * 100, 1) if win1d else None,
            "sample_1d": n1,
            "sufficient": n1 >= 30,
            "note": "胜率来自 reflect_engine 对历史预测的前向验证 (无前视偏差)",
        },
        "meta": {
            "rules": "buy=综合≥65&资金≥60&位置≥55; watch=综合≥55; "
                     "avoid=高位套人/综合<45&资金<40/情绪<30",
            "confidence_formula": "综合分 × 历史1日胜率 × (0.6+0.4×共振度)",
            "caveat": "决策卡是统计规则输出, 非投资建议; 实盘需自行判断",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"[decision] 市场环境: {regime} | 仓位建议: {label} (≤{pos_cap*100:.0f}%)")
    print(f"[decision] 候选 {len(cards)} 只 (买入 {n_buy}), 回避 {len(avoid)} 只")
    for c in cards[: args.top][:8]:
        print(f"  [{c['action']}] {c['code']} {c['name']} 综合{c['composite']:.0f} "
              f"置信{c['confidence']:.2f} 入场{c['entry']} 止损{c['stop']} 目标{c['target']}")
    print(f"[decision] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
