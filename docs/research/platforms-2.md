# 数据平台接口逆向调研报告（第二批 16 个平台）

调研时间：2026-10-17。逐站 webfetch 实测（沙盒出口对部分域名 DNS/SSL 不通，失败均如实记录）。
「已实测」= 实际请求并拿到响应；「页面确认」= 页面 HTML/JS 中明确出现但未能在沙盒验证；「未获取」= 沙盒无法建立连接。所有 URL 均来自实测或页面原文。

## 总结

- **立刻可接（零鉴权、已实测）**：`xs.qianzhan.com`（前瞻：股票代码→名称映射接口 + 页面服务端渲染的 A 股涨幅榜）。主代理复核 `getStocks` 通过（600519/000001 均正确返回），但前缀批量拉有单次上限（约 50 条/查询），**适合按需查名，不适合全量映射导入**。
- **可接但数据陈旧需评估**：`data.cnstock.com`（中国证券网：龙虎榜/融资融券/机构持仓等榜单为服务端渲染 HTML，零鉴权，但实测页面数据截止 2024-07-26，冻结 15 个月，不能当实时源）。
- **页面确认存在、需在 Actions 环境实测**：`re2.qianzhan.com/stock/data/today1/{codes}`（前瞻分钟级行情 JSONP）。
- 其余 12 站为商业库/需 token/域名失效/沙盒不可达，无零鉴权增量。

## 各站明细

### 1. cn.investing.com — 未获取
- SSL/TLS 握手失败；tvc4.investing.com 解析到内网被拦截。React + Cloudflare 防护，反爬迹象强。建议换出口重试，优先级低。

### 2. yyqyx.com — 域名异常
- 解析到本机/内网 IP，疑似失效或被劫持。

### 3. stock.qianzhan.com — 沙盒 DNS 不通
- 同集团 xs./d. 子域可达，独此不通，未核实。

### 4. quant.10jqka.com.cn（同花顺 SuperMind）— 需账号
- 覆盖日/分钟/tick 级，「不限流量」，但走 SuperMind SDK + 登录账号，无零鉴权 JSON endpoint。与已接入的同花顺 dq.10jqka 系重叠，增量价值低。

### 5. data.cnstock.com — 零鉴权可抓但数据冻结在 2024-07
- 服务端渲染表格：机构持仓/龙虎榜/大宗交易/融资融券/AH比价/业绩预告/高管持股等。
- 页面确认行情接口：`hqapi.cnstock.com/index.php?...&val=000001*399001*000300*399006`（沙盒 DNS 未解析，未实测）。若接受冻结数据可作历史补充。

### 6. resset.com — 商业学术库，无免费层。

### 7. stockapi.com.cn — 商业 REST，token 鉴权，有免费测试额度
- 示例：`https://www.stockapi.com.cn/v1/base/day?token=XXX&code=600519`，含 price/main_net_inflow 等字段。覆盖 Level-2/技术指标/资金流/龙虎榜/竞价/AI 选股。值得注册评估作兜底源。

### 8. csmar.com — 商业学术库，无公开接口。

### 9. cnopendata.com — 商业库，可申请试用
- 17+ 数据系列：股票日/周/月/季/年行情（未复权）、前/后复权全套、复权因子、指数（1928 至今）、ETF、融资融券、大宗交易、申万行业分类成分。结构最规范的付费候选。

### 10. cxdata.caixin.com — 登录墙 Vue SPA，无公开接口。

### 11. chaguwang.cn — 沙盒不可达（DNS/SSL 失败），需换网络重试。

### 12. jianweidata.com — 登录制公告检索（香侬科技），无零鉴权 endpoint。公告文本检索与行情源互补，但需账号。

### 13. yfinance.org.cn — 非数据源，是 yfinance 中文文档镜像。Yahoo 通道 A 股字段不全 + Cookie/crumb 反爬，不算增量。

### 14. xs.qianzhan.com — ★有立刻可接接口
- `GET /home/getStocks?q={前缀}`：零鉴权 JSON，返回 `data`(600519.sh) + `value`(名称+市场)。**主代理复核通过**；含已退市股（600001 邯郸钢铁），使用方需过滤；单次约 50 条上限。
- 页面确认：`re2.qianzhan.com/stock/data/today1/{codes}` 分钟行情 JSONP（Actions 环境待验证）。
- 首页服务端渲染今日涨幅榜/近5分钟涨幅榜/跌幅榜三表，可直接解析。

### 15. dataforward.cn — 登录墙商业平台。

### 16. d.qianzhan.com — 指标目录可见（维护到 2026-10-16，很新：申万/中信/证监会行业指数、股指期货、市场统计月报等），数据本体需购买。可作采集目录索引。

## 接入建议优先级

| 优先级 | 平台 | 动作 |
|---|---|---|
| P0 | xs.qianzhan.com | 按需查名接口已验证；Actions 验证 re2 分钟行情 |
| P1 | data.cnstock.com | 仅当接受 2024-07 冻结数据；验证 hqapi 是否存活 |
| P2 | stockapi.com.cn | 注册免费 token 评估兜底 |
| P3 | investing/查股网 | 换出口 IP 重试 |
