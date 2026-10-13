#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/predict_algo.py ═══
# 上游 commit: 3e623ca669eb · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
预测算法 v1 — 多因子合成的次日涨跌概率模型 (qwerzxcva/stocks-algo)

⚠️ 诚实声明: 本模型不是水晶球。它把实测统计规律(晋级率/日历效应/宽度效应/连段结构)
合成为一个"次日方向偏多概率"估计, 并给出置信区间。所有概率来自历史频率,
历史频率≠未来保证。模型目标是: 让你只在高期望动作上出手。

因子库(全部实测, 见 docs/cycle-patterns.md + limitup-stats.md):
  f1 连段结构:   连跌1天→次日反抽为主; 连涨≥4天→次日风险陡增
  f2 星期效应:   周四-0.099% / 周五+0.100%
  f3 月度效应:   2月+0.181% / 10月-0.104%
  f4 宽度效应:   普涨日次日+0.119%/57% ; 普跌日次日-0.005%/51.8%
  f5 涨停情绪:   涨停家数所在区间(冰点/健康/亢奋)→不同次日分布
  f6 动量:       20日累计涨跌
  f7 个股特殊:   持仓/候选股的连板高度→晋级率(vs 断板概率)

输出: {"p_up": 0.xx, "expected_move": ±0.xx%, "confidence": 中/低, "drivers": [...]}
"""
from __future__ import annotations
import json, math, os, urllib.request
from datetime import datetime

TOKEN = os.environ.get("GH_TOKEN", "")
RAW = "https://raw.githubusercontent.com/qwerzxcva/t1/main"

BASE_P = 0.50  # 无信息先验(实测上证长期胜率≈50%)

def _headers():
    h = {"User-Agent": "stocks-algo",
         "Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.raw"}
    return h

def _get(url, timeout=90):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_headers()), timeout=timeout).read().decode()

def fetch_index_daily(code="sh000001"):
    # 优先 contents API (raw 域名本机不稳)
    fp = f"/tmp/stocks_shards/{code}.txt"
    if os.path.exists(fp):
        txt = open(fp).read()
    else:
        try:
            txt = _get(f"https://api.github.com/repos/qwerzxcva/t1/contents/data/index/history/{code}.txt")
        except Exception:
            txt = _get(f"{RAW}/data/index/history/{code}.txt")
        open(fp, "w").write(txt)
    rows = []
    for line in txt.splitlines():
        if line.startswith("#") or "|" not in line:
            continue
        f = line.split("|")
        if len(f) >= 7:
            rows.append({"date": f[1], "close": float(f[3])})
    return rows

def fetch_market_breadth_today() -> float | None:
    """今日主板上涨占比(从 data-live 云端快照, 可选)"""
    try:
        url = f"{RAW.replace('t1','t2')}/data/snapshot/latest.json"
        req = urllib.request.Request(url, headers=_headers())
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return d.get("up_ratio")
    except Exception:
        return None

def logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x))

def predict(target_date: str | None = None, up_ratio: float | None = None,
            zt_count: int | None = None, stock_height: int | None = None) -> dict:
    rows = fetch_index_daily()
    rets = [rows[i]["close"]/rows[i-1]["close"]-1 for i in range(1, len(rows))]
    last = rows[-1]
    dt = datetime.strptime(target_date or last["date"], "%Y-%m-%d")

    logit = 0.0
    drivers = []

    # f1 连段
    n, up = 0, rets[-1] > 0
    for r in reversed(rets):
        if (r > 0) == up:
            n += 1
        else:
            break
    if not up and n == 1:
        logit += 0.15; drivers.append(f"连跌1天→反抽常态(+)")
    elif up and n >= 4:
        logit -= 0.20; drivers.append(f"连涨{n}天→追高险(-)")
    elif up and n <= 2:
        logit += 0.05; drivers.append(f"连涨{n}天→2天内延续略偏多(+)")

    # f2 星期
    wd = dt.weekday()
    wmap = {0: ("周一", +0.10), 1: ("周二", -0.02), 2: ("周三", 0.0), 3: ("周四", -0.09), 4: ("周五", +0.10)}
    nm, adj = wmap.get(wd, ("?", 0.0))
    logit += adj * 1.5
    drivers.append(f"{nm}: 日历期望{adj:+.2f}%")

    # f3 月度
    mo = dt.month
    madj = {1:0, 2:+0.15, 3:-0.05, 4:+0.08, 5:-0.03, 6:0, 7:-0.01, 8:0, 9:+0.06, 10:-0.09, 11:+0.09, 12:0}[mo]
    logit += madj * 1.5
    if madj:
        drivers.append(f"{mo}月效应: {madj:+.2f}")

    # f4 宽度
    if up_ratio is None:
        up_ratio = fetch_market_breadth_today()
    if up_ratio is not None:
        if up_ratio > 0.8:
            logit += 0.12; drivers.append("昨日普涨(>80%)→次日惯性(+)")
        elif up_ratio < 0.2:
            logit += 0.03; drivers.append("昨日普跌→次日中性偏反抽")

    # f5 涨停情绪
    if zt_count is not None:
        if zt_count < 100:
            logit += 0.05; drivers.append("涨停冰点(<100)→情绪反转窗口(+)")
        elif zt_count > 500:
            logit -= 0.10; drivers.append("涨停亢奋(>500)→次日分化风险(-)")

    # f6 动量
    m20 = sum(rets[-20:]) * 100
    logit += max(-0.25, min(0.25, m20 * 0.01))
    drivers.append(f"20日动量{m20:+.1f}%")

    p_up = BASE_P + (logistic(logit * 2.0) - 0.5)  # 压缩到合理幅度
    p_up = max(0.30, min(0.70, p_up))

    exp_move = sum(rets) / len(rets) * 10 * (p_up - 0.5) * 4  # 粗略期望幅度

    return {
        "for_date": str(dt.date()) if hasattr(dt, "date") else target_date,
        "p_up": round(p_up, 3),
        "expected_move_pct": round(exp_move, 2),
        "confidence": "低" if abs(p_up - 0.5) < 0.05 else ("中" if abs(p_up - 0.5) < 0.12 else "高"),
        "drivers": drivers,
        "disclaimer": "历史频率合成, 非保证; 置信<中时不作为仓位依据",
    }

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    dt = args[0] if args and "-" in args[0] else None
    ur = float(args[1]) if len(args) > 1 and args[1].replace(".","").isdigit() else None
    zt = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
    print(json.dumps(predict(dt, ur, zt), ensure_ascii=False, indent=2))
