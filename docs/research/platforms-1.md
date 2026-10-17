# 第二批数据平台接口逆向报告（8 个平台）

调研时间：2026-10-17（UTC+8，非交易日周六晚间）
调研方法：webfetch 抓首页 HTML → 提取内嵌 JS / ajax 地址 → 对候选接口发实测 GET 请求。
注意：调研环境为 Android 容器出口，部分域名（jrdao.com 被解析为本机/内网地址、resource.stockstar.com 解析失败）可能是出口 DNS/安全策略问题，不代表公网真实状态。

## 总览

| 平台 | 立即可接 | 数据类型 | 格式 | 鉴权 |
|---|---|---|---|---|
| 爱股票 | 是 | 7x24 快讯（带题材/个股标签） | JSON | 无 |
| 金融界 | 是 | 每日要闻快讯、频道新闻列表 | JSON / JSONP | 无（列表接口为 POST） |
| 上证 e 互动 | 是 | 互动问答、公司公告、调研纪要 | HTML 片段 | 无 |
| 证券之星 | 部分 | 业绩预告/主力统计/解禁 | HTML 表格 | 无 |
| 淘股吧 | 否（需 POST，参数未知） | 行情/题材 | 未知 | 未见 cookie 要求 |
| 和讯 | 否（接口被风控 567） | 行情/板块 | JSONP | 有反爬 |
| 僚巴（银建） | 不可达 | — | — | — |
| 今日道 | 不可达（DNS/出口限制） | — | — | — |

---

## 1. 淘股吧 https://tgb.cn/newIndex/

- 页面线索：首页引用 `https://js.tgb.cn/res/js/common/hqUrl.js`，内容为：
  - 行情列表：`https://hq.tgb.cn/tgb/realHQList`
  - 行情根路径：`https://hq.tgb.cn/tgb/`
- 实测：`GET https://hq.tgb.cn/tgb/realHQList` → HTTP 500；带 `?stockCode=600519`、`?symbol=600519` 均 500。
  判断：该接口应为 POST（参数在 body），或需要特定 header。当前调研手段无法发 POST，未逆向成功。
- 站内其他接口形态（页面链接可见，未逐个实测）：`https://www.tgb.cn/feeds/qryFeedsViewPointView`（投资策略 feed，GET 返回 HTML 页面，非 JSON）、导航中「极速快讯」栏目存在但接口地址未在首页 HTML 中暴露。
- 反爬：未见到 cookie/签名要求；主要障碍是接口需要 POST。
- 结论：暂不接入。如需接入，下一步用 curl/requests 对 `hq.tgb.cn/tgb/realHQList` 试 POST（常见参数形态：`{"codes":["SH600519"]}` 或 form 表单，未核实）。

## 2. 爱股票 https://www.aigupiao.com/ ★可接

- 发现接口（页面 `express_list.js` 中硬编码，实测成功）：
  - **URL**: `https://apis.aigupiao.com/Express/express_list/`
  - **方法**: GET
  - **参数**: `before=0`（翻页游标，传最后一条的 rec_time；0=最新）, `source=pc`, `web_data=yes`, `number=20`, `division=`, `express_show_type=1`；行业要闻变体加 `type=theme` 且去掉 express_show_type；增量轮询用 `after=<last_time>` 替代 before。
  - 实测：`https://apis.aigupiao.com/Express/express_list/?before=0&source=pc&web_data=yes&number=3&express_show_type=1` 返回 200 JSON。
  - **返回格式**: `{"rslt":"succ","data":{"2026-10-17-6":{"title":"10月17日 星期六","data":[{...}]}}, "last_time":1792248956, "date_time":"2026-10-17 23:02"}` —— 按日期分组。
  - **字段样例**（单条快讯）: `id, content（正文，含【标题】前缀）, rec_time（unix秒）, update_time, view_num, theme（题材名逗号分隔，如 "IP,富士康,存储芯片"）, stock_code（"sz300959,USAAPL"）, stock_infos[{content,code,name,kind}]（kind=stock/theme，theme 带题材 id）, theme_quotes{bk282:{id,theme,chg_pct}}（题材涨跌幅）, support_num/oppose_num/comment_num, important(yes/no), voice（语音播报 mp3 url）`。
  - **数据类型**: 7x24 财经快讯 + 题材打标 + 关联个股，题材标签是增量价值点。
  - **鉴权**: 无。跨域请求带 cookie（`withCredentials`），但实测裸 GET 无 cookie 也返回完整数据。
  - **频率**: 页面端按 after 参数增量轮询，未见显式限制；页面还有 `https://apis.aigupiao.com/...` 一族接口（`__api` 前缀变量），如热评榜 `express_hot_list`（参数 genre=day/week/month/one_day），同域同风格，可顺带探测。
- 同源其他线索：`https://app.aigupiao.com/goapi/match/details`（模拟炒股大赛排行，未实测）、组合排行榜组件（vuebuild 内，未展开）。

## 3. 金融界 https://stock.jrj.com.cn/ ★可接

### 3a. 每日要闻静态 JSON（推荐）
- **URL 模板**: `https://stockjs.jrj.com.cn/share/news/yaowen/yw{yyyy-MM-dd}.js`
- 来源：`https://jscloud.jrjimg.cn/cloud/js/general/7x24news.js` 中拼接，页面按天回溯取昨天。
- 实测：`yw2026-10-17.js`、`yw2026-10-16.js`、`yw2026-10-01.js` 均 200，内容为 `var newsdata=` 前缀去掉后即为 JSON（稳妥做法是剥掉 `var newsdata=` 前缀再 JSON.parse）。
- **字段**: `iiid（新闻id）, title, makedate（"2026-10-17 21:30:56"）, infourl（PC原文页）, mInfoUrl, appInfoUrl, detail, infosource（"市场资讯"/"金融界"/"期货日报"）, chnmb（103=财经/010=股票/102=美股）, infocls, imgurl, picThumb`。
- 单日约 50-100 条，覆盖全天快讯。**一天一个文件，天然适配 GitHub Actions 定时拉取，无频率问题**。
- 鉴权：无。反爬：未见。

### 3b. 频道新闻列表网关（POST，未实测成功）
- **URL**: `https://gateway.jrj.com/jrj-news/news/queryNewsList`
- 来源：`jscloud.jrjimg.cn/cloud/js/general/queryNewsList.js`。
- 方法：POST，Content-Type: application/json，body `{"sortBy":1,"pageSize":20,"makeDate":"","channelNum":"010","infoCls":""}`（channelNum 从页面 .channelNum 读，010=股票频道）。
- 返回结构（据 js）：`res.code==20000`，`res.data.data[]` 含 `title, makeDate, imgUrl, picThumb, paperMediaSource`；翻页传最后一条 makeDate。
- 实测：GET 该 URL 返回 404（需 POST）。调研环境无法发 POST，**未验证**，接入时用 requests 验证。

## 4. 证券之星 https://www.stockstar.com/

- 首页未发现 JSON API；行情 quote.stockstar.com 在当前出口 DNS 解析失败（未核实是否公网可解析）。
- 可用线索为**数据中心静态/服务端渲染页**（HTML 表格，可解析）：
  - `https://resource.stockstar.com/DataCenter/StockData/GetAchieveForecast.htm`（业绩预告）——实测返回完整 HTML 表格：序号/股票代码/简称/公告日期/报告期/预告类型/预告摘要/上年同期EPS，带分页（共483条25页，翻页参数未逆向，首页 20 条已够用）。
  - 同目录还有：`GetMainForceStats.htm`（主力统计/资金流）、`GetStockLimitSaleSummary.htm`（限售解禁）、`PrivateData/GetInvestRankSummary.htm`、`PrivateData/Assess_value_000001.html`（个股估值）。
  - 注意：resource.stockstar.com 在本调研出口第二次请求时报「无法解析主机名」，但 GetAchieveForecast 第一次抓取成功——**域名解析不稳定，接入前需在 Actions 环境实测**。
- 数据类型：业绩预告/资金流/解禁，HTML 需表格解析。鉴权：无。频率：静态页，无明显反爬。

## 5. 上证 e 互动 https://sns.sseinfo.com/ ★可接（HTML）

- 接口（首页 JS switch 硬编码，实测成功）：
  - **URL**: `https://sns.sseinfo.com/ajax/feeds.do`
  - **参数**: `type={10|11|12|13|20|21|30|40|80}, pageSize=10, lastid=-1, show=1, page=1`
  - type 含义（对照页面 tab 顺序）：10=最新问答（全部）、11=已回复问答、12/13=问答（分类，实测返回"暂时没有问答"）、20/21=观点（实测空）、**30=公告/投资者关系活动记录（实测有数据：调研纪要 docx/pdf 标题+链接）**、40=公告（实测"暂时没有公告"）、80=年报视频（空）。
  - **返回**: HTML 片段（非 JSON），结构为 提问人:公司(代码)问题 … 公司回复 … 时间+来源（微信/网站/鸿蒙/Android）。type=30 返回 公司名+公告标题+附件文件名。
  - 实测样例（type=11）：投资者问华丰科技(688629)产能/订单，公司答「在手订单充足，产能利用率维持较高水平」——**对个股基本面监控有直接价值**。
  - 分页：`page` 递增 + `lastid`（页面传 -1，或上一页末条 id）。实测 page=2&lastid=-1 返回空（可能需正确 lastid，未深究）。
  - 鉴权：无（点赞/收藏需登录，只读不需要）。反爬：未见。
- 另有 `ajax/getCompany.do`（公司联想搜索）、`ajax/followCompByCode.do`（关注，需登录），未实测。

## 6. 和讯 https://quote.hexun.com/

- 页面内嵌 JSONP 行情接口（`<script src>` 直接引用）：
  - `https://stocksquote.hexun.com/a/sortlist?callback=conceptionback&block=557&number=1000&title=1&commodityid=0&direction=0&start=0`（板块成分榜）。
  - 实测（带/不带 callback）：均返回 **HTTP 567**（非标准状态码，和讯自定义风控拦截，疑按 UA/Referer/IP）。
  - 知名公开接口 `webstock.quote.hermes.hexun.com/a/quotelist?code=sse000001&column=...` 在当前出口 DNS 解析失败，未验证。
- 结论：有公开接口形态但当前出口被风控，**不建议作为主源**；可留作备选（带浏览器 UA + Referer: quote.hexun.com 重试，未验证）。

## 7. 僚巴 http://liaoba.yjcf360.com/

- 首页与 `?WebShield=0` 均抓取失败：`unexpected end of stream`（TLS 握手被截断，疑 WebShield 防火墙拦截非浏览器指纹）。
- 未取得任何页面内容，无接口线索。**不可达，放弃**。

## 8. 今日道 http://www.jrdao.com/

- 抓取被工具拦截：`拒绝访问本机或内网地址：www.jrdao.com` —— 该域名在当前出口 DNS 解析到了内网/本机 IP（疑似域名已失效或被劫持）。**未获取任何内容，无法评估**。

---

## 接入建议（按优先级）

1. **金融界 yw{date}.js**：一天一个静态 JSON 文件，Actions 每日拉 1-2 次即可，最省事。
2. **爱股票 express_list**：JSON、带题材/个股标签、支持 after 增量轮询，可做 7x24 快讯增量源。
3. **上证 e 互动 feeds.do type=10/11/30**：HTML 片段，正则解析即可，独家的董秘问答+调研纪要源。
4. **证券之星 GetAchieveForecast.htm**：业绩预告 HTML 表格，低频日更。
5. 金融界 gateway POST 接口、淘股吧 hq POST 接口：需要能发 POST 的环境再验证。
