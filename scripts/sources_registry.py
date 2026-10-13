#!/usr/bin/env python3
"""
sources_registry.py — 全数据源注册表 (唯一事实来源)

把本项目探测/使用过的所有接口集中登记, 包括:
  - 可用的 (生产在用)
  - 不可用的 (被封/需 key/地区限制) —— 同样重要, 避免重复踩坑
  - 待验证的 (抓到但未接入)

每条记录:
  id          唯一标识
  url         接口地址
  auth        鉴权方式 (none / key / cookie / oauth)
  status      ok / degraded / blocked / unauth / untested
  freq        更新频率 (realtime / minute / daily / weekly)
  delay       数据延迟
  fields      提供的字段
  provides    产出到哪个本地文件
  notes       实测结论/坑
  degrade_to 降级链 (本源失败时改用什么)

设计原则:
  - 采集器只读注册表, 不硬编码 URL
  - 新增源 = 加一条记录 + 写一个 fetch 函数, 不用改管道
  - 状态由 probe 实测决定, 不靠猜

用法:
  python3 sources_registry.py --list           # 列出全部
  python3 sources_registry.py --probe          # 实测所有 none-auth 源
  python3 sources_registry.py --json           # 导出 JSON
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}

# ─────────────────────────────────────────────────────────────────
# 数据源注册表
# ─────────────────────────────────────────────────────────────────
SOURCES = [
    # ============ A 股核心行情 (可用) ============
    {
        "id": "tencent_quote",
        "name": "腾讯实时行情",
        "url": "https://qt.gtimg.cn/q={codes}",
        "auth": "none",
        "status": "ok",
        "freq": "realtime",
        "delay": "秒级 (盘中实时)",
        "fields": "现价/昨收/开盘/最高/最低/涨跌%/换手%/PE/总市值/流通市值/PB/时间",
        "field_map": {
            "p3": "price", "p4": "prev_close", "p5": "open",
            "p32": "chg_pct", "p33": "high", "p34": "low",
            "p38": "turnover", "p39": "pe", "p44": "mktcap_yi",
            "p45": "floatcap_yi", "p46": "pb", "p30": "time",
        },
        "encoding": "gbk",
        "cors": "*",                     # 浏览器可直连 (realtime.js 依赖此点)
        "provides": ["enrich.json:ths_strong", "latest.json:market.indices"],
        "notes": "容器内稳定可用; 每码1请求, 支持批量60个/请求; "
                 "量比无独立字段一律1.0; p44/p45 单位亿元",
        "degrade_to": ["sina_hq"],
    },
    {
        "id": "tencent_kline",
        "name": "腾讯日K (前复权)",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日K, 收盘定格",
        "fields": "date/open/close/high/low/volume (qfq 前复权)",
        "provides": ["lowpool.json", "cycle.json", "index_kline.json", "hot_impact.json"],
        "notes": "频率限制: 连续约1750次请求后返回 501 Not Implemented, "
                 "需错峰分批; 批量补K线建议并发10, 每批间隔0.15s",
        "degrade_to": ["eastmoney_kline"],
    },
    {
        "id": "tencent_index_history",
        "name": "腾讯指数全史",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,{end},{n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日K",
        "fields": "指数日K全史 (上证1990起/深成1993/沪深300 2005/创业板2010/科创2019)",
        "provides": ["cycle_deep.json"],
        "notes": "分页拉取, 每页最多800根, 用 end 往前翻; "
                 "实测上证 8566 根 (1990-12-19~2026-10-12)",
    },
    {
        "id": "sina_hq",
        "name": "新浪实时行情",
        "url": "https://hq.sinajs.cn/list={codes}",
        "auth": "none",
        "status": "ok",
        "freq": "realtime",
        "delay": "秒级",
        "encoding": "gbk",
        "fields": "指数/个股实时 (需 Referer: finance.sina.com.cn)",
        "provides": ["(降级备用)"],
        "notes": "必须带 Referer 头, 否则 403; 主要作为腾讯的降级源",
    },
    {
        "id": "sina_news",
        "name": "新浪 7x24 快讯",
        "url": "https://zhibo.sina.com.cn/api/zhibo/feed?page=1",
        "auth": "none",
        "status": "degraded",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "title/time/content",
        "provides": ["latest.json:news_recent", "hot_impact.json"],
        "notes": "实测返回体经常只有 113 bytes (zhibo_id missing), "
                 "内容量不稳定; 已由 fetch_remote_news 从 data-news 仓兜底",
    },
    {
        "id": "sina_kline_daily",
        "name": "新浪个股日K (akshare stock_zh_a_daily_sina)",
        "url": "via akshare.stock_zh_a_daily(symbol='sh600519', adjust='qfq')",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "date/open/high/low/close/volume/amount/outstanding_share/turnover",
        "provides": [],
        "notes": "2026-10-13 实测 茅台日K全史 6019 行, 0.79s; "
                 "tencent_kline 501 限流后的 K线补录源",
    },
    {
        "id": "sina_etf_hist",
        "name": "新浪 ETF 日K (akshare fund_etf_hist_sina)",
        "url": "via akshare.fund_etf_hist_sina(symbol='sh510500')",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "date/open/high/low/close/volume/amount/postVol/postAmt",
        "provides": [],
        "notes": "2026-10-13 实测 510500 全史 3294 行, 0.27s; ETF 净值域接入",
    },

    # ============ 东财 (部分可用) ============
    {
        "id": "eastmoney_zt_pool",
        "name": "东财涨跌停池",
        "url": "https://push2ex.eastmoney.com/getTopic{kind}Pool",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "分钟级 (收盘定格)",
        "fields": "涨停/跌停/炸板明细 (code/name/price/涨跌幅/连板/封板时间/行业)",
        "provides": ["latest.json:market.limit_up/down/broken", "latest.json:limit_up_pool"],
        "notes": "关键: data.pool 可能为空但 data.tc 是权威计数 "
                 "(2026-10-12 DT: pool=[] 而 tc=19) → 家数必须读 tc",
    },
    {
        "id": "eastmoney_zdfenbu",
        "name": "东财涨跌分布",
        "url": "https://push2ex.eastmoney.com/getTopicZDFenBu",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "各涨幅档位家数 ({'-11':19},{'11':46},...)",
        "provides": ["latest.json:market.advance/decline/flat/up_pct"],
        "notes": "fenbu 的 11/-11 档 ≈ 涨停/跌停口径",
    },
    {
        "id": "eastmoney_datacenter",
        "name": "东财 datacenter (龙虎榜/大宗交易/两融)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={report}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后 (当日数据约 18:00 后出)",
        "reports": {
            "RPT_DAILYBILLBOARD_DETAILS": "龙虎榜明细 (席位解读+成功率)",
            "RPT_DATA_BLOCKTRADE": "大宗交易 (溢价率/成交额)",
            "RPTA_RZRQ_LSHJ": "融资融券余额历史",
        },
        "provides": ["enrich.json:lhb/blocktrade", "latest.json:margin"],
        "notes": "filter 日期必须 YYYY-MM-DD (横杠), 传 YYYYMMDD 会返回0条; "
                 "当日数据收盘后才出 → 必须做日期回退 (最多5天); "
                 "龙虎榜同一股票多行 (不同上榜原因), 需按股票合并",
        "degrade_to": [],
    },
    {
        "id": "eastmoney_clist",
        "name": "东财板块列表/资金流 (push2 clist)",
        "url": "https://push2.eastmoney.com/api/qt/clist/get",
        "auth": "none",
        "status": "blocked",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "行业/概念板块涨跌/资金流/成分股",
        "provides": [],
        "notes": "容器内间歇 reset (RemoteDisconnected), 约 20 次请求后必然被封; "
                 "曾导致 sector_pool 只剩2个板块、multi_v5 因缺行业映射出0候选。"
                 "已用 tencent_mktHs + tv_screener 替代",
        "degrade_to": ["tencent_mktHs", "tv_screener"],
    },
    {
        "id": "eastmoney_stock_get",
        "name": "东财个股信息 (含 f127 行业)",
        "url": "https://push2.eastmoney.com/api/qt/stock/get",
        "auth": "none",
        "status": "blocked",
        "freq": "realtime",
        "fields": "f127=行业名, f57=代码, f58=名称",
        "provides": [],
        "notes": "首次探测成功返回过 industry, 之后同 push2 域一起被 WAF 封; "
                 "行业映射已改用 tv_screener (5309只全覆盖)",
        "degrade_to": ["tv_screener"],
    },
    {
        "id": "eastmoney_kline",
        "name": "东财历史K线",
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "provides": [],
        "notes": "2026-10-13 实测: push2his 域不在 push2 的 WAF 封锁范围; "
                 "茅台全史 6019 根, 381B 样本; fqt=1 前复权; "
                 "需带 Referer: quote.eastmoney.com; 并发 10 安全",
    },
    {
        "id": "em_a_hist",
        "name": "东财A股历史K线 (akshare stock_zh_a_hist)",
        "url": "via akshare.stock_zh_a_hist(symbol='600519', period='daily', adjust='qfq')",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "日期/股票代码/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率",
        "provides": [],
        "notes": "akshare 封装, 底层 push2his; 2026-10-13 实测 489 行/0.25s",
    },
    {
        "id": "eastmoney_fund_rank",
        "name": "东财混合基金排行",
        "url": "https://fund.eastmoney.com/top/default.aspx",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "混合基金近1年收益排行 (KOL抱团方向代理)",
        "provides": ["latest.json:fund_rank"],
        "notes": "由 eastmoney_fund_rank.py 实现, 49 条",
    },
    {
        "id": "em_fund_etf_hist_min",
        "name": "东财 ETF 历史净值 (akshare fund_etf_fund_info_em)",
        "url": "via akshare.fund_etf_fund_info_em(fund='511280', start_date='20000101')",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "净值日期/单位净值/累计净值/日增长率/申购状态/赎回状态",
        "provides": [],
        "notes": "2026-10-13 实测 784 行全史; 坑: 参数名是 fund 不是 symbol",
    },
    {
        "id": "em_ann_np",
        "name": "东财公告 API (np-ann)",
        "url": "https://np-ann.eastmoney.com/api/security/ann?sr=-1&page_size=5&ann_type=A&client_source=web&f_node=0&s_code={code}",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "fields": "公告标题/日期/类型/URL",
        "provides": [],
        "notes": "2026-10-13 实测: 无 XHR 头返回 HTML SPA 壳; "
                 "需 X-Requested-With: XMLHttpRequest 头 (未完整验证); "
                 "公告域首选巨潮/交易所, 此为备选",
    },
    {
        "id": "eastmoney_news",
        "name": "东财 7x24 / 收评",
        "url": "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web7_24&fastColumn=100&column=100&req_trace={uuid}",
        "auth": "none",
        "status": "degraded",
        "freq": "minute",
        "provides": [],
        "notes": "实测 HTTP 567 Unknown Status; 财经/政策/产经三栏目全 567; "
                 "2026-10-13 补充: req_trace 为随机 32位hex UUID, 带上后正常返回 JSON; "
                 "567 多为容器本地出口, GitHub Actions 出口通常全通",
        "degrade_to": ["sina_news", "remote_news"],
    },

    # ============ 同花顺 (可用) ============
    {
        "id": "ths_strong_stocks",
        "name": "同花顺强势股 + 题材归因",
        "url": "http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "分钟级 (盘中实时)",
        "encoding": "gbk",
        "fields": "code/name/reason(题材归因)",
        "provides": ["enrich.json:ths_strong", "hot_impact.json", "latest.json:commentary.hotspots"],
        "notes": "只返回 code/name/reason 三个字段, 价格必须从腾讯补; "
                 "实测 48 只/次, 容器内稳定",
    },
    {
        "id": "ths_industry",
        "name": "同花顺行业板块",
        "url": "http://q.10jqka.com.cn/interface/stock/thshy/zdf/desc/1/quote/1.js",
        "auth": "none",
        "status": "degraded",
        "freq": "minute",
        "provides": [],
        "notes": "实测返回 HTML (SPA fallback) 而非 JSON; 需登录态",
    },
    {
        "id": "ths_industry_list",
        "name": "同花顺行业板块列表 (akshare stock_board_industry_name_ths)",
        "url": "via akshare.stock_board_industry_name_ths()",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "name/code (90 个行业板块)",
        "provides": [],
        "notes": "底层 d.10jqka.com.cn 板块 API, 同花顺域名零 WAF; "
                 "2026-10-13 实测 90 行/0.22s",
    },
    {
        "id": "ths_concept_list",
        "name": "同花顺概念板块列表 (akshare stock_board_concept_name_ths)",
        "url": "via akshare.stock_board_concept_name_ths()",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "name/code (375 个概念板块)",
        "provides": [],
        "notes": "2026-10-13 实测 375 概念/8.3s (39 页分页); "
                 "概念代码可作事件驱动标签; 建议 8 req/min",
    },
    {
        "id": "ths_financial_abstract",
        "name": "同花顺财务摘要 (akshare stock_financial_abstract_ths)",
        "url": "via akshare.stock_financial_abstract_ths(symbol='600519')",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "fields": "报告期/净利润/扣非净利润/营业总收入/基本每股收益/销售毛利率/净资产收益率/资产负债率",
        "provides": [],
        "notes": "2026-10-13 实测 茅台 1998~2026 共 103 报告期; "
                 "坑: 字段值含'亿'字需 parse; False 表示无数据",
    },
    {
        "id": "ths_financial_abstract_v2",
        "name": "同花顺财务摘要 v2 (akshare stock_financial_abstract_new_ths)",
        "url": "via akshare.stock_financial_abstract_new_ths(symbol='600519')",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "fields": "report_date/report_name/report_period/metric_name/value/yoy/mom (宽表→长表)",
        "provides": ["financial_summary 表"],
        "notes": "2026-10-13 实测 1200 行/0.26s; 长表格式直接入库无需 parse; "
                 "2014 年前 yoy/mom 多为空; 推荐作为财务域入库源",
    },

    # ============ TradingView (可用, 15分钟延迟) ============
    {
        "id": "tv_screener",
        "name": "TradingView 选股器",
        "url": "https://scanner.tradingview.com/{market}/scan",
        "auth": "none",
        "method": "POST",
        "status": "ok",
        "freq": "minute",
        "delay": "约15分钟 (delayed_streaming_900)",
        "fields": "name/close/change/volume/market_cap/PB/sector/industry/type/"
                  "Recommend.All(技术评级)",
        "provides": ["industry_map_tv.json", "latest.json:westock.sector_ranking"],
        "notes": "零鉴权, market=china → 7476只A股全量; "
                 "支持服务端条件筛选 (涨幅/市值/PB/RSI); "
                 "对A股不提供 PE/RSI; name 返回代码, description 返回英文名; "
                 "非实时源 (15分钟延迟), 页面已标注",
    },

    # ============ 加密货币 / 海外 (可用但未接入) ============
    {
        "id": "coingecko",
        "name": "CoinGecko 加密货币",
        "url": "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "fields": "BTC/ETH 等实时价格",
        "provides": [],
        "notes": "完全免费无需 key, 实测 BTC=82532; 免费额度 1万 credits/月; "
                 "A股项目暂不需要, 保留作海外情绪参考",
    },
    {
        "id": "douyin_hot_search",
        "name": "抖音热搜榜",
        "url": "https://aweme.snssdk.com/aweme/v1/hot/search/list/",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "实时", 
        "method": "GET",
        "encoding": "utf-8",
        "ua_mobile": True,
        "fields": "trending_list[].word / sentence_id / video_count / hot_value / active_time",
        "provides": ["latest.json:social.douyin_hot", "hot_impact.json (主题参考)"],
        "notes": "公开零鉴权 JSON; 需移动端 UA (iPhone/Android), 桌面 UA 偶尔返回空; "
                 "hot_value 有时为 0 (实测), 排序时用 video_count 兜底; "
                 "sentence_id 是唯一稳定去重键; 本次实测 30+ 条热搜",
    },
    {
        "id": "eastmoney_fund_pingzhong",
        "name": "东财基金重仓股持仓 (pingzhongdata JS)",
        "url": "https://fund.eastmoney.com/pingzhongdata/{code}.js?v={ts}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后更新",
        "encoding": "utf-8",
        "fields": "fS_name / fS_code / fund_Rate / fund_sourceRate / fund_minsg / stockCodes / ishb",
        "provides": ["latest.json:fund_rank (扩展持仓字段)"],
        "notes": "返回 JS 变量 (非 JSON), 用正则 r'var (\\w+) = ([^;]+);' 解析; "
                 "stockCodes 是前10大重仓股代码列表; "
                 ".aspx FundJZ/FundGS 接口本地 WAF 403, 但此 JS 接口完全可用; "
                 "v 参数 = Unix timestamp 秒, 用于缓存控制; "
                 "每次 CI 运行前需先查最新 ts",
    },
    {
        "id": "sec_edgar",
        "name": "SEC EDGAR 财报 XBRL",
        "url": "https://data.sec.gov/submissions/{cik}.json",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "美股公司财报全量 XBRL (3.7MB/公司)",
        "provides": [],
        "notes": "完全免费无需 key; companyfacts 接口返回完整财务数据; "
                 "A股用不上, 但可作中概股基本面参考",
    },
    {
        "id": "alpha_vantage",
        "name": "Alpha Vantage 美股行情",
        "url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={sym}&apikey=demo",
        "auth": "key",
        "status": "degraded",
        "freq": "minute",
        "fields": "美股实时报价",
        "provides": [],
        "notes": "demo key 可用但只能取少数符号, 实测 IBM 返回真实数据; "
                 "免费额度 25次/天; 注册 key 后 75次/分钟。"
                 "坑: 用垃圾 key 也返回 200 真实数据, 无法校验 key 有效性",
    },

    # ============ 需注册 key (未接入) ============
    {
        "id": "finnhub",
        "name": "Finnhub",
        "url": "https://finnhub.io/api/v1/quote?symbol={sym}&token={key}",
        "auth": "key",
        "status": "unauth",
        "freq": "realtime",
        "fields": "实时报价/基本面/新闻情绪",
        "provides": [],
        "notes": "免费额度最慷慨 (60 req/min); 需注册",
    },
    {
        "id": "twelvedata",
        "name": "Twelve Data",
        "url": "https://api.twelvedata.com/quote?symbol={sym}&apikey={key}",
        "auth": "key",
        "status": "unauth",
        "freq": "realtime",
        "fields": "股/汇/币",
        "provides": [],
        "notes": "免费 800次/天, $29/月 55 credits/min 日无限",
    },
    {
        "id": "fmp",
        "name": "Financial Modeling Prep",
        "url": "https://financialmodelingprep.com/api/v3/quote/{sym}?apikey={key}",
        "auth": "key",
        "status": "unauth",
        "freq": "realtime",
        "fields": "基本面/财报",
        "provides": [],
        "notes": "免费 250次/天; $15/月平铺",
    },
    {
        "id": "marketstack",
        "name": "Marketstack",
        "url": "http://api.marketstack.com/v1/eod?symbols={sym}&access_key={key}",
        "auth": "key",
        "status": "unauth",
        "freq": "daily",
        "fields": "全球股价日线",
        "provides": [],
        "notes": "$9.99/月",
    },
    {
        "id": "tushare",
        "name": "Tushare",
        "url": "http://api.tushare.pro",
        "auth": "key",
        "status": "unauth",
        "freq": "daily",
        "fields": "A股全品类 (行情/财务/因子)",
        "provides": [],
        "notes": "国内最主流的 A股数据源, 需注册; 积分制, 免费额度有限",
    },
    {
        "id": "akshare",
        "name": "AKShare",
        "url": "python 库 (非HTTP)",
        "auth": "none",
        "status": "ok",
        "freq": "varies",
        "fields": "聚合东财/新浪/同花顺等 100+ 接口",
        "provides": [],
        "notes": "akshare 1.19.1 容器已安装验证 (2026-10-13): "
                 "新浪日K/ETF K线、东财 push2his 历史K、同花顺财务摘要、"
                 "统计局宏观数据均跑通; 它是封装层不是独立源, 底层仍是上面这些",
    },

    # ============ 不可用 (地区/反爬) ============
    {
        "id": "yahoo_finance",
        "name": "Yahoo Finance",
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        "auth": "none",
        "status": "degraded",
        "freq": "realtime",
        "provides": [],
        "notes": "实测 HTTP 429 限流 (chart 与 search 都429)",
    },
    {
        "id": "stooq",
        "name": "Stooq",
        "url": "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
        "auth": "none",
        "status": "blocked",
        "freq": "daily",
        "provides": [],
        "notes": "实测 404, 路径已变更",
    },
    {
        "id": "kaipanla",
        "name": "开盘啦 kaipanla.com",
        "url": "https://app.kaipanla.com/open_api.php",
        "auth": "none",
        "status": "blocked",
        "freq": "unknown",
        "provides": [],
        "notes": "app./xg. 子域 DNS 不可解析; www 的 open_api.php 被 Nuxt SPA fallback 劫持返回 HTML",
    },
    {
        "id": "cls_cn",
        "name": "财联社 cls.cn",
        "url": "https://www.cls.cn/nodeapi/telegraphList",
        "auth": "none",
        "status": "blocked",
        "freq": "unknown",
        "provides": [],
        "notes": "实测 404",
    },
    {
        "id": "gitcode_api",
        "name": "gitcode / AtomGit API",
        "url": "https://web-api.gitcode.com/api/v2/repos/{owner}/{repo}",
        "auth": "none",
        "status": "blocked",
        "freq": "unknown",
        "provides": [],
        "notes": "HTTP 418 反爬 (snomyc/stock 与 gh_mirrors 命名空间); "
                 "网页 SPA 渲染, 需浏览器+登录才能读源码",
    },
    {
        "id": "mootdx",
        "name": "mootdx (TCP 7709)",
        "url": "tcp://119.147.164.60:7709",
        "auth": "none",
        "status": "blocked",
        "freq": "unknown",
        "provides": [],
        "notes": "TCP 连接超时, 需国内IP",
    },
    {
        "id": "xiaohongshu_hot",
        "name": "小红书热门话题",
        "url": "https://edith.xiaohongshu.com/api/sns/web/v1/search/hot",
        "auth": "none",
        "status": "blocked",
        "freq": "minute",
        "provides": [],
        "notes": "所有可猜测 API 路径均 404; 主站为 React SPA, 内容需浏览器渲染; "
                 "2026-10-13 实测 edith.xiaohongshu.com/api/sns/web/v1/search/hot → 404, "
                 "所有 /feed, /search/* 路径同样 404; 无法零凭据 HTTP 接入",
    },
    {
        "id": "xueqiu_hot",
        "name": "雪球热门股",
        "url": "https://xueqiu.com/v5/statuses/hot/stock_list.json",
        "auth": "cookie",
        "status": "blocked",
        "freq": "minute",
        "fields": "热门股票列表 (需 xq_a_token)",
        "provides": [],
        "notes": "无 cookie 时返回 400 / 302 反爬 JS; 需 xq_a_token cookie 认证; "
                 "容器内无法获取有效 cookie; 考虑替代: 东方财富股吧热帖 / 同花顺热榜",
    },

    # ============ 公告/交易所/宏观 (候选源, 容器内未全通) ============
    {
        "id": "cninfo_announce",
        "name": "巨潮资讯公告 (cninfo.com.cn)",
        "url": "https://www.cninfo.com.cn/new/hisAnnouncement/query?column={col}&secuCode={code}&seDate={range}",
        "auth": "none",
        "status": "blocked",
        "freq": "daily",
        "fields": "公告标题/日期/类型/PDF URL",
        "provides": [],
        "notes": "2026-10-13 容器内 HTTP 500; 推测大陆网络可用, 容器出口被限; "
                 "公告域首选候选, 国内服务器上再验",
    },
    {
        "id": "sse_market",
        "name": "上交所行情数据 API",
        "url": "http://query.sse.com.cn/commonQuery.do?sqlId={sql_id}&pageHelp.pageSize=10&_=timestamp",
        "auth": "none (Referer: sse.com.cn 必带)",
        "status": "untested",
        "freq": "daily",
        "fields": "沪市行情/公告/基金/财务",
        "provides": [],
        "notes": "2026-10-13 实测 200 但 data=null; sqlId 需配合有效会话/Referer; "
                 "待进一步探测正确参数组合",
    },
    {
        "id": "szse_api",
        "name": "深交所官方数据 API",
        "url": "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=JSON&CATALOGID={id}&TABPAGE=1&pageNum=1&pageSize=5",
        "auth": "none",
        "status": "blocked",
        "freq": "daily",
        "fields": "深市行情/公告/监管数据",
        "provides": [],
        "notes": "2026-10-13 容器内 CATALOGID=1835/1803/1805 均 HTTP 500; "
                 "可能需大陆出口 IP; 国内服务器上再验",
    },
    {
        "id": "macro_china_cpi_gdp",
        "name": "宏观数据 CPI/GDP (akshare)",
        "url": "via akshare.macro_china_cpi() / akshare.macro_china_gdp()",
        "auth": "none",
        "status": "ok",
        "freq": "monthly",
        "fields": "CPI: 月份/全国当月/同比/环比; GDP: 季度/绝对值/同比",
        "provides": [],
        "notes": "统计局数据; 2026-10-13 实测 CPI 224 行/GDP 82 行; "
                 "可用于市场环境判定",
    },

    {
        "id": "em_lcpd_report",
        "name": "东财业绩快报/财务报告 (datacenter RPT_LICO_FN_CPD)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD&columns=ALL&pageSize=50&pageNumber={n}&sortColumns=UPDATE_DATE,SECURITY_CODE&sortTypes=-1,-1&filter=(REPORTDATE%3D%27{report_date}%27)",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "fields": "每股收益/营业总收入/净利润/ROE/营收同比/净利润同比/每股净资产/销售毛利率/行业 (5700+家全市场)",
        "provides": [],
        "notes": "同 datacenter-web 域 (LHB 已验证); 2026-10-13 搜索确认 RPT_LICO_FN_CPD 可用; "
                 "filter 日期格式 YYYY-MM-DD; 分页 50 条/页 约 116 页; 建议 sleep 0.5s/页",
    },
    {
        "id": "baostock",
        "name": "Baostock 免费A股数据 (query_history_k_data_plus)",
        "url": "python 库 (baostock, 需先 login())",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "日K/周K/月K (qfq)/估值/财务指标; 全市场 5000+ 只 2000~2026 全通",
        "provides": [],
        "notes": "2026-10-13 实测: baostock 0.9.3 装容器可跑, 茅台 16326 行历史; "
                 "无注册无积分; K线补录与 tencent_kline 501 限流互补",
    },
    {
        "id": "iwencai_openapi",
        "name": "同花顺问财 OpenAPI (自然语言查数据)",
        "url": "https://openapi.iwencai.com/v1/query2data (POST)",
        "auth": "key (IWENCAI_API_KEY)",
        "status": "unauth",
        "freq": "on-demand",
        "fields": "自然语言选股/行情/财务/指数/基金/板块/期货; /v1/comprehensive/search 搜新闻/公告/研报",
        "provides": [],
        "notes": "请求 body {query, source, page, limit, is_cache}; 返回 datas[] + code_count; "
                 "需申请 key; 限频控制, 30s 超时; 中文语义查询是独家能力",
    },
    {
        "id": "cn_a_stock_data",
        "name": "cn-a-stock-data (OpenStockData 开源库)",
        "url": "pip install cn-a-stock-data",
        "auth": "none (问财层需 key)",
        "status": "untested",
        "freq": "varies",
        "fields": "封装腾讯/东财/百度股市通/同花顺/财联社/新浪/巨潮; 7层: 行情/研报/信号/资金筹码/新闻/基础/公告",
        "provides": [],
        "notes": "百度股市通 K线 (baidu_kline_with_ma, 腾讯 fallback) 与 cls.cn 电报封装值得复用; "
                 "未安装验证; 与 akshare 定位重叠但多了百度源",
    },

    # ============ 社交/渲染管线 (cookie 源) ============
    {
        "id": "xueqiu_anon_token",
        "name": "雪球匿名 token (访问主页自动下发)",
        "url": "GET https://xueqiu.com/ (匿名访问自动 set-cookie xq_a_token 与 u)",
        "auth": "cookie (无需登录, 匿名即可)",
        "status": "ok",
        "freq": "minute",
        "fields": "xq_a_token → 解锁 /v5/stock/hot/list.json 等 v5 API",
        "provides": [],
        "notes": "2026-10-13 实测: 容器匿名访问 xueqiu.com 200 并拿到 140 位 token; "
                 "配合 browser-render 管线可抓雪球热榜; token 有效期短, 需定期重取",
    },
    {
        "id": "toutiao_hot",
        "name": "今日头条热榜/财经榜",
        "url": "https://www.toutiao.com/api/"
             "hotboard/pc/hot-news/feed/?ut=...&max_behave_time=0&_signature=...", "auth": "none",
        "status": "ok",
        "freq": "minute",
        "fields": "热榜 title/hot_value/url/img; 财经榜在 category=finance 下",
        "provides": ["hot_impact.json (社交情绪补充)"],
        "notes": "2026-10-13 实测: ut/max_behave_time 可伪造, 热榜 30 条+; "
                 "_signature 有时校验, 财经榜 412 时跳过不阻断",
    },

    # ============ 本地/仓内数据源 ============
    {
        "id": "local_kline_bulk",
        "name": "本地 K线 bulk",
        "url": "file://stocks-data-kline/data/bulk/day_qfq_*.csv",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "fields": "全市场日K (qfq), 两种格式: code|date|o|c|h|l|v 与 code|date,o,c,h,l,v",
        "provides": ["lowpool.json", "optimizer.py 回测"],
        "notes": "当前覆盖 2577/5343 (48.2%); 腾讯频率限制导致无法一次拉全; "
                 "nightly-kline 工作流负责增量补录",
    },
    {
        "id": "local_snapshot",
        "name": "本地全市场快照",
        "url": "file://stocks-data-live/data/market/snapshot_YYYY-MM-DD.json",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "fields": "5278只 name/price/chg_pct/turnover/mktcap/pe",
        "provides": ["lowpool.json (名称匹配)", "tv_screener 中文名补全"],
    },
    {
        "id": "remote_news",
        "name": "远端 news 仓",
        "url": "https://raw.githubusercontent.com/qwerzxcva/stocks-data-news/main/news/{date}.md",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "provides": ["latest.json:news_recent (兜底)"],
        "notes": "新浪快讯不可用时的兜底; 部分日期返回 404 (未生成)",
    },
    {
        "id": "industry_map_tv",
        "name": "TV 行业映射 (本地)",
        "url": "file://stocks-data-live/data/meta/industry_map_tv.json",
        "auth": "none",
        "status": "ok",
        "freq": "weekly",
        "fields": "5309只 代码 → sector/industry/type",
        "provides": ["multi_screener 行业匹配"],
        "notes": "由 tv_screener.py --industry-map 生成; "
                 "解决了东财被封后 multi_v5 缺行业映射出0候选的问题",
    },
]

BY_ID = {s["id"]: s for s in SOURCES}


# ─────────────────────────────────────────────────────────────────
# 查询接口
# ─────────────────────────────────────────────────────────────────
def all_sources():
    return SOURCES


def usable():
    """状态为 ok 或 degraded 的源。"""
    return [s for s in SOURCES if s["status"] in ("ok", "degraded")]


def by_status(status):
    return [s for s in SOURCES if s["status"] == status]


def resolve(sid):
    """按降级链找第一个可用源 (含自身)。"""
    seen = set()
    stack = [sid]
    while stack:
        cur = stack.pop(0)
        if cur in seen or cur not in BY_ID:
            continue
        seen.add(cur)
        s = BY_ID[cur]
        if s["status"] == "ok":
            return s
        stack.extend(s.get("degrade_to") or [])
    return None


# ─────────────────────────────────────────────────────────────────
# 实测 (只测 none-auth 的 GET 源)
# ─────────────────────────────────────────────────────────────────
def probe_one(s, timeout=10):
    url = s["url"]
    # 占位符填样例值
    subs = {"codes": "sh000001", "code": "sh000001", "n": 5, "end": "",
            "kind": "ZT", "date": "20261012", "market": "china",
            "ids": "bitcoin", "cik": "0000320193", "sym": "IBM",
            "report": "RPTA_RZRQ_LSHJ", "owner": "qwerzxcva", "repo": "stocks"}
    for k, v in subs.items():
        url = url.replace("{" + k + "}", str(v))
    if s.get("method") == "POST":
        url = url.replace("{market}", "china")
    hdr = dict(UA)
    if "sina" in s["id"]:
        hdr["Referer"] = "https://finance.sina.com.cn/"
    try:
        req = urllib.request.Request(url, headers=hdr)
        body = urllib.request.urlopen(req, timeout=timeout).read()
        return "reachable", len(body)
    except urllib.error.HTTPError as e:
        return f"http_{e.code}", 0
    except Exception as e:
        return type(e).__name__, 0


def probe(only_get=True, sleep_s=0.3):
    print(f"{'id':26s} {'status':10s} {'probe':22s} bytes")
    print("-" * 78)
    for s in SOURCES:
        if only_get and (s.get("method") == "POST" or s["auth"] != "none"):
            continue
        if s["url"].startswith(("file://", "tcp://", "python")):
            continue
        st, n = probe_one(s)
        mark = "✅" if st == "reachable" else "❌"
        print(f"{s['id']:26s} {s['status']:10s} {mark} {st:20s} {n}")
        time.sleep(sleep_s)


def export(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(CST).isoformat(timespec="seconds"),
                   "total": len(SOURCES),
                   "by_status": {st: len(by_status(st)) for st in
                                 ["ok", "degraded", "blocked", "unauth", "untested"]},
                   "sources": SOURCES}, f, ensure_ascii=False, indent=2)
    print(f"→ {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--json", help="导出 JSON 路径")
    ap.add_argument("--id", help="只看某个源")
    args = ap.parse_args()

    srcs = [BY_ID[args.id]] if args.id else SOURCES

    if args.probe:
        probe()
        return 0

    if args.json:
        export(args.json)
        return 0

    # 默认 list
    print(f"{'id':26s} {'status':9s} {'freq':9s} {'name'}")
    print("-" * 78)
    for s in srcs:
        icon = {"ok": "✅", "degraded": "🟡", "blocked": "❌",
                "unauth": "🔑", "untested": "❔"}.get(s["status"], "?")
        print(f"{s['id']:26s} {icon}{s['status']:8s} {s['freq']:9s} {s['name']}")
    print()
    from collections import Counter
    c = Counter(s["status"] for s in SOURCES)
    print("统计:", dict(c), f"共 {len(SOURCES)} 个源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
