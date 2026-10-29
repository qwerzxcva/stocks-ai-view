# 结构识别层（structure_engine）接入说明

## 这一层解决什么问题

现有链路是纯指标坐标系：`engine_v5` 投指标票、`ai_pick_v4` 做位置过滤、`multidim_engine`
做多维打分。指标是价格的有损压缩——同样的 MACD 金叉，在上升通道的回踩里是买点，
在区间上沿的假突破里是陷阱，指标本身分不出来。缺的维度是**结构**：这根 K 线处在什么形态、
哪一段结构、量价是否配合。

`scripts/structure_engine.py` 补这一维，方法论来自批次一 8 本书（见 `docs/books/`）。

## 输出契约

`data/structure_signals.json`

```json
{
  "generated_at": "2026-10-19 16:20:00",
  "engine": "structure_engine/v1",
  "count": 775,
  "items": [{
    "code": "sh600016", "date": "2026-10-09", "close": 3.52,
    "score": 10, "bias": "neutral",
    "tags": ["trend:range", "chan:third", "candle:hammer"],
    "candles": [...], "price_action": {...}, "chan": {...},
    "elliott": {...}, "volume_price": {...}
  }]
}
```

每个子结构都带 `evidence`（人话理由），可复核、可进报告。

## 消费方式（重要：不要当方向用）

回测结论（90 只样本、日线、5 日持有，`--validate`）：A 股短周期由**均值回归**主导，
部分结构标签的实测方向与书本相反。完整数字见 `data/structure_calibration.json`
（由 `--calibrate` 生成，带 n / 胜率 / t 值与 edge 标记），这里只列量级：

| 标签 | 方向 | 说明 |
|---|---|---|
| `phase:accumulation` | 顺向有效 | 吸筹区突破后短期偏强 |
| `ew:impulse_up_w5` | 顺向偏弱 | 推动浪雏形有正收益但 t 不足 2 |
| `breakout:up` | **反向** | 追突破在 5 日尺度跑输，与量能合流后才有意义 |
| `breakout:down` | **反向** | 破位后反弹 |
| `chan:third_sell` | **反向** | 跌破中枢后反抽常是洗盘 |
| `phase:distribution` | **反向** | 派发阶段短期反而偏强 |
| `failed_breakout` | 顺向（弱） | 唯一方向与书一致的突破类标签 |

所以本层的定位是**条件器（conditioner）**，不是信号源：

1. `bias`/`score` 只描述结构状态，**禁止**直接映射成买卖
2. 方向与权重一律从校准表取，不从书本先验取：
   ```python
   import signal_hub as sh
   e = sh.structure_expectancy("sz002043")
   # → {'basis': 'breakout:up', 'edge': 'contrarian', 'edge_5d_pct': -0.87,
   #    'win_rate': 40.0, 't': -2.56, 'n': 427, 'horizon_days': 5}
   # None = 该股标签无实测支持，宁可不给结论
   ```
3. `structure_expectancy()` 只放行 `n ≥ 120` 且 `|t| ≥ 1.5` 的标签，并按 |t| 取最强的组合键
4. 5 日尺度的反向特性说明持有期与标签要匹配：结构标签更适合 10~20 日或周线择股

## 接入点

```python
import signal_hub as sh
v = sh.structure_view("sh600016")   # 缺失返回 None，不编造默认值
tags = sh.structure_tags("sh600016")
```

`signal_hub.structure_view()` 已实现并做惰性加载（首次调用读 JSON 并缓存）。

建议接进 `cross_check()` 作为第十项检查：结构层与资金流/指标层冲突时给出 `warn` 而不是 `block`，
因为回测显示结构标签的指向依赖市场状态，硬拦会砍掉大量有效票。

## 生成与检证

```bash
python3 scripts/structure_engine.py --all              # 全市场 → data/structure_signals.json
python3 scripts/structure_engine.py --code sh600016 -v # 单只明细
python3 scripts/structure_engine.py --validate --sample 90   # 逐标签前向收益 + 组合条件
python3 scripts/structure_engine.py --calibrate --sample 90  # → data/structure_calibration.json
python3 tests/test_structure_engine.py                 # 10 条红线测试
```

`--validate` 同时是未来函数检查：任何用到 t 之后数据的地方，都会表现为异常高的 t 值。
锁这条不变量的是 `tests/test_structure_engine.py::NoLookaheadTest`。

性能陷阱（踩过了）：`_duck()` 必须单例，`--all` 要用 `load_all()` 一次把 770 只取完（0.2s/只 → 2s 全部）。
早期版本每只新开一个 266MB 的 DuckDB 连接，770 只跑 28 分钟跑不完。

## 已实现 / 未实现

已实现：蜡烛形态（含位置条件）、趋势三态与棒型、突破/失败突破/测量移动、
缠论包含处理→分型→笔→中枢→三/三类买卖点、背驰近似、波浪 5 浪启发式（含三硬规则）、
量价四象限、VSA 三事件（无需求/上冲回落/停止量）、威科夫阶段提示。

未实现（下一步）：
- 主要趋势反转（MTR）四要件状态机
- 威科夫完整事件序列（PS/SC/AR/ST/Spring/SOS/LPS）
- 缠论多级别联立（需分钟数据）
- 订单流（delta/吸收/失衡）——需 tick 或分钟聚合，属批次二
- 点数图目标测算（威科夫因果定律）
