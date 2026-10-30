# 已读登记（READ-LOG） — 2026-10-30 逐本精读与去重结果

> 这份文件只记「真的读了什么、读到哪、改了什么」，不记计划。判据正文仍在四批卡片里，
> 原文核对笔记在 `fulltext/`，跨书去重索引在 `DEDUPE-33.md`。

## 一、诚实的级别统计（33 本）

| 级别 | 定义 | 本数 |
|---|---|---|
| **A** | 有全文原文、可逐章逐句核对 | **1**（#27 操盘作手：回忆录） |
| **B** | 无全文，但有可打开的公开材料（目录页/简介原句/概念页） | **18** |
| **C** | 无任何原文、无任何公开材料，纯模型知识蒸馏 | **14** |

> 最容易被自欺的一句：「有 3 本公版全文已落盘」。落盘是事实，但**其中只有 1 本对得上 33 本书目**——
> Selden 与 Butler 是「源流对照本」（`SOURCE_AVAILABILITY.md:19-21` 的「对应书目」列已写明）。
> 所以：**33 本中真正逐本读过全文的，只有 #27 一本。**
> B 级里还有 3 本（#5 书名查无此书、#7 仅京东商品标题、#32 中文版无书目页）只有书目级材料，判据零佐证。

## 二、本轮逐章读完了哪 3 本（A 级 + 2 本源流本）

| 源文件 | 体量 | 覆盖 | 笔记 |
|---|---|---|---|
| `reminiscences_of_a_stock_operator.txt` | 11,102 行 | 全书；XVI 尾段/XVII/XXIII/XI 前段约 1,300 行只做检索抽样（笔记第五节已单列） | `fulltext/reminiscences.md`（165 处行号引用） |
| `psychology_of_the_stock_market_selden.txt` | 2,446 行 | 正文 8 章全读（L107/412/605/861/1120/1397/1625/1754 与目录一一对应），131 处引用做过行号↔首句校验 | `fulltext/selden.md`（14 条判据） |
| `successful_stock_speculation_butler.txt` | 2,201 行 | 26 章全读（含版权页/许可证） | `fulltext/butler.md`（190 处行号引用） |

## 三、可复现的关键事实（别只信结论，命令在这儿）

```bash
B=~/.aharou/knowledge/sources/books
grep -ci "pivotal" $B/reminiscences_of_a_stock_operator.txt          # 0
grep -ci "turning point" $B/reminiscences_of_a_stock_operator.txt    # 0
grep -ci "line of least resistance" $B/reminiscences_of_a_stock_operator.txt  # 21
sed -n '4498,4520p' $B/reminiscences_of_a_stock_operator.txt   # 首笔 1/5 → 等额 → 加倍（递增）
sed -n '21p;74p'    $B/psychology_of_the_stock_market_selden.txt  # 1912（此前记 1909）
sed -n '1195,1207p' $B/psychology_of_the_stock_market_selden.txt  # 7 点/2.5 点是反面例证
sed -n '1234,1262p' $B/successful_stock_speculation_butler.txt    # 反对止损的三前提 + 唯一例外
```

## 四、本轮据此回改的卡片（4 处，均已核对原文）

1. `SOURCE_AVAILABILITY.md`：Selden 年份 1909 → **1912**（原书版权页）；并注明 3 本落盘书里只有 #27 属本清单。
2. `SOURCE_AVAILABILITY.md`：「7 点利润 / 2.5 点止损 = 2.8:1，与我们 2:1 同量级」→ 改为**不能背书**：该段在第五章而非第七章，原文紧接
   「Nothing could be more foolish than these arbitrary figures」，是 Selden 举的**反面例证**。
3. `SOURCE_AVAILABILITY.md` + `SUMMARY-33.md`：「情绪极值处反向」收窄为弱用法（同章明确否定靠反转赚钱），且**不得挂在 #27 回忆录名下**（本书 0 次 pivotal）。
4. `batch3_trade_management.md`：P3 的递减加仓 `1 : 0.6 : 0.36` 明确标注**无原文依据、属工程选择**；回忆录原文是首笔 1/5→等额→加倍（**递增**，方向相反）。

## 五、仍未读清 / 不打算读的（如实）

- Reminiscences 的 XVI 尾段、XVII、XXIII、XI 前段约 1,300 行只抽样，相关结论未进入判据表。
- 31 本中文书**没有全文可读**（微信读书需会员且不抓正文），只能停在 B/C 级；C 级 14 本的判据全部缺原文支撑。
- 3 本源流本里无法编码的部分（直觉/hunch、"psychological moment"、缺失成交量）已在各笔记第五节登记，未混进判据表。
