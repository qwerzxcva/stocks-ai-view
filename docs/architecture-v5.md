# 全管道架构 (数据源 → 数据库 → 引擎 → 复盘)

> 本文档描述 2026-10-13 重建后的完整数据管线。
> 每个模块都可独立运行, 也可由 `full-pipeline` 工作流串起来。

## 1. 数据源注册表

**唯一事实来源**: `scripts/sources_registry.py`

登记了本项目探测/使用过的 **35 个数据源**, 每条含:

| 字段 | 说明 |
|------|------|
| `status` | `ok` / `degraded` / `blocked` / `unauth` / `untested` |
| `auth` | `none` / `key` / `cookie` |
| `freq` | `realtime` / `minute` / `daily` / `weekly` |
| `delay` | 数据延迟 (如 TV 的 15 分钟) |
| `field_map` | 字段映射 (如腾讯 p3=price, p39=PE) |
| `degrade_to` | 降级链 (本源失败改用什么) |
| `notes` | 实测结论与坑 |

当前分布: **17 ok / 5 degraded / 7 blocked / 5 unauth / 1 untested**

```bash
python3 scripts/sources_registry.py --list     # 列出全部
python3 scripts/sources_registry.py --probe    # 实测 none-auth 源
python3 scripts/sources_registry.py --json data/sources_registry.json
```

**为什么 blocked 的也要登记**: 避免重复踩坑。例如 `eastmoney_clist`
(东财板块) 在容器内被 WAF 封, 曾导致 sector_pool 只剩 2 个板块、
multi_v5 因缺行业映射出 0 候选——登记后新代码直接走降级链, 不再重试。

## 2. 采集管道

**`scripts/collect_all_sources.py`**

```
8 个 fetcher ──► ThreadPoolExecutor(6~8) ──► 按主键合并 ──► 带溯源 JSON
```

设计要点:

1. **并发**: `ThreadPoolExecutor`, 单源失败不阻断其他 (实测 8 源 11.9s)
2. **去重主键** (按业务语义, 不是简单 code):
   - `quote` → `code`
   - `event` → `code|theme|date` (龙虎榜同股多原因)
   - `fund` → `code|name` (A/C 份额同码)
   - `news` → title 归一化 MD5
3. **冲突解决**: 数值冲突保留高优先级源的值, 记录 `conflicting_fields`
   与 `field_sources` 供审计 (不静默丢弃)
4. **溯源**: 每条记录带 `sources` 数组 + `first_seen`/`last_seen`

来源优先级 (`SOURCE_PRIORITY`): 东财官方涨跌停 90 > 东财 datacenter 85 >
腾讯行情 80 > 本地快照 70 > 同花顺 60 > TV 55 > 新浪 40 > Yahoo 30

```bash
python3 scripts/collect_all_sources.py --out /tmp/all.json --workers 8
```

### 踩过的坑 (已修复并注释在代码里)

| 坑 | 现象 | 修法 |
|---|---|-----|------|
| 东财 `datas` 是字符串数组 | `json.loads('['+...+']')` 把字符串拆成字符数组 → code=0/name=1 | 每个元素先 `split(',')` |
| 基金代码重复 | 用 code 做主键把 A/C 份额错误合并 | 主键改 `code\|name` |
| 龙虎榜当日无数据 | 传当日日期返回 0 条 | 逐日回退 5 天, 记录实际日期 |
| 涨跌停池只读 pool | `pool=[]` 但 `tc=19` → 跌停显示 0 | 家数读 `data.tc` |

## 3. 数据库层

**`scripts/db_layer.py`** — DuckDB 单文件

```
quotes       日行情快照     (code, trade_date) 索引
kline_daily  日K qfq       (code, date) + year 分区
events       龙虎榜/大宗    (code, trade_date), (type, trade_date)
margin       两融余额       date PRIMARY KEY
pools        池子/板块评分  (kind, date)
sources_log  采集溯源日志   (ts, source)
```

选 DuckDB 的理由: 零依赖单文件、列式向量化执行、支持索引与分区、
可被 pandas/BI 共用。

实测查询性能 (2577 只 K线 + 2006 条行情):

| 查询 | 耗时 |
|------|------|
| 全市场快照计数 | 7.46 ms |
| 单股最新行情 | 4.01 ms |
| 涨幅前 20 | 10.71 ms |
| 低估值筛选 | 3.10 ms |
| 龙虎榜按成交额 | 2.16 ms |
| 两融趋势 | 1.68 ms |

```bash
python3 scripts/db_layer.py --init
python3 scripts/db_layer.py --load /tmp/all.json
python3 scripts/db_layer.py --bench
python3 scripts/db_layer.py --query "SELECT ..."
```

## 4. 算法引擎 v5

**`scripts/engine_v5.py`** — 多指标共振评分

旧版问题: 权重是手工常数、只用 MA/RSI 两三个指标、无法回答"为什么"。

v5:

```
score = 50 + 50 × (Σ w·vote·strength / Σ w) × (0.6 + 0.4 × 共振率)
```

- **17 个指标**投票 (`indicators_ext.py`, 移植自 StockSharp 190 指标库):
  SuperTrend / MA5-20 / RSI / MACD / KDJ / BOLL-Keltner / Donchian / CMO /
  Vortex / UltimateOsc / Choppiness / HullMA / ElderForce / ConnorsRSI /
  A/D Line / Pivot / VWMA
- 每个指标给 `vote ∈ {-1,0,+1}` + `strength ∈ [0,1]` + `why` (可解释)
- **共振率** = 同向票数 / 方向票数; 分歧大时系数降到 0.6 (指标打架就降权)
- **自适应权重**: 权重默认等权, 可由 `optimizer.py` WalkForward 校准

```bash
python3 scripts/engine_v5.py --out data/engine_v5.json --limit 400
```

实测输出 (2026-10-13):

```
sh600408 score=76.7 共振=0.88 多15/空2/中0
sh605177 score=74.5 共振=1.00 多13/空0/中4
```

## 5. 复盘与自我反思

**`scripts/reflect_engine.py`** — 回答「预测到底准不准」

流程: 读历史预测 → 到 N 个交易日后用实际K线算涨跌 → 对比 → 统计各指标
历史胜率 → 找出有效指标与噪声指标 → 输出校准建议。

防自欺设计:

- **无前视偏差**: 只用预测时点已知的信息
- **样本门槛**: 样本 <30 标 `evidence: insufficient`, 结论不可信
- **双向披露**: 同时报看多胜率与看空胜率 (只报看多是选择性披露)
- **诚实输出**: 没有未来数据就输出空, 不用模拟数据凑

实测输出 (2026-10-13, 100 条样本):

```
1日后: 看多胜率=38.7% (n=62) | 看空胜率=40.0% (n=10) | 平均收益=-0.51%
```

**这是一个否定结论**: 当前指标组合在该时点 (下跌市) 失效。
复盘的价值就在此——不粉饰, 直接暴露问题。

## 6. K线增量补录

**`scripts/update_klines_recent.py`**

修复本地 K线滞后 (曾停在 10-09, 而实时快照已有 10-12, 导致复盘无数据)。
只追加缺失日期, 不动已有历史。

```bash
python3 scripts/update_klines_recent.py --days 5 --limit 3000
```

## 7. 工作流

| 工作流 | 频率 | 职责 |
|--------|------|------|
| `full-pipeline.yml` | 交易日 5 次/天 | 采集→DB→引擎→复盘 一体化 |
| `hot-impact-realtime.yml` | 盘中每 30 分钟 | 热点推演 + enrich.json |
| `rebuild-pools.yml` | 交易日 4 次/天 | 板块池/周期池/低位池/周期深析 |

## 8. 输出文件

| 文件 | 内容 | 更新方 |
|------|------|--------|
| `data/sources_registry.json` | 数据源注册表快照 | full-pipeline |
| `data/engine_v5.json` | 引擎 v5 评分 | full-pipeline |
| `data/reflect_latest.json` | 复盘结果 | full-pipeline |
| `data/enrich.json` | 强势股/龙虎榜/大宗 (独立文件抗覆盖) | hot-impact |
| `data/hot_impact.json` | 热点推演 | hot-impact |
| `data/cycle_deep.json` | 周期深析 (8566 日全史) | rebuild-pools |
| `data/sector_pool.json` | 板块池 (100 个) | rebuild-pools |
| `data/cycle.json` | 周期池 (5 指数) | rebuild-pools |
| `data/lowpool.json` | 低位池 | rebuild-pools |
| `data/index_kline.json` | 指数历史 K | rebuild-pools |

## 9. 已知限制

1. **K线覆盖 48.2%** (2577/5343): 腾讯接口连续约 1750 次请求后返回 501,
   需错峰分批; nightly-kline 工作流负责长期补录
2. **TV 数据延迟 15 分钟**: 非实时源, 页面已标注
3. **龙虎榜/大宗当日数据要收盘后才出**: 已实现日期回退并标注
4. **指标胜率未经大样本验证**: 当前 100 条样本的 38.7% 看多胜率不足以定论,
   需积累数百条后才能校准权重
5. **东财 push2 域在容器内被封**: 板块资金流、个股行业等字段走降级链
