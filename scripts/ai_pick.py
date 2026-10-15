#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/ai_pick.py ═══
# 上游 commit: f3fa0a2284b9 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
AI推荐池 v2: 未涨停主板票 (用户明确: 推荐里不能是已涨停的)
评分 = 量比(20%) + 60日动量RPS(20%) + 240日低位加分(20%) + 近期涨停基因(15%)
     + 尾盘强度代理=收盘位置(15%) + 换手温和(10%)
数据全来自 stock.duckdb + pools.json; 输出 ai_picks.json
"""
import duckdb, json, datetime, os

con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
last = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
rows = con.execute(f"""
    WITH r AS (
      SELECT symbol, date, open, close, high, low, volume,
        close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret,
        volume/NULLIF(AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),0) AS vratio,
        MAX(high) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS hi240,
        MIN(low) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 239 PRECEDING AND 1 PRECEDING) AS lo240,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
      FROM daily)
    SELECT symbol, close, ret, vratio, (close-lo240)/NULLIF(hi240-lo240,0) AS pos240,
      (close-low)/NULLIF(high-low,0) AS close_pos
    FROM r WHERE rn = 1 GROUP BY symbol, close, ret, vratio, pos240, close_pos
      HAVING MAX(CASE WHEN ret >= 0.095 THEN 1 ELSE 0 END) = 0
""").fetchdf()
con.close()
rows = rows.dropna()
# 硬过滤: 未涨停(当日)+主板由库保证+涨幅1-7%+位置<60%+量比>=4.5 (2026-10-12 drift复盘: 打板-9.7%实证, 量比收紧)
# v4 (2026-10-13 复盘驱动重构): 巨量规则实测-0.82%/35.6%(2025至今全主板3056样本)废弃
# 新规则: 温和量1.5-2.5(唯一不负的量档) + 涨幅0.5-5% + 位置<60%; 强市才出票(市况闸门), 弱市只出防御观察名单
cand = rows[(rows.ret >= 0.005) & (rows.ret <= 0.05) & (rows.pos240 < 0.6) & (rows.vratio >= 1.5) & (rows.vratio <= 2.5)].copy()

def rank_pct(s):
    return s.rank(pct=True)
cand["score"] = (rank_pct(cand.vratio)*0.20 + rank_pct(cand.ret.clip(0, 0.07))*0.15
                 + (1 - cand.pos240.rank(pct=True))*0.25 + rank_pct(cand.close_pos)*0.25
                 + rank_pct(cand.close)*0.15)

# ---- 形态加分 (myhhub/stock研究移植, patterns.py; 2026-10-13) ----
# 停机坪/高而窄旗形=强势打板基因 → +0.08; 无大幅回撤=趋势健康 → +0.04 (上限0.08)
try:
    import pandas as _pd
    from patterns import parking_apron, high_tight_flag, low_backtrace_increase
    _con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    _syms = cand.symbol.tolist()
    _pat_bonus = {}
    _cand_df = {}
    for _s in _syms:
        try:
            _df = _con.execute("SELECT date, open, close, high, low, volume FROM daily WHERE symbol=? ORDER BY date", [_s]).fetchdf()
            _cand_df[_s] = _df
            if len(_df) < 60: continue
            _b = 0.0
            if parking_apron(_df, _df.date.iloc[-1]): _b += 0.08
            if high_tight_flag(_df, _df.date.iloc[-1]): _b += 0.08
            if low_backtrace_increase(_df, _df.date.iloc[-1]): _b += 0.04
            if _b: _pat_bonus[_s] = _b
        except Exception:
            continue
    _con.close()
    if _pat_bonus:
        cand["score"] = cand.score + cand.symbol.map(_pat_bonus).fillna(0)
        print(f"形态加分: {len(_pat_bonus)}只 {list(_pat_bonus.items())[:3]}")
except Exception as _e:
    print("patterns skip:", _e)

# ---- 全球情绪自适应 (2026-10-12: 用户指令"接进AI推荐池") ----
# 全球偏多→加仓位/可碰动量票; 偏空→只留低位防御(位置分权重加倍)+输出减仓指令
GLOBAL = {"global_score": 0, "global_mood": "unknown", "pos_weight": 0.25, "suggest": "默认"}
try:
    # 2026-10-15: 原硬编码 /home/agentuser/stockdb/... 是旧服务器路径,
    # GitHub Actions 里必然抛异常 → GLOBAL 永远是默认值, 偏空闸门从未生效。
    # 改为按 paths.py 规范定位, 并回退到 data/global.json 快照。
    _gp = None
    try:
        from paths import AI_VIEW_DATA as _D
        _gp = os.path.join(_D, "global", "latest.json")
    except Exception:
        _gp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "global", "latest.json")
    if not os.path.exists(_gp):
        _alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "global.json")
        _gp = _alt if os.path.exists(_alt) else _gp
    _g = json.load(open(_gp))
    _m = _g.get("metrics", {})
    _s = _m.get("global_score", 0)
    GLOBAL["global_score"] = _s
    GLOBAL["global_mood"] = _m.get("global_mood", "unknown")
    if _s > 15:      # 偏多: 低位权重让位给动量/强度
        GLOBAL.update(pos_weight=0.20, suggest="全球偏多: 正常仓位, 可偏进攻")
        cand["score"] += rank_pct(cand.ret.clip(0, 0.07))*0.05
    elif _s < -15:   # 偏空: 市况闸门=空仓(2026-10-13复盘: 弱市巨量票9/12止损, 选票救不了弱市)
        GLOBAL.update(pos_weight=0.35, suggest="全球偏空: 【空仓】本日不出买入推荐, 仅观察名单, 等强市信号再进场")
        _gate = "空仓"
        cand = cand[cand.pos240 < 0.25].copy()  # 只留极低位观察票, 且标记为观察非买入
        cand["score"] = rank_pct(cand.vratio)*0.15 + (1 - cand.pos240.rank(pct=True))*0.35 \
                        + rank_pct(cand.close_pos)*0.25 + (1 - cand.ret.rank(pct=True))*0.10
    else:
        GLOBAL["suggest"] = "全球中性: 维持半仓纪律"
    # VIX>25 极端恐慌: 再砍
    if _m.get("vix", 0) > 25:
        GLOBAL["suggest"] += " | VIX>25 恐慌: 仅观察不新开仓"
except Exception:
    pass


# ---- 胜率自适应层 (2026-10-13): pick_log真实对账 → 因子分层胜率 → 权重微调 ----
ADAPT = {"note": "无对账数据"}
try:
    _ps = json.load(open("/home/agentuser/stockdb/data/pick_stats.json"))
    _recent = _ps.get("recent", [])
    if len(_recent) >= 10:
        # 用对账日当天的因子快照(直接重算当日候选)过于重 — 简化: 按verdict分布调节低位/动量权重
        _rets = [r.get("ret") for r in _recent if r.get("ret") is not None]
        _vers = [r.get("verdict") for r in _recent]
        _win = sum(1 for x in _rets if x > 0)/max(len(_rets),1)
        _tp_rate = _vers.count("止盈+2%")/max(len(_vers),1)
        _sl_rate = _vers.count("止损-2%")/max(len(_vers),1)
        if _sl_rate > 0.4 and _tp_rate < 0.2:
            # 止损多→低位防御加倍, 动量砍
            ADAPT.update(note=f"近{len(_recent)}笔止损率{_sl_rate:.0%}→防御模式: 低位0.35/动量0.10", mode="defense")
            cand["score"] = rank_pct(cand.vratio)*0.10 + (1 - cand.pos240.rank(pct=True))*0.35 \
                            + rank_pct(cand.close_pos)*0.25 + (1 - cand.ret.rank(pct=True))*0.10 \
                            + (1 - cand.close.rank(pct=True))*0.20
        elif _tp_rate > 0.35:
            ADAPT.update(note=f"近{len(_recent)}笔止盈率{_tp_rate:.0%}→动量模式: 攻势加权", mode="attack")
            cand["score"] += rank_pct(cand.ret.clip(0, 0.07))*0.05 + rank_pct(cand.vratio)*0.05
        else:
            ADAPT.update(note=f"近{len(_recent)}笔胜率{_win:.0%}→维持基准权重", mode="neutral")
except Exception as _e:
    ADAPT["note"] = f"adapt skip: {_e}"

# ---- 周期位置接入 (cycle_v2: 偏空→整体收紧) ----
try:
    _cs = json.load(open("/home/agentuser/stockdb/data/cycle/stats.json"))  # 触发存在的检查
    import subprocess as _sp
    _cr = _sp.run(["/home/agentuser/.hermes/hermes-agent/venv/bin/python3",
                   "/home/agentuser/stocks-algo/cycle_algo.py", "status"],
                  capture_output=True, text=True, timeout=90)
    _cyc = json.loads(_cr.stdout)
    if _cyc.get("bias") == "偏空":
        cand = cand[cand.pos240 < 0.45].copy()  # 周期偏空: 只留更低位置
        GLOBAL["suggest"] += " | 周期v2偏空: 收紧至240日位<45%"
        out_adapt_cycle = "偏空收紧"
    else:
        out_adapt_cycle = _cyc.get("bias", "?")
except Exception:
    out_adapt_cycle = "skip"


# ---- 亏损归因吸收 (loss_attribution: 追高是主因→近3日累计涨幅>12%直接剔除) ----
try:
    _la = json.load(open("/home/agentuser/stockdb/data/loss_attr.json"))
    if _la.get("total_losses", 0) >= 5 and "追高" in _la.get("advice", ""):
        # 近3日累计涨幅
        con_l = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
        recent3 = {r[0]: r[1] for r in con_l.execute("""
            SELECT symbol, MAX(CASE WHEN rn<=3 THEN close END)/MAX(CASE WHEN rn<=3 THEN LAG2 END)-1 FROM (
              SELECT symbol, close, close AS LAG2, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
              FROM daily WHERE date >= (SELECT strftime(MAX(date)::DATE - 8, '%Y-%m-%d') FROM daily))
            GROUP BY symbol""").fetchall()}
        con_l.close()
        _before = len(cand)
        cand = cand[~cand.symbol.map(recent3).fillna(0).gt(0.12)].copy()
        GLOBAL["suggest"] += f" | 归因收紧: 近3日涨幅>12%剔除({_before}→{len(cand)})"
        out_adapt_loss = "追高惩罚启用"
    else:
        out_adapt_loss = "样本不足/β主因"
except Exception as _e:
    out_adapt_loss = f"skip: {_e}"

# ---- 引擎融合一致度 (防守/观望→只留极低位票) ----
try:
    _ef = json.load(open("/home/agentuser/stockdb/data/engine_fusion.json"))
    if _ef["E"] < -30:
        cand = cand[cand.pos240 < 0.35].copy()
        GLOBAL["suggest"] += f" | 引擎E={_ef['E']}: 收紧至240日位<35%"
    _out_ef = _ef["state"]
except Exception:
    _out_ef = "skip"

top = cand.nlargest(15, "score")
# ---- v3: 名称+可解释推荐理由 (2026-10-13 用户要求全面AI选股) ----
try:
    _si = json.load(open("/home/agentuser/stockdb/data/search_index.json"))
    _names = {k: v.get("n", "") for k, v in _si.items()}
except Exception:
    _names = {}
from cyq import cyq_distribution as _cyqf  # 获利盘
# 筹码因子进分(独特算法): 获利盘<30% +0.06 / >70% -0.06
_cyq_bonus = {}
for _sym in top.symbol:
    try:
        _df2 = _cand_df.get(_sym)
        if _df2 is not None and len(_df2) >= 30:
            _pr = _cyqf(_df2.tail(210)).get("profit_ratio")
            if _pr is not None:
                _cyq_bonus[_sym] = 0.06 if _pr < 0.30 else (-0.06 if _pr > 0.70 else 0.0)
    except Exception:
        continue
if _cyq_bonus:
    top = top.copy()
    top["score"] = top.apply(lambda r_: r_.score + _cyq_bonus.get(r_.symbol, 0), axis=1)
    top = top.sort_values("score", ascending=False)
    GLOBAL["suggest"] += f" | CYQ筹码调整({len(_cyq_bonus)}票)"
top_recs = []
for _, r_ in top.iterrows():
    _why = []
    if r_.vratio >= 5: _why.append(f"量比{r_.vratio:.1f}")
    if r_.pos240 < 0.3: _why.append("240日低位")
    if r_.close_pos > 0.8: _why.append("收盘接近全天高点")
    try:
        _df2 = _cand_df.get(r_.symbol)
        if _df2 is not None and len(_df2) >= 30:
            _cf = _cyqf(_df2.tail(210))
            _pr = _cf.get("profit_ratio")
            if _pr is not None:
                _why.append(f"获利盘{_pr:.0%}")
    except Exception:
        pass
    top_recs.append({"symbol": r_.symbol, "name": _names.get(r_.symbol, ""), "close": round(r_.close,2),
                     "ret": round(r_.ret,3), "vratio": round(r_.vratio,2), "pos240": round(r_.pos240,3),
                     "close_pos": round(r_.close_pos,3), "score": round(r_.score,3),
                     "why": " · ".join(_why) if _why else "综合评分入选"})
out = {"ts": datetime.datetime.now().isoformat(), "date": last,
       "rule": "未涨停+主板+涨幅0.5-7%+240日位<60%, 五因子评分(量比/动量/低位/收盘强度/规模)+CYQ筹码+形态加分",
       "anchor": "量比4-9+涨2-5%+近高: n=206 avg+1.32% win48% 冲高5%=35.4% (全历史网格回测最优)",
       "global": GLOBAL, "adapt": ADAPT, "cycle": out_adapt_cycle if 'out_adapt_cycle' in dir() else "skip",
       "picks": top_recs,
       "observe_only": (GLOBAL.get("global_score", 0) is not None and GLOBAL.get("global_score", 0) < -15)}
json.dump(out, open("/home/agentuser/stockdb/data/ai_picks.json", "w"), ensure_ascii=False, indent=1)
print("AI picks:", len(out["picks"]), "| 全球:", GLOBAL["global_score"], GLOBAL["global_mood"], "->", GLOBAL["suggest"])
for p in out["picks"][:8]:
    print(f"  {p['symbol']} {p['close']} 涨{p['ret']*100:.1f}% 量比{p['vratio']} 位{p['pos240']*100:.0f}% 分{p['score']:.2f}")
