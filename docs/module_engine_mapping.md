# 模块-引擎映射矩阵（35 组）

> 生成日期：2026-10-23 ｜ 状态：**Phase 2 新增文档**
>
> 说明：
> - **★ = 对口引擎（权重最高，0.5）**；其余为辅助引擎。
> - 推股类模块（B、C 组）：**决策权 = 中枢（central_orchestrator）+ LLM（llm_pick_arbiter），引擎只供数**。
> - H 组：**另类情绪因子、非实证、权重硬上限 ≤ 0.02、禁止单独触发推股**。
> - 「新增」指 Phase 2 新增 `scripts/engines/` 下引擎；其余为现有脚本。
>
> 修订：2026-10-23 增量（B1/E 组财务缺口标注更正为「已补 PEAD」、G1 双层模型策略落点、附表补充）。

---

## A 组：大盘预测与周期（8 项）

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| A1 | 当日大盘预测 | ★ `regime_router`（新增） | `decision_engine.py`、`market_phase.py`（投票源）、`market_anomaly_engine`（新增） | 指数日线（至 2026-10-09） | 指数滞后 10 交易日；分钟线缺失 | 日线级推断，分钟线待 db-min-0/1 |
| A2 | 大盘复盘 | ★ `llm_review.py` / `gitcode_ai_review.py` | `walkforward.py`、`pick_tracker.py` | 指数日线 + predictions 记录 | 无重大缺口 | — |
| A3 | 明日大盘预测 | ★ `regime_router`（新增） | `cycle_grid_tester`（新增）、`market_anomaly_engine`（新增） | 指数日线、外围市场 | 指数滞后；外围数据源有限 | 条件概率输出，不预判方向 |
| A4 | 外围关联 | ★ `market_anomaly_engine`（新增） | `overseas.js`（前端现有外围数据） | 外围指数/商品行情 | 外围历史序列不完整 | 仅用可得外围数据做相关性统计 |
| A5 | 牛熊 regime | ★ `regime_router`（新增，唯一入口） | `decision_engine.py`（bull/bear/range）、`market_phase.py`（六阶段） | 指数日线长序列 | 无（双轨保留为投票源） | — |
| A6 | 周期轮动 | ★ `cycle_grid_tester`（新增） | `market_phase.py`、`factor_lab.py` | 指数 + 板块日线 | 板块分类数据完整性待核 | 用指数层面周期先验 |
| A7 | 历史周期 | ★ `cycle_grid_tester`（新增） | `kline_sim.py`、`walkforward.py` | 指数长历史日线 | 无重大缺口 | — |
| A8 | 周期预测 | ★ `cycle_grid_tester`（新增） | `regime_router`（新增） | 指数日线 | 假设未经检验 | 只做条件概率检验（二项 + Monte Carlo 1000 次），不预判点位 |

---

## B 组：个股推股（6 项）

> **决策权 = 中枢 + LLM，引擎只供数。** 最终推股输出由 `central_orchestrator` 集成评分（三轮）+ `llm_pick_arbiter` 终审（±10 封顶）产生；两推股引擎物理隔离、互不 import。

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| B1 | 个股多维打分 | ★ `engine_v5.py`（21 票） | `price_level_engine`（新增）、`factor_lab.py` | 全 A 日线 5343 只 | 分钟线、筹码、资金流（财务**已补**：PEAD 3996 标的 / 2915 事件） | 降级筹码位/资金位因子 |
| B2 | 价位支撑压力 | ★ `price_level_engine`（新增，10 类价位） | `engine_v5.py`、`factor_lab.py` | 日线 OHLCV | 筹码分布数据 | 成交量加权衰减模型降级筹码位 |
| B3 | 明日候选（盘后） | ★ `tomorrow_candidate_engine`（新增） | `engine_v5.py`、`regime_router`（新增） | 日线 + regime 标签 | 无重大缺口 | — |
| B4 | 早盘推股 | ★ `morning_pick_engine`（新增） | `engine_v5.py`、`regime_router`（新增） | 分钟线 + 竞价数据 | **分钟线缺失** | 日线级 + 竞价快照预留适配层（待 db-min-0/1） |
| B5 | 个股回测验证 | ★ `walkforward.py` | `kline_sim.py`、`factor_lab.py` | 日线历史 | 无重大缺口 | — |
| B6 | 推股记录与结算 | ★ `pick_tracker.py`（DuckDB pick_log） | predictions append-only JSON、`feedback_updater`（新增） | 推股记录 + 日线收盘 | 无重大缺口 | — |

---

## C 组：复盘与反馈（3 项）

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| C1 | 收盘 LLM 复盘 | ★ `llm_review.py` / `gitcode_ai_review.py` | `pick_tracker.py` | 结算结果 + 当日行情 | 无重大缺口 | — |
| C2 | 权重反馈更新 | ★ `feedback_updater`（新增） | `pick_tracker.py`、`walkforward.py` | pick_log 结算序列 | 无重大缺口 | w←w·exp(β(r−r̄))，β=0.1，L1 归一，截断 [0.05,0.4]，训练窗内更新 |
| C3 | LLM 终审调分 | ★ `llm_pick_arbiter`（新增） | `central_orchestrator`（新增） | 集成评分 S + 上下文 | LLM 未入预测链路（现仅复盘） | 调分封顶 ±10，超限截断 |

---

## D 组：中枢与治理（4 项）

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| D1 | regime 判定收口 | ★ `regime_router`（新增，唯一入口） | `decision_engine.py`、`market_phase.py`（保留为投票源） | 指数日线 | 无（旧代码不删） | — |
| D2 | 模块路由编排 | ★ `central_orchestrator`（新增） | 各引擎依赖声明 | 模块依赖图 | 无 | 无依赖并行、有依赖串联 |
| D3 | 集成评分 | ★ `central_orchestrator`（新增） | `llm_pick_arbiter`（新增） | 各引擎输出 | regime_gate 终值未定 | 初始 1.0/0.8/0.5，Phase 3 网格搜索定稿 |
| D4 | 回测口径纪律 | ★ `walkforward.py` | `kline_sim.py`、`pick_tracker.py` | 历史日线 | 无 | T 日前数据预测、T 收盘→T+N 收盘、Wilson CI、分年度对比必备 |

---

## E 组：异动与热点（6 项）

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| E1 | 大盘异动识别 | ★ `market_anomaly_engine`（新增） | `regime_router`（新增） | 指数日线 + 全 A 涨跌分布 | 分钟线缺失 | 日线级异动（涨跌停家数、振幅、量能突变） |
| E2 | 板块热点 | ★ `market_anomaly_engine`（新增） | `factor_lab.py` | 板块日线 | 板块分类完整性待核 | 用现有板块划分 |
| E3 | 个股异动 | ★ `market_anomaly_engine`（新增） | `engine_v5.py` | 全 A 日线 | 分钟线缺失 | 日线级放量/涨跌幅异动 |
| E4 | 连板/龙头跟踪 | ★ `market_anomaly_engine`（新增） | `engine_v5.py` | 日线涨停记录 | 无重大缺口 | — |
| E5 | 外围冲击传导 | ★ `market_anomaly_engine`（新增） | `overseas.js` 数据源 | 外围行情 + A 股日线 | 外围历史不完整 | 仅可得数据的相关性统计 |
| E6 | 异动复盘 | ★ `llm_review.py` | `market_anomaly_engine`（新增）、`pick_tracker.py` | 异动记录 + 结算 | 无重大缺口 | — |

---

## F 组：资金行为识别（9 项）

> 统一数据缺口：**资金流历史、筹码分布、两融、北向缺失**。统一降级：**放量 VWAP 链（资金位）+ 成交量加权衰减模型（筹码位）**；正式数据待 `db-cnflow-0`（北向+两融）等远端仓库凭据拉取。

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| F1 | 主力流入 | ★ `market_anomaly_engine`（新增） | `engine_v5.py`、`factor_lab.py` | 资金流历史 + 日线 | 资金流历史缺失 | 放量 VWAP 链估算资金位 |
| F2 | 主力流出 | ★ `market_anomaly_engine`（新增） | `engine_v5.py`、`factor_lab.py` | 资金流历史 + 日线 | 资金流历史缺失 | 放量 VWAP 链（反向） |
| F3 | 微涨大流入 | ★ `market_anomaly_engine`（新增） | `engine_v5.py` | 资金流 + 日线涨幅 | 资金流历史缺失 | 涨幅小 + 放量 VWAP 上移联合判定 |
| F4 | 大涨大流入 | ★ `market_anomaly_engine`（新增） | `engine_v5.py` | 资金流 + 日线涨幅 | 资金流历史缺失 | 同上（涨幅大分支） |
| F5 | 微跌大流出 | ★ `market_anomaly_engine`（新增） | `engine_v5.py` | 资金流 + 日线跌幅 | 资金流历史缺失 | 跌幅小 + 放量 VWAP 下移联合判定 |
| F6 | 低位吸筹 | ★ `price_level_engine`（新增） | `market_anomaly_engine`（新增） | 筹码分布 + 资金流 | 筹码 + 资金流双缺 | 降级筹码位 + 降级资金位联合判定 |
| F7 | 低位洗盘 | ★ `price_level_engine`（新增） | `market_anomaly_engine`（新增） | 筹码 + 资金流 + 日线 | 同上 | 同上，叠加日线形态过滤 |
| F8 | 高位套人 | ★ `price_level_engine`（新增） | `market_anomaly_engine`（新增） | 筹码 + 资金流 + 日线 | 同上 | 同上（高位分支） |
| F9 | 高位洗盘 | ★ `price_level_engine`（新增） | `market_anomaly_engine`（新增） | 筹码 + 资金流 + 日线 | 同上 | 同上（高位分支） |

---

## G 组：LLM 增强（1 项）

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| G1 | LLM 股票 AI（FinGPT 路线） | ★ `llm_pick_arbiter`（新增） | `llm_review.py`、`gitcode_ai_review.py`（现有复盘管线） | 文本/财报语料 + 行情 | 自训 LoRA 未启动 | **双层模型策略（2026-10-23 拍板）**：主链路（选股/分析/推股/复盘/总结）用重推理模型，辅助模块（异动/热点/另类因子）用轻推理模型；落点 `llm_pick_arbiter.py --mode heavy\|light\|auto`，adjust ±10 封顶，裁决权在中枢。借鉴 FinGPT（MIT）管线 + qlib（MIT）做量化 baseline；只借鉴数学模型/架构，独立实现，注明来源 + 许可证；详见 `docs/external_oss_review.md` |

---

## H 组：另类情绪因子（7 项）

> ⚠️ **整组约束：另类情绪因子、非实证、权重硬上限 ≤ 0.02、禁止单独触发推股。** 仅作为集成评分中的微量修正项进入第三轮 `Σ v_j · D_j`。

| # | 模块 | 对口引擎 | 辅助引擎 | 数据依赖 | 当前缺口 | 降级方案 |
|---|---|---|---|---|---|---|
| H1 | 情绪因子 1 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 非实证，无显著性检验 | 权重 ≤0.02，二项检验 p<0.05 才可保留 |
| H2 | 情绪因子 2 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |
| H3 | 情绪因子 3 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |
| H4 | 情绪因子 4 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |
| H5 | 情绪因子 5 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |
| H6 | 情绪因子 6 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |
| H7 | 情绪因子 7 | ★ `factor_lab.py` | `market_anomaly_engine`（新增） | 日线衍生 | 同上 | 同上 |

---

## 附：引擎归属速查

| 引擎 | 状态 | 主要服务模块组 |
|---|---|---|
| `engine_v5.py` | 现有 | B（打分核心） |
| `decision_engine.py` / `market_phase.py` | 现有（保留为投票源） | A5、D1 |
| `walkforward.py` / `kline_sim.py` / `factor_lab.py` | 现有（回测三件套） | B5、D4、H |
| `pick_tracker.py` | 现有 | B6、C |
| `llm_review.py` / `gitcode_ai_review.py` | 现有（复盘） | A2、C1、E6 |
| `regime_router` | Phase 2 新增 | A、D1 |
| `cycle_grid_tester` | Phase 2 新增 | A6–A8 |
| `market_anomaly_engine` | Phase 2 新增 | E、F、A4 |
| `price_level_engine` | Phase 2 新增 | B2、F6–F9 |
| `tomorrow_candidate_engine` / `morning_pick_engine` | Phase 2 新增（物理隔离） | B3、B4 |
| `central_orchestrator` | Phase 2 新增（中枢） | D2、D3、全局 |
| `llm_pick_arbiter` | Phase 2 新增 | C3、G1 |
| `feedback_updater` | Phase 2 新增（反馈层） | C2 |
| `anai_adapter` | Phase 2 新增（已定稿接入 2026-10-23，双模式 cookie/匿名） | 外部数据辅助（E、A4） |
