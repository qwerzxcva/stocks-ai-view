#!/usr/bin/env python3
"""engine_spec.py — 双端算法规格（单一声明源）与黄金向量生成器。

## 为什么需要这个文件

云端（本仓 `scripts/engine_v5.py` 等）与手机端（`skapp-app/engine-algo` 的 Kotlin）
各自实现同一批引擎。此前两端的指标集合、投票规则、聚合公式、阈值全都是各自写死的：

    云端 engine_v5 : 21 个指标各自 vote/strength → Σw·v·s / Σw → 50+50·base·(0.6+0.4·resonance)
    端上 LocalEngine: 12 个引擎各给 0~100 分 → 加权平均 → 族级共振

同一只票在两边能差出 20 分以上，而界面上都叫「引擎评分」。

本文件把「算法的事实」收敛成两份可机读产物：

    data/engine_spec.json            规格：引擎清单、参数、阈值、聚合常数、可用性
    data/parity/golden_v5.json       黄金向量：真实 K 线 → 参考实现的逐票结果

端上把 spec 当资源读、把 golden 当测试夹具读。任何一端改了公式而另一端没跟，
parity 测试立刻红——这才是「双端算法一致」的可执行定义，而不是口头约定。

## 用法

    python3 scripts/engine_spec.py --out data/engine_spec.json
    python3 scripts/engine_spec.py --golden data/parity/golden_v5.json --samples 6 --bars 130
    python3 scripts/engine_spec.py --verify data/parity/golden_v5.json   # 检测漂移

## 口径红线

- `--golden` 用 **engine_v5.build_votes / score_stock 本身**做参考实现，不另写一份公式，
  否则黄金向量只是第三份实现，验证不了任何东西。
- 只导出技术面 17 票。板块共振 / 主力资金 / 涨停池三票依赖云端外部数据，端上拿不到，
  spec 里标 `availability: ["cloud"]`，不进黄金向量。
- K 线取真实分片（`stocks-data-kline/data/bulk`），不造假数据；样本股按代码排序固定抽取，
  保证同一份数据下 golden 可复现。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 规格版本：任何会改变评分结果的定义变更都必须升版。
# 端上启动时比对 spec_version，不一致就标「算法漂移」而不是静默用旧逻辑。
SPEC_VERSION = "3.1.0"
SPEC_ID = "skapp-engine-spec"


def load_calibration():
    """读 riskBudget/regime 的回测校准结果（scripts/calibrate_risk_regime.py 产出）。

    参数进规格是「可追溯」的前提：经验值写死在脚本里，六个月后没人说得清 0.18 是哪来的；
    写进规格并带上数据跨度与样本外指标，端上启动就能看到同一份依据。
    文件不存在时返回 None，产品注册表退回原经验值并标 calibration=missing。
    """
    p = os.path.join(ROOT, "data", "risk_regime_params.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


CALIB = load_calibration()

sys.path.insert(0, HERE)
from engines_v2 import FLOW_P, TAPE_P, ROT_P, VOL_P  # noqa: E402  参数单源：两端菜单都读这里

# ── 聚合与打分（唯一来源，两端都读这里）─────────────────────────────
AGGREGATION = {
    "kind": "weighted_vote",
    # score = score_center + score_scale * base * (resonance_base + resonance_span * resonance)
    "score_center": 50.0,
    "score_scale": 50.0,
    "resonance_base": 0.6,
    "resonance_span": 0.4,
    # resonance = max(bull, bear) / (bull + bear)；无方向票时为 0
    "resonance_kind": "directional_share",
    # 信号档：>= bullish 为多，<= bearish 为空，其余中性
    "signal_bullish": 65.0,
    "signal_bearish": 35.0,
    "round": {"score": 1, "resonance": 3, "strength": 3},
}

# ── 运行时策略（端上引擎链按此执行，不再各写一份魔数）──────────────
RUNTIME = {
    "min_bars": 30,
    "screen_bars": 120,
    "snapshot_stale_ms": 300000,
    "engine_cache_ms": 45000,
    "missing_vote_policy": "drop",          # 输入不足的票直接丢弃，不按 0 计
    "ai_pick_basis": "local-score",         # 端上不给 LLM，界面必须标出来
}

# 权重：从 weight_calibrator 的产物读，缺失时等权。
# 2026-10-29 修：engine_weights.json 此前是「只写不读」的死产物（engine_v5.main() 从不加载它，
# `--weights` 参数也不存在），校准结果对评分毫无影响。现在权重进 spec，两端都读同一份，
# 云端 engine_v5.py 也支持 --weights（默认就是这个文件）。
WEIGHTS_FILE = os.path.join(ROOT, "data", "engine_weights.json")

# ── 引擎清单 ────────────────────────────────────────────────────────
# 每项：id（两端唯一标识）/ family（族，用于族级共振）/ weight_key（权重文件里的键）/
#       vote_rule（可读规则）/ strength_rule / params（两端共用的参数）/ availability
# params 里的数字就是两端唯一来源：改阈值只改这里，不再两端各改一处。
VOTE_ENGINES = [
    {
        "id": "supertrend", "family": "trend", "weight_key": "SuperTrend",
        "availability": ["cloud", "device"],
        "params": {"n": 10, "multiplier": 3.0, "strength": 0.9},
        "vote_rule": "supertrend.trend (1/-1)",
        "strength_rule": "const(strength) when trend != 0 else 0",
        "note": "趋势族主力；trend 为 0（未形成）时不投票",
    },
    {
        "id": "ma_golden", "family": "trend", "weight_key": "MA5/MA20",
        "availability": ["cloud", "device"],
        "params": {"fast": 5, "slow": 20, "deadband_pct": 0.3, "strength_scale_pct": 3.0},
        "vote_rule": "dev>deadband→1, dev<-deadband→-1, else 0 (dev=MAfast/MAslow-1 的百分数)",
        "strength_rule": "min(|dev|/strength_scale_pct, 1)",
        "note": "0.3% 死区用于过滤均线贴合时的抖动",
    },
    {
        "id": "rsi", "family": "momentum", "weight_key": "RSI",
        "availability": ["cloud", "device"],
        "params": {"period": 14, "oversold": 30, "overbought": 70, "strong_lo": 45,
                   "strong_hi": 60, "strong_strength": 0.4, "neutral_strength": 0.2},
        "vote_rule": "rsi<oversold→1, rsi>overbought→-1, 45<=rsi<=60→1, else 0",
        "strength_rule": "超卖 (oversold-rsi)/oversold；超买 (rsi-overbought)/30；强势 0.4；中性 0.2",
        "note": "超买不直接给负票以外的高强度：RSI 高位钝化在牛市是常态",
    },
    {
        "id": "macd", "family": "momentum", "weight_key": "MACD",
        "availability": ["cloud", "device"],
        "params": {"fast": 12, "slow": 26, "strength_floor": 0.3},
        "vote_rule": "sign(EMA_fast - EMA_slow)",
        "strength_rule": "max(min(|hist-prev_hist|/max(|prev_hist|,0.01),1), strength_floor)",
        "note": "strength 衡量的是「柱体变化」而不是柱体绝对值",
    },
    {
        "id": "kdj", "family": "momentum", "weight_key": "KDJ(RSV)",
        "availability": ["cloud", "device"],
        "params": {"period": 9, "mid": 50.0},
        "vote_rule": "rsv>mid→1, rsv<mid→-1（rsv 为 9 日 RSV）",
        "strength_rule": "|rsv-mid|/mid",
        "note": "云端只用 RSV 单点，不用 K/D 平滑；端上若用完整 KDJ 会造成口径差异",
    },
    {
        "id": "boll_keltner", "family": "channel", "weight_key": "BOLL/Keltner",
        "availability": ["cloud", "device"],
        "params": {"ema_n": 20, "atr_n": 10, "k": 2.0, "upper": 0.7, "lower": 0.3},
        "vote_rule": "pos>upper→1, pos<lower→-1, else 0（pos=(close-lower)/(upper-lower)）",
        "strength_rule": "|pos-0.5|*2",
        "note": "云端名义写 BOLL，实际调用的是 Keltner(EMA±k·ATR)；以代码为准，spec 记 keltner",
    },
    {
        "id": "donchian", "family": "channel", "weight_key": "Donchian20",
        "availability": ["cloud", "device"],
        "params": {"n": 20, "near_high_pct": -1.0, "strength_scale_pct": 8.0, "strength_cap": 0.5},
        "vote_rule": "dev>near_high_pct→-1, else 0（不给突破正票）",
        "strength_rule": "min(|dev|/strength_scale_pct,1)*strength_cap",
        "note": "factor_lab 实测追突破为全历史负期望（new_high_20 胜率 40.76%），故只给负票",
    },
    {
        "id": "cmo", "family": "momentum", "weight_key": "CMO",
        "availability": ["cloud", "device"],
        "params": {"n": 14, "threshold": 20.0, "strength_scale": 50.0},
        "vote_rule": "cmo>threshold→1, cmo<-threshold→-1, else 0",
        "strength_rule": "min(|cmo|/strength_scale,1)",
        "note": "",
    },
    {
        "id": "vortex", "family": "trend", "weight_key": "Vortex",
        "availability": ["cloud", "device"],
        "params": {"n": 14, "strength_scale": 5.0},
        "vote_rule": "vi_plus>vi_minus→1 else -1",
        "strength_rule": "min(|vi_plus-vi_minus|*strength_scale,1)",
        "note": "只有两种方向，永不中性——族级共振靠 family 折叠来抵消",
    },
    {
        "id": "ultimate_osc", "family": "momentum", "weight_key": "UltimateOsc",
        "availability": ["cloud", "device"],
        "params": {"n1": 7, "n2": 14, "n3": 28, "lo": 30.0, "hi": 70.0, "mid": 50.0,
                   "strength_active": 0.4, "strength_neutral": 0.2},
        "vote_rule": "lo<uo<hi and uo>mid→1；uo>=hi→-1；lo<=uo<=mid→0",
        "strength_rule": "vote!=0 → strength_active，否则 strength_neutral",
        "note": "",
    },
    {
        "id": "choppiness", "family": "structure", "weight_key": "Choppiness",
        "availability": ["cloud", "device"],
        "params": {"n": 14, "trend_below": 40.0, "ma_n": 20},
        "vote_rule": "chop<trend_below → sign(close-SMA_ma_n)；否则 0",
        "strength_rule": "trend 时 (50-chop)/50，震荡时 0.1",
        "note": "震荡市闸门：无趋势时不投方向票",
    },
    {
        "id": "hull_ma", "family": "trend", "weight_key": "HullMA",
        "availability": ["cloud", "device"],
        "params": {"n": 9, "strength_scale_pct": 2.0},
        "vote_rule": "close>hma→1 else -1",
        "strength_rule": "min(|close-hma|/hma*100/strength_scale_pct,1)",
        "note": "",
    },
    {
        "id": "elder_force", "family": "volume", "weight_key": "ElderForce",
        "availability": ["cloud", "device"],
        "params": {"n": 13, "strength_scale": 100000.0},
        "vote_rule": "ef>0→1 else -1",
        "strength_rule": "min(|ef|/(|ef|+strength_scale),1)",
        "note": "strength 是饱和函数，不会因成交额放大而失真",
    },
    {
        "id": "connors_rsi", "family": "momentum", "weight_key": "ConnorsRSI",
        "availability": ["cloud", "device"],
        "params": {"rsi_n": 3, "streak_n": 2, "rank_n": 100, "lo": 20.0, "hi": 80.0,
                   "neutral_strength": 0.2},
        "vote_rule": "crsi<lo→1, crsi>hi→-1, else 0",
        "strength_rule": "超卖 (lo-crsi)/lo；超买 (crsi-hi)/20；中性 0.2",
        "note": "需要 100 根以上 K 线才有 rank 分量",
    },
    {
        "id": "ad_line", "family": "volume", "weight_key": "A/D Line",
        "availability": ["cloud", "device"],
        "params": {"lookback": 10},
        "vote_rule": "sign(ad[-1]-ad[-1-lookback])",
        "strength_rule": "min(|Δad|/(|ad[-1]|+1),1)",
        "note": "要求序列长度 >= lookback+1",
    },
    {
        "id": "pivot", "family": "structure", "weight_key": "Pivot",
        "availability": ["cloud", "device"],
        "params": {"strength": 0.4},
        "vote_rule": "close>pp→1 else -1（pp=(H+L+C)/3）",
        "strength_rule": "const(strength)",
        "note": "只用当根的枢轴，不是多日结构",
    },
    {
        "id": "vwma", "family": "volume", "weight_key": "VWMA",
        "availability": ["cloud", "device"],
        "params": {"n": 20, "strength_scale_pct": 2.0},
        "vote_rule": "close>vwma→1 else -1",
        "strength_rule": "min(|close-vwma|/vwma*100/strength_scale_pct,1)",
        "note": "",
    },
    {
        "id": "below_ma60", "family": "position", "weight_key": "below_ma60",
        "availability": ["cloud", "device"],
        "params": {"ma_n": 60, "min_bars": 80, "core_dev_lo": -10.0, "core_dev_hi": -5.0,
                   "near_high_pct": 0.98, "ret20_cap": 15.0,
                   "deep_dev_pct": -25.0, "overheat_dev_pct": 20.0,
                   "strength_core": 0.9, "strength_deep": 0.5},
        "vote_rule": "core 区间 -10<=dev<-5 且未近高且 20 日涨幅<=15 → +1；"
                     "dev<deep 且 dev<-25 → -1；dev>overheat → momentum_overheat -1",
        "strength_rule": "core 0.9 / deep 0.5 / overheat min(dev/100,0.8)",
        "note": "唯一有实测正 edge 的技术特征（factor_lab 292 万样本胜率 51.39%）",
    },
    {
        "id": "sector_resonance", "family": "external", "weight_key": "SectorResonance",
        "availability": ["cloud"],
        "params": {},
        "vote_rule": "行业板块当日涨幅共振（云端 all_sources）",
        "strength_rule": "由板块强度决定",
        "note": "端上无全市场行业映射的权威快照，无法复刻 → 只在云端计票",
    },
    {
        "id": "capital_flow", "family": "external", "weight_key": "CapitalFlowConsistency",
        "availability": ["cloud"],
        "params": {},
        "vote_rule": "主力资金方向与价格方向一致性",
        "strength_rule": "由净额占成交额比例决定",
        "note": "端上只有换手/量比代理（见 flow 引擎），不等于真资金流",
    },
    {
        "id": "limit_pool", "family": "external", "weight_key": "InZTPool",
        "availability": ["cloud"],
        "params": {"strength_lianban_scale": 6.0, "dt_strength": 0.9},
        "vote_rule": "在涨停池→+1（强度按连板数）；在跌停池→-1",
        "strength_rule": "min(lianban/6,1) / dt_strength",
        "note": "端上用 snapshot 的 atLimitUp 近似，但拿不到连板数 → 不作主票",
    },
]

# ── 产物注册表（product registry）────────────────────────────────────
# 一份数据产物的「契约」：谁写、算什么层、顶层字段、依赖、端上是否消费。
# 端上消费的项必须有 device 标记；contract_check.py 按 top_fields 校验落盘产物，
# 少一个字段就是契约破裂（历史上 market_phase.json 被 demo 覆盖就是这类事故）。
PRODUCTS = [
    {"id": "engine_v5", "path": "data/engine_v5.json", "writer": "scripts/engine_v5.py",
     "layer": "engine", "version": "v5", "availability": ["cloud", "device"],
     "top_fields": ["ts", "engine", "n_scanned", "n_scored", "market_asof", "data_quality",
                    "freshness", "indicators", "votes_observed", "formula", "top", "stale_top", "meta"],
     # optional_fields: 新增/诊断字段，老产物缺失只算 WARN —— 契约闸门不该因为
     # 「昨晚的产物还没重生成」而变红，那属于新鲜度问题（audit_freshness 管）。
     "optional_fields": ["votes_observed"],
     "item_fields": ["code", "score", "raw", "resonance", "bull", "bear", "neutral", "votes",
                     "date", "price", "bars", "data_fresh"],
     "params": {"score_center": 50.0, "score_scale": 50.0, "resonance_base": 0.6,
                "resonance_span": 0.4, "stale_days_limit": 0, "top_n": 50,
                "main_board_only": True, "weights_file": "data/engine_weights.json"},
     "inputs": ["kline_bulk", "all_sources"], "external_inputs": ["industry_map", "moneyflow", "limit_pool"]},

    {"id": "decision", "path": "data/decision.json", "writer": "scripts/decision_engine.py",
     "layer": "decision", "version": "v2", "availability": ["cloud", "device"],
     "top_fields": ["ts", "engine", "market_regime", "regime_detail", "position_advice",
                    "cards", "avoid", "strategy_summary", "evidence", "meta"],
     "item_fields": [],
     "params": {"bull": {"net_ge": 8, "up_pct_ge": 58, "limit_up_ge": 50},
                "bear": {"net_le": -8, "limit_down_ge": 20, "up_pct_le": 42},
                "position_cap": {"bear": 0.3, "range": 0.6, "bull": 0.9}},
     "inputs": ["multidim", "market_now"], "external_inputs": []},

    {"id": "market_phase", "path": "data/market_phase.json", "writer": "scripts/phase_runner.py",
     "layer": "engine", "version": "v2", "availability": ["cloud", "device"],
     "top_fields": ["ts", "dates", "phases", "phase", "label", "recent", "structure", "source"],
     "item_fields": ["date", "first_board", "ge2", "height", "promo"],
     "params": {"days": 90, "limit_caliber": "limit_caliber"},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "engine_fusion", "path": "data/engine_fusion.json", "writer": "scripts/engine_fusion.py",
     "layer": "fusion", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "E", "state", "agreement", "parts", "missing_inputs", "n_missing", "note"],
     "item_fields": [],
     "params": {"weights": {"market_phase": 0.30, "cycle_v2": 0.25, "global": 0.20,
                            "win_loop": 0.15, "kelly": 0.10},
                "agree": 0.6, "attack": 30.0, "defend": -30.0},
     "inputs": ["market_phase", "kline_sim", "global", "pick_log"], "external_inputs": []},

    {"id": "strategy_card", "path": "data/strategy_card.json", "writer": "scripts/strategy_card.py",
     "layer": "decision", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "strategy", "source", "backtest", "gate", "candidates", "discipline",
                    "picks_db", "note"],
     "item_fields": [], "params": {"candidate_limit": 12},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "rps_rotation", "path": "data/rps_rotation.json", "writer": "scripts/rps_rotation.py",
     "layer": "engine", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "source", "date", "n_universe", "top", "note"],
     "item_fields": ["symbol", "rps_250", "rps_120", "rps_60", "rps_20", "strong_score"],
     "params": {"windows": [250, 120, 60, 20], "top_n": 30,
                "strong_score": "mean(rps_250,rps_120,rps_60,rps_20)"},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "cycle_pool_v2", "path": "data/cycle_pool_v2.json", "writer": "scripts/cycle_pool_v2.py",
     "layer": "engine", "version": "v2", "availability": ["cloud", "device"],
     "top_fields": ["meta", "status", "yearly_kline", "annual_rotation", "regimes", "cycle_law",
                    "demand_shift", "stocks", "stocks_meta"],
     "item_fields": ["code", "name", "industry", "chg_pct", "reason", "algorithm", "price"],
     "params": {}, "inputs": ["kline_bulk", "industry_map"], "external_inputs": []},

    {"id": "ai_pick_v4", "path": "data/ai_pick_v4.json", "writer": "scripts/ai_pick_v4.py",
     "layer": "engine", "version": "v4", "availability": ["cloud", "device"],
     "top_fields": ["ts", "engine", "date", "data_date", "stale", "provenance", "n_scanned",
                    "n_candidates", "global", "adapt", "cycle_note", "picks", "meta"],
     "item_fields": ["code", "symbol", "date", "name", "price", "score", "pattern_bonus"],
     "params": {"rank_weights": {"vratio": 0.20, "mom60": 0.20, "pos240_inv": 0.20,
                                 "zt_genes": 0.15, "close_pos": 0.15, "ret": 0.10},
                "pattern_bonus": {"停机坪/高而窄旗形": 0.08, "无大幅回撤": 0.04}},
     "inputs": ["kline_bulk", "global_fx"], "external_inputs": ["patterns"]},

    {"id": "multidim", "path": "data/multidim.json", "writer": "scripts/multidim_engine.py",
     "layer": "engine", "version": "v1", "availability": ["cloud"],
     "top_fields": ["ts", "engine", "n_scanned", "weights", "top", "meta"],
     "item_fields": ["code", "name", "price", "composite", "dims", "why", "resonance"],
     "params": {"weights": {"技术面": 0.22, "资金面": 0.14, "高低位": 0.13, "基本面": 0.09,
                            "板块面": 0.10, "市场面": 0.08, "情绪面": 0.07, "周期面": 0.05,
                            "消息面": 0.05, "海外面": 0.07}},
     "inputs": ["snapshot", "sector_map", "global_markets", "linkage"],
     "external_inputs": ["eastmoney_fflow"]},

    {"id": "key_levels", "path": "data/key_levels.json", "writer": "scripts/key_levels.py",
     "layer": "engine", "version": "v1", "availability": ["cloud"],
     "top_fields": ["ts", "date", "engine", "n", "breadth", "fields", "stocks"],
     "item_fields": ["price", "kline_date", "pivot", "boll", "keltner"],
     "params": {"min_bars": 30, "boll": {"n": 20, "k": 2.0},
                "keltner": [[10, 10, 1.5], [20, 10, 2.0], [30, 10, 2.5]]},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "trend_strength", "path": "data/trend_strength.json", "writer": "scripts/trend_strength.py",
     "layer": "engine", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "engine", "origin", "rtsi", "tma", "msci", "meta"],
     "item_fields": ["code", "rtsi"],
     "params": {"rtsi_window": 20, "limit": 500},
     "inputs": ["kline_bulk", "global_markets"], "external_inputs": []},

    {"id": "premarket_picks", "path": "data/premarket_picks.json", "writer": "scripts/premarket_picks.py",
     "layer": "decision", "version": "v1", "availability": ["cloud"],
     "top_fields": ["ts", "date", "session", "engine", "status", "reason", "picks", "source_json"],
     "item_fields": ["code", "name", "price", "score", "resonance", "buyable", "why"],
     "params": {"resonance": "min(n_src*12, 36)", "engine_avg": "mean(0-100)*0.3",
                "valuation": "clamp(-dev*0.3, ±15)", "position": "15-|pos240-55|*0.25",
                "flow": {"buy": 12, "sell": -10}},
     "inputs": ["engine_v5", "multidim", "ai_pick_v4", "flow_divergence"], "external_inputs": []},

    {"id": "today_picks", "path": "data/today_picks.json", "writer": "scripts/build_today_picks.py",
     "layer": "decision", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "date", "session", "generated_by", "market", "overseas", "picks",
                    "winrate", "review", "entry_plans", "engines", "disclaimer"],
     "item_fields": [], "params": {}, "inputs": ["engine_v5", "market_now"], "external_inputs": []},

    {"id": "limit_history", "path": "data/limit_history.json", "writer": "scripts/build_limit_history.py",
     "layer": "data", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["ts", "engine", "coverage", "caliber", "caveat", "days", "warnings"],
     "item_fields": [],
     "params": {"caliber": "limit_caliber", "abs_pct_guard": 35.0},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "market_breadth_history", "path": "data/market_breadth_history.json",
     "writer": "scripts/build_market_breadth.py", "layer": "data", "version": "v1",
     "availability": ["cloud", "device"],
     "top_fields": [],  # 顶层是 JSON 数组
     "item_fields": ["date", "limit_up", "limit_down", "broken", "seal_rate_pct", "advance",
                     "decline", "flat", "up_pct", "amount_yi"],
     "params": {"history_limit": 120},
     "inputs": ["market_now"], "external_inputs": ["eastmoney_zt_pool"]},

    {"id": "factor_composite", "path": "data/factor_composite.json",
     "writer": "scripts/factor_composite.py", "layer": "engine", "version": "v1",
     # 端上未复刻（Kotlin 侧无实现），声明不得超前于现实
     "availability": ["cloud"],
     "top_fields": ["ts", "engine", "version", "n_scanned", "weights", "top", "meta"],
     "item_fields": ["code", "z", "score", "factors"],
     "params": {"winsor_pct": 2.0, "factors": {"mom20": 0.25, "mom60": 0.20, "low_vol": 0.20,
                                               "turnover": 0.15, "dev_ma20": 0.20}},
     "inputs": ["kline_bulk"], "external_inputs": []},

    {"id": "sector_rotation", "path": "data/sector_rotation.json",
     "writer": "scripts/sector_rotation.py", "layer": "engine", "version": "v1",
     # 端上未复刻（Kotlin 侧无实现），声明不得超前于现实
     "availability": ["cloud"],
     "top_fields": ["ts", "engine", "sectors", "leaders", "meta"],
     "item_fields": ["sector", "rps", "ret5", "ret20", "n", "state"],
     "params": {"windows": [5, 20, 60], "state_bull": 60.0, "state_bear": 40.0},
     "inputs": ["kline_bulk", "industry_map"], "external_inputs": []},

    {"id": "risk_budget", "path": "data/risk_budget.json",
     "writer": "scripts/risk_budget.py", "layer": "decision", "version": "v1",
     # 端上不消费该产物；端上侧对应物是 vol_target 产品
     "availability": ["cloud"],
     "top_fields": ["ts", "engine", "E", "regime", "budget", "kelly", "vol_target", "gates", "meta"],
     "item_fields": [],
     # 2026-10-29 校准：vol_target/max_pos 由 calibrate_risk_regime.py 在 5 指数 500 个交易日
     # （2024-10-08~2026-10-28）上网格 + 时间三折得到，不是经验值。改这里必须重跑校准并升版。
     "params": {"vol_target_annual": ((CALIB or {}).get("params") or {}).get("vol_target_annual", 0.18),
                "max_pos": ((CALIB or {}).get("params") or {}).get("max_pos", 0.6),
                "half_kelly": 0.5, "drawdown_guard": -0.15,
                "regime_cap": (CALIB or {}).get("regime_cap") or {"bull": 0.9, "range": 0.6, "bear": 0.3}},
     "calibration": "data/risk_regime_params.json" if CALIB else "missing(经验值)",
     "inputs": ["engine_fusion", "market_phase", "pick_log"], "external_inputs": []},

    # 波动率目标闸门：端上自算（本地日级存档 60 日全市场平均涨幅），参数与 risk_budget 同源。
    {"id": "vol_target", "path": "(device-inline)", "writer": "skapp-app VolTargetGate.kt / scripts/engines_v2.py",
     "layer": "decision", "version": "v1", "availability": ["cloud", "device"],
     "top_fields": ["scale", "realizedVol", "status"],
     "item_fields": [],
     "params": {"vol_target_annual": ((CALIB or {}).get("params") or {}).get("vol_target_annual", 0.12),
                "window": 60, "floor": 0.2,
                "max_pos": ((CALIB or {}).get("params") or {}).get("max_pos", 0.8),
                "min_sample": 30},
     "artifact": False,
     "parity_golden": "data/parity/golden_v2.json#vol_target",
     "inputs": ["market_days_archive"], "external_inputs": []},

    # 市场状态机（regime）：本轮只做到「参数校准 + 接线判定」，未接入仓位链路。
    # decision.wire=true 时才允许把它写进实盘仓位；当前依据见 calibration.criteria。
    {"id": "regime", "path": "(inline)", "writer": "scripts/calibrate_risk_regime.py",
     "layer": "engine", "version": "v1", "availability": ["cloud"],
     "top_fields": ["state", "params", "calibration"],
     "item_fields": [],
     "params": {"ma_fast": ((CALIB or {}).get("params") or {}).get("ma_fast", 30),
                "ma_slow": ((CALIB or {}).get("params") or {}).get("ma_slow", 90),
                "dwell": ((CALIB or {}).get("params") or {}).get("dwell", 5),
                "regime_cap": (CALIB or {}).get("regime_cap") or {"bull": 0.9, "range": 0.6, "bear": 0.3}},
     "artifact": False,
     "calibration": "data/risk_regime_params.json" if CALIB else "missing",
     "inputs": ["index_kline"], "external_inputs": []},
]

# ── 单一口径表：任何脚本要这些常数就查这里，不再各写一份 ──────────────
CALIBERS = {
    "limit_up": {"impl": "scripts/limit_caliber.py", "tolerance_pct": 0.15,
                 "prefix_pct": {"688/689/30": 20.0, "43/83/87/88/92": 30.0, "ST": 5.0,
                                "default": 10.0}},
    "main_board": {"impl": "scripts/limit_caliber.py:is_main_board",
                   "prefix": ["600", "601", "603", "605", "000", "001", "002"]},
    "indicator_lib": {"impl": "scripts/indicators_ext.py",
                      "consumers": ["engine_v5.py", "key_levels.py", "trend_strength.py"]},
}

# ── 状态类引擎注册表（flow / tape / rotation / vol_target）──────────
# 这四个不参与 21 票加权，但同样会进决策展示，必须有：参数单源、权威方、黄金向量。
# 2026-10-29 之前它们在云端根本没有参考实现，端上改了公式没有任何东西会红——
# 现在云端实现是 `scripts/engines_v2.py`，黄金向量是 `data/parity/golden_v2.json`，
# 端上 `EngineV2ParityTest` / `VolTargetParityTest` 逐字段比对。
STATE_ENGINES = [
    {"id": "flow", "version": 1, "device_impl": "FlowEngine.kt",
     "cloud_impl": "scripts/engines_v2.py:flow_screen",
     "authority": "dual", "availability": ["cloud", "device"],
     "tiers": {"local": 0.70, "local_main": 0.85, "cloud": 0.95, "local_abs": 0.42},
     "params": FLOW_P, "parity_golden": "data/parity/golden_v2.json#flow",
     "inputs": ["live_row"], "external_inputs": ["main_net_5d_series"]},
    {"id": "tape", "version": 1, "device_impl": "TapeEngine.kt",
     "cloud_impl": "scripts/engines_v2.py:tape_compute",
     "authority": "dual", "availability": ["cloud", "device"],
     "items": ["buy_proxy", "divergence", "shadow", "gap", "close_pos5"],
     "params": TAPE_P, "parity_golden": "data/parity/golden_v2.json#tape",
     "inputs": ["kline", "live_row"], "external_inputs": []},
    {"id": "rotation", "version": 1, "device_impl": "RotationEngine.kt",
     "cloud_impl": "scripts/engines_v2.py:rotation_compute",
     "authority": "dual", "availability": ["cloud", "device"],
     "params": ROT_P, "parity_golden": "data/parity/golden_v2.json#rotation",
     "inputs": ["sector_rows"], "external_inputs": ["sector_mom5", "amount_share_delta"]},
    {"id": "vol_target", "version": 1, "device_impl": "VolTargetGate.kt",
     "cloud_impl": "scripts/engines_v2.py:vol_target_compute",
     "authority": "dual", "availability": ["cloud", "device"],
     "params": VOL_P, "parity_golden": "data/parity/golden_v2.json#vol_target",
     "min_sample": 30, "inputs": ["market_days_archive"], "external_inputs": []},
]

# ── 权重策略：谁能写、谁读、缺失时怎么办 ──────────────────────────
WEIGHT_POLICY = {
    "file": "data/engine_weights.json",
    "writer": "scripts/weight_calibrator_v2.py",
    "candidate_writer": "scripts/weight_calibrator.py",  # 只写 *.candidate.json
    "report_only": ["scripts/weight_calibrator_v3.py"],  # 写 weight_calibration.json
    "reader": "scripts/engine_v5.py",
    "load_order": ["--weights 显式路径", "data/engine_weights.json", "等权 1.0"],
    "missing_fallback": "equal",
    "provenance_field": "meta.weights / meta.weights_used",
    "bounds": [0.6, 1.5],
}

# ── 端上引擎链（DAG）定义 ───────────────────────────────────────────
# 端上流水线的阶段与依赖也由 spec 声明：增删阶段只改这里，两端不再各写一份顺序。
PIPELINE_STAGES = [
    {"id": "snapshot", "needs": [], "produces": ["market_rows"], "blocking": True,
     "desc": "全市场实时盘口（东财 clist）"},
    {"id": "pulse", "needs": ["snapshot"], "produces": ["breadth", "phase_score", "label"],
     "blocking": True, "desc": "宽度/涨跌停/中位涨幅 → 相位"},
    {"id": "screen", "needs": ["snapshot"], "produces": ["candidates"],
     "blocking": False, "desc": "候选池筛选 + 逐只日 K 评分"},
    {"id": "sectors", "needs": ["snapshot"], "produces": ["sector_rows"],
     "blocking": False, "desc": "板块强弱聚合"},
    {"id": "rotation", "needs": ["sectors"], "produces": ["rotation"], "blocking": False,
     "desc": "板块轮动状态"},
    {"id": "execution", "needs": ["screen", "pulse", "snapshot"], "produces": ["flow", "tape"],
     "blocking": False, "desc": "资金流/盘口结构引擎"},
    {"id": "emotion", "needs": ["snapshot"], "produces": ["limit_board", "emotion_series"],
     "blocking": False, "desc": "连板高度/晋级率/封板率"},
    {"id": "regime", "needs": ["pulse", "emotion"], "produces": ["regime"], "blocking": False,
     "desc": "市场状态机"},
    {"id": "risk", "needs": ["regime", "rotation", "emotion"], "produces": ["risk_budget"],
     "blocking": False, "desc": "风险预算/仓位闸门"},
    {"id": "decision", "needs": ["screen", "risk", "regime"], "produces": ["cards", "avoid"],
     "blocking": False, "desc": "决策卡与回避清单"},
]


def load_weights(path=None):
    """读校准权重；任何异常都退回空表（= 等权），不阻断 spec 生成。"""
    path = path or WEIGHTS_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        w = data.get("weights") if isinstance(data, dict) else None
        return {str(k): float(v) for k, v in (w or {}).items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def build_spec():
    """组装 spec 字典（纯函数，便于测试与 verify 比对）。"""
    device_ids = [e["id"] for e in VOTE_ENGINES if "device" in e["availability"]]
    weights = load_weights()
    return {
        "spec_id": SPEC_ID,
        "spec_version": SPEC_VERSION,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "stocks-ai-view/scripts/engine_spec.py",
        "reference_impl": "stocks-ai-view/scripts/engine_v5.py (build_votes/score_stock)",
        "aggregation": AGGREGATION,
        "runtime": RUNTIME,
        "weights": weights,
        "weights_source": os.path.relpath(WEIGHTS_FILE, ROOT) if weights else "equal",
        "engines": VOTE_ENGINES,
        "products": PRODUCTS,
        "calibers": CALIBERS,
        "weight_policy": WEIGHT_POLICY,
        "device_engines": device_ids,
        "cloud_only_engines": [e["id"] for e in VOTE_ENGINES if "device" not in e["availability"]],
        "pipeline": PIPELINE_STAGES,
        # 状态类引擎注册表：不参与 21 票加权，但必须有规格（参数 + 黄金向量 + 权威方）。
        "state_engines": STATE_ENGINES,
        "calibration": ({
            "src": "data/risk_regime_params.json",
            "generated_at": CALIB.get("ts"),
            "data": CALIB.get("data"),
            "params": CALIB.get("params"),
            "oos": (CALIB.get("calibration") or {}).get("oos"),
            "oos_buy_hold": (CALIB.get("calibration") or {}).get("oos_buy_hold"),
            "decision": CALIB.get("decision"),
        } if CALIB else {"src": "missing"}),
    }


# ── 黄金向量 ────────────────────────────────────────────────────────
def _read_bars(bulk_dir, want_codes, bars):
    """从分片 CSV 读 (code → bars)。列: code|date|open|close|high|low|vol。"""
    need = set(want_codes) | {"__seen__"}
    series = {}
    files = sorted(f for f in os.listdir(bulk_dir) if f.startswith("day_qfq_") and f.endswith(".csv"))
    for name in files:
        with open(os.path.join(bulk_dir, name), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                code = parts[0]
                if code not in need and len(series) >= 400:
                    continue
                try:
                    row = (parts[1], float(parts[2]), float(parts[3]),
                           float(parts[4]), float(parts[5]), float(parts[6]))
                except ValueError:
                    continue
                series.setdefault(code, []).append(row)
    out = {}
    for code in sorted(series):
        rows = series[code]
        if len(rows) < bars + 1:
            continue
        out[code] = rows[-bars:]
        if len(out) >= len(want_codes):
            break
    return out


def _reference():
    """导入云端参考实现（不复制公式）。"""
    sys.path.insert(0, HERE)
    import engine_v5  # noqa: E402
    return engine_v5


def expect_from_bars(rows):
    """由一组 bar 算出参考实现应有的 score / votes。纯函数，供 build 与 verify 共用。"""
    ev5 = _reference()
    close = [r[2] for r in rows]
    high = [r[3] for r in rows]
    low = [r[4] for r in rows]
    vol = [r[5] for r in rows]
    result = ev5.score_stock(close, high, low, vol, weights=load_weights())
    if not result:
        return None
    votes = ev5.build_votes(close, high, low, vol)
    return {
        "score": result["score"],
        "resonance": result["resonance"],
        "raw": result["raw"],
        "bull": result["bull"],
        "bear": result["bear"],
        "neutral": result["neutral"],
        # why 文本不进黄金向量：它是给人看的，措辞变化不该让 parity 变红
        "votes": [{"indicator": v["indicator"], "vote": v["vote"],
                   "strength": v["strength"]} for v in votes],
    }


def bars_from_json(bars):
    """黄金向量里内嵌的 bars → _read_bars 的元组形态 (d, o, c, h, l, v)。"""
    return [(b["d"], b.get("o"), b["c"], b["h"], b["l"], b["v"]) for b in bars]


def snapshot_stats(bulk_dir):
    """统计分片快照的覆盖面与新鲜度：唯一代码数、最新一根 K 线的日期。

    生成黄金向量前用它挡住「拿更小 / 更旧的快照静默覆盖现有向量」这种回归。
    2026-10-29 实测：云端 kline 仓 `487cfd0` 的 `data/bulk` 只有 1677 只（多数止于 10-13），
    本地 59 个分片是 5365 只（止于 10-23）；用它重生成会把窗口和覆盖面一起做小。
    """
    codes = set()
    max_date = ""
    for name in sorted(f for f in os.listdir(bulk_dir)
                       if f.startswith("day_qfq_") and f.endswith(".csv")):
        with open(os.path.join(bulk_dir, name), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                codes.add(parts[0])
                if parts[1] > max_date:
                    max_date = parts[1]
    return {"codes": len(codes), "max_date": max_date}

def build_golden(bulk_dir, samples=6, bars=130):
    """按代码排序固定抽样，用 engine_v5 生成黄金向量。"""
    ev5 = _reference()
    # 先取足够多的候选代码，再按序抽前 samples 只（保证可复现）
    codes = []
    files = sorted(f for f in os.listdir(bulk_dir) if f.startswith("day_qfq_") and f.endswith(".csv"))
    for name in files:
        with open(os.path.join(bulk_dir, name), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                code = line.split("|", 1)[0]
                if code and code not in codes:
                    codes.append(code)
        if len(codes) >= samples * 4:
            break
    picked = codes[: max(samples * 2, samples)]
    data = _read_bars(bulk_dir, picked, bars)
    cases = []
    for code in sorted(data)[:samples]:
        rows = data[code]
        expect = expect_from_bars(rows)
        if not expect:
            continue
        cases.append({
            "code": code,
            "bars": [{"d": r[0], "o": r[1], "c": r[2], "h": r[3], "l": r[4], "v": r[5]}
                     for r in rows],
            "expect": expect,
        })
    return {
        "spec_version": SPEC_VERSION,
        "spec_id": SPEC_ID,
        "engine": "v5",
        "source": "engine_v5.build_votes / score_stock",
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "bars_per_case": bars,
        "snapshot": snapshot_stats(bulk_dir),
        "note": "端上 CanonicalEngine 必须逐票复现 vote 与 strength(±0.001)，score 误差 <= 0.05",
        "cases": cases,
    }


def _default_bulk():
    for cand in (os.path.join(ROOT, "..", "stocks-data-kline", "data", "bulk"),
                 os.path.join(ROOT, "data", "kline", "bulk")):
        cand = os.path.normpath(cand)
        if os.path.isdir(cand):
            return cand
    return ""


def verify(golden_path, bulk_dir, samples, bars):
    """重算黄金向量并与落盘文件比对，检测「参考实现改了但 golden 没更新」的漂移。

    优先用 golden 里**内嵌**的 bars 重算：黄金向量自带 130 根 K 线，校验它就不该再依赖
    某个时刻的 K 线分片快照。以前从分片重算，导致 CI 的分片与生成时的分片只要不一致
    就报「漂移 72 处」——那是数据快照差异，不是参考实现漂移，属于假红。
    老黄金向量缺 bars 时才回落到 bulk_dir。
    """
    old = _load(golden_path)
    if old.get("cases") and all(c.get("bars") for c in old["cases"]):
        fresh_cases = []
        for c in old["cases"]:
            expect = expect_from_bars(bars_from_json(c["bars"]))
            if expect:
                fresh_cases.append({"code": c["code"], "bars": c["bars"], "expect": expect})
        fresh = {"spec_version": SPEC_VERSION, "cases": fresh_cases}
    else:
        if not bulk_dir or not os.path.isdir(bulk_dir):
            return False, "老黄金向量缺内嵌 bars，且未提供 K 线分片（--bulk）"
        fresh = build_golden(bulk_dir, samples=len(old["cases"]) or samples, bars=bars)
    if fresh["spec_version"] != old.get("spec_version"):
        return False, "spec_version 不一致: %s vs %s" % (fresh["spec_version"], old.get("spec_version"))
    diffs = []
    for old_case, new_case in zip(old.get("cases", []), fresh["cases"]):
        if old_case["code"] != new_case["code"]:
            diffs.append("样本顺序变化 %s/%s" % (old_case["code"], new_case["code"]))
            continue
        if abs(old_case["expect"]["score"] - new_case["expect"]["score"]) > 1e-9:
            diffs.append("%s score %.4f→%.4f" % (old_case["code"],
                                                 old_case["expect"]["score"],
                                                 new_case["expect"]["score"]))
        for ov, nv in zip(old_case["expect"]["votes"], new_case["expect"]["votes"]):
            if ov["indicator"] != nv["indicator"] or ov["vote"] != nv["vote"] \
                    or abs(ov["strength"] - nv["strength"]) > 1e-9:
                diffs.append("%s %s %s/%s → %s/%s" % (
                    old_case["code"], ov["indicator"], ov["vote"], ov["strength"],
                    nv["vote"], nv["strength"]))
    if diffs:
        return False, "漂移 %d 处: %s" % (len(diffs), "; ".join(diffs[:5]))
    return True, "黄金向量与参考实现一致（%d 例）" % len(old.get("cases", []))


def check_products(root):
    """校验落盘产物是否满足注册表契约。缺文件=WARN，缺字段/多写者=FAIL。"""
    fails, warns = [], []
    for p in PRODUCTS:
        # artifact=False：这类「产物」是两端各自在内存里算的结果（vol_target / regime），
        # 没有落盘文件可比，契约由黄金向量执法，不该在这里报缺失。
        if p.get("artifact") is False:
            continue
        path = os.path.join(root, p["path"])
        if not os.path.exists(path):
            warns.append("%s 缺失（writer=%s）" % (p["id"], p["writer"]))
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception as e:
            fails.append("%s 不是合法 JSON: %s" % (p["id"], e))
            continue
        if isinstance(doc, list):
            if not p["top_fields"] and not doc:
                warns.append("%s 为空数组" % p["id"])
            continue
        optional = set(p.get("optional_fields") or [])
        missing = [k for k in p["top_fields"] if k not in doc and k not in optional]
        soft = [k for k in optional if k not in doc]
        if soft:
            warns.append("%s 缺可选字段 %s（老产物，下次生成即补齐）" % (p["id"], soft))
        if missing:
            fails.append("%s 缺顶层字段 %s（可能被别的脚本覆盖，见 %s）"
                         % (p["id"], missing, p["writer"]))
    # 单写者校验：同一 path 不应被多个脚本写
    by_path = {}
    for p in PRODUCTS:
        by_path.setdefault(p["path"], []).append(p["writer"])
    for path, writers in by_path.items():
        if len(set(writers)) > 1:
            fails.append("%s 有多个写者: %s" % (path, writers))
    return fails, warns


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="双端算法规格与黄金向量")
    ap.add_argument("--out", help="写 engine_spec.json 的路径（写端上那份请走 scripts/sync_parity_artifacts.py publish）")
    ap.add_argument("--golden", help="写黄金向量的路径")
    ap.add_argument("--verify", help="比对已有黄金向量是否漂移")
    ap.add_argument("--bulk", default="", help="K 线分片目录（默认自动找 stocks-data-kline/data/bulk）")
    ap.add_argument("--check-products", action="store_true",
                    help="按注册表校验 data/*.json 产物是否满足契约")
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--bars", type=int, default=130)
    ap.add_argument("--force", action="store_true",
                    help="允许用覆盖面更小 / 更旧的快照覆盖已有黄金向量（会写进 snapshot 字段留痕）")
    args = ap.parse_args()

    if args.check_products:
        fails, warns = check_products(ROOT)
        for w in warns:
            print("WARN  " + w)
        for f in fails:
            print("FAIL  " + f)
        if fails:
            return 1
        print("OK  %d 个产物符合注册表契约（%d 警告）" % (len(PRODUCTS) - len(warns), len(warns)))
        if not args.out and not args.golden and not args.verify:
            return 0

    wrote = False
    if args.out:
        spec = build_spec()
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("spec %s -> %s (engines=%d, device=%d, pipeline=%d, weights=%s)" % (
            spec["spec_version"], args.out, len(spec["engines"]),
            len(spec["device_engines"]), len(spec["pipeline"]), spec["weights_source"]))
        wrote = True

    if args.golden or args.verify:
        # verify 需要分片**只**在老黄金向量缺内嵌 bars 时；生成 golden 必须要有分片。
        if args.verify:
            ok, msg = verify(args.verify, args.bulk or _default_bulk(), args.samples, args.bars)
            print(("OK  " if ok else "DRIFT  ") + msg)
            return 0 if ok else 1
        bulk = args.bulk or _default_bulk()
        if not bulk or not os.path.isdir(bulk):
            print("K 线分片目录不存在: %r（用 --bulk 指定）" % bulk, file=sys.stderr)
            return 2
        golden = build_golden(bulk, samples=args.samples, bars=args.bars)
        if os.path.exists(args.golden) and not args.force:
            old = _load(args.golden)
            snap = old.get("snapshot") or {}
            old_max = snap.get("max_date") or max(
                (c["bars"][-1]["d"] for c in old.get("cases", []) if c.get("bars")), default="")
            old_codes = snap.get("codes") or 0
            new = golden.get("snapshot") or {}
            worse = []
            # 绝对下限：云端产物覆盖 5308 只，分片快照少于 3000 只说明拿到的是
            # 残缺分片（2026-10-29 实测云端 kline 仓 `data/bulk` 只有 1677 只，
            # 用它的「最新日期更新」骗过纯日期比较，覆盖却只有 1/3）。
            if new.get("codes", 0) < 3000:
                worse.append("覆盖股票只有 %d 只（全市场约 5308 只，分片残缺）" % new.get("codes", 0))
            if old_max and new.get("max_date", "") < old_max:
                worse.append("最新 K 线 %s → %s（更旧）" % (old_max, new.get("max_date")))
            if old_codes and new.get("codes", 0) < old_codes:
                worse.append("覆盖股票 %d → %d（更少）" % (old_codes, new.get("codes")))
            if worse:
                print("拒绝覆盖 %s：新快照不如现有黄金向量 → %s" % (args.golden, "；".join(worse)),
                      file=sys.stderr)
                print("确认要用更差的快照重生成，加 --force", file=sys.stderr)
                return 3
        os.makedirs(os.path.dirname(os.path.abspath(args.golden)) or ".", exist_ok=True)
        with open(args.golden, "w", encoding="utf-8") as fh:
            json.dump(golden, fh, ensure_ascii=False)
            fh.write("\n")
        snap = golden.get("snapshot") or {}
        print("golden -> %s (cases=%d, bars=%d, snapshot=%d codes @ %s)"
              % (args.golden, len(golden["cases"]), args.bars, snap.get("codes", 0), snap.get("max_date", "")))
        wrote = True

    if not wrote:
        spec = build_spec()
        print(json.dumps({"spec_version": spec["spec_version"],
                          "engines": len(spec["engines"]),
                          "device_engines": len(spec["device_engines"]),
                          "cloud_only": spec["cloud_only_engines"],
                          "pipeline_stages": [s["id"] for s in spec["pipeline"]]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
