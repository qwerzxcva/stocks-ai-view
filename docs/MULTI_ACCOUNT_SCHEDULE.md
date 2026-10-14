# 多账号仓交叉调度 — 2 分钟级数据粒度

## 问题

GitHub Actions 的 `schedule` cron **最短间隔 5 分钟**（官方硬限制，无法绕过）：

> The shortest interval you can run scheduled workflows is once every 5 minutes.

单仓无论怎么配都是 5 分钟粒度。

## 解法：三仓错峰

三个 GitHub 账号各建一个仓，跑同一套 workflow，但 cron 落在不同的分钟位：

| 账号 | 仓 | cron（UTC，交易时段） | CST 时刻 | mod 5 |
|---|---|---|---|---|
| A `qwerzxcva` | `stocks-ai-view` | `2,7,12,...,57 1,2,3,5,6,7 * * 1-5` | :02 :07 :12 ... | 2 |
| B | `stocks-ai-view-b` | `4,9,14,...,59 1,2,3,5,6,7 * * 1-5` | :04 :09 :14 ... | 4 |
| C | `stocks-ai-view-c` | `0,5,10,...,55 1,2,3,5,6,7 * * 1-5` | :00 :05 :10 ... | 0 |

**任意 2 分钟窗口内必有一个仓刚跑完** → 前端依次读三个 raw 取 `ts` 最新的 → 有效 2 分钟粒度。

## 附带解决的两个隐患

### 1. 公开仓 schedule 60 天自动禁用

GitHub 会禁用公开仓库中 **60 天无任何活动**的 schedule。三仓各有真实提交活动（采集产出每天都在 push），且 B/C 仓的 push 会触发 A 仓的 `push` 事件工作流，互相作为对方的热源。

### 2. 单点故障

A 仓 workflow 挂了（token 失效、上游接口全挂），B/C 继续供数，前端无感切换。

## 前端如何读

`gh_sources.js` 已实现多仓降级 + 取最新：

```js
GHSources.fetchMulti("data/market_now.json")
  // → { data: {...}, source: "A"|"B"|"C", ts: "..." }
```

`index.html` 的 `fetchJSON()` 已接入：配了 B/C 仓时自动走多仓，未配时走原同源路径。B/C 仓建好后只需在 `gh_sources.js` 的 `REPOS` 数组填 owner/repo。

## B/C 仓需要的东西

每个仓都要有：

1. 同一套 `scripts/` + `.github/workflows/`（从 A 仓推过去）
2. 各自的 `secrets.GC_TOKEN`（如果也要同步 GitCode 镜像；不同步可省略）
3. GitHub Pages 开启（可选，B/C 只作数据源的话不必开）
4. **cron 错峰**（关键）：只改 `full-pipeline.yml` 里那行 `cron`

## 一个注意点

B/C 仓也会各自 commit 采集产出，三个仓的 git 历史会不同（各自的分支）。这是预期的——**前端只读 raw 文件内容，不关心 git 历史**。A 仓仍是代码唯一事实源，B/C 的 `scripts/` 是镜像，不要在 B/C 上手改代码。
