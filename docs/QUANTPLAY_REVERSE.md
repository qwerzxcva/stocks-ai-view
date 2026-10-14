# QuantPlay (quantplay.cn) 逆向结论

> 逆向时间：2026-10-14 深夜 ｜ 方式：浏览器 F12（XHR/fetch/EventSource/WebSocket hook）
>  + performance API + JS bundle 静态分析
> 约束：未注册账号、未付费、未使用任何真实密钥。游客态（`session` cookie `role=guest`）。

---

## 0. 结论（TL;DR）

| 项 | 结论 |
|---|---|
| 站点类型 | **纯 SPA**（Flask 后端 + 自研 `/static/js/` 模块化前端，非 Nuxt/Next） |
| 数据获取方式 | **轮询 + ETag 304**（`qp_fetch.js`）；`/stream/` SSE 只做「更新感知」，不承载数据 |
| 找到的 API | **19 个** `/api/*` 端点（见 §2） |
| 游客态可用 | **0 个** — 全部 `403 {"gate_block":true,"redirect":"/login"}` |
| 风控 | **IP + UA + session 三重绑定**：浏览器内 `fetch` 同源可过；外部 curl 带同样 cookie 仍 403 |
| 接入可行性 | **不可直接接入**。需注册会员（公众号「马小帅不过7秒」扫码入圈） |

---

## 1. 架构（F12 + JS 分析得出）

```
浏览器 SPA
  ├─ qp_stream.js    → EventSource("/stream/")  ← 只推 tick 通知, 不含数据
  │                    链路: monitor → Redis rt_events → sidecar(:8083) → Nginx /stream/
  ├─ qp_fetch.js     → 收到 tick 后反拉数据 (ETag/304 优化, 近零成本)
  ├─ chart_endpoint_client.js → /api/chart/<key>?trade_date=...&envelope=1&protocol=v2
  └─ 页面轮询间隔 9-10s
```

关键设计（`qp_stream.js` 注释原文）：
> 「主轮询永不停：轮询是兜底（SSE 只把更新感知从轮询间隔 5-10s 降到 ≤1s；轮询照跑，qp_fetch 的 ETag 使其变成近零成本的 304）」

**含义**：即使断开 SSE，只要带对 cookie 轮询 `/api/*` 就能拿到数据。所以接入的关键是 **cookie/IP/UA 绑定**，不是 SSE。

---

## 2. 挖到的 19 个 API 端点

来自 `qp_fetch.js`（19061 字符）与 `chart_endpoint_client.js`（7129 字符）：

| 端点 | 用途 |
|---|---|
| `/api/sentiment` | 整体情绪指标（含总额/涨跌家/涨停封单/炸板率/昨涨停/综赚钱效应/埋葬资金等 25 列） |
| `/api/sentiment/market_pct_distribution` | 全市场涨跌幅分布（>10% / 7~10% / … / <-10%，共 5368 只） |
| `/api/sentiment/active_reasons` | 活跃原因 |
| `/api/sentiment/intraday_speed_rank` | 盘中速度榜 |
| `/api/mood_summary` | 情绪摘要 |
| `/api/dragon_map` | 龙头图 |
| `/api/market_dragon_trend` | 龙头趋势 |
| `/api/buried_stocks` | 埋葬股（埋资金） |
| `/api/etf_flow/panel` | ETF DDE 净流入面板（1312 只去重，含上证50/沪深300/创业板/科创50） |
| `/api/premarket_catalyst_details` | 盘前催化剂 |
| `/api/replay/availability` | 竞价快照回溯可用性 |
| `/api/chart/<key>` | 图表数据（`key` 含 `sentiment_day` 等，支持 `trade_date` 历史回溯） |
| `/api/filter_table` | 通用筛选表 |
| `/api/concept_*` / `/api/sub_concepts` | 概念挖掘（含层级：概念→子概念→成分股） |
| `/api/bid_*` | 竞价相关 |
| `/api/stock_name_list` | 股票名清单 |
| `/api/watchlist` | 自选（需登录） |
| `/api/user-info` | 用户信息（游客 403） |

**数据字段极其丰富**（首页实测渲染出的）：
- 整体情绪：总额、总额环比、涨家、跌家、长腿、大面、大涨、大跌、涨停封单、跌停封单、涨停、跌停、触跌停、炸板率、昨涨停、昨首板、昨连板、昨多板、昨最高板、昨炸板、最高板、综赚钱效应、综亏钱效应、埋葬资金
- 涨停梯队：连板天数、首次封板时间、封单额、换手、流通市值、题材归属（如「传媒→传媒内容与版权→股权收购」）、强腿/强开标记
- ETF 资金流：占比、净流入、全 ETF 合计

---

## 3. 风控细节（为什么外部调不动）

浏览器内同源 `fetch('/api/sentiment')` 能返回数据；外部脚本带**完全相同的 cookie**（`qp_did` / `qp_fp` / `session`）仍返回：

```json
{"code":403,"gate_block":true,"redirect":"/login","success":false}
```

判定：服务端校验 **IP + UA + cookie 组合**（session 与首次下发时的客户端指纹绑定）。要绕过需完整模拟浏览器环境（或用浏览器自动化保持会话）。

---

## 4. 接入建议

| 方案 | 可行性 | 说明 |
|---|---|---|
| 直接 HTTP 调用 | ❌ | IP/UA 绑定，403 |
| 浏览器自动化（影子屏/vscreen）定时抓首页渲染结果 | ⚠️ 可行但重 | 数据在 DOM 里，需解析表格 |
| 注册会员拿 cookie 后用固定容器跑 | ✅ 最实际 | 需用户扫码入圈（公众号「马小帅不过7秒」） |
| **借鉴其算法而非接其数据** | ✅✅ 推荐 | 它的「综赚钱效应/埋葬资金/炸板率/涨停梯队题材归属」是**算法产出**，我们可以用自己的数据算出同类指标 |

**最有价值的是它的算法思路，不是数据本身**：
1. 「综赚钱效应 / 综亏钱效应」——把打板收益结构化成两个方向相反的综合分
2. 「埋葬资金」——涨停后次日大面的资金沉淀度量
3. 「强腿 / 强开」——连板结构里的强弱标记
4. 「概念→子概念→成分股」三级题材归属（我们已有 `sub_concepts` 类接口可对标）

这些都可以用我们自己的 K 线 + 资金流数据复现，不受风控限制。
