# GitCode Space 24h 算力集群架构

> 目标: 24h 不间断实时获取/更新/分析数据, 覆盖 GitHub 网站的旧数据。
> 约束: Space 有"休息时间"(默认 30 分钟无活动休眠), 单组织 1000 核时/月。

## 一、核时现实账 (为什么必须多组织)

Space 计费: **CPU 核数 × 运行小时数**。休眠不计费。

| 配置 | 24h 常驻月耗 | 单组织 1000 核时 |
|---|---|---|
| 0.5 核常驻 | 0.5×24×30 = **360 核时** | 够 (剩 640) |
| 1 核常驻 | 1×24×30 = **720 核时** | 够 (剩 280) |
| 2 核常驻 | 2×24×30 = **1440 核时** | ❌ 超 44% |
| 2 核 × 3 组织 | 每组织 480 核时 | ✅ 每组织都富余 |

**结论**: 2 核常驻单组织撑不住, **3 个组织分摊最稳** (每组织 480/1000, 留 52% 余量)。

## 二、多组织分摊方案 (3 组织 × 1 个 Space)

```
组织A jiegengxiangjianxue/  组织B (待建)            组织C (待建)
┌──────────────────────┐   ┌──────────────────────┐ ┌──────────────────────┐
│ space: stocks-crawl  │   │ space: stocks-engine │ │ space: stocks-ai     │
│ 职责: 实时采集        │   │ 职责: 算法引擎        │ │ space: stocks-ai     │
│  · 行情/涨跌停/板块   │   │  · 9维因子/决策/估值  │ │ 职责: 复盘+AI+前端    │
│  · 新闻/热榜/资金流   │   │  · K线模拟/回测       │ │  · 复盘/胜率/反思     │
│  · 产出 raw_*.json    │   │  · 产出 analysis_*    │ │  · AI复盘/数据看板    │
│ 频率: 每5分钟         │   │ 频率: 每15分钟        │ │ 频率: 每小时         │
│ 核时: ~180/月        │   │ 核时: ~240/月         │ │ 核时: ~120/月        │
└──────────┬───────────┘   └──────────┬───────────┘ └──────────┬───────────┘
           │                          │                        │
           └──────────┬───────────────┴────────────────────────┘
                      ▼
        GitHub Actions (编排层, 不重算)
          · crawl 产出 → data/raw/
          · engine 产出 → data/
          · ai 产出 → data/
          · 全部 commit 到 GitHub → 网站 fetchJSON
```

### 为什么按职责分而非按数据量分
- **采集**最频繁(5分钟)但最轻, 单独一个 Space 保证实时性
- **引擎**最重(多维/决策/回测), 独占核时不被采集打断
- **AI+复盘**最不急(小时级), 顺带跑 LLM (GitCode AI 额度)

## 三、GitCode 数据集分流 (数据集仓, 非 Space)

数据集用 `repository_type=dataset` 创建, **不耗核时**, 只占存储。适合存历史数据。

| 数据集 | 内容 | 大小 | 用途 |
|---|---|---|---|
| `stocks-kline-daily` | 日K全史 (stock.duckdb 207万行) | 70MB | 引擎训练/回测 |
| `stocks-kline-minute` | 分钟线 | 大 | 盘中信号 |
| `stocks-news-archive` | 新闻归档 (news_digest 历史) | 增长中 | 消息面因子 |
| `stocks-valuation-daily` | 每日估值快照 | 中等 | 估值变迁追踪 |
| `stocks-prediction-log` | 预测对账历史 | 小 | 胜率统计 |

数据集与 Space 的协作:
```
Space (算力)  ──写──→  数据集仓 (存储, 零核时)
     ↑                        │
     └────── 读(回测取历史)───┘
GitHub Actions ──git fetch──→ 数据集仓 (备份/前端懒加载)
```

## 四、休眠问题的解法

Space 默认 30 分钟无 HTTP 访问就休眠。三种应对:

1. **保持轻量访问**: Space 内置 health endpoint, 由 GitHub Actions 每 20 分钟
   `curl` 一次 (space-pull.yml 顺带做) — 成本 0
2. **Gradio 面板本身就是访问点**: 面板有定时 auto_sync_loop, 有交互就不睡
3. **休眠唤醒只损失延迟不损失数据**: 唤醒后 `git pull` + 补跑引擎,
   产出带 ts 时间戳, 前端显示"数据年龄" — 已实现

## 五、GitHub 仓库分工

| 仓库 | 作用 |
|---|---|
| `qwerzxcva/stocks-ai-view` | 主仓: 前端 + CI 编排 + 数据落地 |
| `qwerzxcva/stocks-data-live` | 数据快照镜像 |
| `qwerzxcva/stocks-data-kline` | K线 bulk |
| `qwerzxcva/stocks-tools` | 工具脚本 |

GitCode 侧:
| 仓库 | 作用 |
|---|---|
| `jiegengxiangjianxue/stocks-ai-space` | Space: 采集+引擎+AI (首个, 已建) |
| `jiegengxiangjianxue/stocks-kline-daily` | 数据集: 日K全史 |
| `jiegengxiangjianxue/stocks-news-archive` | 数据集: 新闻归档 |
| 组织B/C Space | 待建 (采集/引擎分担) |

## 六、落地顺序

1. ✅ 组织A Space 已建 (`stocks-ai-space`)
2. ⬜ 创建数据集仓 (日K/新闻/估值/预测)
3. ⬜ 建组织B、组织C (需你在网页创建, 或提供授权)
4. ⬜ Space 内部分工: crawl/engine/ai 三模块
5. ⬜ GitHub Actions 编排: crawl→engine→ai 数据流

---

*核时估算基于 2 核配置; 实际按所选服务器规格浮动。不构成投资建议。*
