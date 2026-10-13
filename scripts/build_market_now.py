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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    now = datetime.now(CST)
    date_str = now.strftime("%Y%m%d")
    date_dash = now.strftime("%Y-%m-%d")
    print(f"[market_now] {now:%Y-%m-%d %H:%M:%S} CST", flush=True)

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

    # 涨跌停家数优先用涨跌分布口径 (全天准确, 含20cm/30cm涨停板)
    # 涨停池 tc 在收盘后只统计到盘中某一时刻 (2026-10-13 实测 67 vs 实际89)
    limit_up = zdfenbu.get("zt_like") or zt["count"]
    limit_down = zdfenbu.get("dt_like") or dtn["count"]
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": date_str,
        "source": "tencent+eastmoney (零鉴权实时)",
        "market": {
            "indices": indices,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "broken": zb["count"],
            "zt_rate": round(limit_up / max(1, limit_up + zb["count"]) * 100, 1),
            "advance": zdfenbu.get("up", 0),
            "decline": zdfenbu.get("down", 0),
            "flat": zdfenbu.get("flat", 0),
            "up_pct": zdfenbu.get("up_pct", 0),
            "turnover": turnover,
            "pool_zt_count": zt["count"],
            "pool_dt_count": dtn["count"],
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
            "caveat": "实时数据, 未做收盘校准; 仅供参考",
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
