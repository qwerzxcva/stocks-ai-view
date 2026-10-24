# 参考仓库深度评审：touhoufan2024/qlibAssistant

- 来源：https://github.com/touhoufan2024/qlibAssistant （本地：`~/workspace/refs/touhoufan2024__qlibAssistant`）
- 许可证：**MIT** [事实]（已读 `LICENSE` 首行 "MIT License, Copyright (c) 2025 touhoufan"）→ 可参考结构并直接改造。
- 更新检查 [事实]：本地 HEAD `7754772` (2026-05-30, "Auto-update trained models [skip ci]")，`git log HEAD..@{u}` 为空（克隆于 10-17，本地无落后提交）；`git fetch origin` 因容器网络无法连接 github.com:443 失败，**无法确认 10-16 后远端有无新提交**。已知最新可见提交 05-30，距今约 4.5 个月；仓库自动化靠 GitHub Actions 每日运行，核心算法文件（roll/*.py）结构稳定。

---

## 1. 核心算法拆解 [事实]

### 1.1 多模型集成（25 模型投票）

- **模型矩阵**：`myconfig.py:15` 列出 8 种候选算法（XGBoost/LightGBM/CatBoost/Linear/DoubleEnsemble/KRNN/Sandwich/TFT），实际流水线用 5 种（README：XGBoost、LightGBM、DoubleEnsemble、Linear + 其一）× 5 种滚动回溯窗口（`traincli.py:198-205` `start_custom()`：12/24/36/48/60 个月训练段）= 25 个 recorder。
- **模型筛选与权重**（`modelcli.py:84-124` `ModelCLI.get_model_list()`）：
  - 遍历 MLflow experiments → 每个 recorder 必须含 `params.pkl` + `sig_analysis/` 产物（`_is_valid_recorder`, `modelcli.py:74-81`）；
  - 用 `rec_filter` 按 IC/ICIR/RankIC/RankICIR 阈值过滤（`filter_rec`, `modelcli.py:66-72`）；
  - **权重 = Rank ICIR 归一化**（`modelcli.py:117-123`）：
    `w_i = rank_icir_i / Σ rank_icir_j`，`rank_icir = mean(rank_ic)/std(rank_ic)`（`get_ic_info`, `modelcli.py:134-146`）。若 Σ=0 退化为等权。
- **投票合成**（`modelcli.py:288-305` `_save_results()` 内 groupby apply）：
  - `avg_score = Σ(score_i·w_i) / Σw_i`（加权平均，权重和为 0 退化为简单平均）；
  - `pos_ratio = mean(score_i > 0)`（**正收益投票占比**，即"多少模型看涨"——独立于权重的多数派信号，README 示例 0.875 = 8 个模型里 7 个看涨）。

### 1.2 复盘统计口径（ModelReviewHelper, `model_review.py`）

- **标签口径**：`real_label = Ref($close,-2)/Ref($close,-1) - 1`（`modelcli.py:334-339`）= T 日盘后出分 → **T+1 收盘买入、T+2 收盘卖出**的两日收益。基准为同口径 CSI300（`get_real_label_csi300`）。
- **胜率口径**（`_review_csv`, `model_review.py:33-96`）：
  - 只统计 `avg_score > 0` 的行（先过滤掉看空票）；
  - TopK 分层：K ∈ {10,20,30,50,80,100}（`model_review.py:9`）；
  - **止盈 n% 胜率**：`P(n2high > n1close×(1+n%))`，n=1..10 —— 用 T+2 最高价对 T+1 收盘价判断盘中是否触及止盈位（可达性口径，比收盘口径宽松）；
  - "持有一天平均收益"= mean(real_label)；"一天正收益占比"= 方向一致率 `mean((real_label×avg_score)>0)`。
- **回测口径**（`_backtest_handle_df_topk`, `model_review.py:270-320`）：每日 TopK 等权组合，扣换手费 `fee_rate=0.002` 双边（`_calculate_daily_equity`, `model_review.py:247-267`），换手率 = 相邻两日 TopK 集合替换比例；净值 cumprod，基准 CSI300 不计费，附回撤列。

### 1.3 filter_ret 规则过滤（`modelcli.py:326-332`）

硬规则链（Alpha158 因子），双重波动+动量夹逼：
```python
df = df[(STD5<0.10) & (STD20<0.10) & (STD60<0.10)]
df = df[(STD60<0.05) & (STD5<0.06)]          # 更严的第二道波动闸
df = df[STD5 < STD60*2]                       # 短期波动不得显著放大
df = df[(ROC10>0.80) & (ROC20>0.80) & (ROC60>0.80)]
df = df[ROC20 < 1.30]                         # 动量上限: 排除已涨 30% 的追高
```
注意 [推断]：Alpha158 的 ROC 是价格比值（>1 为涨），故 `ROC20>0.80` 意为"20 日跌幅不超过 20%"，`<1.30` 意为"20 日涨幅不超过 30%"——**跌不深、涨不多、波动收敛**的稳健形态筛选。

### 1.4 每日自动选股流程 [事实]

`script/run.py` + GitHub Actions（train.yml/analysis.yml/review.yml）：拉数据（chenditc/investment_data → dolt）→ 断点续训（`traincli.py:174-196`，按 train segment 去重跳过）→ `model selection`（25 模型预测当日 → collect → `_save_results` 落 `{date}_ret.csv` / `{date}_filter_ret.csv`）→ 次日复盘（review）→ 回测净值（backtest）→ VitePress 页面发布。子进程隔离训练（`traincli.py:28-88`，spawn + `os._exit(0)` 防内存泄漏）。

---

## 2. 公式对比：它的集成 vs 我们的三轮评分 [事实+推断]

| 维度 | qlibAssistant | 我们（central_orchestrator.py） |
|---|---|---|
| 权重公式 | `w_i = rank_icir_i / Σ rank_icir`（线性归一，直接 ∝ 历史质量指标） | `w_k = softmax(α·s_k)`，s_k = 近 N 日 T+1 胜率超额（α=1.0，`central_orchestrator.py:74, 135-140`） |
| 权重更新 | 静态（每次 selection 时由当时全部 recorder 的 IC 重算） | 乘性在线更新 `w←w·exp(0.1·(r−r̄))` + 截断 [0.05,0.4]（`feedback_updater.py`） |
| 投票信号 | `pos_ratio = mean(score>0)` 多数派占比 | 无独立多数派信号，只有加权分 |
| 权重下限保护 | 无 [事实：代码未截断负值，若 Σrank_icir 含负值权重会失真] | 有 W_MIN=0.05 截断 |
| 冷启动 | Σrank_icir=0 → 等权 | 权重文件缺失 → 等权 cold_start=true |

**结论 [推断]**：两套机制数学上都是"历史表现→权重→加权平均"。它的 Rank ICIR 归一化是**横截面静态加权**（质量指标本身除以其 std，自带稳健性），我们的 softmax(α·s_k) 是**时序自适应加权**；它多了 `pos_ratio` 这个与权重解耦的共识度信号，这正是我们缺的——可作为第一轮引擎级 ensembling 的补充输出（分歧度指标）。25 模型投票机制可以**独立实现**进我们的第一轮：不依赖 qlib/mlflow，仅需各引擎历史每日收益序列即可算 ICIR，与 softmax 可并存（双权重对照或融合 `w = 0.5·softmax + 0.5·icir_norm`）。

---

## 3. 可借鉴点清单

1. **[事实] Rank ICIR 归一化权重（`modelcli.py:117-123`）**：`w_i = rank_icir_i/Σrank_icir`。落地：`feedback_updater.engine_v5_eval` 已逐日收集 per_day 收益，可直接算 `mean(daily_excess)/std(daily_excess)` 作为 r_i，惩罚波动大的引擎，比纯胜率均值稳健。改动量小。
2. **[事实] pos_ratio 多数派共识信号（`modelcli.py:299-302`）**：与加权分并列输出。落地：`aggregate_stock_family()`（`central_orchestrator.py:274`）在 `result[code]` 增加 `pos_ratio = 出席引擎中看多占比`，meta 输出横截面分歧度，供第三轮终审做"共识过滤"（高分但低共识降权）。
3. **[事实] 止盈胜率分层统计（`model_review.py:60-78`）**：`P(T+2.high > T+1.close×(1+n%))` n=1..10 + TopK 分层（10/20/30/50/80/100）。我们的 `feedback_updater` 只有胜率/均值，无盘中止盈可达性口径与分层。落地：分片日线含 high 字段（第 5 列），在 `fwd_ret` 旁加 `fwd_high_hit`；TopK 分层复用 top_pct 循环。
4. **[事实] 换手感知净值（`model_review.py:247-320`）**：换手率 = 相邻期 TopK 集合替换率，`net = ret − turnover×fee`，基准不计费。落地：Phase3 回测脚本目前只算毛利，可并入；fee_rate 双边 0.002 的取值可参考。
5. **[事实] filter_ret 波动/动量硬规则（`modelcli.py:326-332`）**：STD5/20/60 三周期波动闸 + ROC 上下限夹逼。落地：作为终审榜单 post-filter 挂在 `final_rank` 之后（STD/ROC 从分片日线自算，无 qlib 依赖），补充现主板前缀过滤。
6. **[事实] 产物完整性检查（`modelcli.py:74-81` + `traincli.py:174-196` 断点续训）**：recorder 必须同时含关键产物才入集成。对应我们：`run_engine` 已做超时/失败降级，但对输出 JSON 字段完整性无校验，可借鉴 `_is_valid_recorder` 模式加最小 schema 检查。
7. **[事实] 预测-标签合并基数校验（`modelcli.py:249-257`）**：`pd.merge(..., validate="many_to_one")` 显式声明多模型预测对单标签的基数关系，错位即报错。我们的 by_code 聚合是 dict 手工对齐，无此防线；落地成本低（聚合后断言每 code 每引擎至多一条）。

## 4. 落地建议（融入 central_orchestrator.py / feedback_updater.py）

优先级排序（改动小→大）：
1. **pos_ratio 共识信号**（第一轮 `aggregate_stock_family`）：~15 行，零依赖。
2. **ICIR 化 r_i**（`feedback_updater.engine_v5_eval`）：per_day 列表已有，加 `icir = mean/std` 替换或并列胜率超额；`update_rule` 字符串同步注明。~20 行。
3. **止盈命中率统计**（`feedback_updater` 新增 `fwd_high_hit`，仅入 evidence 不改权重公式）：~30 行。
4. **TopK 分层 + 换手费净值**（复盘报告侧，不动权重链）。
5. **filter_ret 式 post-filter**（终审后），需 STD/ROC 自算工具函数，~50 行。

红线遵守：以上全部为**数学模型/统计口径级借鉴**；MIT 许可证下结构参考亦允许；不引入 qlib/mlflow 依赖（我们标准库优先约定），全部基于分片日线自算。

## 5. 接口契约备忘（若后续对接其产物）

- 输入：`qlib_score_csv/selection_YYYYMMDD_HH_MM_SS/{date}_ret.csv`、`{date}_filter_ret.csv`，列含 `instrument/avg_score/pos_ratio/real_label/error/abs_error` + Alpha158 全因子（KMID 起右侧列复盘时裁掉，`model_review.py:140-152`）。
- 复盘产出：`review_csv/`、`backtest_csv/{10..100}_(filter_)ret.csv`（仓库 `page/docs/public/backtest_csv/` 有真实样例可对照）。
