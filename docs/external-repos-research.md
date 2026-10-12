# 外部仓库借鉴与落地记录

> 记录从 4 个外部仓库/项目学到的算法、架构、数据源, 以及在本项目的落地情况。
> 目的: 让后续维护者知道每个设计决策的依据, 避免重复调研。

---

## 1. AIHOT (gitcode.com/gh_mirrors/ai/AIHOT)

**是什么**: Node.js 24 + TypeScript + React Router(SSR) + Fastify + PostgreSQL + pg-boss + Tailwind 的
行业热点聚合框架。112 commits, 640 后端测试 + 33 前端测试。MIT (名字与 Logo 不授权)。

### 借鉴并已落地

| 算法 | 原版做法 | 我们的落地 |
|------|---------|-----------|
| **热度算法** | 按事件算不按文章算; 48h 窗口内每个独立来源只算一次; 24h 减半; 同源多篇小幅加成 | `scripts/heat_algo.py` — `event_heat()` |
| **事件聚簇** | 标题摘要向量(降级为文字重合度) 找候选 → 模型判断同一件事/后续进展/两件事; 拿不准合并; 写入前复核 | `cluster_items()` — jaccard(词集) + 0.35×共同标的, 单链聚合, 回看 14 天 |
| **趋势标注** | 与 6 小时前比, 涨得快的标「上升」, 新出现的标「新」 | `annotate_trend()` — new/rising/falling/flat, 阈值 25% |
| **信源分级** | 三级 (官方一手 / 官方账号准官方 / 媒体个人), 各级入选门槛不同 | 待落地: 需先有多源 |

### 值得后续借鉴 (未落地)

- **双次独立评分**: 同一份评分标准独立打两次分再决定入选, 降低单次抽样噪声
- **防幻觉**: 防止模型把原文没提到的公司写进标题
- **SelectBench 校准**: 用自己标注的 100~200 条样本跑 `eval-selection.ts`, 看选得准不准再回去改标准
- **`industry/` 与 `site/` 分离**: 分类/主题/信源/提示词/门槛全部配置化, 改行业不用改代码
- **`modules/` 插件化**: 一个功能一个文件夹, 通过插口接入后端和网页, 框架本身不带模块
- **抓取频率按产出自动调整**
- **日报/周报/月报**: 日报不调模型按规则编; 周报月报从日报汇编, 模型只写总述
- **同一份内容给人看也给 Agent 看**: RSS/公开 API/MCP/Agent Markdown/llms.txt

### 架构启示

技术栈选型与我们的差异: 我们用 GitHub Pages + 静态 JSON (零运维), 它用 PostgreSQL + pg-boss (重但强)。
我们的场景 (展示型站点 + Actions 定时) 不需要数据库, 但其**进程分离**思想可借鉴:
采集进程 / 计算进程 / 服务进程分离, 各自独立部署与重启。

---

## 2. anthropics/financial-services (gitcode GitHub_Trending/fi/financial-services)

**是什么**: Anthropic 官方金融服务插件仓。290 star, 73 commits, Apache-2.0。
16 个命名 Agent (Pitch / Market Researcher / Earnings Reviewer / Model Builder / GL Reconciler /
Meeting Prep / Valuation Reviewer / Month-End Closer / Statement Auditor / KYC Screener),
5 个垂直插件 (financial-analysis / investment-banking / equity-research / private-equity / fund-admin),
177 个技能文件, 11 个 MCP 数据连接器。Python 38.81% / Markdown 38.45%。

### 借鉴并已落地

| 方法 | 原版做法 | 我们的落地 |
|------|---------|-----------|
| **数据源优先级** | MCP 数据源 > Bloomberg/SEC EDGAR > 其他; **NEVER 把 web search 当主要数据源** | 已在 `collect_market_extras.py` 落实: 腾讯/东财/同花顺 API 优先, 每个源标注 source |
| **公式而非硬编码** | 每个单元格是公式, 原始输入单独放, 且每个输入带来源注释 | `build_lowpool.py` / `rebuild_pools.py` 的评分全部由指标实时计算, 无硬编码分数 |
| **meta 元数据标注** | Notes & Methodology Documentation 为必填章节 | 所有数据块带 `meta: {source, ts, freq, rules, caveat}` |

### 值得后续借鉴 (未落地)

- **5-10 Rule**: 5 个经营指标 + 5 个估值指标 = 10 列, 超过 15 个指标就是噪音。→ 可用于精简我们网站的表格列
- **分位数统计块**: 每个指标都出 Max/75th/Median/25th/Min → 可用于低位池/打板池的横向分位
- **核心估值倍数**: EV/Revenue, EV/EBITDA, P/E, FCF Yield, PEG, P/B, ROE/ROA → 我们目前只有 PE/PB, 可补 EV 系
- **Cross-Reference Rule**: 估值倍数必须引用经营指标区, 同一份原始数据不输入两次
- **财报更新流程**: 先写今天日期 → 搜 latest → 验证发布日期在 3 个月内 → 才继续。→ 防旧数据
- **DCF/LBO/三表模型**: 49KB 的 DCF 方法论 (含 `validate_dcf.py` 校验脚本)

### 11 个 MCP 数据连接器情报 (需付费订阅, 免费额度待测)

```
daloopa       https://mcp.daloopa.com/server/mcp          基本面数据
morningstar   https://mcp.morningstar.com/mcp             晨星
sp-global     https://kfinance.kensho.com/integrations/mcp S&P Kensho
factset       https://mcp.factset.com/mcp                 FactSet
moodys        https://api.moodys.com/genai-ready-data/m1/mcp
mtnewswire     https://vast-mcp.blueskyapi.com/mtnewswires MT 新闻
aiera         https://mcp-pub.aiera.com                    财报电话会
lseg          https://api.analytics.lseg.com/lfa/mcp        LSEG
pitchbook     https://premium.mcp.pitchbook.com/mcp        一级市场
chronograph   https://ai.chronograph.pe/mcp                基金
egnyte        https://mcp-server.egnyte.com/mcp            文档
```

---

## 3. superdesigndev/treg (github.com)

**是什么**: "OpenRouter for agent tools"。5048 star, 96MB, 4313 文件, Python。
Agent 工具的注册/分发/计费平台。

### 借鉴情报 (未落地, 属平台架构)

**`docs/MARKET-DATA-CATEGORY-RESEARCH.md`** — 一份真实的市场数据 provider 调研:

| Provider | 价格 | 额度 | 鉴权 | 适用 |
|----------|------|------|------|------|
| Finnhub | ~$12-100/mo | **60 req/min** (最慷慨) | `X-Finnhub-Token` / `?token=` | 实时报价/基本面/新闻情绪 |
| Twelve Data | $29/mo | 55 credits/min, 日无限 | `?apikey=` | 股/汇/币 |
| Polygon(Massive) | $29/mo | 无限 (按资产类) | `?apiKey=` | 全资产 |
| Alpha Vantage | $49.99/mo | 75 req/min | `?apikey=` | 20万+ ticker |
| FMP | $15/mo | 平铺 | `?apikey=` | 基本面/财报 |
| Marketstack | $9.99/mo | 平铺 | `?access_key=` | 全球股价 |
| EODHD | $19.99/mo | 平铺 | `?api_token=` | 日线 |
| Tiingo | $30/mo | 免费档 500 符号/月 | `Authorization: Token` | 30年+ 历史 |
| FRED | 免费 (需 key) | — | `?api_key=` | 宏观经济 |
| SEC filings | 按量 | — | sec-api.io | 财报原文 |

**关键陷阱 (treg 实测)**: Alpha Vantage 用**垃圾 key 也返回 200 真实数据**, 连付费端点也返回 200 + upsell 提示
→ 无法校验用户粘贴的 key, 因此 treg 把它放进了 rejects。
CoinGecko demo host 和 Tiingo `/api/test` 也对垃圾 key 返回 200。

**定价模式差异**: 其他类目 (SEO/广告/社交) 是按次计费 (credits), 市场数据几乎全是
**月费平铺 + 限速 + 无限调用**。这解释了为什么我们做「按次调用」的网关不合适。

### 我们实测的免费源 (容器内可达)

| 源 | 状态 | 说明 |
|----|------|------|
| Alpha Vantage `demo` key | ✅ 可用 | 返回 IBM 真实报价; 25 次/天 |
| CoinGecko | ✅ 完全免费无需 key | BTC/ETH 等加密货币行情 |
| SEC EDGAR | ✅ 完全免费无需 key | 3.7MB 完整 XBRL 财报 (`data.sec.gov`) |
| Finnhub / TwelveData / FMP / Marketstack | ❌ 401 | 需注册 key |
| Yahoo Finance | ❌ 429 | 限流 |
| stooq | ❌ 404 | 路径变更 |

**结论**: 若后续要补美股行情/加密货币/财报, 优先用这三个免费源, 无需注册。

---

## 4. snomyc/stock (gitcode.com)

**是什么**: 基于 Python 的量化股票分析与交易系统。616 commits, Apache-2.0。
功能: 获取股票数据、计算股票指标、识别股票形态、内置选股策略、股票验证回测、股票自动交易、
支持 PC 及移动设备。

### 状态: 未能深入

- GitHub 上不存在 (404), 仅 gitcode 有 (gitcode 现名 AtomGit)
- gitcode 网页是 SPA, README 由 JS 渲染, 抓到的 HTML 只有 meta
- gitcode API (`web-api.gitcode.com`) 返回 418 反爬
- 代码文件浏览页提示"文件内容审核中"

**待跟进**: 需要浏览器登录 gitcode 后才能读源码。已知信息仅 README 描述。
从描述看, 它的「形态识别」和「验证回测」值得我们后续参考 (我们目前缺形态识别)。

---

## 落地清单 (本次)

- [x] `scripts/heat_algo.py` — AIHOT 聚簇+热度算法 (含趋势标注)
- [x] `collect_market_extras.py` 的 `commentary.hotspots` 接入新算法
- [x] `index.html` 题材 chips 显示热度/事件数/来源/标的
- [x] 全部数据块 meta 标注 (source/ts/rules/caveat)
- [ ] 信源分级 (官方一手/官方账号/媒体个人) — 需先接入多源
- [ ] 双次独立评分 — 需 LLM 接入
- [ ] SelectBench 样本校准 — 需人工标注 100~200 条
- [ ] EV/EBITDA/PEG/FCF Yield 估值倍数 — 需财报数据源
- [ ] 5-10 Rule + 分位数统计块 — 前端表格精简
- [ ] Alpha Vantage / CoinGecko / SEC EDGAR 免费源接入
- [ ] snomyc/stock 形态识别算法 — 需先能读到源码
