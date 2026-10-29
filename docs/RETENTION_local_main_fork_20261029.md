# 本地 main 与 origin/main 分叉的保留策略（2026-10-29）

仓库：`github.com/qwerzxcva/stocks-ai-view`。本文件随 `salvage/on-origin-main-20261029` 分支保存，不进入 main。

## 1. 处理时的分叉快照

- 本地 `main` 相对 `origin/main`：**ahead 14 / behind 3**（处理时点）。
- 远端 main 历史在 11:36 被第三方改写（`git rev-list --count origin/main` 只剩 3 个提交，根提交 `0fd66824` 一次性加入全部 workflow），本地提交的哈希因此不在远端历史中，但**改动内容在线**。
- 工作树当时有 9 个已跟踪文件未提交改动 + 1 个未跟踪文件 `.github/workflows/manifest-heal.yml`（其他代理的在制品）。

## 2. 三重备份

| 形式 | 位置 | 内容 |
|---|---|---|
| 分支 | `backup/local-main-20261029`（= `8782cc4c`） | 14 个本地提交全部可达 |
| bundle | `~/shared/backup-20261029/local-main-20261029.bundle`（71MB） | 含完整父链，可离线恢复 |
| patch | `~/shared/backup-20261029/uncommitted-9files.patch` | 9 个未提交改动的完整 diff |
| 未跟踪件 | `~/shared/backup-20261029/manifest-heal.yml.untracked` | `manifest-heal.yml` 副本 |

```bash
# 从 bundle 恢复（不需要网络）
git fetch ~/shared/backup-20261029/local-main-20261029.bundle main:restored/local-main
# 恢复未提交改动
git apply ~/shared/backup-20261029/uncommitted-9files.patch
```

## 3. 14 个本地提交的逐个处置

判定方法：把每个提交的「新增代码行」逐条在 `origin/main` 树上做子串比对（`git archive origin/main` 导出后比对；样本条件：新增行长度 >= 18 且非注释）。

### A. 内容已在线（10 个）—— 无需推送，可弃

| 提交 | 说明 | 在线证据 |
|---|---|---|
| `3915622b` | engine-chain 契约闸门前移 | 新增行 5/5 命中；远端 workflow 第 593 行 |
| `8f9fc747` | 引擎清单纳入管线发布 | 1/1；远端 publish 段已含 `data/engine_manifest.json` 白名单与 `--protect` |
| `8f74b16b` | golden_v5 拒绝劣化快照重建 | 70/71（唯一未命中是时间戳字段） |
| `2647859d` | 端上仓取不到时硬失败 | 4/4 |
| `ec1cb33f` | 端上仓 Actions 探针 | 16/16 |
| `589d4af6` | 逐用例测试矩阵 | 80/80 |
| `a7ca570f` | pools 数据快照 | 80/80 |
| `737f78a7` / `83abc786` / `2e94ed82` | docs 与 data 快照提交 | 数据类，无需比对 |

### B. 内容未在线（4 个）—— 已移植到可推送分支

| 原提交 | 移植后 | 主题 |
|---|---|---|
| `5d869247` | `a8803ada` | 探针追查 android-build 失败步骤与日志 |
| `b0766eb5` | `0d233ac6` | 探针只抓 FAIL/WARN/结论行 |
| `5ae27a46` | `bd211866` | 探针同时列 releases |
| `8782cc4c` | `ae1512a3` | 探针可对端上仓触发一次 android-build |

分支 `salvage/on-origin-main-20261029`（tip `ae1512a3`）基于 `origin/main` 逐提交 cherry-pick，**4/4 干净应用、零冲突**。

## 4. 本地 main 为什么不 reset 到 origin/main

`git reset --mixed origin/main` 不动工作树，但工作树相对新 HEAD 的差异是 **50 个文件 / +11239 / −10597**（本地 data 快照整体落后远端两个数据提交）。重置后这些旧快照会以「待提交改动」形态出现，任何代理下一次 `git commit -a` 就会把旧 data 推回远端、覆盖新数据；`git reset --hard` 又会抹掉 9 个未提交改动。两条路都违反「保留他人改动」前提，因此 **main 维持原样**（与自身工作树自洽），分叉由本文件的备份与 `salvage/*` 显式承载。

## 5. 今后推向远端 main 的唯一安全路径

远端 main 的 data 快照比本地新，**禁止在本地 main 上直接 commit + push**（会把旧快照回滚）。一律走干净 worktree + cherry-pick：

```bash
cd <repo>
git fetch origin
git worktree add /tmp/push_wt --detach origin/main   # 干净树，远离他人未提交改动
cd /tmp/push_wt
git cherry-pick <你的提交>                            # 只搬代码改动，data 快照留在本地
git push origin HEAD:main                           # fast-forward，不 force
cd <repo> && git worktree remove --force /tmp/push_wt
```

`3915622b`（契约闸门）就是这样推上去的（`c707768d..560f266a`）。

## 6. 本次遵守的红线

- 未执行 `git reset --hard` / `git stash` / `git checkout -- .`：其他代理的 9 个未提交改动与 1 个未跟踪件全程未被触碰（其中 4 个在同窗口被其主人继续编辑，mtime 可证：`data/parity/MANIFEST.json` 12:14:58、`golden_21.json` 12:14:41、`golden_v5.json` 12:14:35、`scripts/engine_spec.py` 12:08:46，均晚于 11:55 的备份时点）。
- 未 force push；未删除 `backup/*` 与 `salvage/*` 分支。
- 临时 worktree 用精确路径创建与删除，不用通配符。
