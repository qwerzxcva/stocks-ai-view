# 双平台架构：GitHub ↔ GitCode 互联

> 实测日期：2026-10-13 · API 能力以当日探测为准

## 一、平台分工

| 维度 | GitHub (`qwerzxcva/stocks-ai-view`) | GitCode (`dufuwuxin/stock-system`) |
|---|---|---|
| CI/CD | ✅ Actions（10 个工作流，采集/引擎/复盘/同步） | ❌ 无 Actions API（404） |
| 静态托管 | ✅ Pages（前端站点） | ❌ 无 Pages API（404） |
| 数据采集 | ✅ 主链路（腾讯/东财/新浪/抖音，并发合并去重） | ➖ 镜像接收 |
| 算法引擎 | ✅ 23 个 `scripts/`（engine_v5/multidim/decision/self_reflect…） | ✅ 25 个 `engines/` + 10 个 `algorithms/`（含 GitHub 没有的） |
| 云容器算力 | ❌ 无（runner 只有 6 小时） | ✅ 华为云容器（CLOUD_SETUP.md，可 crontab 常驻） |
| AI 额度 | GitHub Models（ghc，LLM 复盘） | GitCode AI（DeepSeek-V4-Flash/Qwen3-30B/GLM-5.1 等 8 模型） |
| 数据备份 | 主仓 | 异地镜像（`data/` 每 30 分钟同步） |

**核心原则**：CI 只能在 GitHub 跑（GitCode 无 Actions），GitCode 作为**数据镜像 + 第二算力 + 引擎补充来源**。

## 二、数据流向

```
                    ┌─────────────────────────────────────────┐
                    │      GitHub Actions (CI 主链路)          │
                    │  realtime-poll / full-pipeline / ...     │
                    └───────────────┬─────────────────────────┘
                                    │ 采集+计算
                    ┌───────────────▼─────────────────────────┐
                    │  data/*.json (31 个数据文件)              │
                    │  market_now / multidim / decision /      │
                    │  reflect / kline_sim / self_reflect ...  │
                    └───────┬───────────────────────┬─────────┘
                            │                       │
              ┌─────────────▼──────────┐   ┌────────▼─────────────┐
              │  GitHub Pages 前端     │   │  gitcode-sync.yml    │
              │  (fetchJSON 双路径:    │   │  每30min + push触发  │
              │   本地→GitHub raw兜底) │   └────────┬─────────────┘
              └────────────────────────└───────────│──────────────┘
                                                    ▼
                              ┌─────────────────────────────────────┐
                              │  GitCode dufuwuxin/stock-system     │
                              │  ├─ data/        (GitHub 数据镜像)  │
                              │  ├─ engines/     (25个引擎)         │
                              │  ├─ algorithms/  (10个算法)         │
                              │  ├─ analysis/    (12个分析输出)     │
                              │  └─ cloud/       (云容器产出)       │
                              └──────────────┬──────────────────────┘
                                             │ 云容器 crontab 常驻
                                             │ (CLOUD_SETUP.md)
                                             ▼
                              ┌─────────────────────────────────────┐
                              │  GitCode 云容器 (华为云)            │
                              │  · pull 最新代码+数据               │
                              │  · 跑 decision/strategy_card        │
                              │  · LLM 复盘 (GitCode AI 额度)       │
                              │  · commit + push 回 GitCode         │
                              └─────────────────────────────────────┘
```

**反向链路**（GitCode → GitHub）：
- GitCode 侧新增/修改的 `engines/*.py` → `gitcode-sync.yml` 步骤 B 拉回 GitHub `scripts/`
- GitCode 云容器产出的 `analysis/review_input/*` → 拉回 GitHub `data/gitcode_review/`
- GitHub Actions 16:00 用 LLM 复盘这些输入，回推 GitHub（形成闭环）

## 三、文件映射表

| GitHub `scripts/` | GitCode `engines/` | 说明 |
|---|---|---|
| `engine_v5.py` | `engine_fusion.py` | 多指标共振（GitHub 17指标 / GitCode 融合版） |
| `decision_engine.py` | `decision.py` | 决策引擎 |
| `kline_sim.py` | `kline_sim.py` | K线回测（两侧都有） |
| `self_reflect.py` | `review_sync.py` | 复盘（GitHub 证据驱动 / GitCode 同步版） |
| `llm_review.py` | `llm_review.py` | LLM 复盘（两侧都有） |
| `ban_pick.py` | `settle_bcard.py` | 打板结算 |
| — | `pick_tracker.py` | **GitCode 独有**：推荐闭环对账 |
| — | `loss_attribution.py` | **GitCode 独有**：四因子亏损归因 |
| — | `strategy_card.py` | **GitCode 独有**：策略卡 |
| — | `theme_to_stocks.py` | **GitCode 独有**：主题→个股 |
| — | `intraday_signal.py` / `intraday_ledger.py` | **GitCode 独有**：盘中信号/台账 |
| — | `moneyflow_scan.py` | **GitCode 独有**：资金流扫描 |
| — | `minute1_collect.py` | **GitCode 独有**：分钟线采集 |
| — | `algorithms/walkforward.py` | **GitCode 独有**： walk-forward 验证 |
| — | `algorithms/rps_rotation.py` | **GitCode 独有**：RPS 轮动 |

## 四、GC_TOKEN 配置（GitHub Actions 自动同步的前提）

1. 打开 https://github.com/qwerzxcva/stocks-ai-view/settings/secrets/actions
2. **New repository secret**
3. Name: `GC_TOKEN`，Value: `<GitCode 个人访问令牌>`
4. 保存后 `gitcode-sync.yml` 自动生效（未配置时工作流会 warning 跳过，不失败）

> 令牌在 GitCode 生成：https://gitcode.com/settings/tokens （需要 `read_api` + `write_repository` 权限）

## 五、手动同步命令

```bash
cd ~/workspace/stocks-ai-view

# 查看两侧文件差异（GitHub独有/GitCode独有/共有但不同）
python3 scripts/sync_gitcode.py --status

# 从 GitCode 拉取新增引擎到 scripts/
python3 scripts/sync_gitcode.py --pull-engines

# 推送 data/ + scripts/ 到 GitCode
python3 scripts/sync_gitcode.py --push-data
```

## 六、GitCode 云容器（第二算力）

见 GitCode 仓 `CLOUD_SETUP.md`。要点：

1. 网页打开 https://gitcode.com/dufuwuxin/stock-system → 「云开发」→ 华为云授权（一次）
2. 容器内 `pip install duckdb pandas pyarrow requests`
3. crontab 定时（GitCode 云开发环境常驻时）：
   ```cron
   # 每交易日 16:40 拉最新数据跑引擎
   40 16 * * 1-5 cd ~/stock-system && git pull && python3 engines/decision.py && python3 engines/strategy_card.py
   # 17:00 LLM 日报（消耗 GitCode AI 额度）并回写仓库
   00 17 * * 1-5 cd ~/stock-system && python3 engines/gitcode_daily.py && git add -A && git commit -m "cloud daily $(date +\%F)" && git push
   ```

**双活价值**：本机关机时，云容器仍能拉 GitHub 推来的数据跑引擎，产出回写 GitCode，再被 GitHub Actions 拉回 —— 两侧互为备份。

## 七、已知限制

| 限制 | 影响 | 规避 |
|---|---|---|
| GitCode 无 Actions | CI 不能跑 GitCode | 全部 CI 跑 GitHub，推镜像过去 |
| GitCode 无 Pages | 前端不能托管 GitCode | 前端只在 GitHub Pages；GitCode 纯数据/代码仓 |
| GitCode 无公开 rate_limit API | 无法程序化查配额 | 靠网页看「AI开发→资源用量」 |
| GitCode 历史可能与 GitHub 分叉 | push 冲突 | `gitcode-sync.yml` 用 `--force-with-lease` + `merge -X ours` |

---

*本文档由 2026-10-13 实测 API 能力后编写。不构成投资建议。*
