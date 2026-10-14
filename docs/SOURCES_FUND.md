# 基金 / 养基 类数据源调研报告

> 调研时间：2026-10-14 20:16–21:10 (UTC)
> 调研方式：curl / python urllib 实测（超时 ≤10s，UA 统一 `Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)`）
> 测试基金样本：`110022` 易方达消费行业股票、`161725` 招商中证白酒指数(LOF)、`000001` 华夏成长混合
>
> **诚实声明**：本报告所有「实测可用 / 不通」结论均为上述时间点的单次或数次实测结果，非统计证据。
> 第三方接口随时可能变更、限流或加鉴权；接入前应重新探测，并遵守各站 robots 与服务条款。

---

## 一、用户点名的 P0 产品调研结果

### 1. 养基场 MyFundFarm（myfundfarm.com）

| 项 | 结论 |
|---|---|
| 站点类型 | SPA（Vite + React 19，esm.sh importmap），HTML 壳 3685 字节，无 SSR 数据 |
| 后端 API | `https://api.myfundfarm.com/api/v1`（FastAPI 风格，`{"code":0,"message":"success"}` 包装） |
| 免登录可用 | **仅 1 个**：`GET /api/v1/market/status` → `{"marketStatus":"closed","lastTradeDate":"2026-10-14"}` |
| 需登录 | `/funds`、`/holdings`、`/watchlist`、`/nests`、`/arena/*`、`/ai/*` 全部返回 `401 {"detail":"Not authenticated"}` |
| 能力 | 基金持仓管理、自选、收益曲线、AI 基金分析、OCR 导入持仓截图、投资竞技场（arena）、MCP 服务（`/mcp`） |
| 附带 | 公开 CLI `https://www.myfundfarm.com/cli/fundfarm-cli-*`、skill 文档 `myfundfarm.com/skills/fundfarm-skill.md`（实测返回空）、GitHub Releases `cedar202510-dotcom/fundfarm-releases` |
| 结论 | **产品型数据源，非行情源**。其持仓/收益数据属用户私有，需用户授权 token。仅「市场开闭市状态」可免登录取用。 |

**实测记录**
```
GET https://api.myfundfarm.com/api/v1/market/status        → 200  {"code":0,"data":{"marketStatus":"closed","lastTradeDate":"2026-10-14"}}
GET https://api.myfundfarm.com/api/v1/funds?keyword=110022 → 401  {"detail":"Not authenticated"}
GET https://api.myfundfarm.com/api/v1/holdings              → 401  {"detail":"Not authenticated"}
GET https://api.myfundfarm.com/api/v1/market/pulse          → 连接失败（容器网络不可达，非站点问题）
```

---

### 2. 亿金库 / 养鸡场 yjc.fund

| 项 | 结论 |
|---|---|
| 站点类型 | SPA（Vite + Vue + Monaco 编辑器，1.3MB bundle，含混淆） |
| 自有 API | `/api/funds/sina-valuation`（POST，需登录）、`/api/announcements/latest`、`/api/donation/sponsors`、`/api/global/get`、`/api/ai/recognize-funds`、`/api/auth/login` |
| 免登录可用 | `/api/announcements/latest`（公告）、`/api/donation/sponsors`（赞助名单）、`/api/global/get`（返回 `{"Rt":2,"Rc":102,"data":null}`，无意义） |
| 需登录 | `/api/funds/sina-valuation` → `{"code":-1,"msg":"未登录或登录已过期"}` |
| 上游依赖（从 bundle 提取） | 天天基金 `FundSearch/api/FundSearchAPI.ashx?m=9&key=`、东财 `push2/api/qt/stock/get`、`/api/qt/clist/get`、`/backend/api/webreport` |
| 结论 | **无自有行情数据**，全部转卖天天基金/东财。产品价值在「AI 写代码改站」的工作台，不是数据源。 |

**实测记录**
```
GET  https://yjc.fund/api/announcements/latest   → 200 {"code":1,"data":{"id":18,"title":"AI市场",...}}
GET  https://yjc.fund/api/donation/sponsors      → 200 {"code":1,"data":{"sponsors":[...]}}
GET  https://yjc.fund/api/global/get             → 200 {"Rt":2,"Rc":102,"data":null}
POST https://yjc.fund/api/funds/sina-valuation   → 200 {"code":-1,"msg":"未登录或登录已过期"}
```

---

### 3. 基估宝（jigubao.com / fund.cc.cd / funds.wwliang.cn）

**这是本次调研最大的发现**：基估宝是一个 Next.js 应用，但它的 `/api/*` 后端把天天基金、同花顺(10jqka)、新浪三个上游封装成了免登录的 JSON 接口，可直接调用。

| 子项 | 结论 |
|---|---|
| 站点类型 | Next.js SSR + API Routes |
| API 前缀 | `https://www.jigubao.com/api/*`（免登录、免 token） |
| 分页参数 | `page` + **`count`**（不是 `size`；`size` 无效只会返回 1 条） |
| CORS | OPTIONS 返回 204，GET 带 `Origin` 也返回 200，**前端可直接跨域调用** |
| 上游自标 | 响应 `data.source` 字段直接标注来源：`eastmoney-rankhandler` / `eastmoney` / `sina_ds2` / `sina` / `10jqka` |

#### 3.1 基估宝可用接口清单（全部实测 200）

| 接口 | 上游 | 数据量 | 字段 |
|---|---|---|---|
| `GET /api/funds/search?keyword=110022` | eastmoney-code-list | 全量基金 | `code, name, type` |
| `GET /api/funds/ranking/returns?type=all&sortKey=F009&sortOrder=desc&page=1&count=20` | eastmoney-rankhandler | **total 20561** | `code,name,updateDate,dailyGrowth,weekReturn,monthReturn,quarterReturn,halfYearReturn,yearReturn,twoYearReturn,threeYearReturn,currentYearReturn,sinceReturn,netValue,accumulatedNetValue,detailUrl` |
| `GET /api/funds/ranking/valuation?type=all&sortKey=estimateChange&sortOrder=desc&page=1&count=20` | sina_ds2 | total 175 | `code,name,type,navDate,netValue,dailyGrowth,estimateNav,estimateChange,estimateTime,heat` |
| `GET /api/funds/ranking/exchange?size=3` | sina | 20 条 | ETF 行情：`symbol,code,name,trade,priceChange,changePercent,buy,sell,settlement,open,high,low,volume,amount,tickTime,stateText` |
| `GET /api/funds/ranking/finance?size=3` | 10jqka | total 1 | 货币基金 `code,name,netValue,...` |
| `GET /api/funds/ranking/hong-kong?size=3` | eastmoney-hkfund | total 190 | `code,hkCode,name,fullName,currency,netValue,netValueDate,dailyGrowth,weekReturn,monthReturn,quarterReturn,halfYearReturn,yearReturn` |
| `GET /api/funds/ranking/investment?size=3` | eastmoney | total 16469 | **五星评级榜**：`code,name,netValue,netValueDate,oneYearReturn,twoYearReturn,threeYearReturn,fiveYearReturn,rating,fee` |
| `GET /api/funds/ranking/topic?size=3` | eastmoney | total 132 | 主题选基榜 |
| `GET /api/funds/ranking/net-values?count=3&page=1` | 10jqka | total 22774 | 净值排行 |
| `GET /api/funds/companies?count=3` | 10jqka | total 163 | 基金公司排名：`orgId,name,shortLetter,管理规模` |
| `GET /api/funds/companies/funds?orgId=00064423&kind=netValues&sortKey=rate&sortOrder=desc&count=20` | 10jqka | total 772 | 基金公司旗下产品 |
| `GET /api/funds/tags/public` | 本地 | — | 基金标签/主题词表（AI应用、CPO、PCB、保险…） |
| `GET /api/market-indices/quotes?codes=...` | 上游 | 1–50 | 指数行情（需有效指数代码，`000001` 报「不受支持的指数代码」） |
| `GET /api/funds/technical-status?code=110022` | — | — | **401 需登录/VIP** |
| `POST /api/ocr/analyze-fund` | — | — | 持仓截图 OCR（需登录） |

**实测样本**
```json
// GET https://www.jigubao.com/api/funds/ranking/valuation?type=all&sortKey=estimateChange&sortOrder=desc&page=1&count=3
{"success":true,"data":{"source":"sina_ds2","total":175,"rows":[
 {"rank":1,"code":"004243","name":"广发道琼斯石油指数人民币C","type":"指数型-海外股票",
  "navDate":"2026-10-13","netValue":3.2817,"dailyGrowth":2.91,
  "estimateNav":3.3364,"estimateChange":4.62,"estimateTime":"2026-10-14 16:04"}]}}
```
```json
// GET https://www.jigubao.com/api/funds/search?keyword=110022
{"success":true,"data":[{"code":"110022","name":"易方达消费行业股票","type":"股票型"}],"source":"eastmoney-code-list"}
```

**结论**：**强烈推荐接入**。一个域名为项目同时提供 2 万只基金的阶段收益榜、估值榜、ETF 行情、公司排名、评级榜，且免鉴权、CORS 友好、自带 `source` 溯源字段。

---

### 4. 小倍养基 / 小贝养基（xiaobeiyangji.com / betasmart.tech）

| 项 | 结论 |
|---|---|
| 站点类型 | 双 SPA：`www.xiaobeiyangji.com`（Vite+Vue，905KB bundle）、`www.betasmart.tech/xb_nurture/home`（老版 webpack） |
| API | `https://api.xiaobeiyangji.com/yangji-api/api/{name}` |
| 方法 | **POST + JSON body**（GET 一律 404） |
| 请求头 | `Referer`/`Origin: https://www.xiaobeiyangji.com`、`Token:`（可空）、`VERSION: 3.0.2.1` |
| 鉴权 | 公开接口**无需 token**（空 `Token:` 头即可返回 200） |
| CORS | OPTIONS 204，`access-control-allow-origin` 回显请求 Origin，**前端可跨域** |

#### 4.1 实测可用的免登录接口

| 接口（POST） | 内容 | 实测 |
|---|---|---|
| `get-up-ranking` | **基金加仓榜 / 减仓榜**，各 100 条 | `{"data":{"upList":[{"code":"019711","change":0.0295,"name":"广发道琼斯石油指数(QDII-LOF)美元现汇E","emType":"qdii","type":"up","date":"2026-10-14"}],"downList":[...]}}` |
| `get-hot-industry-ranking` | **板块热度榜**（含东财板块码映射） | `{"code":200,"list":[{"themeCode":"BK000387","themeName":"游戏","mappingCode":"BK1046","change":0.0379,"market":0},...]}` |
| `get-quote` | 大盘指数 + 外盘期货（A50期指、COMEX黄金、NYMEX原油） | `{"list":[{"data":[{"symbol":"SH000001","name":"上证指数","percent":-0.88,"chg":-33.63,"current":3767.23},...],"CN00Y":{...},"GC00Y":{...},"CL00Y":{...}}]}` |
| `get-trading-date` | 交易日历 | `{"preTradingDate":"2026-10-13","lastTradingDate":"2026-10-14","nextTradingDate":"2026-10-15","isTradingDate":true,"nowDate":"2026-10-14","nowTime":"21:02:09"}` |
| `get-dynamic` / `get-comment` / `get-has-position-rank` / `get-hold-list` / `get-ranking-operate` | 社区动态、评论、持仓榜、我的持仓、买卖榜 | 均 200 但**返回空数组**（需登录 token 才有数据） |

**接口命名空间（从 bundle 提取）**
```
get-quote, get-dynamic, get-comment, get-ranking-operate-v2191(404),
get-has-position-rank, get-up-ranking, login-v300app, web-out,
change-status-dynamic, change-dynamic, change-comment,
get-hot-industry-ranking-v2191(404) → 实际可用名为 get-hot-industry-ranking,
get-hold-list, to-user-space-v219, get-trading-date
```

**结论**：**推荐接入**。`get-up-ranking`（基民加仓/减仓情绪榜）与 `get-hot-industry-ranking`（板块热度）是市场上少见的「养基行为数据」，与现有 A 股数据形成互补。

---

### 5. 养基宝（yangjibao.com）

| 项 | 结论 |
|---|---|
| 服务端 | 老接口 `http://browser-plug-api.yangjibao.com`、新接口 `https://app-api.yangjibao.com` |
| 公开接口（据第三方逆向文档） | `/index_data`、`/qr_code`、`/qr_code_state/*`、`/notice`、`/version_info` |
| 实测 | **全部 404**（`/index_data`、`/notice`、`/version_info?version=1.1.4`） |
| 其余接口 | 需登录 token + **签名验证**（secret 需从 IPA 解包逆向） |
| 结论 | **不通**。老接口已下线或路径变更；新接口签名门槛高，不建议接入。文档参考：`github.com/Ye-Yu-Mo/yjb-api`、`chinacarlos.github.io/fund-helper/developer/yjb-api.html`（第三方逆向，非官方） |

---

### 6. 和讯基金（funds.hexun.com）

| 项 | 结论 |
|---|---|
| 用户给的 URL | `https://funds.hexun.com/2025-05-13/218992978.html` → 200，但正文为「内容正在升级改造，请稍后再试！」（文章已下架） |
| 频道首页 | `https://funds.hexun.com/` → 200，**返回 989 字节 JS 反爬 cookie 挑战**（`__tst_status` / `EO_Bot_Ssid`），非数据 |
| 免登录 API | `https://m.hexun.com/api/getHotFocus` → 200，11050 字节热点头条 JSON |
| 免登录 API | `https://m.hexun.com/api/getJrttNews` → 200，今日头条新闻 |
| 其它 | `data.hexun.com` 301；`open.hexun.com` 200 但 `{"code":1,"data":null}` |
| 基金行情接口 | **未找到**。和讯基金频道已无可用行情 API |
| 结论 | 仅「和讯热点头条/今日头条」两个新闻接口可用，**非基金数据源**。基金维度不通。 |

**实测记录**
```
GET https://funds.hexun.com/2025-05-13/218992978.html → 200  "内容正在升级改造，请稍后再试！"
GET https://funds.hexun.com/                            → 200  989B  JS cookie 挑战（反爬）
GET https://m.hexun.com/api/getHotFocus                 → 200  11050B {"datas":[{"title":"...","mNewsUrl":"..."}]}
GET https://m.hexun.com/api/getJrttNews                 → 200
GET http://api.money.hexun.com/gsms/lastest             → 连接失败
```

---

### 7. 金投网（m.cngold.org）

| 项 | 结论 |
|---|---|
| 站点类型 | SSR 页面（HTML 内含行情），非 SPA |
| 发现的接口 | `https://api.jijinhao.com/quoteCenter/realTime.htm?codes={CODE}`（JSONP：`var quote_json = {...}`） |
| 代码格式 | **仅支持金投网自有代码**（`JO_` 前缀） |
| 实测 | `codes=JO_92233` → 200，返回完整行情 `q63` 最新价 / `q64` / `q80` 涨跌幅 / `q70` 等 |
| A 股 / 基金 | `SH_600519`、`fu_110022`、`of_110022` → 200 但 `{"flag":true,"errorCode":["..."]}`，**不支持** |
| 并发限制 | 一次请求多个代码且含不支持的代码时返回 **HTTP 666** |
| 页面 | `m.cngold.org/fund/` 可访问（38KB SSR），基金页内嵌 `jijinhao.jtw` 跳转；`m.cngold.org/quote/` 有期货/外汇/贵金属/股票行情页 |
| 结论 | **基金维度不通**。仅黄金/外汇/期货自有代码可用。若项目需要贵金属行情，可单独评估。 |

---

### 8. 雪球（xueqiu.com）

注册表中 `xueqiu_hot` 标记为 blocked 需 cookie。本次实测**找到一条免 cookie 的可用链路**。

| 接口 | 鉴权 | 实测 |
|---|---|---|
| `GET xueqiu.com/service/v5/stock/screener/quote/list?type=sha&order_by=percent&market=CN&size=30&page=1` | **免 cookie** | **200**，`count:1702`，返回完整行情+财务字段 |
| 同上 `type=sza` | 免 cookie | 200，`count:2906` |
| 同上 `type=hk` | 免 cookie | 200，`count:2883` |
| 同上 `type=us` | 免 cookie | 200，`count:5000` |
| `stock.xueqiu.com/v5/stock/quote.json` | 需 cookie | 400 `遇到错误，请刷新页面或者重新登录帐号` |
| `stock.xueqiu.com/v5/stock/chart/kline.json` | 需 cookie | 400 同上 |
| `stock.xueqiu.com/v5/stock/f10/cn/fund/detail.json` | 需 cookie | 400 同上 |
| `xueqiu.com/cubes/list.json`（组合） | 需 cookie | 400 同上 |
| `xueqiu.com/statuses/hot/listV2.json`（热榜） | 阿里云 WAF | 200 但返回 WAF 挑战 HTML |
| `xueqiu.com/service/v5/stock/batch/quote.json` | — | 403 openresty |
| `xueqiu.com/service/v5/stock/hot_stock/list.json` | — | 403 openresty |

**可用排序键（实测）**：`percent`（涨跌幅）、`amount`（成交额）、`volume_ratio`（量比）、`north_net_inflow`（北向净流入）、`main_net_inflows`（主力净流入）、`followers`（关注人数）、`roe_ttm`（ROE）。
**不可用排序键**：`follow`、`follow7d`、`follow7d_count`、`tweet`、`deal` → 返回空 list。
**type 取值**：`sha` / `sza` / `hk` / `us` 有效；`etf` / `lof` / `fund` → 空 list。
**分页**：`size` 上限约 30（传 200 只回 30），`page` 有效。

**返回字段（sha 样本）**
```json
{"data":{"count":1702,"list":[{"symbol":"SH600252","name":"中恒集团","current":2.48,
 "percent":10.22,"chg":0.23,"amplitude":11.11,"volume":196457645,"amount":478186341,
 "volume_ratio":2.25,"turnover_rate":6.17,"market_capital":7896724414,
 "float_market_capital":7896724414,"total_shares":3184163070,"float_shares":3184163070,
 "pe_ttm":null,"pb":1.511,"pb_ttm":1.511,"ps":5.4749,"pcf":86.6084,
 "eps":-0.14,"roe_ttm":-7.796,"dividend_yield":null,
 "north_net_inflow":16462358.46,"main_net_inflows":22730791,
 "net_profit_cagr":99.83,"income_cagr":-25.46,"current_year_percent":-9.49,
 "total_percent":389.06,"first_percent":245.9,"limitup_days":1,"followers":80852}]}}
```

**结论**：**推荐接入**（替代原 blocked 的 xueqiu_hot）。screener 链路免 cookie、字段极丰富（含北向/主力资金流、CAGR、关注数、量比），是「选股器」型数据，可直接支撑筛选与热度排序。

---

## 二、上游免费接口实测（P1 抓包的实质产出）

以上产品背后几乎都指向同一批免费上游。这些上游才是真正的数据源，且**大多免 cookie、CORS 开放**。

### A. 天天基金 / 东方财富系

#### A1. `fundsuggest.eastmoney.com` — 基金搜索
```
GET https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=9&key=110022
```
- 实测 **200**，3.2KB，`Content-Type: application/json`
- 鉴权：无；CORS：OPTIONS 200
- 字段：`CODE` / `NAME` / `SHORTNAME` / `CATEGORYDESC` / `FundBaseInfo{DWJZ 净值, FSRQ 净值日期, FTYPE 类型, JJGS 基金公司, JJJL 基金经理, MINSG 最小申购, ISBUY}` / `ZTJJInfo[{TTYPE,TYPENAME}]` 主题板块
- `m=1&key=白酒` 支持按名称模糊搜索
- **价值：高**。基金代码/名称/类型/经理/公司一站式，免鉴权。

#### A2. `fundmobapi.eastmoney.com` — 天天基金移动端 API 族（免 cookie，`Access-Control-Allow-Origin: *`）
```
公共 query：&deviceid=1&plat=Android&product=EFund&version=6.2.8
```

| 接口 | 实测 | 返回 |
|---|---|---|
| `FundMNewApi/FundMNBasicInformation?FCODE=110022` | **200** | 单位净值 `DWJZ`、累计 `LJJZ`、日涨跌 `RZDF`、估算偏差 `ESTDIFF`、**阶段收益 `SYL_Z/Y/3Y/6Y/1N/2N/3N`**、夏普 `SHARP1`、最大回撤 `MAXRETRA1`、标准差 `STDDEV1`、规模 `FEGM`(36.5亿)、净资产 `ENDNAV`、经理 `JJJL`、公司 `JJGS`、费率 `SOURCERATE/RATE`、风险等级 `RISKLEVEL`、投资理念 `FUNDINVEST`、主题 `FUNDSUBJECTLIST` |
| `FundMNewApi/FundMNPeriodIncrease?FCODE=110022` | **200** | 阶段收益+同类排名：`[{title:"Z",syl:"-1.00",avg:"-4.15",hs300:"-3.21",rank:"128",sc:"954",diff:"64"}, ...]` 覆盖 Z/Y/3Y/6Y/1N/2N/3N |
| `FundMNewApi/FundMNInverstPosition?FCODE=110022` | **200** | **前十大重仓股**：`[{GPDM:"600519",GPJC:"贵州茅台",JZBL:"9.77",PCTNVCHGTYPE:"减持",PCTNVCHG:"-0.13",INDEXNAME:"食品饮料"}]` |
| `FundMNewApi/FundMNInverstPositionNew?FCODE=161725` | **200** | **一次返回三份数据**：`InverstPosition{fundStocks 重仓股, fundboods 重仓债券, fundfof...}` + `AssetAllocation{FSRQ,GP,ZQ,HB,JZC,QT,JJ}` + `SectorAllocation{FSRQ,HYMC,SZ,ZJZBL}` 行业配置 |
| `FundMNewApi/FundMNAssetAllocation?FCODE=161725` | **200** | 历史资产配置（多报告期） |
| `FundMNewApi/FundMNValuationDetail?FCODE=110022` | 200 | `Datas:null`（无数据，可能已停） |
| `FundMNewApi/FundMNManagerList` 等 | **404** | 基金经理、历史净值、排行接口名未命中，需继续爆破 |

- CORS：`Access-Control-Allow-Origin: *`（OPTIONS 预检 200）
- **价值：极高**。这是基金档案（F10）的核心来源：净值、阶段收益、重仓股、资产配置、行业配置、风险指标全都有，且免鉴权 + 全开 CORS。

#### A3. `fund.eastmoney.com/pingzhongdata/{code}.js` — 基金档案全量数据包
```
GET https://fund.eastmoney.com/pingzhongdata/161725.js
```
- 实测 **200**，566KB（161725）/ 368KB（110022）JS 变量包
- 鉴权：无；CORS：405 on OPTIONS（浏览器跨域受限，服务端抓取无碍）
- 内置变量（实测解析成功）：
  - `Data_netWorthTrend` — **成立以来全部单位净值**（161725 有 2769 个点，`[{x:时间戳,y:净值,equityReturn,unitMoney}]`）
  - `Data_ACWorthTrend` — 累计净值序列 `[[ts, value], ...]`
  - `Data_grandTotal` — 同类排名对比序列
  - `Data_rateInSimilarType` — 同类百分位
  - `Data_fundSharesPositions` — 份额变动序列
  - `Data_currentFundManager` — **现任基金经理**：`{id,pic,name,star,workTime,fundSize:"440.89亿(21只基金)",power{avr,categories,data},profit{...}}`
  - `Data_holderStructure` — **持有人结构**（机构/个人/内部持有比例）
  - `Data_assetAllocation` — 资产配置（股票/债券/现金占净比）
  - `Data_fluctuationScale` — 规模变动（`{categories,series:[{y,mom}]}`）
  - `Data_performanceEvaluation` — 五维评分（选证能力/收益率/跟踪误差/超额收益/管理规模）
  - `stockCodes` / `stockCodesNew` / `zqCodes` — 持仓股票/债券代码
- **价值：极高**。单次请求拿到一只基金的完整生命周期数据，适合做基金详情页与历史曲线。

#### A4. `fund.eastmoney.com/js/fundcode_search.js` — 全量基金代码表
- 实测 **200**，**3.19MB**，`var r = [["000001","HXCZHH","华夏成长混合","混合型-灵活","HUAXIACHENGZHANGHUNHE"],...]`
- 鉴权：无
- **价值：高**（离线建库用）。一次拉全市场基金代码/拼音/名称/类型，避免逐只查。

#### A5. 东财行情接口 — **当前不可达**
```
GET https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.510300&fields=...   → 连接失败 (exit 56)
GET https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=...       → 连接失败
GET https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.510300&...     → 连接失败（本容器网络/或被封锁）
```
注：`push2his.eastmoney.com` 的 K 线接口在本次调研早期曾成功返回（`{"data":{"klines":["2026-10-08,4.389",...]}}`），后续同参数重试全部连接重置，属**不稳定/被限流**。接入时需多重试与降级。

#### A6. 已失效的东财基金接口（实测不通，记录备查）
```
GET https://fundgz.1234567.com.cn/js/110022.js            → 200 但返回「页面未找到」HTML（估值 JSONP 已下线）
GET https://api.fund.eastmoney.com/FundGuZhi/GetFundGuZhiList → 200 {"ErrCode":4,"ErrMsg":"404"}
GET https://api.fund.eastmoney.com/f10/lsjz               → 200 {"ErrCode":-999}
GET https://fund.eastmoney.com/data/rankhandler.aspx      → 200 {"ErrCode":-999,"Data":"无访问权限"}
GET https://fundcomapi.tiantianfunds.com                  → 403 Forbidden
GET https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHistoryNetValue → 404
```
> 注：`fundgz.1234567.com.cn/js/{code}.js` 是全网教程最常引用的基金实时估值 JSONP 接口，**现已确认下线**。第三方文档（如 `github.com/Aliyastlly/FundEval`、`xie268/millionfund`）仍大量引用它，不可照抄。实时估值需改用「重仓股加权法」自行计算（天天基金 App 已于 2026-02-02 关闭估值功能）。

---

### B. 蛋卷基金（danjuanfunds.com）

| 接口 | 实测 | 说明 |
|---|---|---|
| `GET https://danjuanfunds.com/djapi/fund/110022` | **200**，7840B，`application/json` | 需 `Referer: https://danjuanfunds.com/` |
| `GET https://danjuanfunds.com/djapi/index_eva/dj` | **200**，31933B | 指数估值表（PE/PB/百分位/ROE/股息率） |
| `GET https://danjuanfunds.com/djapi/fund/base/110022` | 200 `{"result_code":300001,"message":"请重新登录"}` | 需登录 |
| `GET https://danjuanfunds.com/djapi/fund_nav/growth/day` | 200 同上 | 需登录 |
| `GET https://danjuanfunds.com/djapi/v3/filter/fund?type=1` | 同上 | 需登录 |
| OPTIONS 预检 | **403** | CORS 不开放，仅服务端可用 |

**`/djapi/fund/{code}` 字段（实测 161725/110022）**
```json
{"fd_code":"110022","fd_name":"易方达消费行业","fd_full_name":"易方达消费行业股票型证券投资基金",
 "found_date":"2010-08-20","totshare":"97.06亿","keeper_name":"易方达基金管理有限公司",
 "manager_name":"萧楠","risk_level":"3",
 "fund_derived":{"end_date":"2026-10-14","unit_nav":"2.7620","nav_grtd":"-0.0724",
   "nav_grl1m":"-2.64","nav_grl3m":"0.40","nav_grl6m":"-11.90","nav_grlty":"-19.78",
   "nav_grl1y":"-22.50","nav_grl3y":"-24.68","nav_grl5y":"-41.59","nav_grbase":"176.2",
   "srank_l1m":"316/1123","srank_l3m":"204/1100","srank_l6m":"764/1076",
   "srank_l1y":"923/1029","srank_l3y":"792/839","srank_l5y":"555/639",
   "annual_performance_list":[{"period":"2025","nav":"-4.17","rank":"1033/1065"},...]}}
```
**价值：高**。阶段收益 + **同类排名百分位** + **年度业绩表**，结构比天天基金更干净。`index_eva/dj` 的指数估值（PE/PB 历史百分位）对「低位布局」逻辑很有用。

---

### C. 新浪财经

#### C1. `hq.sinajs.cn/list=fu_{code}` — 基金实时行情（免 cookie，需 Referer）
```
GET https://hq.sinajs.cn/list=fu_110022
Header: Referer: https://finance.sina.com.cn
```
- 实测 **200**
- 返回（GB18030）：
```
var hq_str_fu_110022="易方达消费行业股票,16:03:00,2.7589,2.7640,2.7640,0,-0.1835,2026-10-14,2.7608,-0.116";
```
- `of_{code}` 前缀：`var hq_str_of110022="易方达消费行业股票,2.764,2.764,2.807,-1.53,2026-10-13";`（开放式基金净值格式）
- 批量：`list=fu_000001,fu_110022,fu_161725` → 一次返回多只，实测 297 字节
- **注意**：不带 `Referer` 头实测连接失败（`curl` code 000 / urllib 403）
- **价值：高**。轻量、批量、实时，适合做自选基金行情刷新。

#### C2. `vip.stock.finance.sina.com.cn/fund_center/api/jsonp.php/NetValue_Service.getAllCompany`
- 实测 **200**，18473B，返回全部基金公司 `[{code:"80000080",name:"山西证券股份有限公司"},...]`
- 需 `Referer: http://money.finance.sina.com.cn/fund_center/`
- 同目录下其它 Service（`openFundRank`、`getFundNav`、`getFundByType`、`FundRank_Service.*`）实测均返回 `{"__ERROR":0,"__ERRORMSG":"Invalid service name"}` — **服务名已变更，未找到正确名称**
- **价值：中**。仅基金公司名录可用。

#### C3. `vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData`
- 实测 **200**，1865B，A 股列表分页（`node=hs_a`，`page/num/sort/asc`）
- 非基金维度，但对「重仓股→A 股」穿透分析有用。

#### C4. `finance.sina.com.cn/money/fund/` → **403 Forbidden**（不通）

---

### D. 天天基金网网页端（SSR，可服务端解析）

| 页面 | 实测 | 可解析内容 |
|---|---|---|
| `https://fund.eastmoney.com/161725.html` | 200，127KB | 基金主页 |
| `https://fundf10.eastmoney.com/jjjz_110022.html` | 200，46KB | 历史净值表（HTML 表格） |
| `https://fundf10.eastmoney.com/ccmx_110022.html` | 200，37KB | **持仓明细**（HTML 表格） |
| `https://fundf10.eastmoney.com/jbgk_161725.html` | 200，44KB | 基金概况 |
| `https://fund.eastmoney.com/data/fundranking.html` | 200，40KB | 开放式基金排行页 |
| `https://fund.eastmoney.com/data/fundrating.html` | 200，**3.14MB** | 基金评级全表 |
| `https://fund.eastmoney.com/data/fundfenhong.html` | 200，38KB | 基金分红 |

- 注：这些页面的**数据是前端 JS 异步拉的**，HTML 里没有内联数据（`Data_netWorthTrend` 等内联变量只在 `pingzhongdata` 里）。
- 价值：中（作为 A2/A3 的兜底，需 HTML 解析）。

---

### E. 其它发现

#### E1. GitHub Pages 免费基金数据库
```
https://LST-Serendipity.github.io/fund-data-api/funds_simple.json   (~2MB, 每日 18:30 更新, 26730 只)
https://LST-Serendipity.github.io/fund-data-api/funds_full.json.gz  (~3MB, 含拼音缩写)
```
- 来源：`github.com/LST-Serendipity/fund-data-api`（akshare 驱动 + GitHub Actions）
- 字段：`code` / `name` / `type` / `pinyin`
- **未实测**（本项目红线：仅记录搜索明确出现的地址；且 GitHub Actions 生成的静态库随时可能停更）
- 价值：中（可作 A4 的备选全量代码表）

#### E2. 商业付费 API（需 licence，记录备查）
| 服务 | 文档 | 说明 |
|---|---|---|
| 麦蕊智数 mairui.club | `mairui.club/news/fund-hq-archive-apis-batch1-20260809` | `/jj/*` 15 条基金接口（列表/估值/K线/MA/F10/分红），路径末带 licence |
| 必盈 API biyingapi.com | `biyingapi.com/doc_jj` | `/fd/list/all/{licence}` 等基金数据 |
| 智途 API zhituapi.com | `zhituapi.com/fundmarketapi.html` | `/jh/list/all?token=` 全量基金 + 净值排名 |

- 实测 `https://api.mairui.club/hslt/list/xxxx` → 404 `{"code":404,"message":"Licence证书不存在"}`（确认鉴权机制为路径 licence，无免费额度）
- 价值：备选。若免费源不稳定可考虑采购。

---

## 三、实测不通清单（诚实记录）

| 目标 | 结果 | 原因 |
|---|---|---|
| `fundgz.1234567.com.cn/js/{code}.js`（天天基金估值 JSONP） | **不通** | 已下线，返回「页面未找到」HTML |
| `push2.eastmoney.com` / `push2his.eastmoney.com` | **不通** | 连接重置（curl exit 56），早期曾成功，判定为限流/封锁 |
| `fund.eastmoney.com/data/rankhandler.aspx` | **不通** | `{"ErrCode":-999,"Data":"无访问权限"}` |
| `api.fund.eastmoney.com/f10/lsjz`、`FundGuZhi` | **不通** | `ErrCode:-999` / `ErrCode:4` |
| 养基宝 `browser-plug-api.yangjibao.com` 全部公开接口 | **不通** | 404 |
| 和讯基金行情 API | **未找到** | 频道首页返回 JS 反爬挑战；文章页内容已下架 |
| 金投网 A 股/基金行情 | **不通** | `api.jijinhao.com` 仅支持 `JO_` 自有代码 |
| 雪球 `stock.xueqiu.com/v5/*`、`cubes`、`hot_stock`、`statuses/hot` | **不通** | 需 cookie / 403 / 阿里云 WAF |
| 蛋卷基金 `djapi/fund/base`、`fund_nav/growth`、`v3/filter` | **不通** | `请重新登录` |
| 蛋卷基金 CORS | **不通** | OPTIONS 403 |
| `api.doctorxiong.club/v1/fund` | **不通** | `{"__ERROR":1,"__ERRORMSG":"Input error"}` |
| `qieman.com` 且慢 | **不通** | 连接失败；`api.qieman.com/v1/funds` 404 |
| `fund.cc.cd`（基估宝文档站） | **不通** | Connection reset by peer |
| `funds.wwliang.cn`（基估宝镜像） | **不通** | DNS 解析失败 |
| 新浪 `FundRank_Service.*` / `NetValue_Service.openFundRank` | **不通** | `Invalid service name` |
| 天天基金 `FundMNManagerList` / `FundMNHistoryNetValue` 等 | **不通** | 404，接口名未命中 |

---

## 四、汇总表

### 4.1 可用源总览（共 24 个接口，全部实测返回 200 且有数据）

| # | id | 名称 | URL | 鉴权 | HTTP | 类型 |
|---|---|---|---|---|---|---|
| 1 | `jigubao_returns` | 基估宝-基金收益排行 | `www.jigubao.com/api/funds/ranking/returns` | 无 | 200 | 排行 |
| 2 | `jigubao_valuation` | 基估宝-基金估值排行 | `www.jigubao.com/api/funds/ranking/valuation` | 无 | 200 | 估值 |
| 3 | `jigubao_investment` | 基估宝-五星评级榜 | `www.jigubao.com/api/funds/ranking/investment` | 无 | 200 | 评级 |
| 4 | `jigubao_topic` | 基估宝-主题选基榜 | `www.jigubao.com/api/funds/ranking/topic` | 无 | 200 | 主题 |
| 5 | `jigubao_exchange` | 基估宝-ETF 行情 | `www.jigubao.com/api/funds/ranking/exchange` | 无 | 200 | ETF |
| 6 | `jigubao_hkfund` | 基估宝-香港基金 | `www.jigubao.com/api/funds/ranking/hong-kong` | 无 | 200 | 排行 |
| 7 | `jigubao_netvalues` | 基估宝-净值排行(同花顺) | `www.jigubao.com/api/funds/ranking/net-values` | 无 | 200 | 排行 |
| 8 | `jigubao_companies` | 基估宝-基金公司排名 | `www.jigubao.com/api/funds/companies` | 无 | 200 | 公司 |
| 9 | `jigubao_search` | 基估宝-基金搜索 | `www.jigubao.com/api/funds/search` | 无 | 200 | 搜索 |
| 10 | `ttjj_fund_basic` | 天天基金-基金档案基础 | `fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation` | 无 | 200 | 档案 |
| 11 | `ttjj_period_increase` | 天天基金-阶段收益与同类排名 | `fundmobapi.eastmoney.com/FundMNewApi/FundMNPeriodIncrease` | 无 | 200 | 收益 |
| 12 | `ttjj_holdings` | 天天基金-重仓股+资产+行业配置 | `fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPositionNew` | 无 | 200 | 持仓 |
| 13 | `ttjj_pingzhong` | 天天基金-基金全量档案包 | `fund.eastmoney.com/pingzhongdata/{code}.js` | 无 | 200 | 档案 |
| 14 | `ttjj_fundcode` | 天天基金-全量基金代码表 | `fund.eastmoney.com/js/fundcode_search.js` | 无 | 200 | 字典 |
| 15 | `ttjj_search` | 天天基金-基金搜索 | `fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx` | 无 | 200 | 搜索 |
| 16 | `danjuan_fund` | 蛋卷-基金详情与年度业绩 | `danjuanfunds.com/djapi/fund/{code}` | Referer | 200 | 档案 |
| 17 | `danjuan_index_eva` | 蛋卷-指数估值 | `danjuanfunds.com/djapi/index_eva/dj` | Referer | 200 | 估值 |
| 18 | `sina_fund_hq` | 新浪-基金实时行情 | `hq.sinajs.cn/list=fu_{codes}` | Referer | 200 | 行情 |
| 19 | `sina_fund_company` | 新浪-基金公司名录 | `vip.stock.finance.sina.com.cn/fund_center/api/jsonp.php/NetValue_Service.getAllCompany` | Referer | 200 | 字典 |
| 20 | `xbyj_updown` | 小倍养基-加仓/减仓榜 | `api.xiaobeiyangji.com/yangji-api/api/get-up-ranking` | 无 | 200 | 情绪 |
| 21 | `xbyj_industry` | 小倍养基-板块热度榜 | `api.xiaobeiyangji.com/yangji-api/api/get-hot-industry-ranking` | 无 | 200 | 热度 |
| 22 | `xbyj_quote` | 小倍养基-大盘+外盘 | `api.xiaobeiyangji.com/yangji-api/api/get-quote` | 无 | 200 | 行情 |
| 23 | `xbyj_trading_date` | 小倍养基-交易日历 | `api.xiaobeiyangji.com/yangji-api/api/get-trading-date` | 无 | 200 | 日历 |
| 24 | `xq_screener` | 雪球-选股器(免cookie) | `xueqiu.com/service/v5/stock/screener/quote/list` | 无 | 200 | 选股 |

（另有两个仅新闻用途：`m.hexun.com/api/getHotFocus`、`m.hexun.com/api/getJrttNews`，实测 200 但非基金数据，未计入源。）

### 4.2 按维度覆盖

| 维度 | 可用源 | 缺口 |
|---|---|---|
| 基金代码/名称/类型字典 | #9 #14 #15 | 无 |
| 单位净值/累计净值 | #10 #13 #16 | 无 |
| 日涨跌 | #10 #16 #18 | 无 |
| 阶段收益(1M/3M/6M/1Y/2Y/3Y/5Y/今年/成立来) | #10 #11 #16 #1 #4 | 无 |
| 同类排名/百分位 | #10 #11 #16 #3 | 无 |
| 前十大重仓股 | #12 #13 | **持仓明细（全部持股）仅 HTML** |
| 行业配置/资产配置 | #12 #13 | 无 |
| 基金经理 | #13（`Data_currentFundManager`） | 经理历史业绩需 HTML |
| 规模/份额变动 | #10 #13 | 无 |
| 持有人结构 | #13 | 无 |
| 基金评级 | #3 #13 | 无 |
| 基金公司排名 | #8 #19 | 无 |
| ETF/LOF 行情 | #5 #18 | 无 |
| 基金组合（跟投） | **无** | 雪球组合需 cookie；养基宝需签名；且慢不通 |
| 实时估值（盘中） | #2（175 只，非全量） | **天天基金官方估值 JSONP 已下线**；全量需自建「重仓股加权」估算 |
| 基金社区/情绪 | #20（加仓减仓榜） | 雪球热榜被 WAF 挡 |

---

## 五、Top 5 推荐接入及理由

### 1. `fundmobapi.eastmoney.com` 基金档案 API 族（#10 #11 #12）
**理由**：
- 三个接口覆盖基金 F10 全部核心字段——净值、8 档阶段收益、同类排名、夏普/回撤/波动率、前十大重仓股（含增减持变动）、资产配置、行业配置。
- **免鉴权 + `Access-Control-Allow-Origin: *`**，是本次实测中 CORS 最开放的接口，前后端都能用。
- 与项目现有 A 股数据天然打通：重仓股 `GPDM` 就是 6 位股票代码，可直接做「基金→持仓股」穿透，与已有的板块/热点池联动。
- 替代价值：官方估值 JSONP 已下线，`FundMNInverstPositionNew` 提供的重仓股+占比是**自行计算实时估值的唯一免费原料**。

**接入建议**：`FundMNBasicInformation` + `FundMNPeriodIncrease` + `FundMNInverstPositionNew` 三件套按基金代码批量拉取，日更（净值 16:00 后更新，持仓按季报节奏）。

### 2. `www.jigubao.com/api/*` 基估宝后端（#1–#9）
**理由**：
- **一个域名拿到 9 类榜单**：20561 只基金阶段收益榜、175 只盘中估值榜、16469 只五星评级榜、主题选基榜、ETF 行情、190 只香港基金、22774 只净值排行、163 家基金公司、全量搜索。
- 免 token、CORS 友好、响应自带 `source` 字段标注上游（`eastmoney-rankhandler`/`sina_ds2`/`10jqka`），**可溯源、便于审计**。
- 分页参数 `page`+`count` 简单明确，`total` 字段可直接用于进度条。
- 上游 `rankhandler.aspx` 直连是「无访问权限」，基估宝相当于替项目解决了这个鉴权问题。

**接入建议**：作为基金维度的「榜单层」；`ranking/returns` 做全市场收益扫描，`ranking/valuation` 做盘中异动，`companies/funds` 做基金公司维度聚合。

### 3. `xueqiu.com/service/v5/stock/screener/quote/list`（#24）
**理由**：
- 注册表里 `xueqiu_hot` 已 blocked（需 cookie），**这条 screener 链路免 cookie 实测 200**，是雪球维度唯一可用入口。
- 单条返回 40+ 字段：行情、量比、换手、市值、PE/PB/PS、EPS、ROE、北向净流入、主力净流入、净利润 CAGR、营收 CAGR、年初至今涨幅、关注人数、涨停天数。
- `type=sha/sza/hk/us` 四市场通吃，7 种排序键可用，`size≤30` + `page` 分页。
- 直接替代/扩充现有 `xueqiu_hot`：用 `order_by=followers` 可做出「关注度热度榜」，`order_by=north_net_inflow` / `main_net_inflows` 可做资金流榜。

**接入建议**：替代原 blocked 源；`followers` 排序做热度、`main_net_inflows` 做主力资金、`volume_ratio` 做异动。

### 4. `api.xiaobeiyangji.com/yangji-api/api/*` 小倍养基（#20–#23）
**理由**：
- `get-up-ranking` 提供**基民加仓榜/减仓榜各 100 只**（`change` 为加减仓幅度，`emType` 为基金类型），这是**行为金融维度的独有数据**——全市场只有它在统计「散户在买什么」。
- `get-hot-industry-ranking` 提供板块热度榜，且带 `themeCode`(东财板块码) ↔ `mappingCode`(同花顺/指数码) 双码映射，**可直接桥接项目已有的板块体系**。
- `get-quote` 一次返回 A 股三大指数 + A50 期指 + COMEX 黄金 + NYMEX 原油，做大盘环境判断只需一个请求。
- `get-trading-date` 提供交易日历，可消除项目里「非交易日取数」的判断逻辑。
- POST + JSON body，CORS 回显 Origin，空 `Token:` 头即可访问。

**接入建议**：加仓/减仓榜做「养基情绪指标」，板块热度榜接入板块池，交易日历作为全局工具。

### 5. `danjuanfunds.com/djapi/fund/{code}` + `index_eva/dj`（#16 #17）
**理由**：
- `fund_derived` 里的 `nav_grl1m/3m/6m/1y/3y/5y/grbase` 8 档收益 **与 `srank_l*` 同类排名配对返回**（如 `srank_l1y:"923/1029"`），比天天基金的纯数值更适合做「排名分位」展示。
- `annual_performance_list` 给出**成立以来/今年/近 5 年分年度收益与排名**，是基金详情页最有效的说服材料。
- `index_eva/dj` 返回全指数 PE/PB/ROE/股息率及**历史百分位**（`pe_percentile`/`pb_percentile`/`eva_type`），对「低位布局」类策略有直接价值。
- 需 `Referer: https://danjuanfunds.com/`，CORS 403 → **仅服务端使用**。

**接入建议**：作为 `ttjj_fund_basic` 的交叉校验源（两家独立计算阶段收益，可互查异常）；`index_eva/dj` 单独做「指数估值温度计」模块。

---

## 六、落地注意

1. **不要照抄公开教程里的 `fundgz.1234567.com.cn/js/{code}.js`** —— 已实测下线。若需实时估值，用 `FundMNInverstPositionNew` 的重仓股 + 占比，加权实时行情自算（第三方 `Aliyastlly/FundEval` 项目有完整算法可参考）。
2. **东财 `push2/push2his` 不稳定**（本次出现连接重置）。实时行情优先走 `sina hq.sinajs.cn` 或 `xbyj get-quote`，东财做兜底并加指数退避重试。
3. **必须带 Referer 的源**：`danjuanfunds.com`、`hq.sinajs.cn`、新浪 `jsonp.php`。缺头实测直接 403/连接失败。
4. **必须 POST 的源**：`xiaobeiyangji.com` 全部接口，GET 一律 404。
5. **分页参数陷阱**：`jigubao.com` 用 `count` 不是 `size`；`xueqiu screener` 的 `size` 上限约 30。
6. **雪球 screener 的 `order_by` 白名单**：`percent`/`amount`/`volume_ratio`/`north_net_inflow`/`main_net_inflows`/`followers`/`roe_ttm`，其余键返回空 list（不报错，易误判）。
7. **合规**：以上均为第三方公开接口，无官方开放平台授权。批量抓取前应评估频率限制与服务条款；基金数据涉及投资决策，展示时必须标注数据来源与延迟。
