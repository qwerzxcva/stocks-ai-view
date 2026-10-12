# 市场数据采集模块 (collect_market_extras.py)

## 解决什么问题

`data/latest.json` 由**两处**写入：

| 来源 | 频率 | 内容 |
|------|------|------|
| 本仓库 `scripts/ai_screener.py` + `run_all_v4.py` | 手动 / Actions | 五池 + picks + themes |
| 另一台服务器 `local realtime sync` | 每 10 分钟 | ai_picks / social / fund / moneyflow / cycle_v2 / westock 等 |

两边直接覆盖会导致：一边推的数据被另一边冲掉；远端那套代码没有本次修复时，
网站出现**跌停数显示 0**（真实 19）、涨跌家数/成交额/两融/龙虎榜全空。

## 用法

`collect_market_extras.py` 是**单文件、零依赖**采集模块，只产出新增字段，
用 `--merge` 就地合并，**不动其他模块**：

```bash
# 在另一台服务器 / GitHub Actions / 本地, 任选其一:

# 1) 就地合并进已有 latest.json (推荐, 保留全部原有字段)
python3 scripts/collect_market_extras.py --merge data/latest.json

# 2) 指定日期
python3 scripts/collect_market_extras.py --merge data/latest.json --date 20261012

# 3) 只生成新增字段的独立 JSON (调试用)
python3 scripts/collect_market_extras.py --out /tmp/extras.json
```

在 Python 代码里调用：

```python
from collect_market_extras import collect_all
extras = collect_all()            # 默认今天 CST
latest["market"].update(extras["market"])
latest.update({k: v for k, v in extras.items() if k != "market"})
```

## 采集的数据源 (全部零鉴权)

| 字段 | 来源 | 说明 |
|------|------|------|
| `market.limit_up/down/broken` | 东财 `push2ex getTopic{ZT,DT,ZB}Pool` | **读 `data.tc` 而非 `data.pool`** |
| `market.advance/decline/flat/up_pct` | 东财 `getTopicZDFenBu` | 涨跌家数分布 |
| `market.turnover` | 腾讯 `qt.gtimg.cn` p[37] | 沪深两市成交额（亿元） |
| `commentary` | 规则引擎（本地计算） | 放缩量/情绪/两融趋势/题材聚类/风险 |
| `ths_strong` | 同花顺 `zx.10jqka.com.cn` | 强势股 + 题材归因 |
| `lhb` | 东财 `RPT_DAILYBILLBOARD_DETAILS` | 龙虎榜席位解读 |
| `blocktrade` | 东财 `RPT_DATA_BLOCKTRADE` | 大宗交易溢价率 |
| `margin` | 东财 `RPTA_RZRQ_LSHJ` | 融资融券余额序列 |
| `index_kline` | 腾讯 `web.ifzq.gtimg.cn` | 上证指数历史日K |
| `westock.sector_ranking` | 腾讯 `proxy.finance.qq.com` | 行业板块涨幅/区间排行 |

## 两个关键 bug 修复

### 1. 跌停数显示 0

```python
# ❌ 错误: 只读 pool (东财 2026-10-12 返回 pool=[] 但 tc=19)
pool = (d.get("data") or {}).get("DTPool") or []
count = len(pool)          # → 0

# ✅ 正确: tc 是权威计数
tc = (d.get("data") or {}).get("tc")
count = int(tc) if tc is not None else len(pool)   # → 19
```

### 2. 龙虎榜/大宗交易返回 0 条

东财 `datacenter` 的 `filter` 需要 `YYYY-MM-DD`，而涨跌停池用 `YYYYMMDD`：

```python
date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"   # 2026-10-12
fetch_datacenter(..., date=date_dash)    # ✅ 28 页数据
fetch_datacenter(..., date=date_str)     # ❌ 0 条 (date_str=20261012)
```

## 网络可达性说明

同一份代码在不同环境结果不同（各源有 WAF/地区限制）：

| 环境 | 腾讯 | 东财 push2 | 东财 datacenter | 同花顺 |
|------|------|-----------|-----------------|--------|
| 本容器 | ✅ | ❌ 间歇 reset | ✅ | ✅ |
| 另一台服务器 | ✅ | ✅ | ✅ | ✅ |

代码对每个源独立 `try/except`，单源失败只留空对应字段，不影响其他源。
若某源长期不可用，前端对应 tab 显示"暂无数据"，不会整页报错。

## 前端对应

`index.html` 中：

- `renderCommentary()` → 盘面解读 tab（市场宽度条 + 关键指标 + 题材 chips + 风险）
- `renderVolChart()` → 两市成交量 SVG 线图（60 日 + 5 日均量虚线）
- `renderThsStrong()` / `renderLhb()` / `renderBlocktrade()` → 多池选股的三个新 tab
- `renderTencent()` → 腾讯数据 tab（行业资金排行）
