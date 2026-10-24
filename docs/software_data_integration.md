# 新引擎 → 软件数据链集成方案

> 2026-10-23 新增。目标：把 scripts/engines/ 下 10 个新引擎的产出接入 skapp app 数据链
> （现状：scripts/skapp_app_data.py 只消费 engine_fusion.json / engine_weights.json /
> engine_v5.json 三个旧引擎，新引擎产出软件端看不到）。
> 本文只做接入方案与新文件，不修改 skapp_app_data.py（红线）。

## 1. 引擎产出落盘路径表

所有引擎 `--out` 默认值已从 stdout 改为 `data/engines/<engine_name>.json`
（CLI 传 `--out <path>` 仍可覆盖；传 `--out -` 显式回到 stdout）。
落盘时自动 `mkdir -p data/engines`。

| 引擎文件 | 默认落盘 | 输出顶层关键字段 | 运行示例（小样本） |
|---|---|---|---|
| engines/regime_router.py | data/engines/regime_router.json | regime/regime_score/confidence/phase/regime_gate/routing/h_group_hard_cap/degraded | `python3 regime_router.py` |
| engines/cycle_grid_tester.py | data/engines/cycle_grid_tester.json | pivot_counts/grid_hypothesis_test/window_sensitivity/round_level_support_resistance/focus_level_stats/swing_amplitude_stats | `--sims 200` |
| engines/market_anomaly_engine.py | data/engines/market_anomaly_engine.json | anomalies/event_counts_in_window/hist_winrate_all_types/degraded | `--limit 300` |
| engines/price_level_engine.py | data/engines/price_level_engine.json | code/is_index/levels(10 类价位)/validate(可选) | `--index sh000001` 或 `--code sh600522` |
| engines/tomorrow_candidate_engine.py | data/engines/tomorrow_candidate_engine.json | candidates(≤top)/scanned/weights/output_filter | `--limit 300` |
| engines/morning_pick_engine.py | data/engines/morning_pick_engine.json | picks(3~5)/trade_date/weights/gap_env 状态 | `--limit 300` |
| engines/central_orchestrator.py | data/engines/central_orchestrator.json | regime/routing/rounds/top(终审榜)/llm/feedback_updater/data_asof/degraded | `--limit 300 --no-v5 --top 10 --sims 100` |
| engines/llm_pick_arbiter.py | data/engines/llm_pick_arbiter.json | model_chain(actual/degraded/tried)/picks(裁决后)/meta | `--input <中枢输出>`（需 top30/candidates 键） |
| engines/feedback_updater.py | data/engines/feedback_updater.json | version/updated/weights_after/converged/settled_records/sources | （权重版本另写 engines/weights/v{n}.json + latest.json） |
| engines/anai_adapter.py | data/engines/anai_adapter.json | mode/data(search 或 share 快照)/robots_note | `--search 中天` / `--code 600522` |

说明：
- price_level_engine 是按标的单跑的（--code/--index 二选一必填），多标的产出需要调用方
  显式指定不同 `--out`（建议命名 `price_level_engine_<code>.json`），否则互相覆盖；
  feed 聚合脚本按 `price_level_engine*.json` 通配收集。
- llm_pick_arbiter 输入契约是 `{top30| candidates: [...]}`，central_orchestrator 输出键是
  `top`，喂入前需改键（`{"top30": orch["top"]}`）或由调用方适配。

## 2. data/engines_feed.json 聚合结构

由 scripts/skapp_engines_feed.py 生成（独立可运行，文件头已注明
「供 skapp_app_data.py 后续接入」）。缺文件/解析失败的键置 null 并记入 degraded[]。

```json
{
  "ts": "ISO8601 (CST)",
  "source_dir": "engines",
  "regime":            {"engine","data_asof","regime","regime_score","confidence",
                        "phase","phase_degraded","gates","routing","h_group_hard_cap","degraded"} | null,
  "cycle":             {"engine","data_asof","hypothesis_test","focus_level_stats",
                        "swing_amplitude_stats","round_level_support_resistance"} | null,
  "anomalies":         {"engine","data_asof","asof","events","event_counts_in_window",
                        "conditional_winrate","degraded"} | null,
  "price_levels":      {"<code>": {"data_asof","is_index","levels","validate"}} | null,
  "orchestrator_top":  {"engine","asof","rounds","top","llm","degraded"} | null,
  "llm_verdict":       {"engine","model_chain","verdicts","data_asof","meta"} | null,
  "feedback":          {"engine","version","updated","converged","weights",
                        "summary":{reason?,settled_records?,lookback?,cold_start?,sources?},
                        "degraded"} | null,
  "tomorrow_candidates": {"engine","data_asof","trade_date","picks","degraded"} | null,
  "morning_picks":       {"engine","data_asof","trade_date","picks","degraded"} | null,
  "degraded":          ["..."] | null
}
```

运行：`python3 scripts/skapp_engines_feed.py [--out data/engines_feed.json]`。

## 3. skapp_app_data.py 未来接入点（只读分析，未改该文件）

已通读 skapp_app_data.py（574 行）核实真实输出结构：

**现状链路**：main() 从远端 `data/engine_fusion.json / engine_weights.json / engine_v5.json`
（skapp_app_data.py:152-154）拉分，汇入 `scores[code]` 字典（:234-253），最终写进
`data/api/stocks/<code>.json` 的 `engine_scores` 字段（:257-264, :546-547）。
大盘侧 `data/api/market.json`（:533-542）目前只有
`{ts, market, phase(=news_digest.sentiment), hot_news, news_digest, sources}`。

**建议接入点（改动权在 skapp_app_data.py 维护者，本文不实施）**：

1. `data/api/stocks/<code>.json`：
   - `engine_scores` 合并：在 :234-253 的 scores 汇总段追加
     `feed["orchestrator_top"]["top"]`（S 综合分）、`feed["tomorrow_candidates"]["picks"]`、
     `feed["morning_picks"]["picks"]` 的分值，键名建议 `orchestrator` / `tomorrow_v1` / `morning_v1`，
     与现有 `engine_v5` / `composite` 平级，app 端无需改解析即可透传。
   - 新增 `engines` 块（docstring :12 已预留该词）：`price_levels`（feed.price_levels[code]，
     个股支撑/阻力等 10 类价位）、`llm_verdict`（该股在 feed.llm_verdict.verdicts 中的裁决）。
2. `data/api/market.json`：新增两个顶层键
   - `regime`：feed.regime（regime/phase/gates/confidence），与现有 `phase`
     （news_digest.sentiment 口径）并存不冲突；
   - `anomaly`：feed.anomalies（events + conditional_winrate）；
   - 可选 `cycle`：feed.cycle.focus_level_stats（百点网格关口命中统计）。
3. 数据源声明：对应追加到 market.json 的 `sources` 列表与 index.json 的 `endpoints`
   （如新增 `engines_feed: "data/engines_feed.json"`）。
4. CI：skapp-app-data.yml（.github/workflows，本次未动）需要在跑 skapp_app_data.py 之前
   先跑 10 引擎 + skapp_engines_feed.py；各引擎依赖 stocks-data-kline/live 本地数据，
   需确认 CI 环境是否有 checkout/缓存，否则引擎 degraded 落盘，feed 会以 null+degraded 如实透传。

## 4. skapp-app 端需新增的数据类（Kotlin）

参照 app 端现有 `data/BanProb.kt` 模式：org.json 手工解析（项目未引 kotlinx.serialization）、
data class + companion Client、`optXxx`/`isNull` 容错、null=数据不足绝不臆造、
远端 URL 失败静默降级。建议新增：

- `data/EnginesFeed.kt`
  - `data class RegimeInfo(regime, regimeScore, confidence, phase, gates, degraded)`
  - `data class AnomalyEvent(type, ...)` + `data class Anomalies(events, conditionalWinrate, degraded)`
  - `data class PriceLevel(kind, price, touches?, confidence?, degraded?)` +
    `data class PriceLevels(code, levels: Map<String, Any>)`
  - `data class OrchestratorPick(code, S, gate, dimVotes)`（orchestrator_top.top 元素）
  - `object EnginesFeedClient { suspend fun load(): EnginesFeed? }`
    URL: `.../stocks-ai-view/main/data/engines_feed.json`，整体解析失败返回 null（同 BanProbClient）。
- 个股详情接入：`api/stocks/<code>.json` 新增 `engines.price_levels` 后，
  对应 `data/StockDetail.kt`（现有 record 解析处）加一个 `priceLevels` 可空字段。
- ViewModel 侧参照 SkappViewModel 中 BanProb 的加载方式（异步 load + 可空状态）。

## 5. 冒烟验证结论（2026-10-23 实跑）

- 10 引擎全部默认落盘成功（anomaly/tomorrow/morning/orchestrator 用 --limit 300 小样本，
  cycle 用 --sims 200），data/engines/ 下 10 个 JSON 字段齐全。
- `python3 scripts/skapp_engines_feed.py` → engines_feed.json 11 个键全非 null，
  degraded=null；删掉任一引擎 JSON 重跑，对应键 null 且 degraded 记录，恢复后复原。
- llm_pick_arbiter 无可用 LLM 通道时如实落 `model_chain.actual=rule_fallback, degraded=true`。
