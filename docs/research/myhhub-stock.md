# myhhub/stock (InStock) 源码研究报告

代码位置：`/tmp/repos/stock-master`，核心包 `instock/`（core=crawler/strategy/indicator/backtest, job=每日任务, trade=easytrader实盘, web=Tornado看板）。技术栈：Python + TA-Lib + akshare式自研爬虫 + MySQL(原生SQL建表) + Tornado。

## 一、AI决策引擎架构与策略逻辑

### 架构（pipeline: execute_daily_job.py 串起各 job）
1. `basic_data_daily_job` — 全市场实时快照（东财 clist 分页爬取，过滤 `is_a_stock`(60/00/30开头) + 非ST + 有价格）
2. `indicators_data_daily_job` — TA-Lib 批量算指标（MACD/KDJ/BOLL/TRIX/RSI/CCI/CR/WR/VR 等，`core/indicator/calculate_indicator.py`），再用 SQL 阈值筛出 guess_buy / guess_sell 两张表
3. `strategy_data_daily_job` — 10个形态策略并发扫描（ThreadPoolExecutor 40 workers），命中写库
4. `backtest_data_daily_job` — 对历史信号做101日区间收益回填（`core/backtest/rate_stats.py`：从信号日算累计涨幅序列 sum_pct_change）
5. `klinepattern_data_daily_job` — TA-Lib CDL* K线形态识别
6. 闭盘后 job：资金流/龙虎榜/大宗/分红/涨停原因

### 策略因子清单（core/strategy/，规则全部硬编码，可直抄）
| 策略 | 规则要点 |
|---|---|
| 放量上涨 enter | 当日涨幅>2%且收阳；成交额≥2亿；量/5日均量≥2 |
| 均线多头 keep_increasing | MA30: 30日前 < 20日前 < 10日前 < 当日 |
| 停机坪 parking_apron | 近15日有>9.5%放量上涨，次日高开且收涨、收盘距开盘<3% |
| 回踩年线 backtrace_ma250 | 前段自年线下突破，后段年线上运行，最高价日与低点日相距10-50日，缩量回踩(高点量/低点量>2，低点价/高点价<0.8) |
| 突破平台 breakthrough_platform | 60日内某日收盘≥MA60>开盘 + 放量上涨，此前每日收盘与MA60偏离在-5%~20% |
| 无大幅回撤 low_backtrace_increase | 60日涨幅>0.6；期间无单日跌7%/高开低走7%/两日累计10%（允许一次洗盘） |
| 海龟 turtle_trade | 收盘价≥近60日最高收盘价（纯突破，无ATR仓位模块） |
| 高而窄的旗形 high_tight_flag | 收盘/近24~10日最低≥1.9 + 近期连续两日涨幅≥9.5%；叠加龙虎榜机构参与过滤（近90日买方机构数>1） |
| 放量跌停 climax_limitdown | 跌>9.5% + 成交额≥2亿 + 量为5日均量4倍 |
| 低ATR成长 low_atr | 上市≥250日，近10日最高收盘/最低收盘≥1.1 |

### 指标买卖筛选（indicators_data_daily_job.py，SQL阈值，反向指标风格）
- **guess_buy**：`kdjk>=80 AND kdjd>=70 AND kdjj>=100 AND rsi_6>=80 AND cci>=100 AND cr>=300 AND wr_6>=-20 AND vr>=160`（注意：命名的 buy 实际是超买极端——作者用"强势极值追涨"思路，回测表验证收益）
- **guess_sell**：全指标超卖极端（kdjk<20, rsi<20, cci<-100…）
- **基本面选股**（basic_data_other_daily_job.py）：`0 < PE9 <= 20 AND PB(mrq) <= 10 AND 加权ROE >= 15` → cn_stock_spot_buy

### 特色：筹码分布 CYQ（core/kline/cyq.py）
纯Python实现（accuracy_factor=150价格档，range=120日，cyq_days=210日），三角分布换手衰减算法，从日K自算筹码分布/获利盘比例——**我们没有筹码数据源，这段可直接移植**。

## 二、数据源接口清单（可直抄URL）

### 东方财富 push2 行情族（ut token 通用）
```
# 全市场A股实时快照（分页，pz=50，pn页码；fs含沪深主板+创业板+北交所）
GET http://82.push2.eastmoney.com/api/qt/clist/get
  pn=1&pz=50&po=1&np=1&fltt=2&invt=2&fid=f12
  ut=bd1d9ddb04089700cf9c27f6f7426281
  fs=m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048
  fields=f2,f3,f4,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f37,f39,f40,f45,f62,f100,f112,f113,f115,f221
  # f20总市值 f21流通市值 f9市盈动 f23市净率 f24 60日涨幅 f37 ROE 等

# 历史日K（不复权fqt=0/前复权1/后复权2；klt 101日/102周/103月）
GET http://push2his.eastmoney.com/api/qt/stock/kline/get
  fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116
  ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=1
  secid=1.600519   # 1=沪 0=深，按代码6开头判断
  beg=20200101&end=20500000

# 分时 trends2
GET https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=0.000001&klt=1&fields1=...&fields2=...
```

### 东方财富资金流（★我们 qt 端点之外的补充）
```
# 个股资金流排名（今日/5日/10日，f62=主力净流入）
GET http://push2.eastmoney.com/api/qt/clist/get
  fid=f62&fs=m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2
  ut=b2884a393a59ad64002292a3e90d46a5
  fields=f12,f14,f2,f109,f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f257,f258,f124

# 板块资金流排名（行业fs按 f12~f14 映射 sector_type: 行业2/概念3/地域1）
GET http://push2.eastmoney.com/api/qt/clist/get
  fid=f62&po=1&pz=100&fs=m:90+t:2+f:!2    # 行业；t:3概念 t:1地域
  fields=f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124

# 个股历史每日资金流K线（f51~f65主力/超大/大/中/小单）
GET https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get
  lmt=0&klt=1&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65
  ut=b2884a393a59ad64002292a3e90d46a5&secid=0.000001
```

### 东财 datacenter（我们已有 lhb/大宗/两融，这里补 reportName 全集）
```
GET https://datacenter-web.eastmoney.com/api/data/v1/get
  source=WEB&client=WEB&sortColumns=...&sortTypes=-1&pageSize=500&pageNumber=1&columns=ALL
  filter=(TRADE_DATE<='YYYY-MM-DD')(TRADE_DATE>='YYYY-MM-DD')

reportName 可用值（crawling/stock_lhb_em.py、stock_dzjy_em.py、stock_fhps_em.py）：
  RPT_DAILYBILLBOARD_DETAILSNEW          龙虎榜每日明细
  RPT_BILLBOARD_TRADEALL                 龙虎榜个股上榜统计
  RPT_ORGANIZATION_TRADE_DETAILS         机构买卖明细
  RPT_ORGANIZATION_SEATNEW               机构席位追踪
  RPT_OPERATEDEPT_ACTIVE                 活跃营业部
  RPT_RATEDEPT_RETURNT_RANKING           营业部回报排名
  RPT_LHB_BOARDDATE                      个股上榜日
  PRT_BLOCKTRADE_MARKET_STA              大宗每日市场统计
  RPT_DATA_BLOCKTRADE                    大宗每日明细
  RPT_BLOCKTRADE_STA / RPT_BLOCKTRADE_ACSTA  大宗行业/营业部统计
  RPT_BLOCKTRADE_OPERATEDEPTSTATISTICS / _RANK
  RPT_SHAREBONUS_DET                     分红配送 filter=(REPORT_DATE='YYYY-MM-DD')
```

### 东财选股器（★我们没有）
```
GET https://data.eastmoney.com/dataapi/xuangu/list
  sty=<指标字段>&p=1&ps=500&source=SELECT_SECURITIES&client=WEB
  filter=(MARKET+in+("上交所主板","深交所主板","深交所创业板"))(NEW_PRICE>0)
  # 全指标字典：
GET https://datacenter-web.eastmoney.com/wstock/selection/api/data/get
  type=RPTA_PCNEW_WHOLE&sty=ALL&p=1&ps=1000&source=SELECT_SECURITIES&client=WEB
```

### ★同花顺涨停原因（我们没有）
```
GET http://zx.10jqka.com.cn/event/api/getharden/date/2026-10-13/orderby/date/orderway/desc/charset/GBK/
  UA: Chrome；返回JSON data[]：涨停代码/名称/涨停日期/原因分类/原因描述/题材板块
```

### ★通达信竞价抢筹（早盘/尾盘，我们没有）
```
POST http://excalc.icfqs.com:7616/TQLEX?Entry=HQServ.hq_nlp
  Content-Type: application/json; UA含 TdxW
  body: [{"funcId":20,"offset":0,"count":100,"sort":1,"period":0,
          "Token":"6679f5cadca97d68245a086793fc1bfc0a50b487487c812f","modname":"JJQC"}]
  # period 0=早盘抢筹 1=尾盘抢筹；历史加 "date":"YYYYMMDD"
  # sort: 1抢筹委托金额 2抢筹成交金额 3开盘金额 4抢筹幅度 5抢筹占比
```

### 其他
```
# 交易日历（新浪）
GET https://finance.sina.com.cn/realstock/company/klc_td_sh.txt
# 新浪龙虎榜个股上榜统计(HTML表格翻页)
GET https://vip.stock.finance.sina.com.cn/q/go.php/vLHBData/kind/ggtj/index.phtml?last=10&p=1
# 东财F10操盘必读(基本面主要指标)
GET https://emweb.securities.eastmoney.com/PC_HSF10/OperationsRequired/PageAjax?code=SH600519
```

### 工程细节（直接借鉴）
- `eastmoney_fetcher.py`：Cookie三级管理(环境变量→文件→默认) + requests.Session + Retry(3次, backoff, 429/5xx) + 连接池50 + 固定 Referer `quote.eastmoney.com`。东财反爬主要看 Cookie(含 st_* 埋点字段)，可用 session 先访问一次行情页种 Cookie。
- 分页爬取固定 `time.sleep(random.uniform(1,1.5))` 防限流。

## 三、选股/回测/推演可借鉴点

1. **信号表→回测列回填模式**：信号表建表时就带 N 列未来收益回填位（`TABLE_CN_STOCK_BACKTEST_DATA`，101日），每日 job 扫 `尾列为NULL` 的信号行增量回填，永不重算历史。比我们的按区间重跑省资源，值得抄进 DuckDB 管道。
2. **"策略函数签名统一" `(code_name, data, date) -> bool`**：所有策略同签名、ThreadPool 40 并发扫全市场、以函数对象注册在 TABLE_CN_STOCK_STRATEGIES 元数据里（含表名/中文名/func），加一个策略=加一个文件+一行注册。我们 factor_lib 可采用同款注册表模式。
3. **形态策略的硬阈值可直接当我们的过滤器**：放量确认（额≥2亿+量比≥2）、突破平台偏离带（-5%~20%）、缩量回踩（量比>2+回撤<20%）——与我们五因子/29因子互补，特别是"停机坪""高而窄的旗形"这类短线打板形态是纯统计体系里少有的。
4. **筹码分布 CYQ 纯Python实现**（cyq.py ~300行，三角分布+换手衰减），可作为我们的新因子（获利盘比例、90%筹码集中度、平均成本与现价乖离）。
5. **回测表结构**：`sum_pct_change` 从信号日起算的逐日累计涨幅序列存列，回看时直接绘图对比多策略胜率。

## 四、复盘/胜率优化机制

- **没有胜率自适应/反馈闭环**：backtest 只做"信号→未来101日累计收益"的被动记录 + Web端人工看表对比，无策略淘汰/加权机制。这正是我们 ADAPT 胜率自适应的差异化优势。
- 可借鉴的最小闭环：每日 `backtest_data_daily_job` 增量回填 + `rate_stats.get_rates` 归一化累计收益列；建议在其上加我们已有的：按策略统计 N日胜率/盈亏比 → 低于阈值降权或停用。
- 值得抄的复盘数据面：涨停原因(题材归因) + 竞价抢筹(情绪前置指标) + 龙虎榜机构过滤(90日买方机构数>1 才允许追板) 三个辅助信号组合进每日复盘。

## 结论（与我们体系对照）
| 方面 | InStock | 我们 | 行动建议 |
|---|---|---|---|
| 数据源 | 东财push2全家桶+datacenter+THS涨停原因+TDX抢筹 | qt/sina/东财datacenter | 补：THS涨停原因、TDX竞价抢筹、东财板块资金流、CYQ筹码自算 |
| 策略 | 10形态策略硬规则 | 五因子/29因子/周期v2 | 抄"停机坪/旗形/回踩年线"规则作短线补充因子 |
| 回测 | 信号表101列回填 | DuckDB+云推演 | 抄增量NULL回填模式 |
| 胜率优化 | 无闭环 | ADAPT自适应 | 保持优势，只需它的回填数据结构 |
