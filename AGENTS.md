# AGENTS.md — stocks-ai-view 项目规则

## 一、子代理分工机制（强制，防止重复抓取/浪费额度）

**2026-10-13 教训**：三个子代理并行都去搜「股票数据 API」、都去测东财/新浪/雪球，
合计烧掉 2900 万 token，结论大量重复。用户明确要求：每个账号分开抓取、分开搜索。

### 派发前必做两步

```bash
# 1. 查哪些域还没人做（主会话只派这些）
python3 scripts/domain_lease.py --available

# 2. 认领（必须，否则视为违规派发）
python3 scripts/domain_lease.py --claim <agent名> <domain>
```

### 域清单（14 个）

`realtime_quote` / `kline` / `capital_flow` / `financial` / `news` / `social` /
`fund_etf` / `macro` / `events` / `exchange_official` / `index` / `sector` /
`algo` / `paid_api`

### 完成后必做

```bash
python3 scripts/domain_lease.py --release <agent名> <domain> --status done \
  --output <产出文件> --note "<结论摘要>"
```

- `done` 是**永久锁定**：该域不再重复派，除非 `--force`（会覆盖完成标记）
- 认领 24h 未完成会自动 `expired`，可被他人重派

### 探测节流（源级防重复）

```bash
# 某源 24h 内测过就跳过
python3 scripts/probe_throttle.py --check tencent_quote   # 退出码 1 = 跳过

# 测完登记
python3 scripts/probe_throttle.py --record <source_id> --code 200 --ok 1 --agent <名>
```

已实测的源都登记在 `probe_log.json`（68 个），**任何 agent 不要再重复实测**。

### 派发子代理时的 prompt 必须包含

1. 明确它负责的**唯一域**（`--claim` 过的那个）
2. 「已完成的域不要再碰」+ 指向 `domain_lease.json`
3. 「已实测的源不要再测」+ 指向 `probe_log.json`
4. 只写自己那个产出文件，不要动 `scripts/`
5. **落盘要早**：每探明 2-3 个接口就写盘（App 进程可能被关闭导致成果全丢）

## 二、数据准确性纪律（用户 2026-10-13 明确要求）

1. **页面消费的 JSON 必须带 `stale` 标记**（age_hours + 阈值），否则用户看到旧数据无法察觉
2. **必须带 `provenance`**：每个字段的 source + confidence + snapshot_ts
3. **改动后跑校验**：`python3 scripts/data_audit.py --check`
   （时效性 + 关键字段 vs 实时基准，基准走 push2his clist 绕 WAF）
4. **涨跌停口径统一**：用东财 `tc` 权威计数，宽口径值存 `*_wide`

## 三、爬取与逆向边界

- ✅ 正确 UA / Referer / Cookie（含匿名 session）+ 温和限频（1-2 QPS）
- ❌ CAPTCHA 破解、浏览器指纹伪装、代理池轮换
- 被 WAF 拦的：记录机制 + 给替代源，不试图打穿
- 本地被墙的源交给 GitHub Actions 出口（见 `.github/workflows/v2-extras-collect.yml`）

## 四、凭据

仓库 remote 内嵌的 token、各站 cookie 一律**不复述、不写进产出文件**，引用时打码。

## 五、Git

- Conventional Commits，推送前 `git pull --rebase`
- 主分支是 `main`（两个仓都是）
- 数据文件冲突时：以远端 Actions 新数据为基，本地审计字段（stale/provenance）叠加
- 不提交他人未提交改动；工作区有别人的改动时不 `git commit -a`

## 六、关键路径

| 用途 | 路径 |
|---|---|
| 数据源注册表（唯一事实来源） | `scripts/sources_registry.py` |
| 统一查询接口 | `scripts/query_api.py` |
| DB 层（DuckDB 11 表 20 索引） | `scripts/db_layer.py` |
| 周期池 v2 | `scripts/cycle_pool_v2.py` → `data/cycle_pool_v2.json` |
| 技能算法 | `scripts/skill_algos.py` |
| 数据校验 | `scripts/data_audit.py` |
| v2 采集管线 | `scripts/collect_v2_extras.py` |
| 集思录采集 | `scripts/collect_jisilu.py` |
| 目录/研究产出 | `~/workspace/stocks-data-live/data/research/` |
