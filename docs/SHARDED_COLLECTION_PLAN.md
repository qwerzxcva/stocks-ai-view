# 多平台分片采集架构 — 4 节点 × 按源分片

## 附带发现：K 线覆盖的真实状况（2026-10-14 实测）

项目记忆里写的「daily 207万行/3175只/2024-2026」「覆盖75%」**已过时且夸大**。实测 `data/bulk/`：

| 指标 | 实测值 |
|---|---|
| 有 K 线的股票 | **3997 只**（A 股应约 5400） |
| 覆盖不足 90% 的 | **2947 只（74%）** |
| 每只根数中位数 | **121 根**（约半年） |
| 最早日期在 2026 年的 | **2544 只（64%）** |
| 最早日期分布 | 2023:234 / 2024:1186 / 2025:33 / **2026:2544** |
| 缺口 K 线估算 | **~37 万根** |
| bulk 大小 | 66.4 MB |

**影响**：`data/predictions/` 的 643 个快照、reflect 的 6 万样本、walkforward 的 46 folds，
全部基于这 121 根中位数的短历史。之前所有胜率/过拟合结论都隐含这个前提。

## 问题

83 个数据源里 12 个 blocked、6 个 degraded，**根因不是频率而是出口 IP**：

- GitHub-hosted runner 全在 Azure（官方文档确认），出口是数据中心段
- 国内金融数据源（东财/同花顺/雪球）对数据中心 IP 一律按爬虫对待
- 东财 push2 约 20 次请求后必然 TLS RST，且**按 IP 计数**
- 部分源（mootdx/cninfo/szse）需要大陆出口 IP，GitHub 拿不到

## 解法：4 节点 × 按源分片（不是每节点跑全部源）

「每个账号都跑 80 多个源」会让封锁更快——风控按 IP 计数，4 个 IP 各打一遍全量等于没分散。
正确做法是**把源分到不同节点**，每个 IP 只打自己那批：

| 节点 | 平台 | 出口 IP | 负责的源 | 理由 |
|---|---|---|---|---|
| **A** | GitHub `qwerzxcva/stocks-ai-view` | Azure 海外 | 东财 datacenter/fund/np-*（非 push2）、腾讯行情+K线、新浪、akshare 间接、集思录、海外 10 个 | 主力，跑稳定的那批 |
| **B** | GitHub `jiegengxiangjianxue/stocks-ai-view-b` | Azure 海外（独立 IP） | 东财 push2 系（17个中的实时类）、同花顺、开盘啦、财联社 | push2 容易被封，单独放一个 IP，封了不影响 A |
| **C** | GitHub `dufuwuxin/stocks-ai-view-c` | Azure 海外（独立 IP） | 交易所官方（sse/szse/cninfo）、巨潮、tushare/unauth 6 个 | 低频高价值源 |
| **D** | GitCode Space（华为云容器） | **大陆 IP** | mootdx、cninfo、szse、雪球、小红书、任何需大陆 IP 的源 | 唯一有大陆出口的节点 |

### 为什么这样分

1. **IP 容量 ×4**：东财风控按 IP 计数，4 个 IP 各自只打自己那批，单 IP 频率降到 1/4
2. **故障隔离**：B 的 IP 被 push2 风控封了，A/C/D 的源不受影响
3. **大陆 IP 专节点**：D 节点救活 3-5 个「国内服务器上再验」的源
4. **不重复取同一数据**：同一文件只由一个节点写，其他节点不碰（避免并发覆盖）

## 数据汇总：每个节点写自己的文件，不交叉

```
A 写 data/a_*.json  ─┐
B 写 data/b_*.json  ─┼→ 主仓 merge (git_push_retry --protect)
C 写 data/c_*.json  ─┤
D 写 data/d_*.json  ─┘ (Space 通过 space-pull 拉回)
```

关键：**文件级隔离，不用目录隔离**。因为前端读 `data/xxx.json` 是固定路径，用前缀区分节点，merge 时互不覆盖。

## 现有基础

`collect_all_sources.py` 已有 `--groups quote,event,sector,news,fund`，但粒度太粗（5 组）。
需要细化为 `--shard a/b/c/d`，每个 shard 是一组源 ID 的白名单。

## 待确认

- [ ] GitCode Space 的出口是否真的是大陆 IP（需在 Space 内跑 `curl cip.cc` 验证）
- [ ] Space 被 repository_dispatch 唤醒后能跑多久（休眠前）
- [ ] B/C 仓的 `secrets` 是否需要各自配（如果只用公开源则不需要）

## 风险

- GitCode Space 30 分钟休眠：采集是短时任务（<10 分钟），唤醒→跑→睡 可行，不需要常驻
- 4 节点都写主仓 main：并发 push 冲突。已有 `git_push_retry.py --protect` 兜底
- 源分片后某节点挂了会导致那批数据陈旧：需在 `audit_data.py` 加分片新鲜度检查
