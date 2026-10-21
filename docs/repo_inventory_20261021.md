# 三账号股票仓库全景图（2026-10-21 实测）

共 59 个仓库，其中 **32 个与股票体系相关**。按功能分七层。

---

## 一、网站与镜像层（3 主 + 2 镜像）

| 仓库 | Owner | 作用 | 状态 |
|---|---|---|---|
| `stocks-ai-view` | qwerzxcva | **主站**。前端 + 31 个采集/引擎 workflow + 1368 个 data JSON。GitHub Pages 线上：`qwerzxcva.github.io/stocks-ai-view` | 活跃，10-21 仍在推 |
| `stocks-ai-view-c` | dufuwuxin | 站点镜像节点（多账号 2 分钟轮询分流），含 MIRROR.md | 活跃 |
| `stocks-ai-view-b` | jiegengxiangjianxue | 同上，第二个镜像节点 | 活跃 |
| `news-digest` | qwerzxcva | 每日新闻摘要 md 存档（10-06 至 10-11），非站点数据 | 10-10 后停更 |
| `d4-pages-mirror` | dufuwuxin | Pages latest.json 只读镜像（双 jsdelivr 节点） | 10-12 后停更 |

镜像站点 b/c **不是**线上主站，作用是把采集/定时任务分散到不同账号，避开单账号 Actions 额度与频率上限。

---

## 二、市场总仓（1 个，核心索引）

| 仓库 | 作用 |
|---|---|
| `jiegengxiangjianxue1/db-market` | **全市场的 master/universe + 市场维度数据总仓**。591 文件：`master/universe.json`（6029 只证券主表）、`_index.json`（n_securities_master=6029 / n_stocks_built=6027 / 13 shards / 668MB / 33119 文件）、`_search.json`、`sector/`（404 个行业与概念板块成分及 K 线）、`market/`（指数行情、涨跌停池、两融、市场宽度）、`overseas/`（全球指数、美股/港股个股、ETF）、`predictions/`、`ledger/`。**前端分片仓路由的 universe 事实来源**。 |

---

## 三、个股分片仓（5 个在用 + 2 个已废弃）

这是覆盖最乱、最容易误解的一层。前端 `db-client.js:33-50` 的真实路由：

```
00 → jiegengxiangjianxue1/db-stocks-a   ← 实际生效
30 → jiegengxiangjianxue2/db-stocks-b
60 → jiegengxiangjianxue2/db-stocks-c
68/15/51/50/52/56/58/20 → jiegengxiangjianxue3/db-stocks-d
81/83/89/90/92 → jiegengxiangjianxue3/db-stocks-e
```

| 仓库 | 内容 | 问题 |
|---|---|---|
| `jiegengxiangjianxue1/db-stocks-a` | shard 00，**1506 个 `stock.json`**，10-20 最后推送 | 在用，最新 |
| `jiegengxiangjianxue2/db-stocks-a` | shard 00，**5616 文件但 0 个 stock.json**，是旧的 `kline/qfq.json`+csv 结构（1543 csv） | **已废弃**：10-13 后停更，且与 `jiegengxiangjianxue1/db-stocks-a` 同名不同 owner、内容不同。前端只读前者 |
| `jiegengxiangjianxue2/db-stocks-b` | shard 30，618 个 stock.json | 在用，最新 |
| `jiegengxiangjianxue2/db-stocks-c` | shard 60，1695 个 stock.json | 在用，最新 |
| `jiegengxiangjianxue3/db-stocks-d` | shard 68/20 等，183 个 stock.json | 在用，但样本 `688001` 抽查为 404 |
| `jiegengxiangjianxue3/db-stocks-e` | shard 92/20/90 等，**0 个 stock.json**，仍是旧 csv/json 结构 | **半废弃**：10-13 停更；前端仍把 81/83/89/90/92 指向它，但这些前缀查不到 bundle |

---

## 四、全息数据库（1 个，与分片仓定位重叠）

| 仓库 | 作用 | 状态 |
|---|---|---|
| `jiegengxiangjianxue1/stocks-holo` | 每股一文件夹、九面 JSON：`meta/kline/technical/moneyflow/finance/news/sentiment/holo`。13379 文件、约 1680 只股票 | **10-17 后停更**，与 db-market 的 `data/stocks/<shard>/<code>/` 结构高度重叠，属早期方案遗留 |

---

## 五、历史数据分片仓（9 个，parquet 大仓）

| 仓库 | Owner | 内容 | 最后推送 |
|---|---|---|---|
| `db-daily-0` | jiegengxiangjianxue | A股日线 parquet 分片 0 | 10-16 |
| `db-daily-1` | jiegengxiangjianxue | 分片 1 | 10-16 |
| `db-daily-2` | dufuwuxin | 分片 2 | 10-16 |
| `db-daily-3` | dufuwuxin | 分片 3 | 10-16 |
| `db-daily-4` | qwerzxcva | 分片 4 | 10-18 |
| `db-daily-5` | qwerzxcva | 分片 5 | 10-16 |
| `db-fin-0` | jiegengxiangjianxue | **财务因子全史** fin_indicator 按年分区，1998 至今，字段 roe/roa/net_margin/debt_ratio/eps/bps 等 | 10-21 活跃 |
| `db-min-0` | jiegengxiangjianxue | **5 分钟线全史**分片 0（5575 只，2020-01 起，Baostock 主力） | 10-21 活跃 |
| `db-min-1` | jiegengxiangjianxue | 5 分钟线分片 1 | 10-21 活跃 |

`db-daily-*` 六个仓 10-16 后基本停更；`db-fin-0`/`db-min-0/1` 仍在跑。

---

## 六、专项增量仓（3 个）

| 仓库 | Owner | 作用 |
|---|---|---|
| `db-hk-0` | qwerzxcva / jiegengxiangjianxue **各有一个** | 港股日K（腾讯 qfq，2072 只港股通标的，320 根滚动窗口）。**两个同名仓重复**，qwerzxcva 的 7 文件、jiegengxiangjianxue 的 7 文件，都在 10-21 推送 |
| `db-cnflow-0` | qwerzxcva | 北向资金 + 两融历史（东财 datacenter，北向 2014-11 起、两融 2010-03 起），每小时全量刷新 |
| `t8-fq` | qwerzxcva | A股/ETF 前复权日K全史 parquet 备份，69 个 json，10-12 后停更 |

---

## 七、辅助/支撑层（5 个）

| 仓库 | 作用 | 状态 |
|---|---|---|
| `stock-memory` | jiegengxiangjianxue | 结构化记忆库，AI 可读（projects 274 / global 79 文件），10-17 停更 |
| `cloud-runner` | qwerzxcva | 云端守护：本地断联时自动采集+分析+AI复盘，拉 GitCode cloud_daily，10-20 停更 |
| `stocks-edge` | dufuwuxin | Codespaces 长跑采集（突破 Actions 6 小时限制）+ 分钟级实时，pit2016~pit2020 数据，10-19 停更 |
| `skapp-data` | jiegengxiangjianxue | skapp 符号数据 9 个 json，10-21 仍在更 |
| `db-stocks-a/b/c/d/e` 之外 | — | 其余 qwerzxcva 下 `t1`~`t13`、`sb-*`、`piliAI`、`BiliPai` 等 20+ 仓与股票体系无关（代理工具、动漫编辑器、数据集实验） |

---

## 关键结论

1. **真正在产的只有 3 类**：主站 `stocks-ai-view`、索引 `db-market`、个股 bundle 分片仓 `db-stocks-a(00)/b(30)/c(60)/d(68,20)`。
2. **覆盖率 66% 的根因**：universe 6029，但 shard 00 只有 1506、30 只有 618、60 只有 1695、68 只有 183、e 仓 0 个 stock.json。缺的 2027 只集中在 shard 15/50/51/52/56/58/81/83/89/90/92——前端路由指向 db-stocks-d/e，但这两个仓没建 bundle。
3. **三个可直接清理的重复**：`jiegengxiangjianxue2/db-stocks-a`（旧结构，前端不读）、`stocks-holo`（与 db-market 重叠，停更）、`db-hk-0` 两个同名仓。
4. **两处命名陷阱**：`db-stocks-a` 在 `jiegengxiangjianxue1` 和 `jiegengxiangjianxue2` 下各有一个，内容与结构完全不同；`db-hk-0` 在 qwerzxcva 和 jiegengxiangjianxue 下各有一个。
5. **停更仓占比高**：32 个股票仓中约 11 个 10-16 之前就停了，占 Actions 额度与账号杂乱度的主要部分。
