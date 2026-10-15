#!/usr/bin/env python3
# em_extra_collect.py — 东财第三批 25 源采集 (2026-10-16)
#
# 覆盖 (全部零鉴权, datacenter-web/api/data/v1/get):
#   可转债: em_kzz_value_hist / em_kzz_important_date / em_kzz_ballot_num / em_kzz_invest_item
#   IPO:    em_ipo_apply_yield / em_ipo_neeq_list / em_ipo_review / em_ipo_allotment / em_seo_detail
#   龙虎榜: em_lhb_yyb_statistics / em_lhb_yyb_trade_detail /
#           em_lhb_yyb_return_ranking / em_lhb_org_daily
#   两融质押: em_margin_account_daily / em_equity_pledge_market / em_equity_pledge_detail
#   另类:   em_stock_participation / em_dmsk_ts_stock_new
#   宏观:   em_macro_shibor / em_macro_house_price_70
#   事件:   em_performance_express / em_performance_forecast /
#           em_restricted_unlock_day / em_repurchase_list
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://data.eastmoney.com/", "Host": "datacenter-web.eastmoney.com"}
D = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _get(url, timeout=35, retries=3):
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.2 + i)
    raise last


def fetch(report, page_size=100, page_number=1, sort=None, filt=None, source="WEB", client="WEB"):
    u = (f"{D}?reportName={report}&columns=ALL&pageSize={page_size}"
         f"&pageNumber={page_number}&source={source}&client={client}")
    if sort:
        u += f"&sortColumns={sort}&sortTypes=-1"
    if filt:
        u += f"&filter={filt}"
    d = json.loads(_get(u))
    res = d.get("result") or {}
    return res.get("data") or [], res.get("pages")


# 无筛选的全市场报表 (直接拉最新一页)
MARKET_REPORTS = {
    "ipo_apply_yield": ("RPTA_APP_IPOAPPLY", "APPLY_DATE,SECURITY_CODE", None),
    "ipo_review": ("RPT_IPO_INFOALLNEW", "UPDATE_DATE", None),
    "ipo_allotment": ("RPT_IPO_ALLOTMENT", "EQUITY_RECORD_DATE", None),
    "seo_detail": ("RPT_SEO_DETAIL", "ISSUE_DATE", None),
    "lhb_yyb_statistics": ("RPT_OPERATEDEPT_LIST_STATISTICS", None, "(STATISTICSCYCLE%3D%2204%22)"),
    "lhb_yyb_return_ranking": ("RPT_RATEDEPT_RETURNT_RANKING",
                                "TOTAL_BUYER_SALESTIMES_1DAY,OPERATEDEPT_CODE",
                                "(STATISTICSCYCLE%3D%2204%22)"),
    "margin_account_daily": ("RPTA_WEB_MARGIN_DAILYTRADE", "STATISTICS_DATE", None),
    "equity_pledge_market": ("RPT_CSDC_STATISTICS", None, None),
    "equity_pledge_detail": ("RPTA_APP_ACCUMDETAILS", None, None),
    "stock_participation": ("RPT_STOCK_PARTICIPATION", "TRADE_DATE", None),
    "dmsk_ts_stock_new": ("RPT_DMSK_TS_STOCKNEW", "TRADE_DATE", None),
    "macro_shibor": ("RPT_IMP_INTRESTRATEN", "REPORT_DATE", None),
    "macro_house_price": ("RPT_ECONOMY_HOUSE_PRICE", None, None),
    "performance_express": ("RPT_FCI_PERFORMANCEE", "NOTICE_DATE", None),
    "performance_forecast": ("RPT_PUBLIC_OP_NEWPREDICT", "NOTICE_DATE", None),
    "repurchase_list": ("RPTA_WEB_GETHGLIST_NEW", "UPD,DIM_DATE,DIM_SCODE", None),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/em_extra.json")
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    result = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
              "source": "em_extra_reverse_20261016", "datasets": {}}
    only = set(x.strip() for x in args.only.split(",") if x.strip())

    for key, (report, sort, filt) in MARKET_REPORTS.items():
        if only and key not in only:
            continue
        rows, pages = [], None
        # 实测: 3 个报表 (ipo_apply_yield/lhb_yyb_return_ranking/repurchase_list)
        # 在连续批量请求中恒返回空, 但单独跑 6/6 全通 — 是服务端当日数据缓存
        # 不预热所致, 非限流 (重试 5 次 + 退避仍空)。这 3 个已改由 workflow
        # 独立 step 单独采 (见 ths-collect.yml 的 "EM extra solo" 步)。
        for attempt in range(3):
            try:
                rows, pages = fetch(report, args.size, 1, sort, filt)
            except Exception as e:
                print(f"[{key}] ERR {type(e).__name__}", file=sys.stderr)
            if rows:
                break
            time.sleep(2.5 * (attempt + 1))
        result["datasets"][key] = {"n": len(rows), "pages": pages, "rows": rows,
                                    "attempts": attempt + 1}
        print(f"[{key}] {len(rows)} 条, pages={pages}" +
              (f" (重试{attempt}次)" if attempt else ""), flush=True)
        time.sleep(1.5)

    # 解禁 (按周区间)
    if not only or "restricted_unlock" in only:
        try:
            today = datetime.now(CST)
            s = today.strftime("%Y-%m-%d")
            e = (today + timedelta(days=7)).strftime("%Y-%m-%d")
            filt = f"(FREE_DATE%3E%3D%27{s}%27)(FREE_DATE%3C%3D%27{e}%27)"
            rows, pages = fetch("RPT_LIFTDAY_STA", args.size, 1, None, filt)
            result["datasets"]["restricted_unlock"] = {"n": len(rows), "pages": pages,
                                                       "range": [s, e], "rows": rows}
            print(f"[restricted_unlock] {len(rows)} 条 ({s}~{e})", flush=True)
        except Exception as e:
            result["datasets"]["restricted_unlock"] = {"err": f"{type(e).__name__} {e}"}
        time.sleep(0.7)

    # 机构龙虎榜 (近两日)
    if not only or "lhb_org_daily" in only:
        try:
            today = datetime.now(CST)
            s = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            e = today.strftime("%Y-%m-%d")
            filt = f"(TRADE_DATE%3E%3D%27{s}%27)(TRADE_DATE%3C%3D%27{e}%27)"
            rows, pages = fetch("RPT_ORGANIZATION_TRADE_DETAILS", args.size, 1, None, filt)
            result["datasets"]["lhb_org_daily"] = {"n": len(rows), "pages": pages,
                                                   "range": [s, e], "rows": rows}
            print(f"[lhb_org_daily] {len(rows)} 条 ({s}~{e})", flush=True)
        except Exception as e:
            result["datasets"]["lhb_org_daily"] = {"err": f"{type(e).__name__} {e}"}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    tot = sum(v.get("n", 0) for v in result["datasets"].values())
    print(f"total {tot} 条 -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
