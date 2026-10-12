#!/usr/bin/env python3
"""
tv_screener.py — TradingView 选股器数据源 (零鉴权, 无需登录)

来源: 借鉴 github.com/Mathieu2301/TradingView-API (5572 star) 的 screener 接口
接口: POST https://scanner.tradingview.com/{market}/scan
实测: market=china → 7476 只 A 股全量, 返回 PE/PB/RSI/市值/行业/技术评级

为什么重要:
  1. 东财 push2 在容器内被 WAF 拦, 这是可用的替代源
  2. 提供 Recommend.All/MA/Other (TradingView 技术买卖评级), 我们原来没有
  3. 支持服务端条件筛选 (涨幅/市值/PE/RSI...), 不用拉全量再本地过滤
  4. 提供 sector/industry 字段 → 可补「个股→行业」映射 (此前因东财被封而缺失)

用法:
  python3 tv_screener.py --probe                 # 探测可用字段
  python3 tv_screener.py --top 20                # 市值前20
  python3 tv_screener.py --filter up5            # 涨幅>5% 且市值>100亿
  python3 tv_screener.py --rating                # 按 TV 技术评级排序
  python3 tv_screener.py --industry-map          # 构建 个股→行业 映射
  python3 tv_screener.py --out result.json
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
       "Content-Type": "application/json",
       "Origin": "https://www.tradingview.com"}
BASE = "https://scanner.tradingview.com"

# 已验证可用的字段 (2026-10-13 实测)
# 注意: TV 的 china 市场
#   - name 返回代码而非中文名; description 返回英文名
#   - price_earnings_ttm / relative_strength_index_14 恒为 null (TV 对A股不提供)
#   - price_book_ratio 可用
#   - type/subtype 可区分 stock/common vs etf/fund
#   - update_mode = delayed_streaming_900 → 数据延迟 15 分钟 (非实时!)
COLS_BASIC = ["name", "description", "close", "change", "volume",
              "market_cap_basic", "price_book_ratio",
              "average_volume_10d_calc",
              "sector", "industry", "type", "subtype"]
COLS_RATING = ["name", "description", "close", "change", "type", "subtype",
               "Recommend.All", "Recommend.MA", "Recommend.Other"]

# 数据延迟标注 (TV update_mode: delayed_streaming_900 = 15分钟)
DATA_DELAY_NOTE = "TradingView scanner 延迟约15分钟 (delayed_streaming_900), 非实时源"


def scan(market, columns, sorts=None, filters=None, limit=50, range_start=0):
    """单页查询。filters: [{"left":..., "operation":..., "right":...}]"""
    body = {"columns": columns, "range": [range_start, range_start + limit],
            "filter": filters or []}
    if sorts:
        body["sort"] = sorts
    url = f"{BASE}/{market}/scan"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=HDR, method="POST")
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return d
    except urllib.error.HTTPError as e:
        print(f"[tv] HTTP {e.code}: {e.read()[:150]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[tv] {type(e).__name__}: {e}", file=sys.stderr)
        return None


def to_rows(d, columns):
    """把 TV 响应转成 [{symbol, code, ...}]"""
    if not d or not d.get("data"):
        return []
    out = []
    for row in d["data"]:
        sym = row.get("s", "")
        # SSE:601398 → 601398 ; SZSE:000001 → 000001
        code = sym.split(":", 1)[1] if ":" in sym else sym
        mkt = sym.split(":", 1)[0] if ":" in sym else ""
        rec = {"symbol": sym, "code": code, "market": mkt}
        for i, c in enumerate(columns):
            rec[c] = row["d"][i] if i < len(row["d"]) else None
        out.append(rec)
    return out


# ─── 常用查询 ────────────────────────────────────────────────────
def query_all(limit=50, market="china"):
    """全市场按市值降序 (仅普通股, 排除 ETF/基金)。"""
    d = scan(market, COLS_BASIC,
             filters=[{"left": "type", "operation": "equal", "right": "stock"}],
             sorts={"sortBy": "market_cap_basic", "sortOrder": "desc"}, limit=limit)
    return to_rows(d, COLS_BASIC)


def query_rating(limit=20, market="china"):
    """按 TradingView 技术评级降序 (强烈买入优先)。已过滤 ETF/基金。"""
    d = scan(market, COLS_RATING,
             filters=[{"left": "type", "operation": "equal", "right": "stock"}],
             sorts={"sortBy": "Recommend.All", "sortOrder": "desc"}, limit=limit)
    return to_rows(d, COLS_RATING)


def query_filter_up_big(min_chg=5.0, min_cap=1e10, limit=20, market="china"):
    """涨幅达标 且 市值达标 且 是普通股。"""
    d = scan(market, ["name", "description", "close", "change", "market_cap_basic",
                      "price_book_ratio", "sector", "Recommend.All"],
             filters=[
                 {"left": "type", "operation": "equal", "right": "stock"},
                 {"left": "change", "operation": "greater", "right": min_chg},
                 {"left": "market_cap_basic", "operation": "greater", "right": min_cap},
             ],
             sorts={"sortBy": "change", "sortOrder": "desc"}, limit=limit)
    return to_rows(d, ["name", "description", "close", "change", "market_cap_basic",
                       "price_book_ratio", "sector", "Recommend.All"])


def query_low_valuation(max_pb=1.5, min_cap=1e10, limit=20, market="china"):
    """低估值筛选 (PB<1.5 且 市值>100亿 且 普通股)。

    注: TV 对 A 股不提供 PE, 故只用 PB。
    """
    d = scan(market, ["name", "description", "close", "change", "market_cap_basic",
                      "price_book_ratio", "sector"],
             filters=[
                 {"left": "type", "operation": "equal", "right": "stock"},
                 {"left": "price_book_ratio", "operation": "less", "right": max_pb},
                 {"left": "market_cap_basic", "operation": "greater", "right": min_cap},
             ],
             sorts={"sortBy": "price_book_ratio", "sortOrder": "asc"}, limit=limit)
    return to_rows(d, ["name", "description", "close", "change", "market_cap_basic",
                       "price_book_ratio", "sector"])


def build_industry_map(pages=8, per_page=1000, market="china"):
    """分页拉全市场, 构建 个股代码 → (sector, industry, type, name_en) 映射。

    7476 只分 8 页 (每页1000)。输出可直接给 build_lowpool / multi_screener 用。
    注意: 只含 TV 提供的英文名与行业分类, 中文名需另从腾讯/东财补。
    """
    mapping = {}
    for p in range(pages):
        d = scan(market, ["name", "description", "sector", "industry",
                          "type", "subtype"],
                 filters=[{"left": "type", "operation": "equal", "right": "stock"}],
                 sorts={"sortBy": "market_cap_basic", "sortOrder": "desc"},
                 limit=per_page, range_start=p * per_page)
        rows = to_rows(d, ["name", "description", "sector", "industry",
                           "type", "subtype"])
        if not rows:
            break
        for r in rows:
            mapping[r["code"]] = {
                "name_en": r.get("description"),
                "sector": r.get("sector"),
                "industry": r.get("industry"),
                "type": r.get("type"),
                "subtype": r.get("subtype"),
                "market": r.get("market"),
            }
        print(f"  page {p+1}/{pages}: +{len(rows)} → 共 {len(mapping)}", flush=True)
        if len(rows) < per_page:
            break
    return mapping


def load_cn_names():
    """从本地腾讯快照读 代码→中文名 (TV 的 china 市场只返回英文名)。"""
    import glob
    out = {}
    snaps = sorted(glob.glob("/root/workspace/stocks-data-live/data/market/snapshot_*.json"))
    if not snaps:
        return out
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
        for k, v in d.items():
            bare = k[2:] if k[:2] in ("sh", "sz", "bj") else k
            out[bare] = v.get("name", "")
    except Exception:
        pass
    return out


# ─── main ─────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="探测总字段数与样例")
    ap.add_argument("--top", type=int, default=0, help="市值前N")
    ap.add_argument("--rating", type=int, default=0, help="TV技术评级前N")
    ap.add_argument("--filter", dest="filt",
                    help="up5=涨幅>5%%且市值>100亿; lowpe=低估值 (%%需转义)")
    ap.add_argument("--industry-map", action="store_true", help="构建个股→行业映射")
    ap.add_argument("--out", help="输出 JSON 路径")
    ap.add_argument("--market", default="china")
    args = ap.parse_args()

    result = None
    label = ""

    if args.probe:
        d = scan(args.market, ["name"], limit=1)
        total = (d or {}).get("totalCount", 0)
        print(f"market={args.market}  totalCount={total}")
        rows = query_all(3)
        print("样例:")
        for r in rows:
            print(f"  {r['symbol']:18s} {r.get('name')} close={r.get('close')} "
                  f"PE={r.get('price_earnings_ttm')} PB={r.get('price_book_ratio')} "
                  f"RSI={r.get('relative_strength_index_14')} "
                  f"sector={r.get('sector')} industry={r.get('industry')}")
        return 0

    if args.industry_map:
        print("构建 个股→行业 映射 (分页拉全市场)...")
        mapping = build_industry_map(market=args.market)
        out = args.out or "/root/workspace/stocks-data-live/data/meta/industry_map_tv.json"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        json.dump(mapping, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"\nDONE: {len(mapping)} 只 → {out}")
        # 抽样
        for c in ["600519", "000001", "601127", "600825"]:
            if c in mapping:
                print(f"  {c} → sector={mapping[c]['sector']} "
                      f"industry={mapping[c]['industry']} name_en={mapping[c].get('name_en')}")
        return 0

    if args.top:
        result, label = query_all(args.top, args.market), f"市值前{args.top}"
    elif args.rating:
        result, label = query_rating(args.rating, args.market), f"TV技术评级前{args.rating}"
    elif args.filt == "up5":
        result, label = query_filter_up_big(), "涨幅>5%% 且 市值>100亿 且 普通股"
    elif args.filt == "lowpe":
        result, label = query_low_valuation(), "PB<1.5 且 市值>100亿 且 普通股"
    else:
        ap.print_help()
        return 1

    # TV 的 china 市场只给英文名, 中文名从本地腾讯快照补 (若有)
    cn_names = load_cn_names()
    if cn_names:
        for r in result:
            r["name_cn"] = cn_names.get(r["code"], "")

    print(f"=== {label} (共{len(result)}只) ===")
    for r in result:
        nm = r.get("name_cn") or r.get("description") or r.get("name")
        extra = ""
        if r.get("price_book_ratio"):
            extra += f" PB={r['price_book_ratio']:.2f}"
        if "Recommend.All" in r and r.get("Recommend.All") is not None:
            extra += f" 评级={r['Recommend.All']:.2f}"
        if r.get("sector"):
            extra += f" {r['sector']}"
        cap = r.get("market_cap_basic")
        cap_s = f"{cap/1e8:.0f}亿" if isinstance(cap, (int, float)) else "?"
        print(f"  {r['code']} {str(nm)[:14]:14s} "
              f"close={r.get('close'):<8} chg={r.get('change'):+.2f}% 市值={cap_s}{extra}")

    print(f"\n注: {DATA_DELAY_NOTE}")

    if args.out:
        json.dump(result, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
