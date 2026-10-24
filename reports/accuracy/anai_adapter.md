# anai_adapter 评估说明（跳过）

数据源：`reports/accuracy/anai_adapter.json`

- 状态：**跳过 — 非预测引擎**。
- 理由：anai_adapter 是数据质量/数据源适配层（anai.pro 逆向 + 双模式），不产出选股/方向预测，无胜率可测。
- 定位：为其它引擎提供数据接口，其准确度应体现在下游引擎（regime_router、tomorrow_candidate 等）的评估结果中。
