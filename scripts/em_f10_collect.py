#!/usr/bin/env python3
# em_f10_collect.py — 东财 F10/研报/机构数据采集 (2026-10-16 逆向成果)
#
# 实测 15 个端点全 200, 全部零鉴权:
#   A. emweb.securities.eastmoney.com/PC_HSF10/{page}/PageAjax?code={SH|SZ}{code}
#      ShareholderResearch    36KB  户数+十大股东+十大流通+机构+基金+实控人 (一次拿全)
#      CompanySurvey          2.8K  公司概况/高管/曾用名/发行信息
#      CapitalStockStructure  14K   股本结构+历年变动全史+限售解禁
#      BonusFinancing         4.1K  分红送配+历年分红融资 (可算股息率)
#      BusinessAnalysis       68K   主营构成 (中文 key: zyfw/zygcfx/jyzl)
#      OperationsRequired     99K   经营评述长文本 (degraded, 需 NLP)
#   B. datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_*
#      RPT_ORG_SURVEYNEW          机构调研 (29 万条)
#      RPT_SHARE_HOLDER_INCREASE  股东增减持 (区分大宗/二级)
#      RPT_HOLDERNUMLATEST        最新股东户数 (filter 用 SECURITY_CODE)
#      RPT_MAIN_ORGHOLD           机构持仓全市场 (32 万页)
#      RPT_F10_EH_FREEHOLDERS     十大流通股东 (IS_LANDSTOCK=1 陆股通)
#      RPT_F10_MAIN_ORGHOLD       机构持仓历史
#      RPT_F10_CORETHEME_BOARDTYPE 核心题材所属板块 (入选理由)
#      RPT_F10_FN_MAINOP          主营构成 (英文 key)
#   C. reportapi.eastmoney.com/report/list  个股研报 (盈利预测+评级+PDF直链)
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
D = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# emweb F10 页面 → 输出文件名
HSF10_PAGES = {
    "ShareholderResearch": "shareholder",
    "CompanySurvey": "company",
    "CapitalStockStructure": "capital",
    "BonusFinancing": "bonus",
    "BusinessAnalysis": "business",
    "OperationsRequired": "operations",
}


def _get(url, timeout=40, retries=3):
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.2 + i)
    raise last


def secu_code(code):
    """emweb F10 PageAjax 用: 600519 -> SH600519"""
    code = code.strip()
    if code[:2] in ("60", "68", "90") or code[0] == "9":
        return f"SH{code}"
    return f"SZ{code}"


def datacenter_secu(code):
    """datacenter-web SECUCODE 用: 600519 -> 600519.SH (带点, 代码在前)"""
    code = code.strip()
    if code[:2] in ("60", "68", "90") or code[0] == "9":
        return f"{code}.SH"
    return f"{code}.SZ"


# ══════════════════════════════════════════════════════════
# A. emweb F10 PageAjax
# ══════════════════════════════════════════════════════════
def fetch_hsf10(code, page):
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/{page}/PageAjax?code={secu_code(code)}"
    return json.loads(_get(url))


# ══════════════════════════════════════════════════════════
# B. datacenter-web RPT_*
# ══════════════════════════════════════════════════════════
def fetch_report(report, code=None, page_size=50, page_number=1, sort=None, source="WEB"):
    """source=WEB 用 client=WEB + filter(SECURITY_CODE=\"600519\");
    source=HSF10 用 client=PC (实测两种 filter key 都生效, 统一用 SECUCODE)。"""
    client = "PC" if source == "HSF10" else "WEB"
    u = (f"{D}?reportName={report}&columns=ALL&pageSize={page_size}"
         f"&pageNumber={page_number}&source={source}&client={client}")
    if sort:
        u += f"&sortColumns={sort}&sortTypes=-1"
    if code:
        val = datacenter_secu(code) if source == "HSF10" else code
        if source == "HSF10":
            u += f'&filter=(SECUCODE%3D%22{val}%22)'
        else:
            u += f'&filter=(SECURITY_CODE%3D%22{val}%22)'
    d = json.loads(_get(u))
    return (d.get("result") or {}).get("data") or []


def fetch_org_survey(code=None, n=50, p=1):
    return fetch_report("RPT_ORG_SURVEYNEW", code, n, p, "NOTICE_DATE")


def fetch_shareholder_increase(code=None, n=50, p=1):
    return fetch_report("RPT_SHARE_HOLDER_INCREASE", code, n, p, "NOTICE_DATE")


def fetch_holder_num(code, n=20, p=1):
    return fetch_report("RPT_HOLDERNUMLATEST", code, n, p, "END_DATE")


def fetch_org_hold_allmarket(code=None, n=50, p=1):
    return fetch_report("RPT_MAIN_ORGHOLD", code, n, p, "REPORT_DATE")


def fetch_freeholders(code, n=50, p=1):
    return fetch_report("RPT_F10_EH_FREEHOLDERS", code, n, p, None, source="HSF10")


def fetch_orghold_history(code, n=50, p=1):
    return fetch_report("RPT_F10_MAIN_ORGHOLD", code, n, p, None, source="HSF10")


def fetch_coretheme_board(code, n=50, p=1):
    return fetch_report("RPT_F10_CORETHEME_BOARDTYPE", code, n, p, None, source="HSF10")


def fetch_mainop(code, n=50, p=1):
    return fetch_report("RPT_F10_FN_MAINOP", code, n, p, None, source="HSF10")


# ══════════════════════════════════════════════════════════
# C. 研报
# ══════════════════════════════════════════════════════════
def fetch_reports(code, n=50, p=1, begin="2026-01-01", end=None):
    end = end or datetime.now(CST).strftime("%Y-%m-%d")
    u = ("https://reportapi.eastmoney.com/report/list?industryCode=*"
         f"&pageSize={n}&industry=*&rating=*&ratingChange=*&beginTime={begin}"
         f"&endTime={end}&pageNo={p}&fields=&qType=0&orgCode=&code={code}"
         f"&rcode=&p={p}&pageNum={p}&pageNumber={p}")
    d = json.loads(_get(u, timeout=45))
    return d.get("data") or []


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/em_f10.json")
    ap.add_argument("--codes", default="600519,000063,000001,300750")
    ap.add_argument("--group", default="hsf10",
                    choices=["hsf10", "datacenter", "report", "all"])
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    out = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
           "source": "em_f10_reverse_20261016", "codes": {}, "market": {}}

    for c in codes:
        rec = {}
        if args.group in ("hsf10", "all"):
            for page, tag in HSF10_PAGES.items():
                try:
                    d = fetch_hsf10(c, page)
                    rec[tag] = {k: len(v) if isinstance(v, list) else 1
                                for k, v in d.items() if v}
                    with open(f"/tmp/em_{c}_{tag}.json", "w", encoding="utf-8") as f:
                        json.dump(d, f, ensure_ascii=False)
                except Exception as e:
                    rec[tag] = {"err": f"{type(e).__name__} {e}"}
                time.sleep(0.6)
        if args.group in ("datacenter", "all"):
            for name, fn in [("org_survey", lambda: fetch_org_survey(c, args.limit)),
                             ("shareholder_increase", lambda: fetch_shareholder_increase(c, args.limit)),
                             ("holder_num", lambda: fetch_holder_num(c, args.limit)),
                             ("freeholders", lambda: fetch_freeholders(c, args.limit)),
                             ("orghold_history", lambda: fetch_orghold_history(c, args.limit)),
                             ("coretheme_board", lambda: fetch_coretheme_board(c, args.limit)),
                             ("mainop", lambda: fetch_mainop(c, args.limit))]:
                try:
                    rows = fn()
                    rec[name] = len(rows)
                    with open(f"/tmp/em_{c}_{name}.json", "w", encoding="utf-8") as f:
                        json.dump(rows, f, ensure_ascii=False)
                except Exception as e:
                    rec[name] = {"err": f"{type(e).__name__} {e}"}
                time.sleep(0.6)
        if args.group in ("report", "all"):
            try:
                rows = fetch_reports(c, args.limit)
                rec["reports"] = len(rows)
                with open(f"/tmp/em_{c}_reports.json", "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False)
            except Exception as e:
                rec["reports"] = {"err": f"{type(e).__name__} {e}"}
        out["codes"][c] = rec
        print(f"[{c}] {json.dumps(rec, ensure_ascii=False)}", flush=True)

    if args.group in ("datacenter", "all"):
        try:
            rows = fetch_org_hold_allmarket(None, args.limit)
            out["market"]["org_hold_allmarket"] = len(rows)
            with open("/tmp/em_org_hold_allmarket.json", "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception as e:
            out["market"]["org_hold_allmarket"] = {"err": f"{type(e).__name__} {e}"}
        try:
            rows = fetch_org_survey(None, args.limit)
            out["market"]["org_survey_latest"] = len(rows)
            with open("/tmp/em_org_survey_latest.json", "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception as e:
            out["market"]["org_survey_latest"] = {"err": f"{type(e).__name__} {e}"}
        try:
            rows = fetch_shareholder_increase(None, args.limit)
            out["market"]["shareholder_increase_latest"] = len(rows)
            with open("/tmp/em_shareholder_increase_latest.json", "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception as e:
            out["market"]["shareholder_increase_latest"] = {"err": f"{type(e).__name__} {e}"}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
