# 指标预计算基础设施（_precompute.py）设计与验证

日期：2026-10-24 ｜ 作者：执行子代理（性能瓶颈任务）

## 1. 问题

策略统一接口 `evaluate(code, kline, context)` 为「传 K 线内部算指标」：每次调用对传入序列重算全套
EMA/KDJ/RSI/BOLL/MACD/量比等（`_indicators.py` 各函数均为 O(n) 全序列计算）。逐时点回测每时点截断
`kline[:i+1]` 再 evaluate → 单股单策略 O(n²)；全市场 3200 只 × 数百时点 × 10 策略 = 几十万次全序列
重算，实测 120s 跑不完 300 只×174 bear 天×3 策略。

## 2. 设计（scripts/engines/strategies/_precompute.py，纯增量层，不改策略本体）

- **`precompute_indicators(kline) -> Indicators`**：每股 O(n) 一次算完全部指标序列。覆盖 grep
  s01-s10 实测的全部用法：ema3/5/10/20/60、kdj_k/d/j(9,3,3)、rsi6、boll_mid/up/low(20,2.0)、
  macd_dif/dea/hist(12,26,9)、vol_ratio5/20、bias5、pct_chg、limit_up_main/star/bse。
  键名规范：`<func><period>`，多输出用 `<func>_<field>`。
- **`Indicators` 视图**（`__slots__`）：`at(i)` O(1) 取第 i 根时点全部指标 dict；`get(name,i)` 单值；
  `raw(name)` 取整条序列（兼容策略内窗口切片逻辑，如 s04 `max(high[i-20:i])`）。
- **`PrecomputeCache`**：回测驱动层 `{code: Indicators}` 缓存，(首日期,末日期,根数) 指纹失效。
- **`evaluate_precomputed(mod, code, kline, precomp, i, context)` 适配器**：策略若实现可选的
  `evaluate_with_indicators(code, kline, i, ind_view, context)` 走快速路径；否则回退
  `evaluate(code, kline[:i+1], context)`，与现状逐位等价 → 10 个策略零改动即可接入，提速收益
  取决于策略侧逐步迁移。

## 3. 无前视与数值一致性论证

`_indicators` 中 EMA（前 period 个 SMA 为种子）、KDJ（K/D 从 50 起步递推）、RSI（Wilder 种子）
均为**前缀递推**：全序列第 i 个值 == 截断序列 [:i+1] 的末值；BOLL/SMA/量比/乖离/涨跌幅为纯历史
窗口。故预计算取值与每时点截断重算严格相等，不引入前视。内置 `verify_consistency()` 抽样逐位
比对（容差 1e-12）。

## 4. 实测验证（2026-10-24，本仓数据）

| 验证 | 结果 |
|---|---|
| 一致性：600519（806 根，55 检查点×11 指标） | mismatches=0 → PASS |
| 一致性：000001（808 根）/ 601318（806 根） | 各 55 检查点，mismatches=0 → PASS |
| 耗时：预计算 1 次 + 200 时点 at(i) | 0.004s |
| 耗时：200 时点各自截断重算全套指标 | 0.558s |
| **加速比** | **≈141×**（指标计算环节） |

## 5. 提速预期（全市场场景）

指标计算总量从 `时点数 × 策略数` 次全序列计算降为每股 1 次：300 只×174 天×3 策略场景下约
15.7 万次 → 300 次，指标开销可忽略。**注意**：适配器回退路径下策略 evaluate 内部仍重算，实际
端到端提速需要策略逐步提供 `evaluate_with_indicators`（或驱动层直接用 Indicators 改写热点策略）。
建议优先迁移 s01/s05/s09（大信号量策略）与 bt_driver 主循环。
