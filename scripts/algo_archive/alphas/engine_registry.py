# engine_registry.py — 10 引擎统一注册表 (2026-10-21 重构)
# 目的: 合并去重 + 统一调用接口 + 单一置信融合
# 去重决策记录:
#   - NB 反向共识与 GBDT 同源特征 → 合并为 ml_ensemble 一票 (内部双共识, 不重复计票)
#   - dual_trend 的 ADX 与 117 因子的 trend 类因子重叠 → dual_trend 降权 0.8
#   - pead_alpha 待 fin_pit 全量, 与 acc_detect 同属事件/形态 → 预留注册, 权重 0 直到数据齐

ENGINES = {
    "orchestrator":  {"weight": 1.2, "src": "orchestrator.py",        "status": "active"},
    "factor117":     {"weight": 1.0, "src": "orchestrator.py",        "status": "active"},
    "v7_recoup":     {"weight": 1.0, "src": "test_v7.py",             "status": "active"},
    "moneyflow":     {"weight": 0.9, "src": "extract_mf_factors.py",  "status": "active"},
    "ml_ensemble":   {"weight": 1.0, "src": "alphas/train_ml_ensemble.py", "status": "active",
                      "note": "GBDT+NB 反向双共识合并为一票 (原 engine5+6 去重)"},
    "sentiment":     {"weight": 0.8, "src": "alphas/skill_engines.py: sentiment_phase", "status": "active"},
    "dual_trend":    {"weight": 0.8, "src": "alphas/skill_engines.py: dual_trend_score", "status": "active",
                      "note": "ADX 与 factor117 trend 类重叠 → 降权"},
    "calendar":      {"weight": 0.5, "src": "alphas/calendar_alpha.py", "status": "active",
                      "note": "乘数而非独立票, 仅调整总分"},
    "acc_detect":    {"weight": 0.7, "src": "alphas/acc_detect.py",   "status": "active"},
    "pead":          {"weight": 0.0, "src": "alphas/pead_alpha.py",   "status": "pending",
                      "note": "等 fin_pit 全量 (900/2048), 权重 0"},
}

def fuse_scores(scores: dict) -> dict:
    """scores: {engine: {sym: 0~1 分}} → 统一加权融合 + 票源追溯"""
    total, src = {}, {}
    for eng, sym_scores in scores.items():
        cfg = ENGINES.get(eng)
        if not cfg or cfg["weight"] <= 0:
            continue
        for sym, s in sym_scores.items():
            total[sym] = total.get(sym, 0) + cfg["weight"] * s
            src.setdefault(sym, []).append(eng)
    ranked = sorted(total.items(), key=lambda x: -x[1])
    return [{"sym": s, "score": round(v, 4), "engines": src[s],
             "confidence": "high" if len(src[s]) >= 4 else ("mid" if len(src[s]) >= 2 else "low")}
            for s, v in ranked]

# 2026-10-22 合并上游 (analysis-hub ai_picks) 的低位筹码策略:
# 锚点: 量比4-9+涨2-5%+240日低位 (全历史网格最优 avg+1.32%/win48%)
# CYQ 获利盘维度我方缺失 → 独立注册 (不与 pos240 因子重复: 它是筹码分布非价格位置)
ENGINES["lowpos_cyq"] = {"weight": 0.9, "src": "upstream:analysis-hub ai_picks",
                         "status": "active",
                         "note": "低位+获利盘筹码策略, 弱市实测 8票均值+0.08% vs 上证-0.89%"}

# 2026-10-22 gitcode 深度浏览合并 (dufuwuxin/stock-system 今晨 CI 活跃):
# - ban_prob_table.py: 29882 涨停事件连板实证表 → 纳入 sentiment/dual_trend 评分依据 (2板以下不打)
# - bull_bear_debate: 3角色辩论架构, 其否决率0%=形式化, 只借思想; 我方 11 引擎投票已是更强实现
# - auction_engine (腾讯分笔竞价): 需 level2 分笔数据, 暂无源, 挂 pending
ENGINES["ban_prob"] = {"weight": 0.6, "src": "upstream:gitcode ban_prob_table.py", "status": "active",
                       "note": "连板高度→次日胜率映射, 实证 29882 事件"}

# 2026-10-22 v2: acc_detect 吸收 gitcode flow_divergence 斜率法升级 (价斜率vs流斜率背离+波动压缩)
# acc_detect 权重 0.7 → 0.8 (证据链更严谨: 持续性过滤+防陈旧流)

# 2026-10-22 晚: PEAD 解锁上线 (fin_pit_up 28万行/4452票/1988-2026 全史 PIT 财务)
# 信号: 最新季报 EPS 同比 >30% (剔除 prev<0.05 扭亏失真) + 股价未大涨 (<10%)
ENGINES["pead"] = {"weight": 0.9, "src": "alphas/pead_alpha.py + db-fin-0 全史", "status": "active"}

# 2026-10-22 深夜: 玄学参与评分 (用户指示: 不投票但降权计分 0.2)
ENGINES["mystic"] = {"weight": 0.2, "src": "alphas/news_mystic.py: mystic_daily",
                     "status": "active", "note": "文化参考+可量化天气部分, 低权重计分不单独推票"}
# 外围关联 + 异动监测上线
ENGINES["overseas"] = {"weight": 0.7, "src": "alphas/overseas_alert.py", "status": "active",
                       "note": "恒生科技-5%实测定价: 外围联动计入市况判定"}

# ── 2026-10-23 模块化扩容: 五新引擎 (module_router 统一路由) ──
ENGINES["mf_behavior"] = {"weight": 0.9, "src": "alphas/moneyflow_behavior.py",
    "status": "active",
    "note": "主力行为五态(吸筹/洗盘/套人/派发/出逃), 新浪日线资金流+价量背离; 主力类模块本职权重最高"}
ENGINES["market_forecast"] = {"weight": 0.9, "src": "alphas/market_forecast.py",
    "status": "active",
    "note": "大盘四问(当日预测/复盘/明日预测/β联动), 35年统计先验+台阶关键位+日历乘数; 预测类模块本职"}
ENGINES["beta_impact"] = {"weight": 0.7, "src": "alphas/market_forecast.py: beta_impact",
    "status": "active",
    "note": "收益率法β回归: 高β联动票+反向票(恒β>1.6/<-0.2)"}
ENGINES["oversold"] = {"weight": 0.8, "src": "alphas/pattern_engines.py: oversold_rebound",
    "status": "active",
    "note": "超跌反弹(20日跌>15%+缩量止跌+低位), 35年反弹先验"}
ENGINES["lowhigh"] = {"weight": 0.6, "src": "alphas/pattern_engines.py: low_high_scan",
    "status": "active",
    "note": "240日分位低位/高位股扫描, 需配合 mf_behavior 吸筹态过滤"}
ENGINES["llm_council"] = {"weight": 0.0, "src": "alphas/llm_council.py",
    "status": "advisory",
    "note": "Qwen3-0.6B 本地 LLM (ModelScope 下载中): 只陈述解释+置信度, 不改投票权重 (用户定调)"}
# v6 additions (2026-10-24)
ENGINES_V6 = [
    {'name': 'youzi_engine', 'module': 'youzi_engine', 'desc': '龙虎榜游资含金量(资金30/净买25/卖压20/机构15/加项10)', 'weight': 0.8, 'category': 'lhb'},
    {'name': 'ak_adapter', 'module': 'ak_adapter', 'desc': 'akshare适配层: 北向/概念资金流/千股千评/股东户数/财务(已验证9源)', 'weight': 0.6, 'category': 'data'},
    {'name': 'structure_lens', 'module': 'structure_lens', 'desc': '结构市透镜: 指数vs等权背离(二八/八二)', 'weight': 0.9, 'category': 'regime'},
    {'name': 'sector_watch', 'module': 'sector_watch', 'desc': '49行业板块实时监控(资金向)', 'weight': 0.7, 'category': 'sector'},
    {'name': 'news_pulse', 'module': 'news_pulse', 'desc': '7x24快讯题材映射(消息向)', 'weight': 0.6, 'category': 'news'},
    {'name': 'emotion_gauge', 'module': 'emotion_gauge', 'desc': '涨停情绪温度计(情绪向)', 'weight': 0.7, 'category': 'sentiment'},
]
