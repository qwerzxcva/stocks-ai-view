# 知识卡 → 引擎改动：待办与验收标准

> 2026-10-30。**这份文件只写还没做的**。已做的落在代码里（见文末「已落地」），不在这里重复记账。
> 每条都带「验收标准 + 可复现命令」；没有验收标准的条目不许开工。
>
> 前置纪律（三张卡共同要求的）：
> 1. 方向与权重一律从校准表取，**禁止硬编码书本先验**；
> 2. 置信度「低」的条目（#10 #24 #25 #31 #33）不进代码；
> 3. 任何动到 `engine_spec` / `engine_v5` / `CanonicalEngine` 的改动，**端云必须同批 bump**
>    （`spec_version` 变 → `engine_spec --verify` / `golden_21 --verify` / `build_engine_manifest --check` /
>    `verify_engine_consistency` 全链），否则 `CanonicalEngineParityTest` 必红。

## B1 · CanonicalEngine 主分做族内去共线（来源 #32、batch2 §4）

- **现状**：`LocalEngine.EngineFamily` 已有 6 族族级计票；但主分 `CanonicalEngine.score` 仍是逐指标计票
  （`boll_keltner`/`donchian`/`pivot` 都在答「价格站在通道哪一侧」，`supertrend`/`vortex` 近乎二元）。
- **要做**：主分也按族归一（同族只出 1 票，族内按偏离中性量加权平均），权重表加 `family_weights`。
- **验收**：① `parity_matrix.py` 89/89 仍全绿（端云同步后）；② 同一批 770 只票的 score 分布：族归一前后
  的秩相关 ≥ 0.95，且「8 个看多实际只代表 2 族」的样本 resonance 明显下降（证明共线虚高被压掉）。
- **命令**：`python3 scripts/parity_matrix.py`、`python3 scripts/engine_spec.py --verify data/parity/golden_v5.json`、
  `./gradlew :engine-algo:test --tests "*CanonicalEngineParityTest*"`
- **阻塞**：改主分=改口径，必须端云同批 + 重生成 golden。**当前有并发改动正在同一批文件上，先不要抢**。

## B2 · `flow` / `tape` 进 device_engines（来源：`docs/ISSUES.md` 已记缺陷）

- **现状**：`externalVotes` 对 flow/tape 用 `deviceCanVote` 准入，但 18（现 19）个 id 里长期没有它们 →
  两票从未进过共振比，只经 `blend` 的 0.07/0.05 参与总分。
- **要做**：`book` 已进 `device_engines`；把 `flow`/`tape` 也加进去，再看分数分布变化。
- **验收**：① 加进前/后同快照分数对比表，逐只列出「入选池变化」；② resonance 变化有解释（多空票数变多）；
  ③ 若入选池收缩（不只是扩张），必须逐只人工确认，不许只看总数。
- **命令**：`./gradlew :engine-algo:test --tests "*BookTurnOnDiffTest*"`（同款关/开对照可直接复用，换成 flow/tape）
- **风险**：会让 resonance 首次反映资金流/盘口 → 分数会动，属口径变更，需端云同批。

## B3 · 书籍票权重按标签分档（来源 #4/#15/#29 校准表）

- **现状**：`weights.book` 是**单一标量** 0.6，不管命中的是 `mtr:bottom_confirmed`（+0.67%, t=2.20）
  还是 `limit:board2`（−1.16%）。
- **要做**：改成按标签/标签族取权重（`weights.book_by_tag`），负期望标签直接给 0（不让它投票）。
- **验收**：① 每个标签的权重与该标签的实测 t 值方向一致（负 t 的标签权重 = 0）；
  ② 端上 `BookSignalEngine.CALIB` 与云端校准表逐项一致（现有测试已锁）；
  ③ 关/开对照里不得出现「负 t 标签被投成正票」的样本。
- **命令**：`python3 scripts/signal_validator.py --calibrate`、`./gradlew :engine-algo:test --tests "*BookSignalEngineTest*"`

## B4 · 涨停强度用分钟数据替代「收盘=最高」近似（来源 #15 #29、`SUMMARY-33` 缺口 A）

- **现状**：封板强度只能近似成「收盘 = 最高」，一字板与深水拉板无法区分。
- **验收**：需要 `intraday_events` 有数据（当前 0 行）→ 先补数；补齐判据：
  首封时间 < 30min、开板次数 = 0、封单占流通比 > 1% → 才算 `strong_seal`。
- **命令**：数据到位后 `python3 scripts/limit_structure.py --recalibrate`，对比旧标签的 5 日期望。
- **阻塞**：数据缺口，**不许用日线硬凑**。

## B5 · 财报类判据的解禁门槛（来源 #18 #19 #20 #21）

- **现状**：`financial_summary` 108 只 × 2 指标 = 覆盖 14%，任何「财报选股」都是在 14% 样本上做全市场结论。
- **验收**：覆盖度 ≥ 80% 且至少 3 年序列，才允许 `fundamental_filter` 从 report-only 转为出票；
  转正前必须给出「有财务数据 vs 无财务数据」两组票的 5 日期望差与 t 值。
- **命令**：`python3 scripts/fundamental_filter.py --coverage`（返回 status=None 即未检查）

## B6 · 题材→个股映射的可用性门槛（来源 #17）

- **现状**：两套口径同名率 3.3%，只能做板块级。
- **验收**：`mapping_health()` 同名率 ≥ 70% 才允许个股级题材票；未达标前，题材只做「板块条件器」。
- **命令**：`python3 scripts/theme_engine.py --health`

## B7 · 三层信号的「压分」守卫（本轮实测发现，来源 = 数学而非书）

- **现状**：书籍票只投 +1，但中枢是 `(Σ w·vote·strength)/(Σ w)`，新票贡献 `w·(s−base)/(den+w)`，
  `s < base` 时**加票反而压分**。6 只样本里压分 0 只，但样本太小。
- **要做**：在扫描流程里加一条统计守卫——每日记录「book 有票的票」中抬分/压分只数与金额级影响；
  压分比例 > 20% 时告警并从入选池变化清单里点名。
- **验收**：① 守卫输出可复现（同一快照两次跑结果一致）；② 有 book 票的票里，压分样本逐只列名；
  ③ 若出现「入选池收缩」，必须在发布说明里点名。
- **命令**：`./gradlew :engine-algo:test --tests "*BookTurnOnDiffTest*"` → `engine-algo/build/reports/book_weight_diff.md`

## 已落地（不重复记账）

- 三层信号端上重算 + 只投 +1：`BookSignalEngine.kt`、`EngineChain.externalVotes`。
- **书籍票权重改由云端规格下发**（本轮）：`EngineChain.bookWeightOf()`，缺键即停用；
  云端 `engine_spec` 3.1.1 登记 `weights.book = 0.6` 且 `device_engines` 含 `book`。
- 族级计票（#32 的一半）：`LocalEngine.EngineFamily`。
- 交易管理规则层（#11~#14 #27 #28 #30）：`trade_plan.py`（仓位/止损/时间止损/熔断/期望值）。
