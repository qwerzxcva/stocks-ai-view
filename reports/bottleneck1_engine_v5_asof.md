# 瓶颈 1 修复报告：engine_v5 前视缺陷 → engine_v5_nolookahead.py 包装器

日期：2026-10-24 ｜ 执行：子代理（破甲，瓶颈1）

## 1. 问题确认

`scripts/engine_v5.py`（743 行，21 票打分引擎）**无 --asof 参数、无任何日期截断机制**：

- K 线加载：`load_klines(bulk_dir, limit)`（engine_v5.py:407）读 `data/bulk/day_qfq_*.csv` 全量历史，再用因子表/hfq 做复权覆盖，返回每只票截至**当前最新**的行列表 `[date,o,c,h,l,v]`。
- 打分流程：`main()`（engine_v5.py:592）→ 逐票 `score_stock()` → `build_votes()`（17 技术指标 + 3 外部增强票）。`build_votes` 全程只取序列末尾值（`_last()`），因此结果完全由**最新一根 K 线**决定 → 回测任意历史时点都给出"今天"的答案，即前视。
- CLI 参数仅 `--bulk/--out/--limit/--min-score/--no-ext/--main-board-only`（engine_v5.py:593-602），无 asof。
- 外部增强票（SectorResonance / CapitalFlowConsistency / InZTPool）读 `data/all_sources.json`、`data/moneyflow.json`——两者都是**当前快照文件**，无历史序列，回测时启用即前视。

R3 集成回测因此用 `--no-v5` 规避，集成只剩 2 引擎（arbiter 下游少 21 票信息源）。

## 2. 本体修改风险评估（结论：不改本体）

```bash
$ git -C ~/workspace/stocks-ai-view log --oneline -3 -- scripts/engine_v5.py
559077a news digest report 2026-10-21 (周期任务，自动分析)
```

- engine_v5.py 最近一笔提交在 2026-10-21 周期任务中（可能随批量提交带入），git status 干净，**当前无活跃改动**。
- 但它是旧主引擎，被其它脚本/另一个 AI 引用（本体注释即提到 optimizer、reflect 等下游）。在本体加参数即使默认行为不变，也存在 argparse 兼容、import 副作用等边际风险。
- **决策：采用包装器方案，engine_v5.py 本体零改动。**

## 3. 包装器设计（scripts/engines/engine_v5_nolookahead.py，110 行）

- **import 复用，不重实现**：`import engine_v5`（sys.path 注入 scripts/）+ `import _loader`（strategies/_loader.py，只读复用其 slice_asof 语义约定）。打分、排序、门禁、输出 100% 走本体代码路径。
- **截断点**：monkeypatch `engine_v5.load_klines`——先调原函数（保留因子表复权、freshness 门禁等全部行为），再对每只票行列表按 `date <= asof`（含当日）截断。升序数组遇首个 > asof 即 break，语义与 `_loader.slice_asof` 完全一致（K 线结构不同：本体是行列表，_loader 是列式 dict）。
- **强制 --no-ext（asof 模式）**：all_sources/moneyflow 为当前快照，无前视化手段，截断 K 线做不到的事不假装做到；asof 模式自动追加 --no-ext 并打印原因。
- **无 --asof 时直通** `engine_v5.main()`，参数原样透传 → 回归等价。
- CLI：本体全参数 + `--asof YYYY-MM-DD`（含格式校验）。

## 4. 无前视验证（两时点产出差异）

命令（各 --limit 60，约 1671 只入载）：

```bash
python3 scripts/engines/engine_v5_nolookahead.py --asof 2024-09-24 --out /tmp/v5_asof_20240924.json --limit 60
python3 scripts/engines/engine_v5_nolookahead.py --asof 2026-10-23 --out /tmp/v5_asof_20261023.json --limit 60
```

证据：

| 指标 | asof=2024-09-24 | asof=2026-10-23 |
|---|---|---|
| 截断计数 | **1645/1671 只 K 线被截断** | 0/1671（数据最新日即 10-23） |
| market_asof（输出内 max 截止日） | **2024-09-24**（无一根 > asof，截断严密） | 2026-10-23 |
| TOP10 | sz000555/000652/002184… | sz002368/002527/001328… |
| TOP50 重叠 | **0 / 50** | — |

逐只校验 sz002475（两时点均入榜）：2024-09-24 时点 K 线止于 2023-12-29（该票在分片中的实际最后 bar，bars=3201，price=33.82，score=76.9）；2026-10-23 时点 bars=3211，price=50.42，score=71.9。**同一只票、同一打分代码，输入数据不同 → 输出不同**，前视已消除。

## 5. 回归验证（不带 asof ≡ 原引擎）

```bash
python3 scripts/engine_v5.py                          --out /tmp/v5_orig.json        --limit 60 --no-ext
python3 scripts/engines/engine_v5_nolookahead.py      --out /tmp/v5_wrap_noasof.json --limit 60 --no-ext
```

两份输出 JSON **除 `ts` 时间戳外逐字节一致**（`o == w` → True，含 n_scanned/n_scored/top50/stale_top/data_quality 全字段）。TOP10 stdout 亦逐行一致。

## 6. 接入建议（给 R3 / arbiter 队友）

- 回测调用处把 `--no-v5` 换成：
  `python3 scripts/engines/engine_v5_nolookahead.py --asof <T> --out <v5_T.json> [--limit N]`
  输出 schema 与 engine_v5 完全一致（top/stale_top/score/resonance/votes），下游 parser 零改动。
- 注意 asof 模式下 21 票退化为 17 技术票（外部 3 票因快照数据无前视化手段被禁用），score 口径与实盘全开模式有系统性差异——回测横向比较时应统一用包装器口径。
- 已知局限：`--limit` 是"每分片取前 N 只"的采样语义，回测如需全市场请显式 `--limit 0`（=None，全量，耗时分钟级）。
