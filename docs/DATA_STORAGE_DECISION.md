# 重数据存放决策：留在 GitHub，GitCode 只做冷备

> 决策时间：2026-10-15 ｜ 决策人：AI（用户要求「自己思考，给对策和方案」）

## 1. 问题

如果把 K 线全历史（414 万行 / 281MB / .gz 后约 80MB）放到 GitCode 数据集仓，
算法引擎怎么读？网站怎么显示？GitHub Actions 怎么拉？

## 2. 实测数据（先算账）

| 事实 | 数值 | 来源 |
|---|---|---|
| 主仓 .git pack | **748 KiB** | git count-objects -vH |
| 主仓 data/ | 130 MB（含 22MB predictions 快照） | du -sh |
| K 线独立仓 | 281 MB（另一仓 qwerzxcva/stocks-data-kline） | clone 实测 |
| GitHub 单仓软限制 | 约 1 GB（官方建议 <1GB，>5GB 警告） | GitHub 文档 |
| Actions 读取方式 | sparse-checkout 只拉 data/bulk | 现有 workflow |
| 前端读取 K 线 | **不读 bulk**，读 data/*.json 摘要 | fetchJSON |

**关键发现：前端从来不读 bulk 全历史，只有引擎读。**

## 3. 三个方案对比

### 方案 A：K 线放 GitCode 数据集仓

```
bulk-collect (三仓) → push 到 GitCode stocks-kline-daily
                                    ↓
引擎要读 → Actions 里 git clone GitCode（需 GC_TOKEN secret）
                                    ↓
                             clone 281MB ≈ 40-90s/次
```

问题：
1. 每次引擎运行都要 clone 281MB，一天跑 20+ 次 = 1.5 小时纯 clone
2. 需要管 GC_TOKEN secret（GitHub secret scanning 已拦过我们 3 次）
3. GitCode 无 Actions API，无法用 GitHub 的并发/缓存
4. 前端不读 bulk，所以「网站显示」不受影响——但引擎成本暴涨

### 方案 B：K 线留 GitHub 独立仓（现状）

```
bulk-collect (三仓) → push 到 GitHub stocks-data-kline
                                    ↓
引擎要读 → actions/checkout(sparse-checkout, data/bulk) ≈ 15s
                                    ↓
前端读 data/*.json 摘要，完全不碰 bulk
```

优点：
1. sparse-checkout 已有，15s 拿到
2. 无需额外 secret
3. GitHub Actions 缓存可进一步降到 5s

### 方案 C：混合（GitHub 热数据 + GitCode 冷备）

- 最近 2 年 K 线在 GitHub（引擎常用）
- 全历史归档 .gz 到 GitCode（审计/回测用）

问题：跨仓一致性、双份存储、复杂度翻倍。

## 4. 决策：方案 B，GitCode 只做每日冷备归档

理由：
1. 主仓 pack 仅 748 KiB，K 线在独立仓，互不影响
2. 引擎读取成本：GitHub sparse-checkout 15s vs GitCode clone 40-90s，差 3-6 倍
3. 前端不读 bulk，两种方案对网站显示零差异
4. 少管一个 token，少一个故障点

GitCode 数据集仓定位改为：每日归档快照（daily/YYYY-MM-DD.json），
用于历史审计和「某天数据长什么样」的追溯，不作为引擎热数据源。

## 5. 行动清单

- [x] 确认主仓 pack 748 KiB（健康）
- [x] 确认 K 线在独立仓 stocks-data-kline
- [x] 确认前端不读 bulk
- [ ] dataset-archive.yml 保持每日归档（已存在）
- [ ] gitcode-sync.yml 降频：每 30 分钟 → 每日 1 次
- [ ] Actions 加 cache: data/bulk 加速 sparse-checkout

## 6. 如果未来 K 线超过 5GB 怎么办

1. GitHub Releases 存放 .gz（单文件上限 2GB，不限仓库大小）
2. Cloudflare R2（10GB 免费）
3. DuckDB + Parquet 压缩（integrate_stockdb.py 已支持）

当前 281MB，距离 5GB 还有 18 倍空间，不需要提前优化。
