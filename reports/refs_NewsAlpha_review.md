# 参考仓库深度评审：yehomak/NewsAlpha

- 来源：https://github.com/yehomak/NewsAlpha （本地：`~/workspace/refs/yehomak__NewsAlpha`）
- 许可证：**无 LICENSE 文件** [事实]（`LICENSE*`/`COPYING*` 均不存在；README/pyproject.toml 无 license 字段）。按纪律 → **只借概念与方法论，独立实现，不复制代码**。
- 更新检查 [事实]：本地 git 元数据已损坏（`fatal: bad object HEAD`，clone 时 pack 落盘失败、HEAD 悬空），`git fetch` 因容器网络不通 github.com:443 失败。**无法确认 10-16 后更新**；但工作树文件完整可读（README 已引用 Sep 8–17 评估结果与 Stage 8C 完成记录，代码与文档一致，可视为较新快照）。如需更新结果需父会话重克隆。

---

## 1. 系统概览 [事实]

端到端"新闻→方向信号→T+5 严格评估"流水线（FastAPI + Postgres/pgvector + LangGraph + Claude Haiku 4.5 + yfinance）：

- **摄取**（`app/ingestion/`）：Alpaca + Yahoo RSS + CNBC + NewsData 四源，30 分钟一轮。
- **三层去重**（`app/ingestion/dedup.py`）：
  1. URL hash（`normalize_url` 去 query/尾斜杠后 sha256，`dedup.py:14-20`）；
  2. 时间域去重（同源同 ticker ≤4h 窗口，重复只 `coverage_count+1`，见 README 流程图）；
  3. **语义去重**：标题 embedding 进 pgvector，cosine 相似度 >0.95 且 24h 窗口内 → 标记 `dedup_skipped=true`、记录 `similar_to_id`，**仍落库但跳过 LLM**（`dedup.py:38-61`, `pipeline.py:50-80`）。
- **LangGraph 链**（`app/pipeline/graph.py`）：`extract_companies → resolve_tickers → generate_signal → handle_error`，条件路由。
  - `extract.py`：Haiku 抽取上市公司名（tool_use 结构化输出）；
  - `resolve.py`：**ticker 双重闸门**——ingestion 的 `ticker_hints` 若在 100 票白名单（`universe.py`）内直接命中（fast path，零成本）；否则 Haiku 提议 ticker → `validate_ticker` 对 universe 校验，幻觉 ticker 被拒绝入 DB；
  - `reason.py:23-118`：Haiku 生成 `SignalOutput{direction, confidence∈[0,1], event_type, reasoning}`（Pydantic 校验 + `tool_choice` 强制结构化），system prompt 带 `cache_control: ephemeral` 缓存降本；每调用（含被拒/截断）都写 `extraction_attempts` 成本台账。
- **评估**（`app/eval/`）：独立定时任务，见 §3。

## 2. 特征工程与 LLM 调用方式 [事实]

- 输入特征极简：headline + body 前 2000 字符（`reason.py:28`）、ticker、event_type 由 LLM 自分类。无手工技术指标特征——**信号的全部"特征工程"就是 prompt + 结构化输出 schema**。
- 方向输出三值：BULLISH/BEARISH/NEUTRAL（`db/models.py Direction`），confidence 0-1。
- 成本控制：Haiku 4.5 + system prompt ephemeral 缓存 + max_tokens 512 + body 截断，README 报告 **$0.0062/signal**；`costs.py` 逐调用计价。

## 3. 严格无前视评估框架（核心借鉴对象）[事实]

1. **信号 T0 锁定，架构级防前视**（README "Signal lifecycle"）：direction/confidence 在 T0 写库后**永不修改**；价格在 5 天后才抓取——前视偏差"架构上不可能"而非靠约定。
2. **T0 锚定**（`app/pipeline/market_hours.py:38-47` `t0_anchor()`）：新闻在盘中（NYSE 9:30-16:00 ET）发布 → T0=发布时间（价格已在动）；盘外发布 → T0=下一开盘。解决了"新闻发布时间 vs 价格反应时点"对齐这一经典前视陷阱。
3. **8 点价格曲线**（`eval/runner.py:19` `_OFFSETS = [-2,-1,0,1,2,3,4,5]`）：T-2/T-1 为**事件前基线**，T+1..T+5 为反应窗；全部在评估时 retroactive 抓取（`pricer.py:_fetch_close`，前向/后向各 7 天窗口吸附最近交易日）。
4. **T+5 到期闸门**（`pricer.py:64-68` `t5_elapsed()`）：`now >= t0 + 5d + 17h` 缓冲才评估；未到期跳过。评估幂等：`EvalResult` 存在即跳过（`runner.py:105-110` 竞态防护）。
5. **收益口径**：`return_pct = (P_T5 − P_T0)/P_T0`；**abnormal return = return − (T-2→T0 预漂移)**（`runner.py:96-100`）——把新闻效应从既有动量中剥离，这是它比"裸收益"复盘高明的一点。
6. **判定口径**（`runner.py:24-29` `_is_correct`）：BULLISH 要求 return>0，BEARISH 要求 <0，NEUTRAL 要求 |return|≤1% 带宽。
7. **统计指标**（`eval/analysis.py`）：
   - Pearson IC：direction 编码 ±1/0 vs return_pct，**n<5 不出数**，Neutral 剔除（`analysis.py:26-49`）；
   - profit_factor = 对时均收益 / 错时均亏损绝对值（`analysis.py:52-71`）；
   - **multi_horizon_accuracy**（`analysis.py:89-105`）：T+1..T+5 各 horizon 分别算准确率，每个 horizon 只统计有快照的样本（样本量逐 horizon 标注 n）；
   - **分段披露**（README 结果表）：按 event_type（product_launch/earnings/macro/general）× direction 分段给 n 与 accuracy——**总量 41.8% 低于随机，但分段后 product_launch 61%、earnings 33.3%（反向即 67% edge）**。诚实报告负结果 + 分段找 edge 的方法论是最大亮点。
8. **样本量纪律**：README 全表每格都带 n；IC 标注 n=432。

## 4. 与我们红线的对照 [事实+推断]

| 防前视措施 | NewsAlpha | 我们（feedback_updater / orchestrator） |
|---|---|---|
| 信号不可变 | DB 落库后永不改 [事实] | predictions 快照按日落盘只读 [事实，等同] |
| T0 锚定 | 盘中/盘外分别锚定发布时间/下一开盘 [事实] | 我们只有日线粒度，--asof/--train-end 截止 + fwd_ret 用"≤date 最近交易日"（`feedback_updater.py:131-149`）[事实]，日线场景等价 |
| 到期才评估 | t5_elapsed + 17h 缓冲 [事实] | settled 语义 = T+1 收益可完整计算才入训练 [事实，等价] |
| 幂等重评防护 | EvalResult 存在即跳过 [事实] | 我们权重版本链只追加 [事实] |
| 事件前基线 | T-2→T0 漂移剥离 abnormal return [事实] | **缺失**：我们的超额只对市场等权基准，未剥离个股事件前动量 [推断：可补] |
| 分段统计 | event_type×direction×horizon 三段 [事实] | 我们只有按引擎分段，无按信号类型/市场状态分段 [推断：可补] |

## 5. 可借鉴点清单（全部概念级独立实现；无 LICENSE → 不复制代码）

1. **[事实] 信号锁定 + 延迟取价的前向管道**：news_hotspot_engine 的输出应按 `asof` 落盘为不可变信号记录（code/direction/confidence/event_type/reasoning），T+N 复盘由独立任务在到期后回填——与我们 predictions 快照 + feedback_updater 的既有模式天然兼容。
2. **[事实] abnormal return 口径（return − T-2→T0 预漂移）**：补进 feedback_updater 的 evidence，剥离事件前动量，避免"强者恒强"被误判为信号有效。分片日线自算即可，~15 行。
3. **[事实] 分段披露纪律**：按 event_type（业绩/公告/行业政策/宏观）× direction × horizon(T+1/T+5) 出带 n 的准确率表；n<30 的分段标注不置信。这直接回应了我们红线的"样本量/baseline 必须披露"。
4. **[事实] Neutral 带宽判定（|r|≤1% 算对）**：A 股消息引擎若输出"中性/观望"类信号，需要显式带宽而非简单符号判定。
5. **[事实] 三层去重（URL hash / 时间域 / 语义 0.95）**：我们 D 组消息引擎采集层（cls_collect/collect_hotnews 已有多源）可借鉴前两层的低成本组合——同源同票 ≤4h 只加 coverage_count 不重复打分；语义层可用轻量 embedding 或标题 simhash 替代 pgvector。
6. **[事实] universe 闸门 + ticker 双通道解析**：LLM 提议必须过白名单校验，幻觉标的拒绝入库；ingestion hint fast-path 省 LLM 调用。对应我们：LLM 裁决器输出的 code 必须在校验证券代码表内（我们已有股票清单数据）。
7. **[事实] 成本台账（extraction_attempts 含被拒/截断）**：我们 llm_pick_arbiter 的双层模型策略可借鉴——每次调用（含失败/降级）记录 model/tokens/cost，支出不可见=失控。
8. **[事实] multi_horizon_accuracy（T+1..T+5 分 horizon 准确率）**：我们复盘已有 T+1/5/10 收益但按 horizon 的"方向准确率曲线"未输出；一条曲线即可看信号衰减速度，判断最佳持有窗。

## 6. 落地建议（D 组 news_hotspot_engine 方法论）

1. **信号契约**：`{code, direction∈{bull,bear,neutral}, confidence, event_type, asof, reasoning_digest}` 落盘 `data/news_signals/{date}.json`，只追加不改写；D 组接入 `central_orchestrator` 的 `GROUP_ENGINES["D"]`（当前为空预留位）后以 dim_signal 形式进终审。
2. **复盘闭环**：复用 feedback_updater 的 fwd_ret 框架，加 abnormal-return 口径与 event_type 分段；权重键 `news_hotspot` 进 engine_r 参与乘性更新。
3. **防前视红线对齐**：T0=asof 收盘（日线粒度），评估需 T+5 全部可算才 settled；盘中小时级信号若未来接入，需引入 market_hours 式的盘中/盘外锚定。
4. **去重先行**：消息采集层加 URL/标题 hash + 同源同票时间窗去重，避免同一事件重复打分抬高 pos_ratio 类共识指标。
