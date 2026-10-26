#!/usr/bin/env python3
"""
build_market_now.py — 生成独立的实时行情文件 data/market_now.json

为什么独立文件 (不写进 latest.json):
  远端服务器每 10 分钟推送 local realtime sync 覆盖 data/latest.json,
  任何工作流再写 latest.json 都会与它冲突/被覆盖。
  把实时行情单独放 market_now.json, 前端优先读它, 冲突面降为零。

内容: 指数/涨跌停/涨跌家数/成交额/两融/题材/实时快照摘要
用法: python3 build_market_now.py --out data/market_now.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collect_market_extras import (
    fetch_pool, fetch_zdfenbu, fetch_turnover, fetch_margin, fetch_indices,
    fetch_ths_strong_stocks, fetch_tx_sector_ranking)
from ai_screener import fetch_news, theme_of, sentiment_of, THEMES, fetch_fund_rank

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def _session_state(now):
    """判断当前是否在 A 股交易时段, 返回 (state, last_trading_date)。

    非交易时段 (含凌晨/午休/周末/假日) 采集器若按今天日期去查东财,
    拿到的是**上一个交易日的收盘缓存**, 却被打上今天的 date 标签 ——
    这正是「旧数据冒充新数据」的成因 (2026-10-16 实测: market_now.date=
    20261016 但涨停池 36 只与 latest.json 的 10-15 数据完全相同)。
    """
    mins = now.hour * 60 + now.minute
    dow = now.weekday()          # 0=Mon
    if dow >= 5:
        return "weekend", None
    if 555 <= mins <= 690:       # 09:15-11:30 上午盘
        return "trading", None
    if 780 <= mins <= 900:       # 13:00-15:00 下午盘
        return "trading", None
    if 690 < mins < 780:         # 11:30-13:00 午休
        return "lunch", None
    if mins < 555:               # 盘前 (00:00-09:15): 数据归属上一交易日
        return "pre_open", None
    return "post_close", None   # 15:00 之后: 当日已收盘定格


def _last_trading_date(now):
    """向前找最近一个**已完成**的工作日 (用于非交易时段的数据归属日)。

    周六/周日/周一盘前都要回退到周五, 且必须是过去的日期 ——
    周六 00:30 时 now-1day 是周五白天 (尚未收盘), 但周五的收盘数据
    此刻已经定格, 所以退一天即可; 周五盘中/盘前则退到周四。
    """
    d = now - timedelta(days=1)
    if d.weekday() >= 5:
        # 回退到上一个周五
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    # 若退一天后仍晚于当前时刻 (例如周五 20:00 查, d=周四 20:00 不会),
    # 只需保证 d <= now 的日期部分
    return d.strftime("%Y%m%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    now = datetime.now(CST)
    state, _ = _session_state(now)
    # 只有盘中才按今天查; 盘前/午休/周末回退到上一交易日,
    # 盘后 (15:00 之后) 用今天 —— 当天收盘数据此刻已定格,
    # 若退回昨天反而会丢掉刚产生的收盘池 (2026-10-16 实测)。
    if state in ("trading", "post_close"):
        date_str = now.strftime("%Y%m%d")
    else:
        date_str = _last_trading_date(now)
    is_live = state == "trading"
    date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    print(f"[market_now] {now:%Y-%m-%d %H:%M:%S} CST state={state} "
          f"query_date={date_str}" + (" (非交易时段, 数据归属上一交易日)"
                                     if not is_live else ""), flush=True)

    zt = fetch_pool("ZT", date_str)
    dtn = fetch_pool("DT", date_str)
    zb = fetch_pool("ZB", date_str)
    zdfenbu = fetch_zdfenbu(date_str)
    turnover = fetch_turnover()
    indices = fetch_indices()
    margin = fetch_margin(30)
    ths = fetch_ths_strong_stocks(date_dash)
    tx_rank = fetch_tx_sector_ranking(20)
    fund_rank = fetch_fund_rank(days=90, top=50) or []

    print(f"  ZT={zt['count']} DT={dtn['count']} ZB={zb['count']} "
          f"涨={zdfenbu.get('up')} 跌={zdfenbu.get('down')} "
          f"成交={turnover.get('total_yi')}亿 指数={len(indices)} 基金={len(fund_rank)}")

    # 题材热度 (新闻命中)
    news = fetch_news(60)
    theme_count = {}
    for n in news:
        for t in n.get("theme", []):
            theme_count[t] = theme_count.get(t, 0) + 1

    # 涨跌停家数主口径 = 东财涨停/跌停池 tc（2026-10-27 口径对齐）。
    # 旧实现用 ±10% 直方图桶和当主值，实测同一天系统性偏高：2026-10-26 直方图 77 vs 池 55，
    # 2026-10-23 171 vs 82（2.09 倍）。原因是平价阈值会把创业板/科创 +10~19% 未封板的票算成涨停，
    # 又完全看不到 ST 5% 涨停。直方图值降级为对照字段 limit_up_hist / limit_down_hist，只做交叉校验。
    limit_up = zt["count"]
    limit_down = dtn["count"]
    limit_up_hist = zdfenbu.get("zt_like")
    limit_down_hist = zdfenbu.get("dt_like")

    # 指数站上均线 (借鉴 tick-stock-panel 市场看板, 2026-10-17)
    # index_kline.json 是日线文件, 每轮重算成本可忽略。
    indices_ma = {}
    try:
        ik = json.load(open(os.path.join(DATA_DIR, "index_kline.json"),
                            encoding="utf-8"))
        for code, arr in ((ik.get("indices") or {}).items()):
            closes = [x["close"] for x in arr if x.get("close")]
            if len(closes) < 60:
                continue
            last = closes[-1]
            row = {"date": arr[-1].get("date"), "close": last}
            for n in (5, 20, 60):
                ma = sum(closes[-n:]) / n
                row[f"ma{n}"] = round(ma, 2)
                row[f"above_ma{n}"] = bool(last >= ma)
            indices_ma[code] = row
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": date_str,
        "session_state": state,
        "is_live": is_live,
        "collected_at": now.isoformat(timespec="seconds"),
        "source": "tencent+eastmoney (零鉴权实时)",
        "market": {
            "indices": indices,
            "limit_up": limit_up,
            "limit_down": limit_down,
            # ±10% 直方图桶和：平价阈值口径，仅作对照证据，不得当主值用
            "limit_up_hist": limit_up_hist,
            "limit_down_hist": limit_down_hist,
            "broken": zb["count"],
            "zt_rate": round(limit_up / max(1, limit_up + zb["count"]) * 100, 1),
            "advance": zdfenbu.get("up", 0),
            "decline": zdfenbu.get("down", 0),
            "flat": zdfenbu.get("flat", 0),
            "up_pct": zdfenbu.get("up_pct", 0),
            "turnover": turnover,
            "pool_zt_count": zt["count"],
            "pool_dt_count": dtn["count"],
            "indices_ma": indices_ma,   # 指数站上 MA5/20/60 (借鉴市场看板)
        },
        "limit_up_pool": zt["pool"][:50],
        "limit_down_pool": dtn["pool"][:30],
        "margin": margin,
        "ths_strong": ths[:40],
        "tx_sector_ranking": tx_rank,
        "fund_rank": fund_rank[:50],
        "themes": [{"name": t, "news_count": c}
                   for t, c in sorted(theme_count.items(), key=lambda x: -x[1])[:10]],
        "meta": {
            "freq": "交易时段每10分钟 (GitHub Actions)",
            "independence": "独立文件, 不与 latest.json 冲突 (远端 sync 只覆盖 latest.json)",
            "session_state": state,
            "is_live": is_live,
            "data_belongs_to": date_str,
            "caveat": ("非交易时段采集: 涨跌停/成交等字段为上一交易日收盘定格值, "
                        "指数价为最近快照; 已按真实归属日标注, 不用当日标签冒充"
                        if not is_live else
                        "实时数据, 未做收盘校准; 仅供参考"),
        },
    }

    out_path = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "market_now.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
