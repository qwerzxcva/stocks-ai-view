# 新增数据源候选清单（NEW_SOURCES_CANDIDATES）

> 调研日期：2026-10-14 ｜ 调研方式：网络检索 + 开源项目接口档案比对（**未实测任何接口**）
> 基线：`data/sources_registry.json` 现有 83 个源；本清单只收 83 个之外的新接口/新厂商
> 规则：URL 均来自搜索结果明确出现的地址或官方文档；未找到的标注「未找到」；同一厂商的新接口算新源

---

## 一、按类别分组的新源清单

### 1. 港股数据（现有缺口最大的方向）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| hk_tencent_quote | 腾讯港股实时行情 | `http://qt.gtimg.cn/q=hk00700` | none | untested | realtime | 现价/昨收/开/高/低/成交量/成交额/时间戳/市值/PE/币种 | 否（text/plain） | quote | 与 A 股同一接口，仅前缀 `hk`；港股报价延时 15 分钟（腾讯侧标注） | 港美股 |
| hk_tencent_index | 腾讯港股/美股指数快照 | `https://sqt.gtimg.cn/qt?app=tengxuncaijing&q=s_hkHSI,s_usDJI,s_usIXIC` | none | untested | realtime | 指数点位/涨跌/成交量 | 否 | quote/index | 一条请求同时取恒指、国企指数、道指、纳指 | 港美股 |
| hk_tencent_kline_min | 腾讯港股/指数分钟K线 | `https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=hk00700,m5,,10` | none | untested | minute | OHLC + 成交量 + 换手率 | 否 | kline | 与 `tencent_kline` 同族不同 param 前缀；已有 A 股入口，港股为新市场 | 港美股 |
| hk_em_kline | 东财港股日/周/月K线（secid=116） | `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=116.00700&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end=20500101&lmt=0` | none | untested | daily | 开收高低/成交量/成交额/振幅/涨跌幅/换手率 | 否 | kline | 复用 `push2his`，secid 前缀 `116` 即港股；akshare 对应 `stock_hk_hist` | 港美股 |
| hk_em_spot | 东财港股实时行情 | `https://push2.eastmoney.com/api/qt/stock/get?secid=116.00700&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170` | none | untested | realtime | 现价/涨跌幅/市值/成交量 | 否 | quote | 同一 push2 域名新市场前缀 | 港美股 |
| hk_sina_hq | 新浪港股实时行情 | `https://hq.sinajs.cn/list=rt_hk00700` | cookie(referer) | untested | realtime | 港股全字段（HKEX 免费发放，延时 15 分钟） | 否 | quote | 需带 Referer；新浪港股页明确「实时基本行情由港交所指定新浪免费发放」 | 港美股 |
| hk_sina_index | 新浪港股指数实时/历史 | `https://stock.finance.sina.com.cn/hkstock/quotes/HSI.html` + `https://vip.stock.finance.sina.com.cn/mkt/#zs_hk` | cookie(referer) | untested | daily | 恒指/国企/红筹/中华港股通精选100 等指数行情 | 否 | index | akshare `stock_hk_index_spot_sina` / `stock_hk_index_daily_sina` | 港美股 |
| hk_xueqiu_quote | 雪球港股/美股行情（匿名 token） | `https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol=HK00700,SH600519` | cookie(xq_a_token) | untested | realtime | 现价/涨跌幅/成交量/成交额/市值 | 否 | quote | 已有 `xueqiu_anon_token` 源；此处为**具体行情端点**（注册表只有匿名 token 获取） | 港美股 |
| hk_xueqiu_kline | 雪球港股/美股历史K线 | `https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=HK00700&begin=...&period=day&type=before` | cookie | untested | daily | OHLC + 成交量 | 否 | kline | 逆向端点（多个开源库在用） | 港美股 |
| hk_hkex_daily_stat | 港交所沪深港通官方日统计 | `https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_YYYYMMDDc.js` | none | untested | daily | 沪股通/深股通/港股通每日买卖成交统计 + 十大成交活跃股 | 否 | northbound | **权威北向替代源**；a-stock-data 的备用源速查明确引用该地址 | 港美股 |
| hk_hkex_equity_quote | 港交所个股报价页 | `https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/Equities-Quote?sc_lang=zh-HK&sym=700` | none | untested | daily | 收盘价/成交量/市值 | 否 | quote | 官方页面，需解析 HTML | 港美股 |
| hk_alltick | AllTick 港股/A股/美股 Tick 与盘口 | `https://quote.alltick.io/quote-stock-b-api/kline?token={key}&query={"data":{"code":"000055.SZ","kline_type":"8",...}}` | key | untested | realtime/tick | K线/tick/十档盘口/WebSocket 推送，延迟宣称 170ms | 否 | quote/kline/tick/depth | 免费 Key；覆盖 A股+港股+美股，**是免费档里少见的三市场 tick 源** | 港美股 |
| hk_itick | iTick 港股/A股/美股 WebSocket | `wss://api.itick.org/stock`（REST: `https://api.itick.org/stock/info?type=stock&region=HK&code=700`） | key | untested | realtime/tick | L1/L2、逐笔、五/十档、K线、IPO、除权因子 | 否 | quote/kline/tick/depth | 免费试用档；单连接 500 标的 | 港美股 |
| hk_tickdb | TickDB A股全量行情 WebSocket | `wss://api.tickdb.ai/v1/realtime?api_key={key}` | key | untested | realtime | 全市场 ticker 推送（permessage-deflate） | 否 | quote | 免费版不可用全量订阅，需付费档 | 港美股 |
| hk_mairui | 麦蕊智数 港股/沪深行情 | `https://api.mairuiapi.com/hsrl/ssjy/{code}/{licence}` | key(licence) | untested | realtime | 实时交易/五档/技术指标/龙虎榜/资金流 | 否 | quote/depth/lhb/fundflow | 免费版每日 500 次，licence 短信即领 | 国内行情 |

### 2. Level-2 / 逐笔 / 高频（明确缺口）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| l2_sse_official | 上交所 Level-2 行情（LDDS/STEP-FAST） | `https://www.sse.com.cn/services/tradingtech/data`（接口规范 IS120/Level-2 产品说明书） | none(规范公开) | untested | tick | 十档盘口 + 最优档前50笔委托 + 逐笔合并 + 撤单 | 否 | tick/depth | **协议为组播/专线，非 HTTP**；需评估接入成本，作为免费 L2 的唯一官方路径 | 国内行情 |
| l2_cmes_pkg | CMES 金融数据库 L2 历史包 | 官网 `cmes-data.com`（下载页；SDK `pip install cmesdata`，API: `cmes.MarketDataAPI(token)`） | key | untested | tick | 逐笔成交/逐笔委托/十档五档快照/订单薄/分钟线，A股+港股 | 否 | tick/depth | 按天下载 + token 制；第三方整合源 | 另类/高频 |
| tdx_g4day_pkg | 通达信官网盘后全日市场包 | `https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip` | none | untested | daily | 沪深北全市场日线含成交额（二进制 zip，单包 2-3MB） | 否 | kline | a-stock-data V3.9 实测可用；**2022-05-06 起含北交所** | 国内行情 |
| tdx_binary_spec | 北交所/股转 行情二进制接口规范 | `https://www.bse.cn/uploads/6/file/public/202512/20251231194000_rj9ne6i3b4.pdf` | none | untested | tick | 行情网关二进制协议定义 | 否 | tick | 仅规范文档，TCP 组播接入 | 国内行情 |
| tick_fxyz | 蜂向标 API（逐笔/五档/资金流/炸板） | `http://api.fxyz.site/wolf/deal?code=000001&tradeDate=2026-01-19&token={key}` 等同族 10 个端点 | key | untested | tick/realtime | 逐笔交易/分价/五档/资金流/涨跌停/炸板/股票列表 | 否 | tick/depth/fundflow/zt | 免费 token 需注册；**逐笔+分价+五档一套齐全** | 国内行情 |
| minute_tencent_m5 | 腾讯 m1/m5/m15/m30/m60 分钟K线 | `https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=sh600519,m5,,320` | none | untested | minute | OHLC + 量 + 换手率基点 | 否 | kline | 已有 `tx_minute`（分时），**分钟K线族为不同端点**；第7字段是换手率基点非成交额 | 国内行情 |
| em_pre_minute | 东财盘前分钟数据（含集合竞价） | `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.000001&klt=1&fqt=0&beg=0925&end=0930` | none | untested | minute | 09:15-09:25 集合竞价分钟线 | 否 | kline/auction | akshare `stock_zh_a_hist_pre_min_em`；**竞价数据现无独立源** | 国内行情 |

### 3. 龙虎榜 / 两融 / 大宗（实时与第二来源）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| lhb_ths_datacenter | 同花顺龙虎榜-个股明细 | `http://data.10jqka.com.cn/api/lhcjmxgg/code/{code}/date/{YYYY-MM-DD}/` | none | untested | daily | 营业部买卖金额TOP5/机构专用/净额/上榜原因 | 否 | lhb | 与东财 datacenter **完全独立的风控面**，可作降级备胎 | 资金 |
| lhb_sse_official | 上交所官方龙虎榜（交易公开信息） | `http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_HQ_LHB...`（注册表已有 `sse_market` 同域） | cookie(referer) | untested | daily | 官方上榜证券/营业部席位 | 否 | lhb | 零鉴权权威一手 | 资金 |
| margin_sse_detail | 上交所融资融券明细 | `https://www.sse.com.cn/market/othersdata/margin/detail/`（页面数据接口 `query.sse.com.cn`） | cookie(referer) | untested | daily | 逐标的融资余额/买入额/偿还额/融券余量 | 否 | margin | 官方源，与东财 RPTA 报表独立 | 资金 |
| margin_ths | 同花顺融资融券个股明细 | `http://data.10jqka.com.cn/market/rzrqgg/code/{code}/` | none | untested | daily | 融资余额/买入/偿还/净买入/融券余量/卖出/偿还 | 否 | margin | 同花顺数据中心 HTTP 直取 | 资金 |
| block_ths | 同花顺大宗交易 | `http://data.10jqka.com.cn/market/dzjy/`（数据中心大宗交易页） | none | untested | daily | 成交价/量/买卖营业部/溢价率 | 否 | blocktrade | 未找到公开 JSON 端点，页面可能需解析 → **待验证** | 资金 |
| asharehub_lhb_inst | AShareHub 龙虎榜机构 API | `https://asharehub.com/v2/market/top-inst`（`X-API-Key` 头） | key | untested | daily | 逐席位买入/卖出/净买入/占比/上榜原因 | 否 | lhb | 免费层 100 次/天；字段比东财更细 | 资金 |
| asharehub_market | AShareHub 全量市场参考数据 | `https://asharehub.com/v2/market/*`（daily/fundamentals/margin/top-inst/block-trade/holders 等 47 端点） | key | untested | daily | 日线/财务/两融/大宗/龙虎榜/股东/概念/筹码/北向/南向 | 否 | quote/financial/margin/lhb/blocktrade | 一个 Key 覆盖 47 端点，**作为第三方聚合备份** | 国内行情 |

### 4. 财务 / 估值 / 指数（新增源）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| lixinger_open | 理杏仁 Open API（估值分位全家桶） | `https://open.lixinger.com/api/a/stock/fundamental`（POST, token） | key(token) | untested | daily | PE-TTM/PB/PS/股息率 + 1/3/5/10/20年与上市以来**分位点**、最大最小均值 | 否 | valuation | 免费实名可建 token；**估值分位数据现有源完全没有** | 财务 |
| lixinger_extra | 理杏仁 财务/分红/增减持/股东 | `https://open.lixinger.com/api/a/stock/fs`、`/dividend`、`/major-shares-change`、`/majority-shareholders` | key(token) | untested | quarterly | 三表/分红记录/高管与大股东增减持/十大股东 | 否 | financial/dividend/holder | 50+ 接口，与东财/新浪独立风控面 | 财务 |
| csindex_official | 中证指数官网（成分/权重/行业市盈率） | `https://www.csindex.com.cn/`（`/csindex-home/index-list/query-index-item` 等页面接口） | none | untested | monthly | 指数成分/权重/行业市盈率/估值 | 否 | index/valuation | 官方权威；a-stock-data V3.8 已接中证+国证，但**本项目注册表未收** | 板块/指数 |
| cnindex_official | 国证指数网 | `https://www.cnindex.com.cn/` | none | untested | monthly | 国证系列成分与权重（月末快照） | 否 | index | 同上，项目未覆盖 | 板块/指数 |
| ths_basic_consensus | 同花顺一致预期 EPS | `https://basic.10jqka.com.cn/new/{code}/worth.html` | none | untested | daily | 机构一致预期 EPS/预测 PE | 否 | consensus | a-stock-data 已用，项目未收 | 财务 |
| ths_quantapi | 同花顺数据接口 quantapi（官方商业） | `https://quantapi.10jqka.com.cn/` | key | untested | minute/tick | 2000+ 报表、宏观、分钟/tick、全球行情 | 否 | quote/financial/macro | 付费，**7种语言 SDK**；备选而非首选 | 国内行情 |
| datayes_shop | 通联数据 DataYes 商城 | `https://m.datayes.com/` | key | untested | daily | A股/港股/期货/期权/基金/债券 + 电商/搜索/社交媒体另类数据 | 否 | quote/financial/altdata | 部分免费额度 | 另类 |
| ricequant_rqdata | 米筐 RQData HTTP API | `https://www.ricequant.com/doc/rqdata/http/index-rqdatah` | key(账号) | untested | tick/minute | 股票/期货/期权 tick、分钟、财务、因子 | 否 | quote/financial/factor | HTTP 版不限语言；试用账号前15个月历史 | 国内行情 |
| joinquant_jqdata | 聚宽 JQData | `https://www.joinquant.com/help/api/doc?name=JQDatadoc` | key(账号) | untested | tick/minute | get_price/get_ticks/get_money_flow/get_fundamentals/alpha101/191 | 否 | quote/tick/financial/factor | 试用账号每日 100 万条；**2026-08 起仅限大陆 IP** | 国内行情 |
| tushare_hk_daily | Tushare 港股日线 | `http://api.tushare.pro`（接口 `hk_daily`） | key(token) | untested | daily | 港股 OHLC + 涨跌幅（18点更新） | 否 | kline | 需单独开权限/积分 | 港美股 |
| tushare_stk_weekly | Tushare 周/月线行情 | `http://api.tushare.pro`（接口 `stk_weekly_monthly`） | key | untested | weekly | 周/月 OHLC | 否 | kline | 2000 积分解锁 | 国内行情 |
| tushare_index_dailybasic | Tushare 大盘指数每日指标 | `http://api.tushare.pro`（接口 `index_dailybasic`） | key | untested | daily | PE/PB/换手率/流通市值（2004起） | 否 | valuation/index | 2000 积分解锁 | 财务 |
| tushare_trade_cal | Tushare 交易日历（含港交所） | `http://api.tushare.pro`（接口 `trade_cal`） | key | untested | daily | 各交易所开休市标志 | 否 | calendar | 含 XHKG | 宏观 |

### 5. 舆情 / 另类数据（明确缺口）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| alt_cls_telegraph_v1 | 财联社电报 v1 官方接口 | `https://www.cls.cn/v1/roll/get_roll_list` | sign(本地算) | untested | realtime | 全市场电报快讯 | 否 | news | sign = `md5(sha1(按key字典序拼接query))`，**零 key 可算**；注册表 `cls_cn` 为旧 nodeapi（blocked） | 另类 |
| alt_cls_nodeapi | 财联社 nodeapi 电报列表 | `https://www.cls.cn/nodeapi/telegraphList` | sign | untested | realtime | 电报列表 | 否 | news | 第三方 skill 实测「可用，主抓取源」 | 另类 |
| alt_cls_article | 财联社文章详情 | `https://api3.cls.cn/share/article/{id}` | none | untested | realtime | 单篇正文 | 否 | news | 详情补全 | 另类 |
| alt_wallstreetcn | 华尔街见闻 7×24 快讯 | `https://api-one.wallstcn.com/apiv1/content/lives?channel={ch}&client=pc&limit={n}` | none | untested | realtime | 快讯正文/score 分级/关联文章/财经日历 | 否 | news/macro-calendar | 逆向端点，多频道 + cursor 翻页；**比东财 np-* 更细** | 另类 |
| alt_wallstreetcn_item | 华尔街见闻单条详情 | `https://api-one.wallstcn.com/apiv1/content/lives/{id}` | none | untested | realtime | 单条完整内容/图片/数据解读链接 | 否 | news | 配合上者 | 另类 |
| alt_cctv_news | 央视网新闻联播文字稿 | `https://tv.cctv.com/lm/xwlb/`（当日节目单 + 文稿页） | none | untested | daily | 条目标题 + 全文文字稿（晚约20:00更新） | 否 | news/policy | **政策信号独家源**，a-stock-data V3.9 已接 | 另类 |
| alt_sse_interaction | 上证e互动 问答 | `https://sns.sseinfo.com/qa.do` / `company.do?stockcode={code}` | none | untested | realtime | 投资者提问 + 上市公司回复 | 否 | irm/sentiment | 沪市互动问答，**巨潮互动易只覆盖深市** | 另类 |
| alt_irm_cninfo | 深交所互动易 | `https://irm.cninfo.com.cn/ircs/company/companyDetail?orgId={orgId}&stockcode={code}` | none | untested | realtime | 深市公司问答/云访谈/调研活动 | 否 | irm/sentiment | 注册表仅有 `cninfo_announce`（公告，blocked），问答为新能力 | 另类 |
| alt_xueqiu_hot | 雪球热门股/讨论 | `https://xueqiu.com/v5/statuses/hot/stock_list.json` | cookie | untested | realtime | 热股榜/讨论热度 | 否 | sentiment | 注册表 `xueqiu_hot` 为 blocked；此处记录具体端点备用 | 另类 |
| alt_stockapi_change | StockApi 股票异动历史 | `https://www.stockapi.com.cn/v1/change/codeHistory?code=300688&startDate=...&endDate=...` | none(每日1000次免费) | untested | daily | 异动时间/类型（火箭发射/快速反弹等）/幅度 | 否 | anomaly | **异动类型学数据现有源没有** | 另类 |
| alt_gugudata | 咕咕数据 个股资金流/行情 | `https://api.gugudata.com/stock/cn/...` | key(appkey) | untested | daily | 资金流30日/行情/指标 | 否 | fundflow | 付费 appkey | 资金 |
| alt_baidu_index | 百度指数（个股搜索热度） | `https://index.baidu.com/api/SearchApi/index` | cookie | untested | daily | 搜索指数/资讯指数/媒体指数 | 否 | sentiment | 官方无开放 API（FAQ 明示），需 Cookie + 解密；**金融类关键词被屏蔽**，慎入 | 另类 |
| alt_stockemotions | StockEmotions 数据集（AAAI 2023） | `https://github.com/adlnlp/StockEmotions` | none | untested | static | 1万条金融情绪标注（多空 + 12类情绪） | 否 | sentiment/dataset | 学术数据集，非实时；可做情绪模型训练标注参考 | 另类 |

### 6. 宏观 / 利率 / 指数（补充）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| macro_pbc_social_financing | 人民银行社融 | `http://www.pbc.gov.cn/`（三级跳取 xls 附件） | none | untested | monthly | 社融增量12列（人民币贷款/委托/信托/未贴现汇票/企业债/政府债/股票融资/ABS/核销） | 否 | macro | a-stock-data 已接，项目未收 | 宏观 |
| macro_nbs_pmi | 国家统计局 PMI | `https://www.stats.gov.cn/` | none | untested | monthly | 制造业/非制造业/综合 + 大中小型企业分档 | 否 | macro | 同上 | 宏观 |
| macro_chinabond_curve | 中债收益率曲线 | `https://www.chinabond.com.cn/` | none | untested | daily | 国债/商业银行AAA/中短票AAA，3月~30年关键期限 | 否 | rates | 项目未收 | 宏观 |
| macro_chinamoney_repo | 中国货币网回购定盘利率 | `https://www.chinamoney.com.cn/` | none | untested | daily | FR001/007/014 + FDR 同期限 | 否 | rates | FR 约近3年、FDR 约近1年 | 宏观 |
| macro_em_lcpd_report | 东财 LPR 历史 | `https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_IMP_INTRESTRATEN` | none | untested | daily | 1年/5年 LPR 全历史 | 否 | rates | 与注册表 `em_lcpd_report` 同域，reportName 不同 → **同厂商新接口** | 宏观 |
| macro_wscn_calendar | 华尔街见闻全球宏观日历 | `https://api-one.wallstcn.com/apiv1/content/calendar?...`（a-stock-data `macro_calendar`） | none | untested | daily | 公布值/预期/前值/修正值/重要性 | 否 | macro-calendar | 项目未收 | 宏观 |

### 7. 期货 / 期权 / 大宗商品（补齐）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| fut_sina_realtime | 新浪实时期货（含大商所） | `https://hq.sinajs.cn/list=RB0,M0,IF0` | cookie(referer) | untested | realtime | 六家交易所品种实时价/买一卖一（大商所品种走这里） | 否 | futures | 涨跌停价仅中金所有 | 宏观/期货 |
| fut_sina_kline | 新浪期货日K（主力连续+具体合约） | `https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_RB0=/InnerFuturesNewService.getDailyKLine?symbol=RB0` | cookie | untested | daily | 逐日 K 线，最早到 2005 年 | 否 | futures/kline | **补上大商所历史日K**（官网有 JS 反爬） | 宏观/期货 |
| fut_sse_daily | 上期所日行情/期权/持仓排名 | `https://www.shfe.com.cn/data/dailydata/kx/kx{date}.dat` | none | untested | daily | 开高低收/结算价/成交量/持仓量及变化/成交额；期权 Delta/IV | 否 | futures/options | 官方源；a-stock-data 实测最早 2002-01-07 | 宏观/期货 |
| fut_czce_daily | 郑商所日行情/持仓排名 | `http://www.czce.com.cn/cn/DFSStaticFiles/Future/{date}/FutureDataDaily.htm` | none | untested | daily | 期货日行情 + 前20会员持仓 | 否 | futures | 官方 | 宏观/期货 |
| fut_cffex_daily | 中金所日行情/持仓排名 | `http://www.cffex.com.cn/sj/hqsj/rtj/{date}/index.xml` | none | untested | daily | 股指/国债期货 + 股指期权（不公布 Delta/IV） | 否 | futures/options | 官方 | 宏观/期货 |
| fut_gfex_daily | 广期所日行情 | `http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml` | none | untested | daily | 工业硅/碳酸锂日期货期权 | 否 | futures/options | 官方；会员持仓排名未接 | 宏观/期货 |
| fut_sge_spot | 上金所现货日线 | `https://www.sge.com.cn/` | none | untested | daily | Au99.99/Au(T+D)/Ag(T+D)/Pt99.95 | 否 | spot | 官方 | 宏观/期货 |
| opt_sina_etf | 新浪 ETF 期权 T型报价/Greeks | `https://hq.sinajs.cn/list=CON_OP_10006801` 系列（合约清单 `option_sse_list_sina`） | cookie | untested | realtime | 50ETF/300ETF/科创50/500ETF 认购认沽 T型报价 + Delta/Gamma/Theta/Vega/IV | 否 | options/greeks | **交易所预算的 Greeks，免本地 BSM**；项目未收 | 国内行情 |

### 8. 已存在厂商的新接口（去重后仍算新）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| em_emappdata_hot | 东财股吧人气榜/概念命中 | `https://emappdata.eastmoney.com/stockrank/...`（POST JSON，appId+globalId） | none(appId任意UUID) | untested | realtime | 个股人气排名/排名变化/概念命中热度 | 否 | sentiment | 与注册表 em 域名相同但为独立端点族 | 另类 |
| em_gbapi_posts | 东财股吧帖子 | `https://gbapi.eastmoney.com/...` | none | untested | realtime | 股吧帖子/评论 | 否 | sentiment | 免登录 | 另类 |
| em_datacenter_f10 | 东财 F10 证券数据接口 | `https://datacenter.eastmoney.com/securities/api/data/v1/get?source=HSF10&client=PC&reportName=...` | none | untested | daily | F10 各类报表 | 否 | financial | 与 datacenter-web 同厂商不同端点 | 财务 |
| em_searchapi_quoteid | 东财搜索 API（代码→secid 权威解析） | `https://searchapi.eastmoney.com/...` | none | untested | realtime | 返回 QuoteID 即 secid | 否 | meta | 代码→市场解析的权威来源（解决 000001 歧义） | 国内行情 |
| em_reportapi_industry | 东财行业研报（qType=1） | `https://reportapi.eastmoney.com/report/list?qType=1&industry_code=*` | none | untested | daily | 行业研报列表/行业码/评级 | 否 | research | 与个股研报同端点、不同参数 → **同厂商新接口** | 财务 |
| em_pdf_report | 东财研报 PDF 下载 | `https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf` | referer | untested | daily | 完整研报 PDF | 否 | research | 需处理 Referer 鉴权 | 财务 |
| em_np_notice | 东财公告接口 | `https://np-anotice-stock.eastmoney.com/...` | none | untested | daily | 个股公告 | 否 | announcement | 注册表已有 `em_ann_np`（degraded），此为另一域名端点 | 财务 |
| em_quote_zixuan | 东财自选页资讯 | `https://quote.eastmoney.com/zixuan/api/...` | none | untested | realtime | 自选股资讯流 | 否 | news | 免登录 | 另类 |
| em_myfavor | 东财自选股（需登录） | `https://myfavor.eastmoney.com/...` | cookie | untested | realtime | 自选股列表 | 否 | meta | 需 Cookie，appkey 已公开 | 另类 |
| em_push2_ulist | 东财批量行情 ulist.np | `https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.000001,0.300339&fields=f2,f3,f4,f12,f14` | none | untested | realtime | 多标的批量快照 | 否 | quote | 注册表有 clist/get，**ulist.np 为不同端点** | 国内行情 |
| em_push2_trends2 | 东财分时 trends2 | `https://push2.eastmoney.com/api/qt/stock/trends2/get?secid=..&ndays=5&iscr=0` | none | untested | minute | 多日分时序列 | 否 | kline | 注册表无 trends2 | 国内行情 |
| em_slist_blocks | 东财个股所属全部板块（slist spt=3） | `https://push2.eastmoney.com/api/qt/slist/get?spt=3&secid=..` | none | untested | daily | 行业/概念/地域 + BK码 + 涨跌幅 + 龙头股 | 否 | sector | **一次请求拿全板块归属**；注册表无 | 板块 |
| ths_strong_reason | 同花顺涨停揭秘/强势股题材归因 | `http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/order/desc/` | none | untested | daily | 强势股 + reason 人工标注题材 | 否 | signal | ⚠️ **不计入新源**：与注册表 `ths_strong_stocks` 为同一端点，此行仅作字段说明保留 | 板块 |
| ths_datacenter_lhb_list | 同花顺龙虎榜列表 | `http://data.10jqka.com.cn/api/lhb/` | none | untested | daily | 每日上榜股票汇总 | 否 | lhb | 与 `lhb_ths_datacenter` 同站不同端点 | 资金 |
| tencent_ff_quote | 腾讯复权行情快照 | `https://qt.gtimg.cn/q=ff_sh600519` | none | untested | realtime | 复权后现价 | 否 | quote | 注册表只有 qt 基础行情 | 国内行情 |
| tencent_proxy_ifzq | 腾讯 proxy.finance.qq.com 镜像 | `https://proxy.finance.qq.com/ifzqgtimg/...` | none | untested | realtime | ifzq 全部能力的代理镜像 | 否 | quote/kline | **主域名被墙时的备用入口** | 国内行情 |
| tencent_ticks | 腾讯当日逐笔成交 | `https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600519&p=0` | none | untested | tick | 时间/价格/量/额/买卖方向 | 否 | tick | 注册表 `tx_tick_detail` 已含同端点 → **去重，不单列** | 国内行情 |
| sina_vip_report | 新浪研报列表 | `https://vip.stock.finance.sina.com.cn/q/go.php/vReport_List/kind/{kind}/index.phtml` | none | untested | daily | 标题/类型/机构/研究员/日期 | 否 | research | 东财研报的第二来源 | 财务 |
| sina_realtime_stock_js | 新浪实时行情 JS（复权因子） | `https://finance.sina.com.cn/realstock/company/{symbol}/{kind}.js` | none | untested | daily | qfq/hfq 复权因子序列 | 否 | adjust | 注册表 `sina_vip_kline` 之外的复权端点 | 国内行情 |
| sina_lhb_ggtj | 新浪龙虎榜个股上榜统计 | `http://vip.stock.finance.sina.com.cn/q/go.php/vLHBData/kind/ggtj/index.phtml` | none | untested | daily | 上榜次数/累积购买额/净额/席位位数 | 否 | lhb | akshare `stock_sina_lhb_ggtj`；注册表未收 | 资金 |
| jisilu_sw_industry | 集思录申万行业信息 | `https://legulegu.com/stockdata/sw-industry-overview` | none | untested | daily | 申万一二三级行业 PE/PB/股息率/成份数 | 否 | sector/valuation | 注册表有 11 个 jisilu 但无申万行业 | 板块 |
| tv_screener_extra | TradingView 多市场 screener | `https://scanner.tradingview.com/{market}/scan`（market: hongkong/usa/china） | none | untested | realtime | 各市场全字段筛选 | 否 | screener | 注册表 `tv_screener` 未限定市场，**港股/美股为新市场** | 港美股 |

### 9. 本地/开源数据集（非 HTTP，但可作数据管道）

| id | name | url | auth | status | freq | fields | cors | provides | notes | category |
|---|---|---|---|---|---|---|---|---|---|---|
| qlib_cn_1d | Qlib 官方 A股日线数据集 | `https://qlibpublic.blob.core.windows.net/data/default/stock_data/v2/qlib_data_cn_1d_latest.zip` | none | untested | daily | 全A OHLCV + 因子字段（bin 格式） | 否 | dataset | `python scripts/get_data.py qlib_data --region cn` | 国内行情 |
| qlib_cn_1min | Qlib 官方 A股 1分钟数据集 | `https://qlibpublic.blob.core.windows.net/data/default/stock_data/v2/qlib_data_cn_1min_latest.zip` | none | untested | minute | 全A 1分钟 K线 | 否 | dataset/minute | 微软官方免费分钟数据 | 国内行情 |
| qlib_us_1d | Qlib 官方美股日线 | `https://qlibpublic.blob.core.windows.net/data/default/stock_data/v2/qlib_data_us_1d_latest.zip` | none | untested | daily | 美股 OHLCV | 否 | dataset | Yahoo 采集，官方提示质量未必完美 | 港美股 |
| qlib_csv_cn | Qlib csv 格式 A股数据 | `https://qlibpublic.blob.core.windows.net/data/default/stock_data/csv_data_cn.zip` | none | untested | daily | csv 原始日线 | 否 | dataset | 免 bin 转换 | 国内行情 |
| qlib_crowd_bin | 众包 Qlib bin 数据 | `https://github.com/chenditc/investment_data/releases` | none | untested | daily | 社区维护 A股 bin | 否 | dataset | `wget .../qlib_bin.tar.gz` | 国内行情 |
| free_stockdb | free-stockdb 本地日/分钟数据库 | `https://github.com/iwerevip/free-stockdb_1` | none | untested | minute | 7400+ 股票 2000年至今日/周/月 + 1/5/15/30分钟前复权 | 否 | dataset | 本地引擎，含 MCP/HTTP/Excel 五种调用 | 国内行情 |
| adata_lib | adata 开源库（多源融合） | `https://github.com/1nchaos/adata` | none | untested | daily | 行情/概念/龙虎榜/资金流/财务/人气榜/债券 | 否 | library | Apache-2.0；东财+百度+同花顺+通达信混合 | 国内行情 |
| efinance_lib | efinance 开源库 | `https://github.com/Micro-sheep/efinance`（GitHub topic: finance/stock/quant） | none | untested | daily | 基金/股票/债券/期货 | 否 | library | 搜索结果仅确认库存在与定位，**具体接口未核实** | 国内行情 |
| stock_sdk_js | stock-sdk 前端 JS SDK | `https://github.com/chengzuopeng/stock-sdk` | none | untested | daily/minute | A股/港股/美股 K线、盘口、资金流、龙虎榜、两融、大宗、指标信号 | 否 | library | 零依赖 JS；**与项目前端技术栈契合** | 港美股 |

---

## 二、汇总表

| 维度 | 数量 |
|---|---|
| 新发现候选源（去重后，不含 1 行自标重复项） | **99** |
| 其中：实时行情类（realtime/tick/minute） | 43 |
| 其中：财务/估值类 | 9 |
| 其中：港美股类 | 18 |
| 其中：另类/舆情类 | 18 |
| 其中：宏观/期货/期权类 | 14 |
| 其中：本地数据集/开源库 | 7 |

按 category 归类（主分类，可重叠）：

| category | 数量 |
|---|---|
| 国内行情 | 27 |
| 港美股 | 18 |
| 另类 | 18 |
| 资金（龙虎榜/两融/大宗/资金流） | 9 |
| 财务 | 9 |
| 宏观/期货/期权 | 14 |
| 板块/指数 | 5 |

---

## 三、最有价值接入的 Top 8（数据独特 + 免费 + 鉴权简单）

| 排名 | 源 | 理由 | 预期工作量 |
|---|---|---|---|
| 1 | `hk_tencent_quote` + `hk_tencent_kline_min` + `hk_em_kline`（secid=116） | **零鉴权、零新依赖**，直接把项目已有腾讯/东财客户端扩展到港股；与 A 股共用限流逻辑 | 极低（半天） |
| 2 | `alt_cls_telegraph_v1` | sign 纯本地可算（md5(sha1(排序query))），**零 key**；注册表 `cls_cn` 是 blocked 旧接口，这是官方新版，实时快讯质量高于东财 np-listapi | 低（1天） |
| 3 | `alt_wallstreetcn` | 零鉴权逆向端点，按频道 + cursor 翻页，快讯带重要性分级与财经日历数据解读链接；与东财/财联社形成三方新闻冗余 | 低（1天） |
| 4 | `lixinger_open` | **估值历史分位（1/3/5/10/20年）是现有 83 个源完全没有的维度**；免费实名 token；银行股有专用端点 | 中（2天，需处理 token） |
| 5 | `tdx_g4day_pkg` | 一个 zip 拿到某交易日**沪深北全市场日线含成交额**，收盘后全市场截面筛选/落库效率远高于逐股请求；a-stock-data 已实测 2022-01 至 2026-09 可用 | 中（2天，二进制解析） |
| 6 | `lhb_ths_datacenter` + `margin_ths` + `fut_sina_kline` | 同花顺数据中心 + 新浪期货日K 是**与东财完全独立的风控面**，东财被封时龙虎榜/两融/大商所历史不断供；符合项目已有的「多源降级」架构 | 中（2-3天） |
| 7 | `hk_hkex_daily_stat` | **权威北向资金**：东财全系北向自 2024-08 净买额字段断供（NaN/0），同花顺深股通实时也不可靠；港交所官方日统计是唯一权威替代 | 中（1-2天，JS 解析） |
| 8 | `opt_sina_etf` | 交易所预算的期权 Greeks/IV，免本地 BSM；覆盖 50ETF/300ETF/科创50/500ETF，注册表无任何期权数据 | 中（2天） |

---

## 四、调研过程中的判断与风险提示

1. **港股是最大缺口且最容易补**：腾讯 `qt.gtimg.cn/q=hkXXXXX`、东财 `secid=116.XXXXX` 都是现有客户端的参数扩展，无需新依赖、无需 key。优先做。
2. **免费 Level-2 基本不存在 HTTP 通道**：上交所 Level-2 是 STEP/FAST 组播协议，北交所是二进制 TCP 网关，均需专线/组播接入，工程成本高。第三方（AllTick/iTick/CMES/StockApi）有免费额度但需注册 key。若项目要 L2，建议先评估 AllTick 免费档（三市场 tick + 盘口 + WebSocket）。
3. **聚宽 JQData 自 2026-08-01 起仅支持大陆 IP**，云端（GitHub Actions / GitCode Space）若在海外将不可用，接入前需确认运行环境。
4. **百度指数官方无开放 API 且金融类关键词被屏蔽**，不建议投入。
5. **同花顺官方 Financial-API（hithink-finance / fuyao.aicubes.cn）** 是 2026 年新出现的官方 REST + MCP 服务，覆盖行情/财务/指数/涨跌停/龙虎榜/集合竞价/主力资金/高频动向，需 `X-api-key`。因其文档站明确「已注册公开能力同时通过 MCP 暴露」，可作为 `iwencai_openapi` 之外的同花顺系 Key 型备选，但**免费额度未在检索中确认**，需注册后核实。
6. 报告中所有 URL 均来自搜索结果明确出现的地址或官方文档页；`block_ths`（同花顺大宗交易）未找到公开 JSON 端点，标注待验证。
7. 本清单未做任何接口实测，`status` 全部为 `untested`；频率限制、字段完整性、风控策略需由实测子代理确认。
