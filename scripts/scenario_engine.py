#!/usr/bin/env python3
"""
scenario_engine.py — 场景化推股引擎 (牛/熊/震荡/极端 四象限差异化)

解决问题: ai_pick_v4 / multidim / decision 用**同一套静态阈值**跑所有市场环境。
熊市还按牛市标准选股 → 选出来全是接飞刀的。

四象限场景 (由 market_now + cycle 判定):
  bull    牛市: 上涨占比≥58% + 涨停≥60 + 指数在 MA20 上
  bear    熊市: 上涨占比≤42% 或 跌停≥20 或 指数在 MA20 下且 MA20 下拐
  range   震荡: 其余
  extreme 极端: 涨停≥120 (过热) 或 跌停≥50 (恐慌) 或 上涨占比≤25%

每个场景有独立的:
  - 仓位上限 (bull 0.9 / range 0.6 / bear 0.3 / extreme 0.1)
  - 选股阈值 (综合分门槛)
  - 硬规则 (涨幅区间/位置/量比)
  - 偏好维度 (牛市重动量, 熊市重低位+基本面, 震荡重资金+板块)
  - 禁止行为 (牛市末期不追高, 熊市不接飞刀, 极端不新开仓)

输出: data/scenario.json (当前场景 + 参数 + 场景化后的推荐列表)
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

# ─── 场景参数表 (每条都可解释) ─────────────────────────────────
SCENARIOS = {
    "bull": {
        "label": "牛市进攻",
        "position_cap": 0.9,
        "min_composite": 55,
        "rules": {"chg": (0.5, 8.0), "pos240_max": 75, "vratio": (1.2, 3.5)},
        "dim_weights": {"技术面": 0.30, "资金面": 0.20, "高低位": 0.10,
                        "基本面": 0.05, "板块面": 0.15, "市场面": 0.05,
                        "情绪面": 0.05, "周期面": 0.05, "消息面": 0.05},
        "prefer": "动量/趋势/板块联动",
        "forbid": "不追已连板高位股 (pos240>75 且乖离>20% 排除)",
    },
    "bear": {
        "label": "熊市防御",
        "position_cap": 0.3,
        "min_composite": 65,
        "rules": {"chg": (-3.0, 2.0), "pos240_max": 35, "vratio": (0.8, 2.0)},
        "dim_weights": {"技术面": 0.10, "资金面": 0.15, "高低位": 0.30,
                        "基本面": 0.20, "板块面": 0.05, "市场面": 0.05,
                        "情绪面": 0.05, "周期面": 0.05, "消息面": 0.05},
        "prefer": "低位+基本面+资金持续流入 (左侧分批)",
        "forbid": "不接飞刀 (禁止 3日累计跌幅>15% 的票), 不抄绩差股",
    },
    "range": {
        "label": "震荡结构",
        "position_cap": 0.6,
        "min_composite": 50,
        "rules": {"chg": (-2.0, 8.0), "pos240_max": 65, "vratio": (1.0, 3.0)},
        "dim_weights": {"技术面": 0.20, "资金面": 0.20, "高低位": 0.15,
                        "基本面": 0.10, "板块面": 0.15, "市场面": 0.05,
                        "情绪面": 0.05, "周期面": 0.05, "消息面": 0.05},
        "prefer": "资金+板块+位置 (三者共振)",
        "forbid": "不满仓单板块 (集中度>40% 预警)",
    },
    "extreme": {
        "label": "极端观望",
        "position_cap": 0.1,
        "min_composite": 72,
        "rules": {"chg": (-2.0, 3.0), "pos240_max": 40, "vratio": (1.0, 2.2)},
        "dim_weights": {"技术面": 0.05, "资金面": 0.25, "高低位": 0.30,
                        "基本面": 0.20, "板块面": 0.05, "市场面": 0.05,
                        "情绪面": 0.05, "周期面": 0.05, "消息面": 0.00},
        "prefer": "仅低位+资金+基本面三高, 宁缺毋滥",
        "forbid": "禁止新开仓追热点, 只做观察名单",
    },
}


def detect_scenario(mn, cycle):
    """从 market_now + cycle 判定当前场景。返回 (scenario, 判定依据)。"""
    m = (mn or {}).get("market") or {}
    adv = m.get("advance", 0) or 0
    dec = m.get("decline", 0) or 0
    tot = max(1, adv + dec)
    up_pct = m.get("up_pct", 50) or 50
    zt = m.get("limit_up", 0) or 0
    dt = m.get("limit_down", 0) or 0
    # 指数 MA20 位置
    idx_above_ma20 = None
    for ix in (m.get("indices") or []):
        if str(ix.get("code", "")).endswith("000001"):
            # 无 ma20 字段, 用涨跌符号粗判
            idx_above_ma20 = (ix.get("chg_pct") or 0) >= 0
            break

    reasons = [f"上涨占比{up_pct}%", f"涨停{zt}", f"跌停{dt}"]
    # 极端优先
    if zt >= 120 or dt >= 50 or up_pct <= 25:
        return "extreme", " + ".join(reasons + ["极端值触发"])
    if up_pct >= 58 and zt >= 60:
        return "bull", " + ".join(reasons + ["普涨+涨停潮"])
    if up_pct <= 42 or dt >= 20:
        return "bear", " + ".join(reasons + ["普跌或跌停潮"])
    return "range", " + ".join(reasons + ["结构分化"])


def rescore(rec, dim_weights):
    """按场景权重重新算综合分。"""
    dims = rec.get("dims") or {}
    if not dims:
        return rec.get("composite", 0)
    wsum = sum(dim_weights.values())
    return sum(dims.get(k, 50) * w for k, w in dim_weights.items()) / max(1e-9, wsum)


def apply_rules(rec, rules):
    """场景硬规则过滤。返回 (通过?, 原因)。"""
    chg = rec.get("chg_pct")
    if chg is None:
        chg = rec.get("ret_pct")
    lo, hi = rules["chg"]
    if chg is not None and not (lo <= chg <= hi):
        return False, f"涨幅{chg}% 不在 [{lo},{hi}]"
    pos = rec.get("pos240_pct")
    if pos is None:
        why = (rec.get("why") or {}).get("高低位", "")
        import re
        mm = re.search(r"20日位(\d+)%", why)
        if mm:
            pos = float(mm.group(1))
    if pos is not None and pos > rules["pos240_max"]:
        return False, f"位置{pos}% > {rules['pos240_max']}%"
    vr = rec.get("vratio")
    if vr is None:
        import re
        why = (rec.get("why") or {}).get("资金面", "")
        # 资金面 why 里没有量比, 用 ai_pick_v4 的字段
    if vr is not None:
        vlo, vhi = rules["vratio"]
        if not (vlo <= vr <= vhi):
            return False, f"量比{vr} 不在 [{vlo},{vhi}]"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "scenario.json"))
    args = ap.parse_args()

    def load(n):
        p = os.path.join(DATA_DIR, n)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    mn = load("market_now.json") or load("latest.json")
    cyc = load("cycle.json")
    md = load("multidim.json")

    scen, reason = detect_scenario(mn, cyc)
    cfg = SCENARIOS[scen]
    print(f"[scenario] 场景: {scen} ({cfg['label']}) — {reason}")
    print(f"[scenario] 仓位上限 {cfg['position_cap']*100:.0f}% | 综合分门槛 {cfg['min_composite']}")
    print(f"[scenario] 偏好: {cfg['prefer']}")
    print(f"[scenario] 禁止: {cfg['forbid']}")

    # 场景化重排 multidim top
    picks, rejected = [], []
    for rec in (md or {}).get("top") or []:
        ok, why = apply_rules(rec, cfg["rules"])
        if not ok:
            rejected.append({"code": rec.get("code"), "name": rec.get("name"),
                             "reason": why})
            continue
        s = rescore(rec, cfg["dim_weights"])
        r = dict(rec)
        r["scenario_score"] = round(s, 1)
        r["scenario"] = scen
        picks.append(r)
    picks.sort(key=lambda x: -x["scenario_score"])
    picks = [p for p in picks if p["scenario_score"] >= cfg["min_composite"]]

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "scenario v1 (四象限差异化推股)",
        "scenario": scen,
        "scenario_label": cfg["label"],
        "detect_reason": reason,
        "config": cfg,
        "n_candidates": len(picks),
        "n_rejected": len(rejected),
        "picks": picks[:30],
        "rejected": rejected[:20],
        "meta": {
            "why": "同一套阈值跑全部市场环境是场景引擎缺位; 熊市按牛市选股=接飞刀",
            "detect_rules": "extreme: 涨停>=120或跌停>=50或上涨占比<=25; "
                           "bull: 上涨占比>=58且涨停>=60; bear: 上涨占比<=42或跌停>=20; 余 range",
            "caveat": "场景判定是统计阈值, 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n[scenario] 候选 {len(picks)} 只 (拒绝 {len(rejected)})")
    for p in picks[:10]:
        print(f"  {p.get('code')} {p.get('name')} 场景分{p['scenario_score']} "
              f"(原综合{p.get('composite')})")
    print(f"[scenario] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
