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
                 "量比无独立字段一律1.0; p44/p45 单位亿元; "
                 "必带 Referer: https://gu.qq.com; 字段坑: p43 是振幅% 不是 PB (PB 是 p46)",
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
        "id": "sina_kline_ma",
        "name": "新浪日K + MA (money.finance.sina.com.cn)",
        "url": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=100",
        "auth": "none (需 Referer: finance.sina.com.cn)",
        "status": "ok",
        "freq": "daily",
        "fields": "day/open/high/low/close/volume + ma_price5/ma_volume5/ma_price10/ma_volume10 (接口直算 MA)",
        "provides": [],
        "notes": "2026-10-13 实测 200: 接口直接返回 MA5/MA10, 省本地计算; "
                 "scale=240 日线, 120=30分钟, 60=小时; 与 tencent_kline 互补",
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
        "name": "东财 datacenter (龙虎榜/大宗交易/两融/陆股通)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={report}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后 (当日数据约 18:00 后出)",
        "reports": {
            "RPT_DAILYBILLBOARD_DETAILS": "龙虎榜明细 (席位解读+成功率)",
            "RPT_DAILYBILLBOARD_DETAILSNEW": "龙虎榜明细 (新版报表名, 2026 实测)",
            "RPT_DATA_BLOCKTRADE": "大宗交易 (溢价率/成交额)",
            "RPTA_RZRQ_LSHJ": "融资融券余额历史",
            "RPTA_WEB_RZRQ_GGMX": "融资融券个股明细 (新版, 排序键 DATE 非 TRADE_DATE)",
            "RPT_MUTUAL_DEAL_HISTORY": "陆股通 (北向资金) 历史",
        },
        "provides": ["enrich.json:lhb/blocktrade", "latest.json:margin"],
        "notes": "filter 日期必须 YYYY-MM-DD (横杠), 传 YYYYMMDD 会返回0条; "
                 "当日数据收盘后才出 → 必须做日期回退 (最多5天); "
                 "龙虎榜同一股票多行 (不同上榜原因), 需按股票合并; "
                 "坑: 各报表排序键不统一 (TRADE_DATE/DATE/dim_date), 传错返回 9501",
        "degrade_to": [],
    },
    {
        "id": "em_push2_clist",
        "name": "东财板块列表/资金流 (push2 clist)",
        "url": "https://push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:2&fields=f2,f3,f12,f14",
        "auth": "none",
        "status": "blocked",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "行业/概念板块涨跌/资金流/成分股",
        "provides": [],
        "notes": "容器内间歇 reset (RemoteDisconnected), 约 20 次请求后必然被封; "
                 "曾导致 sector_pool 只剩2个板块、multi_v5 因缺行业映射出0候选。"
                 "已用 tencent_mktHs + tv_screener 替代",
        "degrade_to": ["tencent_mktHs", "tv_screener", "em_push2delay_clist"],
    },
    {
        "id": "klineshare_api",
        "name": "KlineShare A股实时行情API",
        "url": "https://klineshare.cn/v1/realtime",
        "auth": "key",
        "status": "unauth",
        "freq": "realtime",
        "delay": "交易时段实时",
        "fields": "盘中价量(realtime)/当日分时(trend)/涨停专题(limit-up)/K线",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "unknown",
        "provides": ["quote", "kline", "minute", "limit_up"],
        "notes": "2026-10-14 实测: /v1/health 免Key返回 {success:true,version:1}; "
                 "其余 401 缺 X-API-Key。支持 REST + 远程 MCP (/mcp) + Agent Skill, "
                 "认证头 X-API-Key。注册拿 Key 后可接入 realtime/trend/limit-up。",
        "degrade_to": ["tencent_quote", "sina_hq"],
    },
    {
        "id": "tickflow_free",
        "name": "TickFlow 免费服务 (日K+标的信息, 无需注册)",
        "url": "pip install tickflow[all]",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "历史日K (盘中不更新)",
        "fields": "日K线(1d/1w/1M/1Q/1Y)/标的信息/交易所/标的池",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "n/a",
        "provides": ["kline", "instrument"],
        "notes": "2026-10-14 实测文档: TickFlow.free() 无需 API key; 提供历史日K与标的池; "
                 "不提供实时行情与分钟K。支持期货等多市场 (symbol 后缀 .SH/.SZ 等)。",
        "degrade_to": [],
    },
    {
        "id": "wscn_lives",
        "name": "华尔街见闻 7x24 live (多频道)",
        "url": "https://api-one.wallstcn.com/apiv1/content/lives?channel=a-stock-channel",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "标题/正文/时间/关联题材(related_themes)/关联股票(symbols)/日历标记",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "*",
        "provides": ["news", "theme", "calendar"],
        "notes": "2026-10-14 实测: a-stock/us-stock/hk-stock 三频道各 30 条, 零鉴权; "
                 "global/tech 频道超时或空。symbols 在快讯流实测为空, 关联维度靠 "
                 "related_themes (dict 数组, 取 title)。next_cursor 可翻页。",
        "degrade_to": [],
    },
    {
        "id": "cls_telegraph",
        "name": "财联社电报 (逆向算签)",
        "url": "https://www.cls.cn/api/cache?name=telegraphList",
        "auth": "sign",
        "status": "ok",
        "freq": "minute",
        "delay": "分钟级",
        "fields": "标题/正文/时间/关联股票",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "unknown",
        "provides": ["news"],
        "notes": "2026-10-14 逆向打通: 真实接口是 /api/cache?name=telegraphList "
                 "(不是网传的 /v1/roll/get_roll_list); sign=md5(sha1(raw)), "
                 "raw=字典序 key=value & 连接; 用 5 组浏览器真实请求反推 5/5 命中。"
                 "lastTime=0 返回最新 20 条。实现见 scripts/cls_sign.py。",
        "degrade_to": [],
    },
    {
        "id": "zaobao_news",
        "name": "联合早报 (SSR 解析)",
        "url": "https://www.zaobao.com/news/china",
        "auth": "none",
        "status": "ok",
        "freq": "hourly",
        "delay": "小时级",
        "fields": "标题/链接/详情页发布时间",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "unknown",
        "provides": ["news"],
        "notes": "2026-10-14 实测: 无公开 RSS (rss/*.xml 是 SPA fallback 返回 Page Not "
                 "Found), 改解析 SSR HTML; 列表页无时间, 前 8 条抓详情页补 datePublished。"
                 "Actions 出口 IP 下实测 0 条 (本地 30 条), 原因待查。",
        "degrade_to": ["cls_telegraph", "wscn_lives"],
    },
    {
        "id": "tushare_pro",
        "name": "Tushare Pro (积分制, 覆盖最全)",
        "url": "https://api.tushare.pro",
        "auth": "key",
        "status": "unauth",
        "freq": "realtime",
        "delay": "实时 (部分接口需单独权限)",
        "fields": "A股行情/财务/打板/ETF/指数/公募基金/期货/期权/债券/外汇/港股/美股/"
                 "宏观/量化因子库/大模型语料",
        "field_map": {},
        "encoding": "utf-8",
        "cors": "unknown",
        "provides": ["quote", "kline", "financial", "fund", "futures",
                     "option", "bond", "fx", "hk", "us", "macro",
                     "news", "research", "factor"],
        "notes": "2026-10-14 读官方文档 doc_id=340 整理: 接口覆盖极广 — "
                 "股票(行情/财务/打板/龙虎榜/热榜) / ETF全套 / 指数 / 公募基金(净值/持仓/经理) / "
                 "期货(日线/实时分钟 rt_fut_min 500次每分钟/持仓排名/仓单) / 期权 / 可转债 / "
                 "外汇 / 港股 / 美股(含三张财务报表) / 宏观(GDP/CPI/PPI/PMI/社融/Shibor/LPR) / "
                 "量化因子库 / 大模型语料(国家政策库/券商研报/新闻联播文字稿/"
                 "上证e互动问答/深证易互动问答)。token 从环境变量 TUSHARE_TOKEN 读, "
                 "积分制, 部分接口需单独申请权限。是唯一能一站式补齐多市场+另类数据的源。",
        "degrade_to": [],
    },
    {
        "id": "em_push2delay_clist",
        "name": "东财实时行情延迟版 (push2delay, clist 降级第三顺位)",
        "url": "https://push2delay.eastmoney.com/api/qt/clist/get?fs=m:90+t:2&fields=f2,f3,f12,f14",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "约15分钟",
        "fields": "行业/概念板块涨跌/资金流/成分股",
        "provides": [],
        "notes": "2026-10-13 实测: push2 主域被 TLS reset, 但 push2delay 同账号域全部 200; "
                 "容器 IP 风控是按域名分级的, 换国内 IP 后 push2 可恢复; "
                 "降级链: push2 clist → tencent_mktHs → tv_screener → push2delay",
    },
    {
        "id": "em_push2his_clist",
        "name": "东财全市场快照 (push2his 域, 绕开 push2 WAF)",
        "url": "https://push2his.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f20,f21&fid=f8",
        "auth": "none",
        "status": "ok",
        "freq": "realtime",
        "delay": "秒级 (与 push2 同源)",
        "fields": "f12=代码 f14=名称 f2=最新价(分) f3=涨跌幅 f4=涨跌额 f5=成交量 f6=成交额 f7=振幅 f8=换手 f9=市盈 f10=量比 f20=总市值 f21=流通市值",
        "provides": ["quotes 表 (全市场)"],
        "notes": "2026-10-13 实测 200 total=5923 (全 A 股); 关键: push2 域 TLS reset 但 "
                 "push2his 域同路径可用 → 这是 push2 WAF 的绕行方案, 不是降级; "
                 "价格单位是分 (f2=3880 → 38.80 元)",
    },
    {
        "id": "em_push2his_etf_onsite",
        "name": "东财场内基金/ETF 全量快照 (push2his)",
        "url": "https://push2his.eastmoney.com/api/qt/clist/get?pn=1&pz=200&po=1&np=1&fs=b:MK0021&fields=f12,f13,f14,f2,f3,f6",
        "auth": "none",
        "status": "ok",
        "freq": "realtime",
        "fields": "f12=代码 f13=市场(0深1沪) f14=名称 f2=最新价(分) f3=涨跌幅 f6=成交额",
        "provides": ["fund_nav 表 (场内基金实时)"],
        "notes": "2026-10-13 实测 200 total=1357 只场内基金; fs=b:MK0021 是场内基金板块参数; "
                 "ETF 在用户交易范围内, 此前 catalog 缺这个源",
    },
    {
        "id": "em_fund_lsjz",
        "name": "天天基金历史净值 (api.fund.eastmoney.com/f10/lsjz)",
        "url": "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=50",
        "auth": "none (Referer 静默鉴权, 必带)",
        "status": "ok",
        "freq": "daily",
        "delay": "T+1 (当日净值 20:00 后)",
        "fields": "DWJZ=单位净值 LJJZ=累计净值 JZZZL=日增长率 FSRQ=净值日期 SGZT=申购状态 SHZT=赎回状态",
        "provides": ["fund_nav 表"],
        "notes": "2026-10-13 实测: 不带 Referer 返回 HTTP 200 + ErrCode=-999 + Data="" (97B) "
                 "—— 静默失败比 403 更危险, 必须校验 ErrCode; "
                 "必带 Referer: https://fundf10.eastmoney.com/jjjz_{code}.html; "
                 "TotalCount 给全量条数 (510300 实测 3508)",
    },
    {
        "id": "jisilu_dividend_rate",
        "name": "集思录股息率列表 (含估值温度/5年均值)",
        "url": "https://www.jisilu.cn/data/stock/dividend_rate_list/?rp=100&page=1",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "dividend_rate=股息率 dividend_rate2 pb_temperature/pe_temperature=历史分位0-100 roe_average/revenue_average/profit_average=5年均值 负债率/质押率/行业/省份",
        "provides": [],
        "notes": "2026-10-13 实测 200, 105KB, 100 只/页; 单接口信息密度最高: "
                 "估值温度可直接做低估值筛选, 5年均值规避单期财报噪音; "
                 "需先访问首页拿 session + Referer: /data/stock/",
    },
    {
        "id": "sina_vip_kline",
        "name": "新浪多周期 K线 (vip.stock.finance.sina.com.cn)",
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=1023",
        "auth": "none (需 Referer: finance.sina.com.cn)",
        "status": "ok",
        "freq": "daily",
        "fields": "day/open/high/low/close/volume + ma_price5/ma_volume5/ma_price10/ma_volume10",
        "provides": [],
        "notes": "2026-10-13 实测 200; scale=5/15/30/60/240 分钟/日; ma=5/10/20; "
                 "datalen 最多 1023; 支持指数 sh000001; 接口直返 MA, 省本地计算; "
                 "可作东财/腾讯 K线的第三方交叉校验源 (用户反馈 K线数据有问题)",
    },
    {
        "id": "em_push2his_kline_DEPRECATED",
        "name": "东财历史K线 (push2his kline) — 已失效",
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "auth": "none",
        "status": "blocked",
        "freq": "daily",
        "fields": "(原: date/open/close/high/low/volume/amount)",
        "provides": [],
        "notes": "2026-10-13 晚间复测 rc=102 data=null — 早间 (19:04) 实测 200, 晚间 (21:xx) 失效; "
                 "疑似按 IP 动态风控或端点改版; K线一律改用 tencent_kline (fqkline) 或 sina_vip_kline; "
                 "clist 端点 (em_push2his_clist) 仍可用, 勿混淆",
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

    # ============ 集思录 jisilu.cn (2026 新版路径, 匿名 session 可拿) ============
    {
        "id": "em_fflow_history",
        "name": "东财历史资金流 (五档: 超大/大/中/小单净流入)",
        "url": "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get?secid={1|0}.{code}&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&lmt={n}&klt={101日|102周|103月|104年}",
        "auth": "none (需 Referer: quote.eastmoney.com)",
        "status": "ok",
        "freq": "daily",
        "fields": "date/主力净流入/小单净流入/中单净流入/大单净流入/超大单净流入 (元); klines 逗号分隔, 6列(部分版本7列带收盘)",
        "provides": ["level2/{code}_fflow_{date}.json"],
        "notes": "2026-10-13 实测 200; push2delay 域未被 WAF; "
                 "主力=超大单+大单; 当日数据收盘后才出; klt=101/102/103/104 日周月年",
    },
    {
        "id": "em_quote_snapshot",
        "name": "东财个股实时快照 (24字段: 盘口/资金/市值/估值/量比)",
        "url": "https://push2delay.eastmoney.com/api/qt/stock/get?secid={1|0}.{code}&fltt=2&invt=2&fields=f43,f44,...",
        "auth": "none (需 Referer: quote.eastmoney.com)",
        "status": "ok",
        "freq": "realtime",
        "fields": "f43=价 f44=高 f45=低 f46=开 f47=成交量(手) f48=成交额 f49=外盘(手) f50=内盘 f51=涨停价 f52=跌停价 f60=昨收 f71=均价 f116=总市值 f117=流通市值 f162=PE(TTM) f167=PB f168=换手% f169=涨跌额 f170=涨跌幅 f171=振幅 f177=量比",
        "provides": ["level2/{code}_quote_{date}.json"],
        "notes": "2026-10-13 实测 200; 单接口覆盖用户要求的 价/量/额/市值/PE/PB/换手/量比/内外盘/均价/振幅; "
                 "坑: f50 内盘实测值 0.85 (需折算), f177 量比 577 (需 /100)",
    },
    {
        "id": "em_tick_details",
        "name": "东财逐笔成交 (3秒聚合, 主买主卖方向)",
        "url": "https://push2delay.eastmoney.com/api/qt/stock/details/get?secid={1|0}.{code}&fields1=f1..f6&fields2=f51,f52,f53,f54,f55&pos={-N}&fltt=2",
        "auth": "none (需 Referer: quote.eastmoney.com)",
        "status": "ok",
        "freq": "realtime",
        "fields": "f51=时间 HH:MM:SS f52=成交价 f53=笔数 f54=成交量(手) f55=方向 1=主买/2=主卖",
        "provides": ["level2/{code}_tick_{date}.json"],
        "notes": "2026-10-13 实测 200; pos=-N 取最近 N 条 (倒序); 3秒聚合快照, 非逐笔委托",
    },
    {
        "id": "tx_tick_detail",
        "name": "腾讯分笔成交 (3秒聚合, B/S/M 性质 + 成交额)",
        "url": "https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={sh|sz}{code}&p={page}",
        "auth": "none (需 Referer: gu.qq.com)",
        "status": "ok",
        "freq": "realtime",
        "fields": "col1=时间 col2=价 col3=价格变动 col4=成交量(手) col5=成交金额(元) col6=性质 B=主买 S=主卖 M=中性",
        "provides": ["level2/{code}_tick_tx_{date}.json"],
        "notes": "2026-10-13 实测 200; 比东财 tick 多成交额字段; p=0 最新页; "
                 "格式 v_detail_data_sz000001=[...], 需正则提取引号内 | 分隔",
    },
    {
        "id": "tx_minute",
        "name": "腾讯分时 (每分钟累计成交量/成交额)",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sh|sz}{code}",
        "auth": "none (需 Referer: gu.qq.com)",
        "status": "ok",
        "freq": "minute",
        "fields": "HHMM 价格 累计成交量(手) 累计成交额(元) — 每日 240 行; data.date=交易日",
        "provides": ["level2/{code}_minute_{date}.json"],
        "notes": "2026-10-13 实测 200, 267 行 (9:30-15:00); 累计值需 diff 得每分钟量",
    },
    {
        "id": "sina_bill_sum",
        "name": "新浪大单分级汇总 (分档占比 + 主买/中性/主卖)",
        "url": "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_Bill.GetBillSum?symbol={sh|sz}{code}&volume=0",
        "auth": "none (需 Referer: finance.sina.com.cn)",
        "status": "ok",
        "freq": "realtime",
        "fields": "minvol=档位阈值 voltype=分档类型(0=固定4万/5万/6万/7万/8万/2万股, 1-5=成交量分位, 11=全部) totalvol=总量 totalvolpct=占比 totalamt=总额 avgprice=均价 kuvolume=主买量",
        "provides": ["level2/{code}_bill_{date}.json"],
        "notes": "2026-10-13 实测 200, 茅台 28 档; 大单占比直接用 totalvolpct; "
                 "HTTP (非 HTTPS); volume=0 全部, 40000=仅4万股以上",
    },
    {
        "id": "sina_trans_list",
        "name": "新浪逐笔成交 (含内外盘总量)",
        "url": "http://vip.stock.finance.sina.com.cn/quotes_service/view/CN_TransListV2.php?num={n}&symbol={sh|sz}{code}",
        "auth": "none (需 Referer: finance.sina.com.cn)",
        "status": "ok",
        "freq": "realtime",
        "fields": "trade_item_list[]=时间/成交量/价/方向(UP/DOWN/EQUAL); trade_INVOL_OUTVOL=外盘/内盘",
        "provides": ["level2/{code}_trans_{date}.json"],
        "notes": "2026-10-13 实测 200; HTTP; 返回 JS Array 需正则解析; 收盘后 direction 多为 EQUAL",
    },

    # ============ 集思录 jisilu.cn (2026 新版路径, 匿名 session 可拿) ============
    {
        "id": "jisilu_lof_index",
        "name": "集思录 指数 LOF 列表",
        "url": "https://www.jisilu.cn/data/lof/index_lof_list/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "minute",
        "fields": "fund_id/fund_nm/price/increase_rt/volume/amount/fund_nav/nav_dt/asset_ratio",
        "provides": [],
        "notes": "2026-10-13 逆向确认: 老 .php 路径已全部 404, 新版是无后缀路径; "
                 "需先访问首页拿 session, 再带 Referer + X-Requested-With; 20 行/页",
    },
    {
        "id": "jisilu_lof_stock",
        "name": "集思录 股票 LOF 列表",
        "url": "https://www.jisilu.cn/data/lof/stock_lof_list/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "minute",
        "fields": "fund_id/fund_nm/price/increase_rt/volume/amount/fund_nav/nav_incr_rt",
        "provides": [],
        "notes": "比 index_lof_list 多 nav_incr_rt; 同样 20 行/页",
    },
    {
        "id": "jisilu_etf_list",
        "name": "集思录 ETF 列表 (费率/管理人/申赎单位)",
        "url": "https://www.jisilu.cn/data/etf/etf_list/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "fund_id/fund_nm/index_id/fee(管理费)/m_fee(托管费)/t_fee/creation_unit(申赎单位)/issuer_nm(管理人)/t0/ex_dt",
        "provides": [],
        "notes": "费率与申赎单位是独家字段 (东财/akshare 无); 适合做 ETF 筛选特征; 20 行/页",
    },
    {
        "id": "jisilu_cb_list_new",
        "name": "集思录可转债新券列表",
        "url": "https://www.jisilu.cn/data/cbnew/cb_list_new/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "minute",
        "fields": "bond_id/bond_nm/price/increase_rt/stock_id/stock_nm/pb/convert_price",
        "provides": [],
        "notes": "用户交易范围是股票+ETF, 可转债数据作市场情绪/溢价率辅助指标",
    },
    {
        "id": "jisilu_cb_adjust",
        "name": "集思录可转债下修/强赎列表",
        "url": "https://www.jisilu.cn/data/cbnew/adjust_list/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "bond_id/bond_nm/price/stock_id/stock_nm/curr_iss_amt/convert_price/adjust_price_ratio",
        "provides": [],
        "notes": "319 条下修/强赎事件, 可作事件驱动数据",
    },
    {
        "id": "jisilu_cb_delisted",
        "name": "集思录可转债退市列表",
        "url": "https://www.jisilu.cn/data/cbnew/delisted/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "weekly",
        "fields": "bond_id/bond_nm/price/stock_id/stock_nm/issue_dt/redeem_dt/delist_dt/maturity_dt",
        "provides": [],
        "notes": "733 条退市历史, 含到期日/赎回日",
    },
    {
        "id": "jisilu_new_stock_apply",
        "name": "集思录新股申购日历",
        "url": "https://www.jisilu.cn/data/new_stock/apply/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "stock_cd/stock_nm/apply_dt/need_market_value/issue_price/above_rt/pub_pe/avg_pe",
        "provides": [],
        "notes": "打新市值要求 + 发行 PE, 100 条/页",
    },
    {
        "id": "jisilu_new_stock_hkipo",
        "name": "集思录港股 IPO 日历",
        "url": "https://www.jisilu.cn/data/new_stock/hkipo/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "stock_cd/stock_nm/market/apply_dt/list_dt/issue_price/total_shares/jsl_advise/jsl_first_incr_rt",
        "provides": [],
        "notes": "含集思录首日涨幅建议 (jsl_advise)",
    },
    {
        "id": "jisilu_new_stock_sub",
        "name": "集思录新债申购列表",
        "url": "https://www.jisilu.cn/data/new_stock/sub/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "apply_dt/stock_cd/stock_nm/price/jsl_theory_price/increase_rt/issue_amt",
        "provides": [],
        "notes": "新债申购, 含理论价 (jsl_theory_price)",
    },
    {
        "id": "jisilu_new_stock_super",
        "name": "集思录超级申 (顶格申购) 列表",
        "url": "https://www.jisilu.cn/data/new_stock/super/",
        "auth": "cookie (kbzw__Session 匿名)",
        "status": "ok",
        "freq": "daily",
        "fields": "stock_cd/stock_nm/apply_dt/raise_money/jsl_issue_price/valid_apply_account/total_draw_count",
        "provides": [],
        "notes": "中签预测相关字段 (total_draw_count)",
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


# ─── 采集分片 (多平台错峰, 避免同一 IP 打全部源触发风控) ──────────
# 背景: GitHub-hosted runner 全在 Azure, 出口是数据中心段, 国内金融源
# (东财/同花顺/雪球) 对这类 IP 一律按爬虫对待 — 东财 push2 约 20 次请求
# 后必然 TLS RST, 且按 IP 计数。所以 4 个节点各打自己那批, 单 IP 频率
# 降到 1/4; 某个 IP 被封也不影响其他源。
#
# 分片原则:
#   a = 主力稳定源 (海外/akshare/集思丽/腾讯/新浪)
#   b = 东财实时系 (push2*, 最容易被封, 单独一个 IP)
#   c = 交易所官方 + 需 key 的源 (低频高价值)
#   d = 需大陆出口 IP 的源 (只在 GitCode Space 华为云容器跑)
SHARDS = {
    "a": {
        "label": "主力稳定源 (腾讯/新浪/集思璐/海外/本地)",
        "ids": [
            # 腾讯系
            "tencent_quote", "tencent_kline", "tencent_index_history",
            "tx_tick_detail", "tx_minute",
            # 新浪系
            "sina_hq", "sina_kline_ma", "sina_kline_daily", "sina_etf_hist",
            "sina_vip_kline", "sina_bill_sum", "sina_trans_list",
            # 集思璐
            "jisilu_dividend_rate", "jisilu_lof_index", "jisilu_lof_stock",
            "jisilu_etf_list", "jisilu_cb_list_new", "jisilu_cb_adjust",
            "jisilu_cb_delisted", "jisilu_new_stock_apply",
            "jisilu_new_stock_hkipo", "jisilu_new_stock_sub",
            "jisilu_new_stock_super",
            # 东财非 push2 域 (datacenter/fund/push2ex/push2delay 历史)
            "eastmoney_zt_pool", "eastmoney_zdfenbu", "eastmoney_datacenter",
            "eastmoney_kline", "eastmoney_fund_rank", "eastmoney_fund_pingzhong",
            "em_fund_lsjz", "em_lcpd_report",
            "em_fflow_history", "em_quote_snapshot", "em_tick_details",
            "sina_news",
            # 海外 + 宏观 + 本地
            "coingecko", "sec_edgar", "macro_china_cpi_gdp",
            "local_kline_bulk", "local_snapshot", "remote_news",
            "industry_map_tv", "douyin_hot_search", "toutiao_hot",
        ],
    },
    "b": {
        "label": "东财 push2 实时系 (最易被封, 独立 IP)",
        "ids": [
            "em_push2_clist", "em_push2delay_clist", "em_push2his_clist",
            "em_push2his_etf_onsite", "em_push2his_kline_DEPRECATED",
            "eastmoney_stock_get", "eastmoney_news", "em_ann_np",
        ],
    },
    "c": {
        "label": "交易所官方 + 同花顺 + 需 key",
        "ids": [
            "sse_market", "szse_api", "cninfo_announce",
            "ths_strong_stocks", "ths_industry", "ths_industry_list",
            "ths_concept_list", "ths_financial_abstract",
            "ths_financial_abstract_v2",
            "tushare", "iwencai_openapi", "alpha_vantage", "finnhub",
            "twelvedata", "fmp", "marketstack", "yahoo_finance", "stooq",
        ],
    },
    "d": {
        "label": "需大陆出口 IP (仅 GitCode Space 华为云容器跑)",
        "ids": [
            "mootdx", "cninfo_announce", "szse_api", "sse_market",
            "xueqiu_hot", "xueqiu_anon_token", "xiaohongshu_hot",
            "cls_cn", "kaipanla", "gitcode_api",
        ],
    },
    "e": {
        "label": "akshare/baostock 库 (本地 Python 库, 不耗 IP 配额)",
        "ids": [
            "akshare", "baostock", "em_a_hist", "em_fund_etf_hist_min",
            "cn_a_stock_data", "tv_screener",
        ],
    },
}


def by_shard(shard):
    """取某个分片的源 (按 id 白名单, 保持注册表里的顺序)。"""
    ids = set((SHARDS.get(shard) or {}).get("ids") or [])
    return [s for s in SOURCES if s["id"] in ids]


def shard_of(sid):
    """反向查: 某个源属于哪个分片 (可能在多个分片, 返回列表)。"""
    return [k for k, v in SHARDS.items() if sid in set(v["ids"])]


def shard_ids(shard):
    """分片内存在注册表中的源 id (不存在的忽略, 便于容忍注册表变动)。"""
    want = set((SHARDS.get(shard) or {}).get("ids") or [])
    return [s["id"] for s in SOURCES if s["id"] in want]


def unassigned():
    """未分片的源 (ok/degraded 但不在任何 shard 里) — 用于发现遗漏。"""
    assigned = set()
    for v in SHARDS.values():
        assigned |= set(v["ids"])
    return [s["id"] for s in SOURCES
            if s["id"] not in assigned and s["status"] in ("ok", "degraded")]


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
