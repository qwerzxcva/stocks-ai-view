#!/usr/bin/env python3
"""
engine_hive.py — 引擎蜂巢中枢 v1 (2026-10-27 全新设计)
=========================================================
用户定调: 全部算法/引擎重新设计——
  1. 每个引擎计算内容互不重叠 (正交性是硬约束)
  2. 每个引擎内部独立多策略/多算法/多口径 (引擎内自洽, 不依赖其他引擎)
  3. 中枢只做调度+融合+冲突裁决, 不自己算票

与旧架构的本质区别:
  旧: 29 个注册引擎, 但推股链实际只用 multidim 9 维 → 引擎多数是摆设
  新: 8 个正交域引擎, 每个域内 N 个策略并行出票, 中枢跨域融合

八大正交域 (每域独立计算, 输入输出互不共享中间变量):
  E1 price_action   价格行为域   (纯K线形态/结构, 不用任何指标)
  E2 flow           资金流域     (量/额/主力方向, 不看价格形态)
  E3 regime         市场状态域   (大盘相位/情绪/风控仓位, 不看个股)
  E4 stat_factor    统计因子域   (横截面统计套利: 反转/低波/流动性)
  E5 ml_ensemble    机器学习域   (多标签多模型投票: nextday/strongup/p4hit)
  E6 event          事件域       (公告/龙虎榜/涨停池/日历, 非价格数据)
  E7 execution      执行域       (买区/止损/止盈/滑点估计, 不做选股)
  E8 risk           风险域       (回撤/相关性/拥挤度, 一票否决权)

输出协议: 每引擎返回 {"engine", "domain", "signals": [{sym, score, conf, why}], "meta"}
中枢融合: 域间加权(可配) + 冲突检测(E8一票否决) + 置信校准 → final_report.json
"""
import json, os, time, traceback

T1 = "/home/agentuser/stockdb/t1"
ALPHAS = f"{T1}/alphas"
OUT = "/tmp/hive_report.json"

# ───────────────────────── 引擎注册 ─────────────────────────
# 每个引擎: {module, entry, domain, strategies: [...], weight}
# strategies = 引擎内多策略清单 (同引擎内并行计算, 引擎自己聚合)
ENGINE_SPECS = [
    {"id": "E1", "domain": "price_action", "module": "hive_price_action", "entry": "run",
     "strategies": ["pure_structure", "candle_language", "range_position", "swing_map"],
     "weight": 1.0,
     "desc": "纯K线: N日结构/锤头吞没/区间位置/波段高低点, 零指标"},

    {"id": "E2", "domain": "flow", "module": "hive_flow", "entry": "run",
     "strategies": ["volume_anomaly", "moneyflow_state", "turnover_regime"],
     "weight": 1.0,
     "desc": "量额资金: 放缩量异动/主力五态/换手 regime, 不看K线形态"},

    {"id": "E3", "domain": "regime", "module": "hive_regime", "entry": "run",
     "strategies": ["phase_machine", "breadth_pulse", "limitup_ecology"],
     "weight": 1.0,
     "desc": "市场状态: 冰点/发酵/亢奋相位机+涨跌家数+涨停生态, 输出全局仓位系数"},

    {"id": "E4", "domain": "stat_factor", "module": "hive_stat_factor", "entry": "run",
     "strategies": ["short_reversal", "low_vol_shelter", "liquidity_prem", "seasonal_edge"],
     "weight": 0.9,
     "desc": "横截面统计: 短反转/低波避风/流动性溢价/日历边际, 纯排名无预测"},

    {"id": "E5", "domain": "ml_ensemble", "module": "hive_ml", "entry": "run",
     "strategies": ["nextday_head", "strongup_head", "p4hit_head", "disagree_gate"],
     "weight": 1.2,
     "desc": "ML 三头: nextday/strongup/p4hit 独立推理 + 分歧门控(三头打架降置信)"},

    {"id": "E6", "domain": "event", "module": "hive_event", "entry": "run",
     "strategies": ["lhb_seat_quality", "calendar_alpha", "zt_pool_chain"],
     "weight": 0.8,
     "desc": "事件: 龙虎榜席位质量/日历效应/涨停池梯队, 全部非价格输入"},

    {"id": "E7", "domain": "execution", "module": "hive_execution", "entry": "run",
     "strategies": ["atr_zone", "gap_tactic", "vwap_pullback"],
     "weight": 0.0,   # 不出票, 只给通过票补执行计划
     "desc": "执行: 对中枢选出的票计算 买区/止损/冲高目标/预期滑点"},

    {"id": "E8", "domain": "risk", "module": "hive_risk", "entry": "run",
     "strategies": ["crowding_veto", "drawdown_guard", "st_liquidity_veto"],
     "weight": 1.5,   # 否决权
     "desc": "风险: 拥挤度否决/回撤保护/ST与流动性硬过滤, 一票否决"},
]


def _call_engine(spec, ctx):
    """调用单引擎, 异常隔离 (单引擎挂不影响整体)"""
    t0 = time.time()
    try:
        import importlib
        mod = importlib.import_module(spec["module"])
        fn = getattr(mod, spec["entry"])
        res = fn(ctx)  # ctx = 共享只读数据快照 (行情/日期), 引擎内部互不通信
        res["meta"]["elapsed_s"] = round(time.time() - t0, 2)
        res["meta"]["ok"] = True
    except Exception as e:
        traceback.print_exc()
        res = {"engine": spec["id"], "domain": spec["domain"],
               "signals": [], "meta": {"ok": False, "err": str(e)[:120], "elapsed_s": round(time.time()-t0, 2)}}
    return res


# ───────────────────────── 中枢融合 ─────────────────────────
def fuse(results, ctx):
    """
    融合规则 (全部显式可审计):
      1. 收票: 仅 E1-E6 出 signals; E7 补执行计划; E8 出 veto
      2. 归一: 每引擎 score 在自身域内 rank-pct 到 [0,1] (跨引擎 score 量纲不可比, 只用排名)
      3. 加权: fused = Σ w_e * rank_pct_e  (引擎缺票=0, 不补)
      4. 冲突: E8 veto 票直接剔除; 同票被 ≥3 个域给 rank>0.9 → conf 提升 (共振加成)
      5. 输出: TOP N + 每票证据链 (哪些引擎为什么给分) + 全局仓位 (E3)
    """
    votes = {}
    for r in results:
        spec = next(s for s in ENGINE_SPECS if s["id"] == r["engine"])
        if spec["weight"] <= 0 or not r.get("signals"):
            continue
        sigs = sorted(r["signals"], key=lambda x: -x["score"])
        n = len(sigs)
        for i, s in enumerate(sigs):
            rp = 1 - i / max(n, 1)          # 域内 rank-pct
            v = votes.setdefault(s["sym"], {"fused": 0.0, "evidence": [], "domains": 0})
            v["fused"] += spec["weight"] * rp * s.get("conf", 1.0)
            v["domains"] += 1
            v["evidence"].append({"engine": r["engine"], "domain": spec["domain"],
                                  "strategy": s.get("strategy"), "rank_pct": round(rp, 3),
                                  "why": s.get("why", "")[:100]})

    # E8 否决
    veto = set()
    for r in results:
        if r["engine"] == "E8":
            for s in r.get("signals", []):
                if s.get("veto"):
                    veto.add(s["sym"])

    finals = []
    for sym, v in votes.items():
        if sym in veto:
            continue
        boost = 1.15 if v["domains"] >= 4 else (1.05 if v["domains"] >= 3 else 1.0)
        finals.append({"sym": sym, "fused": round(v["fused"] * boost, 4),
                       "domains": v["domains"], "evidence": v["evidence"]})
    finals.sort(key=lambda x: -x["fused"])

    # 全局仓位 (E3 相位机)
    regime = next((r for r in results if r["engine"] == "E3"), {})
    pos = regime.get("meta", {}).get("max_position", 0.5)

    # E7 补执行计划
    exec_plans = {}
    for r in results:
        if r["engine"] == "E7":
            exec_plans = {s["sym"]: s for s in r.get("signals", [])}

    top = finals[:30]
    for t in top:
        if t["sym"] in exec_plans:
            t["execution"] = exec_plans[t["sym"]]

    return {"as_of": ctx.get("date"), "max_position": pos,
            "picks": top, "vetoed": sorted(veto),
            "engines_ok": [r["engine"] for r in results if r.get("meta", {}).get("ok")],
            "engines_fail": [r["engine"] for r in results if not r.get("meta", {}).get("ok")]}


def run(date=None, top_n=30):
    """中枢主入口: 数据快照 → 8引擎并行 → 融合"""
    import duckdb
    con = duckdb.connect(f"{T1}/qfq_all.duckdb", read_only=True)
    con.execute("SET memory_limit='3GB'; SET temp_directory='/tmp/ducktmp'")
    d = con.execute("SELECT max(CAST(d AS DATE)) FROM qfq").fetchone()[0]
    px = con.execute("""
      SELECT sym, CAST(d AS DATE) d, o, c, h, l, v FROM qfq
      WHERE CAST(d AS DATE) >= ? - INTERVAL 120 DAY
      ORDER BY sym, d
    """, [d]).fetchdf()
    con.close()
    ctx = {"date": str(d), "px": px, "t1": T1, "top_n": top_n}

    results = []
    for spec in ENGINE_SPECS:
        results.append(_call_engine(spec, ctx))

    report = fuse(results, ctx)

    # 二次调用 E7: 把 TOP 候选补执行计划
    try:
        ctx2 = dict(ctx)
        ctx2["candidates"] = [p["sym"] for p in report["picks"]]
        e7 = _call_engine(next(s for s in ENGINE_SPECS if s["id"] == "E7"), ctx2)
        plans = {s["sym"]: s for s in e7.get("signals", [])}
        for t in report["picks"]:
            if t["sym"] in plans:
                t["execution"] = {k: plans[t["sym"]][k] for k in
                                  ("buy_zone", "stop", "target", "atr")}
    except Exception:
        pass
    report["engine_detail"] = [{"engine": r["engine"], "domain": r.get("domain"),
                                "n_signals": len(r.get("signals", [])),
                                "meta": r.get("meta")} for r in results]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, default=str)
    return report


if __name__ == "__main__":
    rep = run()
    print(json.dumps({k: rep[k] for k in ("as_of", "max_position", "engines_ok", "engines_fail")},
                     ensure_ascii=False))
    for p in rep["picks"][:10]:
        print(p["sym"], "fused", p["fused"], "域数", p["domains"])
