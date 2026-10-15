# 算法与引擎清单 (2026-10-15 盘点)

仓库 `scripts/` 共 155 个 Python 脚本, 其中 82 个被 GitHub Actions 实际调用。
分四类: **采集器** (拿数据) / **引擎** (算信号) / **推股** (出清单) / **校验闭环** (复盘校准)。

---

## 一、采集器 (12 个, 零鉴权为主)

| 脚本 | 职责 | 数据源 |
|---|---|---|
| `collect_market_extras.py` | 涨跌停池/涨跌分布/两融/龙虎榜/大宗/同花顺强势股/**板块资金**/指数K线 | 东财 datacenter + push2, 腾讯 mktHs, 同花顺 |
| `collect_global_markets.py` | 全球六大市场 15 指数实时价+状态+情绪 | 腾讯 sqt / 东财 push2 / 新浪 znb(韩股) |
| `collect_global_kline.py` | 全球指数与个股历史日K 增量构建 | 腾讯 fqkline |
| `collect_moneyflow.py` | 个股主力资金流入/流出 | 东财 push2his fflow |
| `collect_hotnews.py` | Newsnow 15+ 平台热点聚合 | Newsnow 公共 API |
| `collect_news_extra.py` | 财联社电报/华尔街见闻/联合早报实时资讯 | 逆向 + SSR 解析 |
| `collect_all_sources.py` | 统一采集管道 (并发+合并+去重+溯源) | 83 个注册源 |
| `collect_adjusted_bulk.py` | 全市场复权K线 (qfq/hfq) | 腾讯 + 新浪 |
| `collect_level2.py` | Level2/逐笔/盘口/资金 | 逆向成果 |
| `collect_v2_extras.py` | 公告/新闻/基金净值等 v2 新域 | 多源 |
| `collect_klineshare.py` | KlineShare 看板免 key 数据 | klineshare.cn |
| `collect_jisilu.py` | LOF/ETF/可转债/新股 | 集思录 |

---

## 二、核心引擎 (信号计算)

### 2.1 技术面
| 脚本 | 职责 |
|---|---|
| `engine_v5.py` | **17 个技术指标共振打分** (所有技术信号的底座) |
| `patterns.py` | K线形态识别 |
| `indicators_ext.py` | 扩展指标库 |

### 2.2 多维因子 (十维)
`multidim_engine.py` — 在 engine_v5 之上叠加 10 个维度:

| 维度 | 权重 | 数据来源 |
|---|---|---|
| 技术面 | 0.22 | engine_v5 17 指标 |
| 资金面 | 0.14 | 东财 fflow 主力净额 + 持续性 |
| 高低位 | 0.13 | 20/60日区间分位 + 乖离 MA20 |
| 基本面 | 0.09 | 腾讯快照 PE/PB/市值 |
| 板块面 | 0.10 | sector_pool 梯队 + **板块资金主力净额** |
| 海外面 | 0.07 | **overseas_linkage 的 corr/beta + 全球情绪** |
| 市场面 | 0.08 | 涨跌家数/涨停数/成交额/赚钱效应 |
| 情绪面 | 0.07 | 晋级率/封板率 + 主题热度 |
| 周期面 | 0.05 | cycle_deep 五维 + 月度季节性 |
| 消息面 | 0.05 | news_impact 主题命中 + hot_impact |

### 2.3 消息面 (三个引擎分工)
| 脚本 | 职责 |
|---|---|
| `news_impact.py` | 热点 → A股行业/个股映射 (502 条新闻 → 113 条已映射) |
| `news_transmission.py` | **消息传导: 坏消息利好谁 / 好消息利空谁** (16 条规则: 替代/避险/资金转移) |
| `hot_impact.py` | 热点 → 个股走势推演 (动量/过热/超跌/蓄势四态) |
| `heat_algo.py` | 事件聚类 + 热度计算 |
| `theme_to_stocks.py` | 题材 → 个股 (RPS强势 + 涨停基因 + 主板过滤) |
| `zsxq_collector.py` | 知识星球行业情报 (消息/政策/板块因子源) |

### 2.4 周期与结构
| 脚本 | 职责 |
|---|---|
| `cycle_deep.py` | 指数全史五维统计 (星期/月份/连段/动量/年内位置) |
| `cycle_pool_v2.py` | 35 年周期归位 (牛熊区间年份分布) |
| `phase_runner.py` | 市场六阶段机 (从涨停结构自算) |
| `rps_rotation.py` | RPS 相对强度轮动 |

### 2.5 海外关联
`overseas_linkage.py` — 量化「A股受海外影响的程度」, 输出 corr / beta / r2 / 最优滞后 / 隔夜传导。
实测: 沪深300 ← 恒生科技 corr 0.47 beta 0.35; 创业板指 ← 纳斯达克 corr 0.33 beta 0.70 隔夜 0.22。

### 2.6 资金与筹码
| 脚本 | 职责 |
|---|---|
| `flow_divergence.py` | 资金流-价格背离 (吸筹/派发/波动收敛) |
| `cyq.py` | 筹码分布 |
| `moneyflow_scan.py` | 资金流向扫描 |

### 2.7 打板
| 脚本 | 职责 |
|---|---|
| `ban_prob.py` | 涨停次日概率/炸板率实证统计 (20081 样本) |
| `ban_pick.py` | 打板个股判断 (封板强度/量比/前期涨幅) |

---

## 三、推股引擎 (出清单)

| 脚本 | 产出 | 时机 |
|---|---|---|
| `premarket_picks.py` | **早盘推股** A/B/C 三级 + 入场区间/止损/目标 + 市况闸门 | 09:15 前 |
| `midday_picks.py` | 午盘修正 (上午验证 + 下午清单) | 11:30 |
| `afternoon_picks.py` | 尾盘清单 | 14:00 后 |
| `build_today_picks.py` | 今日推股 (多源汇总) | 盘中 |
| `ai_pick_v4.py` | AI 推荐 v4 (多引擎融合) | 夜间 |
| `ai_screener.py` | AI 筛选 (全市场扫描) | 夜间 |
| `multi_screener.py` | 多轮审核 × 多源融合 (TSP v4) | 夜间 |
| `engine_fusion.py` | 多引擎信号融合 | 夜间 |
| `decision_engine.py` | 决策卡 (市场环境/仓位/买入信号/回避) | 夜间 |
| `scenario_engine.py` | 场景化推股 | 夜间 |
| `intraday_signal.py` | 日内反转信号 | 盘中 |
| `entry_planner.py` | 入场计划 | 盘前 |
| `build_lowpool.py` | 低位启动池 | 夜间 |
| `build_mainlines.py` | 今日主线 + 明日主线预测 | 收盘 |
| `strategy_card.py` | 策略卡 (208万行网格搜索最优) | 夜间 |
| `portfolio_optimizer.py` | 组合权重优化 (风险预算) | 夜间 |
| `risk_engine.py` | 风险引擎 | 夜间 |

---

## 四、估值与财务

| 脚本 | 职责 |
|---|---|
| `valuation_engine.py` | 每股内在价值估算 (多方法) |
| `loss_attribution.py` | 亏损归因 (四因子) |
| `numeric_validator.py` | 数值校验 |

---

## 五、复盘与校准闭环

| 脚本 | 职责 |
|---|---|
| `close_review.py` | **收盘复盘** |
| `morning_review.py` | 早盘复盘 |
| `review_yesterday.py` | 昨日推股验证 (逐条对照止盈/止损) |
| `reflect_engine.py` | 历史胜率反思 |
| `winrate_matrix.py` | 持有期 × 市场阶段 交叉胜率矩阵 |
| `weight_calibrator_v2.py` | **从实际收益反推引擎权重** |
| `kline_sim.py` | K线模拟回测 (含滑点) |
| `walkforward.py` | Walk-forward 滚动前推验证 |
| `self_reflect.py` | 引擎自我反思 |
| `pick_tracker.py` | 推股跟踪对账 |
| `signal_auditor.py` | 单张决策卡审核 |
| `edge_miner.py` | 边际挖掘 |
| `gitcode_ai_review.py` | **GitCode AI 结构化复盘** (11 类引擎产出 → LLM) |

---

## 六、数据仓与基础设施

`db_layer.py` (DuckDB) / `data_warehouse.py` / `sources_registry.py` (83 源注册表) /
`build_industry_map_em.py` (个股→东财中文行业) / `build_search_index.py` /
`slim_latest.py` / `git_push_retry.py` (多 bot 并发推送) / `deploy_mirror.py`

---

## 当前已知问题 (2026-10-15)

1. **看多胜率 45.3% < 随机 50%**, 看空 54.4%, **多空倒挂**; 10 日维度仅 44.1%。
   说明正向买入信号整体负期望 — 这是「推荐股票表现一般」的量化证据。
2. **板块面曾恒为占位分 50** (英文行业 vs 中文板块名不匹配), 已修。
3. **海外面曾全部无数据** (global_kline 字段名 symbols/klines 不一致), 已修。
4. **消息传导的行业覆盖不足** (东财限流, 只拿到 80/500+ 行业), 半导体/黄金/化工
   等关键行业暂无成分股, 周一 cron 重建后补全。
5. **未做的算法**: 涨停归因与预测 / 每个板块独立算法+报告 / 收盘复盘与明日推股展示。

---

## 备注

- 所有 token 从环境变量读, 无硬编码。
- 6 个 bot 并发推 main, 用 `git_push_retry.py` 的 fetch+reset+protect 避免互相覆盖。
- 采集中「Research reference only, not investment advice」口径不变。
