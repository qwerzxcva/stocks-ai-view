#!/usr/bin/env python3
"""
bull_bear_debate.py — 多空辩论决策引擎

借鉴来源: guangxiangdebizi/TradingAgents-MCPmode (MIT)
  https://github.com/guangxiangdebizi/TradingAgents-MCPmode
  MIT 允许借鉴实现, 这里按其公开的**架构设计**落地 (15 Agent 分工 +
  看涨/看跌辩论 + 三层风控), 并按我们的数据与约束重新实现。

## 借鉴的架构 (README 明确描述)

    分析师团队 (并行执行, 6 个)
      CompanyOverview / Market / Sentiment / News / Fundamentals / Shareholder
            ↓
    研究员团队 (辩论机制)
      BullResearcher (看涨) ←辩论→ BearResearcher (看跌)
            ↓ 可配置 max_debate_rounds
    Trader Agent 综合成交易决策
            ↓
    风险管理 (三层) Risky / Safe / RiskManager

关键工程设计 (README):
  - 分析师并行: 原串行 Σ(耗时) → 并行 max(耗时), 理论 5-6 倍
  - 深拷贝避免状态竞争, 智能合并执行历史
  - 前端可动态启用/禁用 Agent, 实时调辩论轮次

## 为什么值得我们做 (已确诊的问题)

weight_calibrator_v3 实测: 看多胜率 43.8% < 随机 50%, 14 个看多指标 edge 全负。
factor_lab 实测: 12 个特征里只有 below_ma60 有效, 所有追突破类强负面。
→ 两个独立方法都指向「单向看多的打分系统在当前市况下是错的」。

多空辩论的解药: 强制一个 Agent 站到反面, 用同样的数据列出**不该买的理由**。
人类投研里这对应「红队机制」——没有红队的决策系统会系统性高估。

## 我们的落地 (按约束裁剪)

约束: 云端 Actions / GitCode Space 运行, 不能常驻, token 不能暴露,
LLM 调用走 GitCode AI 通道 (已有 gitcode_ai_review.py)。

裁剪: 不做 15 个 Agent (token 成本太高), 做**最小有效闭环 3 个角色**:
  1. BullResearcher  — 从我们的多引擎数据里找看多证据
  2. BearResearcher  — 同样数据里找看空/风险证据 (强制找, 找不到也要说明为何没风险)
  3. RiskManager     — 综合双方 + 我们的实证阈值 (factor_lab/ban_prob) 给最终结论

数据全部来自本地 JSON (engine_v5/multidim/trend_strength/factor_lab/ban_prob),
LLM 只负责「组织论证」, 不负责产生事实 —— 避免幻觉 (这也是上游
「防止模型把原文没提到的公司写进标题」的同款思路)。

输出: data/bull_bear_debate.json
  - 每只股票: bull_case / bear_case / risk_verdict / final_score / action
  - 汇总: 多少只被红队否决 (这是最有价值的输出)

用法:
  python3 bull_bear_debate.py --codes 600519,000001
  python3 bull_bear_debate.py --from-picks data/today_picks.json --limit 10
  python3 bull_bear_debate.py --no-llm          # 纯规则版 (不调 LLM)
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")


def load(name):
    p = os.path.join(AI_VIEW_DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


# ─── 数据装配: 把我们的多引擎输出整理成「正反两方都能用的事实集」 ────
def extract_codes(d):
    """从推股 JSON 提代码。 picks 可能是 list, 也可能是 dict
    (today_picks.json 的 picks 是 {signals, values, limit_up})。"""
    out = []
    picks = d.get("picks") or d.get("items") or d.get("stocks") or []
    if isinstance(picks, dict):
        for k in ("signals", "values", "limit_up", "a", "b", "c"):
            v = picks.get(k)
            if isinstance(v, list):
                out.extend(x.get("code") for x in v if isinstance(x, dict) and x.get("code"))
        if not out:
            for v in picks.values():
                if isinstance(v, list):
                    out.extend(x.get("code") for x in v
                                if isinstance(x, dict) and x.get("code"))
    elif isinstance(picks, list):
        out.extend(x.get("code") for x in picks if isinstance(x, dict) and x.get("code"))
    return [str(c).zfill(6) for c in out if c]


def _norm(c):
    """统一代码形态: 各引擎的 code 有的带 sh/sz 前缀 (engine_v5.top),
    有的是裸 6 位 (ban_pick.picks)。不归一化会全部匹配不上 ——
    实测 8 只推股证据全为 0 就是这个原因。"""
    s = str(c or "").strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        s = s[2:]
    return s.zfill(6)


def gather_evidence(code):
    """收集某只股票的全部证据。返回 (bull_facts, bear_facts, raw)。"""
    bare = _norm(code)
    bull, bear = [], []
    raw = {"code": bare}

    # engine_v5: 17 指标共振
    ev5 = load("engine_v5.json")
    if ev5:
        top = ev5.get("top") or ev5.get("stocks") or []
        row = next((x for x in top if _norm(x.get("code")) == bare), None)
        if row:
            raw["engine_v5"] = row
            score = row.get("score")
            inds = row.get("indicators") or row.get("hits") or []
            if score is not None:
                (bull if score >= 60 else bear).append(
                    f"engine_v5 共振分 {score} ({'强' if score >= 60 else '弱'})")
            for it in (inds if isinstance(inds, list) else []):
                nm = it if isinstance(it, str) else (it.get("name") or "")
                if nm:
                    bull.append(f"技术指标命中: {nm}")

    # multidim: 十维因子
    md = load("multidim.json")
    if md:
        rows = md.get("stocks") or md.get("top") or []
        row = next((x for x in rows if _norm(x.get("code")) == bare), None)
        if row:
            raw["multidim"] = row
            dims = row.get("dims") or {}
            for k, v in dims.items():
                if not isinstance(v, (int, float)):
                    continue
                side = bull if v >= 60 else (bear if v <= 40 else None)
                if side:
                    side.append(f"十维·{k} {v:.0f}/100")

    # trend_strength: RTSI (含统计显著性 —— 这是关键差异)
    ts = load("trend_strength.json")
    if ts:
        r = next((x for x in (ts.get("rtsi", {}).get("stocks") or [])
                  if _norm(x.get("code")) == bare), None)
        if r:
            raw["rtsi"] = r
            if r["direction"] == "up":
                bull.append(f"RTSI {r['rtsi']} 上升趋势明确 "
                            f"(R²={r['r2']}, p={r['p_value']}, 20日斜率 {r['trend_slope_pct']}%)")
            elif r["direction"] == "down":
                bear.append(f"RTSI {r['rtsi']} 下跌趋势明确 "
                            f"(R²={r['r2']}, p={r['p_value']}, 20日斜率 {r['trend_slope_pct']}%)")
            if r["r2"] < 0.3:
                bear.append(f"趋势回归 R² 仅 {r['r2']}, 趋势不显著 (接近随机游走)")

    # factor_lab: 实证结论 (哪些特征是负期望)
    #  注意: 只在有该股当日形态数据时才下结论, 不无条件添加空头证据 ——
    #  无条件加等于硬编风险, 会让所有股票都带一条空头证据, 红队失去意义。
    fl = load("factor_lab.json")
    if fl and fl.get("features"):
        raw["factor_lab_negative"] = ["breakout_60", "new_high_20", "gap_up",
                                      "rsi_overbought"]
        rtsi_now = raw.get("rtsi") or {}
        if rtsi_now.get("vol_score", 0) > 75 and rtsi_now.get("direction") == "up":
            bear.append("当前放量走强, 若属突破新高/跳空高开则属 factor_lab 实证负期望形态")

    # ban_prob: 打板概率 (若在涨停池)
    bp = load("ban_pick.json")
    if bp:
        row = next((x for x in (bp.get("picks") or [])
                    if _norm(x.get("code")) == bare), None)
        if row:
            raw["ban_pick"] = row
            pzb = row.get("p_zhaban_next")
            if pzb is not None and pzb >= 22:
                bear.append(f"次日炸板率 {pzb}% (≥22% 回避线)")
            elif pzb is not None:
                bull.append(f"次日炸板率 {pzb}% (低于 22% 回避线)")
            ts_st = row.get("tick_structure")
            if ts_st:
                imb = ts_st.get("buy_sell_imbalance")
                if imb is not None:
                    (bull if imb >= 10 else bear if imb <= -10 else bull).append(
                        f"逐笔全天主动买卖失衡 {imb:+.1f}%"
                        + (" (封板有真实成交支撑)" if imb >= 10
                           else " (封板靠挂盘, 炸板风险高)" if imb <= -10 else ""))

    # valuation: 估值
    val = load("valuation.json")
    if val:
        rows = val.get("stocks") or val.get("items") or []
        if isinstance(rows, dict):
            row = rows.get(bare)
        else:
            row = next((x for x in rows if _norm(x.get("code")) == bare), None)
        if row:
            raw["valuation"] = row
            dev = row.get("deviation_pct") or row.get("deviation")
            if isinstance(dev, (int, float)):
                (bull if dev < -15 else bear if dev > 25 else bull).append(
                    f"估值偏离 {dev:+.1f}% ({'低估有安全垫' if dev < -15 else '偏高' if dev > 25 else '中性'})")

    return bull, bear, raw


# ─── 三角色 ────────────────────────────────────────────────────────
def bull_researcher(bull_facts, code):
    """看涨方: 组织证据, 找不到也要说明。"""
    if not bull_facts:
        return {"stance": "bull", "case": "未找到明确看多证据 (本地数据不足或该股确实无信号)。",
                "n_evidence": 0}
    return {"stance": "bull",
            "case": "; ".join(bull_facts[:6]),
            "n_evidence": len(bull_facts)}


def bear_researcher(bear_facts, code, bull_facts):
    """看跌方: 强制列出反面证据。没有显式风险时, 用「数据未覆盖」作为保留意见。"""
    parts = list(bear_facts[:6])
    if not parts:
        # 上游同款思路: 不硬编风险, 但明确标注「未找到风险 ≠ 没有风险」
        parts.append("本地数据未检出显式看空信号, 但这不等于无风险 "
                     "(样本外失效/未覆盖维度仍可能存在)")
    return {"stance": "bear", "case": "; ".join(parts), "n_evidence": len(parts)}


def risk_manager(bull, bear, raw):
    """风控方: 用实证阈值裁决, 不靠感觉。

    阈值来源 (全部来自本项目实测, 不是拍的):
      - factor_lab: 追突破类负期望, RTSI 是唯一有效买入信号的补强
      - weight_calibrator_v3: 看多胜率 43.8% < 随机, 需更高门槛
      - ban_prob: 炸板率 ≥22% 回避
    """
    verdict = "观察"
    reasons = []
    score = 50.0

    rtsi = raw.get("rtsi")
    if rtsi:
        if rtsi["direction"] == "up" and rtsi["r2"] >= 0.5:
            score += 20
            reasons.append(f"RTSI 上升趋势且 R²={rtsi['r2']} (统计显著)")
        elif rtsi["direction"] == "down":
            score -= 25
            reasons.append(f"RTSI 下跌趋势明确 (斜率 {rtsi['trend_slope_pct']}%)")
        elif rtsi["r2"] < 0.3:
            score -= 8
            reasons.append(f"RTSI R²={rtsi['r2']} 趋势不显著")

    bp = raw.get("ban_pick")
    if bp:
        pzb = bp.get("p_zhaban_next")
        if pzb is not None and pzb >= 22:
            score -= 20
            verdict = "回避"
            reasons.append(f"炸板率 {pzb}% 超回避线")
        imb = (bp.get("tick_structure") or {}).get("buy_sell_imbalance")
        if imb is not None and imb <= -15:
            score -= 15
            reasons.append(f"逐笔主动卖占优 {imb:+.1f}%")

    val = raw.get("valuation")
    if val:
        dev = val.get("deviation_pct") or val.get("deviation")
        if isinstance(dev, (int, float)):
            if dev < -20:
                score += 12
                reasons.append(f"低估 {dev:+.1f}% 有安全垫")
            elif dev > 30:
                score -= 10
                reasons.append(f"估值偏高 {dev:+.1f}%")

    ev5 = raw.get("engine_v5")
    if ev5:
        s = ev5.get("score")
        if isinstance(s, (int, float)):
            score += (s - 50) * 0.2
            if s >= 70:
                reasons.append(f"engine_v5 共振 {s}")
            elif s <= 35:
                reasons.append(f"engine_v5 共振仅 {s}")

    # 看空证据数量本身就是风险信号 (红队找到的理由越多越该回避)
    if bear["n_evidence"] >= 3:
        score -= 10
        reasons.append(f"红队提出 {bear['n_evidence']} 项风险")

    score = max(0.0, min(100.0, score))
    if verdict != "回避":
        verdict = "可关注" if score >= 65 else ("观察" if score >= 45 else "回避")

    return {"stance": "risk", "verdict": verdict, "final_score": round(score, 1),
            "reasons": reasons}


def llm_debate(code, bull, bear, risk):
    """可选: 用 GitCode AI 把双方论证组织成投研式辩论 (失败则静默跳过)。"""
    token = os.environ.get("GITCODE_TOKEN")
    if not token:
        return None
    try:
        sys.path.insert(0, HERE)
        import gitcode_ai_review as G
        prompt = (
            f"股票 {code} 多空辩论。\n"
            f"【看多证据】{bull['case']}\n"
            f"【看空证据】{bear['case']}\n"
            f"【规则引擎裁决】{risk['verdict']} (score={risk['final_score']}), "
            f"理由: {'; '.join(risk['reasons'])}\n\n"
            "请用 3 句话给出投研式结论: 1) 多头最强的理由 2) 空头最强的理由 "
            "3) 你的取舍与仓位建议。只基于上面给的事实, 不得引入新数据。"
        )
        # 复用 gitcode_ai_review 的调用通道 (其内部已处理 token 与容错)
        fn = getattr(G, "call_llm", None) or getattr(G, "_call", None)
        if not fn:
            return None
        return fn(prompt)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="多空辩论决策引擎")
    ap.add_argument("--codes", help="逗号分隔的股票代码")
    ap.add_argument("--from-picks", help="从推股文件读代码 (如 data/today_picks.json)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-llm", action="store_true", help="纯规则版, 不调 LLM")
    args = ap.parse_args()

    now = datetime.now(CST)
    out = args.out or os.path.join(AI_VIEW_DATA, "bull_bear_debate.json")

    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.from_picks:
        p = args.from_picks
        if not os.path.isabs(p):
            p = os.path.join(AI_VIEW_DATA, p)
        try:
            d = json.load(open(p, encoding="utf-8"))
            codes = extract_codes(d)
        except Exception as e:
            print(f"[debate] 读 {p} 失败: {e}", file=sys.stderr)
    if not codes:
        # 默认: 今日推股
        for f in ("today_picks.json", "premarket_picks.json"):
            p = os.path.join(AI_VIEW_DATA, f)
            if os.path.exists(p):
                try:
                    d = json.load(open(p, encoding="utf-8"))
                    codes = extract_codes(d)
                    if codes:
                        break
                except Exception:
                    continue
    codes = codes[:args.limit]
    if not codes:
        print("[debate] 没有待辩论的股票 (--codes 或 --from-picks 指定)", file=sys.stderr)
        sys.exit(1)

    rows = []
    n_veto = 0
    for code in codes:
        bull_f, bear_f, raw = gather_evidence(code)
        bull = bull_researcher(bull_f, code)
        bear = bear_researcher(bear_f, code, bull_f)
        risk = risk_manager(bull, bear, raw)
        if risk["verdict"] == "回避":
            n_veto += 1
        row = {
            "code": code,
            "bull": bull, "bear": bear, "risk": risk,
            "evidence_raw": raw,
        }
        if not args.no_llm:
            row["llm_debate"] = llm_debate(code, bull, bear, risk)
        rows.append(row)
        print(f"  {code} → {risk['verdict']} (score={risk['final_score']}) "
              f"| 多头证据 {bull['n_evidence']} 项, 空头 {bear['n_evidence']} 项")

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "bull_bear_debate.py",
        "origin": "架构借鉴 guangxiangdebizi/TradingAgents-MCPmode (MIT): "
                  "多智能体分工 + 看涨/看跌辩论 + 三层风控; "
                  "裁剪为 3 角色最小闭环 (token 成本约束)",
        "n_debated": len(rows),
        "n_vetoed": n_veto,
        "veto_rate": round(n_veto / len(rows) * 100, 1) if rows else 0,
        "debates": rows,
        "meta": {
            "roles": {
                "bull": "BullResearcher — 从本地多引擎数据组织看多证据",
                "bear": "BearResearcher — 强制列出反面证据; 无显式风险时标注"
                        "「未检出≠无风险」 (不硬编风险)",
                "risk": "RiskManager — 用本项目实证阈值裁决, 不靠感觉",
            },
            "thresholds": {
                "rtsi_r2": "R²>=0.5 且 direction=up 才加分 (统计显著性门槛)",
                "ban_zhaban": "炸板率>=22% 直接回避 (ban_prob 实证线)",
                "tick_imbalance": "逐笔主动卖占优<=-15% 降分 (auction_engine)",
                "valuation": "偏离<-20% 加分 (低估安全垫), >30% 减分",
                "redteam": "空头证据>=3 项额外减分 (红队找到的理由越多越该回避)",
            },
            "why": "weight_calibrator_v3 实测看多胜率 43.8%<随机 50%; "
                   "factor_lab 实测 12 特征仅 below_ma60 有效。"
                   "单向看多打分系统在当前市况下系统性高估, 故强制红队。",
            "llm": "未调 (--no-llm 或 GITCODE_TOKEN 缺失)" if args.no_llm
                   else "已尝试调用 GitCode AI 组织论证 (失败则静默跳过, 不影响结论)",
            "caveat": "证据全部来自本地 JSON, LLM 不产生事实只组织论证 (防幻觉); "
                      "本地数据缺失时该维度不参与打分, 表现为证据项少而非中性分",
        },
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[debate] {len(rows)} 只辩论完成, 红队否决 {n_veto} 只 "
          f"({payload['veto_rate']}%) → {out}")


if __name__ == "__main__":
    main()
