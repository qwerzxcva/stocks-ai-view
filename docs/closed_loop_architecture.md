# 闭环架构设计（Closed-Loop Architecture）

> 生成日期：2026-10-23 ｜ 状态：**Phase 2 新增文档** ｜ 裁决：主代理已定稿的设计决策直接采用，未决项见 §8。
>
> 修订：2026-10-23 增量（§0.1 重盘差异、§8 anai.pro 定稿、§10 双层模型策略）。

---

## 0. 现状摘要（已定稿事实）

| 维度 | 现状 |
|---|---|
| 脚本布局 | 仓库 **223 个脚本**（重盘 2026-10-23，原 155）；`scripts/engines/` **已建**（Phase 2 产物）；**仍无 `scripts/backtest/` 目录**，回测实为 `scripts/walkforward.py` + `scripts/kline_sim.py` + `scripts/factor_lab.py` |
| 核心引擎 | `scripts/engine_v5.py`：21 票（17 技术 + 3 外部 + 回测信号）；`score = 50 + 50 × 加权票 × (0.6 + 0.4 × 共振率)`（engine_v5.py:380）；主板过滤 `MAIN_BOARD_PREFIX` 在 engine_v5.py:574 / :577 / :711，**只裁输出层**，不改打分 |
| regime 现状 | 双轨并存：`scripts/decision_engine.py`（bull/bear/range）+ `scripts/market_phase.py`（六阶段）。**裁决：Phase 2 新增 regime_router 为唯一判定入口，两者保留为投票源，不删旧代码** |
| 推股记录 | `data/predictions/engine_v5_*.json`（append-only）+ `scripts/pick_tracker.py`（DuckDB `pick_log`） |
| LLM 现状 | 全部用于复盘：`scripts/llm_review.py`（gpt-4o-mini / llama-3.1-8b）、`scripts/gitcode_ai_review.py`（DeepSeek-V4-Flash / Qwen3-30B）；**尚未进入预测链路** |
| 数据底座 | 全 A 日线 5343 只 / 约 1366 万行 / 至 2026-10-16；指数至 2026-10-09（**滞后 10 个交易日，已知限制**）；财务缺口**已补**（`data/pead_events.json`，3996 标的 / 2915 事件，commit 8b7c833）；剩余主缺口 = 分钟线、筹码、资金流历史、两融、北向 |
| 远端待拉取 | GitHub 存在未同步仓库：`db-min-0`/`db-min-1`（5 分钟线）、`db-fin-0`（财务）、`db-cnflow-0`（北向 + 两融），拉取**待用户提供凭据** |
| 基线胜率 | （待 Phase 3 复现核实）1 日 45.5%（n=64122）/ 3 日 45.2% / 5 日 44.4% / 10 日 43.8%；牛市 45.7%，熊市 54.4% |

### 0.1 仓库重盘差异（2026-10-23）

- `scripts/` 155 → **223** 个脚本；`scripts/engines/` 已建（Phase 2 产物）；仍无 `scripts/backtest/`。
- `engine_v5.py` 仍 **743 行 21 票未动**（最后提交 559077a，10-21）。
- 分钟线仍空壳：`data/bulk/m60_*` 仅注释行；资金流历史有 `scripts/backfill_flow_history.py` 但**未落地**；筹码/两融/北向仍缺。
- **【重要风险】`stocks-data-kline` git 仓损坏**：HEAD 指向不存在的 commit 06675d8，无法 log/diff/commit（数据文件本身完好）。数据滞后：个股日线止 2026-10-16、指数 2026-10-09、news 2026-10-12。
- 涨停 ML 已入 `ban_prob`（commit 81bf465）；玄学全维控制已落地（ee0ae56，negative control 禁入权重）。
- predictions 累计 **656 个文件**至 2026-10-22；`picks.duckdb` mtime 10-14（结算库两周未写，需关注）。

---

## 1. 六层架构总表

| 层 | 职责 | 现有资产（真实脚本） | Phase 2 新增 |
|---|---|---|---|
| **数据层** | 行情/指数/外部数据采集与存储 | 日线库（全 A 5343 只）、指数库、各采集脚本、`data/` 存储 | （暂缺）分钟线/财务/筹码/资金流 → **先降级**（§6），远端仓库拉取待凭据 |
| **因子层** | 技术指标与因子计算 | `scripts/factor_lab.py`、engine_v5 内嵌 17 技术因子 | 降级筹码位因子、降级资金位因子（成交量加权衰减、放量 VWAP 链） |
| **引擎层** | 专项分析引擎 | `scripts/engine_v5.py`、`scripts/decision_engine.py`、`scripts/market_phase.py`、`scripts/walkforward.py`、`scripts/kline_sim.py` | `scripts/engines/` 下 8 引擎（见 §1.1） |
| **中枢层** | regime 判定、路由、集成评分、复盘反馈 | （空缺，目前各引擎各自为政） | `central_orchestrator`（四职责见 §2） |
| **输出层** | 推股、前端展示、记录落盘 | `data/predictions/engine_v5_*.json`（append-only）、`scripts/pick_tracker.py`、前端 `index.html`/`picks.js` | 明日候选输出、早盘推股输出（各自独立落盘） |
| **反馈层** | 结算、复盘、权重更新 | `scripts/pick_tracker.py`（DuckDB pick_log）、`scripts/llm_review.py`、`scripts/gitcode_ai_review.py` | `feedback_updater`（权重指数更新，见 §2.4）、`llm_pick_arbiter`（终审调分） |

### 1.1 Phase 2 新增引擎清单（共 9 个，`scripts/engines/`）

裁决原文为「8 引擎」，实际清单含中枢与反馈层共 **9 个**，如实列出：

| # | 引擎 | 层 | 职责 |
|---|---|---|---|
| 1 | `regime_router` | 引擎层 | 唯一 regime 判定入口；decision_engine（bull/bear/range）与 market_phase（六阶段）保留为投票源，不删旧代码 |
| 2 | `cycle_grid_tester` | 引擎层 | 大盘周期/网格假设的可回测化检验（§5） |
| 3 | `market_anomaly_engine` | 引擎层 | 大盘异动/热点识别 |
| 4 | `price_level_engine` | 引擎层 | 个股 10 类价位计算与命中率检验（§4） |
| 5 | `tomorrow_candidate_engine` | 引擎层 | 盘后「明日候选」推股（与早盘引擎**物理隔离、互不 import**） |
| 6 | `morning_pick_engine` | 引擎层 | 早盘推股（同上，物理隔离） |
| 7 | `central_orchestrator` | **中枢层** | regime 判定收口、路由、集成评分、复盘反馈调度（§2） |
| 8 | `llm_pick_arbiter` | 中枢层 | LLM 终审调分，**调分封顶 ±10** |
| 9 | `feedback_updater` | **反馈层** | 结算驱动的权重更新回路（§2.4） |

> 说明：1–6 为狭义「引擎」，7–9 属中枢与反馈层；文档统称 9 个新增组件。

---

## 2. 中枢四职责（central_orchestrator）

### 2.1 regime 判定
- `regime_router` 为唯一对外入口；内部聚合 decision_engine（bull/bear/range）与 market_phase（六阶段）两个投票源。
- 输出 regime 标签 + 置信度，供路由与 `regime_gate` 使用。

### 2.2 路由
- 按模块声明的依赖关系编排：**可并行的模块并行**（如无数据依赖的个股多维打分），**有依赖的串联**（如 regime → 异动 → 个股打分），按模块权重分配算力/调用顺序。

### 2.3 集成评分（三轮）

| 轮次 | 层级 | 方法 |
|---|---|---|
| 第一轮 | 引擎级 | softmax 胜率 ensembling，参与引擎数 **K ≤ 5** |
| 第二轮 | 维度级 | 各维度加权，对口模块权重 **0.5** |
| 第三轮 | 终审 | `S = regime_gate × Σ v_j · D_j`；`regime_gate` 初始值 1.0 / 0.8 / 0.5（牛/震荡/熊），**终值交 Phase 3 回测网格搜索确定**；`llm_pick_arbiter` 在 S 基础上调分**封顶 ±10** |

### 2.4 复盘反馈（权重更新）

```
w ← w · exp(β · (r − r̄))      β = 0.1
```

- 更新后做 **L1 归一**；单权重**截断 [0.05, 0.4]**，防单引擎独大或被清零。
- **收敛判据**：连续 10 轮 `|Δw| < 1e-3`。
- **防过拟合纪律**：仅在 walkforward **训练窗内**更新权重，测试窗冻结。

---

## 3. 全链路闭环数据流（文字版）

```
数据层（日线/指数/外部数据，降级因子补足缺口）
  → 因子层（factor_lab 技术因子 + 降级筹码/资金因子）
  → regime_router（唯一判定入口；decision_engine + market_phase 投票）
  → market_anomaly_engine（异动/热点）
  → 个股多维打分（engine_v5 21 票 + price_level_engine 价位）
  → central_orchestrator 三轮集成评分（K≤5 softmax → 维度 0.5 → regime_gate×Σv_j·D_j，LLM 终审 ±10）
  → 推股输出（tomorrow_candidate_engine 盘后 / morning_pick_engine 早盘，物理隔离）
  → data/predictions/*.json 记录（append-only，只增，带时间戳）
  → pick_tracker.py 结算（DuckDB pick_log，T 收盘 → T+N 收盘）
  → feedback_updater（w←w·exp(β(r−r̄))，训练窗内更新）
  → 下一轮预测（闭环）
```

**记录纪律**：predictions 一律 append-only、带时间戳，禁止覆盖历史记录，保证任何一次推股可回溯到当时的输入与权重快照。

---

## 4. 价位引擎（price_level_engine）：10 类价位算法

| # | 算法 | 说明 | 降级标记 |
|---|---|---|---|
| 1 | 分形聚类 | 局部高低点分形聚类成支撑/压力带 | |
| 2 | POC（成交量控制点） | 成交量分布峰值价位 | |
| 3 | 黄金分割 | 波段高低点斐波那契回撤/扩展位 | |
| 4 | Pivot Point | 经典枢轴点（P/S1/R1/S2/R2） | |
| 5 | 整数网格 | 整数关口/百元网格心理位 | |
| 6 | ATR 抄底位 | 基于 ATR 的波动幅度下探位 | |
| 7 | 降级筹码位 | **成交量加权衰减模型**（筹码数据缺失的降级） | ★ 降级 |
| 8 | 降级资金位 | **放量 VWAP 链**（资金流历史缺失的降级） | ★ 降级 |
| 9 | 均线簇位 | 关键均线汇聚区（由 1–8 结果交叉验证） | |
| 10 | 前高前低枢轴 | 历史显著高低点 | |

**检验方式**：触位后 T+N 命中率 vs 随机基准，**二项检验 p < 0.05** 方视为有效价位；检验在 walkforward 框架内分窗进行，杜绝全样本拟合。

---

## 5. 用户大盘假设的可回测化（cycle_grid_tester）

- 将「100 点网格顶谷命中」假设形式化为可检验命题：**二项检验 + Monte Carlo 1000 次随机对照**，检验网格命中率是否显著超越随机。
- 已知事实（仅作条件，不做预判）：
  - 指数最新 **3765.71 @ 2026-10-09**；
  - 近 250 日低点 **3741.11**；
  - **3500 在样本窗口内未出现**。
- 口径：**只做条件概率检验**（"若触及某网格，则 T+N 内到下一网格的概率"），**不预判点位方向**。

---

## 6. 推股三时段约定与数据缺口降级

### 6.1 三时段

| 时段 | 引擎 | 说明 |
|---|---|---|
| 盘后 | `tomorrow_candidate_engine` | 明日候选，盘后运行 |
| 早盘 | `morning_pick_engine` | 早盘推股；早盘阶段先用**日线级 + 竞价快照预留适配层** |
| 收盘 | 现有 `llm_review.py` / `gitcode_ai_review.py` | 收盘复盘，独立运行 |

两推股引擎**物理隔离、互不 import**，可独立演进与回测。

### 6.2 数据缺口 Phase 2 降级方案（裁决已定）

| 缺口 | 降级方案 | 正式方案（待凭据） |
|---|---|---|
| 筹码分布 | 成交量加权衰减模型估算筹码位 | 真实筹码数据 |
| 资金流历史 | 放量 VWAP 链估算资金位 | `db-cnflow-0`（北向+两融） |
| 分钟线（早盘） | 日线级打分 + 竞价快照预留适配层 | `db-min-0`/`db-min-1`（5 分钟线） |
| 全市场财务 | **已补（PEAD 3996 标的 / 2915 事件，commit 8b7c833）**，仍不进入打分核心，待回测验证后再引入 | `db-fin-0`（更全财务） |

---

## 7. 回测口径纪律（红线）

1. **只用 T 日及之前数据**做 T 日预测，严禁未来函数；
2. 结算口径：**T 收盘 → T+N 收盘**；
3. 每次报告必备四项：**基准胜率、超额 pp、Wilson 置信区间、分年度对比**；
4. 权重更新仅限 walkforward 训练窗（§2.4）；
5. H 组另类因子（详见 module_engine_mapping.md）：**权重硬上限 0.02，禁止单独触发推股**。

---

## 8. 待用户裁决项清单

| # | 事项 | 建议/状态 |
|---|---|---|
| 1 | anai.pro 接入 | **已定稿：强制接入（2026-10-23）**。双模式：凭据模式（用户自带 cookie，从环境变量 `ANAI_COOKIE` 读）+ 匿名模式（分享页 + 搜索接口，默认）；adapter = `scripts/engines/anai_adapter.py`。红线：不破解签名/验证码；robots 风险如实标注（该站 `ai-train=no, use=reference`） |
| 2 | 远端数据仓库（db-min-0/1、db-fin-0、db-cnflow-0）拉取 | **待用户提供凭据** |
| 3 | 数据更新 SLA（日线/指数更新时点，指数滞后 10 交易日问题） | 待用户确认可接受的更新频率 |
| 4 | `regime_gate` 终值（初始 1.0/0.8/0.5） | 交 Phase 3 回测网格搜索，用户确认搜索结果后定稿 |
| 5 | 私有仓库核查 | 待用户提供访问方式 |

---

## 9. LLM 股票 AI 选型路线（裁决已定）

| 路线 | 项目 | 许可证 | 用法 |
|---|---|---|---|
| 主线 | **FinGPT** | MIT | 借鉴管线 + 自训 LoRA |
| 量化 baseline | **qlib model zoo** | MIT | 作为量化对照基线 |

纪律：**只借鉴数学模型/架构，全部独立实现，代码中注明来源 + 许可证**；不直接复制代码。详细评估见 `docs/external_oss_review.md`。

---

## 10. 双层模型策略（用户 2026-10-23 拍板）

| 链路 | 模型档位 | 覆盖模块 |
|---|---|---|
| **主链路**（选股/分析/推股/复盘/总结） | **重推理分析模型** | B 组推股、C 组复盘/终审、A 组大盘预测 |
| **辅助模块**（异动/热点/另类因子等） | **轻推理分析模型** | E 组异动热点、F 组资金行为、H 组另类因子 |

- **开源路线「两者并行」**：FinGPT（MIT，借鉴推理管线 + 自训 LoRA，独立实现）+ qlib model zoo（MIT，量化 baseline）。
- **落点**：`scripts/engines/llm_pick_arbiter.py`，参数 `--mode heavy|light|auto`；调分 adjust **±10 封顶**；**裁决权在中枢（central_orchestrator）**，LLM 只供调分建议。
