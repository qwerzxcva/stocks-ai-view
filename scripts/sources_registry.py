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
    # ── 2026-10-17 逆向调研新源 (docs/research/platforms-1.md) ──
    {
        "id": "vango_articles",
        "name": "stock-web-vango 公开文章流 (零鉴权 GET)",
        "url": "https://vango-member-hhjebqnncv.cn-hangzhou.fcapp.run/articles",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日级 (盘前贴为主)",
        "fields": "title/visibility/minLevel/published/publicBody(仅public)",
        "provides": ["shard_a.json:news", "data/vango_articles.json"],
        "notes": "2026-10-18 接入; 只存公开元数据+public 正文预览, 不碰会员私有内容; "
                 "每日 1 次, scripts/fetch_vango_articles.py",
    },
    {
        "id": "jrj_yaowen_daily",
        "name": "金融界每日要闻 (静态 JSON)",
        "url": "https://stockjs.jrj.com.cn/share/news/yaowen/yw{yyyy-MM-dd}.js",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "分钟级 (按天文件)",
        "fields": "title/makedate/detail/infourl/infosource/chnmb",
        "provides": ["shard_a.json:news", "news_extra.json"],
        "notes": "一天一个静态 js 文件 (var newsdata= 前缀需剥离), 实测 200; "
                 "单日 50-100 条, 天然适配 Actions 日更",
    },
    {
        "id": "aigupiao_express",
        "name": "爱股票 7x24 快讯 (带题材标签)",
        "url": "https://apis.aigupiao.com/Express/express_list/",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "秒级",
        "fields": "content/rec_time/theme/stock_infos/theme_quotes/important",
        "provides": ["shard_a.json:news", "hot_themes.json"],
        "notes": "实测 200; 快讯自带题材名+关联个股+题材涨跌幅, 增量价值在题材打标; "
                 "after=<last_time> 可增量轮询",
    },
    {
        "id": "sseinfo_qa",
        "name": "上证 e 互动董秘问答",
        "url": "https://sns.sseinfo.com/ajax/feeds.do?type=11",
        "auth": "none",
        "status": "ok",
        "freq": "hourly",
        "delay": "小时级",
        "fields": "公司/代码/问题/回答/时间 (HTML 片段)",
        "provides": ["shard_a.json:event"],
        "notes": "实测 200; type=10 全部/11 已回复/30 调研纪要; 独家董秘问答源, "
                 "对个股基本面监控有直接价值",
    },
    {
        "id": "sseinfo_survey",
        "name": "上证 e 互动调研纪要",
        "url": "https://sns.sseinfo.com/ajax/feeds.do?type=30",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "天级",
        "fields": "公司/代码/公告标题/附件链接 (HTML)",
        "provides": ["shard_a.json:event"],
        "notes": "实测有数据; 投资者关系活动记录 (调研纪要 docx/pdf 标题+链接)",
    },
    {
        "id": "stockstar_forecast",
        "name": "证券之星业绩预告",
        "url": "https://resource.stockstar.com/DataCenter/StockData/GetAchieveForecast.htm",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "天级",
        "fields": "代码/简称/公告日期/报告期/预告类型/摘要 (HTML 表格)",
        "provides": ["shard_a.json:event"],
        "notes": "实测 200 但域名解析偶发失败 (需 Actions 环境实测); 同目录还有 "
                 "主力统计/解禁页",
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
                 "lastTime=0 返回最新 20 条。实现见 scripts/cls_sign.py。"
                 "2026-10-16 复核: **该端点仍可用且实测免签也能返回 200** "
                 "(errno=0, 20 条, 31 字段); "
                 "与 cls_telegraph_v1 的差异: cache 版 title 非空率更高 (7/20 vs 0/20), "
                 "但字段更少 (31 vs 53) 且无 category 分类筛选; "
                 "lastTime 分页**仅部分生效** — 传末条 ctime 返回 19 条 (边界重叠 1 条), "
                 "无法真正翻页; 两条端点互补: cache 拿 title, v1 拿分类+富字段",
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
        "url": "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=20&type={type}",
        "auth": "cookie (匿名 xq_a_token 即可, 无需登录)",
        "status": "ok",
        "freq": "minute",
        "fields": "热门股 code/name/value/percent/current/rank_change/increment; type=12 A股 13 港股 10/11/14 美股",
        "provides": [],
        "notes": "2026-10-28 实测: 先 GET https://xueqiu.com/hq 拿匿名 xq_a_token, 再带 Cookie → 200, "
                 "type=12 返 20 条 A 股热搜 (size 上限实测 100); 匿名 token 与登录 token 取行情字段/值完全一致 (63/63); "
                 "参数名是 type 不是 _type (写 _type 返 100002 Required Integer parameter 'type'); "
                 "Host 头必须 stock.xueqiu.com, 写 xueqiu.com 会被 302; "
                 "原登记的 xueqiu.com/v5/statuses/hot/stock_list.json 属主站域, 被阿里云 WAF 拦, 已弃用",
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

    # ============ 同花顺 第二批 (2026-10-15 逆向, 全部本容器实测 200) ============
    # 采集器: scripts/ths_collect.py  |  逆向报告: data/research/ths_reverse_20261015.md
    {
        "id": "ths_kline_day",
        "name": "同花顺日K (d.10jqka v6 line)",
        "url": "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js",
        "auth": "none (Referer: d.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "delay": "日K, 收盘定格",
        "encoding": "gb18030",
        "fields": "date/open/close/high/low/volume(股)/amount(元)/amplitude/chg_pct/chg/turnover; "
                 "start=上市日, total=历史总根数, num=本次返回根数",
        "provides": ["kline/day_ths.csv", "kline/cross_check"],
        "notes": "JSONP quotebridge_v6_line_{sym}_{cyc}_{scope}; last=最近140根, all=全史(19万B/只); "
                 "周期码只支持 01=日线 21=月线 (02-20/30 全 404, 周线/分钟线此域不给); "
                 "code 前缀 hs_=A股通用 sh_=沪 sz_=深 (实测 hs_/sh_ 均 200); "
                 "顶层无 symbol 包裹 (time 系列才有), 解析时注意; "
                 "字段 11 位, 第9/10位 chg_pct/chg 历史股为空串, 需自行计算",
    },
    {
        "id": "ths_kline_month",
        "name": "同花顺月K (d.10jqka v6 line 21)",
        "url": "https://d.10jqka.com.cn/v6/line/hs_{code}/21/last.js",
        "auth": "none (Referer: d.10jqka.com.cn)",
        "status": "ok",
        "freq": "monthly",
        "encoding": "gb18030",
        "fields": "月K 全量返回 (num=total, 中兴通讯 347 根/1997-11 起)",
        "provides": ["kline/month_ths.csv"],
        "notes": "last 即全史 (不像日线只给140根); 数据从上市首月开始; "
                 "早期行含负价格 (未复权原始口径), 需注意与复权源对齐",
    },
    {
        "id": "ths_timeshare",
        "name": "同花顺分时 (d.10jqka v6 time)",
        "url": "https://d.10jqka.com.cn/v6/time/hs_{code}/last.js",
        "auth": "none (Referer: d.10jqka.com.cn)",
        "status": "ok",
        "freq": "minute",
        "delay": "实时 (盘中)",
        "encoding": "gb18030",
        "fields": "每分钟: time/price/cum_volume(股)/avg_price/minute_volume(股); "
                 "pre=昨收, isTrading, tradeTime",
        "provides": ["kline/m1_ths.csv", "intraday 校验"],
        "notes": "实测 242 根/全天 (含 14:57-15:00 尾盘); 序列连续无缺口; "
                 "顶层结构 {sym:{...}} 与 line 系列不同",
    },
    {
        "id": "ths_finance_benefit",
        "name": "同花顺利润表 JSON (basic api)",
        "url": "https://basic.10jqka.com.cn/api/stock/finance/{code}_benefit.json",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "quarterly",
        "delay": "报告期披露后",
        "fields": "45 指标 × 111 报告期 (含营业总收入/营业成本/三费/研发/投资收益/利润总额/净利润/EPS/综合收益)",
        "provides": ["financial/ths_benefit.json", "financial_summary 表"],
        "notes": "~470KB/只; 结构 {flashData, fieldflashData}; flashData.title=指标名列表, "
                 "report/simple/year 各表 + *_yoy + simple_mom 共 8 个表; "
                 "坑1: report[1] 是分隔空行, 真实数据从 report[2] 开始; "
                 "坑2: 值是带单位字符串 (27.85亿/780.25亿), 需 parse; "
                 "坑3: year 表头是年份 int 不是日期; 比 akshare 版快 10 倍且不用解析 HTML",
    },
    {
        "id": "ths_finance_debt",
        "name": "同花顺资产负债表 JSON (basic api)",
        "url": "https://basic.10jqka.com.cn/api/stock/finance/{code}_debt.json",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "quarterly",
        "fields": "81 指标 × 110 报告期 (流动资产/非流动资产/负债/所有者权益全科目)",
        "provides": ["financial/ths_debt.json"],
        "notes": "~810KB/只; 同 benefit 结构; 这是项目此前完全缺失的报表 (registry 只有摘要)",
    },
    {
        "id": "ths_finance_cash",
        "name": "同花顺现金流量表 JSON (basic api)",
        "url": "https://basic.10jqka.com.cn/api/stock/finance/{code}_cash.json",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "quarterly",
        "fields": "75 指标 × 103 报告期 (经营/投资/筹资现金流, 现金净增加额)",
        "provides": ["financial/ths_cash.json"],
        "notes": "~670KB/只; 同 benefit 结构",
    },
    {
        "id": "ths_f10_worth",
        "name": "同花顺F10 业绩预测页 (basic html)",
        "url": "https://basic.10jqka.com.cn/new/{code}/worth.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "yjycData 机构业绩预测 (年份/EPS/净利润/实际vs预测) + 31 张表 "
                 "(机构预测明细/研究员/评级/预测EPS区间/详细指标预测)",
        "provides": ["financial/ths_forecast.json"],
        "notes": "页面静态渲染无需额外 API; 注意 Content-Type 是 gbk 不是 utf-8 (乱码根因); "
                 "预测数据含 2026/2027 一致预期, 是估值模型的关键输入",
    },
    {
        "id": "ths_f10_holder",
        "name": "同花顺F10 股东页 (十大股东/股东人数)",
        "url": "https://basic.10jqka.com.cn/new/{code}/holder.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "gdrsFlashData 股东人数(旬频, 户数+较上期%) + 19 张表 "
                 "(十大股东/十大流通股东 5 期, 含机构名/持股/变化/占流通比/股份类型/orgid)",
        "provides": ["holder/ths_top10.json", "holder/ths_number.json"],
        "notes": "股东人数是旬频 (2026-09-30/09-18/09-10...), 比东财月频更细; "
                 "机构名带 orgid (如 00000379) 可关联快查; "
                 "页面 160KB, 采集需 gb18030 解码",
    },
    {
        "id": "ths_f10_bonus",
        "name": "同花顺F10 分红页 (分红/增发/配股全史)",
        "url": "https://basic.10jqka.com.cn/new/{code}/bonus.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "bonusData 分红 + 16 张表 (分红方案 59 期/增发/配股/获配机构/锁定期)",
        "provides": ["events/ths_bonus.json"],
        "notes": "分红方案含董事会日期/股东大会/实施公告日/方案说明; "
                 "增发表含实际发行价格/上市公告日/方案进度",
    },
    {
        "id": "ths_f10_operate",
        "name": "同花顺F10 经营页 (主营构成/客户供应商集中度)",
        "url": "https://basic.10jqka.com.cn/new/{code}/operate.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "quarterly",
        "encoding": "gb18030",
        "fields": "9 张表 (业务构成 26 行多报告期/前5大客户/前5大供应商/占比)",
        "provides": ["financial/ths_operate.json"],
        "notes": "主营构成按地区+按产品双维度; 客户/供应商集中度是排雷关键字段",
    },
    {
        "id": "ths_f10_event",
        "name": "同花顺F10 大事页 (高管增减持/调研/担保/处罚)",
        "url": "https://basic.10jqka.com.cn/new/{code}/event.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "21 张表 (高管增减持 175 行/股东增减持 64 行/机构调研 338 家/担保明细/处罚/披露日历)",
        "provides": ["events/ths_event.json"],
        "notes": "页面 459KB 最大; 高管增减持含变动日期/关系/数量/交易均价; "
                 "机构调研表含机构类别+名称, 是机构关注度指标",
    },
    {
        "id": "ths_f10_capital",
        "name": "同花顺F10 资本运作页 (募资/项目投入/并购)",
        "url": "https://basic.10jqka.com.cn/new/{code}/capital.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "94 张表 (募集资金/项目承诺与已投入/并购交易 24 笔含金额与进度)",
        "provides": ["events/ths_capital.json"],
        "notes": "募资使用进度表 35 行 (承诺/已投入/建设期), 可做项目落地追踪",
    },
    {
        "id": "ths_f10_field",
        "name": "同花顺F10 行业排名页",
        "url": "https://basic.10jqka.com.cn/new/{code}/field.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "4 张表 (每股收益/每股净资产 排名, 同行 42-94 只对照)",
        "provides": ["sector/ths_field_rank.json"],
        "notes": "同行横向排名, 比绝对估值更有信息量",
    },
    {
        "id": "ths_f10_equity",
        "name": "同花顺F10 股本结构页",
        "url": "https://basic.10jqka.com.cn/new/{code}/equity.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "astockconstrData1/2 股本结构 + 5 张表 (流通A股/限售/高管持股占比, 解禁时间表 139 行)",
        "provides": ["financial/ths_equity.json"],
        "notes": "解禁时间表是潜在抛压监控的核心; 股本结构比例直接可用",
    },
    {
        "id": "ths_f10_news",
        "name": "同花顺F10 个股新闻全史 (linkagedata)",
        "url": "https://basic.10jqka.com.cn/new/{code}/news.html",
        "auth": "none (Referer: basic.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "linkagedata: seq/ctime(unix)/curl/title/source(同花顺iNews等)/author/stocks/type",
        "provides": ["news/ths_stock_news.json"],
        "notes": "单只股票新闻全史 65KB 内联 JSON, 按 seq 倒序; "
                 "type 区分 yidong/news; source 标注研究栏目 (两融研究/财闻等)",
    },
    {
        "id": "ths_board_kline",
        "name": "同花顺板块日K (d.10jqka v4 line bk_, 仅行业板块)",
        "url": "https://d.10jqka.com.cn/v4/line/bk_{bk}/01/{year}.js",
        "auth": "none (Referer: d.10jqka.com.cn)",
        "status": "degraded",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "板块指数日线 (date/open/close/high/low/volume/amount)",
        "provides": ["sector/industry_kline_ths"],
        "notes": "按年分文件, 14KB/年; 实测 881272(软件开发)/881121(半导体)/881273(白酒)/881131(白电) 全 200; "
                 "**概念板块不可用**: bk_301558/bk_307822 及 gn_/hs_ 前缀全部 404 — "
                 "d.10jqka 的 bk_ 只覆盖 90 个行业板块, 375 个概念板块的 K 线此域不给; "
                 "概念板块日线降级: 用东财 clist 的板块行情或从成分股自行加权聚合; "
                 "行业板块 K 线仍可作东财板块K线的独立交叉校验源",
    },
    {
        "id": "ths_board_constituents",
        "name": "同花顺板块成分股 (q.10jqka detail 翻页)",
        "url": "http://q.10jqka.com.cn/{thshy|gn}/detail/code/{bk}/page/{n}/",
        "auth": "none (Referer: q.10jqka.com.cn)",
        "status": "ok",
        "freq": "minute",
        "delay": "盘中实时",
        "encoding": "gb18030",
        "fields": "序号/代码/名称/现价/涨跌幅%/涨跌/涨速%/换手%/量比/振幅%/成交额/流通股/流通市值/市盈率",
        "provides": ["sector/thshy_constituents", "sector/gn_constituents"],
        "notes": "翻页规律: /{kind}/detail/code/{bk}/page/{n}/ (实测 page/2 返回序号21起); "
                 "注意不是 akshare 用的 field/.../page/{n}/ajax/1/ (那条全 404); "
                 "?page=N 无效 (返回第1页); 每页 20 只; 含 14 个实时字段比东财成分股更全",
    },
    {
        "id": "ths_news_push",
        "name": "同花顺 7x24 快讯 (tapp/news/push/stock)",
        "url": "https://news.10jqka.com.cn/tapp/news/push/stock?page=1&tag=&track=website",
        "auth": "none",
        "status": "ok",
        "freq": "minute",
        "delay": "实时",
        "encoding": "utf-8",
        "fields": "title/digest/rtime(unix)/url/seq/id",
        "provides": ["news/ths_724.json"],
        "notes": "零签名 JSON (akshare stock_info_global_ths 底层); "
                 "实测 20 条/页, 覆盖全球财经不只A股; "
                 "与东财 7x24/新浪 zhibo 三源可互为备份",
    },
    {
        "id": "ths_rank_cxfl",
        "name": "同花顺持续放量/创新高排名 (data.10jqka)",
        "url": "http://data.10jqka.com.cn/rank/cxfl/field/count/order/desc/ajax/1/free/1/page/1/free/1/",
        "auth": "cookie (匿名 session 不足)",
        "status": "blocked",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "持续放量天数/创新高天数/连续上涨排名 + 8 个子榜单 (lxsz/lxxd/cxsl/ljqd/ljqs/cxg/cxd/xstp/xxtp)",
        "provides": [],
        "notes": "2026-10-15 实测 HTTP 401 (data.10jqka.com.cn 全域加鉴权); "
                 "akshare 源码路径同样 401, 说明不是 Referer 问题而是登录态; "
                 "降级: 用 ths_strong_stocks + 东财 zt_pool 组合近似; "
                 "若后续拿到匿名 cookie, 该域有 9 个独家榜单值得补采",
    },
    {
        "id": "ths_funds_flow_rank",
        "name": "同花顺行业/概念资金流排名 (data.10jqka)",
        "url": "http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/ajax/1/free/1/",
        "auth": "cookie (匿名 session 不足)",
        "status": "blocked",
        "freq": "daily",
        "encoding": "gb18030",
        "fields": "行业资金流(hyzjl)/概念资金流(gnzjl)/个股资金流(ggzjl)/大单异动(ddzz) 四类排名",
        "provides": [],
        "notes": "2026-10-15 实测 HTTP 401; 与东财 fflow 五档重复度高, 优先级低; "
                 "留档避免下次再探",
    },

    # ============ 雪球选股器 (2026-10-15 逆向, 单端点全字段) ============
    {
        "id": "xueqiu_screener",
        "name": "雪球选股器全市场 40 字段 (service/v5 screener)",
        "url": "https://xueqiu.com/service/v5/stock/screener/quote/list?page={n}&size={n}&order=desc&order_by={f}&market=CN",
        "auth": "none (Referer: xueqiu.com)",
        "status": "ok",
        "freq": "realtime",
        "delay": "盘中实时",
        "fields": "40 字段: symbol/name/current/chg/percent/percent5m/first_percent/"
                 "current_year_percent/total_percent/amplitude/turnover_rate/volume/amount/"
                 "volume_ratio/limitup_days/pe_ttm/pb_ttm/ps/pcf/eps/dividend_yield/"
                 "roe_ttm/net_profit_cagr/income_cagr/north_net_inflow/main_net_inflows/"
                 "total_shares/float_shares/market_capital/float_market_capital/"
                 "followers/issue_date_ts/tick_size/lot_size/type",
        "provides": ["latest.json:xueqiu_screener", "multi_screener 候选池", "data/xq_screener.json"],
        "notes": "采集器 scripts/xueqiu_screener_collect.py; "
                 "实测 count=5000(A股)/2919(港股)/5000(美股), size=100 单页 100 行, 0.35s/页; "
                 "**无需 cookie** (registry 旧的 xueqiu_anon_token 是给其他接口用的); "
                 "order_by 有效字段 9 个: percent/market_capital/pe_ttm/pb_ttm/volume/amount/"
                 "turnover_rate/amplitude/north_net_inflow/net_profit_cagr; "
                 "**筛选参数不生效** (pe/pb/percent/market_capital/limitup_days/roe_ttm/"
                 "net_profit_cagr 传了 count 仍恒为全量) — 必须拉全量后本地过滤; "
                 "**出口敏感**: 本机容器 IP 全通 (200, 4290B); GitHub Actions (Azure) 出口 "
                 "实测 HTTP 418 I'm a Teapot — 雪球按 IP 段风控数据中心 IP; "
                 "试过 cookie 预热/xq_a_token/移动UA/Referer/HTTP2 均无法在 Actions 绕过; "
                 "→ 该源只能在 GitCode Space (大陆 IP) 或本机跑, 不要放 GitHub Actions; "
                 "独家字段: limitup_days(连板天数)/percent5m(5分钟涨跌)/north_net_inflow(北向)/"
                 "followers(关注人数)/dividend_yield/roe_ttm/net_profit_cagr/income_cagr; "
                 "与东财 push2 clist 形成交叉校验 (后者 24 字段但有市值/量比, 前者有估值成长)",
    },
    {
        "id": "xueqiu_screener_dead_paths",
        "name": "雪球 v5 其余 API 路径 (带匿名 token 后可用)",
        "url": "https://stock.xueqiu.com/v5/stock/{quote,chart/kline,chart/minute,capital/flow,f10/cn/*,finance/cn/*}.json",
        "auth": "cookie (匿名 xq_a_token, 无需登录)",
        "status": "ok",
        "freq": "realtime",
        "fields": "快照/K线/分时/资金流/F10 公司·股东·分红·行业/财务三表·指标/选股器",
        "provides": [],
        "notes": "2026-10-28 复测推翻 2026-10-15 的 blocked 结论: 当时探测未带任何 cookie (auth=none) 所以全 404/400; "
                 "先 GET https://xueqiu.com/hq 取匿名 xq_a_token 后, quote(63 字段)/batch quote/kline(day|week)/"
                 "minute(1d|5d)/capital flow/f10 cn company·holders·bonus·industry/"
                 "finance cn income·balance·cash_flow·indicator/screener quote 均 200; "
                 "仍不可用: pankou、quotec 返 200 但 0 字节; f10 shares、f10 fund detail 404; block rank 403; "
                 "账号私有接口 (portfolio) 仍需登录态, 匿名返 60201 用户id无效; "
                 "Host 头须为 stock.xueqiu.com (写 xueqiu.com 会被 302)",
    },

    # ============ 腾讯第二批 (2026-10-16 复核子代理结论, curl+urllib 双测) ============
    {
        "id": "tencent_board_rank",
        "name": "腾讯A股全市场排行榜 (含主力资金流+多周期涨幅)",
        "url": "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?_appver=11.17.0&board_code=aStock&sort_type={price|turnover|volume|pn|speed}&direct={down|up}&offset={n}&count=200",
        "auth": "none",
        "status": "ok",
        "freq": "realtime",
        "delay": "秒级 (盘中实时)",
        "encoding": "utf-8",
        "fields": "28 字段: code/name/zxj(现价)/zd/zdf(涨跌%)/zf(振幅%)/hsl(换手%)/lb(量比)/"
                 "volume/turnover/pe_ttm/pn/zsz(总市值亿)/ltsz(流通市值亿)/speed/state/stock_type/"
                 "zllr(主力流入)/zllc(主力流出)/zljlr(主力净流入)/zllr_d5/zllc_d5(5日流)/"
                 "zdf_d5/zdf_d10/zdf_d20/zdf_d60/zdf_w52/zdf_y(年初至今)",
        "provides": ["latest.json:market.stocks", "data/board_rank.json"],
        "notes": "2026-10-16 实测: HTTP 200, 95457B/200只/0.17s; total=5576 (A股全量); "
                 "**sort_type 白名单极窄, 仅 5 个**: price/turnover/volume/pn/speed "
                 "— 传 zdf/hsl/zllr/zllc/ltsz/zsz/pe_ttm/zd/zdf_w52/zdf_d5/zdf_d60/zllr_d5/lb/zxj "
                 "一律返回 code=1619377101 '参数错误:sort_type error' (全部实测); "
                 "board_code 只有 aStock 有数据, hkStock/usStock/etf/hk/us 全 total=0 (实测); "
                 "offset 分页实测 0/200/400/5370 均正常, 5576 只需 28 请求 (现 qt.gtimg.cn 需 ~93 次); "
                 "direct=up 可拿跌幅榜, state='S' 为停牌; "
                 "坑: pn 字段含义未定 (茅台 6.53, 疑为市净率另一口径/每股净资产, **未核实**); "
                 "坑: volume/turnover/zsz/ltsz 单位需再确认 (volume=57677 疑为万手, turnover=753771)",
        "degrade_to": ["tencent_quote", "em_push2_clist"],
    },
    {
        "id": "tencent_newfqkline",
        "name": "腾讯K线任意日期区间 (newfqkline)",
        "url": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=kline_dayqfq&param={code},day,{start},{end},640,qfq&r=0.82",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日K, 收盘定格",
        "encoding": "utf-8",
        "fields": "date/open/close/high/low/volume (前复权)",
        "provides": ["kline/backfill", "global_kline"],
        "notes": "2026-10-16 实测 HTTP 200 (sh000919, 2025-01-01~2025-03-01); "
                 "JSONP 变量名固定 kline_dayqfq; 日期格式 YYYY-MM-DD, 起止都生效; "
                 "单次上限 640 根, 超长历史需按年分段; "
                 "**这是唯一支持任意起止区间的 K 线端点** — tencent_kline/tencent_index_history "
                 "都只能取最近 N 根, 补历史必须翻页; 指数代码同样适用",
    },
    {
        "id": "tencent_hk_kline",
        "name": "腾讯港股后复权K线 (含回购/派息事件)",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk{code},day,{start},{end},{n},hfq",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日K",
        "encoding": "utf-8",
        "fields": "date/open/close/high/low/volume + 事件对象{cqr(除权日),FHcontent(派息),"
                 "HGcontent(回购),paixiri,hgcgContent,ggContent} + 换手率 + 成交额 + qt 实时快照 80+ 字段",
        "provides": ["overseas/hk_kline", "overseas/hk_events"],
        "notes": "2026-10-16 实测 HTTP 200, 119407B (hk00700, 2026-09-01~2026-10-15); "
                 "**起止日期参数真实生效** (与美股 usfqkline 的行为不同, 见下条); "
                 "周期可换 week (返回键变 hfqweek); 复权用 hfq (qfq 未验证); "
                 "附带的 qt 是港股实时快照 (含52周高低/市值/PE/回购数据); "
                 "**除权事件文本质量优于现有港股源** — 回购公告直接可读, 如 '回购24.00万股, 均价418.271港元'",
    },
    {
        "id": "tencent_us_kline",
        "name": "腾讯美股K线 (日期参数失效, 留档)",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param=us{code},day,{start},{end},{n},qfq",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "delay": "日K",
        "encoding": "utf-8",
        "fields": "date/open/close/high/low/volume + qt 实时快照 + pandata + market(全市场状态串)",
        "provides": [],
        "notes": "2026-10-16 实测: **日期区间参数完全失效** — "
                 "param=usAAPL,day,2026-10-01,2026-10-15,10,qfq 与 "
                 "param=usAAPL,day,2011-06-02,2011-06-02,10,qfq 返回**完全相同的 1541 字节** "
                 "(都是首日 2011-06-02 + 当日两条), start/end 被忽略, 只按 count 退化; "
                 "**要美股历史不能用这个端点**; "
                 "可用价值只剩 qt 实时快照 (52周高低/市值/PE/交易所后缀, 比 qt.gtimg.cn 全); "
                 "代码格式 us{TICKER}, 返回带 .OQ 后缀; usDJI 等指数同构可用, 指数 qt 带成分统计",
    },
    {
        "id": "tencent_index_weektrends",
        "name": "腾讯指数周线全史 (单请求 20 年)",
        "url": "https://web.ifzq.gtimg.cn/other/klineweb/klineWeb/weekTrends?code={code}&type=qfq&_var=trend_qfq&r=0.35",
        "auth": "none",
        "status": "ok",
        "freq": "weekly",
        "delay": "周K, 周末定格",
        "encoding": "utf-8",
        "fields": "[date, close] 周线收盘价全史",
        "provides": ["cycle_deep 长期趋势", "index_weektrends"],
        "notes": "2026-10-16 实测 HTTP 200, 30017B (sh000919 从 2005-01-04 起 1100+ 根); "
                 "无分页, 单请求拿 20 年; JSONP 变量名须与 _var 一致; type=qfq; "
                 "**只有收盘价, 无开高低量** — 用途限长期趋势/年线位置判断",
    },

    # ============ 新浪第二批 (2026-10-16 复核) ============
    {
        "id": "sina_futures_realtime",
        "name": "新浪期货实时盘口 (分品种, 修正 akshare bug)",
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData?page=1&num={n}&sort=position&asc=0&node={品种}_qh&base=futures",
        "auth": "none",
        "status": "degraded",
        "freq": "realtime",
        "delay": "实时 (夜盘 ticktime 到 22:59:59)",
        "encoding": "utf-8",
        "fields": "symbol/exchange/name/trade/settlement/presettlement/open/high/low/close/"
                 "bidprice1/askprice1/bidvol1/askvol1/volume/position/ticktime/tradedate/"
                 "preclose/changepercent/bid/ask/prevsettlement",
        "provides": [],
        "notes": "2026-10-16 实测: **修正 akshare 重大 bug** — akshare futures/cons.py:121-129 "
                 "写死 node=001, 导致 Market_Center.getHQFuturesData 恒返回 []; "
                 "正确格式是 {大写品种代码}_qh; "
                 "**node 白名单实测极窄**: sngz_qh(10年国债, 1422B) 与 IM_qh(中证1000, 2471B) 通; "
                 "TA_qh / IF_qh / T_qh 实测返回 [] (空, 2B) — 子代理称 TA_qh 可用但本次实测为空; "
                 "同一 node 返回该品种全部挂牌合约 (主力连续 IM0/TA0 排最前), 按 position 降序即主力置顶; "
                 "changepercent 是小数 (-0.0001367 = -0.01367%); "
                 "**白名单未穷举** (akshare futures_inventory_em_symbol_dict 约 70 个品种待逐一试), "
                 "故标 degraded 而非 ok",
    },
    {
        "id": "sina_moneyflow_zjlrqs",
        "name": "新浪个股主力资金趋势 (含行业对比)",
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={n}&sort=opendate&asc=0&daima={sym}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "T-1 至 T 日, 当日盘中有当日数据 (部分延迟)",
        "encoding": "utf-8",
        "fields": "opendate/trade/changeratio/turnover/netamount/ratioamount/r0_net/r0_ratio/"
                 "r0x_ratio/cnt_r0x_ratio/cate_ra/cate_na",
        "provides": ["money/fflow", "flow_divergence"],
        "notes": "2026-10-16 实测 HTTP 200, 869B; "
                 "netamount 单位是元 (不是万元, 需 /1e4); r0=超大单; "
                 "独有 r0x_ratio (超大单占主力比%) 与 cate_ra/cate_na (同类/行业对比), lscjfb 没有; "
                 "偶发 15s 超时 (实测第一次超时第二次成功), 需重试",
    },
    {
        "id": "sina_moneyflow_lscjfb",
        "name": "新浪个股历史成交分布 (资金四档)",
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb?page=1&num={n}&sort=opendate&asc=0&daima={sym}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "T-1 至 T 日",
        "encoding": "utf-8",
        "fields": "opendate/trade/changeratio/turnover/netamount/ratioamount/r0/r1/r2/r3/"
                 "r0_net/r1_net/r2_net/r3_net",
        "provides": ["money/fflow", "flow_divergence"],
        "notes": "2026-10-16 实测 HTTP 200, 995B; "
                 "四档 (r0 超大单 ~ r3 小单) 成交额+净额都给全, 比 zjlrqs 细; "
                 "**生产已在用** (scripts/moneyflow_scan.py:52) 但此前未注册; "
                 "同族 ssl_qsfx_zjlrrd 实测返回 Service not found (死)",
    },
    {
        "id": "sina_esg_stocks",
        "name": "新浪ESG多机构评级 (53418 条, 10 家机构)",
        "url": "https://global.finance.sina.com.cn/api/openapi.php/EsgService.getEsgStocks?page={p}&num={n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "symbol(SZ000001 格式)/market/esg_info[{agency,agency_name,esg_score,esg_dt,remark}] + total",
        "provides": ["sector/esg_pool.json"],
        "notes": "2026-10-16 实测 HTTP 200, total=53418; 首请求 5.76s (慢, 后续 0.3-0.8s); "
                 "聚合 10 家机构 (商道融绿/中财绿金院/盟浪/中诚信/晨星/联合赤道/妙盈/有机数/中债/国证指数); "
                 "**symbol 格式 SZ000001, 与其他 4 个 ESG 接口全不兼容**, 需归一化",
    },
    {
        "id": "sina_esg_msci",
        "name": "新浪 MSCI ESG 评级 (8959 条, E/S/G 分项)",
        "url": "https://global.finance.sina.com.cn/api/openapi.php/EsgService.getMsciEsgStocks?p={p}&num={n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "symbol(000001.SZ/00066.HK)/quarter_date/market/esg_rating/env_score/social_score/governance_score + total",
        "provides": ["sector/esg_pool.json"],
        "notes": "2026-10-16 实测 HTTP 200, total=8959; **分页参数用 p 不是 page**; "
                 "E/S/G 三项分开且带 quarter_date, 可做 ESG 趋势; 5 个 ESG 源里结构最规范",
    },
    {
        "id": "sina_esg_zd",
        "name": "新浪中证 ESG 评级 (8200 条, 评级+分数)",
        "url": "https://global.finance.sina.com.cn/api/openapi.php/EsgService.getZdEsgStocks?page={p}&num={n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "ticker(300124.SZ)/market/esg_rating/esg_score/environmental_rating/environmental_score/"
                 "social_rating/social_score/governance_rating/governance_score/report_date + total",
        "provides": ["sector/esg_pool.json"],
        "notes": "2026-10-16 实测 HTTP 200, total=8200; "
                 "坑: score 是字符串且带评级后缀如 '87.53(AAA)', 需正则拆分数与评级",
    },
    {
        "id": "sina_esg_rft",
        "name": "新浪润凡 ESG 评级 (4518 条, 15 细分维度)",
        "url": "https://global.finance.sina.com.cn/api/openapi.php/EsgService.getRftEsgStocks?p={p}&num={n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "symbol/exchange/industry/market + esg/env/resource/emission/innovation/social/work/"
                 "human_rights/community/product/governance/management/holder/csr/zy "
                 "各带 score+date+grade + total",
        "provides": ["sector/esg_pool.json"],
        "notes": "2026-10-16 实测 HTTP 200, total=4518; **唯一覆盖 US/CN/HK 三市场**; "
                 "15 个分项各带 score+date+grade, 粒度最细, ESG 因子研究首选; "
                 "分页参数用 p; score 同样带评级后缀",
    },
    {
        "id": "sina_esg_hz",
        "name": "新浪华证 ESG 评级 (6355 条, 含中文名)",
        "url": "https://global.finance.sina.com.cn/api/openapi.php/EsgService.getHzEsgStocks?page={p}&num={n}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "date/symbol(603605.SH)/market/name(中文名)/esg_score/esg_score_grade/"
                 "e_score/e_score_grade/s_score/s_score_grade/g_score/g_score_grade/created_time + total",
        "provides": ["sector/esg_pool.json"],
        "notes": "2026-10-16 实测 HTTP 200, total=6355; "
                 "**5 个 ESG 源里唯一带公司中文名**, 省一次代码→名称映射; "
                 "esg_score 百分制不带评级后缀, 解析最简单",
    },
    {
        "id": "sina_spot_goods",
        "name": "新浪商品现货价格指数 (BDI/钢坯/澳粉)",
        "url": "https://stock.finance.sina.com.cn/futures/api/openapi.php/GoodsIndexService.get_goods_index?symbol={BDI|GP|PB}&table=0",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "gbk",
        "fields": "opendate/price/zde/zdf + page/total_number",
        "provides": ["macro/goods_index.json"],
        "notes": "2026-10-16 实测 HTTP 200, 852412B (BDI 全史); "
                 "**必须显式传 symbol** — 不带 symbol/table 时返回错乱脏数据 "
                 "(2016 年起, symbol/name 全空, price 在 53~2090 间跳变的 54 条); "
                 "zdf 是小数 (0.0317 = 3.17%); symbol 为中文时需 URL 编码 (未实测)",
    },
    {
        "id": "sina_option_stock_name",
        "name": "新浪 ETF 期权合约月份日历",
        "url": "https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "cateList(50ETF/300ETF/500ETF/科创50)/contractMonth(可用合约月)/stockId/cateId",
        "provides": ["events/option_calendar.json"],
        "notes": "2026-10-16 实测 HTTP 200, 238B, 零参数; "
                 "数据有脏项: cateList 有重复 ('50ETF' 出现两次) 且 '科创扳50' 是错别字 (应为板), "
                 "contractMonth 也有重复 (2026-10 出现两次), 需去重清洗; "
                 "仅覆盖 ETF 期权, 不含商品期权",
    },
    {
        "id": "sina_hk_klc_js",
        "name": "新浪港股指数K线 (K2 编码 JS)",
        "url": "https://finance.sina.com.cn/stock/hkstock/{code}/klc2_kl.js",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "delay": "日K",
        "encoding": "gbk",
        "fields": "港股指数K线 (var KLC_K2_{code}=\"K2/...\" 形式, 需解码)",
        "provides": [],
        "notes": "2026-10-16 实测 HTTP 200, 75199B (CES100); "
                 "**host 必须是 finance.sina.com.cn** — stock.finance.sina.com.cn 同路径实测 404; "
                 "数据是 K2 前缀的压缩编码字符串, 不是标准 JSON, 需专用解码器 (解析成本高); "
                 "标 degraded 是因为可用但解析代价大, 且 tencent_hk_kline 已覆盖港股K线需求",
    },
    {
        "id": "sina_us_index_static",
        "name": "新浪美股指数静态数据 (K2 编码)",
        "url": "https://finance.sina.com.cn/staticdata/us/{symbol}",
        "auth": "none",
        "status": "degraded",
        "freq": "realtime",
        "delay": "秒级",
        "encoding": "gbk",
        "fields": "美股指数/个股静态页 (var KLC_K2_{symbol} 形式, 非结构化)",
        "provides": [],
        "notes": "2026-10-16 实测 HTTP 200, 112352B (.IXIC); "
                 "体量大且非结构化, 同 K2 编码需专用解码; 解析成本高于收益, 标 degraded",
    },
    {
        "id": "hexun_dead_all",
        "name": "和讯所有数据接口 (已死, 留档)",
        "url": "http://webusstock.hermes.hexun.com/usa/kline | http://stockdata.stock.hexun.com/zrbg/",
        "auth": "none",
        "status": "blocked",
        "freq": "-",
        "fields": "美股K线/业绩报告列表",
        "provides": [],
        "notes": "2026-10-16 实测: akshare 全库 grep hexun 仅 4 处命中, 对应 2 个 URL 全死; "
                 "webusstock.hermes.hexun.com **DNS 无法解析主机名** (彻底死亡); "
                 "stockdata.stock.hexun.com https 证书错乱 (CN=*.unionpayintl.com 银联国际证书劫持), "
                 "改 http 可连但 301 到 stock.hexun.com 首页, 无 JSON 端点; "
                 "另: stock.gtimg.cn/data/hk_rank.php 实测返回加密串 "
                 "{\"code\":2003,\"msg\":\"鉴权失败时鉴权失败支鉴权街\"} (200, 116B, 需专用解密); "
                 "**结论: 和讯放弃猜 URL 路线, 若要继续需改抓 quote.hexun.com 页面 XHR**",
    },

    # ============ 东财第二批 (2026-10-16 复核子代理结论, 15 端点全实测 200) ============
    # 核心发现: emweb.securities.eastmoney.com/PC_HSF10/*/PageAjax 是一次请求拿整类 F10 数据的富接口
    {
        "id": "em_org_survey",
        "name": "东财机构调研 (RPT_ORG_SURVEYNEW)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ORG_SURVEYNEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后",
        "fields": "SECUCODE/SECURITY_CODE/SECURITY_NAME_ABBR/NOTICE_DATE/RECEIVE_START_DATE/"
                 "RECEIVE_END_DATE/RECEIVE_OBJECT_TYPE/RECEIVE_OBJECT/RECEIVE_PLACE/"
                 "RECEIVE_WAY_EXPLAIN/RECEPTIONIST/NUM/ORG_NAME",
        "provides": ["events/org_survey.json", "stock_detail 机构关注度"],
        "notes": "2026-10-16 实测 200, 3150B, pages=96170 (总量约 29 万条); "
                 "RECEPTIONIST 含接待高管姓名 (如 '公司董事长、总裁 谢子龙, 独立董事 武连峰...'); "
                 "RECEIVE_OBJECT_TYPE 区分调研方式 (业绩说明会/网络文字互动); "
                 "个股查询加 filter=(SECUCODE=\"600519.SH\"); "
                 "**registry 此前完全无机构调研维度**, 与 THS F10 event 的调研表可互校",
    },
    {
        "id": "em_shareholder_increase",
        "name": "东财股东增减持 (RPT_SHARE_HOLDER_INCREASE)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_SHARE_HOLDER_INCREASE&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "公告后实时",
        "fields": "SECURITY_CODE/SECURITY_NAME_ABBR/HOLDER_NAME/NOTICE_DATE/START_DATE/END_DATE/"
                 "CHANGE_NUM/CHANGE_NUM_SYMBOL/CHANGE_RATE/DIRECTION/AFTER_CHANGE_RATE/"
                 "HOLD_RATIO/CLOSE_PRICE/TRADE_AVERAGE_PRICE/MARKET",
        "provides": ["events/shareholder_increase.json"],
        "notes": "2026-10-16 实测 200, 2086B, pages=49023; "
                 "DIRECTION=增持/减持; **MARKET 区分 大宗交易/二级市场** (关键); "
                 "CHANGE_RATE 是变动比例%, AFTER_CHANGE_RATE 是变动后持股比例; "
                 "增减持是强信号, 与 THS F10 event 的高管增减持互补 (那边是高管视角)",
    },
    {
        "id": "em_holder_num_latest",
        "name": "东财最新股东户数 (RPT_HOLDERNUMLATEST)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_HOLDERNUMLATEST&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=END_DATE&sortTypes=-1&source=WEB&client=WEB&filter=(SECURITY_CODE%3D%22600519%22)",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "SECURITY_CODE/HOLDER_NUM/PRE_HOLDER_NUM/HOLDER_NUM_CHANGE/"
                 "HOLDER_NUM_RATIO/INTERVAL_CHRATE/AVG_MARKET_CAP/AVG_HOLD_NUM/"
                 "TOTAL_MARKET_CAP/END_DATE/PRE_END_DATE/CLOSE_PRICE/CHANGE_SHARES",
        "provides": ["holder/num.json", "stock_detail 筹码集中度"],
        "notes": "2026-10-16 实测 200, 799B (茅台 HOLDER_NUM=296404, 较上期 +21.9%); "
                 "**filter 必带 SECURITY_CODE, 不是 SECUCODE** (写 SECUCODE 会静默返回空); "
                 "户数减少 + 股价涨 = 筹码集中 (强看多); INTERVAL_CHRATE 是区间涨跌幅; "
                 "与 THS F10 的旬频 gdrsFlashData 形成 密度差: 东财季度 vs 同花顺旬度",
    },
    {
        "id": "em_org_hold_allmarket",
        "name": "东财机构持仓汇总全市场 (RPT_MAIN_ORGHOLD)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_MAIN_ORGHOLD&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "SECURITY_CODE/SECURITY_NAME_ABBR/REPORT_DATE/ORG_TYPE/ORG_TYPE_NAME/"
                 "HOULD_NUM/TOTAL_SHARES/HOLD_VALUE/FREESHARES_RATIO/HOLDCHA/HOLDCHA_NUM/"
                 "HOLDCHA_RATIO/QCHANGE_RATE/FREE_MARKET_CAP",
        "provides": ["holder/org_hold.json", "机构持仓全市场扫描"],
        "notes": "2026-10-16 实测 200, 1849B, pages=325332 (全市场按季扫描); "
                 "ORG_TYPE 00=机构汇总 01=基金 05=保险 07=其他; "
                 "HOLDCHA_RATIO 是持仓变动%, QCHANGE_RATE 是季度变化率; "
                 "**全市场扫描机构持仓变动的唯一源**, 可按 HOLDCHA_RATIO 排序找机构加仓股",
    },
    {
        "id": "em_f10_freeholders",
        "name": "东财F10 十大流通股东 (RPT_F10_EH_FREEHOLDERS)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_EH_FREEHOLDERS&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECUCODE%3D%22600519.SH%22)&source=HSF10&client=PC",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "HOLDER_NAME/HOLD_NUM/FREE_HOLDNUM_RATIO/HOLD_NUM_CHANGE/CHANGE_RATIO/"
                 "HOLDER_RANK/HOLDER_TYPE/SHARES_TYPE/HOLDER_MARKET_CAP/FREE_RATIO_QOQ/"
                 "HOLDER_NEWTYPE/IS_LANDSTOCK/UPDATE_DATE/END_DATE",
        "provides": ["holder/free_holders.json"],
        "notes": "2026-10-16 实测 200, 4140B, pages=314; "
                 "**IS_LANDSTOCK=1 表示陆股通** (香港中央结算); HOLDER_NEWTYPE 已归一化; "
                 "比 registry 的 em_datacenter_lhb 更结构化; "
                 "与 THS F10 holder 页互校 (后者带 orgid 可跳工商)",
    },
    {
        "id": "em_f10_orghold_history",
        "name": "东财F10 机构持仓历史 (RPT_F10_MAIN_ORGHOLD)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_MAIN_ORGHOLD&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECUCODE%3D%22600519.SH%22)&source=HSF10&client=PC",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "REPORT_DATE/TOTAL_ORG_NUM/TOTAL_FREE_SHARES/TOTAL_MARKET_CAP/"
                 "TOTAL_SHARES_RATIO/IS_INCREASE/CHANGE_RATIO_CHANGE/FREE_SHARES_CHANGE/"
                 "CHANGE_RATIO/CLOSE/TOTAL_ORG_NUM_CHANGE/LABEL_NAME/NOTICE_DATE",
        "provides": ["holder/org_hold_history.json"],
        "notes": "2026-10-16 实测 200, 1577B, pages=33 (茅台 33 期历史); "
                 "LABEL_NAME 为 '机构重仓' 等标签; IS_INCREASE -1=减持; "
                 "TOTAL_ORG_NUM_CHANGE 是机构家数增减 (茅台本期 +339 家)",
    },
    {
        "id": "em_f10_coretheme_board",
        "name": "东财F10 核心题材所属板块 (RPT_F10_CORETHEME_BOARDTYPE)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_CORETHEME_BOARDTYPE&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECUCODE%3D%22600519.SH%22)&source=HSF10&client=PC",
        "auth": "none",
        "status": "ok",
        "freq": "weekly",
        "delay": "周更新",
        "fields": "SECUCODE/BOARD_CODE/BOARD_NAME/SELECTED_BOARD_REASON/IS_PRECISE/"
                 "BOARD_RANK/BOARD_YIELD/NEW_BOARD_CODE/DERIVE_BOARD_CODE",
        "provides": ["hot_impact 题材归因", "sector/core_theme.json"],
        "notes": "2026-10-16 实测 200, 1236B, pages=10; "
                 "**IS_PRECISE=1 是精确匹配板块**; SELECTED_BOARD_REASON 是入选理由长文本 "
                 "(如 '2025年年报显示，公司主导产品贵州茅台酒是我国大曲酱香型白酒的鼻祖...'); "
                 "NEW_BOARD_CODE 形如 BK0999 可反查 push2 板块行情; "
                 "**个股题材归因的唯一结构化源** — 直接回答'为什么这只票在这板块'",
    },
    {
        "id": "em_f10_mainop",
        "name": "东财F10 主营构成 (RPT_F10_FN_MAINOP)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FN_MAINOP&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECUCODE%3D%22600519.SH%22)&source=HSF10&client=PC",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "MAINOP_TYPE/ITEM_NAME/MAIN_BUSINESS_INCOME/MBI_RATIO/GROSS_RPOFIT_RATIO/"
                 "RANK/REPORT_DATE/REPORT_NAME",
        "provides": ["financial/main_op.json"],
        "notes": "2026-10-16 实测 200, 1997B, pages=139; "
                 "MAINOP_TYPE 区分按产品/按地区/按行业; "
                 "与 THS F10 operate 页互校 (两个独立源算主营占比)",
    },
    {
        "id": "em_reportapi_stock_report",
        "name": "东财个股研报 (reportapi, 含盈利预测+评级+PDF)",
        "url": "https://reportapi.eastmoney.com/report/list?industryCode=*&pageSize={n}&industry=*&rating=*&ratingChange=*&beginTime={begin}&endTime={end}&pageNo={p}&fields=&qType=0&orgCode=&code={code}&rcode=&p={p}&pageNum={p}&pageNumber={p}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "实时",
        "fields": "title/stockName/stockCode/orgSName/publishDate/infoCode/"
                 "predictThisYearEps/predictThisYearPe/emRatingName/sRatingName/"
                 "researcher/attachPages/indvInduName/count/TotalPage",
        "provides": ["events/research_reports.json", "盈利预测输入"],
        "notes": "2026-10-16 实测 200, 1493B, 1.10s (茅台 2026-09-01~10-16); "
                 "qType=0 个股研报; **PDF 直链 = https://pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf**; "
                 "predictThisYearEps/Pe 是券商一致预期, 可与 THS F10 worth 的机构预测互校; "
                 "emRatingName 东财评级 vs sRatingName 券商原评级; "
                 "**registry 此前无任何研报源**",
    },
    {
        "id": "em_hsf10_shareholder_research",
        "name": "东财F10 股东研究全量 (一次请求拿全)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "gdrs(股东户数 HOLDER_TOTAL_NUM/AVG_FREE_SHARES/AVG_HOLD_AMT/HOLD_FOCUS/"
                 "HOLD_RATIO_TOTAL) + sdltgd(十大流通股东) + sdgd(十大股东) + jgcc(机构持仓) + "
                 "jjcg(基金持仓 含 NETVALUE_RATIO 基金净值占比) + ltgf(流通股份) + "
                 "jgcc_date + jjcg_date + sjkzr(实际控制人)",
        "provides": ["holder/all_in_one.json"],
        "notes": "2026-10-16 实测 200, 36341B, 0.37s (茅台); "
                 "**一个接口覆盖 5 类数据**, 比分别调 RPT_HOLDERNUMLATEST/"
                 "RPT_F10_EH_FREEHOLDERS/RPT_F10_MAIN_ORGHOLD 省 4 次请求; "
                 "HOLD_FOCUS 是集中度标签 ('非常分散'/'较为集中'...); "
                 "jjcg 的 NETVALUE_RATIO 是该基金净值中本股的占比, 可反推基金重仓程度; "
                 "**本轮价值最高的发现**",
    },
    {
        "id": "em_hsf10_company_survey",
        "name": "东财F10 公司概况 (高管/曾用名/发行信息)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "实时",
        "fields": "jbzl(ORG_NAME/ORG_NAME_EN/FORMERNAME/SECURITY_TYPE/EM2016/PRESIDENT/"
                 "LEGAL_PERSON/CHAIRMAN/ORG_TEL/ORG_EMAIL/ADDRESS/PROVINCE/EMP_NUM/"
                 "ORG_PROFILE/BUSINESS_SCOPE) + fxxg(FOUND_DATE/LISTING_DATE/"
                 "AFTER_ISSUE_PE/ISSUE_PRICE/TOTAL_FUNDS/NET_RAISE_FUNDS/"
                 "OPEN_PRICE/CLOSE_PRICE/TURNOVERRATE)",
        "provides": ["meta/company_survey.json"],
        "notes": "2026-10-16 实测 200, 2778B, 0.32s; "
                 "EM2016 是行业三级分类 '食品饮料-饮料-白酒'; "
                 "FORMERNAME 含曾用名 ('贵州茅台→G茅台'); "
                 "EMP_NUM 员工数 + ORG_PROFILE 公司简介长文本; "
                 "registry 此前无公司基本信息源",
    },
    {
        "id": "em_hsf10_capital_structure",
        "name": "东财F10 股本结构 (含 2001 至今变动全史)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "实时",
        "fields": "gbjg(最新股本 TOTAL_SHARES/UNLIMITED_SHARES/LISTED_A_SHARES/"
                 "LIMITED_SHARES/LISTED_SHARES_RATIO) + lngbbd(历年股本变动含 CHANGE_REASON) + "
                 "gbgc(股本构成) + xsjj(限售解禁)",
        "provides": ["financial/capital_structure.json", "解禁监控"],
        "notes": "2026-10-16 实测 200, 14464B, 0.43s; "
                 "lngbbd 含 2001-2026 全史, CHANGE_REASON 标注 回购/送股上市/股改 等原因; "
                 "xsjj 茅台为空 (无限售), 有解禁的票才有数据; "
                 "与 THS F10 equity 的解禁时间表互校",
    },
    {
        "id": "em_hsf10_bonus_financing",
        "name": "东财F10 分红融资 (可算股息率)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "实时",
        "fields": "fhyx(分红送配 IMPL_PLAN_PROFILE/ASSIGN_PROGRESS/EQUITY_RECORD_DATE/"
                 "EX_DIVIDEND_DATE/PAY_CASH_DATE) + lnfhrz(历年分红融资 TOTAL_DIVIDEND/"
                 "SEO_NUM/ALLOTMENT_NUM/IPO_NUM) + zfmx + pgmx",
        "provides": ["events/bonus_financing.json", "股息率计算"],
        "notes": "2026-10-16 实测 200, 4098B, 0.55s; "
                 "IMPL_PLAN_PROFILE 直接可读 ('10派280.2423元'); "
                 "TOTAL_DIVIDEND 是年度累计分红总额(元) — 茅台 2025 年 650.3 亿; "
                 "配合市值可算真实股息率, 比集思录的 cb 分红更通用",
    },
    {
        "id": "em_hsf10_business_analysis",
        "name": "东财F10 主营构成 (中文 key, 67KB 富响应)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "zyfw(主营范围) + zygcfx(主营构成分析 ITEM_NAME/MAIN_BUSINESS_INCOME/"
                 "MBI_RATIO/GROSS_RPOFIT_RATIO/RANK/REPORT_DATE/REPORT_NAME) + jyzl(经营质量)",
        "provides": ["financial/business_analysis.json"],
        "notes": "2026-10-16 实测 200, 67671B, 0.33s; "
                 "**key 是中文 (zyfw/zygcfx/jyzl) 不是英文**, 解析时注意; "
                 "响应大 (67KB), 因含多报告期×多维度交叉; "
                 "与 em_f10_mainop (英文 key) 是同一数据的两个入口",
    },
    {
        "id": "em_hsf10_operations_required",
        "name": "东财F10 经营评述 (99KB 长文本)",
        "url": "https://emweb.securities.eastmoney.com/PC_HSF10/OperationsRequired/PageAjax?code={SH|SZ}{code}",
        "auth": "none",
        "status": "degraded",
        "freq": "quarterly",
        "delay": "季报后",
        "fields": "经营情况评述长文本 (chartReportUrl + HTML 混合)",
        "provides": [],
        "notes": "2026-10-16 实测 200, 99307B, 0.38s; "
                 "chartReportUrl 指向 https://bdstatics.eastmoney.com/web/prd/pcyearReport... 图表; "
                 "**纯长文本需 NLP 加工才能用**, 标 degraded 而非 ok; "
                 "对 LLM 复盘 (ai_review) 有输入价值, 但结构化入库暂不适用",
    },
    {
        "id": "em_datacenter_dead_reportnames",
        "name": "东财 datacenter 失效报表名 (留档)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_*",
        "auth": "none",
        "status": "blocked",
        "freq": "-",
        "fields": "-",
        "provides": [],
        "notes": "2026-10-16 实测返回 9501 '报表配置不存在' 的报表名: "
                 "RPT_ORG_SURVEYJIGO / RPT_F10_MAIN_FUNDHOLD / RPT_F10_MAIN_SHAREHOLD / "
                 "RPT_F10_MAIN_BELONGBOARD / RPT_F10_MAIN_TOP10HOLDERS / "
                 "RPT_F10_MAIN_HOLDERHOLD / RPT_F10_MAIN_HOLDERNUM / "
                 "RPT_F10_BASIC_ORGBRIEF / RPT_HSGT_HOLD / RPT_MUTUAL_HOLD_DETAIL / "
                 "RPT_MUTUAL_DEAL_TRADE; "
                 "另 blocked: push2 kamt.rtmin/ulist.np (TLS reset), "
                 "emappdata stockrank/getAllCurrentList (GET 返回 code=-2, 需 POST), "
                 "fund rankhandler.aspx ft=all (ErrCode:-999 无权限), "
                 "data.eastmoney.com dataapi/zlsj/list (返回 []), "
                 "api.fund FundHomePage/GetFinanceDetail (ASP.NET 404), "
                 "FundGuZhi/GetFundGZList (ErrCode:4 路径失效), "
                 "search-api-web search/jsonp type=cmsArticleWebBean (type 名已变更), "
                 "so.eastmoney.com/news/s (空壳 HTML), xinsanban ReadData.aspx (空), "
                 "data.1234567.com.cn api/fund/compareFunds (404), "
                 "np-cnlist-api comm/wap/getListInfo (400); "
                 "**不要重复探测这些**",
    },

    # ============ 深交所 (2026-10-16 实测, 替代已 blocked 的 szse_api) ============
    {
        "id": "szse_report_a_stock",
        "name": "深交所A股列表 (szse api/report, 零鉴权)",
        "url": "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1110&TABKEY=tab1&pageno=1&random={r}",
        "auth": "none (Referer: szse.cn)",
        "status": "degraded",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "agdm(代码)/agjc(简称, 带HTML链接需剥)/bk(板块 主板/创业板/科创板)/"
                 "agssrq(上市日期)/agzgb(A股总股本, 亿股)/agltgb(A股流通股本)/sshymc(所属行业 J金融业)/"
                 "ylbz/sfjybjqcy/gskzjglx",
        "provides": ["meta/szse_stock_list.json"],
        "notes": "2026-10-16 实测 200, 21476B; recordcount=2906, pagecount=146, pagesize=20; "
                 "**翻页参数不生效** — pageno=1/2/3/100 全部返回同样的前 20 条 (平安银行开头), "
                 "实测 4 个页码都一样, 只能拿首页 20 只; 若要全量需另找入口; "
                 "random 参数任意浮点数 (防缓存); TABKEY 只 tab1 有数据, tab2/tab3 全空; "
                 "**agjc 字段含完整 HTML 锚标签** (带 href+u 标签), 需正则剥离; "
                 "sshymc 是证监会行业门类 'J 金融业' 格式 (带字母前缀); "
                 "虽只能拿 20 只, 但这是 registry 里 szse_api(blocked) 的可用替代, "
                 "且板块/上市日期/股本信息比腾讯快照更权威",
    },
    {
        "id": "szse_report_fund_list",
        "name": "深交所基金列表 (1069 只)",
        "url": "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1105&TABKEY=tab1&random={r}",
        "auth": "none (Referer: szse.cn)",
        "status": "degraded",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "sys_key(代码, HTML)/jjjcurl(简称+公告链接, HTML)/jjlb(基金类别 ETF/LOF)/"
                 "tzlb(投资类别 股票基金)/ssrq(上市日期)/dqgm(当前规模)/glrmc(管理人名称)/cxjqhq(存续期限)",
        "provides": ["meta/szse_fund_list.json"],
        "notes": "2026-10-16 实测 200, recordcount=1069; 同样只能拿首页 20 只 (翻页失效); "
                 "**两个字段都带 HTML** (sys_key 和 jjjcurl 都有锚标签+公告链接), 剥离更麻烦; "
                 "但 jjjcurl 的公告 URL 是可用的额外价值 (disclosure/fund/notice/index.html?stock=)",
    },
    {
        "id": "szse_report_etf_list",
        "name": "深交所ETF列表 (756 只, 含净值)",
        "url": "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1945&TABKEY=tab1&random={r}",
        "auth": "none (Referer: szse.cn)",
        "status": "degraded",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "sys_key(代码)/kzjcurl(简称+多链接)/zxjjjz(最新基金净值)/nhzs(年内涨势)/"
                 "dqgm(当前规模)/glrmc(管理人)/cxjqhq(存续期限)",
        "provides": ["meta/szse_etf_list.json"],
        "notes": "2026-10-16 实测 200, recordcount=756; **唯一带 zxjjjz 净值的源**; "
                 "同样翻页失效只能拿 20 只; kzjcurl 含多个锚标签 (行情页+ETF公告+货币公告)",
    },
    {
        "id": "dead_third_batch",
        "name": "第三批失效端点留档 (富途/财联社/淘股吧/开盘啦/问财/股吧/东财补充)",
        "url": "-",
        "auth": "-",
        "status": "blocked",
        "freq": "-",
        "fields": "-",
        "provides": [],
        "notes": "2026-10-16 实测, 全部无效, 不要再探测: "
                 "**富途** futunn.com 5 个 quote-api/news-site-api 路径全返回 404 SPA 页 (10769B); "
                 "**财联社** v1 签名错误(10012), v2 返回 '小财正在加载中'(50101), "
                 "nodeapi/api 路径全 404 — 签名算法已变且新路径未找到; "
                 "**淘股吧** tgb.cn moreHot 返回页脚框架 (94KB, 无热帖数据), api/topic 路径 404; "
                 "**开盘啦** kaipanla.com api/v1/* 返回 Nuxt SPA HTML (8.6KB), 需登录态; "
                 "**问财** iwencai.com unifiedwap/v2 返回 200 但 0 字节, urp/v7 403; "
                 "**东财股吧** guba.eastmoney.com/interface/GetData.aspx 200 但 0 字节; "
                 "**上交所** query.sse.com.cn getStockListData 返回 success:false 需 jsonCallBack 精确匹配; "
                 "**新浪** MoneyFlow.ssl_bestsort 与 Market_Center.getHQNodeData 返回 "
                 "'Service not found' (__ERROR:3); "
                 "**腾讯** rank/hs/getMoneyRankList 与 getLimitUpList 404, "
                 "ifzqgtimg fflow/daykline 返回 'No dispatch info found' (code 11); "
                 "**巨潮** web.cninfo.com.cn DNS 无法解析 (与和讯同样死)",
    },
    {
        "id": "sina_hq_node_count",
        "name": "新浪 A股总数计数 (零鉴权)",
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "A股总数 (纯数字字符串, 无引号)",
        "provides": ["meta/stock_count.json"],
        "notes": "2026-10-16 实测 200, 6B, 返回裸字符串 \"5575\" (不是 JSON, 无引号); "
                 "用来快速确认全市场股票数与雪球 screener 的 count=5000 交叉校验 "
                 "(5575 vs 5000 差 575, 疑为新股/ST/退市整理期口径差, **未核实**)",
    },

    # ============ 财联社 (2026-10-16 签名逆向成功, 替代已 blocked 的 cls_cn) ============
    {
        "id": "cls_telegraph_v1",
        "name": "财联社电报 (v1 签名版, 分类筛选)",
        "url": "https://www.cls.cn/v1/roll/get_roll_list?app=CailianpressWeb&category={cat}&os=web&rn=50&sv=7.7.5&sign={sign}",
        "auth": "sign (md5(sha1(排序参数串)))",
        "status": "ok",
        "freq": "minute",
        "delay": "实时",
        "encoding": "utf-8",
        "method": "GET",
        "fields": "53 字段; 核心: id/ctime/level(A|B|C)/brief/content/subjects(subject_name+plate_id)/"
                 "stock_list(关联股票)/audio_url/shareurl/reading_num/comment_num/share_num",
        "provides": ["news/cls_telegraph.json", "hot_impact 题材映射"],
        "notes": "采集器 scripts/cls_collect.py; 2026-10-16 实测 200, 51909B; "
                 "**签名算法: 所有参数(含空 category)按 key 排序 → k=v& 拼接 → "
                 "sha1().hexdigest() → md5().hexdigest()**; "
                 "**关键坑: category 必须参与签名, 即使为空** — 项目旧 cls_sign.py 漏了它, "
                 "导致一直返回 errno=10012 '签名错误'; 新采集器已修正; "
                 "sv 版本号不敏感 (7.7.5/8.0.0/8.7.9 均可); "
                 "**rn 上限 50** — rn=60/70/80/99/100/150/200 全部返回 0 条; "
                 "**分页参数全无效**: last_time/last_id/ctime/min_time/refresh_type/page/pageNo/p "
                 "实测 5 页翻下来都是同样 50 条 — 只返回最新窗口, 无历史回溯; "
                 "category 白名单 6 个且服务端真过滤: red(40条全B级)/announcement/watch/"
                 "hk_us/fund(38条C级)/remind; "
                 "title 字段恒为空串 (电报正文在 brief/content); "
                 "**subjects 100% 覆盖且带 plate_id** — 可直接映射东财板块, 是 hot_impact 的优质输入; "
                 "stock_list 覆盖率低 (实测 2/50), 不要依赖它做个股关联",
    },
    {
        "id": "cls_nodeapi_dead",
        "name": "财联社 nodeapi 路径 (全 404, 留档)",
        "url": "https://www.cls.cn/nodeapi/{telegraphList,updateTelegraphList}",
        "auth": "none",
        "status": "blocked",
        "freq": "-",
        "fields": "-",
        "provides": [],
        "notes": "2026-10-16 实测: 网上多个开源项目 (sshelll/clstelegraph Go 版等) 用的 "
                 "nodeapi 免签路径现在全 404; v2/roll/get_roll_list 返回 errno=50101 "
                 "'小财正在加载中...'; nodeapi/api/telegraph 路径同样 404; "
                 "结论: 财联社已把免签入口全关, 只剩 v1 签名版可用",
    },
    {
        "id": "cls_article_detail",
        "name": "财联社文章全文 (detail 页 SSR 免签)",
        "url": "https://www.cls.cn/detail/{id}",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "实时",
        "encoding": "utf-8",
        "method": "GET",
        "fields": "column(电报|文章)/isFree/level(A|B|C)/title/brief/content/ctime/"
                 "author/readingNum/commentNum/images/assocVideoUrl/assocArticleUrl/"
                 "associatedFastFact/miniMaxAudioUrl",
        "provides": ["news/cls_articles.json", "hot_impact 题材映射", "ai_review 输入"],
        "notes": "采集器 scripts/cls_article_collect.py; 2026-10-16 实测; "
                 "**Next.js SSR 页, 从 __NEXT_DATA__.props.pageProps.articleDetail 免签取全文**; "
                 "column=电报 → content 短 (平均 200 字符, 与 v1 列表等价); "
                 "column=文章 → **content 长 10~40 倍 (实测 199~8456 字符)**; "
                 "isFree 实测 12/12 全 True, 未见 False — **VIP 墙不在这一层**; "
                 "样本: 2506108 存储芯片业绩预告(8456字) / 2506093 美PPI(3449字) / "
                 "2505833 焦点复盘(2589字) / 2505784 每日收评(3112字); "
                 "一次采集 63 篇文章 / 6 电报 / 0 失败; "
                 "**VIP 内参未破**: category=vip/depth/neican/insider/premium 全部静默降级 "
                 "为空 category (返回同样 50 条, 0 独有, errno=0); /vip /depth 栏目页是 SPA "
                 "无 detail id; 需要登录态; "
                 "**文章列表 API 全 404** (v1/article/list, api/article/list, "
                 "cache?name=articleList, nodeapi/telegraphList, m.cls.cn/* 全 420); "
                 "文章 id 只能反查: 首页 /detail/{id} 链接 / 电报 shareurl / id 邻域扫描; "
                 "**限流: HTTP 420** — 连打 detail 会触发, 需 1.5~2.5s 间隔 + 重试退避",
    },

    # ============ 可转债 + 龙虎榜 (2026-10-16 破甲, 采集器 cb_lhb_collect.py) ============
    {
        "id": "ths_kzz_all",
        "name": "同花顺可转债全量 969 只 (19 字段)",
        "url": "https://data.10jqka.com.cn/ipo/kzz/",
        "auth": "none (Referer: data.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "sub_date(申购日)/bond_code/bond_name/code(正股)/name(正股名)/"
                 "sub_code/share_code/sign_date/plan_total(发行规模亿)/issue_total/"
                 "issue_price/success_rate(中签率)/listing_date/expire_date(到期日)/"
                 "price/quota/number/market_id/stock_market_id",
        "provides": ["cb/kzz_all.json", "新股/可转债申购日历"],
        "notes": "2026-10-16 实测 200, **765KB JSON, 969 只**; "
                 "**注意: data.10jqka.com.cn 域大部分接口已 401 (见 ths_rank_cxfl), "
                 "但 ipo/kzz 是例外, 仍免签可用**; "
                 "listing_date=0 表示未上市; success_rate 是中签率; "
                 "覆盖申购前(计划发行)到已上市全生命周期; "
                 "比集思录的 5 个可转债源更全 (含发行规模/到期日/正股代码)",
    },
    {
        "id": "jisilu_cb_realtime",
        "name": "集思录可转债实时 63 字段 (溢价率/双低)",
        "url": "https://www.jisilu.cn/data/cbnew/cb_list_new/?___jsl=LST&rp={page}",
        "auth": "none (匿名 session + Referer)",
        "status": "ok",
        "freq": "minute",
        "delay": "盘中实时",
        "encoding": "utf-8",
        "fields": "bond_id/bond_nm/price/increase_rt/stock_id/stock_nm/sprice/"
                 "sincrease_rt/pb/convert_price/convert_value/convert_begin/"
                 "premium_rt(溢价率%)/dblow(双低值)/sw_cd(申万行业)/rating(评级)/...",
        "provides": ["cb/realtime.json", "cb 双低策略选债"],
        "notes": "2026-10-16 实测 200, 40796B, 30 只/页; **63 个字段**; "
                 "**premium_rt 溢价率 + dblow 双低值是核心** — 可转债双低策略的输入; "
                 "convert_value 转股价值 + convert_price 转股价; "
                 "翻页参数 rp (不是 page); 单页 30 只, 全市场约需 15 页; "
                 "与 ths_kzz_all 互补: 集思录给估值指标, 同花顺给发行条款",
    },
    {
        "id": "ths_lhb_yyb",
        "name": "同花顺龙虎榜营业部排名 (3 类)",
        "url": "https://data.10jqka.com.cn/ifmarket/lhbyyb/type/{1|2|3}/tab/{btcz|sbcs|zjsl}/field/{xsjs|sbcs|zgczje}/sort/desc/page/{n}/",
        "auth": "none (Referer: data.10jqka.com.cn)",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后",
        "encoding": "gb18030",
        "fields": "排名/营业部名称/上榜次数(或买入成交)/买入成交股票数/买入成功率(%)",
        "provides": ["lhb/yyb_rank.json", "游资跟踪"],
        "notes": "2026-10-16 实测 200, 17797B/页, **翻页生效** (page1/2/5 内容不同); "
                 "每页 20 家, 3 个 type: 1=榜上榜次数(btcz) 2=买入成交(sbcs) 3=总成交金额(zjsl); "
                 "**注意: data.10jqka.com.cn 域大部分接口已 401, 这条是例外仍免签**; "
                 "返回 HTML 表格需解析; 营业部名称含 Unicode 转义 (\\u07b9 等) 需 decode; "
                 "**买入成功率是独家字段** — 识别真游资 vs 对倒的关键; "
                 "registry 此前只有东财龙虎榜个股明细, 无营业部维度",
    },
    {
        "id": "em_lhb_detail",
        "name": "东财龙虎榜明细 39 字段 (含解读)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "收盘后实时",
        "encoding": "utf-8",
        "fields": "TRADE_DATE/SECUCODE/SECURITY_CODE/SECURITY_NAME_ABBR/CLOSE_PRICE/"
                 "CHANGE_RATE/BILLBOARD_DEAL_AMT/BILLBOARD_BUY_AMT/BILLBOARD_SELL_AMT/"
                 "BILLBOARD_NET_AMT/DEAL_AMOUNT_RATIO/DEAL_NET_RATIO/ACCUM_AMOUNT/"
                 "EXPLANATION(上榜原因)/CHANGE_TYPE/MARKET/"
                 "D1/D2/D5/D10_CLOSE_ADJCHRATE(后N日涨跌)/TURNOVERRATE/FREE_MARKET_CAP",
        "provides": ["lhb/detail.json", "lhb 后N日胜率统计"],
        "notes": "2026-10-16 实测 200, 11115B/100条, pages=2695; "
                 "**EXPLANATION 是上榜原因中文解读** (如 '上市首日可转债' '连续三个交易日内, "
                 "涨幅偏离值累计达到12%的ST和*ST证券'); "
                 "**D1/D2/D5/D10_CLOSE_ADJCHRATE 是龙虎榜后 N 日涨跌幅** — 可直接算胜率; "
                 "DEAL_AMOUNT_RATIO 是龙虎榜成交占全天比, DEAL_NET_RATIO 是净买占比; "
                 "比 registry 里旧的 em_datacenter_lhb 字段更全 (那条是 RPT_* 老报表)",
    },
    {
        "id": "em_lhb_dead_reports",
        "name": "东财龙虎榜/两融失效报表名 (留档)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_*",
        "auth": "none",
        "status": "blocked",
        "freq": "-",
        "fields": "-",
        "provides": [],
        "notes": "2026-10-16 实测返回 '报表配置不存在' 或 message 非 null 的报表名: "
                 "RPT_OPERATEDEPT_STATISTICS (营业部统计, 想替代 ths_lhb_yyb 但不可用), "
                 "RPTA_WEB_RZRQ_GGMX (两融明细, message=DIM...), "
                 "RPT_BOND_CB_LIST (东财可转债列表); "
                 "另: push2.eastmoney.com 的 clist 可转债 fs 参数与 kline 均未返回有效数据 (未深究); "
                 "集思录 /data/cbnew/premium/ 404; 同花顺 q.10jqka zillist SPA 页"
                 "**结论: 可转债走 ths_kzz_all + jisilu_cb_realtime; "
                 "龙虎榜营业部走 ths_lhb_yyb; 明细走 em_lhb_detail**",
    },

    # ============ 东财第三批 (2026-10-16, 子代理探测 + 主代理 25/28 复测通过) ============
    # 全部在 datacenter-web.eastmoney.com/api/data/v1/get, 零鉴权
    # 错误码语义 (实测): 9501=排序列/列不存在; 9201=filter合法但无数据; 9701=服务器繁忙(重试)
    {
        "id": "em_kzz_value_hist",
        "name": "东财可转债历史价值分析 (转股/纯债/双溢价率)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_KZZ_LS&columns=ALL&pageSize={n}&pageNumber={p}&filter=(zcode%3D{bondcode})&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "DATE/ZCODE/FCLOSE(收盘价)/SWAPVALUE(转股价值)/PUREBONDVALUE(纯债价值)/"
                 "PUREBONDOR/SWAPOR/SWAPPRICE/USWAPTVOL/USWAPTRATE/SY...",
        "provides": ["cb/value_hist.json", "cb 双低策略回测"],
        "notes": "2026-10-16 实测 200, 1557B, pages=292 (113527 单只); "
                 "**filter 用小写 zcode + 6 位债券代码** (如 113527); "
                 "SWAPVALUE/PUREBONDVALUE 可为 null (早期数据缺失); "
                 "**这是双低策略的直接输入** — registry 只有集思录的实时快照, 无历史序列; "
                 "注意: 老路径 api/data/get?type=RPTA_WEB_KZZ_LS 已失效 (实测 100B 空)",
    },
    {
        "id": "em_kzz_important_date",
        "name": "东财可转债重要日期时间轴",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_CB_IMPORTANTDATE&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECURITY_CODE%3D%22{code}%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "BOND_COMBINE_CODE/SECURITY_CODE/SECURITY_NAME_ABBR/DATE_TYPE_CODE/"
                 "DATE_TYPE(债券承销期/发行结果公告/上市...)/START_DATE/END_DATE/IS_INTERVAL",
        "provides": ["cb/important_date.json", "cb 条款监控"],
        "notes": "2026-10-16 实测 200, 1575B, pages=3 (123116 万兴转债); "
                 "DATE_TYPE 是中文事件名 (债券承销期/网上发行/摇号/上市等); "
                 "覆盖申购→发行→上市全时间轴, 比 ths_kzz_all 的单一 listing_date 细得多",
    },
    {
        "id": "em_kzz_ballot_num",
        "name": "东财可转债中签号",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_CB_BALLOTNUM&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECURITY_CODE%3D%22{code}%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "SECUCODE/SECURITY_CODE/BOND_NAME_ABBR/TYPE_CODE/TYPE(末N位数)/BALLOT_NUM",
        "provides": ["cb/ballot.json", "打新验签"],
        "notes": "2026-10-16 实测 200, 1077B, pages=1; "
                 "BALLOT_NUM 是逗号分隔的中签号码串; TYPE 标注末几位匹配",
    },
    {
        "id": "em_kzz_invest_item",
        "name": "东财可转债募资投入项目进度",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOND_BS_OPRFINVESTITEM&columns=ALL&pageSize={n}&pageNumber={p}&filter=(SECURITY_CODE%3D%22{code}%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "encoding": "utf-8",
        "fields": "ITEM_NUM/ITEM_NAME(项目名)/PLAN_INVEST_AMT(计划投入)/PLAN_INPUT_RF/"
                 "YIELD(收益率)/INVEST_RE...(已投进度)",
        "provides": ["cb/invest_item.json", "募投项目追踪"],
        "notes": "2026-10-16 实测 200, 7150B; 单位万元; "
                 "PLAN_INVEST_AMT vs 已投进度可判断项目落地情况",
    },
    {
        "id": "em_ipo_apply_yield",
        "name": "东财打新收益全表 (中签率/首日涨幅/连板)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_APP_IPOAPPLY&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=APPLY_DATE,SECURITY_CODE&sortTypes=-1,-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "SECUCODE/SECURITY_CODE/APPLY_CODE/APPLY_DATE/BALLOT_PAY_DATE/"
                 "LISTING_DATE/TRADE_MARKET/MARKET_TYPE_NEW/ASSIGN...(中签率/认购倍数/顶格资金/首日涨幅)",
        "provides": ["ipo/apply_yield.json", "打新策略回测"],
        "notes": "2026-10-16 实测 200, 20225B/100条, pages=57~1128; "
                 "**一表给全打新收益闭环**: 中签率/超额认购倍数/顶格资金/首日涨幅/开盘溢价/"
                 "连板数/发行PE vs 行业PE; "
                 "**采集坑: 连续批量请求中恒返回空 (n=0, pages=None), 单独跑 6/6 全通** "
                 "— 服务端当日数据缓存不预热, 重试 5 次仍空; 必须单独一个 step 采; "
                 "坑 (子代理实测): APPLY_DATE/LISTING_DATE **不可 filter**, 只能用 UPDATE_DATE/ORG_CODE 排序; "
                 "回溯分页用 lastmark 翻页更稳",
        "status": "degraded",
    },
    {
        "id": "em_ipo_neeq_list",
        "name": "东财北交所新股发行 (每百股获利)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_NEEQ_ISSUEINFO_LIST&columns=ALL&pageSize={n}&pageNumber={p}&source=NEEQSELECT&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "ORG_CODE/SECURITY_CODE/SECURITY_NAME_ABBR/APPLY_CODE/EXPECT_ISSUE_NUM/"
                 "PRICE_WAY/ISSUE_PRICE/ISSUE_PE_RATIO/APPLY_DATE + "
                 "每百股获利/上市首日年化收益/稳获百股需配资金",
        "provides": ["ipo/neeq_list.json", "北交所打新"],
        "notes": "2026-10-16 实测 200, 5859B, pages=72; "
                 "**source=NEEQSELECT (不是 WEB)**; "
                 "**每百股获利 + 上市首日年化收益是北交所打新的独家字段**; "
                 "SECUCODE 带 .BJ 后缀",
    },
    {
        "id": "em_ipo_review",
        "name": "东财 IPO 审核进度 (881 家)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_IPO_INFOALLNEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=UPDATE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "STATE(上市委会议通过等)/REG_ADDRESS/INFO_CODE/CSRC_INDUSTRY/"
                 "ACCEPT_DATE/DECLARE_ORG/PREDICT_LISTING_MARKET/LAW_FIRM",
        "provides": ["ipo/review.json", "新股储备跟踪"],
        "notes": "2026-10-16 实测 200, 2824B, pages=881; "
                 "STATE 是审核阶段中文名; PREDICT_LISTING_MARKET 是拟上市板块; "
                 "同族 RPT_IPO_AUDIT (6142 条) / RPT_IPO_REGISTER (884 条) 未复测",
    },
    {
        "id": "em_ipo_allotment",
        "name": "东财配股 (已实施 197 只 + 预案)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_IPO_ALLOTMENT&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=EQUITY_RECORD_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "FINANCE_CODE/SECUCODE/CORRECODE/CORRECODE_NAME_ABBR/PLACING_RATIO/"
                 "ISSUE_PRICE/TOTAL_SHARES/...(配股比例/价格/股权登记日)",
        "provides": ["ipo/allotment.json", "配股套利监控"],
        "notes": "2026-10-16 实测 200, 3434B, pages=197; "
                 "PLACING_RATIO 是配股比例 (如 4 = 10配4); CORRECODE 是配股代码",
    },
    {
        "id": "em_seo_detail",
        "name": "东财增发实施明细 (1183 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_SEO_DETAIL&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=ISSUE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "SECUCODE/TRADE_MARKET/CORRECODE/ISSUE_NUM/ISSUE_WAY/ISSUE_PRICE/"
                 "RAISE_FUNDS/...(增发价/募资额/发行方式/上市日)",
        "provides": ["events/seo_detail.json", "再融资追踪"],
        "notes": "2026-10-16 实测 200, 9385B, pages=1183; "
                 "ISSUE_WAY 是发行方式 (网下定价发行/定向增发等); "
                 "覆盖 1990s 至今全史; 同族 RPT_SEO_REGISTER / RPT_SEO_FOREHELD 未复测",
    },
    {
        "id": "ths_ipo_xgsr_html",
        "name": "同花顺新股上市 (HTML, 含是否破发)",
        "url": "https://data.10jqka.com.cn/ipo/xgsr/",
        "auth": "none (Referer: data.10jqka.com.cn)",
        "status": "degraded",
        "freq": "daily",
        "encoding": "gbk",
        "fields": "新股上市列表 (含是否破发字段)",
        "provides": ["ipo/xgsr.json"],
        "notes": "2026-10-16 实测 200, 84519B; "
                 "**返回 HTML 需解析** (不是 JSON) 所以标 degraded; "
                 "**注意: 同路径的 ajax 接口 /ipo/xgsr/field/SSRQ/... 实测 401** "
                 "(见 ths_rank_cxfl), 只有 HTML 页可用; "
                 "**是否破发字段是独家** — em_ipo_apply_yield 没有; "
                 "用途: 与 em_ipo_apply_yield 交叉校验打新收益",
    },
    {
        "id": "em_lhb_yyb_statistics",
        "name": "东财龙虎榜营业部统计 (4 周期)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_OPERATEDEPT_LIST_STATISTICS&columns=ALL&pageSize={n}&pageNumber={p}&filter=(STATISTICSCYCLE%3D%2204%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "OPERATEDEPT_CODE/OPERATEDEPT_NAME/STATISTICSCYCLE/AMOUNT/"
                 "SALES_ONLIST_TIMES/ACT_BUY/TOTAL_BUYER_SALESTIMES/ACT_SELL/...",
        "provides": ["lhb/yyb_statistics.json", "游资席位追踪"],
        "notes": "2026-10-16 实测 200, 1985B, pages=933; "
                 "STATISTICSCYCLE: 01=当日 02=5日 03=10日 **04=30日**; "
                 "与 em_lhb_yyb_return_ranking 配合可算营业部胜率; "
                 "同族 RPT_OPERATEDEPT_LIST_STATISTICS_RATIO 未复测",
    },
    {
        "id": "em_lhb_yyb_trade_detail",
        "name": "东财营业部逐笔交易明细 (含榜后N日涨跌)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_OPERATEDEPT_TRADE_DETAILSNEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=TRADE_DATE,SECURITY_CODE&sortTypes=-1,-1&filter=(OPERATEDEPT_CODE%3D%22{code}%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "OPERATEDEPT_CODE/OPERATEDEPT_NAME/TRADE_DATE/D1/D2/D3/D5_CLOSE_ADJCHRATE/"
                 "...(买入额/卖出额/净额 + 榜后1/2/3/5日涨跌)",
        "provides": ["lhb/yyb_trade.json", "游资胜率计算"],
        "notes": "2026-10-16 实测 200, 3576B, pages=44 (单营业部); "
                 "**D1/D2/D3/D5_CLOSE_ADJCHRATE 直接给榜后涨跌**, 是营业部胜率的原始输入; "
                 "坑 (子代理实测): sortColumns 必须是 **TRADE_DATE,SECURITY_CODE** "
                 "(写 ONLIST_DATE 会 9501 排序列不存在)",
    },
    {
        "id": "em_lhb_yyb_return_ranking",
        "name": "东财营业部收益率排行榜 (胜率)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_RATEDEPT_RETURNT_RANKING&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=TOTAL_BUYER_SALESTIMES_1DAY,OPERATEDEPT_CODE&sortTypes=-1,-1&filter=(STATISTICSCYCLE%3D%2204%22)&source=WEB&client=WEB",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "OPERATEDEPT_CODE/OPERATEDEPT_NAME/STATISTICSCYCLE/"
                 "AVERAGE_INCREASE_1DAY/RISE_PROBABILITY_1DAY/TOTAL_BUYER_SALESTIMES_1DAY/"
                 "AVERAGE_INCREASE_2DAY/...(各周期均涨幅+上涨概率+上榜次数)",
        "provides": ["lhb/yyb_return.json", "游资胜率榜"],
        "notes": "2026-10-16 实测 200, 3728B, pages=931; "
                 "**RISE_PROBABILITY_1DAY 是 1 日上涨概率 (%)** — 识别真游资的核心指标; "
                 "坑 (子代理实测): sortColumns 必须 TOTAL_BUYER_SALESTIMES_1DAY,OPERATEDEPT_CODE; "
                 "与同花顺 ths_lhb_yyb 形成两个独立源的交叉校验",
    },
    {
        "id": "em_lhb_org_daily",
        "name": "东财机构龙虎榜每日明细",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ORGANIZATION_TRADE_DETAILS&columns=ALL&pageSize={n}&pageNumber={p}&filter=(TRADE_DATE%3E%3D%27{start}%27)(TRADE_DATE%3C%3D%27{end}%27)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "SECUCODE/TRADE_DATE/CLOSE_PRICE/CHANGE_RATE/BUY_TIMES/SELL_TIMES/"
                 "BUY_AMT/SELL_AMT/NET_BUY_AMT/...(机构买卖次数与金额)",
        "provides": ["lhb/org_daily.json", "机构席位监控"],
        "notes": "2026-10-16 实测 200, 3214B, pages=19 (单日); "
                 "BUY_TIMES/SELL_TIMES 是机构上榜次数, 可判断机构一致性; "
                 "注意 BUY_TIMES=2 SELL_TIMES=3 表示有分歧",
    },
    {
        "id": "em_margin_account_daily",
        "name": "东财两融账户与余额日统计 (682 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_MARGIN_DAILYTRADE&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=STATISTICS_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "STATISTICS_DATE/DATE_MARK/FIN_BALANCE(融资余额亿)/LOAN_BALANCE(融券)/"
                 "MARGIN_BALANCE(两融余额)/BALANCE_RATIO/FIN_BUY_AMT/LOAN_SELL_AMT",
        "provides": ["market/margin_account.json", "杠杆情绪"],
        "notes": "2026-10-16 实测 200, 3298B, pages=682; "
                 "坑 (子代理实测): sortColumns 必须 **STATISTICS_DATE** "
                 "(写 TRADE_DATE 会 9501); "
                 "BALANCE_RATIO 是融资余额占比 (2.62% = 杠杆水平); "
                 "比 registry 的 em_datacenter_margin (个股) 更宏观",
    },
    {
        "id": "em_equity_pledge_market",
        "name": "东财股权质押市场统计 (129 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_CSDC_STATISTICS&columns=ALL&pageSize={n}&pageNumber={p}&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "TRADE_DATE/TOTAL_PLEDGED_SHARES/PLEDGE_MARKET_VALUE/CSI_300_INDEX/"
                 "CSI_300_CHG/PM_RATIO(质押比例)/PLEDGE_CO_NUM",
        "provides": ["market/equity_pledge.json", "系统性风险监控"],
        "notes": "2026-10-16 实测 200, 1287B, pages=129; "
                 "**PM_RATIO 是 A股整体质押比例** — 系统性风险指标; "
                 "同时给沪深300点位与涨跌, 可算质押市值随指数变化",
    },
    {
        "id": "em_equity_pledge_detail",
        "name": "东财股权质押明细 (25397 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_APP_ACCUMDETAILS&columns=ALL&pageSize={n}&pageNumber={p}&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "SECURITY_NAME_ABBR/HOLDER_NAME/IS_CONTROL_SHAREHOLDER/"
                 "ACCUM_PLEDGE_TSR(累计质押占总股本%)/PRE_ACCUM_PLEDGE_TSR/NOTICE_DATE",
        "provides": ["events/equity_pledge.json", "高质押排雷"],
        "notes": "2026-10-16 实测 200, 6231B, pages=25397; "
                 "**IS_CONTROL_SHAREHOLDER=1 是控股股东质押** (风险最高); "
                 "ACCUM_PLEDGE_TSR 是累计质押比例 — >70% 是高危信号",
    },
    {
        "id": "em_stock_participation",
        "name": "东财市场参与意愿 (5294 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_STOCK_PARTICIPATION&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "delay": "日更",
        "encoding": "utf-8",
        "fields": "SECURITY_INNER_CODE/SECURITY_CODE/TRADE_DATE/PARTICIPATION_WISH/"
                 "PARTICIPATION_WISH_5DAYS/PARTICIPATION_WISH_CHANGE/PARTICIPATION_WISH_5DAYSCHANGE",
        "provides": ["sentiment/participation.json", "个股情绪"],
        "notes": "2026-10-16 实测 200, 1253B, pages=5294; "
                 "**PARTICIPATION_WISH 是市场参与意愿 0-100** — 低成本另类情绪指标; "
                 "单请求无鉴权, 接入成本极低; registry 无同类",
    },
    {
        "id": "em_dmsk_ts_stock_new",
        "name": "东财主力成本统计 (20/60 日均价 + 关注度)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_TS_STOCKNEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "SECURITY_CODE/TRADE_DATE/SUPERDEAL_INFLOW/SUPERDEAL_OUTFLOW/"
                 "PRIME_INFLOW/CL...(主力20/60日均价 + FOCUS 关注度)",
        "provides": ["money/dmsk_cost.json", "主力成本/支撑位"],
        "notes": "2026-10-16 实测 200, 4111B, pages=1040; "
                 "**主力 20/60 日均价是支撑/压力位判断的关键**; "
                 "FOCUS 关注度可与雪球 followers 交叉校验; registry 无同类",
    },
    {
        "id": "em_macro_shibor",
        "name": "东财 Shibor 利率 (38776 页)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_IMP_INTRESTRATEN&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "REPORT_DATE/REPORT_PERIOD(3M/1Y等)/IR_RATE/CHANGE_RATE/INDICATOR_ID/"
                 "MARKET(上海银行同业拆借市场)/CURRENCY(Shibor人民币)",
        "provides": ["macro/shibor.json", "无风险利率基准"],
        "notes": "2026-10-16 实测 200, 1526B, pages=38776; "
                 "REPORT_PERIOD 区分各期限; IR_RATE 是年化利率%; "
                 "**DCF 估值的无风险利率输入**; 比 akshare 的宏观封装更直接",
    },
    {
        "id": "em_macro_house_price_70",
        "name": "东财 70 城房价指数 (2632 页, 2011 起)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_HOUSE_PRICE&columns=ALL&pageSize={n}&pageNumber={p}&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "monthly",
        "encoding": "utf-8",
        "fields": "REPORT_DATE/CITY/FIRST_COMHOUSE_SAME/SECOND_HOUSE_SAME/"
                 "FIRST_COMHOUSE_SEQUENTIAL/SECOND_HOUSE_SEQUENTIAL/FIRST_COMHOUSE_BASE/...",
        "provides": ["macro/house_price.json", "地产链监控"],
        "notes": "2026-10-16 实测 200, 1840B, pages=2632; "
                 "按城市分 (韶关等三线也有), SAME=同比 SEQUENTIAL=环比 BASE=定基; "
                 "2011-01 起 15 年历史; **地产链股票的领先指标**",
    },
    {
        "id": "em_performance_express",
        "name": "东财业绩快报",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_FCI_PERFORMANCEE&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "encoding": "utf-8",
        "fields": "业绩快报 (NOTICE_DATE 排序)",
        "provides": ["events/performance_express.json", "财报季跟踪"],
        "notes": "2026-10-16 实测 200, 3954B; "
                 "与 registry 的 em_lcpd_report 互补 (那条是 RPT_LICO_FN_CPD)",
    },
    {
        "id": "em_performance_forecast",
        "name": "东财业绩预告 (8521B)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_PUBLIC_OP_NEWPREDICT&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "quarterly",
        "encoding": "utf-8",
        "fields": "业绩预告 (预告净利润上下限/同比/原因)",
        "provides": ["events/performance_forecast.json", "预增选股"],
        "notes": "2026-10-16 实测 200, 8521B; "
                 "**业绩预告是财报季最关键的先行指标**; "
                 "坑 (子代理实测): 不可用 SECURITY_CODE 筛选, 只能用 NOTICE_DATE 排序后本地过滤",
    },
    {
        "id": "em_restricted_unlock_day",
        "name": "东财解禁时间表 (按日)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LIFTDAY_STA&columns=ALL&pageSize={n}&pageNumber={p}&filter=(FREE_DATE%3E%3D%27{start}%27)(FREE_DATE%3C%3D%27{end}%27)&source=WEB&client=WEB",
        "auth": "none",
        "status": "ok",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "解禁明细 (FREE_DATE 区间过滤)",
        "provides": ["events/unlock_day.json", "解禁抛压监控"],
        "notes": "2026-10-16 实测 200, 1440B (2026-10-16~10-23 一周); "
                 "**filter 用 FREE_DATE 区间**; "
                 "按周监控解禁抛压; 与 THS F10 equity 的解禁表互补",
    },
    {
        "id": "em_repurchase_list",
        "name": "东财回购增持统计 (9939B)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_GETHGLIST_NEW&columns=ALL&pageSize={n}&pageNumber={p}&sortColumns=UPD,DIM_DATE,DIM_SCODE&sortTypes=-1,-1,-1&source=WEB",
        "auth": "none",
        "status": "degraded",
        "freq": "daily",
        "encoding": "utf-8",
        "fields": "回购/增持统计 (UPD/DIM_DATE/DIM_SCODE 排序)",
        "provides": ["events/repurchase.json", "回购增持监控"],
        "notes": "2026-10-16 实测 200, 9939B; "
                 "坑 (子代理实测): sortColumns 必须 **UPD,DIM_DATE,DIM_SCODE** 且 **source=WEB**; "
                 "与 THS F10 bonus 的分红表互补",
    },
    {
        "id": "em_rev_d_dead_reports",
        "name": "第三批复测失效报表名 (留档)",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_*",
        "auth": "none",
        "status": "blocked",
        "freq": "-",
        "fields": "-",
        "provides": [],
        "notes": "2026-10-16 复测 3 个失效: "
                 "RPT_BOND_CB_LIST (东财可转债列表, 100B 空 — 改用 ths_kzz_all), "
                 "api/data/get?type=RPTA_WEB_KZZ_LS (老路径已废, 100B — 改用 v1/get), "
                 "RPTA_OPERATEDEPT_ACTIVE (活跃营业部, 116B 空); "
                 "另子代理报 9501 排序列不存在的报表 (写错 sortColumns 所致, 非接口失效): "
                 "RPTA_WEB_MARGIN_DAILYTRADE(应 STATISTICS_DATE) / "
                 "RPT_OPERATEDEPT_TRADE_DETAILSNEW(应 TRADE_DATE,SECURITY_CODE) / "
                 "RPT_RATEDEPT_RETURNT_RANKING(应 TOTAL_BUYER_SALESTIMES_1DAY,OPERATEDEPT_CODE) / "
                 "RPTA_APP_IPOAPPLY(APPLY_DATE 不可 filter) / "
                 "RPT_EXECUTIVE_HOLD_DETAILS(应 CHANGE_DATE,SECURITY_CODE,PERSON_NAME) / "
                 "RPTA_WEB_ZDHT_LIST(应 DIM_RDATE) / "
                 "RPT_MUTUAL_HOLD_DET(应 HOLD_DATE)",
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
    return [s for s in SOURCES if s.get("status") in ("ok", "degraded")]


def by_status(status):
    # 2026-10-19: 并非所有源字典都有 status 字段 (新增源容易漏), 直接
    # s["status"] 会在云端抛 KeyError: 'status', 导致 collect_all_sources
    # 在收尾统计时崩溃 —— 所有采集都成功了但文件写不出来, 引擎因此
    # 永远报「all_sources 未找到」。用 .get 防空键。
    return [s for s in SOURCES if s.get("status") == status]


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
            # 2026-10-17 逆向调研新源 (docs/research/platforms-1.md):
            # 全部零鉴权、实测 200, 与已有源零重叠
            "jrj_yaowen_daily", "aigupiao_express",
            "sseinfo_qa", "sseinfo_survey", "stockstar_forecast",
            "vango_articles",
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
            "ths_kline_day", "ths_kline_month", "ths_timeshare",
            "ths_finance_benefit", "ths_finance_debt", "ths_finance_cash",
            "ths_f10_worth", "ths_f10_holder", "ths_f10_bonus",
            "ths_f10_operate", "ths_f10_event", "ths_f10_capital",
            "ths_f10_field", "ths_f10_equity", "ths_f10_news",
            "ths_board_kline", "ths_board_constituents", "ths_news_push",
            "ths_rank_cxfl", "ths_funds_flow_rank",
            "xueqiu_screener", "xueqiu_screener_dead_paths",
            "tencent_board_rank", "tencent_newfqkline", "tencent_hk_kline",
            "tencent_us_kline", "tencent_index_weektrends",
            "sina_futures_realtime", "sina_moneyflow_zjlrqs", "sina_moneyflow_lscjfb",
            "sina_esg_stocks", "sina_esg_msci", "sina_esg_zd",
            "sina_esg_rft", "sina_esg_hz",
            "sina_spot_goods", "sina_option_stock_name",
            "sina_hk_klc_js", "sina_us_index_static",
            "hexun_dead_all",
            "em_org_survey", "em_shareholder_increase", "em_holder_num_latest",
            "em_org_hold_allmarket", "em_f10_freeholders", "em_f10_orghold_history",
            "em_f10_coretheme_board", "em_f10_mainop", "em_reportapi_stock_report",
            "em_hsf10_shareholder_research", "em_hsf10_company_survey",
            "em_hsf10_capital_structure", "em_hsf10_bonus_financing",
            "em_hsf10_business_analysis", "em_hsf10_operations_required",
            "em_datacenter_dead_reportnames",
            "szse_report_a_stock", "szse_report_fund_list", "szse_report_etf_list",
            "dead_third_batch", "sina_hq_node_count",
            "cls_telegraph_v1", "cls_nodeapi_dead", "cls_article_detail",
            "ths_kzz_all", "jisilu_cb_realtime", "ths_lhb_yyb",
            "em_lhb_detail", "em_lhb_dead_reports",
            "em_kzz_value_hist", "em_kzz_important_date", "em_kzz_ballot_num",
            "em_kzz_invest_item", "em_ipo_apply_yield", "em_ipo_neeq_list",
            "em_ipo_review", "em_ipo_allotment", "em_seo_detail",
            "ths_ipo_xgsr_html", "em_lhb_yyb_statistics", "em_lhb_yyb_trade_detail",
            "em_lhb_yyb_return_ranking", "em_lhb_org_daily",
            "em_margin_account_daily", "em_equity_pledge_market",
            "em_equity_pledge_detail", "em_stock_participation",
            "em_dmsk_ts_stock_new", "em_macro_shibor", "em_macro_house_price_70",
            "em_performance_express", "em_performance_forecast",
            "em_restricted_unlock_day", "em_repurchase_list",
            "em_rev_d_dead_reports",
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
            if s["id"] not in assigned and s.get("status") in ("ok", "degraded")]


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
        if s.get("status") == "ok":
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
                "unauth": "🔑", "untested": "❔"}.get(s.get("status"), "?")
        print(f"{s['id']:26s} {icon}{s['status']:8s} {s['freq']:9s} {s['name']}")
    print()
    from collections import Counter
    c = Counter(s.get("status") for s in SOURCES)
    print("统计:", dict(c), f"共 {len(SOURCES)} 个源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
