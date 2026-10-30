# StockSharp 数据源与策略思想速览

仓库: `/tmp/repos/StockSharp-master`（C# 交易框架主仓）。**注意：连接器实现在独立仓库 [StockSharp/Connectors](https://github.com/StockSharp/Connectors)**，本仓只有主框架（Algo、Algo.Indicators、Algo.Strategies、Algo.PositionManagement、Messages 等）+ Samples。README 内嵌 371 个连接器清单及文档链接（doc.stocksharp.com/en/topics/api/connectors/…）。

## ① 免费数据源连接器（可移植目标）

| 连接器 | 类型 | 端点要点 | 文档 |
|---|---|---|---|
| Yahoo | 免费行情/历史 | query1.finance.yahoo.com/v8/finance/chart/{symbol} | …/stock_market/yahoo.html |
| AlphaVantage | 免费键值行情 | alphavantage.co/query (TIME_SERIES_DAILY 等, API key 免费) | …/stock_market/alphavantage.html |
| Finnhub | 免费+付费行情/新闻 | finnhub.io/api/v1/quote / news (token 免费) | …/finnhub.html |
| CoinGecko | 免费加密行情 | api.coingecko.com/api/v3/coins/{id}/market_chart | …/crypto_exchanges/coingecko.html |
| CoinMarketCap | 免费层加密 | pro-api.coinmarketcap.com/v1 | …/crypto_exchanges/ |
| RSS | 免费**新闻** | 通用 RSS/Atom feed 解析（news 类连接器中唯一完全免费开放的） | …/common/rss.html |
| Marketaux | 新闻（免费层） | api.marketaux.com/v1/news/all (entity 关联到股票) | …/marketaux.html |
| StockData.org / Marketstack / Twelve Data / Tiingo / EODHD / FMP | 免费层行情/基本面 | 各自 REST v1/v2 | …/stock_market/ |
| SEC EDGAR / EDINET / Open DART / KRX / TWSE OpenAPI | 免费披露/财报 | 各交易所官方 API | …/stock_market/ |

## ② 中国 A 股相关连接器（实现均在 Connectors 仓库，本仓仅有清单+文档链接）

- **Zhongtai XTP**（中泰证券柜台，A股/两融高频）— doc: …/stock_market/xtp.html
- **CTP**（上期所期货）— …/stock_market/ctp.html
- **QMT / MiniQMT / XtQuant**（迅投，券商量化常用，Python 生态直接可用）— …/stock_market/qmt.html
- 老虎 Tiger OpenAPI、长桥 Longbridge OpenAPI（均支持港/美股+A股通）

## ② 新闻/热点数据源（统一接口 NewsMessage）

核心抽象在主仓 `Messages/NewsMessage.cs`：字段 `Id, BoardCode, SecurityId(可空), Source, Headline, Text, ServerTime, SeqNum` —— 所有新闻源适配到同一条消息流，订阅方式与行情一致（`DataType.News`）。适配器实现于 Connectors 仓库，含：

- **MT Newswires**（…/stock_market/mt_newswires.html，商业）
- **RavenPack**（事件/情感分析，商业）、**Dow Jones**、**Benzinga**（商业）
- **Marketaux**（免费层：新闻+实体情感）、**RSS**（免费通用）、**Unusual Whales**（异动/国会交易，付费）
- 新闻存储：`Algo/Storages/Csv/NewsCsvSerializer.cs`、`Binary/NewsBinarySerializer.cs` —— 离线存取可直接借鉴（CSV 列即可用于 Python 端新闻库）。

## ③ 值得移植成 Python 的策略思想

**执行/拆单算法（`Algo/PositionManagement/`，最高移植价值）**
- **TwapAlgo / VwapAlgo**：时间/成交量加权拆单执行。
- **IcebergAlgo**：冰山单——只挂 DisplayVolume，成交后自动补挂下一片。
- **NetExposureHedger**：多组合对冲管理——按"应有仓位 vs 实际仓位"差值预预留缺口再补单（下单即预留，防止重复下单），思想非常干净。
- **NbboAlgo / MarketOrderAlgo / PositionTargetManager**。

**报价引擎（`Algo.Strategies/Quoting/`）**
- QuotingBehavior 架构：Market/BestByPrice/BestByVolume/LastTrade/Level/Limit/TheorPrice 七种报价行为统一接口，做市与追单可参数化切换——适合抽象为 Python 的 QuotingEngine 类。

**保护性出场（`Algo.Strategies/Protective/`）**
- ProtectiveController + IProtectiveBehaviour（本地/服务器侧）：统一管理全组合的止盈止损与移动止损，`Update(price, value, time)` 纯函数式判断激活——极易移植。

**风险管控（`Algo/Risk/`）**
- RiskManager + 20 余条独立规则（单笔/累计成交额、频率、价格偏离、滑点、PnL、持仓量、持仓时间、佣金），规则链挂在消息管道上——对应 Python 可做成中间件/装饰器。

**示例策略（`Samples/06_Strategies/`）**
- `08_LiveArbitrage/ArbitrageStrategy.cs`：期货-现货基差套利，状态机 Contango/Backwardation/下单/止盈退出。
- `03_HistoryTrend/StairsTrend`：连续 N 根同向 K 线的阶梯趋势（含 Countertrend 逆势版）；`01_HistorySMA`、`02_BollingerBands` 等入门范例。
- 优化器：`Algo.Strategies/Optimization/` 遗传算法（GeneticOptimizer + StrategyFitness）参数寻优。

**指标库（`Algo.Indicators/`，190 个）**
- 标准 TA 之外少见的：KalmanFilter、JurikMovingAverage、HurstExponent、FractalDimension、SchaffTrendCycle、ConnorsRSI、NRTR（Nick Rypock Trailing Reverse，跟踪止损指标）、AdaptiveLaguerreFilter、EhlersFisherTransform、KaufmanEfficiencyRatio、LunarPhase 等——Python 端可直接按公式移植补 TA-Lib 缺口。
- K 线形态：`Algo/Candles/Patterns/ComplexCandlePattern.cs` 支持表达式定义的多根 K 线组合形态。
- 期权：`Algo/Derivatives/`（Black-Scholes、Greeks、合成工具、期权桌 IOptionDesk）。

**架构层思想**：消息管道（MessageAdapter 洋葱式包装：心跳/延迟/风控/离线缓存层层包裹）、StorageRegistry 统一 CSV/Binary 存储、Subscription 抽象 —— 移植 Python 时可用"适配器+中间件管道"复刻。
