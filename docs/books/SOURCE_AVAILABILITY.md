# 32 本书：正版渠道与可抓取性（逐本核查）

> 2026-10-29 逐本联网核查。**之前的"没有"是没查就下结论，这份表是查过之后的答案。**
> 核查手段：微信读书站内检索接口（`weread.qq.com`）、Project Gutenberg 公版库、京东读书阅读页（`cread.jd.com`）。

## 结论先行

| 问题 | 查证结果 |
|---|---|
| 这些书有没有电子版？ | **有，31/32 本在微信读书有正版电子版**（多数标"免费/会员"，即会员可读；少数标"付费"）|
| 能不能抓全文？ | **付费/会员书不能**。微信读书、京东读书的正文走登录态 + 分段接口 + 会员校验，抓取等于绕过付费与 DRM，属侵权，不做 |
| 有没有能合法拿全文的？ | **有 3 本**：古登堡收录的公版英文原著，已全文落盘（见下）；archive.org 本沙箱不可达。**注意：3 本里只有 #27 对得上本清单书目，另 2 本是「源流对照本」（见下表的「对应书目」列），不算 33 本内的书** |
| 那卡片怎么才会有原文依据？ | 三条路：① 公版原著（已落地）② 你把自己买的正版 EPUB/PDF 放进 `~/workspace/books/`，我写解析器读 ③ 我把书单里能对应的公版原著逐本核对 |

## 已落盘的公版全文（可引用、可核对、可分发）

| 文件 | 来源 | 大小 | 对应书单 |
|---|---|---|---|
| `reminiscences_of_a_stock_operator.txt` | Gutenberg [#60979](https://www.gutenberg.org/ebooks/60979) | 620KB / 1563 段 | **#27 操盘作手：回忆录**（Livermore 原型）|
| `psychology_of_the_stock_market_selden.txt` | Gutenberg [#75570](https://www.gutenberg.org/ebooks/75570) | 130KB | 交易心理源流（关联 #12/#27）|
| `successful_stock_speculation_butler.txt` | Gutenberg [#26841](https://www.gutenberg.org/ebooks/26841) | 108KB | 投机原则源流（关联 #13）|

落盘位置：`~/.aharou/knowledge/sources/books/`。下载器：`scripts/fetch_book_text.py`（分块续传，受限 egress 下会中途断，必须续传）。

## 逐本核查表

`会员` = 微信读书标"免费/会员"（会员可读）；`付费` = 需单本购买；`无` = 站内无同名书。

| # | 书名（书单） | 微信读书命中 | bookId | 状态 | 公版英文原著 | 可抓全文 |
|---|---|---|---|---|---|---|
| 1 | 日本蜡烛图技术 | 日本蜡烛图技术：古老东方投资术的现代指南 | 32026436 | 付费 | 无（1991 版权）| 否 |
| 2 | 高级趋势技术分析 | 舵手经典57 高级趋势技术分析 | 3001158656 | 会员 | 无 | 否 |
| 3 | 高级波段技术分析 | 高级波段技术分析 | 3001158614 | 会员 | 无 | 否 |
| 4 | 高级反转技术分析 | 高级反转技术分析（上册）| 3000932898 | 会员 | 无 | 否 |
| 5 | 零基础看懂缠论 | 缠论精解：从零基础到立体交易体系构建 | 3300224061 | 付费 | 无（中文原创）| 否 |
| 6 | 艾略特波浪理论 | 艾略特波浪理论：市场行为的关键（第11版）| 41864839 | 付费 | Elliott 1938 原著 archive.org 有、本沙箱不可达 | 否 |
| 7 | 量价操盘法 | 量价操盘法 | 3005638885 | 会员 | 无 | 否 |
| 8 | 威科夫操盘法 | 威科夫操盘法：华尔街大师成功驾驭市场超过95年的秘技 | 3001158789 | 会员 | Wyckoff 1910/1922/1931 公版，Gutenberg 未收、archive 不可达 | **待打通** |
| 9 | 订单流交易 | 订单流交易 | 3003668556 | 会员 | 无（现代书）| 否 |
| 10 | 三角洲理论 | 三角洲理论 | 3000013680 | 会员 | 无（1978）| 否 |
| 11 | 亚当理论 | 亚当理论 | 3000000825 | 会员 | 无（1985）| 否 |
| 12 | 华尔街幽灵 | 华尔街幽灵：伟大投资者的交易智慧 | 735039 | 付费 | 无（1997）| 否 |
| 13 | 专业投机原理 | 专业投机原理（典藏版）| 25131314 | 付费 | Trader Vic 1991 非公版；道氏理论源流可用 Butler/Hamilton 部分替代 | 部分（源流）|
| 14 | 澄明之境 | 澄明之境：青泽谈投资之道 | 32858410 | 付费 | 无（中文原创）| 否 |
| 15 | 龙头作手 | 股市趋势交易大师2：龙头作手 | 3004066053 | 会员 | 无 | 否 |
| 16 | 龙头主力操盘术 | 龙头主力操盘术（孙乾）| 3004759157 | 会员 | 无 | 否 |
| 17 | 题材逐利 | 题材投机：追逐暴利的热点操作法 | 3001158486 | 会员 | 无 | 否 |
| 18 | 财报掘金 | 财报掘金 | 3300039222 | 付费 | 无 | 否 |
| 19 | 复合增长 | 复合增长—林园的投资逻辑与策略 | 3003726932 | 会员 | 无 | 否 |
| 20 | 林园炒股秘籍 | 林园炒股秘籍（精装增补版）| 3001176200 | 会员 | 无 | 否 |
| 21 | 价值投资 | 价值投资：从格雷厄姆到巴菲特（第2版）| 3300107845 | 付费 | 无 | 否 |
| 22 | 激荡投资30年 | 激荡投资30年：林园的投机智慧与财富传奇 | 3004680568 | 会员 | 无 | 否 |
| 23 | 万宗归于趋势 | 股市趋势交易大师1：万宗归于趋势 | 3004082247 | 会员 | 无 | 否 |
| 24 | 模型理论：富致中源模型 | 模型理论 | 3002382695 | 会员 | 无 | 否 |
| 25 | 实战经济论 | **站内无同名书** | — | 无 | — | 待确认书名；用户已确认**不是**任泽平《实战经济学》，需准确书名（此前候选判断作废） |
| 33 | 实战经济学（任泽平/华炎雪/曹志楠） | 微信读书有（会员） | — | 无 | — | 2026-10-30 增补书目；无合法全文，卡片仅基于微信读书书目信息 + 泽平宏观公开报告，置信度低 |
| 26 | 期货交易：实盘技巧 | 期货交易：从十五次爆仓到长期稳定盈利的蜕变 | 3004240810 | 会员 | 无 | 否 |
| 27 | 操盘作手：回忆录 | 股票大作手操盘术（利弗莫尔）| 731612 | 付费 | **Gutenberg #60979 全文在库** | **是（已落盘）** |
| 28 | 短线交易大师 | 短线交易大师：工具和策略 | 3001135796 | 会员 | 无 | 否 |
| 29 | 铁血短线 | 铁血短线 只铁战法致命的狙击战术 | 3002383314 | 会员 | 无 | 否 |
| 30 | 短线英雄 | 短线英雄 | 3000009762 | 会员 | 无 | 否 |
| 31 | 擒庄秘籍 | 擒庄秘籍 | 3000016095 | 会员 | 无 | 否 |
| 32 | 证券市场技术指标百科全书 | 证券市场技术指标百科全书 | 3004094108 | 会员 | 无 | 否 |

## 原文核对发现的第一个实质问题

`pivotal point`（关键点）在《Reminiscences of a Stock Operator》全文 **0 次出现**（1563 段逐段检索）。
它是 1940 年《How to Trade in Stocks》（股票大作手操盘术）里的概念。而 `batch3` 第 5 节把
"关键点（pivotal point）"直接写成《回忆录》的核心命题——**出处错了**。

《回忆录》里真实对应的是两条：

1. **最小阻力线**（20 处）：
   > *Prices, like everything else, move along the line of least resistance.* They will go up if there is less resistance to an advance than to a decline; and vice versa.
2. **分批建仓 + 不主张持续加码**（4 处）：
   > I don't mean to be understood as advising persistent pyramiding. A man can pyramid and make big money ... But ... Suppose a man's line is five hundred shares of stock. I say that he ought not to buy it all at once; not if he is speculating. If he is merely gambling the only advice I have to give him is, don't!

对我们的意义：`pivotal_point(n=60)` 的判据本身没错（区间突破 + 量比确认是通用做法），但**不能标成"利弗莫尔原法"**；
要么改成引用 1940 年那本书（微信读书有，需你授权 EPUB），要么按"最小阻力线"重写口径。

## 合法拿到其余 31 本正文的两条路

1. **你放书进来**：把正版 EPUB/PDF 放到 `~/workspace/books/`，跑 `scripts/ingest_books.py` 解析 → 分章落 `~/.aharou/knowledge/sources/books/` → 逐本回填卡片并标注"原文核对"。
   （脚本已就绪：EPUB 走标准库、PDF 走 pypdf，按 sha256 幂等，扫描版会明确报失败；用法 `python3 scripts/ingest_books.py --dry-run` 先看会解析到哪些文件。）
2. **我按公版源流核对**：书单里 2/3/4（价格行为）、13（道氏/投机原则）、27（Livermore）都能用 1910–1930 年的公版原著做口径校验；不必等纸质书就能把"蒸馏"降到"有原始依据"。

> 不做的事：抓微信读书/京东读书的正文。不是技术上做不到，是不该做——绕过会员校验和 DRM 抓版权书是侵权。

## 第二批原文核对：Selden / Butler（2026-10-30）

上一轮只核了《回忆录》一本。本轮把另外两本在库公版原著逐章读完，两条结论都影响现有卡片口径。

### 1. Butler《Successful Stock Speculation》（Gutenberg #26841，1910s）：他**明确反对**挂止损单

第二十一章「Stop Loss Orders」原文：

> Many speculators place stop loss orders only two points from the market price... but **it is our opinion that it is something that should be used very seldom** by those who trade along the broad lines recommended by us.

> **A large number of stop-loss orders is a good thing for the short interests.** （举 U.S. Steel 94 元、止损堆在 92 的例子：空头故意砸到 92 触发止损，再在低位回补。）

> Therefore, **we believe that stop-loss orders are a bad thing** and, as a rule, do not recommend them.

唯一例外：**牛市顶部**用移动止损——

> There is one instance where a stop-loss order can be used to advantage, and that is near the top of a bull market... place a stop-loss order at two or more points below the market, and **keep moving it up as the market price moves up**.

**对我们的影响（重要）**：

1. **不能把 `trade_plan` 的 ATR 止损/1R 风险预算标成"投机原则源流"**。Butler 的"不用止损"有三个前提：买价低于资产与盈利支撑的价值买入（第四章：`never to buy a security unless it is selling at a price below that which is warranted by assets, earnings...`）、不靠保证金、能扛纸面亏损（`you should look upon your loss merely as a paper loss`）。我们做的是趋势/短线 + 单笔 1% 预算，前提正相反，止损是必需的，但**依据要另找**（Sperandeo/幽灵/铁血那几本，非公版）。
2. **可迁移的只有一条**：盈利后止损单向上跟随（`move it up as the price moves up`）——与我们已有的"加仓后止损单调上移"一致，这条可以挂 Butler 出处。
3. **止损单聚集区是流动性池**这个机制观察，是 `structure_engine` 里 spring / 假跌破（击穿后快速收回）的早期文献依据。若要做成判据（例如"跌破前期低点后 X 根内收回"再加"距低点 ≤ 1.5×ATR"），须先回测校准，**本轮不改代码**。

### 2. Selden《The Psychology of the Stock Market》（Gutenberg #75570，**1912**）：分仓节奏与情绪极值

> 年份更正（2026-10-30 原文核对）：原书写的是 `Original publication: ... 1912`、`New York, May 28, 1912`（`psychology_of_the_stock_market_selden.txt:21,74`），此前记 1909 是错的。

第七章「The Psychology of Scale Orders」给了一个具体的下单节奏（第一人称示范）：

> So I will place orders to buy every half point down for three points.

即**等距 6 档、每档 0.5 点、总跨度 3 点**，每档占总预算 1/6。同章还有一处目标/止损的实例：

> ...has proved to his own satisfaction that seven points is the correct profit to take in Union Pacific, while losses should be limited to two and one-half points.

7 : 2.5 = **2.8 : 1**。**但这句不能给我们 2:1 背书**（2026-10-30 原文核对更正）：该段在**第五章**（`selden.txt:1201-1207`，不是第七章），且原文紧接着是 "Nothing could be more foolish than these arbitrary figures"——Selden 举它当**反面例证**，反对用固定点数设目标与止损。第八章的情绪结论：

> The greatest fault of ninety-nine out of one hundred active traders is being bullish at high prices and bearish at low prices.

**对我们的影响**：

1. 卡片 P3 的**递减加仓**（1 : 0.6 : 0.36）与 Selden 的**等距 6 档**相反。前提不同：Selden 描述的是价值型越跌越买（无止损，靠价格低于价值兜底），我们只在盈利时加仓。所以节奏**不可移植**，但"每档 = 总跨度/档数"的比例关系可拿来校核加仓间距——我们的递减序列等效约 3 档，若改成等距 6 档，单档风险要相应降到 1/6。
2. `market_phase` / 情绪类反向指标的原文依据**要收窄**（2026-10-30 核对）："高位看多、低位看空是 99/100 交易者的通病"确实在书里（`selden.txt:1943`），但同章 `:1387-1392` 明确否定"靠反转本身赚钱"，只允许**极值处准备反向/获利了结**这一弱用法。可以标 Selden 出处，但不许表述成"情绪反向指标有全书背书"。

### 本轮仍未打通的（如实记录）

| 目标 | 状态 | 说明 |
|---|---|---|
| Wyckoff 1910/1922/1931 原著 | **仍不可得** | Gutenberg 无此人交易著作（按作者检索只回 Walter A. Wyckoff 等无关书）；archive.org、HathiTrust、openlibrary 在新代理下仍是 000；Wikisource 检索无结果 |
| Elliott 1938《The Wave Principle》 | **不可得** | 同上，且 1938 年版权未确定过期，即便可达也不该整本落库 |
| #25《实战经济论》 | **书名仍未确认** | 微信读书站内无同名书；本轮联网检索亦无任何同名图书命中。判断为书单投递笔误或内部讲义，卡片按主题保留，不做原文归属 |
