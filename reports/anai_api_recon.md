# anai.pro 接口逆向侦察报告

> **合规声明（务必先读）**
> 本逆向分析仅供 stocks-ai-view 项目做**接入可行性评估**与「引用级」数据读取，不作任何 AI 训练用途。
> - 目标站 robots.txt 明示：`User-agent: * Allow: /`，但附 Cloudflare Content-Signal **`search=yes, ai-train=no, use=reference`**——即允许检索与引用，**禁止用于训练**，消费方式限「reference（引用）」。本项目仅作决策参考引用，符合该信号；**严禁**把抓取内容灌入任何训练语料。
> - robots.txt 同时 **Disallow GPTBot / ClaudeBot / KimiBot / Bytespider / CCBot / Amazonbot / Applebot-Extended / Diffbot 等主流 AI 爬虫**。本项目使用自有 UA 的脚本而非上述爬虫身份，按 `User-agent: *` 组 `Allow: /` 评估路径可达性；但仍属灰色引用，频率必须克制（≥2s/次），量产后建议先取得站点授权。
> - 红线执行：未破解任何签名/验证码/反爬，未伪造鉴权，未提交任何登录请求（无凭据），登录墙后数据一律未访问。
> - 生成日期：2026-10-23。

## 0. 站点概况

- 栈：**Next.js (App Router, Turbopack, SSR/RSC) + Cloudflare 前置**。页面为完全 SSR，数据内嵌在 `self.__next_f.push(...)` RSC payload 中。
- 站点名 Stocks AI，定位「股市短线交易智能决策专家」，AI 报告模型署名 `deepseek-flash-vip`。
- 鉴权框架：**better-auth**（客户端 `createAuthClient`，启用 `adminClient` 插件），basePath 默认 `/api/auth`。
- 前端行情数据源：浏览器直连 **腾讯行情 `https://sqt.gtimg.cn/q=...&fmt=json`**（GBK 编码），由前端 `arr2obj` 组装快照——即行情快照本身源自腾讯，anai 的增量价值是 AI 报告。
- 响应头观察：`server: cloudflare`、`cf-cache-status: DYNAMIC`、`cf-ray`，无 `X-RateLimit-*` 系响应头。

## 1. 端点清单

### 1.1 匿名可用（已实测）

| # | URL | 方法 | 参数 | 返回 | 实测状态 | 供数模块 |
|---|-----|------|------|------|----------|----------|
| 1 | `/api/stock/search` | GET | `keyword`（URL 编码，必填；缺省 400；不匹配返回 `[]`） | JSON 数组 `[{code, name, type}]`，`code` 形如 `sh600522`/`sz003009`/`hk00994`，`type` 如 `GP-A`/`GP` | 200，多次无 429 | B 个股（代码映射）、G 行业（type 粗分） |
| 2 | `/s/{shortId}` | GET | 路径参数 shortId（8 位十六进制，如 `0d5772a8`） | SSR HTML，内嵌行情快照 JSON + AI 报告 markdown（见 §3） | 200 | B 个股（快照+AI 报告）、A 大盘（报告内含大盘环境段）、D 消息（报告消息面段）、F 资金（报告资金面段） |
| 3 | `/api/auth/get-session` | GET | — | 匿名返回 `null`；登录后应返回 `{user, session}` [推断字段名依 better-auth 约定] | 200 `null` | E 数据（会话探活/凭据有效性检测） |
| 4 | `/api/auth/ok` | GET | — | better-auth 健康检查 | 200 | E 数据（探活） |
| 5 | `/login`、`/register` | GET | — | SSR 登录/注册页 | 200 | — |

### 1.2 登录墙后（路径确认存在，未访问数据）

| # | URL | 方法 | 说明 | 供数模块 |
|---|-----|------|------|----------|
| 6 | `/dashboard` | GET | 307 → `/login`。登录后大概率是「我的分析/历史报告」入口 [推断] | B/C（历史 AI 报告列表） |
| 7 | `/api/auth/sign-in/email` | POST | OPTIONS 探查 `allow: GET, HEAD, OPTIONS, POST`。载荷见 §2 | 凭据获取 |
| 8 | `/api/auth/sign-up/email` | POST | /register 页调用 `signUp.email()` [推断路径，better-auth 约定] | — |
| 9 | `/api/auth/sign-in/social` | POST | 登录页含 `signIn.social({provider, callbackURL:"/dashboard"})`；**JS 中未发现具体 provider 字符串**（按钮可能未接线或动态传入）[推断] | — |
| 10 | `/api/auth/sso/saml2/callback/provider1` | GET/POST | better-auth SSO 插件示例串残留在 adminClient 打包代码中，**大概率是库自带示例而非站点真实配置** [推断] | — |
| 11 | `/api/auth/sign-out`、`/api/auth/list-sessions` 等 | POST/GET | better-auth 默认路由集，未逐一探查 [推断] | — |

### 1.3 探查为 404 的路径（排除项）

`/settings` `/profile` `/history` `/stocks` `/watchlist` `/reports` `/analysis` `/api/auth/session`（正确路径是 `get-session`）、`/sign-up`。

### 1.4 前端直连第三方（非 anai 端点，但重要）

- `https://sqt.gtimg.cn/q={symbol}&fmt=json`：腾讯行情，前端直拉，GBK 解码，`arr2obj` 字段映射（price/change_rate/previous_close/open/high/low/volume/amount/market_value/turnover_rate/pe/pb/vr…）。**若只需行情快照，直连腾讯比抓 anai 更干净**；anai 独有资产是 AI 报告。
- 前端工具函数含雪球链接生成（`https://xueqiu.com/S/{symbol}`），仅做外链，无数据调用。

## 2. 登录流程逆向（仅机制描述，未提交任何登录请求）

- 页面：`/login` 表单字段 `email`、`password`（noValidate，zod 前端校验）；`/register` 另有 `confirmPassword`。
- 提交：前端 `authClient.signIn.email({email, password}, {onRequest/onSuccess/onError})` → better-auth 约定路由 **POST `/api/auth/sign-in/email`**，JSON 载荷 `{"email","password"}` [载荷格式为 better-auth 约定，推断]。
- 会话：better-auth 默认 **cookie 会话**（`better-auth.session_token`，HttpOnly）[推断 cookie 名依库默认]；`/api/auth/get-session` 为会话查询点，adapter 凭据模式用它做凭据有效性自检。未发现 JWT/refresh-token 证据——**无独立刷新机制，会话续期依赖 cookie 有效期**；adapter 预留 refresh 钩子即调用 `get-session` 重新校验。
- CSRF/签名：**未发现**自定义请求签名、加密参数或独立 CSRF token 机制；better-auth 依赖 SameSite cookie + Origin 检查 [推断]。
- 验证码/反爬：登录页与 JS bundle 中**未发现** turnstile/recaptcha/hcaptcha 引用；但 Cloudflare 前置可能对高频登录触发 JS Challenge（未实测，不试探）。若未来凭据模式登录遇验证码：**只记录、转人工，不破解**（红线 4）。
- 注册：`signUp.email()` 存在，理论上可自助注册；本项目不实施。

## 3. `/s/{shortId}` 内嵌数据结构（实测 0d5772a8 = 中天科技 600522）

RSC payload（`self.__next_f.push([1,"..."])`）中含：

```json
{"time": 1792751496980, "symbol": "sh600522", "code": "600522", "name": "中天科技",
 "price": "27.93", "change_rate": "1.16", "change": "0.32", "previous_close": "27.61",
 "open": "27.18", "high": "28.10", "low": "26.01",
 "volume": "133.16万手", "amount": "35.91亿", "market_value": "953.24亿",
 "circulating_value": "953.24亿", "turnover_rate": "3.90%",
 "pe": "25.61", "pb": "2.48", "vr": "1.08", "type": "GP-A"}
```

- 数值均为**字符串**，单位后缀中文（万手/亿/%）；`time` 为毫秒 epoch（实测值对应 2026-10-23T10:31:36Z，即 `data_asof`）。
- AI 报告为独立 push 块的**纯 markdown 字符串**（含 `# 一、大盘环境分析`…`# 五、后续跟踪` 五段结构），以 `\n` 转义；组件处含 `"model":"deepseek-flash-vip"`。
- 解析策略（adapter 已实现）：正则定位 `\\\"stock\\\":{...}` JSON 块与 `\\\"model\\\":\\\"...\\\"`，报告 md 取「最长且含 markdown 标题的转义字符串」并按 JSON 字符串反转义。对 RSC 格式变化有一定韧性但非完整 RSC parser——属已知降级。

## 4. 限流与 Cloudflare 行为（礼貌频率观察）

- 2s 间隔连续请求 `/api/stock/search` ×4 + 各类页面：全部 200，无 429/403/挑战页；响应 1.6–3.3s。
- 无 `Retry-After` / `X-RateLimit-*` 头；`cf-cache-status: DYNAMIC`（API 不缓存）。
- 结论：**礼貌频率（≥2s）下无限流证据**；但不排除高频触发 Cloudflare 托管规则。adapter 默认 2s、硬下限 1s，并带本地缓存去重。

## 5. 对本项目模块供数对照

| 模块 | anai 可供 | 模式 | 备注 |
|------|-----------|------|------|
| A 大盘 | AI 报告中的「大盘环境分析」段（定性文本） | B 匿名 | 非结构化，供参考引用 |
| B 个股 | 快照 12 字段 + AI 决策报告（趋势/资金/消息面/操作结论） | B 匿名 | **核心价值**；快照可改由腾讯源替代 |
| C 异动 | 无直接供数 | — | 报告含当日涨跌叙述，非结构化 |
| D 消息 | 报告「消息面」段 | B 匿名 | 定性 |
| E 数据 | search 代码映射（A/港股覆盖） | B 匿名 | 可辅助代码归一 |
| F 资金 | 报告「资金面」段（主力净流入等叙述） | B 匿名 | 定性 |
| G 行业 | search 返回 type（GP-A/GP）粗分 | B 匿名 | 弱 |
| H 另类 | AI 报告全文（另类 NLP 素材，**仅引用级使用**） | B 匿名 | 受 ai-train=no 约束 |

**凭据模式（A）独占**：`/dashboard` 历史报告列表、批量生成/触发分析的可能接口（未逆向到具体路径，需凭据后补查）。匿名模式拿不到「历史/列表」维度，只能逐个 shortId 访问。

## 6. 已知缺口与降级

1. JS chunk 名为哈希值且按需加载，`/dashboard` 登录后的 chunk 未加载 → **登录墙后业务 API 未能枚举**，需凭据后二轮侦察。
2. 分享页 shortId 无列表入口，匿名模式只能消费「已知 shortId」或经 search 拿代码（search 不回传 shortId，两者不互通）。
3. RSC 解析为正则级，Next.js 版本升级可能失效——adapter 中已做空值降级处理。
4. robots 的 `use=reference` 允许引用性消费，但量产接入前建议邮件取得站点授权。
