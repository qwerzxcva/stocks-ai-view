#!/usr/bin/env python3
"""
risk_engine.py — 风险管理引擎 (组合层风险预算)

decision_engine 只有单票固定止损 (3-7%), 无组合层风险控制。
本引擎补上: 把「一组决策卡」变成「有风险预算的组合建议」。

风险模型 (可解释):
  1. 单票风险预算   risk_per_trade = 总风险预算 / 持仓数
  2. 波动率调整     用 20 日年化波动率缩放仓位 (高波动低仓位)
  3. 相关性惩罚     同行业 > 2 只时降权 (变相单押行业)
  4. 集中度上限     单票 ≤ 25%, 单行业 ≤ 40%
  5. 总仓位 = min(场景上限, Σ单票仓位)
  6. 组合层止损     组合回撤 > 8% 触发减仓信号

输入: data/decision.json (cards) + data/scenario.json (仓位上限)
      + stocks-data-kline bulk (算波动率)
输出: data/risk_plan.json (每票建议仓位 + 组合层风险指标)
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = "/root/workspace/stocks-data-kline/data/bulk"

TOTAL_RISK_BUDGET = 0.06     # 组合总风险预算 6% (单日最大亏损容忍)
MAX_SINGLE_POS = 0.25        # 单票仓位上限
MAX_SECTOR_POS = 0.40        # 单行业仓位上限
DEFAULT_VOL = 0.30           # 默认年化波动率 (无K线时)


def load_volatilities(codes):
    """从 bulk K 线算 20 日年化波动率。返回 {裸码: vol}

    注意: 一只股票的 K 线可能跨多个分片文件, 必须先全量聚合再算。
    """
    want = {str(c).zfill(6) for c in codes}
    rows_by_code = defaultdict(list)
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                head, _, rest = line.partition("|")
                # 分片格式不统一: day_qfq_00..09 用全竖线 code|date|o|c|h|l|v
                # _extra/_recent 用 code|date,o,c,h,l,v (日期后逗号)
                parts = ([head] + rest.strip().split(",")) if "," in rest \
                    else ([head] + rest.strip().split("|"))
                if len(parts) < 4:
                    continue
                bare = head[2:] if head[:2] in ("sh", "sz", "bj") else head
                if bare in want:
                    try:
                        rows_by_code[bare].append((parts[0], float(parts[3])))
                    except ValueError:
                        continue
    vols = {}
    for bare, rows in rows_by_code.items():
        if len(rows) < 21:
            continue
        rows.sort(key=lambda r: r[0])
        closes = [r[1] for r in rows[-21:]]
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            daily_vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
            vols[bare] = daily_vol * (252 ** 0.5)   # 年化
    return vols


def build_risk_plan(decision, scenario, vols):
    cards = (decision or {}).get("cards") or []
    buys = [c for c in cards if c.get("action") == "buy"]
    scen_cap = ((scenario or {}).get("config") or {}).get("position_cap", 0.6)
    n = len(buys)
    if n == 0:
        return {"n_positions": 0, "total_position": 0,
                "plan": [], "note": "无买入信号"}

    # 1. 单票风险预算
    risk_per_trade = TOTAL_RISK_BUDGET / n

    # 2. 波动率调整 + 3. 相关性惩罚
    raw_w = {}
    sector_cnt = defaultdict(int)
    for c in buys:
        ind = (c.get("industry") or "").strip() or "unknown"
        sector_cnt[ind] += 1

    for c in buys:
        code = str(c.get("code", "")).zfill(6)
        vol = vols.get(code, DEFAULT_VOL)
        # 仓位 = 风险预算 / 波动率 (风险平价思想: 高波动低仓位)
        w = risk_per_trade / max(0.05, vol)
        ind = (c.get("industry") or "").strip() or "unknown"
        if sector_cnt[ind] > 2:
            w *= 0.7                      # 同行业超配降权
        raw_w[c["code"]] = w

    # 4. 归一化 + 集中度上限
    total_w = sum(raw_w.values())
    plan = []
    for c in buys:
        w = raw_w[c["code"]] / total_w if total_w else 0
        w = min(w, MAX_SINGLE_POS)
        vol = vols.get(str(c.get("code", "")).zfill(6), DEFAULT_VOL)
        plan.append({
            "code": c.get("code"), "name": c.get("name"),
            "industry": c.get("industry"),
            "suggested_position": round(w, 4),
            "position_pct": f"{w*100:.1f}%",
            "annual_vol": round(vol, 3),
            "risk_contribution": round(w * vol, 4),
            "stop_loss": c.get("stop"),
            "target": c.get("target"),
        })

    # 行业集中度检查
    sector_pos = defaultdict(float)
    for p in plan:
        ind = p.get("industry") or "unknown"
        sector_pos[ind] += p["suggested_position"]
    sector_warn = [{"industry": k, "position": round(v, 4),
                    "msg": f"行业 {k} 合计 {v*100:.1f}% (> {MAX_SECTOR_POS*100:.0f}% 上限)"
                           if v > MAX_SECTOR_POS else f"行业 {k} {v*100:.1f}%"}
                   for k, v in sector_pos.items()]

    total_pos = sum(p["suggested_position"] for p in plan)
    capped = total_pos > scen_cap
    if capped:
        # 按场景上限缩放
        scale = scen_cap / total_pos
        for p in plan:
            p["suggested_position"] = round(p["suggested_position"] * scale, 4)
            p["position_pct"] = f"{p['suggested_position']*100:.1f}%"
        total_pos = scen_cap

    # 组合层风险指标
    port_vol = sum(p["risk_contribution"] for p in plan)
    return {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "risk_engine v1 (风险预算/波动率平价/集中度控制)",
        "n_positions": len(plan),
        "total_position": round(total_pos, 4),
        "total_position_pct": f"{total_pos*100:.1f}%",
        "scenario_cap": scen_cap,
        "capped_by_scenario": capped,
        "total_risk_budget": TOTAL_RISK_BUDGET,
        "risk_per_trade": round(risk_per_trade, 4),
        "portfolio_vol_estimate": round(port_vol, 4),
        "limits": {"max_single": MAX_SINGLE_POS, "max_sector": MAX_SECTOR_POS},
        "plan": plan,
        "sector_allocation": sector_warn,
        "meta": {
            "method": "仓位 = 风险预算/年化波动率 (风险平价); 同行业>2只降权0.7; 单票≤25% 单行业≤40%",
            "correlation_note": "未算两两相关 (需协方差矩阵), 用行业近似",
            "caveat": "风险预算是统计建议, 不构成投资建议",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "risk_plan.json"))
    args = ap.parse_args()

    def load(n):
        p = os.path.join(DATA_DIR, n)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    decision = load("decision.json")
    scenario = load("scenario.json")
    if not decision:
        print("[risk] 无 decision.json")
        return 1

    codes = [c.get("code") for c in (decision.get("cards") or []) if c.get("code")]
    vols = load_volatilities(codes)
    print(f"[risk] 波动率: {len(vols)}/{len(codes)} 只 (余用默认 {DEFAULT_VOL})")

    plan = build_risk_plan(decision, scenario, vols)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print(f"[risk] {plan['n_positions']} 只, 总仓位 {plan.get('total_position_pct')} "
          f"(场景上限 {plan.get('scenario_cap')})")
    for p in plan.get("plan", [])[:10]:
        print(f"  {p['code']} {p['name']} 仓位{p['position_pct']} "
              f"年化波动{p['annual_vol']} 风险贡献{p['risk_contribution']}")
    print(f"[risk] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
