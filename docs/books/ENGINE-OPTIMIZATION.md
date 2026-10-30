<!-- 本轮只读分析产物：未改动任何引擎常量、未跑 --calibrate/publish、未触碰端上仓。 -->
# 引擎优化清单（基于实测校准证据）

> 只读产出。本轮没有修改 `stocks-ai-view` / `skapp-app` 任何文件，没有 git 操作，没有跑 `--calibrate` / `--out` / `publish`。
> 所有数字都能用第五节之外的复算命令重跑（都是只读脚本，读 `data/*.json`，不写盘）。

**复算入口（本文每个数字的出处）**

| 数字 | 复算方式 |
|---|---|
| 表行数 / t 门槛 / Bonferroni / 重复行 | 读 `data/{reversal,limit,orderflow,structure,wyckoff}_calibration.json` 的 `table`，统计 `len(table)`、`n>=100 且 \|t\|>=1.5`、按 `(n,avg_fwd,t,win_rate)` 四元组分组找重复 |
| 标签在股票上的命中数 / 层间重叠 | 读 `data/{reversal,limit,orderflow}_signals.json`（770 只，各 1 个切点）的 `items[].tags` |
| book 分数分布 / 阈值敏感度 | 用 `BookSignalEngine.kt` 的同一套规则（组合键优先 + `min(\|t\|,4)/4` 加权 + `50+50·exp5/3`）在 770 只快照上重算 |
| 权重两套口径 | `data/engine_weights.json`（生效）对 `data/weight_calibration.json`（report_only），16 个共享指标算 Spearman |
| 抬分上界 Δ | 代数：`EngineChain`/`CanonicalEngine` 的加权式；数值校验：模拟 900+ 组票型取最大 |

---

## 一、样本量与显著性风险（表）

**先说结论**：五张表里**没有** `n < 100` 或 `|t| < 1.5` 的条目——写表器已经自过滤（`scripts/signal_validator.py:105` `calibrate()` 的 `if r["n"] < min_n or abs(r["t"]) < min_t: continue`，三引擎均以 `min_n=100, min_t=1.5` 调用：`reversal_engine.py:409`、`limit_structure.py:277`、`orderflow_proxy.py:230`）。

风险不在"漏拦"，而在下面三处，全部可复算：

1. **门槛 1.5 相对"检验次数"太松**。五张表合计 **116 行**，全部经过 `|t| ≥ 1.5` 这道闸。双尾 `p(|t|≥1.5) = 0.134`，纯噪声下**期望假阳性 116 × 0.134 ≈ 15.5 条**。同族 116 次检验要做 FWER 5% 的 Bonferroni，门槛应是 `|t| ≥ 3.52`；被门槛放行的 **79 条里只有 24 条过 3.52**（30%）。
2. **端上 `CALIB` 17 条里有 7 条不过 3.52**（4 条 `limit:*` 全在 t=1.96~2.33，加 3 条 mtr 顶/次级标签）——而 `limit` 层恰好是期望收益最大（+1.4~1.8%）的一层，等于"用最弱的证据下最大的注"。
3. **权重层完全没有显著性检验**。`data/weight_calibration.json` 的 17 个指标条目**没有 `t` 字段**，`rel_weight` 是 `edge` 的 min-max 重标（`scripts/weight_calibrator_v3.py:120`）：`rel_weight = 0.1 + 0.4·(edge−lo)/rng`。而这张表的 16 个 `edge` **全部为负**（−5.0 ~ −7.5），所以它排的是"哪个指标更没用"，不是"哪个更有用"。这个 `rel_weight` 端上有人吃（见三.3）。

### 表 1-A：端上 `BookSignalEngine.CALIB` 的 17 条（按 n 升序）

消费者：`~/workspace/skapp-app/engine-algo/src/main/java/com/skapp/app/data/BookSignalEngine.kt:48-69`（`CALIB`）、门槛 `:73-74`（`MIN_N=100`, `MIN_T=1.5`）。

| label | n | t | avg_fwd_5d(%) | win_rate | 表内位置 | 3.52 门槛 |
|---|---|---|---|---|---|---|
| `flow:delta_down & flow:imbalance_down` | 272 | 3.75 | 1.70 | 58.8 | orderflow_calibration.json:23 | 过 |
| `limit:first_board & limit:active_5d` | 333 | 2.33 | 1.77 | 47.1 | limit_calibration.json:10 | **不过** |
| `limit:first_board & limit:strong_seal` | 352 | 1.96 | 1.42 | 46.6 | limit_calibration.json:31 | **不过** |
| `limit:first_board` | 366 | 1.96 | 1.40 | 46.2 | limit_calibration.json:24 | **不过** |
| `flow:imbalance_down` | 374 | 4.76 | 1.97 | 59.4 | orderflow_calibration.json:16 | 过 |
| `limit:strong_seal & limit:active_5d` | 387 | 2.08 | 1.55 | 45.7 | limit_calibration.json:17 | **不过** |
| `mtr:bottom_confirmed` | 438 | 2.20 | 0.67 | 54.3 | reversal_calibration.json:121 | **不过** |
| `mtr:top_confirmed` | 725 | 1.86 | 0.42 | 53.2 | reversal_calibration.json:149 | **不过** |
| `mtr:second_higher_low` | 1229 | 3.85 | 0.68 | 51.8 | reversal_calibration.json:93 | 过 |
| `mtr:second_lower_high` | 1944 | 1.86 | 0.28 | 50.1 | reversal_calibration.json:156 | **不过** |
| `flow:delta_down` | 4263 | 7.21 | 0.68 | 52.7 | orderflow_calibration.json:9 | 过 |
| `mtr:pullback_down` | 5320 | 6.60 | 0.51 | 52.3 | reversal_calibration.json:23 | 过 |
| `mtr:break_up` | 5555 | 6.61 | 0.56 | 52.2 | reversal_calibration.json:9 | 过 |
| `mtr:extreme_bottom` | 6075 | 6.10 | 0.55 | 51.6 | reversal_calibration.json:44 | 过 |
| `mtr:pullback_up` | 6591 | 4.41 | 0.40 | 49.2 | reversal_calibration.json:65 | 过 |
| `mtr:break_down` | 6795 | 4.60 | 0.41 | 49.2 | reversal_calibration.json:51 | 过 |
| `mtr:extreme_top` | 7137 | 4.07 | 0.35 | 48.6 | reversal_calibration.json:86 | 过 |

补充：`mtr:bottom_confirmed` 的 95% CI 下限只有 **+0.07%**（SE=0.67/2.20=0.305%），`limit:first_board` 与 `limit:first_board & limit:strong_seal` 的 CI 下限是 **0.000%**——即"刚压线"。这两条不是错，但**不该被当成和 `flow:delta_down`（CI 下限 +0.50%）同级**的证据。

### 表 1-B：云端 `confluence_score` 会吃、但 n<200 或 |t|<2.0 的条目（按 n 升序，共 24 条）

消费者：`scripts/signal_hub.py:568`（`layer_expectancy` 默认 `min_n=100, min_t=1.5`）、`:601`（`confluence_score` 同默认）、`:210`（`structure_expectancy` 默认 `min_n=120`）。

| label | n | t | avg_fwd_5d(%) | win_rate | edge | 表内位置 |
|---|---|---|---|---|---|---|
| `ew:impulse_up_w5 & phase:markup` | 123 | 2.04 | 1.39 | 56.1 | trend | structure_calibration.json:428 |
| `chan:third_sell & phase:markup` | 125 | 2.26 | 0.90 | 56.8 | trend | structure_calibration.json:421 |
| `neutral & candle:doji` | 126 | 1.61 | 0.93 | 50.0 | contrarian | structure_calibration.json:407 |
| `wyckoff:sc` | 134 | -1.54 | -0.49 | 48.5 | contrarian | wyckoff_calibration.json:141 |
| `wyckoff:ut & wyckoff:stage_distributing` | 159 | 3.50 | 1.28 | 52.8 | contrarian | wyckoff_calibration.json:22 |
| `ew:impulse_up_w5` | 170 | 1.69 | 0.89 | 55.3 | trend | structure_calibration.json:323 |
| `neutral & candle:bullish_engulfing` | 171 | -2.08 | -0.82 | 42.1 | contrarian | structure_calibration.json:316 |
| `wyckoff:ut & wyckoff:stage_markdown` | 190 | -1.61 | -0.62 | 38.9 | trend | wyckoff_calibration.json:134 |
| `phase:markup & candle:bullish_engulfing` | 196 | -2.62 | -1.24 | 40.3 | contrarian | structure_calibration.json:281 |
| `breakout:up & phase:markup` | 347 | -1.84 | -0.82 | 40.6 | contrarian | structure_calibration.json:197 |
| `bull & breakout:up` | 384 | -1.94 | -0.75 | 40.6 | contrarian | structure_calibration.json:183 |
| `mtr:pullback_up & mtr:failed_break_up` | 396 | 1.82 | 0.62 | 50.0 | contrarian | reversal_calibration.json:184 |
| `mtr:break_down & mtr:failed_break_up` | 453 | 1.99 | 0.67 | 48.8 | contrarian | reversal_calibration.json:128 |
| `mtr:extreme_top & mtr:failed_break_up` | 550 | 1.94 | 0.59 | 47.6 | contrarian | reversal_calibration.json:135 |
| `candle:bullish_engulfing` | 562 | -1.75 | -0.41 | 45.0 | contrarian | structure_calibration.json:134 |
| `failed_breakout` | 865 | -1.72 | -0.33 | 45.2 | trend | structure_calibration.json:78 |
| `mtr:extreme_top & mtr:second_lower_high` | 1944 | 1.86 | 0.28 | 50.1 | contrarian | reversal_calibration.json:163 |
| `mtr:break_down & mtr:second_lower_high` | 1944 | 1.86 | 0.28 | 50.1 | contrarian | reversal_calibration.json:170 |
| `mtr:pullback_up & mtr:second_lower_high` | 1944 | 1.86 | 0.28 | 50.1 | contrarian | reversal_calibration.json:177 |
| `bear & phase:markdown` | 1951 | 1.68 | 0.21 | 50.6 | contrarian | structure_calibration.json:43 |
| `wyckoff:lps` | 3181 | 1.69 | 0.21 | 46.9 | trend | wyckoff_calibration.json:127 |
| `phase:markdown` | 3211 | 1.89 | 0.19 | 50.3 | contrarian | structure_calibration.json:22 |
| `wyckoff:stage_accumulating` | 3246 | **1.50** | 0.11 | 49.3 | trend | wyckoff_calibration.json:148 |
| `mtr:failed_break_up` | 4046 | 1.90 | 0.20 | 47.9 | contrarian | reversal_calibration.json:142 |

三个额外标注：
- `wyckoff:stage_accumulating` 的 `t=1.50` 是**浮点压线**（`|1.5| >= 1.5` 为真），再降一个点就被过滤——这种条目在门限处极不稳定。
- 表 1-B 里 **10 条是负 `avg_fwd_5d`**（`wyckoff:sc` / `neutral & candle:bullish_engulfing` / `phase:markup & candle:bullish_engulfing` / `breakout:up` 系 / `failed_breakout` 系 …）。端上 `BookSignalEngine.kt:525` 有一条断言"校准表不应含负向标签"（因为端上 book 票只投 +1），**云端 `confluence_score` 却把这 10 条当负贡献用**——两端对同一只票可以给出方向相反的"书籍层"结论，且都各自自洽。这是一处真实的口径分叉，不是 bug 报告里的笔误。
- 端上只装 3 层（`BookSignalEngine.kt` 的 reversal/limit/orderflow），云端 `confluence_score` 吃 5 层（`signal_hub.py` `_LAYER_FILES` 含 structure/wyckoff）。同一个 `code` 的"多层期望分"两端不同源。

### 表 1-C：权重层的三条"没有证据却有数值"条目

| 条目 | 位置 | 问题 |
|---|---|---|
| `ConnorsRSI: 1.0` | `data/engine_weights.json:29` | `weight_calibration.json` 里**完全没有** `ConnorsRSI`（`grep -c ConnorsRSI data/weight_calibration.json` = 0），即最新校准对它零记录，但它拿满权重 1.0。17 个指标里唯一一条"无校准支持却全权重"。 |
| `rel_weight` 0.1~0.5（17 条） | `data/weight_calibration.json`（`indicators[].rel_weight`） | 无 `t`、无 CI，纯 `edge` min-max 重标（`weight_calibrator_v3.py:120`）；16 个 `edge` 全为负。且这张表**只写报告**（`scripts/engine_spec.py:544` 的 `report_only`），不进 spec——但端上 `FactorWeightStrategy.kt:124` 直接读它。 |
| `market_advice: "avoid_first"` | `data/weight_calibration.json`（`market_advice`，由 `weight_calibrator_v3.py:126-132` 用 `bear_win_rate − bull_win_rate ≥ 5` 生成） | 结论用的是 `bear_n=2302`（占 3.5%）的分支胜率，表自己的 `findings[2]` 就写着"置信区间宽，不宜作为强结论"。同一份文件自相矛盾。（本轮只读，未核实有没有人消费它；`grep -rn market_advice scripts/` 只命中生成处。） |

**潜伏但当前无害的口径差**（已核）：`signal_validator.load_calibration()` 默认 `min_n=120`（`signal_validator.py:134`），`signal_hub.layer_expectancy()` 默认 `min_n=100`（`signal_hub.py:568`）。两张门槛不一样，但**当前五张表里没有任何条目落在 `100 ≤ n < 120`**，所以暂未产生实际差异；一旦有人用更小的 `min_n` 重跑写表就会立刻分叉。

---

## 二、重复信号与可合并项

判定"重复"的硬标准（可复算，不靠肉眼）：同一张表里 `(n, avg_fwd, t, win_rate)` 四元组完全相同 → 两条标签命中的是**同一批切点、同一批前向收益**，统计上是同一个估计量，多写几行只是把证据数从 1 说成 N。

### 2.1 反转层：26 行里 12 行是数值完全相同的重复行

`reversal_calibration.json` 26 行 → 只有 **14 个独立的统计体**：

| 独立估计 | n | t | avg_fwd | 被重复写成的行 |
|---|---|---|---|---|
| break_up | 5555 | 6.61 | 0.564 | `mtr:break_up`(:9)、`mtr:extreme_bottom & mtr:break_up`(:16) |
| pullback_down | 5320 | 6.60 | 0.507 | `mtr:pullback_down`(:23)、`:30`、`mtr:break_up & mtr:pullback_down`(:37) |
| break_down | 6795 | 4.60 | 0.407 | `mtr:break_down`(:51)、`mtr:extreme_top & mtr:break_down`(:58) |
| pullback_up | 6591 | 4.41 | 0.395 | `mtr:pullback_up`(:65)、`:72`、`mtr:break_down & mtr:pullback_up`(:79) |
| second_higher_low | 1229 | 3.85 | 0.676 | `:93`、`:100`、`:107`、`:114`（4 行） |
| second_lower_high | 1944 | 1.86 | 0.279 | `:156`、`:163`、`:170`、`:177`（4 行） |

**为什么会重复**：`signal_validator.validate()` 对同一根 bar 上的标签做两两组合（`signal_validator.py:71-75`），而 MTR 的状态标签是**层层叠加**的——`extreme_bottom` 是 `break_up`/`pullback_down` 的超集。770 只快照实测：命中 `mtr:extreme_bottom` 的 115 只里，108 只同时有 `break_up`、87 只同时有 `pullback_down`；**没有一只只有 `break_up` 而没有 `extreme_bottom`**。所以 `"extreme_bottom & break_up"` ≡ `"break_up"`。

**能不能合并、合并后 t 会不会更稳**：能删，但**删了 t 不会变大**——重复行的 n 本来就相同（不是把一个子集并进超集，而是同一个集合写了两遍）。合并的收益是"证据数从 26 回到 14"，不是显著性的提升。t 只随真实样本增长，`t ∝ √n`。

**已实测的合并代价 = 0**：把 12 条重复组合键从端上 `CALIB` 里删掉，在 770 只快照上重算 book 分，**分数变化的票数 = 0，有证据票数 470→470**（见四-E1 的验证命令）。

### 2.2 涨停层：4 行是同一批票的三种切面，合并也救不了显著性

| 行 | n | t | 与 `first_board` 的关系 |
|---|---|---|---|
| `limit:first_board` | 366 | 1.96 | 基准 |
| `limit:first_board & limit:strong_seal` | 352 | 1.96 | 子集（770 快照：`first_board` 10 只 ⊂ `strong_seal` 11 只，无一例外） |
| `limit:first_board & limit:active_5d` | 333 | 2.33 | 子集（10 只 ⊂ `active_5d` 164 只） |
| `limit:strong_seal & limit:active_5d` | 387 | 2.08 | 兄弟切面 |

合并成一条的并集上界是 387（`strong_seal & active_5d` 已是最大），相对 333 只涨 16%，`t` 最多 ×√1.16 = **×1.08**，即 2.33 → **2.51**，仍远低于 3.52。**结论：合并不解决涨停层的显著性，它缺的是样本期，不是标签切分**（样本是 200 只 × 400 根日 K、`cut_step=2`，涨停是低频事件，n≈350 已经是这套切点的上限）。

### 2.3 订单流层：3 行是同一条 `clv` 定义的三层嵌套，应压成 1 行

`flow:imbalance_down` n=374 ⊂ `flow:delta_down` n=4263；组合行 n=272 是两者交集；三者的经济含义都是"收盘位置弱 + 放量"（`orderflow_proxy.py` 的 `clv=(close-low)/(high-low)*2-1`）。合并成单一 `flow:clv_weak`（n=4263, t=7.21, +0.68%）是干净的；`imbalance_down` 作为**加强子集**保留在理由里，但不该再当作一条独立层证据参与加权。

### 2.4 跨层重复：两层在给同一件事投票（50.4% 重叠）

- 底链（`extreme_bottom` / `break_up` / `pullback_down` / `second_higher_low` / `bottom_confirmed` 的并集）= 115 只；顶链 = 186 只；**顶/底同时出现 = 0 只**（互斥约束生效，`docs/BOOK-LAYERS.md` 的测试 6 已锁）。
- `flow:delta_down` 命中 280 只，其中 **58 只（底链票的 50.4%）同时命中底链**。端上 `BookSignalEngine.scoreOf()` 与云端 `confluence_score()` 都把各层当独立估计做 `min(|t|,4)/4` 加权平均。两个估计来自同一批"下跌 + 弱收盘"的 bar：**期望点估计不会因此翻倍（加权平均仍是同一均值的两个估计），但有效样本量会接近减半，置信度虚高**。
- 定量提醒：`flow:delta_down`（t=7.21）与 `mtr:break_up`（t=6.61）同时命中时，分数按两者各 1.0 权重平均，得到 (0.68+0.56)/2=0.62%；如果两者相关系数 ρ≈0.5，合并估计的 SE 应放大约 `√((1+ρ)/2)=0.87` 倍 → 实际 t 约 7.2×0.87≈6.3，被当成"两个独立 t≈7 的证据"，置信度虚高约 14%（这只是一阶近似，未做严格推导，见第五节）。

### 2.5 保留独立行 / 合并的判定规则（建议写成测试，不是口号）

只有当 **(a)** 组合行 `n ≤ 0.85 × 单标签 n`（说明组合真的筛出了子集）**且 (b)** 组合行与单标签的平均前向收益差 `> 2 × 合并标准误` 时，才保留独立的一行。按此规则：
- 反转层 12 条组合行：(a) 全部不满足（n 完全相同）→ 删除；
- 涨停层 3 组：(a) **不**满足（352/366=0.96、333/366=0.91，都 > 0.85 —— 组合几乎等于全集，没筛出子集）→ 合并；
- 订单流：(a) 满足（272/374=0.73 ≤ 0.85），但 (b) 不满足（两者收益差 |1.70−1.97|=0.27 < 2×合并 SE ≈ 0.85，其中 SE=√(0.094²+0.414²)=0.425）→ 合并。

---

## 三、权重口径（BOOK_WEIGHT 与 minCanonical 的定量关系）

### 3.1 `BOOK_WEIGHT = 0.6` 是拍的，不是算出来的

代码自己写了它是怎么来的（`scripts/engine_spec.py:56-58`）：

> 「取 0.6 = 规格指标权重区间 `[0.6, 1.5]` 的下界：衍生层信息不如原始指标直接，首次接入不应压过任何一个主指标。」

证据链齐全：
- `engine_spec.py:58` `BOOK_WEIGHT = 0.6`；
- `data/engine_spec.json` → `weights.book = 0.6`，`weight_policy.bounds = [0.6, 1.5]`（`engine_spec.py:548`）；
- 0.6 在 `data/engine_weights.json` 里正好是**降权档**（`BOLL/Keltner: 0.6`「质量45 反向/无效 → 降权」、`Choppiness: 0.6`「质量41 → 降权」）→ 0.6 是从那里借来的"最低档"数字；
- **没有任何 calibration 表参与推导**：`weight_calibrator_v3` 的 `indicators` 只有 17 个技术指标，`weight_calibrator_v2` 写的 `engine_weights.json` 也没有 book 键；book 层从未进过任何校准器（`engine_spec.py:589-600` 的注释明确说"不写进 engine_weights.json，塞一个非技术票进去会破坏它的归一假设"）。

所以它是**叙事值（区间下界）**，不是从数据反推出来的。

### 3.2 用代数 + 现有表反推一个上限（可复算）

**装置**：`CanonicalEngine.kt:66-79` 与 `engine_v5.py:391-403` 同一式：

```
score = 50 + 50 · base · (0.6 + 0.4·resonance),   base = Σ wᵢ·voteᵢ·strengthᵢ / Σ wᵢ
```

**分母 D**（这台机器上实际能被加进去的权重和）：

| 票 | 权重来源 | 值 |
|---|---|---|
| 17 个技术指标 | `spec.weights`：15×1.0 + BOLL/Keltner 0.6 + Choppiness 0.6 | 16.2 |
| `below_ma60` | `spec.weights` **无此键** → `CanonicalEngine.kt:71` fallback 1.0 | 1.0 |
| `flow` / `tape` | 同样无键 → fallback 1.0 各一张（叠加条件见下） | 2.0 |
| `book` | `spec.weights.book` | w |

→ **D ≈ 19.2**（`flow` 或 `below_ma60` 缺席时 18.2；两者都缺席时 17.2；只有 17 个技术指标时 16.2）。

⚠️ 顺带一个口径疑点：`EngineChain.W_FLOW=0.07` / `W_TAPE=0.05` 只在 `blend()` 那一层起作用，**进中枢时 flow/tape 票走的是 `w[indicator] ?: 1.0` = 1.0**，两张代理票在中枢里的实际话语权约等于两个技术指标。这解释了为什么 D 这么大（也解释了 book 票为什么几乎掀不动分数）。

**抬分上界**（book 票只投 +1、不投 0/-1，`EngineChain.kt` `externalVotes()`；strength = (bookScore−50)/50 ∈ [0,1]）：

```
一阶：Δ ≤ 50 · w / (D + w)
w=0.6, D=19.2 → 1.52 分 ;  D=17.2 → 1.69 分
含共振二阶（+1 票把 bull 占比抬高，factor 从 0.6+0.4·res 变大）
  模拟 900+ 组票型的实测最大 → +1.22 分
```

**与 `minCanonical = 55.0`（`LocalScreener.kt:30`）的关系（这就是题目问的那条）**：

| 情形 | book strength | Δ 上界 | 能把多少分顶过 55.0 |
|---|---|---|---|
| 理论最坏（book score = 100，即 exp5 打满 `CLAMP_PCT=3.0`） | 1.00 | **+1.22 分** | 53.78 以上可以 → **53.x 里有 53.78~54.99 这一窄带会被顶进池** |
| 现有 770 只实测（book 最高分 82.8） | 0.656 | **+0.78 分** | 54.22 以上才行 → **当前数据下 53.x 进不了池** |

所以答案是分层的，两个都成立：
- **能**（理论上）：`CLAMP_PCT=3.0` 允许 book score 到 100（strength=1），此时 0.6 的权重足以把 **53.8 分以上**的票顶到 55.0——边界正好是 **53.78**，也就是说"book 分打满 3% 且 canonical 落在 53.8~55.0"的票会被顶进池，而这条边界离 55.0 只有 1.22 分。
- **目前不能**（数据上）：770 只里 book 分最高 82.8，实测抬分 ≤0.78，够不到 53.x→55.0 所需的 2.0（53.0）或 1.0（54.0）。

**反推"保证永不越界"的 w**：要求 `Δ ≤ 55.0 − s_min`，则

```
w ≤ D · Δ_target / (50 − Δ_target)
```

- 目标"54.0 的票不许被顶进池"（Δ_target = 1.0）：D=19.2 → **w ≤ 0.39**；D=17.2 → **w ≤ 0.35**。
- 目标"54.5 不许进"（Δ_target = 0.5）：**w ≤ 0.20 / 0.17**。
- 现状 0.6 **已经越过这条线**——今天没出事，靠的是"book 分最高只到 82.8"这个数据事实，不是靠设计。这是一条应当被测试锁住的东西（见四-E5）。

**反推"让 book 真的起作用"的 w**：把抬分折算成期望收益，`1 分 = 3%/50 = 0.06%`（`score = 50 + 50·exp5/3`）。

| 量 | 数值 |
|---|---|
| w=0.6 实测抬分 | 0.78 分 = **0.047%** 5 日期望 |
| 最弱过检标签的标准误（`mtr:bottom_confirmed`） | 0.305% |
| 比值 | 抬分只有最弱标签 SE 的 **1/6.5** → 统计上不可分辨 |
| 要让 book 抬分 = 它自己最强标签的期望（`flow:delta_down` 0.68% = 11.3 分） | **w ≈ 5.0~5.6**（D=17.2/19.2），**超出 bounds[0.6,1.5] 上界 3.5 倍** |
| 就算取 bounds 上界 1.5 | 3.6 分 = 0.22%，仍只是它最强标签的 1/3 |

**结论（这一节的落点）**：`BOOK_WEIGHT` 处在"既不生效、又刚好可能越界"的双输区间——**加大到 1.5 不足以让它生效，却把越界裕度从 0.22 分吃掉**。所以这不是"调个数字"的问题，是用法问题：book 层要么承认是**理由/贴纸层**（那就把 w 压到保证不可越界，并写死测试），要么换用途（同分排序 / gate 准入条件），不要在"加数"这条路上继续加码。

### 3.3 同一批指标、两套互相矛盾的权重（都在端上跑）

| | `data/engine_weights.json`（v1，2026-10-25） | `data/weight_calibration.json`（v3，2026-10-30） |
|---|---|---|
| 谁写 | `weight_calibrator_v2.py` | `weight_calibrator_v3.py`（`report_only`，`engine_spec.py:544`） |
| 谁读 | `engine_spec.build_spec()` → `spec.weights` → `CanonicalEngine` | 端上 `FactorWeightStrategy.kt:124`（`relWeight`） |
| 口径 | Wilson CI + 质量分，`mean=1.0`、`cap[0.6,1.5]` | `edge` 的 min-max 重标，`0.1 + 0.4·(edge−lo)/rng` |
| 结果 | 16/17 = 1.0，只有 `BOLL/Keltner`、`Choppiness` = 0.6 | `Donchian20` 0.5（最高）、`Choppiness` 0.356（第二）、`RSI` 0.1（最低） |
| 有 t 检验 | 有（CI 下限 > 50% 才动权重） | **没有** |

**两表 16 个共享指标的秩相关 Spearman = −0.059**（等于无关）。具体打脸的两处：
- `Choppiness`：v1 判"质量 41，反向/无效 → 降权 0.6"；v3 里它是**第二高**的 rel_weight 0.356。
- `Donchian20`：v1 等权 1.0；v3 最高 0.5。

也就是说：**同一个指标，中枢引擎说"没差别"，因子策略说"最有差别"**，两边都在同一个 APK 里跑。加上 `ConnorsRSI` 在 v3 里根本不存在却拿 1.0，这一节的结论很硬：**"权重"在本项目里有三个互不兼容的含义**（spec 里的投票权重 / v3 报表里的相对 edge 排序 / 因子策略里的族内权重），必须显式区分命名，否则下一次"调权重"改的是哪一份都说不清。

---

## 四、可验证改法清单（改哪里 / 怎么验 / 怎么回滚 / 预期量级）

按「证据强度」排序：E1 是**可证明等价**的改动（数学上零差异），E5/E8 是**代数上界可证**但需要端上回归，越往后越依赖未验证假设。

> 所有"怎么验"里 E1~E4、E6、E7 的命令**现在就能跑**（只读，不写盘）。E5、E8 涉及 Kotlin/规格，需要跑 Gradle，会写 `build/`（算构建产物，但仍是仓内文件）——请在**没有别的写入者**的窗口执行。
> 基线：`cd ~/workspace/stocks-ai-view && PYTHONDONTWRITEBYTECODE=1 python3 tests/test_book_layers.py` → 13 tests OK（本轮已跑，绿）。

### E1 删掉反转层 12 条数值完全重复的组合键（等价改写，零风险）

- **证据**：`reversal_calibration.json` 26 行 → 14 个独立统计体（见二.1，四元组 `(n, avg_fwd, t, win_rate)` 全等）。
- **改哪里**：`scripts/signal_validator.py:64-70`（`validate()` 里对同 bar 标签做两两组合的那段）。加一条判据：组合键的 `n` 与其中任一单标签的 `n` 相同 → 不写进表（即"组合没有筛出子集，就没有信息增量"）。表本身由 `reversal_engine.py --calibrate` 重写，所以改脚本、不手改 JSON。
- **怎么验**（现在就能跑，证明等价）：
  ```bash
  cd ~/workspace/stocks-ai-view && python3 - <<'PY'
  import json
  T=json.load(open('data/reversal_calibration.json'))['table']
  single=[k for k in T if ' & ' not in k]
  dup=[k for k in T if ' & ' in k and any(T[k]==T[s] for s in single)]
  assert len(dup)==12, dup          # 12 条组合行与某条单标签四项统计完全同值
  print('可删:', dup)
  PY
  ```
  已实测：删掉这 12 条后，770 只快照上 book 分**变化 0 只**、有证据票数 470→470（脚本见二.1 口径）。
- **怎么回滚**：`git diff` 只落在 `signal_validator.py` 一处；回滚 = 撤掉新增判据的 3 行。表的回滚需要重跑 `--calibrate`（写盘，本轮未跑）。
- **预期量级**：分数 0 变化；`basis` 字符串（理由里显示的那条标签名）会从「`mtr:extreme_bottom & mtr:break_up`」变回「`mtr:break_up`」，**界面文案会变、数值不变**。表的行数 26→14。

### E2 把端上 `MIN_T` 从 1.5 提到 2.5（点估计 CI 不含 0 的方向要求 + 多重检验折扣）

- **证据**：116 行同族检验，Bonferroni 门槛 3.52、`|t|≥1.5` 的期望假阳性 15.5 条；端上 17 条里 7 条不过 3.52（表 1-A）。2.5 是 `|t|≥1.96`（单条 CI 不含 0）与 3.52（116 条 FWER）之间的折中，也是"至少让 4 条 `limit:*` 全部出局"的最小整数门槛（最大 `limit` t = 2.33）。
- **改哪里**：`~/workspace/skapp-app/engine-algo/src/main/java/com/skapp/app/data/BookSignalEngine.kt:74` `MIN_T = 1.5` → `2.5`；云端同步 `scripts/signal_hub.py:568`（`layer_expectancy`）、`:601`（`confluence_score`）的 `min_t` 默认值。**注意 `MIN_N` 不要动**（100 与写表门槛一致）。
- **怎么验**（现在就能跑）：
  上面 E1 之外的独立脚本（见本文档附带的阈值扫描，输出四行）给出：`min_t=1.5` → 有证据 470/770、`>=65`（会产 +1 票）38 只；`min_t=2.5` → **462/770、29 只**；`min_t=3.52` 与 2.5 结果相同（说明 2.5~3.52 之间的标签不影响"是否产票"）。
  端上回归：`cd ~/workspace/skapp-app && ./gradlew :engine-algo:test --tests 'com.skapp.app.data.BookSignalEngineTest'`（现有测试里 `BookSignalEngine.kt:525` 的"只出多头证据"断言不受影响）。
- **怎么回滚**：两个常量各改回 `1.5`（云端是默认参数，端上是 `const val`），无状态迁移、无数据格式变化 → 下一次拉 spec/发版即恢复。
- **预期量级**（770 只快照实测）：失去全部证据的票 470→462（−8 只）；**会产 book 票的从 38 降到 29（−24%）**。
  关键细节：掉出去的 9 只 book 分是 **67.7 与 75.8×8**，比留下的 29 只（中位 74.1）**并不弱**——它们全都是 `limit:first_board` 票，即"用 t=1.96 的涨停标签打出 75.8 分（strength 0.52、单票抬分 0.79 分）"的那批。所以这条不是"削掉最弱的"，而是**削掉最不该被相信的那批**；留下的 29 只 strength 区间 0.303~0.657，单票抬分 0.46~0.99 分（D=19.2 一阶），**幅度不变，覆盖率 −24%**。

### E3 订单流层 3 条压成 1 条（`flow:clv_weak`）

- **证据**：`flow:imbalance_down` n=374 ⊂ `flow:delta_down` n=4263；组合 n=272 是交集；三者同源于 `clv=(close-low)/(high-low)*2-1`（`orderflow_proxy.py`）。
- **改哪里**：`scripts/orderflow_proxy.py` 的 `BULLISH_TAGS` 与写表（调用点 `:230`）：把 `imbalance_down` / 组合键从"独立标签"降级为"`delta_down` 的加强子集（只用于理由文本）"；端上 `BookSignalEngine.kt:66-68` 对应删两行 `Calib`。
- **怎么验**：断言 `limit/orderflow` 表里不存在"组合键 n 与单标签 n 差值 < 15% 且 fwd 差 < 2×SE"的行；端上跑 `BookSignalEngineTest`（组合键命中路径仍有覆盖）。
- **怎么回滚**：两处常量/表行恢复（端上是 `CALIB` 列表里加回两行，云端重跑 `--calibrate`）。
- **预期量级**（770 只快照实测，把 `imbalance_down` 与它的组合键从分数里摘掉）：命中它的 **33 只**票里，book 分下降 **中位 8.9 分 / 均值 9.3 分 / 最大 12.7 分**，等价 `exp5` 平均 **−0.56pp**；其中 **9 只**因此失去全部证据（它们只有 `imbalance_down` 一条过检标签）。这是本清单里对分数影响最大的一条——因为它动的是期望值本身，不是权重。

### E4 涨停层 4 条压成 1 条（并接受"合并不解决显著性"）

- **证据**：四行 n = 333 / 352 / 366 / 387，是同一批票的三种切面；770 快照实测 `first_board` 10 只 ⊂ `strong_seal` 11 只 ⊂ `active_5d` 164 只（无一例外）。并集上界 387，相对最小的 333 只涨 16%，`t ∝ √n` 最多 2.33→2.51，仍 < 3.52。
- **改哪里**：`scripts/limit_structure.py` 的 `BULLISH_TAGS` + 写表；端上 `BookSignalEngine.kt:61-64` 四条 `Calib` 合成一条 `limit:board_strong`（n 取并集、fwd 取四个估计的 |t| 加权）。**同时在 `docs/BOOK-LAYERS.md` 里写明"涨停层单条 t<2.6，属于未经多重检验校正的证据，只做理由不做加分"**（否则下次有人又会拿 1.77% 当铁证）。
- **怎么验**：表行数 4→1；用 770 快照重算，命中 `limit:*` 的票 186 只中真正过 `MIN_T=2.5` 的（合并后 t≈2.5 边缘）→ 需实测合并后的 t，**这一步必须重跑 `--calibrate`（写盘，本轮禁跑）**，因此这条列为"待执行"，预期 t ≈ 2.4~2.6。
- **怎么回滚**：`CALIB` 与 `BULLISH_TAGS` 各自可单点恢复；表需重跑。
- **预期量级**：受影响面很小（770 只快照里 `first_board` 10 只 / `strong_seal` 11 只；校准样本里对应 333~387 个切点），但**证据等级**从"4 条独立标签"降为"1 条弱标签"，语义上从"最可靠的层"变成"最不可靠的层"——这是**认知**修正，不是分数修正；分数上唯一的变化是这 10 只在 `MIN_T=2.5` 之后不再产票（见 E2 实测：8 只彻底失去证据）。

### E5 `BOOK_WEIGHT` 收紧到"保证不可越界"，并把这条不变量写成测试

- **证据**：`w ≤ D·Δ/(50−Δ)`；要保证 54.0 的票不被顶进 55.0，需 `w ≤ 0.35`(D=17.2)/`0.39`(D=19.2)；现状 0.6 的上界是 1.52~1.69 分（含二阶 1.22），能把 53.78 的票顶过线（表见三.2）。
- **改哪里**：
  - `scripts/engine_spec.py:58` `BOOK_WEIGHT = 0.6` → `0.35`，并把注释里的"下界叙事"换成公式与算例；
  - 新增常量 `BOOK_MAX_LIFT_PTS = 1.0`（或按 `minCanonical` 推导），在 `build_spec()` 里断言 `50*w/(D+w) <= BOOK_MAX_LIFT`，`D` 由 `weights` 求和 + flow/tape/below_ma60 的 fallback 1.0 显式算出；
  - 端上 `EngineChain.selfCheck()` 加一条：给定 `spec.weights`，book 票最大抬分 < 1.0 且 **不改变 `canonicalScore >= minCanonical` 的判定**（用一个 54.5 分的合成票跑一遍 `blend`）。
- **怎么验**：
  ```bash
  cd ~/workspace/stocks-ai-view && python3 - <<'PY'
  import json
  w=json.load(open('data/engine_spec.json'))['weights']
  D=sum(v for k,v in w.items() if k!='book') + 1.0 + 1.0 + 1.0   # + below_ma60 + flow + tape 的 fallback 1.0
  for wb in (0.35,0.6,1.0,1.5):
      print(f"w={wb}: 一阶抬分上界 {50*wb/(D+wb):.2f} 分 = {50*wb/(D+wb)*0.06:.3f}% 5日期望")
  print("D =", D)
  PY
  ```
  实测输出：`D = 19.2`；`w=0.35 → 0.90 分 / 0.054%`；`w=0.6 → 1.52 分 / 0.091%`；`w=1.0 → 2.48 分`；`w=1.5 → 3.62 分`。
  端上：`./gradlew :engine-algo:test --tests 'com.skapp.app.data.EngineChainSelfCheckTest'`（新断言必须绿）。
- **怎么回滚**：常量一处（0.35 ↔ 0.6），且**这个参数本来就走云端 spec 下发**（`EngineChain.kt:37` `bookWeightOf()`），回滚不需要发版——这本身就是选它当第一个可调旋钮的理由。
- **预期量级**：抬分上界 1.22 → 0.72 分（一阶 1.52→0.90，二阶按 0.35/0.6 线性折算 ≈0.72，未单独模拟），即 **−0.03% 期望收益**；对"是否入池"的影响：**0 只票改变**（0.6 和 0.35 都推不动 53.x），所以这条是**买保险，不是改表现**——保险的价格是 0。

### E6 把三套"权重"改名分开，堵住下一次误调

- **证据**：`engine_weights.json`（spec 生效权重，16/17=1.0）与 `weight_calibration.json` 的 `rel_weight`（0.1~0.5，无 t、全负 edge）秩相关 **−0.059**；`Choppiness` 在两份里一个被判降权、一个是第二高；`Donchian20` 一个等权、一个最高。
- **改哪里**：`scripts/weight_calibrator_v3.py:120` 把 `rel_weight` 改名为 `rel_edge_rank`（并在 `out` 里加 `"applied": false`）；`~/workspace/skapp-app/engine-algo/src/main/java/com/skapp/app/data/FactorWeightStrategy.kt:124` 读的键名同步改，同时在注释里写明"这是 edge 排序，不是 spec 权重，不进中枢"；`engine_spec.py:544` 的 `report_only` 段落补一句"v3 输出不参与 spec.weights"。
- **怎么验**：`grep -rn "rel_weight" scripts/ engine-algo/src/main/java/` 应只剩一处生成点 + 一处读取点且都带 `rel_edge_rank`；再跑一次 Spearman（本文档记录值 −0.059 作为基线，改动后必须还是 −0.059，说明只是改名没改数）。
- **怎么回滚**：改名类改动，回滚 = 反向改名；`applied: false` 字段可留（纯元数据）。
- **预期量级**：**分数 0 变化**（当前 `rel_weight` 只用于 `FactorWeightStrategy` 的族内权重，改的是键名与注释）。风险是同时要改端上两处，**必须同一个 commit**，否则 `optDouble("rel_weight", 0.0)` 缺键会退化成 0 权重（`FactorWeightStrategy.kt:124` 的默认值就是 0.0）——这是这条唯一的真实风险点，务必先跑因子策略的测试。

### E7 `ConnorsRSI`：补校准，或把 1.0 降到 0.6

- **证据**：`grep -c ConnorsRSI data/weight_calibration.json` = **0**，但 `data/engine_weights.json:29` 是 `1.0`（理由字段写"质量50 噪声区 → 等权"，n=27313）。它是 17 个指标里唯一"最新校准零记录、却拿满权重"的。
- **改哪里**：优先**补**——`weight_calibrator_v2/v3` 的输入 `reflect_latest.json.by_indicator` 里缺这一项，需要查为什么（下一轮）；临时处置：`weight_calibrator_v2.py` 加一条"若指标在最新 v3 报表中缺席，权重上限 0.6"。
- **怎么验**：脚本断言 `engine_weights.json` 的每个键都能在 `weight_calibration.json` 找到记录（缺失即 fail），并把缺失名单打进 `meta`。
- **怎么回滚**：单条权重值。
- **预期量级**：`ConnorsRSI` 权重 1.0→0.6 使 D 减少 0.4（19.2→18.8），对 canonical 分数的影响 < 0.05%（该票只在极超卖/超买才非中性）。**量级极小，价值在"消灭一个无证据的全权重"**。

### E8 端云"多层期望"对齐：层集合 + `CALIB` 同步，写成测试

- **证据**：端上 `BookSignalEngine` 只用 3 层（reversal/limit/orderflow，17 条标签）；云端 `signal_hub.confluence_score` 用 **5 层**（`_LAYER_FILES` 含 structure/wyckoff，79 条被门槛放行、其中 10 条是负向标签）。同一只票两端"书籍层分数"不同源；端上还有一条断言"校准表不应含负向标签"（`BookSignalEngine.kt:525`）——与云端把负向标签当负贡献用直接冲突。
- **改哪里**：`scripts/signal_hub.py` `confluence_score()` 增加 `layers=("reversal","limit","orderflow")` 参数（默认 3 层，与端上一致），5 层版本改名 `confluence_score_all()` 并在返回里带 `layers` 字段；端上 `BookSignalEngine.kt:525` 的断言改为"三层内不出负向标签；structure/wyckoff 的负向标签只允许出现在云端 5 层模式"。
- **怎么验**：
  ```bash
  cd ~/workspace/stocks-ai-view && python3 - <<'PY'
  import json
  T=lambda f: json.load(open('data/'+f))['table']
  rev,lim,ofl,st,wy=[T(f) for f in ('reversal_calibration.json','limit_calibration.json',
      'orderflow_calibration.json','structure_calibration.json','wyckoff_calibration.json')]
  def fwd(v): return v.get('avg_fwd', v.get('avg_fwd_5d'))
  used=[(n,k,v) for n,t in (('rev',rev),('lim',lim),('ofl',ofl),('st',st),('wy',wy))
        for k,v in t.items() if v['n']>=100 and abs(v['t'])>=1.5]
  print("云端放行总数:",len(used),"其中负向:",sum(1 for _,k,v in used if fwd(v)<0))
  print("端上 CALIB 覆盖:",sum(1 for _,k,v in used if k in {
   'mtr:second_higher_low','mtr:bottom_confirmed','mtr:extreme_bottom','mtr:pullback_down','mtr:break_up',
   'mtr:top_confirmed','mtr:break_down','mtr:pullback_up','mtr:extreme_top','mtr:second_lower_high',
   'limit:first_board & limit:active_5d','limit:strong_seal & limit:active_5d','limit:first_board',
   'limit:first_board & limit:strong_seal','flow:imbalance_down','flow:delta_down & flow:imbalance_down',
   'flow:delta_down'}),"/ 17")
  PY
  ```
  期望：`云端放行 79，其中负向 10，端上覆盖 17/17`。
- **怎么回滚**：新增参数默认值即旧行为（3 层）；`confluence_score_all()` 是新增函数，删掉即回滚。
- **预期量级**：端上 0 变化；云端若有人调 `confluence_score()`，分数会**变**（从 5 层平均变 3 层平均）——本轮已核 `data/` 下没有任何产物由它直接写出（它只在被调用时算），所以影响面是"未来调用方"，不是存量数据。

---

## 五、未验证项

以下是本文档**没有**证明、但结论依赖它们的地方。按"会不会翻结论"排序：

1. **合并后的真实 t 值未算**。E3/E4 的"预期量级"里，凡是涉及"合并后 t≈2.4~2.6"的地方，都需要重跑 `reversal_engine.py --calibrate` / `limit_structure.py --calibrate`（**写盘，本轮禁跑**）。现在给的只是 `t ∝ √n` 的上界推算，不是实测值。
2. **端上真实 `canonicalScore` 分布没取到**。三.2 的 Δ 是"代数上界 + 770 只 book 分实测"两头夹出来的；"54.22 以上会被顶过线"这句话里，**没有一只真机票的 canonicalScore 被实际测量过**。要闭环需要端上导出一批 `canonicalScore ∈ [52,56]` 的票，做 book 票开/关的 A/B。
3. **`flow` / `tape` 票在中枢里的权重 = 1.0，是读码得出的**（`spec.weights` 无这两个键 → `CanonicalEngine.kt:71` fallback）。若某条路径（如未来 spec 生成时补上 `flow`/`tape` 键）改变了这一点，D 会变小、Δ 上界会变大，E5 的阈值要重算。
4. **跨层相关系数 ρ 没有实测**。二.4 里"置信度虚高约 14%"是取 ρ=0.5 的示例演算，**不是结论**；要真值需要 770 只票的逐日/逐切点前向收益序列做相关。
5. **交易成本未计入**。所有 `avg_fwd` 都是毛收益。5 日双边成本约 0.15~0.3%，会把 `mtr:second_lower_high`(+0.28%)、`mtr:extreme_top`(+0.35%)、`wyckoff:stage_accumulating`(+0.11%) 吃掉一半以上，甚至吃掉 `flow:delta_down`(+0.68%) 的 1/3。**本清单里没有任何一条 edge 是净收益**，因此"这条信号有没有用"在扣费前不能被判定为成立。
6. **端云判据是否逐字同源，只比了"层集合与门槛"，没做逐票 diff**。需要端上导出 `BookSignalEngine.Result`（score/basis/layers）与云端 `book_signal_context()` 对同一批 770 只票做逐只比对。
7. **ST 涨停误判未评估影响面**。`limit_signals.json` 的 `known_limits.st_stock` 自曝：ST 涨跌幅 5% 无法从代码判断，可能把 ST 涨停当普通涨幅。ST 名单没有接入，`limit` 层的 n 与 edge 里含多少 ST 样本未知。
8. **`market_advice: "avoid_first"` 有没有消费方没查全**。本轮只 `grep -rn market_advice scripts/`（只命中生成处），没有查端上 Kotlin 与前端 JS。
9. **`ConnorsRSI` 为什么从 v3 报表缺席没查根因**（E7 只给了临时处置）。可能是 `reflect_latest.json` 的 `by_indicator` 缺项，也可能是 v3 的 `min_n=5000` 之外的另一个过滤条件；未验证。
10. **`wyckoff:stage_accumulating` 的 `t=1.50` 浮点压线稳定性未知**。`abs(1.5) >= 1.5` 在 Python 里为真，但如果上游是把 `t` 先 `round(2)` 再比、或换了浮点运算路径，这条会翻转成"被过滤"。
11. **没有样本外（OOS）证据**。五张表都是同一套切点（200 只 × 400 根日 K、`cut_step=2`、`horizon=5`）算出来的，**没有任何一张做过时间序列切分的 OOS 检验**（`docs/BOOK-LAYERS.md` 也未记录 OOS 结果）。这是"每一条都可能是噪声"的根本原因，也是第一节的"期望假阳性 15.5 条"之所以要写出来的原因。
12. **只有 `horizon=5`**。book 分、`confluence_score`、`CLAMP_PCT=3.0` 全部锚在 5 日；10 日/20 日持有期完全没有校准数据，`docs/BOOK-LAYERS.md` 的判据在别的持有期是否成立未知。
13. **`spec_version` 漂移未核**。`data/engine_spec.json` 是 `3.1.1`，端上 `EngineSpecLoader.loadBundled()` 有 `drift` 检测机制，本轮没有核对端上内置规格的版本号是否一致（需要端上产物或跑 Gradle 测试）。
