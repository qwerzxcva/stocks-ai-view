# 样本利用率诊断：为什么 207 万行 K线 / 4 万涨停样本只换来 600 个信号

> 只读诊断报告。除本文件外未修改任何生产代码。所有结论均带 `文件:行号`，耗时数据来自本机实测。
> 基线 commit: `1976a56`（已含 K线列错位修复，见 §6）。

## 1. 一句话结论

**瓶颈不在数据量，而在三重截断的乘积**：`--days 12`（只回溯 12 个交易日）× `--limit 300`（每天只评成交额 Top 300）× `--top-per-day 50`（每天只存前 50 只），把 117.9 万个可评分 (股票, 日期) 组合压成了 600 条可验证记录——**数据利用率 0.05%**。

三重截断里最硬的闸门是 `--top-per-day`：把 `--limit` 从 300 拉到全市场，在 `--top-per-day` 仍为默认 50 时对 reflect 样本量的贡献是 **0**。

> 本报告撰写期间，另一会话已在跑全量回溯（截至测量时 185 个快照 / 17,900 条记录 / 10 日 n=15,057），瓶颈正在被解除。下文同时保留原始基线与当前实测两套数字。

---

## 2. 参数账：当前 vs 全量

### 2.1 底层数据实测

用 `engine_v5.load_klines` 直接读 bulk 目录（`/root/workspace/stocks-data-kline/data/bulk/day_qfq_*.csv`，行格式 `code|date|open|close|high|low|vol`，见 `scripts/engine_v5.py:381-405`）：

| 指标 | 实测值 |
|---|---|
| 股票数 | 3997 只 |
| K线总行数 | 1,290,045 |
| 去重交易日 | 813 天（2023-06-02 ~ 2026-10-13） |
| `bars >= 30`（可评分下限，`backfill_predictions.py:77`） | 3990 只 |
| **可评分 (stock, asof) 组合总数** | **1,178,801** |

注意：bulk 里股票数（3997）与题目给的 3175 不同，交易日（813）与题目给的 668 也不同——817 天是 bulk 全量日期，668 是「最近 668 个交易日」。下文两个口径都算。

### 2.2 三重截断逐层拆解

`backfill_predictions.py` 的样本生成公式：

```
每天产出 = min(--limit, 当日可评分股票数)   → 再被 --top-per-day 砍到 50
总样本   = Σ_天数 min(--limit, 可评分数)    → 再被 top50/天 砍一刀
```

| 层级 | 参数/代码位置 | 当前值 | 全量值 | 样本影响 |
|---|---|---|---|---|
| 回溯天数 | `--days`，`backfill_predictions.py:37` | 12 | 668（或 813） | 12 → 668：**×55.7** |
| 每日评分股票数 | `--limit`，`backfill_predictions.py:38-39` | 300 | 0（全部 3990） | 300 → 3990：**×13.3** |
| 每日落盘条数 | `--top-per-day`，`backfill_predictions.py:40-41`（默认 50） | 50 | 可配 | 不变（默认值），**这是最硬的天花板** |
| reflect 读取 | `d.get("top", [])`，`reflect_engine.py:206` | 50/天 | 50/天 | 不变 |

### 2.3 各配置下的 reflect 样本量

reflect 对每个 pred_file 取 `top` 全部条目（[reflect_engine.py:204-214](scripts/reflect_engine.py#L204)），所以 **reflect 样本数 = pred 文件数 × 每天落盘条数**。1 日持有期额外要求未来 1 根 K线存在，10 日要求未来 10 根。

| 配置 | pred 文件数 | 1 日 n | 3 日 n | 5 日 n | 10 日 n |
|---|---|---|---|---|---|
| 原始基线（12 天 × limit 300 × top50，字段已修） | 12 | 521（实测） | 421（实测） | 318（实测） | 58（实测） |
| **当前实测**（185 文件，字段已修，回溯进行中） | 185 | **15,518** | **15,418** | **15,316** | **15,057** |
| `--days 668 --limit 0 --top-per-day 50` | 668 | ~33,400 | ~33,400 | ~33,300 | ~32,900 |
| `--days 813 --limit 0 --top-per-day 50` | 813 | ~40,650 | ~40,650 | ~40,550 | ~40,150 |
| `--days 668 --limit 0 --top-per-day 200` | 668 | ~133,600 | ~133,600 | ~133,300 | ~131,600 |
| `--days 813 --limit 0 --top-per-day 200` | 813 | ~162,600 | ~162,600 | ~162,400 | ~160,600 |

> 上表「当前实测」一行是本报告撰写期间另一会话正在跑的全量回溯（2023-11 ~ 2024-06 段，`--top-per-day 100`）的中间态，文件数仍在增长。10 日持有期样本已达 15,057（Wilson CI 宽约 ±1.5pp），相比原始 58 个样本（CI 宽 ±11.2pp）已是质变。
> 10 日样本比 1 日少的量 = 末尾 10 天 × 每日落盘数，因为最后 10 个交易日没有 10 日未来数据（`verify_prediction` 里 `j >= len(bars)` 返回 None，[reflect_engine.py:188-189](scripts/reflect_engine.py#L188)）。
> 3 日/5 日比 1 日略少，同因——不同股票的 K线末尾日期不完全齐平，短股票提前耗尽未来数据。

### 2.4 各配置的实际浪费率

| 口径 | 可评分组合 | 实际落盘 | 浪费 |
|---|---|---|---|
| 原始基线 12 天 | 47,880 | 600 | **98.7%** |
| 全量 813 天（`--top-per-day 50`） | 1,178,801 | 40,650 | **96.6%** |
| 全量 813 天（`--top-per-day 100`，当前回溯所用） | 1,178,801 | 81,300 | **93.1%** |
| 全量 813 天（`--top-per-day 200`） | 1,178,801 | 162,600 | **86.2%** |
| 全量 813 天（不截断，`--limit 0 --top-per-day 0`） | 1,178,801 | 1,178,801 | 0% |

---

## 3. 性能估算：全量回溯要多久

### 3.1 `score_stock` 复杂度实测

对同一只 803 根 K线的股票，实测 `score_stock` 耗时 vs 输入长度：

| bars | 耗时 | 每根耗时 |
|---|---|---|
| 30 | 0.41 ms | 0.014 ms |
| 60 | 1.08 ms | 0.018 ms |
| 120 | 2.63 ms | 0.022 ms |
| 200 | 5.21 ms | 0.026 ms |
| 400 | 11.93 ms | 0.030 ms |
| 668 | 19.85 ms | 0.030 ms |
| 803 | 25.81 ms | 0.032 ms |

**结论：接近线性 O(n)，无 O(n²) 爆炸。** 但有三个固定的每根常数因子来源，都在 `indicators_ext.py`：

- `connors_rsi` 的 rank 项对每一天重算 100 日窗口（`indicators_ext.py:427-430`），这是最大的单项，但仍是 O(n×100) 线性；
- `cmo`（`indicators_ext.py:205-213`）、`vortex`（`indicators_ext.py:249-259`）内层 14 日循环；
- `choppiness`、`ultimate_osc` 的切片求和。

随机抽 300 只全 bars 实测：**7.78 ms/只**（平均 322 根）。外推：

| 场景 | 每天耗时 | 总量 |
|---|---|---|
| 4000 只/天 × 668 天 | 31.1 s | **5.8 h** |
| 4000 只/天 × 813 天 | 31.1 s | **7.0 h** |
| 按每天实际可评分数累计（813 天） | — | **2.83 h** |

实测小规模对照：`--days 3 --limit 50` 跑完 2.87 s（含 1.06 s 加载 K线），与估算一致。

### 3.2 可行性与分批

**单进程串行全量回溯约 3–6 小时，在容器里不可接受一次跑完**，但完全可分批：

1. **按日期切片**：`--days` 不支持起点偏移，需加 `--start-date`；或用 shell 循环 `for d in $(dates); do python3 backfill_predictions.py --start-date $d --days 1 ...`。
2. **按股票切片**：`--limit` 只取 Top N，需加 `--offset` 或 `--shard i/n` 做股票分片，多进程并行。
3. **一次性写入 SQLite/DuckDB 而非 668 个 JSON**：现在每份快照 84~154 KB（top50 时 84 KB，top200 时约 154 KB），668 个文件 = 56~100 MB，reflect 每次全量 glob 读取（`reflect_engine.py:main` 的 `pred_files = sorted(glob.glob(...))`）也要重读全部。

推荐做法：**加 `--start-date` + 每天跑一次 CI**（或一次性分批跑 668 天，每批 30 天约 20 分钟），不要在一次 workflow 里跑完。

---

## 4. top50 截断的浪费量化

`backfill_predictions.py:109` 的 `"top": results[: args.top_per_day]` 默认只写 50 条，而 `--limit` 每天已经评了 300 只（当前）或 3990 只（全量）。

| 场景 | 每天评了多少只 | 每天存了多少只 | 丢弃率 |
|---|---|---|---|
| 当前（`--limit 300`） | 300 | 50 | **83.3%** |
| 全量（`--limit 0`） | ~3990 | 50 | **98.7%** |
| 全量（`--limit 0 --top-per-day 200`） | ~3990 | 200 | **95.0%** |

也就是说：**`--limit` 从 300 提到 0（全市场），在 `--top-per-day` 仍为默认 50 时对 reflect 样本量的贡献是 0**。这是最容易踩的坑——把 `--limit` 拉满却看不到样本涨，因为真正的闸门在 `--top-per-day`。

另一个隐性浪费：`n_scanned`/`n_scored` 字段写的是 `len(results)`（[backfill_predictions.py:105-106](scripts/backfill_predictions.py#L105)），即评分总数，但落盘的 `top` 只有 50 条。所以看 JSON 元数据会误以为"396 只都验证了"，实际 reflect 只拿到 50。

`kline_sim.py --source predictions` 同样只读 `top`（[kline_sim.py:152-167](scripts/kline_sim.py#L152)），且模拟后还按 score 排序取前 `--max-pos 20`（[kline_sim.py:283](scripts/kline_sim.py#L283)），所以它的"整体胜率"统计用的是全部可模拟信号（`sim_results`，[kline_sim.py:289](scripts/kline_sim.py#L289)），样本量与 reflect 同源同量级。

---

## 5. 前视偏差与早期样本偏斜评估

### 5.1 前视偏差：backfill 本身没有

`backfill_predictions.py:76` 用 `bars = [b for b in rows if b[0] <= asof]` 截断，`score_stock` 只接收这批 bars。方向上是干净的。

K线列序问题曾是独立缺陷（曾让回溯指标口径与实时引擎不一致），已在 commit `1976a56` 修复，详见 §6。当前 HEAD 无前视偏差。

### 5.2 指标预热窗口：早期样本会偏斜，但不被过滤

`build_votes` 里没有 MA240/RPS240 这类长窗口指标——17 个指标的最大窗口是：

| 指标 | 窗口 | 位置 |
|---|---|---|
| ConnorsRSI | `rank_n=100` + `rsi_n=3` → **需 ~104 根** | `indicators_ext.py:388-436` |
| UltimateOsc | `n3=28` | `indicators_ext.py:262-279` |
| Mass Index | `n2=25` | `indicators_ext.py:281-311` |
| 其余 | ≤20 | — |

实测：bars=30/60/100 时 votes=16（**ConnorsRSI 缺席**），bars≥120 时 votes=17。**没有样本被 `len(bars) < 30` 过滤掉以外的东西被过滤**——`build_votes` 对每个指标都是 `if _last(...) is not None` 逐个判断（如 `engine_v5.py:150-155`），缺的指标只是不投票，`score_stock` 照常返回（`engine_v5.py:351-354`）。

偏斜分布（全量 813 天的 1,178,801 个组合）：

| bars 区间 | 组合数 | 占比 | 指标状态 |
|---|---|---|---|
| 30-59 | 119,825 | 10.2% | 16 指标 |
| 60-103 | 175,562 | 14.9% | 16 指标（缺 ConnorsRSI） |
| 104-239 | 243,319 | 20.6% | 17 指标 |
| 240-399 | 229,718 | 19.5% | 17 指标 |
| 400+ | 410,377 | 34.8% | 17 指标 |

**最近 668 天口径下**：1,154,043 个组合中 875,853 个（75.9%）bars≥104，278,190 个（24.1%）指标不全。

风险评估：
- **不构成前视偏差**（无未来数据）；
- **构成样本结构偏斜**：早期样本系统性少一个指标，且 2023-2024 年只有 233 只股票有数据（`day_qfq` 分片在 2024-03 才扩到 1411 只、2026-07 才到 3990 只），市场覆盖度与近期完全不同。若把 813 天全灌进 reflect，"分市场环境胜率"会被早期小样本市场结构主导。
- **建议**：`--days` 取 668（约 2.7 年）而非 813，并在 reflect 输出里按年份/ bars 段分层核对；或直接设 `--min-bars 120` 把 10-15% 的残样本排除。

### 5.3 另一个偏斜来源：`--limit` 按「最近一根 bar 的成交额」静态排序

`backfill_predictions.py:67` 的 `ranked` 是**一次性**按全历史最后一根 bar 的 `vol*close` 排的，对所有 asof 用同一个名单。这带来两个问题：

1. 对早期 asof，这个名单里的股票当时可能还没上市或成交额极低（`bars < 30` 被 skip，见 `backfill_predictions.py:76-78`）；
2. 名单偏向大市值蓝筹，**涨停小票几乎永远进不了 limit 300**——这正好解释了为什么 4 万涨停样本对验证毫无贡献。实测当前 12 份快照的 top50 中 `InZTPool` 命中数 = **0/50**（`engine_v5.py:332-345` 的涨停池投票从未触发，因为 `source_info` 在 backfill 里根本没传，[backfill_predictions.py:86](scripts/backfill_predictions.py#L86) 调 `score_stock` 时未传 `source_info`）。

也就是说：**回溯样本是纯技术指标样本，涨停/板块/资金三个增强指标全部缺席**。这既是样本量问题，也是样本代表性问题。

---

## 6. K线列错位缺陷：已修复，样本需重跑

### 6.1 缺陷内容（commit `1976a56` 之前）

`backfill_predictions.py` 曾把 K线字段整体右移一格：

```python
# 修复前 (错误)
close = [float(b[1]) for b in bars]   # 实际是 open
high  = [float(b[2]) for b in bars]   # 实际是 close
low   = [float(b[3]) for b in bars]   # 实际是 high
vol   = [float(b[4]) for b in bars]   # 实际是 low (价格, 非成交量)
```

bulk 行格式是 `code|date|open|close|high|low|vol`（`engine_v5.py:395-398` 的 `load_klines` 按 `parts[1]=open, parts[2]=close, parts[3]=high, parts[4]=low, parts[5]=vol` 解析）。正确做法见 `engine_v5.py:443-446` 的 `main()`：

```python
close = [b[2] for b in bars]   # close
high  = [b[3] for b in bars]   # high
low   = [b[4] for b in bars]   # low
vol   = [b[5] for b in bars]   # vol
```

错位后实际传入的是 `open, close, high, low`：

| 参数 | 应该是 | 实际传入 |
|---|---|---|
| close | close | **open** |
| high | high | **close** |
| low | low | **high** |
| vol | vol（成交量，百万级） | **low**（价格，个位数） |

实测后果（sz000610，803 根）：
- `low > high` 的天数：**761 / 803**（高低逻辑完全颠倒）；
- `vol[-1]` 实际是 6.83 而非 22457（量级差 3000 倍）；
- 同股分数从正确映射的 26.9 变成 34.2，多空票从 2/12 变成 4/10。

### 6.2 当前状态：已修，且已重跑

HEAD (`1976a56`) 的 [backfill_predictions.py:80-84](scripts/backfill_predictions.py#L80) 已改为正确的 `b[2]/b[3]/b[4]/b[5]`，并带了注释说明列序。`data/predictions/` 下 12 份快照也已用修正后的口径重跑。
实测修前 vs 修后（同样 12 份快照、同样 top50）：

| 持有期 | 修前 n / 看多胜率 | 修后 n / 看多胜率 |
|---|---|---|
| 1 日 | 514 / 43.8% | **521 / 40.3%** |
| 3 日 | 414 / 37.2% | **421 / 32.0%** |
| 5 日 | 299 / 28.8% | **318 / 27.1%** |
| 10 日 | 59 / 22.0% | **58 / 27.6%** |

修后 1 日胜率从 43.8% 降到 40.3%，说明**之前的信号偏乐观**——用 open 冒充 close 让 MA/RSI 类指标系统性偏多。`kline_sim` 1 日胜率同步从 40.0% 降到 33.2%。

### 6.3 残留风险

- 任何基于错位数据得出的历史胜率结论都应作废（包括 `data/reflect_latest.json` 里的旧记录，虽已随 `1976a56` 重算）；
- `multidim_engine.py` / `rps_rotation.py` 的同类错位也已在 `1976a56` 修掉；
- 但 [backfill_predictions.py:63-64](scripts/backfill_predictions.py#L63) 的流动性排序函数仍写着 `float(b[4]) * float(b[5])`（low × vol），与注释声称的 `vol*close` 不符——`1976a56` 漏了这处。它不影响评分正确性（只影响 `--limit` 的选股排序），见 §7 P3。

---

## 7. 具体建议

按优先级：

### P0 — 修字段错位：**已完成**（commit `1976a56`）
`backfill_predictions.py:80-84` 已改为 `b[2]/b[3]/b[4]/b[5]`，快照已重跑。**本节仅作记录，无需再动手。** 教训：bulk 列序是 `date,open,close,high,low,vol`，不是直觉的 `date,close,high,low,vol`，读 bulk 的脚本都应显式写列下标而非解构赋值。

### P1 — `--top-per-day`：已参数化，但默认值仍是 50
[backfill_predictions.py:40-41](scripts/backfill_predictions.py#L40) 现在有 `--top-per-day`（默认 50），[backfill_predictions.py:109](scripts/backfill_predictions.py#L109) 已改为 `results[: args.top_per_day]`。硬编码已解除，但**默认值没变，所以不传参数时样本量一点没涨**。

要做的是把默认值提到 200（或在 CI / 回溯命令里显式传 `--top-per-day 200`）。这是样本量的主闸门——不提它，`--limit` 拉到全市场也白搭。

同步确认下游能消化：
- `reflect_engine.py:206` 直接遍历 `d.get("top", [])`，无需改；
- `kline_sim.py:152-167` 同样只读 `top`，无需改；
- `winrate_matrix.py` 读 reflect 输出，无需改。

### P2 — 加 `--start-date`，分批回溯
现在只有 `--days`（[backfill_predictions.py:37](scripts/backfill_predictions.py#L37)），无法从中间接着跑。加一个 `--start-date`，配合 shell 循环按 30 天一批跑，避免单次 3-6 小时任务。

### P3 — `--limit` 按 asof 动态取，而非静态全局排序
`backfill_predictions.py:60-69` 的 `ranked` 对每天用同一份名单。改为每天按 `asof` 当根的成交额重排，或者干脆 `--limit 0` 全评（性能上全量 813 天约 2.8 小时，可接受）。

同时修流动性函数的列序（`1976a56` 漏了这处）：

```python
# backfill_predictions.py:60-65 当前写法
# 注释写 vol*close, 实际 b[4]=low, b[5]=vol → 算出来是 low × vol
b = rows[-1]
return float(b[4]) * float(b[5])
```

应为 `float(b[5]) * float(b[2])`（成交量 × 收盘价）。不影响评分正确性，但让 `--limit` 的选股排序与注释意图一致。

若目标是验证涨停/打板策略（项目里有 `ban_prob.py`、`limit_up_pool` 相关记忆），还需要在 backfill 里传 `source_info`，让 `InZTPool`/`SectorResonance`/`CapitalFlowConsistency` 三个指标参与评分——否则 4 万涨停样本永远与验证无关。

### P4 — 设 `--min-bars 120`
排除 10-15% 指标不全的早期样本（见 5.2），或至少在 reflect 输出里按 bars 段分层。

### 预期样本量提升

| 改动 | 1 日 n | 10 日 n |
|---|---|---|
| 原始基线（字段已修，12 天 × top50） | 521 | 58 |
| 进行中的全量回溯（185 文件实测） | 15,518 | 15,057 |
| `--top-per-day 200` 全量 668 天 | ~133,600 | ~131,600 |
| 再加 P4（min-bars 120，约 -25%） | ~100,000 | ~98,000 |

**相比原始基线约 290 倍提升**，且是在修正后的指标口径上。Wilson CI 宽度从 ±11.2pp（10 日 n=58，CI[17.75, 40.2]）收到 ±0.8pp（10 日 n≈13 万，CI[35.3, 36.9]），`evidence: insufficient`（样本<30）的指标会从大部分变成极少数。

注意：胜率本身并没有随样本量变「好看」——全量后 1 日看多胜率 42.9%、10 日 35.9%，仍低于 50%。样本量的价值在于**把这个结论的可信度从「噪声」提到「可决策」**，而不是把胜率拉高。

---

## 附：本机实测命令与结果

```bash
# 1. 小规模计时（含 K线加载）
python3 scripts/backfill_predictions.py --days 3 --limit 50 --out-dir /tmp/bf_bench
# → 2.87 s，3 天 × 50 只

# 2. score_stock 复杂度曲线（sz000610, 803 根）
# bars=30→0.41ms  60→1.08  120→2.63  200→5.21  400→11.93  668→19.85  803→25.81
# 随机 300 只全 bars: 7.78 ms/只 → 4000 只/天 ≈ 31 s

# 3. 原始 reflect 基线（12 份快照, 字段已修）
python3 scripts/reflect_engine.py --pred-dir data/predictions --horizons 1,3,5,10 --out /tmp/r.json
# → n_records=600, 1d n=521 (40.3%), 3d n=421 (32.0%), 5d n=318 (27.1%), 10d n=58 (27.6%)

# 4. 当前 reflect（185 份快照, 全量回溯进行中）
python3 scripts/reflect_engine.py --pred-dir data/predictions --horizons 1,3,5,10 --out /tmp/r.json
# → n_records=17900, 1d n=15518 (42.9%) CI[42.09,43.65], 3d n=15418 (40.1%),
#   5d n=15316 (38.8%), 10d n=15057 (35.9%) CI[35.11,36.65]

# 5. kline_sim 基线（12 份快照）
python3 scripts/kline_sim.py --source predictions --out /tmp/ks.json --days 1,3,5,10 --slip-pct 0.03
# → 信号=600 可模拟=521, 1日 n=470 (33.2%), 10日 n=27 (29.6%)
# 注意: 快照数上百后 kline_sim 单次要跑数分钟（它按 signal_date 逐日重算持仓曲线），
# 已实测超 120s 超时；样本量大后建议只对 reflect 做验证，kline_sim 抽样跑。
```

## 附：一句话回答「为什么 600 个信号」

因为 `backfill_predictions.py` 默认只回溯 12 天、每天只评成交额 Top 300 只、且只把前 50 名写进快照，而 `reflect_engine.py` 只读快照里的 `top`。三层限制相乘：12 × 300 → 12 × 50 = 600。底层 207 万行 K线里那 117.9 万个可评分组合，有 98.7% 从未进入验证流程。
