#!/usr/bin/env python3
"""fetchers_hsf10.py — 东财 HSF10 / 机构持仓 / 股东户数（2026-10-26 实测）。

实测结论（SH600519）：
  PC_HSF10/CapitalStockStructure/PageAjax   JSON  ok（xsjj/gbjg/lngbbd/gbgc）
  PC_HSF10/BonusFinancing/PageAjax          JSON  ok（fhyx/lnfhrz/zfmx/pgmx）
  PC_HSF10/BusinessAnalysis/PageAjax        JSON  ok（zyfw/zygcfx/jyps）
  PC_HSF10/ShareholderResearch/PageAjax     JSON  ok（gdrs 含股东户数/户均持股/筹码集中度）
  RPT_HOLDERNUMLATEST                       ok（HOLDER_NUM/HOLDER_NUM_CHANGE/AVG_MARKET_CAP）
  RPT_F10_MAIN_ORGHOLD                      ok（TOTAL_ORG_NUM/TOTAL_FREE_SHARES/TOTAL_SHARES_RATIO）

不可用（已实测，勿再试）：
  PC_HSF10/CompanySurvey/PageAjax  返回 HTML，不是 JSON
  PC_HSF10/OperationsRequired/PageAjax 返回 HTML
  RPT_F10_MAIN_ORGHOLDDETAILS / RPT_MAIN_ORGHOLD_DETAIL / RPT_F10_MAIN_ORG_HOLDDETAILS 报表不存在

返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

from fetchers_f10 import target_codes, UA  # 复用代码池与请求头

HSF10 = "https://emweb.securities.eastmoney.com/PC_HSF10/%s/PageAjax?code=%s"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get?"


def _secucode(code):
    code = str(code)[-6:]
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return code + ".SH"
    if code.startswith(("000", "001", "002", "300", "301")):
        return code + ".SZ"
    return code + ".BJ"


def _emcode(code):
    """HSF10 的 code 形如 SH600519。"""
    sec = _secucode(code)
    return sec.split(".")[1] + sec.split(".")[0]


def _json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _scalars(item, limit=12):
    out = {}
    for k, v in (item or {}).items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str) and 0 < len(v) <= 40:
            out[k] = v
        if len(out) >= limit:
            break
    return out


def _hsf10_sections(name, page, sections, src):
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            data = _json(HSF10 % (page, _emcode(code)))
            hit = 0
            for section in sections:
                rows = data.get(section) or []
                if not isinstance(rows, list):
                    continue
                for idx, item in enumerate(rows[:6]):
                    if not isinstance(item, dict):
                        continue
                    hit += 1
                    recs.append({
                        "_key": "hsf10-%s-%s-%s-%s" % (section, code, idx,
                                                       item.get("END_DATE") or item.get("REPORT_DATE") or ""),
                        "code": code,
                        "section": section,
                        "date": str(item.get("END_DATE") or item.get("REPORT_DATE") or "")[:10],
                        "fields": _scalars(item),
                        "src": src,
                    })
            ok += 1 if hit else 0
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": src, "ok_codes": ok, "errors": err[:5]}


def f_em_hsf10_capital_structure(ctx=None):
    """股本结构：限售解禁/股本结构/历年变动/股本构成。"""
    return _hsf10_sections("capital", "CapitalStockStructure",
                           ("xsjj", "gbjg", "lngbbd", "gbgc"),
                           "em_hsf10_capital_structure")


def f_em_hsf10_bonus_financing(ctx=None):
    """分红融资：分红明细/历年融资/增发明细/配股明细。"""
    return _hsf10_sections("bonus", "BonusFinancing",
                           ("fhyx", "lnfhrz", "zfmx", "pgmx"),
                           "em_hsf10_bonus_financing")


def f_em_hsf10_business_analysis(ctx=None):
    """经营分析：主营范围/主营构成分析/经营评述（中文 key）。"""
    return _hsf10_sections("business", "BusinessAnalysis",
                           ("zyfw", "zygcfx", "jyps"),
                           "em_hsf10_business_analysis")


def f_em_hsf10_shareholder_research(ctx=None):
    """股东研究：股东户数、户均持股、筹码集中度（gdrs 字段实测明确）。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            data = _json(HSF10 % ("ShareholderResearch", _emcode(code)))
            rows = data.get("gdrs") or []
            ok += 1 if rows else 0
            for item in rows[:10]:
                if not isinstance(item, dict):
                    continue
                end = str(item.get("END_DATE") or "")[:10]
                recs.append({
                    "_key": "hsf10-gdrs-%s-%s" % (code, end),
                    "code": code,
                    "end_date": end,
                    "holder_total_num": item.get("HOLDER_TOTAL_NUM"),
                    "prev_total_num_ratio": item.get("TOTAL_NUM_RATIO"),
                    "avg_free_shares": item.get("AVG_FREE_SHARES"),
                    "avg_free_shares_ratio": item.get("AVG_FREESHARES_RATIO"),
                    "hold_focus": item.get("HOLD_FOCUS"),
                    "price": item.get("PRICE"),
                    "avg_hold_amt": item.get("AVG_HOLD_AMT"),
                    "src": "em_hsf10_shareholder_research",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_hsf10_shareholder_research", "ok_codes": ok, "errors": err[:5]}


def f_em_holder_num_latest(ctx=None):
    """股东户数最新（RPT_HOLDERNUMLATEST）：户数/变化/户均市值。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            flt = urllib.parse.quote('(SECUCODE="%s")' % _secucode(code))
            url = (DC + "reportName=RPT_HOLDERNUMLATEST&columns=ALL&filter=%s"
                   "&pageNumber=1&pageSize=2&source=HSF10&client=PC" % flt)
            rows = (_json(url).get("result") or {}).get("data") or []
            ok += 1 if rows else 0
            for row in rows:
                end = str(row.get("END_DATE") or "")[:10]
                recs.append({
                    "_key": "holder-num-%s-%s" % (row.get("SECURITY_CODE"), end),
                    "code": row.get("SECURITY_CODE"),
                    "name": row.get("SECURITY_NAME_ABBR"),
                    "end_date": end,
                    "holder_num": row.get("HOLDER_NUM"),
                    "pre_holder_num": row.get("PRE_HOLDER_NUM"),
                    "holder_num_change": row.get("HOLDER_NUM_CHANGE"),
                    "holder_num_ratio": row.get("HOLDER_NUM_RATIO"),
                    "avg_market_cap": row.get("AVG_MARKET_CAP"),
                    "avg_hold_num": row.get("AVG_HOLD_NUM"),
                    "src": "em_holder_num_latest",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_holder_num_latest", "ok_codes": ok, "errors": err[:5]}


def f_em_f10_orghold_history(ctx=None):
    """机构持仓历史（RPT_F10_MAIN_ORGHOLD）：机构家数/持股/市值/占比。"""
    recs, ok, err = [], 0, []
    for code in target_codes(12):
        try:
            flt = urllib.parse.quote('(SECUCODE="%s")' % _secucode(code))
            url = (DC + "reportName=RPT_F10_MAIN_ORGHOLD&columns=ALL&filter=%s"
                   "&pageNumber=1&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1"
                   "&source=HSF10&client=PC" % flt)
            rows = (_json(url).get("result") or {}).get("data") or []
            ok += 1 if rows else 0
            for row in rows:
                rep = str(row.get("REPORT_DATE") or "")[:10]
                recs.append({
                    "_key": "orghold-%s-%s" % (row.get("SECURITY_CODE"), rep),
                    "code": row.get("SECURITY_CODE"),
                    "report_date": rep,
                    "org_num": row.get("TOTAL_ORG_NUM"),
                    "org_num_change": row.get("TOTAL_ORG_NUM_CHANGE"),
                    "total_free_shares": row.get("TOTAL_FREE_SHARES"),
                    "total_market_cap": row.get("TOTAL_MARKET_CAP"),
                    "shares_ratio": row.get("TOTAL_SHARES_RATIO"),
                    "change_ratio": row.get("CHANGE_RATIO"),
                    "src": "em_f10_orghold_history",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "em_f10_orghold_history", "ok_codes": ok, "errors": err[:5]}


if __name__ == "__main__":
    for fn in (f_em_hsf10_capital_structure, f_em_hsf10_bonus_financing,
               f_em_hsf10_business_analysis, f_em_hsf10_shareholder_research,
               f_em_holder_num_latest, f_em_f10_orghold_history):
        recs, meta = fn()
        print(fn.__name__, "records=", len(recs), "meta=", meta)
        if recs:
            print("  sample=", json.dumps(recs[0], ensure_ascii=False)[:220])
