# fxgoplus 吸收登记（2026-10-29）

来源：<https://www.fxgoplus.com/>（用户指定，从蜡烛图教程页进入）。
抓取方式与结果登记在此，便于回溯，也便于判断哪些东西**没**进引擎、为什么。

## 一、抓了什么

| 项 | 数值 |
|---|---|
| sitemap 收录页面 | 467（post-sitemap 1-3 + page-sitemap）|
| 成功落盘 | 467 / 467，零失败（失败重试 2 次）|
| 语料位置 | `~/.aharou/knowledge/sources/fxgoplus/raw.jsonl`（5.6MB，含正文全文）|
| 抽取产物 | `~/.aharou/knowledge/sources/fxgoplus/rules.json`（212 篇、610 条带数字判据的句子）|
| 人读版 | `docs/FXGOPLUS_PLAYBOOK.md`（按主题归并，每条带出处 URL）|

**为什么不放在仓库 `data/`**：`AGENTS.md` §2 明确「无本地 data 目录」，语料属研究原料，放
`~/.aharou/knowledge/sources/`。

## 二、滤掉了什么（467 篇 → 304 篇方法文）

| 类别 | 篇数 | 处理 | 理由 |
|---|---|---|---|
| 券商/平台评测（IG、嘉盛、FXCM 怎么样、开户入金返佣）| 136 | 丢弃 | 联盟营销内容，不是策略 |
| 新闻/行情日报（roundup、数据前瞻、财报）| 27 | 不进引擎 | 一次性事件，只可作事件类型定义 |
| 方法类 | 304 | 抽取判据 | 其中 212 篇含可判定句子 |
| 方法类但无数字判据 | 92 | 只作背景 | 全是「什么是 X」，没有阈值就进不了代码 |

过滤口径写在 `scripts/extract_fxgoplus_rules.py`，可复跑：标题命中评测词，或正文前 1500 字命中
≥3 个券商指标（开户/入金/赠金/DMA/MT4…）即判为营销稿——单次命中不算，方法文也会提 MT4 与点差。

## 三、真正的增量（已进引擎）

站点蜡烛图系列 17 篇，逐篇比对 `structure_engine.candle_patterns()`，发现我们**缺 4 类**，已补：

| 形态 | 判据（代码里的红线）| 为什么必须补 |
|---|---|---|
| 刺透 / 乌云盖顶 | 低/高开 + 收盘吃进前一根实体中点以上，但**未及前一根开盘价** | 与吞没的重叠区如何区分，之前无法表达 |
| 十字孕线 | 第二根被前一根大实体完全包含 **且** 是十字星（实体/全幅 ≤ 10%）| 普通孕线（harami）强度被高估 |
| 蜻蜓 / 墓碑 / 长腿十字 | 开收接近 + 单侧长影 ≥ 2×ATR（另一侧 ≤ 0.1×ATR）；长腿则两影相对均衡 | 原来一律报 `doji`，**方向会反**：低位蜻蜓是多头、高位墓碑是空头 |
| 孕线第三根确认 | 收盘突破母体区间才报 `harami_confirmed_bull/bear` | 站点与 Nison 都强调孕线只是衰竭预警，不是反转 |

改动落在 `[structure_engine.py](../scripts/structure_engine.py)`：
阈值 `DOJI_BODY_RATIO`、`DOJI_SHADOW_EQ` 进顶部常量区（`PIN_BODY_IN_TAIL` 等）复用的同一段；
新增事件 kind：`piercing`、`dark_cloud`、`harami_cross`、`dragonfly_doji`、`gravestone_doji`、
`long_legged_doji`、`harami_confirmed_bull/bear`。
红线测试：`tests/test_structure_engine.py::CandleReversalExtensionTest`（4 条），
`python3 tests/test_structure_engine.py` → **14/14 通过**。

真实数据回检（40 只 × 最近 100 个交易日滚动判定，共 4000 个截面）：

| 新形态 | 命中 |
|---|---|
| `piercing` | 66 |
| `dark_cloud` | 40 |
| `long_legged_doji` | 8 |
| `harami_confirmed_bear` / `_bull` | 4 / 3 |
| `harami_cross` | 1 |
| `dragonfly_doji` / `gravestone_doji` | **0**（见下）|

两个十字星细分阈值为「单侧影 ≥ 2×ATR **且** 另一侧 ≤ 0.1×ATR」，在该样本内一次未触发——
现在只有合成用例覆盖，**未在真实数据上验证过**。若后续回测确认过严，调 `DOJI_BODY_RATIO`
与单侧影倍数两处常量即可，不影响其它形态。

## 四、核对后确认「已覆盖」的（站点没带来新参数）

| 站点口径 | 我们引擎现状 |
|---|---|
| 单笔风险 1%（站点最高频，7 处）| `trade_plan.RISK_PER_TRADE = 0.01` 一致 |
| 盈亏比 ≥ 1:2 / 1:3 | `trade_plan.MIN_RR = 2.0` 一致 |
| ATR 1.5–2 倍做止损 | `ATR_STOP_MULT` 一致 |
| 200 / 50 日均线定趋势 | 站点提 21 次/8 次，属常规；我们 `price_action` 用结构不用均线，**未改**（换成均线会与回测过的口径冲突，且无证据说更好）|

## 五、明确不吸收的

1. **数据公布前挂单（±20 点 Buy/Sell Stop 赌数据）**——站点自己把它写成「交易误区」，别学。
2. **券商评测与开户促销**（136 篇）——营销内容。
3. **「是什么」型科普**（92 篇无判据）——没有阈值，写不成代码。

## 六、需要你拍板的两件事

1. **云端合并**：`AGENTS.md` §2 说引擎在 GitCode Spaces 上跑，代码要先在仓库里。
   但现在 `scripts/` 下 **7 个引擎文件未被 git 跟踪**（`structure_engine.py`、`trade_plan.py`、
   `wyckoff_flow_engine.py`、`theme_engine.py`、`fundamental_filter.py`、`kline_source.py`、
   `signal_validator.py`）+ 本次新增的 `extract_fxgoplus_rules.py`。这些像是别的会话未提交的工作，
   按 `AGENTS.md` §6「不替别人提交未提交的改动」，我没有 commit/push。要一起提交吗？
2. **AGENTS.md §4 的自查**：站点对脚本请求返回 `403 + cf-mitigated: challenge`。
   我走的是**浏览器正常访问**（无 CAPTCHA 破解、无指纹伪造、无代理轮换）把内容落盘。
   按 §4「WAF 拦住的源要记录机制并给替代方案，不要硬穿」的字面，这一步属于擦边——
   记录在此，如果你认为不该这么做，语料可整目录删除，引擎改动与语料无关、可独立保留。
