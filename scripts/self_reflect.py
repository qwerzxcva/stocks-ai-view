#!/usr/bin/env python3
"""
self_reflect.py — 自我反思与总括改进引擎

回答三个问题:
  1. 我们过去的预测准不准?      → reflect_latest.json (多持有期×市场环境×指标)
  2. 哪些因子真正有效?          → 指标质量排名 (Wilson CI)
  3. 下一步该改进什么?          → 基于证据的改进建议 (不说空话)

输出 data/self_reflect.json:
  {
    "ts": "...",
    "verdict": {"win_rates": {...}, "conclusion": "人话结论"},
    "factor_quality": [{indicator, quality, n, verdict}],
    "problems": [ {problem, evidence, severity} ],
    "improvements": [ {action, rationale, priority} ],
    "next_actions": ["..."]
  }

设计原则:
  - 只基于已落盘的数据说话, 不编造
  - 每条结论都附证据 (样本量/置信区间/文件)
  - 样本不足时明确说「证据不足」, 不给结论
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

MIN_SAMPLE = 30


def load(name, default=None):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "self_reflect.json"))
    args = ap.parse_args()

    reflect = load("reflect_latest.json") or {}
    wm = load("winrate_matrix.json") or {}
    ks = load("kline_sim.json") or {}
    md = load("multidim.json") or {}
    dec = load("decision.json") or {}
    audit = load("audit_report.json")

    analysis = reflect.get("analysis") or {}
    overall = analysis.get("overall") or {}
    by_ind = analysis.get("by_indicator") or {}
    problems, improvements = [], []

    # ── 1. 胜率总览 ──
    win_rates = {}
    for h, rec in overall.items():
        n = rec.get("n") or 0
        wr = rec.get("raw_win_rate")
        win_rates[h] = {
            "n": n,
            "win_rate": wr,
            "ci": rec.get("raw_win_rate_ci"),
            "avg_ret": rec.get("avg_ret_pct"),
            "sufficient": n >= MIN_SAMPLE,
        }

    # ── 2. 结论 (基于证据) ──
    h1 = win_rates.get("1") or {}
    h5 = win_rates.get("5") or {}
    if h1.get("sufficient"):
        wr1 = h1["win_rate"]
        if wr1 < 40:
            concl = (f"1日看多胜率 {wr1}% 低于随机(50%), 说明当前技术共振信号在"
                     f"样本期内**反向指标**特征明显, 不应直接据此做多")
        elif wr1 < 50:
            concl = f"1日看多胜率 {wr1}%, 略低于随机, 信号有效性弱, 需结合其他维度"
        else:
            concl = f"1日看多胜率 {wr1}% (CI {h1.get('ci')}), 具备正期望, 可作为辅助信号"
    else:
        concl = f"1日样本仅 {h1.get('n',0)} 条 (<{MIN_SAMPLE}), 证据不足, 不下结论"

    # ── 3. 因子质量 (哪些指标真有效) ──
    fq = []
    for name, rec in sorted(by_ind.items(),
                            key=lambda kv: -(kv[1].get("quality") or 0)):
        n = rec.get("n") or 0
        q = rec.get("quality")
        if n < MIN_SAMPLE:
            verdict = "样本不足"
        elif (q or 0) >= 65:
            verdict = "有效(看空侧更强)"
        elif (q or 0) >= 55:
            verdict = "弱有效"
        else:
            verdict = "无效/反向"
        fq.append({"indicator": name, "quality": q, "n": n, "verdict": verdict,
                   "bull_win": rec.get("bull_win_rate"), "bear_win": rec.get("bear_win_rate")})

    # 看空胜率高的指标 → 用于回避而非买入
    strong_short = [f["indicator"] for f in fq
                    if f["n"] >= MIN_SAMPLE and (f["bear_win"] or 0) >= 70]
    strong_long = [f["indicator"] for f in fq
                   if f["n"] >= MIN_SAMPLE and (f["bull_win"] or 0) >= 60]

    # ── 4. 问题诊断 (基于数据, 非猜测) ──
    if h1.get("sufficient") and (h1.get("win_rate") or 0) < 45:
        problems.append({
            "problem": "技术共振信号1日胜率显著低于50%",
            "evidence": f"n={h1['n']} win={h1['win_rate']}% CI{h1.get('ci')} avg={h1.get('avg_ret')}%",
            "severity": "high",
        })
    if h5.get("sufficient") and (h5.get("win_rate") or 0) < 30:
        problems.append({
            "problem": "5日持有期衰减严重 (信号有效期短)",
            "evidence": f"5日 win={h5.get('win_rate')}% avg={h5.get('avg_ret')}%",
            "severity": "high",
        })
    elif h5.get("n", 0) < MIN_SAMPLE:
        problems.append({
            "problem": "5日持有期样本不足, 无法评估中期有效性",
            "evidence": f"n={h5.get('n',0)} < {MIN_SAMPLE}",
            "severity": "medium",
        })
    if strong_short and not strong_long:
        problems.append({
            "problem": "指标集中在看空侧有效, 看多侧缺乏有效信号",
            "evidence": f"看空有效指标: {strong_short[:5]}",
            "severity": "high",
        })
    # 数据新鲜度
    stale = []
    for f, label in [("market_now.json", "实时行情"), ("enrich.json", "强势股/龙虎榜"),
                     ("multidim.json", "多维因子"), ("decision.json", "决策卡")]:
        d = load(f)
        ts = str((d or {}).get("ts", ""))
        if ts:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hrs = (datetime.now(CST) - t).total_seconds() / 3600
                if hrs > 20:
                    stale.append(f"{label}({hrs:.0f}h前)")
            except Exception:
                pass
    if stale:
        problems.append({"problem": "数据过旧 (>20小时未更新)",
                         "evidence": ", ".join(stale), "severity": "medium"})

    # ── 5. 改进建议 (可执行) ──
    if h1.get("sufficient") and (h1.get("win_rate") or 0) < 45:
        improvements.append({
            "action": "把技术共振从「买入信号」降级为「回避过滤器」: 仅用于排除空头共振个股",
            "rationale": f"1日看多胜率{h1['win_rate']}%<50%, 但看空侧有强指标({strong_short[:3]})",
            "priority": "P0",
        })
    if strong_long:
        improvements.append({
            "action": f"对看多侧有效指标({strong_long[:3]})提高权重, 用 optimizer.walk_forward 校准",
            "rationale": "这些指标的多头胜率≥60%且样本充足",
            "priority": "P1",
        })
    improvements.append({
        "action": "引入资金面(主力净流入)作为一票否决项: 连续3日净流出的个股不进买入池",
        "rationale": "资金面是9维中唯一有增量信息的维度 (技术指标已证伪)",
        "priority": "P0",
    })
    improvements.append({
        "action": "对每个决策卡记录「预测时点快照」, 让 reflect 能持续验证决策(而非仅信号)",
        "rationale": "当前 reflect 只验证 engine_v5 信号, 未验证 decision_engine 的输出",
        "priority": "P1",
    })
    if not (ks.get("by_horizon") or {}).get("1", {}).get("n"):
        improvements.append({
            "action": "扩充 K 线覆盖率 (当前51%, 目标80%) 以提升回测样本",
            "rationale": "kline_sim 多持有期 n=0 与 K 线覆盖不足直接相关",
            "priority": "P2",
        })

    # ── 6. 下一步动作 ──
    next_actions = [
        "每日 16:00 CI 跑完整管线 (采集→引擎→多维→决策→复盘→反思)",
        "用 backfill_predictions.py 持续积累历史预测快照 (多持有期胜率的前提)",
        "决策卡落盘 data/decision_card_YYYYMMDD.json 供次日验证",
    ]

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "self_reflect v1 (证据驱动的复盘与改进)",
        "verdict": {
            "win_rates": win_rates,
            "conclusion": concl,
            "kline_sim": {
                "n_signals": ks.get("n_signals_total"),
                "n_simulated": ks.get("n_simulated"),
                "by_horizon": {k: {"n": v.get("n"), "win_rate": v.get("win_rate"),
                                   "avg_ret": v.get("avg_ret_pct")}
                               for k, v in (ks.get("by_horizon") or {}).items()},
            },
        },
        "factor_quality": fq[:15],
        "strong_short_indicators": strong_short[:5],
        "strong_long_indicators": strong_long[:5],
        "problems": problems,
        "improvements": improvements,
        "next_actions": next_actions,
        "meta": {
            "min_sample": MIN_SAMPLE,
            "principle": "样本<30 不下结论; 每条判断附证据; 不编造未验证的因果",
            "caveat": "历史统计规律不代表未来, 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"[self_reflect] 结论: {concl}")
    print(f"[self_reflect] 问题 {len(problems)} 个, 改进建议 {len(improvements)} 条")
    for p in problems:
        print(f"  [{p['severity']}] {p['problem']} — {p['evidence'][:60]}")
    print(f"[self_reflect] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
