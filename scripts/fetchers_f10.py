#!/usr/bin/env python3
"""fetchers_f10.py — 东财 F10 与股权质押（2026-10-26 实测 HTTP 200 有数据）。

实测记录（茅台 600519）：
  RPT_F10_FN_MAINOP           主营构成   200，含 REPORT_DATE/MAINOP_TYPE/ITEM_NAME/收入/占比/毛利率
  RPT_F10_EH_FREEHOLDERS      十大流通股东 200，含 HOLDER_NAME/HOLD_NUM/FREE_HOLDNUM_RATIO
  RPT_F10_CORETHEME_BOARDTYPE 核心题材   200，含 BOARD_NAME/BOARD_CODE
  RPT_CSDC_LIST               股权质押   200（市场表，按 SECURITY_CODE 过滤）

注意：RPT_F10_EH_PLEDGE 不存在（9501 报表配置不存在），勿再引用。
财联社电报需签名，未突破，不在此文件内。

返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}
WEB = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
SEC = "https://datacenter.eastmoney.com/securities/api/data/v1/get?"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MAIN_BOARD = ("600", "601", "603", "605", "000", "001", "002")


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _secucode(code):
    code = str(code).strip()[-6:]
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return code + ".SH"
    if code.startswith(("000", "001", "002", "300", "301")):
        return code + ".SZ"
    if code.startswith(("8", "4", "9")):
        return code + ".BJ"
    return code + ".SH"


def target_codes(limit=12):
    """优先取当日引擎 top 里的主板股票，避免对全市场逐只请求。"""
    for name in ("engine_v5.json", "today_picks.json"):
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            root = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        rows = root.get("top") or (root.get("picks") or {}).get("signals") or []
        codes = []
        for row in rows:
            code = str(row.get("code") or "")[-6:]
            if code[:3] in MAIN_BOARD and code not in codes:
                codes.append(code)
            if len(codes) >= limit:
                break
        if codes:
            return codes
    return []


def _f10_rows(report, columns, code, page_size=6, sort="REPORT_DATE", base=WEB,
              extra=""):
    flt = urllib.parse.quote('(SECUCODE="%s")' % _secucode(code))
    url = (base + "reportName=%s&columns=%s&filter=%s&pageNumber=1&pageSize=%d"
           % (report, columns, flt, page_size))
    if sort:
        url += "&sortColumns=%s&sortTypes=-1" % sort
    url += extra
    return (_get_json(url).get("result") or {}).get("data") or []


def f_em_f10_mainop(ctx=None):
    """主营构成：按报告期分产品/地区，含收入与毛利率。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            rows = _f10_rows(
                "RPT_F10_FN_MAINOP",
                "SECUCODE,SECURITY_CODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,"
                "MAIN_BUSINESS_INCOME,MBI_RATIO,GROSS_RPOFIT_RATIO",
                code, page_size=8)
            ok += 1 if rows else 0
            for row in rows:
                date = str(row.get("REPORT_DATE") or "")[:10]
                recs.append({
                    "_key": "f10-mainop-%s-%s-%s-%s" % (
                        row.get("SECURITY_CODE"), date,
                        row.get("MAINOP_TYPE"), row.get("ITEM_NAME")),
                    "code": row.get("SECURITY_CODE"),
                    "report_date": date,
                    "mainop_type": row.get("MAINOP_TYPE"),
                    "item": row.get("ITEM_NAME"),
                    "income": row.get("MAIN_BUSINESS_INCOME"),
                    "ratio_pct": row.get("MBI_RATIO"),
                    "gross_margin_pct": row.get("GROSS_RPOFIT_RATIO"),
                    "src": "em_f10_mainop",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_f10_mainop", "ok_codes": ok, "errors": err[:5]}


def f_em_f10_freeholders(ctx=None):
    """十大流通股东：持有人、持股数、流通占比。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            rows = _f10_rows(
                "RPT_F10_EH_FREEHOLDERS",
                "SECUCODE,SECURITY_CODE,END_DATE,HOLDER_NAME,HOLD_NUM,"
                "FREE_HOLDNUM_RATIO,HOLD_NUM_CHANGE",
                code, page_size=10, sort="END_DATE")
            ok += 1 if rows else 0
            for row in rows:
                end = str(row.get("END_DATE") or "")[:10]
                recs.append({
                    "_key": "f10-freeholder-%s-%s-%s" % (
                        row.get("SECURITY_CODE"), end, row.get("HOLDER_NAME")),
                    "code": row.get("SECURITY_CODE"),
                    "end_date": end,
                    "holder": row.get("HOLDER_NAME"),
                    "hold_num": row.get("HOLD_NUM"),
                    "free_ratio_pct": row.get("FREE_HOLDNUM_RATIO"),
                    "change_num": row.get("HOLD_NUM_CHANGE"),
                    "src": "em_f10_freeholders",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_f10_freeholders", "ok_codes": ok, "errors": err[:5]}


def f_em_f10_coretheme_board(ctx=None):
    """核心题材：股票所属核心题材板块（比行业映射更贴近炒作逻辑）。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            rows = _f10_rows(
                "RPT_F10_CORETHEME_BOARDTYPE",
                "SECUCODE,SECURITY_CODE,BOARD_NAME,BOARD_CODE",
                code, page_size=20, sort=None, base=SEC, extra="&source=HSF10&client=PC")
            ok += 1 if rows else 0
            for row in rows:
                recs.append({
                    "_key": "f10-coretheme-%s-%s" % (
                        row.get("SECURITY_CODE"), row.get("BOARD_CODE")),
                    "code": row.get("SECURITY_CODE"),
                    "board": row.get("BOARD_NAME"),
                    "board_code": row.get("BOARD_CODE"),
                    "src": "em_f10_coretheme_board",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_f10_coretheme_board", "ok_codes": ok, "errors": err[:5]}


def f_em_equity_pledge_detail(ctx=None):
    """股权质押（RPT_CSDC_LIST 市场表）：按股票取最近一条质押比例。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            flt = urllib.parse.quote('(SECURITY_CODE="%s")' % str(code)[-6:])
            url = (WEB + "reportName=RPT_CSDC_LIST&columns=ALL&filter=%s"
                   "&pageNumber=1&pageSize=1&sortColumns=TRADE_DATE&sortTypes=-1"
                   "&source=WEB&client=WEB" % flt)
            rows = (_get_json(url).get("result") or {}).get("data") or []
            ok += 1 if rows else 0
            for row in rows:
                date = str(row.get("TRADE_DATE") or "")[:10]
                recs.append({
                    "_key": "pledge-%s-%s" % (row.get("SECURITY_CODE"), date),
                    "code": row.get("SECURITY_CODE"),
                    "trade_date": date,
                    "pledge_ratio_pct": row.get("PLEDGE_RATIO"),
                    "pledge_count": row.get("PLEDGE_COUNT"),
                    "src": "em_equity_pledge_detail",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_equity_pledge_detail", "ok_codes": ok, "errors": err[:5]}


def f_em_equity_pledge_market(ctx=None):
    """市场级质押表：最近交易日名单（RPT_CSDC_LIST 首页）。"""
    url = (WEB + "reportName=RPT_CSDC_LIST&columns=ALL&pageNumber=1&pageSize=30"
           "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
    rows = (_get_json(url).get("result") or {}).get("data") or []
    recs = []
    for row in rows:
        date = str(row.get("TRADE_DATE") or "")[:10]
        recs.append({
            "_key": "pledge-market-%s-%s" % (row.get("SECURITY_CODE"), date),
            "code": row.get("SECURITY_CODE"),
            "name": row.get("SECURITY_NAME_ABBR"),
            "trade_date": date,
            "pledge_ratio_pct": row.get("PLEDGE_RATIO"),
            "src": "em_equity_pledge_market",
        })
    return recs, {"source": "em_equity_pledge_market", "n": len(recs)}


if __name__ == "__main__":
    for fn in (f_em_f10_mainop, f_em_f10_freeholders,
               f_em_f10_coretheme_board, f_em_equity_pledge_detail,
               f_em_equity_pledge_market):
        recs, meta = fn()
        print(fn.__name__, "records=", len(recs), "meta=", meta)
        if recs:
            print("  sample=", json.dumps(recs[0], ensure_ascii=False)[:200])
