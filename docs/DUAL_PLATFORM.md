# 双平台架构：GitHub ↔ GitCode 互联

> 实测日期：2026-10-13 · 两侧文件数以当日 `scripts/sync_gitcode.py --status` 输出为准

## 一、平台分工

| 维度 | GitHub (`qwerzxcva/stocks-ai-view`) | GitCode (`dufuwuxin/stock-system`) |
|---|---|---|
| CI/CD | ✅ Actions（11 个工作流，采集/引擎/复盘/同步） | ❌ 无 Actions API（探测返回 404） |
| 静态托管 | ✅ Pages（前端站点） | ❌ 无 Pages API（404） |
| 数据采集 | ✅ 主链路（腾讯/东财/新浪/抖音，并发合并去重） | ➖ 镜像接收 |
| 算法引擎 | ✅ 61 个 `scripts/*.py`（engine_v5/multidim/decision/self_reflect…） | ✅ 25 个 `engines/` + 10 个 `algorithms/` |
| 云容器算力 | ❌ 无（runner 只有 6 小时） | ✅ 华为云容器（CLOUD_SETUP.md，可 crontab 常驻） |
| AI 额度 | GitHub Models（LLM 复盘） | GitCode AI（多模型，供云容器日报） |
| 数据备份 | 主仓 | 异地镜像（`data/` 由 workflow 步骤 A/C 同步） |

**核心原则**：CI 只能在 GitHub 跑（GitCode 无 Actions API），GitCode 作为**数据镜像 + 第二算力 + 引擎补充来源**。两侧脚本流向是双向的：GitHub 全量推，GitCode 独有引擎按需回流。

## 二、数据流向

```
        ┌──────────────────────────────────────────────────────────────┐
        │                GitHub Actions (CI 主链路, 唯一 CI)            │
        │  realtime-poll / full-pipeline / multi-tier / llm-review ...  │
        └───────────────┬──────────────────────────────┬───────────────┘
                        │ 采集 + 计算                  │ 步骤 B/D 回推
        ┌───────────────▼──────────────────────┐   ┌───▼────────────────┐
        │  data/*.json (36 个数据文件)          │   │  scripts/*.py      │
        │  market_now / multidim / decision /   │   │  (GitCode 独有引擎 │
        │  reflect / kline_sim / lowpool ...    │   │   回流后 +91 个)   │
        └───────┬───────────────────┬──────────┘   └───▲────────────────┘
                │                   │                  │
   ┌────────────▼──────────┐  ┌─────▼──────────────────┴───────────────┐
   │  GitHub Pages 前端    │  │  .github/workflows/gitcode-sync.yml    │
   │  (fetchJSON 双路径:   │  │  · 步骤 A  push data/      (30min+push) │
   │   本地→GitHub raw兜底)│  │  · 步骤 B  pull engines/   → scripts/   │
   └───────────────────────┘  │  · 步骤 C  push scripts/   → engines/  │
                              │  · 步骤 D  pull analysis/  → data/gc   │
                              └───────┬──────────────────┬─────────────┘
                                      │ push (force-with-lease)
                                      ▼                  ▲ merge -X ours
        ┌────────────────────────────────────────────────────────────────┐
        │  GitCode  dufuwuxin/stock-system  (公开仓, 56 个文件)           │
        │  ├─ engines/     25 个引擎                                     │
        │  ├─ algorithms/  10 个算法                                     │
        │  ├─ analysis/    12 个产出 (含 review_input/latest.json)       │
        │  ├─ cloud/        4 个云容器产出                               │
        │  └─ CLOUD_SETUP.md (云容器部署说明)                            │
        └───────────────────────────┬────────────────────────────────────┘
                                    │ 云容器 crontab 常驻 (华为云)
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  GitCode 云容器                                               │
        │  · git pull 最新代码 + GitHub 镜像来的 data/                   │
        │  · 跑 engines/decision.py / strategy_card.py / cloud_daily.py  │
        │  · 产出写回 analysis/ + cloud/                                 │
        │  · commit + push 回 GitCode → 再由步骤 B/D 回流 GitHub          │
        └────────────────────────────────────────────────────────────────┘
```

**反向链路**（GitCode → GitHub）：
- GitCode 侧新增的 `engines/*.py` / `algorithms/*.py` → 步骤 B 拉到 `scripts/` 并 commit 回 GitHub
- GitCode 云容器产出的 `analysis/review_input/*`、`analysis/cloud/*` → 步骤 D 拉到 `data/gitcode_review/`
- GitHub Actions 的 LLM 复盘消费这些输入，形成闭环

## 三、本地同步工具

`scripts/sync_gitcode.py` 是 workflow 的手动等价物，用于本机排查与一次性批量操作。只走 `subprocess` + git，不依赖 `requests`。

| 命令 | 作用 | 对应 workflow 步骤 |
|---|---|---|
| `--status` | 对比两侧文件清单：GitHub 独有 / GitCode 独有 / 共有但不同（脚本与数据分开统计） | 无（纯查询） |
| `--pull-engines` | 把 GitCode `engines/` + `algorithms/` 里 GitHub 没有的 `.py` 拉到 `scripts/`，文件头标注来源与上游 commit | 步骤 B |
| `--push-data` | 把 `data/*.json` + `scripts/*.py` 推到 GitCode（后者落到 `engines/`），基于 GitCode main 建临时 worktree，不污染当前分支 | 步骤 A + C |

设计要点：
- **不覆盖已有文件**：`--pull-engines` 只拉 GitHub 缺失的引擎，共有文件即使内容不同也不动，避免把 GitHub 的改进版覆盖回上游旧版。
- **临时 worktree**：`--push-data` 用 `git worktree add --detach` 基于 `gitcode/main` 建独立工作树，提交只在 worktree 内发生，当前分支的 HEAD/暂存区不受影响。
- **token 脱敏**：所有回显的命令都会把 token 替换成 `***`，不会泄漏到日志。
- **冲突策略**：push 一律 `--force-with-lease`；被拒时先 `fetch` + `merge -X ours`（保留 GitHub 侧内容）再重试，仍失败则明确报错退出，不静默。

## 四、GC_TOKEN 配置（自动同步的前提）

GitHub Actions 侧：

1. 打开 https://github.com/qwerzxcva/stocks-ai-view/settings/secrets/actions
2. **New repository secret**
3. Name: `GC_TOKEN`，Value: `<GitCode 个人访问令牌>`
4. 保存后 `gitcode-sync.yml` 自动生效

未配置时：workflow 的 `Prepare GitCode remote` 步骤会 `::warning::` 提示并写入 `GC_SKIP=1`，A/B/C/D 四个步骤全部跳过，**工作流本身成功退出**（不 fail），不影响其他 CI。

> 令牌在 GitCode 生成：https://gitcode.com/settings/tokens，需要 `read_api` + `write_repository` 权限。
> 本地跑 `sync_gitcode.py` 时优先读环境变量 `GC_TOKEN`；未设置则用脚本内默认 token。

## 五、手动同步命令

```bash
cd ~/workspace/stocks-ai-view

# 1. 先看两侧差异（三类清单 + 统计，只读不动文件）
python3 scripts/sync_gitcode.py --status

# 2. 从 GitCode 拉取新增引擎到 scripts/（只拉缺失的，不覆盖）
python3 scripts/sync_gitcode.py --pull-engines

# 3. 推送 data/ + scripts/ 到 GitCode（data/ 原样，scripts/ → engines/）
python3 scripts/sync_gitcode.py --push-data

# 指定 token（不满足于默认值时）
GC_TOKEN=<token> python3 scripts/sync_gitcode.py --status
```

典型排查顺序：`--status` 看清差异 → `--pull-engines` 拉上游新引擎 → 本地验证引擎可跑 → `--push-data` 把验证过的全量推回 GitCode。

**回流引擎接入注意**：GitCode 侧脚本常用 `~/stockdb` 等自有路径约定，本项目用 [scripts/paths.py](scripts/paths.py) 统一解析。拉下来后先读文件头标注的来源路径，再按需适配 `paths.py`，不要直接假定目录结构一致。

## 六、GitCode 云容器（第二算力）

见 GitCode 仓根目录 `CLOUD_SETUP.md`。要点：

1. 网页打开 https://gitcode.com/dufuwuxin/stock-system →「云开发」→ 华为云授权（一次）
2. 容器内 `pip install duckdb pandas pyarrow requests`
3. crontab 定时（云开发环境常驻时）：
   ```cron
   # 每交易日 16:40 拉最新数据跑引擎
   40 16 * * 1-5 cd ~/stock-system && git pull && python3 engines/decision.py && python3 engines/strategy_card.py
   # 17:00 云容器日报（消耗 GitCode AI 额度）并回写仓库
   00 17 * * 1-5 cd ~/stock-system && python3 engines/cloud_daily.py && git add -A && git commit -m "cloud daily $(date +\%F)" && git push
   ```
4. 回写的 `analysis/` + `cloud/` 由 workflow 步骤 D 自动拉回 GitHub `data/gitcode_review/`

**双活价值**：本机关机时，云容器仍能拉 GitHub 镜像来的数据跑引擎，产出回写 GitCode，再被 GitHub Actions 拉回 —— 两侧互为备份。

## 七、已知限制

| 限制 | 影响 | 规避 |
|---|---|---|
| GitCode 无 Actions | CI 不能跑 GitCode | 全部 CI 跑 GitHub，推镜像过去 |
| GitCode 无 Pages | 前端不能托管 GitCode | 前端只在 GitHub Pages；GitCode 纯数据/代码仓 |
| GitCode 历史可能与 GitHub 分叉 | push 冲突 | `--force-with-lease` + `merge -X ours` |
| 两侧同名脚本可能不同版本 | `--push-data` 会用 GitHub 版覆盖 | push 前先 `--status` 看「共有但不同」清单，确认覆盖是否有意 |
| GitCode 侧路径约定不同 | 回流引擎直接跑会报错 | 读文件头 + 适配 `paths.py` 后再接入 |

---

*本文档与 `scripts/sync_gitcode.py`、`.github/workflows/gitcode-sync.yml` 同步维护。不构成投资建议。*
