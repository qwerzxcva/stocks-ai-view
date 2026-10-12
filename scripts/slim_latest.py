#!/usr/bin/env python3
"""
slim_latest.py — 裁剪 latest.json 中前端不使用的字段, 降低首屏传输量。

字段白名单与 index.html 渲染逻辑逐一核对得出 (index.html:458-532), 不靠猜测。
原文件备份为 latest.full.json (保留全量字段, 供调试/后续算法使用)。

用法:
  python3 scripts/slim_latest.py            # 从 latest.full.json 重新生成
  python3 scripts/slim_latest.py --restore  # 还原为全量

典型效果: 68.6KB -> 45.7KB (-33%)
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
SRC = os.path.join(DATA, "latest.json")
BACKUP = os.path.join(DATA, "latest.full.json")

# 池级保留字段 (对应 index.html renderPools 各分支)
POOL_KEEP = {
    "cycle": ["market_phase", "macro_signals", "top_sectors"],
    "sector_pool": ["tier1_count", "tier2_count", "tier1", "market_phase"],
    "lowpool": ["strong_buy", "watch", "strong", "total_filtered"],
    "hotspot_infer": ["strong_count", "watch_count", "strong", "themes_processed"],
    "multi_v4": ["tier_buy_count", "tier_watch_count", "tier_avoid_count",
                 "buy", "market_phase"],
}

# 列表元素保留字段 — 严格按 index.html 模板字面量读取的键列出
ITEM_KEEP = {
    ("lowpool", "strong"): ["code", "name", "industry", "price", "chg_pct",
                            "price_pct", "signals"],
    ("lowpool", "watch"): ["code", "name", "industry", "price", "chg_pct",
                           "price_pct", "signals"],
    ("hotspot_infer", "strong"): ["code", "name", "theme", "industry", "price",
                                  "chg_pct", "score", "signals"],
    ("multi_v4", "buy"): ["code", "name", "industry", "price", "chg_pct",
                          "score", "signals"],
    ("sector_pool", "tier1"): ["sector", "score", "signals", "avg_chg_1d",
                               "win_rate", "stock_count"],
    ("cycle", "top_sectors"): None,   # None = 原样保留
}

TOP_ITEM_KEEP = {
    "fund_rank": ["code", "name", "nav", "daily_chg_pct", "ret_1m", "ret_3m",
                  "ret_6m", "ret_1y"],
    "picks": ["code", "name", "price", "chg_pct", "lianban", "zt_time",
              "theme_hit", "rps5", "rps20", "score"],
    "limit_up_pool": ["code", "name", "lianban", "zt_time", "chg_pct"],
    "news_recent": ["title", "sentiment", "theme", "ts", "src", "source", "digest"],
}


def slim(items, keep):
    return [{k: it[k] for k in keep if k in it} if isinstance(it, dict) else it
            for it in items]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true", help="还原为全量 latest.json")
    args = ap.parse_args()

    if args.restore:
        if not os.path.exists(BACKUP):
            print("no backup found", file=sys.stderr)
            return 1
        with open(BACKUP, encoding="utf-8") as f:
            d = json.load(f)
        with open(SRC, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        print("restored latest.json from latest.full.json")
        return 0

    src = BACKUP if os.path.exists(BACKUP) else SRC
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    before = len(json.dumps(d, ensure_ascii=False))

    if src == SRC:   # 首次运行, 先备份全量
        with open(BACKUP, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        print(f"backup -> {BACKUP}")

    pools = d.get("pools")
    if isinstance(pools, dict):
        for name, keep in POOL_KEEP.items():
            p = pools.get(name)
            if not isinstance(p, dict):
                continue
            pools[name] = {k: v for k, v in p.items() if k in keep}
            for (pn, fn), ik in ITEM_KEEP.items():
                if pn == name and ik is not None and isinstance(pools[name].get(fn), list):
                    pools[name][fn] = slim(pools[name][fn], ik)

    for key, keep in TOP_ITEM_KEEP.items():
        if isinstance(d.get(key), list):
            d[key] = slim(d[key], keep)

    # 新增数据块的字段裁剪 (与 renderTencent/renderVolChart 等前端渲染核对)
    if isinstance(d.get("index_kline"), list):
        d["index_kline"] = slim(d["index_kline"], ["date", "close", "vol"])
    if isinstance(d.get("margin"), list):
        d["margin"] = slim(d["margin"], ["date", "rzye", "rqye", "rzrqye", "rzmre", "rzche"])
    if isinstance(d.get("lhb"), list):
        d["lhb"] = slim(d["lhb"], ["code", "name", "close", "chg", "deal_amt",
                                   "expl", "seats", "d1", "d2", "d5"])
    if isinstance(d.get("blocktrade"), list):
        d["blocktrade"] = slim(d["blocktrade"],
                               ["code", "name", "price", "premium", "vol", "amt", "deals"])
    if isinstance(d.get("ths_strong"), list):
        d["ths_strong"] = slim(d["ths_strong"], ["code", "name", "reason", "price",
                                                 "chg_pct", "turnover", "mktcap",
                                                 "pe", "pb", "score", "in_zt",
                                                 "quote_time"])
    if isinstance(d.get("enrich_meta"), dict):
        em = d["enrich_meta"]
        d["enrich_meta"] = {k: em[k] for k in ["lhb_date", "lhb_fallback",
                                               "blocktrade_date", "blocktrade_fallback"]
                            if k in em}
    ws = d.get("westock")
    if isinstance(ws, dict) and isinstance(ws.get("sector_ranking"), dict):
        sr = ws["sector_ranking"].get("data")
        if isinstance(sr, list):
            ws["sector_ranking"]["data"] = slim(
                sr, ["name", "code", "changePct", "chg5d", "chg20d",
                     "leader", "leaderPct", "mainNetInflow", "mainNetInflow5d"])

    after = len(json.dumps(d, ensure_ascii=False))
    with open(SRC, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    print(f"latest.json: {before} -> {after} bytes (-{(1 - after / before) * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
