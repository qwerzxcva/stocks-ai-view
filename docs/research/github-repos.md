# GitHub 开源仓库调研报告（adata / free-stockdb_1 / tdx-api）

调研时间：2026-10-17。结论只基于实际读到的 README 与源码。
（子代理交付，主代理落盘。adata 源码读自 Gitee 镜像 gitee.com/inchaos/adata 原文；
free-stockdb_1 与 tdx-api 内容来自 websearch 抓取的 GitHub 页面快照，github.com
在沙盒被拦截无法直接读源码，未核实项已标注。）

## 1. 1nchaos/adata（PyPI: adata，Star ~5k，Apache-2.0）

**定位**：纯 HTTP 爬虫型 A 股数据库 SDK，多数据源融合切换，全部返回统一 pandas DataFrame。

### 数据源与具体 endpoint（源码确认）

| 数据 | 源 | Endpoint |
|---|---|---|
| 日/周/月/分钟 K 线（含前复权 fqt=1） | 东财 | `http://push2his.eastmoney.com/api/qt/stock/kline/get`，参数 secid=`{0或1}.{code}`、klt=101/102/103、fqt、beg/end，ut=`7eea3edcaed734bea9cbfc24409ed989` |
| 当日分时 | 东财 | `https://push2.eastmoney.com/api/qt/stock/trends2/get`，ut=`fa5fd1943c7b386f172d6893dbfba10b` |
| K 线/分时/五档/分笔（备用源） | 百度股市通 | `https://finance.pae.baidu.com/selfselect/getstockquotation`（group=quotation_kline_ab / quotation_minute_ab）、`https://finance.pae.baidu.com/vapi/v1/getquotation`（五档 askinfos/buyinfos、分笔 detailinfos） |
| 批量实时快照 | 新浪 | `https://hq.sinajs.cn/list=s_sz000001,...` |
| 批量实时快照/五档 | 腾讯 | `https://qt.gtimg.cn/q=s_sh600011,...`、`https://web.sqt.gtimg.cn/q=sh601666,...`（~ 分隔 85+ 字段） |
| 分笔成交（分页） | 腾讯 | `http://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={code}&p={page}` |
| 个股资金流（主力/小/中/大/超大单，日度仅 120 天） | 东财 | `https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get`、`https://push2.eastmoney.com/api/qt/stock/fflow/kline/get`（分时） |
| 龙虎榜每日列表/个股买五卖五营业部 | 东财 | `https://datacenter-web.eastmoney.com/api/data/v1/get`，reportName=RPT_DAILYBILLBOARD_DETAILSNEW / RPT_BILLBOARD_DAILYDETAILSBUY / SELL |
| 人气榜 100 | 东财 | `https://emappdata.eastmoney.com/stockrank/getAllCurrentList`（POST json） |
| 热股 100 / 热门概念板块 20 | 同花顺 | `https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?stock_type=a&type=hour`、`.../v1/plate?type=concept|industry` |
| 北向资金（历史/分时） | 东财+同花顺双源 | `datacenter-web...reportName=RPT_MUTUAL_DEAL_HISTORY`、`https://push2.eastmoney.com/api/qt/kamt.rtmin/get`；降级源 `https://data.hexin.cn/market/hsgtApi/method/dayChart/` |
| 解禁、融资融券 | 同花顺/东财 | data.10jqka.com.cn/market/xsjj、data.eastmoney.com/rzrq |
| 扫雷避险（风险打分，独有） | 通达信网页 | `http://page3.tdx.com.cn:7615/site/pcwebcall_static/bxb/json/{code}.json`，按 trig 项扣分制计算 score |
| 概念板块（代码/成分/个股所属概念） | 同花顺+东财双套 | 东财 `data.eastmoney.com/bkzj/gn.html` 系列 |
| 交易日历 | 深交所 | — |
| ETF 行情、可转债 | 东财/同花顺/新浪 | — |

### 架构亮点（源码确认）
- **多源降级链**：如 `list_market_current` 先新浪、空则腾讯；分时先东财空则百度；北向分时先东财空则同花顺。
- **SunRequests 重试封装**：循环 times 次（默认 3），间隔 retry_wait_time（默认 1588ms），仅 200/404 立即返回；可选 wait_time 请求前限速（adata/common/utils/sunrequests.py）。
- **全局代理池**：`adata.proxy(is_proxy=True, ip=... 或 proxy_url=...)`，单例 SunProxy 存储，proxy_url 每次请求动态取新 IP。
- **同花顺反爬**：hexin-v cookie 用 py_mini_racer 本地执行 hexin.js 生成（base_ths.py），问财 token 从首页 JS 中正则提取 TOKEN_SERVER_TIME。
- **@handler_null 装饰器**统一异常转空 DataFrame；输出列名标准化（trade_time/open/close/...），同名字段跨源一致。

### 与我们的差异（增量）
- 注册表已覆盖腾讯/新浪/东财/同花顺，**百度股市通 `finance.pae.baidu.com` 系列（K线/分时/五档/分笔）大概率是新源**（需对照注册表确认）。
- 增量数据类型：龙虎榜明细（营业部买五卖五）、东财人气榜/同花顺热度榜、通达信扫雷打分、北向资金历史+分时、个股资金流（120 日）。
- License：Apache-2.0，代码可自由借鉴，风险低。

## 2. iwerevip/free-stockdb_1（Star 3，fork/镜像自 hello245m/free-stockdb，上游 MIT）

**定位**：不是爬虫库，是**本地量化数据引擎的成品分发**：C++ 定制时序引擎（闭源二进制 `stockdb.exe` + `数据更新.exe` + `数据网页版.html`），数据随仓库分发，每日更新。

### 已确认内容（README/上游 README，未能读源码）
- 数据覆盖：2000 年至 2026-07，7400+ 标的（沪深 ~5400 股 + 2000+ 只 ETF），1.1 亿条；1/5/15/30 分钟 + 日/周/月 K，前复权。
- 表头字段（逻辑 schema）：日期/时间、代码、名称、开高低收、前收、成交量、成交额、换手率、涨幅%、振幅%、是否ST、量比、总股本、流通股本、总市值、流通市值、市盈率、市净率——**比我们 K 线 bulk 仓多出 ST 标记、量比、股本、市值、PE/PB 横截面字段**。
- 更新机制：`sync_url.txt` 配置数据源，Zstd 压缩传输、增量同步、断点续传。
- 复权：内置复权因子，"写时计算"（查询时算，不回刷历史）。
- 5 种调用接口：Python SDK（`rd.get_data()` 批量查询、`zb.get()` 39 种批量指标、`bk.get()` 板块映射）、HTTP API、Excel/WPS JS 宏、HTML 视图、AI MCP。

### 差异与风险
- **无 SQL 表结构可复用**：闭源 C++ 引擎私有格式，schema/索引读不到。可借鉴的只有字段清单和"本地落盘 + 批量扫描 + 写时复权"架构思路。
- 与我们 K线 bulk 仓定位重叠，直接采用等于替换基础设施，**不建议引入二进制黑盒**。
- License 风险：**高**。fork 顶层无 LICENSE 文件（上游 MIT）；核心是可执行二进制无法审计，带"测试期 2000 次限制、远端流量升级"运营痕迹，存在供应链与数据投毒风险。只抄思路，不接入。

## 3. oficcejo/tdx-api（Star ~594，README 自称 MIT；上游 injoyai/tdx 为 MIT，Star 652）

**定位**：injoyai/tdx（Go 通达信协议解析库）的 fork，加了 Web UI + 32 个 RESTful 接口 + Docker。核心协议能力来自上游。

### 协议封装方式（上游 injoyai/tdx 文档确认）
- 标准行情走 **TCP 7709 端口**（通达信公共 hq 服务器），扩展行情（期货/港股/外盘）走 **7727 端口**，需独立 DialExHq 连接。
- 服务器按地域内置子表（上海/北京/广州/武汉，华为云/腾讯云 IP），支持最快服务器选择、断线重连（WithRedial）、连接池、DefaultRetry=3。
- 已实现协议功能：五档盘口 GetQuote、代码数量/列表、分时/历史分时、分时成交/历史分时成交、K线（10 种周期 1分钟~年K）/指数K线、股本变迁 GetGbbq、集合竞价、财务/F10、板块（zhb.zip 经 **0x06B9 report file 协议**整体下发，含 tdxzs.cfg/tdxbk.cfg/block_gn.dat 概念/block_hy.dat 行业/block_zs.dat 指数/tdxhy.cfg 行业归属等 44 个文件）。
- **北交所 workaround**：标准协议不支持 market=2，通过 zhb.zip 内 `tdxbjmore.cfg` 或爬虫北交所官网补代码表。
- **复权算法亮点**：基于 gbbq 除权除息事件做仿射变换 `price_adj = QFQMul × price_raw + QFQAdd`，四舍五入到分，宣称与通达信桌面端逐日对齐；gbbq 数据本地 sqlite 缓存 + cron 自动更新。成交量/额不复权。

### 与 mootdx/pytdx 的差异
- pytdx：Python 版标准协议客户端，维护停滞；mootdx 在其上加了本地行情文件读取与最优 IP 探测。
- injoyai/tdx 系（含 tdx-api）：Go 实现，自带连接池/重试/断线重连/最快节点、复权引擎对齐桌面端、扩展行情 7727、zhb.zip 板块文件协议、北交所补丁，并以 REST API + Docker 封装（/api/quote、/api/kline、/api/minute 等 32 个接口）。

### 对我们的增量
- 若暂无通达信 TCP 源：**免费、无鉴权、无限频的实时五档/分笔/10周期K线通道**，是 HTTP 爬虫的强互补；Go 版协议帧结构可作为自研 Python 版的参照。
- gbbq 仿射复权算法 + sqlite 缓存 + cron 更新，可对照校验/改进复权逻辑。
- zhb.zip（0x06B9）一次拉全板块/行业/概念映射文件，比逐页爬概念成分高效。

## 可内化建议汇总
1. **接入通达信 7709 协议源**（参照 injoyai/tdx 的协议与 hosts 表自研，避开 fork 的许可证模糊），补齐免费实时五档/分笔与复权基准。
2. **从 adata 抄 6 个增量接口**：百度股市通四接口（K线/分时/五档/分笔）、东财龙虎榜营业部明细、东财人气榜+同花顺热度榜、通达信扫雷打分 json、东财 fflow 资金流、北向资金双源降级；并吸收其"主源→备源空结果降级链 + 重试/限速 + 全局代理"三件套。
3. **free-stockdb 只抄思路不接入**：横截面字段（ST/量比/股本/市值/PE-PB）并入日K宽表设计、增量同步+断点续传、写时复权、MCP 接口形态；二进制本体因 License 与供应链风险不接入。

## 结论
最值得内化的三点：① tdx-api/injoyai/tdx 的通达信 7707/7709 协议——免费无鉴权的实时五档、分笔、10 周期 K 线，及 gbbq 仿射复权算法，建议参照自研；② adata 的多源降级链架构与增量 endpoint（百度股市通、龙虎榜营业部明细、人气/热度榜、扫雷打分、北向资金），Apache-2.0 可直接借鉴；③ free-stockdb_1 仅取架构思路，闭源二进制与缺失 LICENSE 有供应链风险，禁止接入。
