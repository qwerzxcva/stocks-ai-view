# 软件—云端协同架构（契约版 v3）

> 本文不是旧 `DUAL_PLATFORM.md` / `ALGO_ARCHITECTURE_AUDIT.md` 的修订，而是**重写**：
> 用「一份注册表 + 四道闸门」替换过去「两端各写一遍、靠口头约定对齐」的做法。
> 所有数值口径的事实来源是 `scripts/engine_spec.py`（生成 `data/engine_spec.json`），
> 本文只解释设计，不复述参数——参数改了本文不需要跟着改。

## 0. 一句话

**云端算「需要全市场视角的东西」，端上算「只需要本地 K 线的东西」，两边算同一件事时，
由 `engine_spec` 声明唯一口径、由 `golden_v5.json` 逐票比对。**

---

## 1. 六层分工与权威边界

| 层 | 职责 | 云端实现 | 端上实现 | 权威方 |
|---|---|---|---|---|
| **数据层** | 行情/涨停/宽度/资金流原始事实 | `collect_*` / `build_limit_history.py` / `build_market_breadth.py` / `build_market_now.py` | 实时盘口 `snapshot`（东财 clist，只读） | **云端**（历史与全市场池）；端上只做当日实时 |
| **特征层** | 指标计算（RSI/MACD/KDJ/BOLL/Keltner/…） | `scripts/indicators_ext.py`（唯一实现，28 个函数） | `engine-algo/CanonicalIndicators.kt` | **双实现、必须逐位对齐**（±1e-3） |
| **引擎层** | 单票打分/行业轮动/因子合成/关键价位 | `engine_v5.py`、`rps_rotation.py`、`factor_composite.py`、`sector_rotation.py`、`key_levels.py`、`trend_strength.py`、`cycle_pool_v2.py`、`ai_pick_v4.py` | `CanonicalEngine.kt`（21 票）、`LocalEngine.kt`、`LocalSectors.kt` | 技术面 17 票：**双实现对齐**；外部 4 票：**云端唯一** |
| **融合层** | 市场状态 → 综合能量 | `engine_fusion.py`（E + agreement + state） | `MarketStateEngine.kt`（相位/宽度） | **云端**（依赖外部账本与跨市场数据） |
| **决策层** | 仓位、风控、名单 | `decision_engine.py`、`risk_budget.py`、`strategy_card.py`、`premarket_picks.py`、`build_today_picks.py` | `PositionCap.kt`、`ExitPlan.kt`（消费云端参数） | **云端产参数，端上产交互即时建议** |
| **渲染层** | 信息架构/组件/令牌 | — | `ui/theme/SkTokens.kt`、`ui/components/*`、`ui/screens/*` | **端上唯一** |

判断规则（放之四海）：
1. 需要**全市场横截面**（rank/z-score/行业广度）→ 云端。
2. 需要**外部数据**（行业映射、主力资金、涨停池连板数、跨市场情绪、picks 账本）→ 云端。
3. 只需要**该票自己的 OHLCV** → 双实现，进 golden 比对。
4. 只需要**今日快照**且要求零延迟 → 端上，不进 golden（但要声明 `availability: [device]`）。

---

## 2. 统一注册表（`scripts/engine_spec.py`）

生成产物 `data/engine_spec.json`（当前 `spec_version = 3.0.0`），端上作为资源
`engine-algo/src/main/resources/engine_spec.json` 打包，`EngineSpecLoader.BUNDLED_VERSION`
必须与之相等，否则 `drift = true`，界面上标「算法漂移」而不是静默用旧逻辑。

四张表：

| 表 | 内容 | 用途 |
|---|---|---|
| `VOTE_ENGINES`（21 项） | `id / family / weight_key / availability / params{vote_rule, strength_rule}` | 单票投票的**唯一**参数来源。改阈值只改这里 |
| `PRODUCTS`（17 项） | `id / path / writer / layer / version / availability / top_fields / item_fields / params / inputs / external_inputs` | 产物契约。`contract_check` 按它对 `data/*.json` 做字段校验 + **单写者校验** |
| `CALIBERS` | `limit_up`（交接 `limit_caliber.py`）、`main_board`、`indicator_lib` | 口径唯一化：涨停阈值、主板范围、指标库位置 |
| `WEIGHT_POLICY` | `file / writer / candidate_writer / reader / load_order / missing_fallback / bounds` | 权重只允许一个写者、一个读者、一条加载顺序 |

`AGGREGATION` 声明打分常数：`score = 50 + 50·base·(0.6 + 0.4·resonance)`，
`resonance = max(bull,bear)/(bull+bear)`，`base = Σw·v·s / Σw`。

### 2.1 引擎清单（名称 / 版本 / 输入 / 输出 / 权重 / 阈值）

分三组，**判据是「端上能不能算」**，不是「重要不重要」：

- **A. 云端唯一权威（端上只读快照，禁止重算）**
  `multidim`(v1，10 维 • 需东财资金流/板块成分/海外)、`key_levels`(v1，Pivot/Keltner×3/布林)、
  `premarket_picks`(v1)、`trend_strength.msci`(v1，需 global_markets)、
  `engine_fusion`(v1，需 picks 账本)、`risk_budget`(v1，需账本+全市场波动)、
  `cycle_pool_v2`(v2，需行业映射+长历史)、`market_breadth_history`(v1，需涨停池)。
- **B. 端云双实现，必须逐位对齐（进 golden，容差见 §3）**
  `engine_v5` 的 21 票里的 **17 个技术票** + 聚合公式；
  `rps_rotation` 的 RPS 定义（端上 `LocalSectors` 复刻）；`factor_composite` 的 z-score 合成。
- **C. 仅端上（声明 `availability: [device]`，不进 golden）**
  实时快照派生：`pulse`/`sectors`/`emotion`/`execution`（见 `PIPELINE_STAGES`）。

`PIPELINE_STAGES`（10 段 DAG）声明端上流水线顺序与 `needs/produces`，端上不再自己写死。

---

### 2.2 生成物的唯一写入方（`scripts/sync_parity_artifacts.py`）

`skapp-app` 里有两类文件是**生成物且无合并语义**——谁写谁赢，覆盖了也不会报错：

- `engine-algo/src/main/resources/engine_spec.json`
- `testdata/parity/golden_{v5,v2,21}.json`

2026-10-29 实测到并发会话同时改同一工作区（一个会话改引擎，一个会话改规格/夹具），
为了让这种覆盖变成**可发现、可追责**的事故而不是静默事故，所有写入收敛到一个入口：

```bash
python3 scripts/sync_parity_artifacts.py publish --writer <会话/人>   # 唯一写入口
python3 scripts/sync_parity_artifacts.py check                        # 归属校验（CI 2e/4）
python3 scripts/sync_parity_artifacts.py who                          # 看租约与登记
```

- `publish` 先跑四道闸门（engines_v2 / golden_21 / calibrate 的 `--verify` + `--check-products`），
  任一红就**拒绝同步**；通过后拿 `flock`、原子写（`tmp` + `os.replace`）、把每个目标的 sha256 登记进
  `data/parity/MANIFEST.json`（含 writer 与时间）
- `check` 重算磁盘指纹与登记比对：文件被**登记写入方以外**的进程改过就红，并打印登记/实际两份哈希
- 租约只在 publish 期间持有：`flock` 只对活着的描述符有效，跨会话长期占锁不成立，
  跨会话的事后防护靠 MANIFEST 指纹

**为什么是 sha256 不是 mtime**：任何一次 `cp` 都会刷新 mtime，sha256 不会。

已实测的负向验证：往 `resources/engine_spec.json` 追加一个换行 → `check` 退出码 1，
报出「指纹不符」与两份哈希 → `publish` 重新同步后恢复 OK。

### 2.3 状态引擎注册表（`spec.state_engines`，2026-10-29 新增）

`flow` / `tape` / `rotation` / `vol_target` 不参与 21 票加权，但都会进决策展示，
所以同样需要「参数单源 + 权威方 + 黄金向量」。这四个此前**云端没有参考实现**——
端上改了公式，没有任何东西会红。

| 引擎 | 端上 | 云端参考实现 | 黄金向量 | 输入 |
|---|---|---|---|---|
| flow 资金流强度 | `FlowEngine.kt` | `engines_v2.py:flow_screen` | `golden_v2.json#flow` | 实时行（+ 可选 5 日主力序列） |
| tape 量价结构 | `TapeEngine.kt` | `engines_v2.py:tape_compute` | `golden_v2.json#tape` | 日 K + 实时行 |
| rotation 板块轮动 | `RotationEngine.kt` | `engines_v2.py:rotation_compute` | `golden_v2.json#rotation` | 板块行（+ 5 日动量/额占比） |
| vol_target 波动率闸门 | `VolTargetGate.kt` | `engines_v2.py:vol_target_compute` | `golden_v2.json#vol_target` | 本地日级存档 |

三档置信（`flow`）是严格比对的字段而不是装饰：`tier=local/local_main/cloud/local_abs`
决定这个数是「真值」还是「代理量」，分数对上而档位漂了，界面就会把代理量标成真资金流。

---

## 3. 一致性契约：怎么定义「对齐」，容差是多少

| 对象 | 判据 | 容差 | 执行者 |
|---|---|---|---|
| 单个指标 vote | 完全相同（整数 -1/0/1） | 0 | `CanonicalEngineParityTest.逐票复现云端 engine_v5` |
| 单个指标 strength | `abs(Δ) <= 1e-3` | 1e-3 | 同上 |
| 单票 score | `abs(Δ) <= 0.05` | 0.05 | 同上 |
| bull/bear/neutral 计数 | 完全相同 | 0 | 同上 |
| 规格版本 | `spec_version` 字符串相等 | 严格 | 同上 + `EngineSpecLoader` |
| 参数文件 | sha256 相等 | 严格 | `build_engine_manifest.py --check` |
| 引擎版本 | manifest `version` == 端上 `EXPECTED_VERSIONS` | 严格 | `verify_engine_consistency.py` |
| 产物结构 | 注册表 `top_fields` 全在 | 严格 | `engine_spec.py --check-products` |
| 21 票（含 3 张外部票） | 票名集合 + 逐票 vote + 聚合 score | vote/集合严格，score 0.05 | `Canonical21VoteParityTest` |
| flow / tape / rotation | 逐字段（含 `tier`/`status`/`signal` 档位） | 档位严格，数值 `max(1e-6, 1e-12·|x|)` | `EngineV2ParityTest` |
| vol_target（波动率闸门） | `scale`/`status`/`sampleDays` | 1e-9 | `VolTargetParityTest` |
| 校准参数 | `params`/`decision.wire` 重算相等 | 严格 | `calibrate_risk_regime.py --verify` |

**黄金向量怎么产生**（`engine_spec.build_golden`）：取真实分片 `day_qfq_*.csv`，按代码排序
固定抽样 6 只 × 130 根，**直接调用 `engine_v5.build_votes/score_stock`**（不另写一份公式，
否则就是第三份实现，什么也验证不了），并**带校准权重重算**（与 CLI 同一条加载顺序）。
`why` 文案不进向量——措辞变化不该让 parity 变红。

**为什么是 1e-3 / 0.05**：Kotlin `Double` 与 Python `float` 同为 IEEE754 双精度，
差异只来自运算顺序与 `round()` 策略；`strength` 在 spec 里统一保留 3 位（`AGGREGATION.round`），
所以 1e-3 是「舍入末位」，0.05 是 21 项误差的线性累积上界（21×1e-3×权重≈0.02，留 2.5 倍余量）。

---

## 4. 单一事实来源：本次废弃/收敛的五件事

### 4.1 `engine_weights.json` 恒等权空转 → 权重真正生效
- 事实（改前）：`engine_v5.main()` 无 `--weights` 参数，`score_stock` 调用处不传 `weights`，
  只有 `weight_calibrator.py:124` / `_v2.py:135` 在写文件，没有人读。
- 改后：`engine_v5.py` 新增 `--weights`、默认读 `data/engine_weights.json`、
  产物写 `meta.weights`（来源串）+ `meta.weights_used`（实际字典）；`--weights none` 强制等权。
- 单一来源链：`engine_weights.json`（校准输出）→ `engine_spec.json.weights`（规格快照）
  → 端上 `CanonicalEngine.score(weights = spec.weights)`。**三处同源，只有校准脚本能写第一处。**
- 实测：同一条 K 线，等权 `score=65.8`；`{"RSI":0.6,"MACD":1.5}` → `66.9`（权重确实参与）。

### 4.2 `weight_calibrator` v1/v2/v3 多写 → 单写者
- v1 默认输出改为 `data/engine_weights.candidate.json`（候选，不覆盖生产）；
- v2 是 `engine_weights.json` 的唯一写者（带 provenance，链上每轮跑）；
- v3 只写 `weight_calibration.json`（报告），不碰权重文件。
- 登记在 `WEIGHT_POLICY`，`--check-products` 的单写者校验兜底。

### 4.3 `decision.json` 双写双实现 → 单写者 + 降级写别名
- 权威：`decision_engine.py`（`cards/avoid/position_advice`，`engine-chain.yml:301,391`）；
- `decision.py`（凯利仓位树，完全不同的结构，且硬编码 `BASE=/home/agentuser/stockdb`）
  改为写 `data/decision_legacy.json`，需 `ALLOW_DECISION_OVERWRITE=1` 才允许覆盖权威产物。
- 下游（`risk_engine.py:187`、`portfolio_optimizer.py:238`、`pool_audit.py:14`、
  `gitcode_ai_review.py:102`）按 `cards` 读，此前一旦被覆盖就是**静默读空**。

### 4.4 `market_phase.json` 双写双实现 + `engine_fusion` 契约断裂 → 单一写者 + 判据
- 权威：`phase_runner.py`（`{ts, dates, phases, phase, label, recent, structure, source}`）。
- `market_phase.py` 是**算法库冒烟入口**，`__main__` 用 2026-10-01..09 的合成梯队造 demo；
  它的 `--out` 曾把权威产物覆盖成 `{ts, daily, phases, labels}`（**磁盘上 10-17 那版就是它**）。
  现在写 `data/market_phase.json` 会被拒绝（`sys.exit(2)`），确需覆盖要 `--force`。
- `engine_fusion.phase_score` 原来只 `ph.get("phase","")` → 那份 legacy 文件里没有该键，
  **0.30 权重被静默吞成 0**。现在：`phase` →（有 `dates` 时）`phases[-1]` → 否则记
  `missing_inputs` 并返回 0，`engine_fusion.json` 增 `n_missing`。
- 实测修复前后：同一份 legacy 文件，修前 `phase_score=0` 且 `missing_inputs=[]`；
  修后 `missing_inputs=["market_phase.json(legacy/demo 格式…)"]`；重跑 `phase_runner` 后
  `phase=ignite → 40`、`E=31.0 → state=进攻`、`risk_budget budget=32.5%`。

### 4.5 涨停口径两套 → `scripts/limit_caliber.py`
- 改前：`phase_runner.py:82` 写死 `ret>=0.095`（20cm/30cm 板块全漏判）；
  `build_limit_history.py:48` 按前缀 9.9/19.9/29.9。同一只创业板票两份产物可以互相矛盾。
- 改后：唯一实现 `limit_pct / limit_line / is_limit_up / is_limit_down / is_main_board /
  caveats`，触发线 = 上限 − 0.15（容差，容纳分位舍入）；ST 5% 由 `name` 触发。
  `phase_runner` 与 `build_limit_history` 都已改为 import 它，`CALIBERS.limit_up` 登记。
- 副作用（有意）：`market_phase.json` 的阶段判定结果变了（20cm 涨停现在算进梯队），
  这是**修正**不是回归，重跑后用新产物。

### 4.6 指标库一份实现
- 唯一实现：`scripts/indicators_ext.py`；`trend_strength.py` 自带的 `rsi/macd_at`
  原本是 SMA 近似版（与 Wilder 版不是同一个数），现委托 `indicators_ext._rsi / ema`。
- 消费方声明在 `CALIBERS.indicator_lib.consumers = [engine_v5.py, key_levels.py, trend_strength.py]`。
- `engine_v5.py` 的 §6 名义 `BOLL` 实际调 `keltner`，spec 里 `boll_keltner.params` 记的是
  Keltner 参数并在 `note` 写明——**以代码为准，不粉饰**。

### 4.7 `engine_v5.indicators` 计数错误
- 改前产物写死 `"indicators": 20`，`build_votes` 实为 21 项（17 技术 + 4 外部，含 `below_ma60`）。
- 改后：常量 `EXPECTED_VOTES = 21`（容量），另加 `votes_observed{min,max,external_available}`
  记录**本轮实际票数**（无 `all_sources` 时只有 17）。两个数分开，避免再出现「容量冒充实际」。

---

## 4.8 riskBudget / regime：从「经验值不接线」到「回测校准 + 有条件接线」

上一轮拒绝接线是对的：`vol_target_annual=0.18 / max_pos=0.6 / regime_cap(0.9/0.6/0.3)`
是经验值，未经校准就替换实盘仓位比不接更糟。本轮补上了校准：

`scripts/calibrate_risk_regime.py`（真实数据，非合成）：

- 数据：`data/index_kline.json` 5 个指数 × 500 个交易日（2024-10-08 ~ 2026-10-28）
- 方法：网格搜索（vol_target 5 × max_pos 3 × 均线 2 × 停留 3 = 90 组），
  **前 2 折选参、最后 1 折样本外**，成本 5bp × |Δ仓位|
- 选中参数：`vol_target_annual=0.12, max_pos=0.8, ma 30/90, dwell=5`

| 指标（样本外 168 日） | 本策略 | 满仓 B&H | 固定 0.6 |
|---|---|---|---|
| Sharpe | -0.897 | -1.014 | -1.014 |
| 最大回撤 | **-7.04%** | -20.23% | -12.53% |
| 年化波动 | 9.68% | 19.35% | 11.61% |
| 平均仓位 | 0.446 | 1.0 | 0.6 |

判据（先写死、不事后找理由）全过：样本外 Sharpe ≥ B&H、回撤 ≤ B&H×0.8、3 折里 2 折不输固定 0.6
→ `decision.wire=true`。

**接线决策（分开处理，不搞一刀切）**：

1. **vol_target 已接线（端上自算）**：`PositionCap.compute(..., volScale=)` × 上限，
   系数来自 `VolTargetGate`。为了让这条闸门在端上真的算得出来，`MarketDayArchive.MAX_DAYS`
   从 1 提到 65（原设计只留当天，导致任何 60 日序列永远不可得；每天 ~50KB，65 天约 3MB）。
2. **regime 状态机不接线**：它需要跨市场广度序列，端上只有 17 天广度历史、260 天涨停名单，
   不足以为「六态 + 最短停留」定阈值；现阶段只作为云端 `risk_budget.py` 的 `regime_cap` 输入，
   并保留 `decision` 字段供追溯。**没接线不等于没校准**——参数与判据都在规格里可查。
3. 说清楚代价：vol_target 相对固定 0.6 的超额很小（3 折里 2 折略胜、1 折略负），
   它的真实价值在**回撤控制**（-7.0% vs -12.5%）而不是收益。把它当「提高收益」的来源就理解错了。

### 4.9 真机验收缺位时的替代方案（本环境实测不可用）

本容器里三条真机通道全部不可用（`Shizuku` → `SHIZUKU_NOT_READY`；`vscreen` 依赖它；
`a11y` → `A11Y_NOT_ENABLED`），装包与截图做不了。替代方案分两层，**都不等于真机渲染验收**：

1. **APK 表面级验收** `scripts/verify_apk_surface.py`（实测 OK）：
   包内 `engine_spec.json` 自洽（3.1.0 / 21 引擎 / 4 状态引擎 / 权重 / 聚合阈值）、
   **包内规格的 sha256 与 MANIFEST 登记一致**（证明用户装的就是唯一写入方发布的那份）、
   9 个关键类确实进了 DEX（`VolTargetGate`/`PositionCap`/`FlowEngine`/`TapeEngine`/`RotationEngine`…）。
2. **渲染数据契约** `UiRenderContractTest`（3/3 绿）：用云端真实 `risk_budget.json` / `engine_fusion.json`
   跑一遍决策页要显示的数，断言非空、在界面有意义范围内、格式化成文案后不含 `null/NaN/Infinity`。

**覆盖不到**：布局错位、字体缺失、深色对比度、状态栏遮挡、Compose 重组开销。
拿到 root / Shizuku / 无障碍任一通道后，仍需补一次真机逐屏截图。

---

## 5. 变更管理（改算法的正确姿势）

1. 改 `engine_spec.py` 的 `VOTE_ENGINES/PRODUCTS/AGGREGATION` 或 `indicators_ext.py`；
2. `python3 scripts/engine_spec.py --out data/engine_spec.json`（若改了常量则同步升 `SPEC_VERSION`）；
3. `python3 scripts/engine_spec.py --golden data/parity/golden_v5.json --samples 6 --bars 130`；
4. 同步端上：`cp data/engine_spec.json ../skapp-app/engine-algo/src/main/resources/`、
   `cp data/parity/golden_v5.json ../skapp-app/testdata/parity/`，
   改 `EngineSpec.kt:BUNDLED_VERSION` 与 Kotlin 侧 `CanonicalIndicators/CanonicalEngine`；
5. 跑四道闸门（见 §6）。**只改云端不改端上，第 4 步会红；只改端上不改云端，第 2/3 步会红。**

---

## 6. 验证命令（全部实测通过）

```bash
cd stocks-ai-view
python3 scripts/engine_spec.py --out data/engine_spec.json      # spec 3.0.0
python3 scripts/engine_spec.py --check-products                 # OK 17 个产物（1 WARN）
python3 scripts/engine_spec.py --verify data/parity/golden_v5.json --samples 6 --bars 130
                                                                # OK 黄金向量与参考实现一致（6 例）
python3 scripts/build_engine_manifest.py                        # 写清单
python3 scripts/build_engine_manifest.py --check                # OK 一致, 6 个引擎
python3 scripts/verify_engine_consistency.py                    # 结论: 一致
python3 scripts/limit_caliber.py                                # 口径自测（9.9/19.9/29.9 三档）
python3 scripts/phase_runner.py --days 90 --out data/market_phase.json
python3 scripts/engine_fusion.py                                # E=31.0 → 进攻
python3 scripts/risk_budget.py                                  # budget=32.5%
python3 scripts/factor_composite.py --limit 800                 # 1486 只
python3 scripts/sector_rotation.py                              # 64 个行业

# 2026-10-29 新增：状态引擎 / 21 票 / 校准三道闸门（均已实测）
python3 scripts/engines_v2.py --golden data/parity/golden_v2.json
python3 scripts/engines_v2.py --verify data/parity/golden_v2.json   # OK 一致
python3 scripts/golden_21.py --golden data/parity/golden_21.json
python3 scripts/golden_21.py --verify data/parity/golden_21.json    # OK 24 用例
python3 scripts/calibrate_risk_regime.py                            # 校准 + 写 data/risk_regime_params.json
python3 scripts/calibrate_risk_regime.py --verify data/risk_regime_params.json  # OK wire=True
cd ../skapp-app && ./gradlew :engine-algo:test :engine-runtime:test  # 89 个测试全绿
```
CI：`.github/workflows/engine-contract.yml`（四道闸门，PR 触发）；
`engine-chain.yml` 每轮末尾也跑同样的闸门，产物不合契约即 `::error::`。

---

## 7. 端上侧落地状态（本次核对，含未验证项）

已有且与本契约对齐：
`engine-core/EngineManifest.kt`（`EXPECTED_VERSIONS` 6 项）、
`engine-algo/EngineSpec.kt`（`BUNDLED_VERSION = 3.0.0`，读 `weights`）、
`engine-algo/CanonicalEngine.kt`（默认 `weights = spec.weights`，与云端同源）、
`engine-algo/CanonicalIndicators.kt`、`engine-algo/src/test/.../CanonicalEngineParityTest.kt`
（读 `testdata/parity/golden_v5.json`，容差 1e-3/0.05）、
`ui/theme/SkTokens.kt`（颜色均标注对比度，禁止裸 `Color(0x..)`）、
`ui/components/{CommonUi,SkComponents,ParityStrip}.kt`（空态/加载/一致性条）、
`engine-core/src/test/.../EngineManifestTest.kt`。

**已实测（2026-10-29 更正上一轮的「容器内无 Gradle」结论——实际可跑）**：

```
./gradlew --offline :engine-algo:test :engine-runtime:test
BUILD SUCCESSFUL，89 个测试全绿
```

含 `CanonicalEngineParityTest`（18 票）、`Canonical21VoteParityTest`（21 票）、
`EngineV2ParityTest`（flow/tape/rotation 共 18 个用例）、`VolTargetParityTest`（4 场景 + 参数默认值）。

## 8. 未做 / 下一轮

- `engine_v5.json` 未在本容器重生成（4766 只 × 21 票，本地 >10 min 未完成，x86 runner 6–9 min）；
  磁盘上仍是 10-26 版，缺 `votes_observed`（已列为 `optional_fields`，只 WARN）。
- `event_driven`（事件驱动）只做了注册表设计，脚本未实现。
- 端上 `factor_composite` / `sector_rotation` 的 Kotlin 复刻未做（云端已声明 `availability` 含 device，
  但端上还没有对应实现 → 目前应视为 cloud-only）。
- `market_breadth_history` / `limit_history` 的端上消费路径未核对（端上只读快照）。
