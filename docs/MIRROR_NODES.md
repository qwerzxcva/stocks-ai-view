# 镜像节点现状（2026-10-16 合并后）

## 定位

只有一个站点：<https://qwerzxcva.github.io/stocks-ai-view/>（A 仓 `qwerzxcva/stocks-ai-view`，Pages 源 = `main` 根目录）。

B/C 仓是**纯采集镜像节点**，不承载站点、不承载前端逻辑：

| 节点 | 仓 | 分片 | cron 分钟位 | 站点 |
|---|---|---|---|---|
| A | `qwerzxcva/stocks-ai-view` | a + e | mod 5 = 2 | **唯一站点** |
| B | `jiegengxiangjianxue/stocks-ai-view-b` | b | mod 5 = 4 | 无 |
| C | `dufuwuxin/stocks-ai-view-c` | c | mod 5 = 0 | 无 |

前端 `gh_sources.js` 的三仓 `REPOS` 已按上表填好，`data_origin_patch.js` 并发读三仓 raw 取 `ts` 最新的那份，有效数据粒度 2 分钟。

## C 仓曾经的歧义

C 仓一度开启了 Pages（`dufuwuxin.github.io/stocks-ai-view-c/`），并自带 `pages-sync.yml` 每 10 分钟把 A 仓 25 个关键文件 diff 到自己的 `data/`，让它的前端走「同源优先」。那是在 A 仓还没有同源可读数据时的替代方案。

合并后确认：**A 仓 Pages 源就是 `main` 根目录，`data/` 与 `index.html` 天然同源**，实测同源可读且 Pages 构建跟随 push 约 46 秒一次。因此 C 仓的 Pages 与 pages-sync 都不再需要，C 仓回归纯采集。

处理方式：C 仓的 Pages 关闭、`pages-sync.yml` 停用；`data/` 里由 sync 产生的副本可留作采集产物的落地目录，不影响 A 仓。C 仓的 `scripts/` 仍是 A 仓镜像，不要在 C 仓上手改代码。

## C 仓里独有、未并入 A 仓的产物

这些是 C 仓自己跑的实验产出，不是 A 仓缺失的能力，A 仓已有等价或更完整的实现，故不并入：

| 文件 | 内容 | A 仓对应物 | 不并入的原因 |
|---|---|---|---|
| `ai_pick_v5.json` | 月频 7 因子等权多头池（2956 只打分取 top50），外部量化流程 `composite_pick_v2` 产出 | `factor_lab.py` / `multidim_engine.py` | 口径与调仓频率都不同，月度池对日更站点无增量 |
| `composite_pool_v3.json` | 全市场 composite 池 295 只 | 同上 | 同上 |
| `fin_screen.json` | 财务 5 维 zscore 筛选（数据来自 `db-fin-0` 仓） | `factor_lab.py` 财务维度 + `valuation_engine.py` | 源仓独立、口径不同，并入会产生两套基本面分数打架 |
| `watchlist_snapshot.json` | 一次性人工观察快照（4 指数 + 自选股盘中价） | `market_now.json` / `global_markets.json` | 非流水线产物，无生成脚本 |

若以后要把其中某个纳入站点，先确认它的生成脚本与数据源归属，再决定是搬到 A 仓还是继续由 C 仓产出、前端按 `SOURCES` 指向 C 仓 raw。

## 分片采集的边界

B/C 只跑 `shard-collect.yml`（改 cron + 分片号），其余 workflow 的 schedule 由 `scripts/deploy_mirror.py` 统一注释掉，避免三仓重复跑重任务、重复烧额度并互相覆盖 commit。

部署/更新镜像仓：

```bash
export MIRROR_TOKEN_B=ghp_xxx  MIRROR_TOKEN_C=ghp_xxx
python3 scripts/deploy_mirror.py --label B
python3 scripts/deploy_mirror.py --label C
```
