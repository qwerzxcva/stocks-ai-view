# NoFxAiOS/nofx 源码调研报告（Go 交易AI平台）

## ① 美股/全球数据源（可直接抄用的 URL）

| 数据源 | 用途 | URL 格式 | Key | 免费额度 |
|---|---|---|---|---|
| **Alpaca Market Data** | 美股 K 线（1Min~1Month bar），代码用 `feed=iex` 走免费 IEX 行情 | `https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Day&limit=500&adjustment=raw&feed=iex&start=YYYY-MM-DD` | 需要（HTTP 头 `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`） | IEX feed 免费，仅 IEX 交易所成交（约全市场3%量），日线可用；全量 SIP 需订阅 |
| **Twelve Data** | 外汇/贵金属 K 线与 quote | `https://api.twelvedata.com/time_series?symbol={symbol}&interval=1day&outputsize=500&apikey={key}`；quote: `/quote?symbol=...&apikey=...` | 需要（免费 key） | 免费 8 req/min、800/day，支持外汇、股票、指数（指数 symbol 如 `SPX`） |
| **CoinAnk OpenAPI** | 加密货币 K 线/资金费/持仓量（默认 fallback Binance 数据） | `https://open-api.coinank.com/...`（国内镜像 `https://open-api-cn.coinank.com`） | 需要 key | 有限免费额度 |
| **Hyperliquid** | 加密 perp + 股票 perp（xyz dex），无需注册 | `POST https://api.hyperliquid.xyz/info`（body `{"type":"candleSnapshot","req":...}`） | 不需要 | 完全免费 |
| **NofxOS 自家 API** | AI500 指数、涨跌榜、净流出、OI | `https://nofxos.ai/api/price/ranking?duration=24h&limit=50` 等 | 项目内默认无 key | — |
| **claw402.ai (vergex)** | 胜率/信号类数据 | `https://claw402.ai` | 项目内配置 | — |

要点：nofx 没有美股实时 websocket/quote 个股源，美股只有 Alpaca IEX 历史与分钟 bar（免费层延迟约15分钟可接受）；外汇/贵金属/指数可借 Twelve Data 免费层。**对"海外指数历史K线免费源"最有价值的正是 Twelve Data（免费 key、800次/天，支持 SPX/NDX/HSI 等）**。

## ② 中国A股数据源

**没有。** 全仓 grep 无 tushare/akshare/sina/eastmoney/xueqiu 等任何 A 股接口；nofx 是纯加密货币 + 美股(Alpaca) + 外汇(TwelveData) 平台。A 股实时仍需你们现有腾讯 qt / 东财方案。

## ③ 架构上值得借鉴的点

1. **Provider 多源路由（`provider/` + `api/handler_klines.go`）**：统一 `market.Kline` 结构，`handler` 按 exchange 字符串 switch 路由到 alpaca/twelvedata/hyperliquid/coinank，每个 provider 一个目录一个 `kline.go`，新增源即插即用。单文件约 30 行即可接入新源，非常适合抄。
2. **AI 决策内核（`kernel/engine*.go` + `prompt_builder.go`）**：市场数据→结构化 Context（持仓/账户/指标）→ prompt_builder 拼 prompt → 多 LLM provider（`mcp/provider/` 有 openai/claude/gemini/deepseek/kimi/minimax/grok 统一接口）→ `schema.go` 强校验 JSON 输出 → 落 `store.DecisionRecord`。决策与执行解耦。
3. **AutoTrader 决策循环（`trader/auto_trader*.go`）**：decision / risk / throttle / signal_policy / grid 分文件，每周期：拉行情→构建上下文→AI决策→风控闸→下单→存 equity snapshot + decision record（权益曲线与决策日志分离存储）。
4. **`agents.md` 的 "80% skill + 20% 动态规划"**：高频/高风险操作预定义 skill 固定流程，仅开放性任务交给 LLM 自由规划；高风险动作（启动实盘/删除）强制确认。这套 AI 助手规范可直接复用到我们的运维助手。
5. **回测**：**没有回测框架**（无 backtest 代码），仅实盘决策记录可事后算胜率。回测需自建或参考 pybroker 等仓库。
6. **GitHub Actions（`.github/workflows/`，11 个 workflow）**：PR 多级检查（run→comment 两段式，advisory 结果以评论回帖）、多架构 Docker 构建推 ghcr（matrix amd64/arm64）、docker-compose 健康检查、go test coverage。是成熟的后端 CI 模板。
7. **数据存储（`store/`）**：GORM + 驱动抽象，`DB_TYPE=sqlite`（默认 `data/data.db`，modernc.org/sqlite 纯Go免CGO）或 postgres；表含 decision/equity/order/position/strategy/user 等。轻量单机即 SQLite，可平滑升 PG，值得照抄这套 driver 抽象。

## ④ 对我们项目的直接可用结论

- 补海外指数/美股历史 K 线：抄 Alpaca IEX bars + Twelve Data time_series 两个 URL 格式即可，均免费 key。
- 美股实时：无免费个股实时源在 nofx 中，其 Alpaca IEX 分钟 bar 是最接近的免费方案。
- A 股：nofx 无可借鉴，维持腾讯/东财。
- 最值得移植：provider 路由模式、决策记录+权益快照分离落库、SQLite/PG 双驱动抽象、"skill 优先"的 AI 助手规范。
