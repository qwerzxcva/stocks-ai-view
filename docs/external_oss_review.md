# 外部开源项目评估（External OSS Review）

> 生成日期：2026-10-23 ｜ 状态：**Phase 2 新增文档**
>
> 说明：评估 7 个外部候选项目对本系统（双层模型策略，见 closed_loop_architecture.md §10）的可借鉴价值。
> 统一纪律：**一行不抄——只借鉴数学模型/架构/思想，全部独立实现，代码中注明来源 + 许可证**；无 LICENSE 的项目一律视为「保留所有权利」，代码不可参考、仅可验证公开描述的行为。

---

## 1. 评估总表

| 项目 | 仓库 | 许可证 | 集成定位 | 结论 |
|---|---|---|---|---|
| FinGPT | github.com/AI4Finance-Foundation/FinGPT | MIT | 主链路**重推理层** | **推荐**：借鉴推理管线 + 自训 LoRA，独立实现 |
| qlib | github.com/microsoft/qlib | MIT | 量化 **baseline** | **推荐**：model zoo 作为量化对照基线 |
| TradingAgents | github.com/TauricResearch/TradingAgents | Apache-2.0 | 主链路重推理层（仅思想） | 仅借鉴多智能体**辩论结构** |
| FinRobot | github.com/AI4Finance-Foundation/FinRobot | Apache-2.0 | 主链路重推理层（仅思想） | 依赖付费 API；仅借鉴**分层哲学** |
| AI-Trader | github.com/HenryXiaoYang/AI-Trader | **无 LICENSE** | **排除** | 无许可证 = 保留所有权利，排除 |
| jadesun/ai_stock | github.com/jadesun/ai_stock | Apache-2.0 | 辅助**轻推理层**（舆情） | 借鉴契约设计；仓库代码不完整 |
| heyfluke/stock-scanner | github.com/heyfluke/stock-scanner | **无 LICENSE**（上游 DR-lin-eng 为 MIT） | 中枢路由（仅思想） | 借鉴 agent_orchestrator preset/graph 模式；一行不抄 |

---

## 2. 逐项评估

### 2.1 FinGPT（MIT）— 推荐，主链路重推理层

- **可借鉴点**：金融 LLM 推理管线（数据 → 微调 → 推理分层）、LoRA 低成本微调方法、RLHF 思路。
- **集成定位**：主链路重推理模型（选股/分析/推股/复盘/总结）的方法论来源；自训 LoRA，独立实现。
- **独立实现声明模板**：
  > 本模块推理管线参考 FinGPT（https://github.com/AI4Finance-Foundation/FinGPT，MIT License）之架构思想独立实现，未复制其代码；模型为自训 LoRA。

### 2.2 qlib（MIT）— 推荐，量化 baseline

- **可借鉴点**：model zoo 的量化模型对照基线（LSTM/Transformer 等预测模型）、workflow 与数据handler 设计。
- **集成定位**：量化 baseline，用于 Phase 3 回测中作为对照组（验证自研引擎超额）。
- **独立实现声明模板**：
  > 本 baseline 对照参考 qlib model zoo（https://github.com/microsoft/qlib，MIT License）之模型定义思想独立实现，仅用于回测对照，未复制其代码。

### 2.3 TradingAgents（Apache-2.0）— 仅借鉴辩论结构

- **可借鉴点**：多智能体辩论（多头/空头/风控角色对抗）的决策结构。
- **集成定位**：主链路重推理层；辩论结构启发 `llm_pick_arbiter` 的多角色评审流程。
- **独立实现声明模板**：
  > 多角色辩论结构参考 TradingAgents（https://github.com/TauricResearch/TradingAgents，Apache-2.0 License）之思想独立实现，未复制其代码。

### 2.4 FinRobot（Apache-2.0）— 仅借鉴分层哲学

- **可借鉴点**：金融 AI Agent 的分层架构哲学（任务分解/工具调用层）。
- **限制**：依赖付费 API，不可直接落地。
- **集成定位**：主链路重推理层（仅架构思想）。
- **独立实现声明模板**：
  > 分层架构思想参考 FinRobot（https://github.com/AI4Finance-Foundation/FinRobot，Apache-2.0 License）独立实现；因依赖付费 API 未采用其运行时，未复制其代码。

### 2.5 AI-Trader（无 LICENSE）— 排除

- **结论**：无 LICENSE = 保留所有权利，**整项目排除**，不借鉴、不参考、不引用。

### 2.6 jadesun/ai_stock（Apache-2.0）— 辅助舆情层

- **可借鉴点**：**新闻 → LLM → {推荐, 置信度, 风险}** 的输出契约设计。
- **限制**：仓库代码不完整，仅能借鉴契约与流程描述。
- **集成定位**：辅助轻推理层（舆情分析模块），契约并入 H 组之外的舆情因子。
- **独立实现声明模板**：
  > 输出契约（推荐/置信度/风险三元组）参考 ai_stock（https://github.com/jadesun/ai_stock，Apache-2.0 License）之公开描述独立实现；其仓库代码不完整，未复制任何代码。

### 2.7 heyfluke/stock-scanner（无 LICENSE，上游 MIT）— 中枢路由启发，一行不抄

- **可借鉴点**：规则打分 + LLM 多角色编排；`agent_orchestrator` 的 **preset/graph 两种编排模式**启发 `central_orchestrator` 的路由设计。
- **许可证风险**：本仓库无 LICENSE = 保留所有权利；上游 DR-lin-eng 原仓库为 MIT。**纪律：一行不抄**，仅借鉴公开描述的 preset/graph 编排思想。
- **集成定位**：中枢层路由（central_orchestrator）设计参考。
- **独立实现声明模板**：
  > preset/graph 编排思想参考 stock-scanner（https://github.com/heyfluke/stock-scanner，仓库无 LICENSE，上游 DR-lin-eng 为 MIT）之公开 README 描述独立实现，未阅读/复制其代码实现。

---

## 3. 纪律汇总

1. **一行不抄**：所有项目仅借鉴思想/契约/架构，代码全部独立实现。
2. **许可证优先级**：MIT / Apache-2.0 可借鉴思想并注明来源；无 LICENSE 一律视为保留所有权利，排除或仅限公开描述的思想参考。
3. **独立实现声明**：凡借鉴处，在对应模块 docstring 顶部附上 §2 中声明模板并标注来源 + 许可证。
4. **落点对照**：重推理层 = 主链路（FinGPT/TradingAgents/FinRobot 思想）；轻推理层 = 辅助模块（ai_stock 舆情契约）；中枢路由 = stock-scanner 编排思想；量化 baseline = qlib。
