# 批次二~四引擎：量价微结构 / 交易管理 / 题材基本面

三个新引擎 + 一个共用验证框架，对应 `docs/books/` 批次二/三/四的判据卡。
所有新引擎都必须回答同一个问题：**这条判据在 A 股日线上成立了没有**——没成立就降级或弃用。

```
scripts/signal_validator.py      共用前向收益检验与校准（唯一实现）
scripts/kline_source.py          日线数据访问（qfq 优先、DuckDB 单例）
scripts/wyckoff_flow_engine.py   威科夫事件状态机 + 三角洲时间窗 + 订单流守卫
scripts/trade_plan.py            仓位/止损/加仓/期望值/纪律（交易管理层）
scripts/theme_engine.py          题材生命周期（板块环境）
scripts/fundamental_filter.py    基本面闸门（当前覆盖不足，返回未检查）
```

## 一、能力边界先说清（这比功能列表重要）

| 内容 | 状态 | 原因 |
|---|---|---|
| 威科夫事件序列（SC/AR/ST/Spring/Test/SOS/LPS/UT/UTAD/SOW） | 可落地 | 日线 OHLCV 足够 |
| Sperandeo 2B 法则 | 可落地 | 与 UT 同一几何现象，已作为别名标签 |
| 三角洲时间窗 | 可落地但低置信 | 极易过拟合；只给窗口不给方向，置信度上限 0.5 |
| 订单流（delta/吸收/失衡/足迹） | **不可落地** | 无分钟/tick/逐笔数据；拒绝用 CLV/量比冒充 |
| 仓位与止损/加仓/期望值/连亏熔断 | 可落地 | 纯规则，不依赖新数据 |
| 题材生命周期（板块级） | 可落地 | concept_heat.json（96 板块×30 日收益率） |
| 题材→个股归属 | **不可用** | concept_map（申万口径）与 concept_heat（东财概念）同名率 3.3% |
| 基本面筛选（ROE/现金流/应收/商誉/PB 分位） | **不可落地** | financial_summary 仅 108 只×2 指标（覆盖 14%） |

## 二、威科夫层（wyckoff_flow_engine.py）

```bash
python3 scripts/wyckoff_flow_engine.py --code sh600016 -v   # 单只明细
python3 scripts/wyckoff_flow_engine.py --all                # 全市场 → data/wyckoff_flow_signals.json
python3 scripts/wyckoff_flow_engine.py --calibrate --sample 90
```

事件判据全部是「价格相对区间边界的位置 + 量能相对 20 日均量 + 收盘在当根的位置」，
不画趋势线、不靠眼力，每个事件带 evidence 可复核。阶段判定在证据不足时返回 `unknown`
（全市场约 14% 落在 unknown，这是诚实不是缺陷）。

**三角洲的基线纪律**：输出里带 `baseline_prob`（随机一天落在窗口内的概率）。
不报基线的话，"命中 30%" 听起来像本事，其实随机也差不多。

## 三、交易管理层（trade_plan.py）

```bash
python3 scripts/trade_plan.py --demo
python3 scripts/trade_plan.py --code sh600016 --equity 100000
```

落成的规则：单笔风险预算反推仓位（100 股整数倍，不足 1 手返回 0）、
结构止损 vs ATR 止损（结构位超过 3×ATR 就改口径并降规模）、止损只收紧不放松、
只在盈利加仓且加仓后止损上移、巨量（≥3×量）优先减仓、
时间止损（N 根未被证明正确就减半）、连亏熔断阈值按胜率算而不是写死 3 笔：

```
k = ln(0.05) / ln(1 − p)        p=0.45 → 5.4 笔；p=0.30 → 8.6 笔
```

**A 股约束写进代码**：T+1（止损最早次日生效，日内不做止损假设）、涨跌停口径复用
`limit_caliber.py`（唯一实现）、成本按双边 25bp 折算进期望值。

`blockers` 非空就是**拒绝下单**，不是"建议谨慎"。

## 四、题材与基本面

```bash
python3 scripts/theme_engine.py --top 10          # 板块生命周期
python3 scripts/theme_engine.py --all             # → data/theme_signals.json
python3 scripts/fundamental_filter.py --coverage  # 诚实报覆盖度
```

题材阶段判据全用收益率（5 日/20 日累计 + 加速），阈值单位统一为百分比。
2026-10-13 实测分布：neutral 56 / sprout 24 / ebb 16。

基本面层当前只做一件事：**明确告诉你没做**。
`gate()` 返回 `status=None` + 原因，报告里不得写成"基本面良好"。

## 五、统一上下文入口

```python
import signal_hub as sh
ctx = sh.market_context("sz002043")
# ctx["structure"]  标签 + 校准期望
# ctx["wyckoff"]    事件/阶段 + 三角洲 + 校准期望
# ctx["theme"]      板块阶段 + mapping_ok
# ctx["fundamental"] status=None 表示未检查
# ctx["missing"]    缺哪层（不要当成中性）
```

## 六、实测结论（90 只样本、日线、5 日持有）

| 标签 | 5 日收益 | t | 判定 |
|---|---|---|---|
| `wyckoff:ut`（上冲回落） | +0.42% | 3.25 | **反向**：A 股 UT 多是洗盘 |
| `wyckoff:ut & stage_distributing` | +1.34% | 3.69 | **反向**：派发区 UT 后短期更强 |
| `wyckoff:test & sos`（吸筹完成） | −0.78% | −1.68 | **反向**：确认后追高吃亏 |
| `wyckoff:stage_unknown` | +0.45% | 5.65 | 反向（市场整体均值回归底色） |
| `breakout:up`（结构层） | −0.68% | −2.04 | 反向 |
| `chan:third_sell`（结构层） | +0.63% | 4.61 | 反向（最稳） |

**一句话结论**：在 A 股日线 5 日尺度上，所有"突破确认/形态确认"叙事系统性反向。
不是书错了，是这个持有期 + 这个市场由均值回归主导。所以三层结构信号（结构/威科夫/题材）
的定位都是**条件器**，方向与权重一律从校准表取，禁止硬编码书本先验。

## 七、待补

1. 订单流：需分钟或逐笔数据（当前表里没有），补齐后 `order_flow()` 直接接
2. 基本面：需财报全量（ROE/现金流/应收/商誉/PB 分位），`fundamental_filter.RULES` 已留接口
3. 题材→个股：需统一分类体系的成分股映射（东财板块 → 代码），当前两套口径不通
4. 点数图目标测算（威科夫因果定律的精确版）、MTR 四要件状态机、缠论多级别联立
