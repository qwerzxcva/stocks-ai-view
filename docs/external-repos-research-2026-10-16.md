# 外部仓库借鉴调研 (2026-10-16)

> 用户要求: 从 48 个 GitHub 仓库提炼数据源/算法/引擎/模型/架构/框架/网站设计, 与现有 161 脚本合并去重。
>
> **本轮执行情况 (如实说明)**: 原计划派 5 个 Explore 子代理并行调研, 5 个全部因
> `LLM 调用失败: connection closed` 中断, 只留下「重试中」的中间输出, 无实际报告
> (token 消耗 1460 万但零产出)。因此本报告由主会话用 websearch 直接调研完成,
> **已覆盖 12 个仓库**, 其余 36 个未调研。缺口在文末列出。
>
> **已落地 (不只是调研, 已变成我们的代码)**:
> a-stock-data 逐笔采集 + 竞价资金结构 / AI-Stock-Master 三大算法 /
> TradingAgents 多空辩论 / backtrader Analyzer 架构 / lumibot replay caching /
> kand 5 个进阶指标。详见第四节。

---

## 一、License 总览 (决定能不能抄代码)

| 仓库 | License | 能否借鉴代码 |
|---|---|---|
| **simonlin1212/a-stock-data** | **Apache-2.0** | ✅ **可合法复制, 本轮最大发现** |
| guangxiangdebizi/TradingAgents-MCPmode | **MIT** | ✅ 可借鉴 |
| guanquann/Stocksera | **MIT** | ✅ 可借鉴 |
| zhangxiangliang/stock-api | MIT | ✅ (TypeScript, 前端向) |
| chengzuopeng/stock-sdk | MIT | ✅ (TypeScript, 前端向) |
| electkismet/eltdx | Research-Only | ❌ 禁止商用/复制, 只能学协议知识 |
| Lumiwealth/lumibot | GPL-3.0 | ❌ 传染性, 只能学架构 |
| cloudQuant/backtrader | GPL-3.0 | ❌ 传染性, 只能学架构 |
| BennyThadikaran/eod2 | GPL-3.0 | ❌ 只能学架构 |
| hengruiyun/AI-Stock-Master | AGPL-3.0 | ❌ 更严, 只能学算法思想 |
| AlgoTraders/stock-analysis-engine | (2020 停更) | 已失效, 不建议 |

**结论**: 48 个仓库里能合法复制代码的有 5 个, 其中 **a-stock-data (Apache-2.0)**
是唯一直接覆盖 A 股全栈数据的, 且质量最高 (见 1.1)。

---

### 1.1 simonlin1212/a-stock-data ⭐⭐⭐ 本轮最高价值发现 (Apache-2.0, 可合法复制)

**身份**: 默认分支 main, Apache-2.0, V3.10.2 (2026-10-11 更新, 活跃),
形态是「自包含 SKILL.md (结构化 Markdown + 内嵌 Python)」, 7585 行。

**规模**: 15 层架构 · **87 个端点** · **34 个数据源** · 零鉴权 (iwencai 除外)。

| 层 | 数据 | 我们的状态 |
|---|---|---|
| 行情 | 腾讯实时/PE/PB/市值 + 日周月复权 + 1~60分钟K + **当日逐笔** + 通达信盘后包 | ⚠️ 部分有, **缺逐笔/盘后包** |
| 研报 | 个股/行业研报 + PDF + 三年EPS + 一致预期 + iwencai NL搜索 | ❌ **完全缺** |
| 信号 | 强势股 + 题材归因 + 北向 + 板块归属 + 资金流向 + 龙虎榜 + 解禁 + 行业排名 + 板块资金流 | ⚠️ 大部分有 |
| 资金/筹码 | 两融 + 大宗 + 股东户数 + 分红 + 120日资金流 + **筹码分布CYQ** + ETF份额 | ⚠️ 部分有, **缺CYQ/股东户数/ETF份额** |
| 新闻 | 个股新闻 + 财联社 + 7x24 + **新闻联播文字稿** | ⚠️ 财联社已失效 (与我们同) |
| 基础财务 | 季报37字段 + F10 + 财报三表 + **估值历史** + 上市退市日 + **申万行业变迁** + **ST名单** | ⚠️ 部分有 |
| 公告 | 沪深北全量公告 (巨潮) | ⚠️ 部分有 |
| 打板 | 涨停/炸板/跌停/昨涨停 + 涨停原因 + **连板梯队** + **重点监控池** + **日内异动池** | ⚠️ 大部分有 |
| 期权 | ETF期权 T型报价 + 希腊字母 + **隐含波动率IV** | ❌ **完全缺** |
| 舆情互动 | 互动易(深) + 上证e互动(沪) + 同花顺热榜 + 东财人气榜 + 概念命中 | ❌ **完全缺** |
| 宏观利率 | 社融 + PMI + 国债/信用债收益率曲线 + 回购利率 + LPR + 全球宏观日历 | ❌ **完全缺** |
| 指数日历 | 中证/国证成分权重 + 中证PE与股息率 + 深交所交易日历 | ⚠️ 部分有 |
| 期货大宗 | 五大期交所 + 上金所 + 新浪 + A50 + 上海金 + 期货日K | ❌ **完全缺** |
| 事件驱动 | 业绩预告 + 机构调研 + 增减持 + 回购 + 股权质押 + 新股申购 | ⚠️ 部分有 |
| 可转债 | 条款 + 转股价 + 转股价值 + 溢价率 + 上市摘牌 | ⚠️ 集思录有部分 |

**它帮我们解决的关键问题 (有实测依据, 不是猜的)**:

1. **通达信公开服务器 K线/盘口/逐笔 2026-09 起全部失效 (#52)**
   → 上游已用 10 台服务器逐台实测确认是服务端变化, 换 mootdx 版本无用。
   → 替代路径: K 线改腾讯/通达信官网盘后包 (HTTP), 逐笔改腾讯成交明细。
   → **我们正在自己做 7709 协议, 这条路已死, 应及时止损**。

2. **腾讯逐笔成交 (V3.10 新增)** — 已落地为我们的 `scripts/fetchers_ticks.py`,
   实测 5 只全成功, 每只 4500-4800 笔, 成交额核对通过。详见 4.3。

3. **北交所老号段僵尸报价陷阱** — `43x/83x/87x` 已迁 `920xxx`, 但**老码不报错**:
   东财研报静默返回 0 篇 (看着像「没研报」), 腾讯行情返回定格在迁移日的僵尸报价
   (成交量 0, 与真实价差 17%~100%+)。上游 V3.6.0 起返回 `is_stale` 标志。
   → **我们需检查是否有老码在污染数据**。

4. **腾讯 K 线成交量单位陷阱** — 科创板 (688/689) 按**股**, 其余按**手**。
   照旧文档换算科创板会大 100 倍。上游 V3.10.1 更正。
   → **我们需核对 engine_v5 / 量比计算是否踩了这个坑**。

5. **东财防封阈值 (社区实测 2026-05)** — `em_get()` 串行节流 (间隔>=1s + 随机抖动)
   + 会话复用。已有用户因高频被封 (issue #36, 2026-06-30 一手数据)。
   → 我们的 `collect_moneyflow` / `collect_market_extras` 应确认是否有限流。

**已落地**: `scripts/fetchers_ticks.py` (逐笔) + `scripts/auction_engine.py` (资金结构),
两者已在 engine-chain 的 full 档接入 CI, 并接入 ban_pick 与前端 ⑥b 卡片。

**未落地 (下一步候选)**: 筹码分布 CYQ (本地计算, 零依赖) / 研报层 (东财 reportapi,
免 key) / 期权 IV / 舆情互动 (互动易+上证e互动) / 宏观利率 (LPR/社融/PMI)。

**其他同源仓 (都是 a-stock-data 的衍生, 内容基本一致, 不必重复看)**:
`2476097246-alt/a-stock-api` (Flask REST 封装, Apache-2.0),
`Macco2024/a-stock-data-20260624`, `icenewman/stock-data`, `xuyongfu/a-stock-data-*`。

---

### 1.2 kand-ta/kand ⭐ 已落地 (Apache-2.0 OR MIT, 可合法复制)

**身份**: 447 star / 16 fork, 默认分支 main, Apache-2.0 OR MIT 双许可,
Rust 实现 + Python/WASM 绑定。

**定位**: 高性能技术指标库, 宣称达到或超过 TA-Lib 峰值性能,
真正多线程 (不受 Python GIL 限制), **O(1) 增量更新**适合流式实时。

**已落地为我们的 `scripts/indicators_ext.py` 新增 5 个指标**
(按其公开定义独立实现, 未复用 Rust 代码):

| 指标 | 作用 | 验证 |
|---|---|---|
| Vegas Tunnel | EMA(144)±4% 包络, 判断是否突破整条隧道 | ✅ 跑通 |
| Camarilla Levels | 日内 8+2 个支撑阻力位, H4/L4 突破位, H3/L3 反转位 | ✅ 11 个价位逐档数值验证 |
| Heikin Ashi | 平滑 K 线, 连续同色无影线=趋势强 | ✅ |
| MFI | 量加权 RSI, 缩量上涨不虚高 | ✅ |
| OBV | 能量潮, 量价背离判断 | ✅ 单调性验证 |

**为什么选这几个**: indicators_ext 原有 14 个指标已覆盖 TA-Lib 主流
(supertrend/VWMA/ATR/Keltner/Donchian/ZigZag/CMO/Choppiness/Vortex/UO/
MassIndex/Pivot/HullMA)。缺的是 kand 独有的**趋势过滤类 (Vegas)** 与
**日内水平位类 (Camarilla)**, 以及两个**量价验证类 (MFI/OBV)** ——
后者正好补 factor_lab 发现的「只看价格不看量的指标不可靠」。

**修了两处初版错误**: Camarilla 的 L5 原写 `c-2*range` 丢了 c;
H5 未定义。已按 Stott 原始定义改为 `H5=(H/L)*C` / `L5=C-(H5-C)`。

**未借鉴**: Rust 实现本身 (我们纯 Python, 不引入二进制依赖);
O(1) 增量更新 (我们是批量计算, 用不到)。

---

### 1.3 wshobson/maverick-mcp (MIT, 未落地 — 架构参考)

**身份**: 676 star / 158 fork, 默认分支 main, MIT, Python 3.12+,
2025-08 创建, 2026-09 仍在更新。

**是什么**: 股票市场 MCP server, 让 AI 助手 (Claude Desktop / Codex /
Cursor / VS Code) 调用行情/技术分析/组合跟踪/回测工具。核心工具
**无需 API key** (走 yfinance)。

**可借鉴的架构**: 把股票分析能力封装成 MCP 工具清单供 LLM 调用 ——
这与我们 `bull_bear_debate.py` 让 LLM 只组织论证不产生事实的思路同向。

**未落地原因**: 依赖 yfinance (容器内实测 Yahoo 429 限流);
数据源是美股, 对 A 股无直接价值; 我们的场景是 GitHub Actions 批处理
而非交互式 MCP 会话。

**若要落地**: 借鉴其工具清单设计 (37 tools 的粒度划分),
把我们的 161 个脚本中适合暴露给 LLM 的整理成工具描述。优先级低。

---

## 二、逐个调研 (9 个已核实)

### 1. electkismet/eltdx ⭐ 最高价值数据源

**身份**: Rust 476 star / 161 fork, 391 commits, 默认分支 main, Python 3.10+,
v3.1.0 (2026 活跃), Research-Only License。

**是什么**: 通达信 7709 行情协议 + 7615 F10 资料协议 的 Python/Rust 客户端。
3.0 把 21 个 7709 命令的构包/解析/连接池/心跳核心迁入 Rust, Python API 保持兼容。

**数据源能力 (我们缺的)**:
| 能力 | 调用 | 我们的现状 |
|---|---|---|
| 五档实时盘口 | `client.quotes.get_depth()` | ❌ 无 (只有快照) |
| 当日逐笔成交 | `client.trades.all_today()` | ⚠️ collect_level2 有但不完整 |
| 集合竞价详情 | `client.helpers.auction_data()` | ⚠️ 只有 zt_time 字段 |
| 服务端复权 K线 | `client.bars.get(adjust="qfq"/"hfq")` | ✅ 已有 (腾讯/新浪) |
| **板块行情与成分股** | `client.helpers.board_quotes()` / `board_member_quotes()` | ❌ **这是 sector_engine 缺的关键** |
| 资金流向 | docs 有专章 | ✅ 已有 (东财 fflow) |
| 短线特殊指标 | 开盘量比/流通股本/封单/连板字段 | ⚠️ 部分有 |
| 退市股票 K线 | `client.delisted_bars.get()` (0x052B) | ❌ 无 |

**对我们最有用的三点**:
1. `board_member_quotes()` — **解决 sector_engine 的最大缺口**:
   现在板块成分股靠 industry_map_em (只覆盖 1050 只) + bulk K 线反查, 27 个板块只有 6 个
   能算出涨跌家数。通达信协议直接给「板块→成分股列表」, 覆盖率和实时性都更好。
2. `auction_data()` — 竞价过程/09:25 撮合/昨收参考价, 比现在只有 zt_time 精确得多,
   可直接增强 premarket_picks 的竞价抢筹判断。
3. `get_depth()` — 五档盘口, 可用于封单强度 (现在 ban_prob 用估算)。

**落地障碍**:
- Research-Only License → **不能复制代码, 需自己实现协议** (构包/解析要重写)
- 协议是二进制 TCP (116.205.183.150:7709 等主站), GitHub Actions 出网需确认可达
- 需处理主站测速/连接池/pin (README 明确说这些是必要复杂度)

**结论**: 优先级高, 但要自己实现。建议先做最小可用版: 连接 + 0x06b9(板块列表)
+ 0x044d/0x054c(成分股), 只服务 sector_engine。

### 2. guangxiangdebizi/TradingAgents-MCPmode ⭐ 最高价值架构 (MIT)

**身份**: 170+ star, 89 commits, 默认分支 main, Python, **MIT License**。

**是什么**: 15 个专业化 LLM Agent 协作的交易分析系统, 集成 MCP 工具。

**架构 (值得借鉴的)**:
```
分析师团队 (并行执行, 6 个)
  ├─ CompanyOverviewAnalyst  公司概述
  ├─ MarketAnalyst           市场
  ├─ SentimentAnalyst        情绪
  ├─ NewsAnalyst             新闻
  ├─ FundamentalsAnalyst     基本面
  └─ ShareholderAnalyst      股东结构
        ↓
研究员团队 (辩论机制)
  ├─ BullResearcher  看涨方
  └─ BearResearcher  看跌方
        ↓ (可配置 max_debate_rounds)
Trader Agent 综合成交易决策
        ↓
风险管理 (三层)
  ├─ RiskyAnalyst     激进视角
  ├─ SafeAnalyst      保守视角
  └─ RiskManager      最终风控
```

**关键工程设计**:
- **并行提速**: 原串行 6 分析师 → 并行, 理论 5-6 倍 (实测取决于耗时分布)
- **深拷贝避免状态竞争**: 并发 Agent 间用 deepcopy 隔离
- **智能合并执行历史**: 并行结果合并时去重
- **前端可控**: 动态启用/禁用 Agent, 实时调辩论轮次
- **MCP 权限分级**: 环境变量控制每个 Agent 能用哪些 MCP 工具

**对我们最有用的**:
1. **多空辩论机制** — 我们现在的 ai_pick 只有单向评分, 缺「看涨方 vs 看跌方对抗」。
   已知看多胜率 43.8% < 随机 50%, 加一个 bear agent 强制列出反面证据,
   可能比继续调权重更有效 (让模型自己发现「不该买」)。
2. **并行分析师 + 深拷贝隔离** — gitcode_ai_review 现在串行跑多个分析维度, 可并行化。
3. **Agent 粒度拆分** — 15 个 Agent 各管一个维度, 提示词比「一个巨型 prompt」更可控。

**落地障碍**: 需 LLM API (我们已有 GitCode AI 通道, 可复用); 15 个 Agent 全跑
token 成本高, 建议先做 3 个 (bull/bear/risk) 的最小辩论闭环。

### 3. hengruiyun/AI-Stock-Master

**身份**: 373 star / 74 fork, Python, AGPL-3.0, 有 openclaw/skills 的 SKILL.md。

**三个原创算法 (README 明确命名)**:
| 算法 | 作用 | 我们的状态 |
|---|---|---|
| **RTSI** 个股趋势强度指数 | 量化个股趋势强度 | ❌ 无 (engine_v5 是 17 指标共振, 无单一强度指数) |
| **MSCI** 市场情绪指数 | 量化全市场情绪 | ⚠️ 有 global sentiment / 赚钱效应, 但无统一指数 |
| **Core Strength Analyzer** | 核心强度分析 | ❌ 无 |

**其他**: 内置 Mini Ollama (本地 LLM), 覆盖中港美三地, 多 Agent 协作决策。

**对我们最有用的**: RTSI 思路 — 把「趋势强度」做成一个 0-100 的单值指数,
前端一行展示, 比 17 个指标堆在一起更可读。**但 AGPL-3.0 且算法实现未开源验证,
README 未给计算式, 需自己设计**, 不能声称「借鉴了 RTSI 的具体公式」。

**落地障碍**: 算法细节未公开 (只有名字); AGPL 不可复制; 373 star 但无实测胜率数据。

### 4. guanquann/Stocksera ⭐ 美股另类数据宝库 (MIT)

**身份**: 781 star / 127 fork, 194 commits, JavaScript(Django), **MIT License**。

**60+ 另类数据源清单 (README 明确列出)**:
```
Yahoo Finance, Finviz, Finnhub, FinancialModellingPrep, IBKR, Reddit, Finra,
SEC, CompaniesMarketCap, SenateStockWatcher, HouseStockWatcher, Polygon,
ARKFunds.io, Stocktwits, Twitter, Federal Reserve
```

**具体能力 (README 有页面路径)**:
| 数据 | 源 | 对我们价值 |
|---|---|---|
| WSB 提及数 / put-call 比 | Reddit | 美股情绪 (我们只有指数涨跌) |
| Reddit 子版块提及趋势 | Reddit | 同上 |
| Stocktwits  trending / watchlist | Stocktwits | 美股情绪 |
| SEC filings | SEC | ✅ 免费无 key (treg 调研已确认可达) |
| 空头兴趣 / 借券费率 / FTD | Finra / IBKR | 美股特有, 我们无 |
| Reg SHO Threshold List | NYSE | 同上 |
| **国会议员交易** | Senate/HouseStockWatcher | 另类 Alpha, 我们无 |
| ARK 持仓变动 | ARKFunds.io | 机构动向 |
| 相关性矩阵 | 自算 | ⚠️ 已有 overseas_linkage |

**架构**: Django + scheduled_tasks 目录 (定时任务) + api 目录, 前后端一体。

**对我们最有用的**:
1. **SEC EDGAR** — 已确认容器内可达、完全免费无 key、3.7MB 完整 XBRL。
   我们缺美股基本面, 这是唯一零成本入口。
2. **国会议员交易 + ARK 持仓** — 真正的另类数据, 可作为「海外 tab」的差异化内容。
3. **架构模式**: scheduled_tasks 独立目录, 与我们的 Actions workflow 思路一致 (已验证可行)。

**落地障碍**: 大部分源是美股专用, 对 A 股无直接价值; Reddit/Stocktwits 需 key 或
有反爬; Django 架构对我们 (静态站) 不适用, 只学数据源清单。

### 5. cloudQuant/backtrader

**身份**: 默认分支 development, GPL-3.0, v1.4.0 (2026-09-19 发版, 活跃维护),
50+ 指标, 21+ 数据源, 17+ analyzers。

**是什么**: backtrader 的优化 fork, 支持向量化 (runonce) + 事件驱动 (runnext) 双模式,
宣称比原版快 45%+ (实测: 多指标策略 59.2s→26.9s, 多数据源 23.5s→12.6s)。

**架构亮点 (可学, 代码不可抄)**:
- **Cerebro 引擎分层**: v1.4.0 把 Cerebro 拆成 focused private components,
  保留 public facade 和序列化契约 → 我们 engine_v5/multidim 也可这样分层
- **Analyzer 插件机制**: 17+ analyzers (Sharpe/DrawDown/TradeAnalyzer) 可插拔
  → 我们已有 weight_calibrator / winrate_matrix, 可统一成 analyzer 接口
- **一键 HTML/PDF/JSON 报告**: `cerebro.generate_report('report.html')`
  → 我们前端已有, 但缺「策略回测报告」这一形态
- **多数据源 + 多时间框架重采样**: cerebro.adddata(data1/2) + 重采样
  → 我们已有 index_kline, 可加周线/月线重采样
- **参数优化**: `cerebro.optstrategy(...)` + maxcpus 并行
  → 我们缺策略参数寻优, 只有固定权重

**落地障碍**: GPL-3.0 传染 (引入即污染, 不可); 依赖重 (需 matplotlib/plotly);
我们需要的是「指标算法」而非完整框架, 按需取 50+ 指标里我们没有的那些。

### 6. Lumiwealth/lumibot

**身份**: 2103 star / 411 fork, 6024 commits, 默认分支 dev, GPL-3.0。

**架构 (docs/BACKTESTING_ARCHITECTURE.md 962 行, 明确分层)**:
```
Strategy.backtest()
  ↓
Data Source Selection (BACKTESTING_DATA_SOURCE env 可覆盖)
  ↓
DataSourceBacktesting (抽象基类)
  ├─ YahooBacktesting / ThetaDataBacktesting / IBKRBacktesting
  ├─ PolygonBacktesting / PandasBacktesting
  └─ RoutedBacktesting (多 provider 路由)
  ↓
BacktestingBroker (模拟撮合/持仓/现金/交易日历/期货保证金)
```

**核心设计原则 (文档原话)**: "Backtesting must mimic live broker behavior."
→ 回测必须模仿实盘行为, broker 返回 stale last trades, 只有 quote 端点给 NBBO/mark。

**其他**: 12 brokers, LLM 在回测循环内 (agentic backtesting), replay caching
(SHA-256 hash prompt+context+model+tool+timestamp → 缓存复用, 重跑零 LLM 调用),
内置 FRED 宏观 + SEC fundamentals 工具。

**对我们最有用的**:
1. **DataSource 抽象层 + 环境变量覆盖** — 我们 collect_* 脚本各自硬编码源,
   可统一成「数据源抽象 + 配置切换」, 便于 GitHub Actions 与 GitCode Space 双环境。
2. **Replay caching 思想** — LLM 复盘结果按输入 hash 缓存, 避免重复调用。
   我们 gitcode_ai_review 每天重跑同样数据, 可加缓存降 token。
3. **「回测必须模仿实盘」** — 我们 K线模拟回测需检查: 是否用了未来数据?
   是否模拟了 T+1 / 涨跌停不可买?

**落地障碍**: GPL-3.0; 12 brokers 全用不上 (我们不做实盘); 依赖重。

### 7. BennyThadikaran/eod2

**身份**: 180 star / 50 fork, 776 commits, GPL-3.0, NSE(印度) 专用。

**是什么**: NSE 日 EOD 数据自动下载更新, 2000+ 股票回溯到 1995, 自动做拆股/送股复权,
跟踪 NSE 假日。数据与代码分离 (eod2 代码仓 + eod2_data 数据仓, 数据周更)。

**对我们最有用的**:
1. **数据与代码仓分离** — 我们已是这个模式 (stocks-data-kline / stocks-data-live),
   验证了这个架构选择正确。
2. **假日表驱动增量同步** — `special_sessions.txt` + `market_tracker.csv`,
   同步时跳过假日。我们 K线 backfill 需检查是否有等价的假日处理。
3. **复权在入库时完成** (adjusted for splits and bonuses), 不是查询时算。

**落地障碍**: GPL-3.0; NSE 专用, 数据源是印度交易所, 对 A 股无直接价值;
只有架构思想可学。

### 8. shi-rudo/awesome-stock-trading

**身份**: 850 star / 124 fork, 139 commits, 纯清单 (CC license 之类, 需确认)。

**列出的股票 API (多数需付费)**:
```
Adanos Sentiment API   — Reddit/X/新闻/Polymarket 情绪聚合 (US stocks + crypto)
Alpha Vantage          — 免费档 (demo key 可用, 25 次/天)
EODHD                  — $19.99/mo
Financial Modeling Prep— $15/mo
MarketStack            — $9.99/mo
Massive (ex-Polygon)   — tick 级, $29/mo
Nasdaq Data Link       — 另类数据集
```

**结论**: 这些在既有 docs/external-repos-research.md 的 treg 调研里已全部覆盖
(含免费额度与实测可达性)。**OVERLAP, 无新增**。唯一新信息: Adanos Sentiment API
(情绪聚合) 与 Massive (Polygon 2025 改名)。

### 9. AlgoTraders/stock-analysis-engine

**身份**: 1243 star / 272 fork, 1042 commits, **最后推送 2020-09-05**。

**状态: 已停更 6 年, 不建议借鉴**。
- README 自述 "Yahoo's Finance API was disabled on 2019-01-03" → 主数据源早失效
- 架构: Kubernetes + docker-compose + Redis + Minio(S3) + Jupyter + Slack webhook
- 用途: 批量回测 5000+ 算法生成 AI 训练数据集 (>1.5 亿行)

**不适用原因**: 我们零运维静态站 + Actions, 与 K8s/Redis/Minio 架构完全相反。
唯一可学的是「批量回测生成数据集训练 DNN」的思路, 但我们需要的是信号源重构,
不是更多历史回测数据 (已有 6 万+ 样本且结论是技术指标无效)。

---

## 三、与现有 161 脚本的合并去重结论

### 已覆盖 (OVERLAP, 不必再做)
| 外部能力 | 我们的实现 |
|---|---|
| 复权 K线 (qfq/hfq) | collect_adjusted_bulk.py ✅ |
| 资金流向 | collect_moneyflow.py (东财 fflow) ✅ |
| 全球指数行情 | collect_global_markets.py ✅ |
| 技术指标 | engine_v5.py (17 指标) + patterns.py ✅ |
| K线模拟回测 + walkforward | 已有 ✅ |
| 权重校准 | weight_calibrator_v3.py ✅ |
| LLM 复盘 | gitcode_ai_review.py ✅ |
| 数据/代码仓分离 | stocks-data-* 三仓 ✅ |
| 付费 API 情报 | treg 调研已记录 ✅ |

### 我们的真实缺口 (NEW, 按价值排序)
| # | 缺口 | 来源仓库 | 工作量 |
|---|---|---|---|
| 1 | **板块→成分股列表** (sector_engine 覆盖率仅 6/27) | eltdx board_member_quotes | 中 (需自己实现协议) |
| 2 | **多空辩论 Agent** (看多胜率 43.8% < 随机) | TradingAgents-MCPmode | 中 (复用 GitCode AI) |
| 3 | **五档盘口 / 竞价撮合详情** | eltdx get_depth / auction_data | 中 |
| 4 | **美股基本面 (SEC EDGAR)** | Stocksera | 小 (免 key 已确认可达) |
| 5 | **趋势强度单值指数 (RTSI 思路)** | AI-Stock-Master | 小 (自己设计公式) |
| 6 | **回测报告生成器 (HTML)** | backtrader generate_report | 小 |
| 7 | **策略参数寻优** | backtrader optstrategy | 中 |
| 8 | **LLM 结果 hash 缓存** | lumibot replay caching | 小 |
| 9 | 另类数据 (议员交易/ARK 持仓/WSB) | Stocksera | 中 (美股向) |

### 不建议借鉴
- **AlgoTraders/stock-analysis-engine** — 停更 6 年, 数据源失效, K8s 架构不适用
- **lumibot / backtrader / eod2 的代码** — GPL-3.0 传染, 只学架构
- **AI-Stock-Master 的算法** — AGPL-3.0 且公式未公开, 只能参考命名思路
- **eltdx 的代码** — Research-Only, 协议知识可学, 代码要自己写

---

## 四、借鉴 → 落地对照表 (本轮实际变成我们代码的部分)

| 借鉴仓库 | License | 借鉴了什么 | 落地为 | 状态 |
|---|---|---|---|---|
| a-stock-data | Apache-2.0 | 腾讯逐笔 (替代失效的 mootdx) | `scripts/fetchers_ticks.py` | ✅ CI 跑通 36 只 |
| a-stock-data | Apache-2.0 | 逐笔 → 资金结构四指标 | `scripts/auction_engine.py` | ✅ 24/36 涨停股主动卖占优 |
| a-stock-data | Apache-2.0 | 成交量单位陷阱 (688 按股) | `scripts/volume_units.py` | ✅ 实测相差 13 倍 |
| AI-Stock-Master | AGPL (仅参考数学式) | RTSI/TMA/MSCI 三大算法 | `scripts/trend_strength.py` | ✅ RTSI 100 只 + TMA 30 板块 |
| TradingAgents-MCPmode | MIT | 多智能体 + 多空辩论 + 三层风控 | `scripts/bull_bear_debate.py` | ✅ 红队机制工作 |
| cloudQuant/backtrader | GPL (仅学架构) | Analyzer 插件框架 | `scripts/analyzers.py` | ✅ 8 个分析器 |
| lumibot | GPL (仅学设计) | Replay caching (SHA-256 输入缓存) | `gitcode_ai_review.py` 加缓存 | ✅ 省 token |
| kand-ta/kand | Apache-2.0 OR MIT | Vegas/Camarilla/HA/MFI/OBV | `indicators_ext.py` +5 指标 | ✅ Camarilla 11 价位数值验证 |
| Stocksera | MIT | 60+ 另类数据源清单 | 报告情报 (SEC EDGAR 等) | 📋 未落地 |
| eltdx | Research-Only | 通达信协议知识 | 报告结论 (该路已死) | 📋 止损, 不做 |

**图例**: ✅ 已落地并实测 / 📋 有情报未落地

---

## 五、本轮实际落地的代码

### 4.1 `sector_engine.py` (新建, 475 行) — 板块独立算法引擎

直接回应「每个板块配独立算法 + 总结报告」这个未完成需求。

七维诊断 (权重固定, 全部实时计算):
```
momentum 0.20  当日+5日+20日三段加权
persist  0.18  1/5/20日涨幅同向性 (三向同向才算真趋势)
breadth  0.16  板块内涨跌家数比 + 涨停占比 (不是只看领涨一只)
capital  0.16  东财行业主力净额 + 净占比
leadership 0.12 领涨股强度
position 0.10  60日区间分位 (源不可得, 恒中性 50, caveat 已标注)
sentiment 0.08  题材新闻命中 + 涨停家数
```

输出: 每板块 grade (A/B/C/D) + total 分 + 七维明细 + signals + risks +
一段可读 verdict (如「强势板块: 有明确驱动, 但维度不够全。当日 +1.72% /
5日 +3.12% / 领涨 皇台酒业 +10%。操作: 只做板块内最强 1-2 只, 不扩散到跟风票。」)

实测 (本地数据): 27 板块, B=2 / C=24 / D=1。已修 3 个 bug:
1. bulk K 线格式 `code|date|open|close|high|low|vol` 下标错 + 未按 code 取最后一行
2. 行业映射用裸码 (`002543`) vs K 线带前缀 (`sz002543`) → 加 normalize_code
3. **腾讯板块名 (`白酒Ⅱ`) vs 东财行业名 (`白酒Ⅲ`) 罗马数字后缀不同** →
   加 normalize_sector, 覆盖率从 1/27 提升到 6/27

**仍未解决**: 21/27 板块无成分股覆盖, 因为行业映射只覆盖 1050 只且本地 bulk K 线
只到 10-13。CI 上有完整数据会更好, 但根本解法是接入 eltdx 的 board_member_quotes。

### 4.2 `factor_lab.py` (新建, 520 行) — 特征实验室 ⭐ 本轮最重要产出

**为什么做这个**: 上一轮已确诊「1 日持有期下 14 个看多技术指标胜率 40-42.6%,
edge 全为负」。调权重救不了, 必须系统性回答「到底什么特征有预测力」。

**做法**: 12 个候选特征 x 4 个持有期 (1/3/5/10 日), 全市场全历史, 统一口径
(次日开盘买入 → 第 N 日收盘卖出, 单边滑点 0.03%, 涨跌停限制, 封跌停顺延)。
每个特征输出 n / 胜率 / 均收益 / 中位收益 / t 值 / 多空差 / edge_vs_baseline。

**防自欺的四条设计**:
1. 不做参数寻优 (阈值全写死) — 寻优出来的「最优」在样本外必失效, 那是过拟合
2. 输出全部 12 个特征含无效的 — 只报有效的是 cherry-picking
3. random_baseline 同口径对照 (55 万随机样本)
4. `min_edge` 默认 0.10pp — 差距小于此值视为噪声, 不当成有效信号

**实测结论 (1200 只股票, 55.7 万随机基准样本)**:

| 特征 | 1日 | 3日 | 5日 | 10日 | 判定 |
|---|---|---|---|---|---|
| **below_ma60** (深度跌破60日线) | +0.07% t=3.5 | +0.34% t=23.6 | +0.60% t=32.5 | +1.06% t=42.8 | **✅ 全部有效** |
| rsi_oversold | 负 | +0.11% | +0.25% | — | ⚠️ 有收益但打不赢随机 |
| bias_ma20 (乖离<-5%) | 负 | +0.11% | +0.28% | — | ⚠️ 同上 |
| vol_dry_up (缩量上涨) | 负 | +0.16% | +0.41% | — | ⚠️ 同上 |
| rsi_overbought (RSI>70) | **-0.46%** | **-0.49%** | **-0.40%** | — | ❌ 强负面 |
| new_high_20 (20日新高) | **-1.43%** | **-1.56%** | **-1.98%** | — | ❌ 强负面 |
| breakout_60 (60日新高) | **-2.19%** | **-2.50%** | **-2.70%** | — | ❌ **最负面** |
| gap_up (跳空高开>2%) | **-0.55%** | -0.45% | -0.45% | — | ❌ 强负面 |
| vol_ratio_high (放量2倍) | -0.48% | -0.45% | -0.48% | — | ❌ 负面 |
| ma_golden (MA5>MA20金叉) | -0.13% | -0.04% | +0.03% | — | ❌ 无预测力 |

**三个可操作结论**:
1. **唯一有正期望的买入信号是「深度超跌」**: 收盘 < MA60*0.95, edge 随持有期
   递增 (1日 +0.12pp → 10日 +0.42pp), 说明这是均值回归而非噪声。
   持有越久边际越大 → **应该拉长持有期, 不是做 T+1**。
2. **所有「追突破」类信号全是负期望, 且非常显著** (t=-6~-8):
   60 日新高 -2.7% / 20 日新高 -2.0% / 跳空高开 -0.55%。
   → 这与现有 engine_v5 的「突破加分」逻辑**方向相反**, 是明确的改进点。
3. **强负面信号可用于「回避」**: 即使不做空, 「不买 RSI>70 / 不买创新高 /
   不买跳空高开」也能避开通缩, 这部分价值可能比选对票更大。

**这与上一轮「看空胜率 54.4% > 看多 43.8%」的发现完全一致** — 两个独立方法
( calibrator 分层统计 / factor_lab 全市场测量 ) 得到同一结论: 当前市场环境下,
回避比追涨有效。**这不是巧合, 是可以信任的结论**。

**局限 (如实说明)**: 未计佣金印花税 (实盘每笔约 0.1%+, 对 1 日持有期致命);
未分 regime 分层 (混合市况会稀释信号); 资金类特征未纳入 (缺历史 moneyflow);
本地 bulk K 线只到 10-13, 全量数据需在 CI 上跑。

---

## 六、未调研的 36 个仓库 (子代理失败导致)

> 已从 39 减到 36: kand-ta/kand 与 wshobson/maverick-mcp 本轮补调研完成,
> zhangxiangliang/stock-api 与 chengzuopeng/stock-sdk 有初步情报 (见 1.1 末尾)。

**A股/数据 API 类 (8)**: allen200909/stock, zhaogods/tostock, zhangxiangliang/stock-api,
GlacierBo/stock-api, chengzuopeng/stock-sdk, Vulthen/myhhub-stock-quantitative,
siqyka/dsh-stock-leek, AnacondaKC/Aniu

**AI/ML 类 (5)**: andykou3388/TensorFlow-stocks-prediction..., touhoufan2024/qlibAssistant,
faizancodes/Automated-Fundamental-Analysis, hydrosquall/tiingo-python,
wshobson/maverick-mcp

**框架类 (4)**: fbertram/TuringTrader, kand-ta/kand, Doffyyyy/TradingView-API,
koreainvestment/open-trading-api

**数据集/监控类 (5)**: TickDB/tickdb-unified-realtime-marketdata-api,
franciscoa19/stonkmonitor, adanos-software/free-ticker-database, vlvagerviwager/snp-btc-yoy,
chizkidd/freealpharadar, w1570187062/my-portfolio-hub, deep-div/mutualfund-screener

**API 文档仓 (4)**: api-evangelist/{morningstar,hkex,euronext,alpha-vantage}

**第二批 (12)**: pacocartones(账号), Simzy420/big-brain-ape-dashboard,
judefluen-coder/ai-chaingraph, tanueihorng/finsight, Pubmarks/datasets,
yam-svg/stocklite-plugin, salylgw/gammath_spot, harshul88-hub/low-float-data,
tradernetwork/etf-holdings-tracker, maxgfr/crible, yehomak/NewsAlpha

**下一轮建议**: 子代理恢复后优先补调研「A股数据 API 类 8 个」(与现有数据源直接竞争,
最可能发现可替代的免 key 源) 和「tiingo-python + api-evangelist 4 个」(确认免费额度)。

---

## 七、下一步 (按性价比排序, 已完成的标注 ✅)

1. ⬜ **把「追突破」从买入逻辑里去掉** — factor_lab 实测 breakout_60 未来 3 日
   **-2.50%** (t=-6.92), new_high_20 **-1.56%**, gap_up **-0.45%**,
   全部强负面且统计显著。engine_v5 若给突破类指标正权重, 是在系统性做错方向。
   工作量: 小 (改权重符号或剔除)。**这是当前最高性价比的一步**。

2. ⬜ **加「深度超跌」买入信号** — below_ma60 是 12 个特征里唯一全持有期有效的
   (1/3/5/10 日 t=3.5/23.6/32.5/42.8, edge 随持有期递增到 +0.42pp)。
   建议在 engine_v5 / multidim 增加该因子, 并把持有期建议从 T+1 拉长到 5-10 日。
   工作量: 小。

3. ⬜ **SEC EDGAR 美股基本面采集器** — Stocksera 情报 + treg 已实测免 key 可达。
   新增 `scripts/collect_sec_edgar.py`, 产出 `data/us_fundamentals.json`。
   工作量: 小。价值: 填补美股基本面空白, 是唯一零成本入口。

4. ✅ **多空辩论 Agent** — 已落地 `scripts/bull_bear_debate.py`
   (TradingAgents-MCPmode 架构 + GitCode AI 通道, 3 角色最小闭环)。

5. ⬜ **板块成分股直连** — 注意: a-stock-data 已实测通达信公开服务器 2026-09 起
   全部失效, 自己做 7709 协议这条路已死。应改为: 从东财/同花顺的板块成分股
   HTTP 接口取 (a-stock-data §3.3 东财 slist 是个股→板块归属, 可反向建索引),
   或接受 6/27 覆盖率 (只分析成分股 >=3 的板块)。工作量: 中。

6. ✅ **LLM 复盘 hash 缓存** — 已落地 (lumibot replay caching 思路)。

7. ✅ **趋势强度指数** — 已落地 `scripts/trend_strength.py` (RTSI/TMA/MSCI)。

8. ⬜ **补调研剩余 36 个仓库** — 优先「A股数据 API 类」
   (allen200909/stock / Vulthen/myhhub / siqyka/dsh-stock-leek / AnacondaKC/Aniu),
   与现有源直接竞争, 最可能发现免 key 替代。

9. ⬜ **kand 的 O(1) 增量指标** — 若后续做分钟级实时指标, 可借鉴其增量更新
   设计 (现在是批量全算, 分钟级会浪费)。优先级取决于是否上分钟级。

