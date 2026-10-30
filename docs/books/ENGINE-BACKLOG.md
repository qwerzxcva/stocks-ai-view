# 卡片 → 引擎改动：**验收标准与验证命令**（`ENGINE-OPTIMIZATION.md` 的补充）

> 2026-10-30。**待办正文与复算证据在 [ENGINE-OPTIMIZATION.md](ENGINE-OPTIMIZATION.md)**（E1~E8，每条带复算入口），
> 本文不重复其分析，**只补那篇没有的一栏：改完怎么算通过**（验收标准 + 可复现命令 + 阻塞条件）。
> 该篇明写「E1–E8 一条未改，需单独授权」——本文同样不含任何已执行的引擎变更。
>
> 通用前置（三张卡共同要求，改任何一条都适用）：
> ① 方向与权重一律从校准表取，禁止硬编码书本先验；
> ② 置信度「低」不进代码（#10 #24 #25 #31 #33）；
> ③ 动 `engine_spec` / `engine_v5` / `CanonicalEngine` ⇒ **端云同批 bump**（`spec_version` 变会连锁
> `engine_spec --verify` / `golden_21 --verify` / `build_engine_manifest --check` / `verify_engine_consistency`），
> 否则 `CanonicalEngineParityTest` 必红。

## 一、E1–E8 的验收标准（编号与 `ENGINE-OPTIMIZATION.md` 对齐）

| 项 | 通过标准（可判定） | 可复现命令 |
|---|---|---|
| E1 反转层删 12 条重复组合键 | 删后重跑校准：`table` 行数 116→104，**每条幸存标签的 `(n, avg_fwd, t, win_rate)` 四元组逐字节不变**（等价改写） | `python3 scripts/signal_validator.py --calibrate`；对比 `data/reversal_calibration.json` 前后四元组集合 |
| E2 端上 `MIN_T` 1.5→2.5 | ① 端上 `CALIB` 只剩 t≥2.5 的标签；② 保留标签数下降但**每条与云端校准表一致**；③ 新增一条测试锁 `MIN_T` 值 | `./gradlew :engine-algo:test --tests "*BookSignalEngineTest*"` |
| E3 订单流 3 条压成 1 条 | 合并后 `flow:*` 标签数 3→1，且合并条目的 `n` = 三条 `n` 之和、`avg_fwd` = 按 n 加权平均（误差 <1e-6） | 读 `data/orderflow_calibration.json` 复算 |
| E4 涨停 4 条压成 1 条 | 同上口径；**且文档必须写明「合并不解决显著性」**（4 条 t=1.96~2.33 仍未过 Bonferroni 3.52） | 同上；对照 `ENGINE-OPTIMIZATION.md` 第一节假阳性统计 |
| E5 `BOOK_WEIGHT` 收紧为不可越界 + 测试 | 权重越界（≤0 或 >2）时**不出票且 degraded 留字**；测试覆盖 0 / 0.6 / 2.0 / 3.0 四个输入 | `./gradlew :engine-algo:test --tests "*EngineChainSelfCheck*"` |
| E6 三套「权重」改名分开 | 代码里不再有裸 `weights`：`spec.weights`（指标权重）/ `engine_weights.json`（校准产物）/ `BOOK_WEIGHT`（外部票）三者命名可区分，各自单一写入口 | `grep -rn "weights" engine-algo/src/main/java/com/skapp/app/data/ \| grep -v "spec.weights"`，人工过一遍 |
| E7 ConnorsRSI 补校准或 1.0→0.6 | 二选一：补校准后该指标有 `t` 与 CI；或降到 0.6 并在此后一次 `--calibrate` 里不再被自动抬回 1.0 | `python3 scripts/weight_calibrator_v2.py`（注意 `weight_policy.writer` 单一写入口） |
| E8 端云多层期望对齐 | 层集合（`mtr/limit/flow`）与 `CALIB` 键集合**双向相等**；不一致时测试失败并打印差集 | `./gradlew :engine-algo:test --tests "*BookSignal*"` + `python3 scripts/signal_hub.py --selftest` |

## 二、我要补的一条（E 系列之外，本轮实测发现）

### B7 · 三层信号的「压分」守卫

- **事实**：书籍票只投 +1，但中枢是 `score=(Σ w·vote·strength)/(Σ w)`，新票贡献
  `w·(s−base)/(den+w)` —— **`s < base` 时加票会压分**。所以「只投 +1 ⇒ 只抬分 ⇒ 候选池只扩不缩」是错的。
- **本轮实测**（6 只真实日线夹具，`book` 关 vs 开 w=0.6）：有票 2/6，抬分 2 只（sz000701 +1.50、sz000678 +0.20），
  **压分 0 只**。样本太小，不能当全市场结论。
- **通过标准**：① 同快照两次跑结果一致；② 有 book 票的票里压分样本**逐只列名**；③ 压分比例 > 20% 告警；
  ④ 若入选池收缩，发布说明必须点名。
- **命令**：`./gradlew :engine-algo:test --tests "*BookTurnOnDiffTest*"` → 报告落
  `engine-algo/build/reports/book_weight_diff.md`（测试遍历夹具全量，往 `testdata/kline_e2e_20261030.json`
  多导几只票即自动纳入，不用改代码）。

## 三、数据缺口 → 补齐后才许动（顺序不能倒）

| 缺口 | 挡住 | 通过标准 | 命令 |
|---|---|---|---|
| 分钟/L2 逐笔 | 封单强度、首次封板时间 | `intraday_events` 有数据；首封 <30min、开板 0 次、封单占流通 >1% 才算 `strong_seal` | `python3 scripts/limit_structure.py --recalibrate` |
| 龙虎榜席位 | 主力/游资识别 | `events` 表含席位字段且非「102 行非此用途」 | — |
| 财务全量 | #18~#21 | 覆盖 ≥80% 且 ≥3 年；转正前给出「有/无财务数据」两组 5 日期望差与 t | `python3 scripts/fundamental_filter.py --coverage` |
| 宏观序列 | #25 #33 | M2/CPI/名义 GDP/产出缺口有序列 | — |
| 题材→个股映射 | #17 | `mapping_health()` 同名率 ≥70% | `python3 scripts/theme_engine.py --health` |

## 四、已落地（不重复记账）

- 三层信号端上重算 + 只投 +1：`BookSignalEngine.kt`、`EngineChain.externalVotes`。
- **书籍票权重改由云端规格下发**（2026-10-30）：`EngineChain.bookWeightOf()`，缺键即停用、不计票不稀释；
  云端 `engine_spec` 3.1.1 登记 `weights.book = 0.6` 且 `device_engines` 含 `book`。
- 族级计票（#32 的一半）：`LocalEngine.EngineFamily`（6 族）。
- 交易管理规则层（#11~#14 #27 #28 #30）：`trade_plan.py`。
