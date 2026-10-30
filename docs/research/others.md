# 9 个外部仓库评估报告

评估基线：我们的体系 = A股 DuckDB+parquet 数据仓 / 纯统计云推演 / 五因子选股 / 胜率对账闭环(pick_tracker)。
注：任务清单中的 lumibot 未在 /tmp/repos/x 下解包（不存在），故实际评估 9 个中已有 8 个 + 说明。核对 /tmp/repos/x 实际内容后，9 个为：ticker、TradingView-API、pybroker、alpaca-mcp-server、watch-list、market-dashboard、myhhub-stock-analysis-suite、options-daily-report（lumibot 缺失）。

---

## 总分类

| 档位 | 仓库 |
|---|---|
| **值得接入** | pybroker（思想+部分代码）、market-dashboard（数据源清单+_streamlit页面结构）、watch-list（Yahoo免费行情URL+页面交互模式） |
| **仅借鉴思想** | options-daily-report、ticker、TradingView-API、alpaca-mcp-server、myhhub-stock-analysis-suite |
| **不推荐** | （无绝对不推荐项；myhhub 若非 .NET 技术栈可视为不推荐接入） |

---

## 1. pybroker（Python 回测框架，65 个 py 文件）—— ★ 值得接入

**价值**：NumPy+Numba 极速向量化回测引擎，正是"纯统计云推演"最匹配的架构——无事件循环、无 broker 模拟开销，适合 A股全市场五因子批量回测。
**可借鉴点（重点）**：
- **因子回测架构**：`strategy.py` / `scope.py` / `vect.py` 三层——指标先向量化预计算（IndicatorSymbol），再用 ctx 对象暴露给规则函数；`ctx.indicator('name')` 取滚动窗口、`ctx.buy_shares / hold_bars / stop_loss_pct` 声明式下单。我们的五因子可直接套此"预计算指标数组 + 每symbol执行函数"模式。
- **Walkforward Analysis**（strategy.py L1533+ `_walkforward_row_mask` / `walkforward_split`）：train/test 滚动窗口切分，防止因子过拟合——比我们目前的单一区间回测更严谨，值得移植到 pick_tracker 的胜率对账前置验证。
- **Bootstrap 随机化指标**：交易指标用 bootstrapping 估计置信区间，对"胜率对账闭环"是天然互补（胜率±CI）。
- **Optuna 参数优化 + diskcache 缓存**（indicator/model 缓存键 = (指标名, symbol, 参数, 数据指纹)），缓存设计可直接抄。
- **AI Agent Skills**（skills/ 目录 6 个 skill：indicator-creator、strategy-creator、optimize、rotational-trading 等）——本身就是给 agent 写回测用的提示词模板，可直接参考为我们自己 skill 的写法。
- 自定义数据源接口 `DataSource` 抽象：只需实现 `_fetch(symbols, start, end)` 返回标准 OHLCV DataFrame——可写一个 DuckDBDataSource 适配器接我们的 parquet 仓。
**接入建议**：pip 安装 lib-pybroker，写 DuckDB DataSource + 用其 walkforward 跑五因子基准回测。

## 2. watch-list（stockwatchlist.app，Node/TS 全栈）—— ★ 值得接入（轻量）

**价值**：极简自托管 watchlist + 冻结买入价对账的产品形态，与 pick_tracker 思路高度同构（"给想法标价，然后看之后发生了什么"）。
**可提取数据源接口**：
- Yahoo 免费行情（`server/quotes.mjs`）：`https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?interval=1d&range=1mo`（备 host query2，双 host 轮换容错——限速规避手法值得抄）
- 财报日历：Nasdaq（`server/earnings.mjs`），美股 >$1B，按周分列
**UI 借鉴点**：冻结 add-day 价格做盈亏归因（而非实时价）、只读 `/share/` capability-token 分享链接、市场切换器（US/EU/CA/Crypto）、"Compare vs Yahoo/Google/TradingView"页。React+Vite+shadcn/ui 结构清晰。
**接入建议**：抄 Yahoo chart API 的 URL 拼装 + host 轮换逻辑进我们的数据采集层；watchlist 冻结价思想并入 pick_tracker 的入账快照。

## 3. market-dashboard（Streamlit 日美股市综合面板）—— ★ 值得接入（数据源+页面结构）

**价值**：单文件 app.py + analytics.py + 40+ scripts 的 Streamlit 全家桶，README 里有完整的"生产踩坑实录"和**数据源冗余策略表**，这是本仓最值钱的部分。
**免费数据源清单（README 明列）**：yfinance、Tiingo、FMP、BLS、FRED、J-Quants、Finnhub、irbank.net（信用残备源）、Yahoo/株探/みんかぶ/TDnet RSS 新闻聚合；AI 侧 Gemini/Groq/DeepSeek/NVIDIA/OpenRouter 五家自动 fallback。对我们可借鉴的是**"每类数据至少两源、失败自动降级"**的工程模式（与 stock-data-sources skill 的思路互证）。
**Dashboard/UI 借鉴点**：
- 宏观首页卡片：指数+VIX+汇率+美债收益率 watchlist 卡片带 sparkline；Shiller CAPE、Fear&Greed、NAAIM 仓位、复合情绪分
- 弱势/暴跌形态**历史模式匹配**（与历次股灾复合指标比对）——纯统计可落地，契合我们"纯统计云推演"
- RRG 板块轮动图、52周高低/均线乖离/金叉死叉扫描、ROIC-WACC 批量计算、SMB/HML 因子面板（与我们五因子直接对标，可参考其展示切片）
- 日次快照存 GitHub Actions + Google Sheets 持久化（对我们可用 Actions+parquet 替代）
- "LLM as a Judge 批量评披露文本"：全标的合并成一次 AI 调用打分（重要性/股价冲击/情绪/紧急度）——可平移到 A股公告/龙虎榜点评
**接入建议**：不搬代码，抽 3 样：数据源冗余表、暴跌模式匹配算法、AI 公告批量评分模式。

## 4. options-daily-report（期权日报，仅发布产物无引擎）—— 仅借鉴思想

**价值**：**无源码**（引擎私有，CC BY-NC 数据协议），只有生成的 markdown 日报 + dashboard JSON + 交易日志 CSV。但产物结构本身就是一份优秀的"期权日报模板"。
**可借鉴框架**：
- 日报三件套：`reports/YYYY-MM-DD.md`（日）+ `weekly_summary.md`（周日展望）+ `dashboard/data.json`（聚合指标）
- **内容寻址发布**：`data-<hash12>.json` 不可变快照 + `latest.json` 指针，先发 payload 再翻指针——防手改、可校验，跟我们 pick_tracker 的对账防篡改完全同构，强烈建议照抄这套发布协议
- 交易日志字段设计：`Date, Symbol, Strategy, Strike, OTM%, Premium, IV, Delta, CP Score, POP(胜率), Annualized Return, Expiry, Spread Quality`——其中"每行策略机会自带 POP 胜率与年化收益预估"正是我们胜率闭环想要的逐笔形态
- TTL 分层保留策略（日报/AI结果/快照各自独立保留期）
- GitHub Actions 定时发布 + schema-check + "guard-no-python" CI 门禁（纯数据仓不混代码的纪律）
**结论**：不接代码（没有可接的），但其 artifact 协议与期权/衍生品日志 schema 可直接套用。

## 5. ticker（Go 终端行情 TUI）—— 仅借鉴思想

**价值**：Go 编写的终端股价/持仓跟踪器（Yahoo 免费行情 + coinbase/crypto），与我们的 Python 栈异构。
**可借鉴点**：配置即持仓（`~/.ticker.yaml`：watchlist + 多批次 cost basis）、多 cost-lot 持仓模型、盘前盘后报价开关、缓存层设计（internal/cache）。UI 是 bubbletea TUI，与我们的 Web dashboard 无交集。
**结论**：不接；若需要终端快速看盘可 brew 装来用，仅此而已。

## 6. TradingView-API（TS 社区库 @mathieuc/tradingview）—— 仅借鉴思想

**价值**：逆向 TradingView 私有 websocket 协议的 TS 客户端，V4 重写为 ESM+TypeScript，能拿全球标的 K 线/报价/指标/筛选器（免费、无需 key，但非官方、随时可能被断）。
**可借鉴点**：`getCandles({symbol:'BINANCE:BTCUSDT', timeframe:'D', count:40})` 一行取数最省事；docs/ 里有 protocol、screener、coverage/reliability 矩阵的文档化方式值得学。
**结论**：A股数据帮不上（TradingView 对 A股历史 K 线有限制），协议属灰色地带且不可控；如需海外对标行情做跨境联动分析可临时用，不作数据仓正式源。

## 7. alpaca-mcp-server（Alpaca 官方 MCP，FastMCP 重写）—— 仅借鉴思想

**价值**：券商 MCP 服务器，v2 由 OpenAPI spec 自动派生工具 + toolsets 环境变量过滤 + 信任边界/注入防护测试全套。
**可借鉴点**：`src/tool_registry.py`——"OpenAPI spec → MCP tool"自动生成模式；安全测试集（prompt injection envelope、trust boundary middleware、HTTP security）是我们若做自家 MCP 服务的安全 checklist。
**结论**：美券商，与 A股 DuckDB 体系无数据交集；等我们做自研 MCP 时回来抄其 spec 派生与安全测试模式。

## 8. myhhub-stock-analysis-suite-toolkit（C#/.NET WPF 股票套件）—— 不推荐接入（可浅借鉴）

**价值**：.NET WPF 自绘 K 线控件（StockChartControl）+ Tushare/通达信行情线程读取 + SqlServer 存储 + vue 前端分离 v3。
**可借鉴点（有限）**：ReadTushareStockMarketThread / ReadTdxStockMarketThread 展示了 Tushare、通达信本地行情的解析路径（但我们已有 pytdx/akshare 通道，无增益）；指标 Transforms 目录是 C# 实现的常用技术指标，翻译价值低。
**结论**：技术栈完全异构（C#/WPF/SQLServer），代码陈旧（.vs 目录都提交进仓库），不接不抄，仅存档。

## 9. lumibot —— 未解包/缺失

/tmp/repos/x 下不存在 lumibot 解包目录，本任务无法实地评估。基于常识备注：lumibot 是 Python 多券商（Alpaca/IBKR）实盘 bot 框架，对我们的价值点本应是"策略基类 + 回测/实盘同构接口"，但与 A股纯统计推演体系匹配度一般，缺失不影响结论。如需补评请先解包该仓库。

---

## 落地优先级（结合我们体系）

1. **pybroker walkforward + bootstrap 指标** → 五因子选股的防过拟合验证层；DuckDB DataSource 适配器。
2. **options-daily-report 的发布协议** → pick_tracker 对账产物改为 content-addressed（hash 快照 + latest 指针 + TTL）。
3. **watch-list 的 Yahoo chart URL + 双 host 轮换** → 备用/海外行情通道。
4. **market-dashboard 的数据源冗余表 + 暴跌模式匹配 + AI 公告批量评分** → dashboard 迭代路线图。
5. 其余归档备查。
