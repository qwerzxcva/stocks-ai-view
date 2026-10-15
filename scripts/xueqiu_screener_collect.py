#!/usr/bin/env python3
# xueqiu_screener_collect.py — 雪球选股器全市场采集 (2026-10-15 逆向)
#
# 端点: https://xueqiu.com/service/v5/stock/screener/quote/list
#        ?page={n}&size={n}&order=desc&order_by={field}&market=CN
# 实测 (2026-10-15):
#   - 单次 size=100 返回 100 行; count=5000 (A股) / 2919 (港股) / 5000 (美股)
#   - 40 个字段, 含 pe_ttm/pb_ttm/ps/pcf/roe_ttm/net_profit_cagr/income_cagr/
#     dividend_yield/turnover_rate/amplitude/volume_ratio/limitup_days/north_net_inflow/
#     main_net_inflows/followers/total_shares/float_shares/market_capital/float_market_capital
#   - order_by 有效字段: percent/market_capital/pe_ttm/pb_ttm/volume/amount/
#     turnover_rate/amplitude/north_net_inflow/net_profit_cagr (9 个实测通过)
#   - 筛选参数 (pe/pb/percent/market_capital/limitup_days/roe_ttm/net_profit_cagr)
#     **不生效** — count 恒为全量, 需本地过滤
#   - 无需 cookie, 只需 Referer: https://xueqiu.com/
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HDR = {"Referer": "https://xueqiu.com/", "Host": "xueqiu.com"}
BASE = "https://xueqiu.com/service/v5/stock/screener/quote/list"

ORDER_FIELDS = ["percent", "market_capital", "pe_ttm", "pb_ttm", "volume", "amount",
                "turnover_rate", "amplitude", "north_net_inflow", "net_profit_cagr"]


def fetch_page(page, size, market, order_by):
    u = (f"{BASE}?page={page}&size={size}&order=desc"
         f"&order_by={order_by}&market={market}")
    h = dict(UA)
    h.update(HDR)
    r = urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=25)
    return json.loads(r.read().decode("utf-8", "replace"))


def collect(market="CN", size=100, max_pages=60, order_by="percent"):
    """按页拉全量 (count/size 页)"""
    first = fetch_page(1, size, market, order_by)
    total = first["data"]["count"]
    rows = list(first["data"]["list"])
    pages = min(max_pages, (total + size - 1) // size)
    print(f"[{market}] count={total} pages={pages}", file=sys.stderr)
    for p in range(2, pages + 1):
        for attempt in range(3):
            try:
                d = fetch_page(p, size, market, order_by)
                rows.extend(d["data"]["list"])
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  p{p} fail: {type(e).__name__}", file=sys.stderr)
                time.sleep(1.5)
        time.sleep(0.35)
        if p % 10 == 0:
            print(f"  ... p{p}/{pages} rows={len(rows)}", file=sys.stderr)
    return total, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/xq_screener.json")
    ap.add_argument("--markets", default="CN")
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--order-by", default="percent")
    args = ap.parse_args()

    result = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
              "source": "xueqiu_screener_reverse_20261015", "markets": {}}
    for m in [x.strip() for x in args.markets.split(",") if x.strip()]:
        try:
            total, rows = collect(m, args.size, args.max_pages, args.order_by)
            result["markets"][m] = {"count": total, "fetched": len(rows), "rows": rows}
            print(f"[{m}] fetched {len(rows)}/{total}", file=sys.stderr)
        except Exception as e:
            result["markets"][m] = {"error": f"{type(e).__name__} {e}"}
        time.sleep(1.0)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    tot = sum(v.get("fetched", 0) for v in result["markets"].values())
    print(f"total {tot} rows -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
