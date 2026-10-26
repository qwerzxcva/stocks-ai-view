#!/usr/bin/env python3
"""fetchers_em3.py — 东财剩余源（2026-10-26 全部实测 success=ok 有数据）。

实测：
  RPT_DMSK_TS_STOCKNEW           大单/大宗跟踪
  RPT_IPO_ALLOTMENT              新股配号
  RPTA_APP_IPOAPPLY              新股申购与中签
  RPT_NEEQ_ISSUEINFO_LIST        新三板发行
  RPT_IPO_INFOALLNEW             IPO 审核状态
  RPT_CB_BALLOTNUM               可转债中签号
  RPT_CB_IMPORTANTDATE           可转债重要日期
  RPT_BOND_BS_OPRFINVESTITEM     可转债募投项目
  RPTA_WEB_KZZ_LS                可转债历史价值
  RPT_ECONOMY_HOUSE_PRICE        70 城房价
  RPT_IMP_INTRESTRATEN           利率（Shibor 等）
  RPTA_WEB_MARGIN_DAILYTRADE     两融每日
  RPT_MAIN_ORGHOLD               全市场机构持仓
  RPT_ORG_SURVEYNEW              机构调研
  RPT_SEO_DETAIL                 定增/再融资明细
  RPT_STOCK_PARTICIPATION        参与意愿
  PC_HSF10/CompanySurvey         公司概况（JSON，本次实测可用）
  PC_HSF10/OperationsRequired    经营评述（JSON，本次实测可用）
  reportapi/report/list          研报列表（JSON）

返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetchers_f10 import UA, target_codes  # noqa

DC = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
PAGE = 30


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _rows(report, extra=""):
    payload = json.loads(_get(DC + "reportName=%s&columns=ALL&pageNumber=1&pageSize=%d%s"
                              % (report, PAGE, extra)))
    if not payload.get("success"):
        raise RuntimeError(payload.get("message") or "report failed")
    return (payload.get("result") or {}).get("data") or []


def _pick(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


# id / report / 排序 / 字段映射
SPECS = [
    ("em_dmsk_ts_stock_new", "RPT_DMSK_TS_STOCKNEW",
     "&sortColumns=TRADE_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "trade_date": ("TRADE_DATE",), "super_inflow": ("SUPERDEAL_INFLOW",)}),
    ("em_ipo_allotment", "RPT_IPO_ALLOTMENT",
     "&sortColumns=EQUITY_RECORD_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "apply_code": ("APPLY_CODE",), "record_date": ("EQUITY_RECORD_DATE",)}),
    ("em_ipo_apply_yield", "RPTA_APP_IPOAPPLY",
     "&sortColumns=APPLY_DATE,SECURITY_CODE&sortTypes=-1,1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "apply_code": ("APPLY_CODE",), "apply_date": ("APPLY_DATE",),
      "listing_date": ("LISTING_DATE",), "market": ("TRADE_MARKET",)}),
    ("em_ipo_neeq_list", "RPT_NEEQ_ISSUEINFO_LIST", "",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "apply_code": ("APPLY_CODE",), "issue_num": ("EXPECT_ISSUE_NUM",)}),
    ("em_ipo_review", "RPT_IPO_INFOALLNEW",
     "&sortColumns=UPDATE_DATE&sortTypes=-1",
     {"name": ("DECLARE_ORG", "SECURITY_NAME_ABBR"), "state": ("STATE",),
      "industry": ("CSRC_INDUSTRY",), "accept_date": ("ACCEPT_DATE",),
      "update_date": ("UPDATE_DATE",)}),
    ("em_kzz_ballot_num", "RPT_CB_BALLOTNUM", "",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "bond_code": ("BOND_COMBINE_CODE",), "type": ("TYPE_CODE",)}),
    ("em_kzz_important_date", "RPT_CB_IMPORTANTDATE", "",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "bond_code": ("BOND_COMBINE_CODE",), "date_type": ("DATE_TYPE_CODE",)}),
    ("em_kzz_invest_item", "RPT_BOND_BS_OPRFINVESTITEM", "",
     {"code": ("SECURITY_CODE",), "bond_name": ("BOND_NAME_ABBR",),
      "item_no": ("ITEM_NUM",), "item_name": ("ITEM_NAME",)}),
    ("em_kzz_value_hist", "RPTA_WEB_KZZ_LS", "",
     {"date": ("DATE",), "bond_code": ("ZCODE",),
      "swap_value": ("SWAPVALUE",), "pure_bond": ("PUREBONDVALUE",)}),
    ("em_macro_house_price_70", "RPT_ECONOMY_HOUSE_PRICE", "",
     {"report_date": ("REPORT_DATE",), "city": ("CITY",),
      "first_yoy": ("FIRST_COMHOUSE_SAME",), "first_mom": ("FIRST_COMHOUSE_SEQUENTIAL",),
      "second_yoy": ("SECOND_HOUSE_SAME",)}),
    ("em_macro_shibor", "RPT_IMP_INTRESTRATEN",
     "&sortColumns=REPORT_DATE&sortTypes=-1",
     {"report_date": ("REPORT_DATE",), "period": ("REPORT_PERIOD",),
      "rate": ("IR_RATE",), "change": ("CHANGE_RATE",)}),
    ("em_margin_account_daily", "RPTA_WEB_MARGIN_DAILYTRADE",
     "&sortColumns=STATISTICS_DATE&sortTypes=-1",
     {"stat_date": ("STATISTICS_DATE",), "fin_balance": ("FIN_BALANCE",),
      "loan_balance": ("LOAN_BALANCE",), "margin_balance": ("MARGIN_BALANCE",),
      "balance_ratio": ("BALANCE_RATIO",)}),
    ("em_org_hold_allmarket", "RPT_MAIN_ORGHOLD",
     "&sortColumns=REPORT_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "report_date": ("REPORT_DATE",), "org_type": ("ORG_TYPE",),
      "org_num": ("HOULD_NUM",)}),
    ("em_org_survey", "RPT_ORG_SURVEYNEW",
     "&sortColumns=NOTICE_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "notice_date": ("NOTICE_DATE",), "receive_start": ("RECEIVE_START_DATE",)}),
    ("em_seo_detail", "RPT_SEO_DETAIL",
     "&sortColumns=ISSUE_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
      "issue_date": ("ISSUE_DATE",), "market": ("TRADE_MARKET",)}),
    ("em_stock_participation", "RPT_STOCK_PARTICIPATION",
     "&sortColumns=TRADE_DATE&sortTypes=-1",
     {"code": ("SECURITY_CODE",), "trade_date": ("TRADE_DATE",),
      "wish": ("PARTICIPATION_WISH",), "wish_5d": ("PARTICIPATION_WISH_5DAYS",),
      "wish_change": ("PARTICIPATION_WISH_CHANGE",)}),
]

DATE_KEYS = ("trade_date", "apply_date", "listing_date", "accept_date", "update_date",
             "report_date", "stat_date", "notice_date", "issue_date", "record_date")


def _make(spec):
    sid, report, extra, mapping = spec

    def _fetch(ctx=None):
        rows = _rows(report, extra)
        recs = []
        for idx, row in enumerate(rows):
            out = {k: _pick(row, *keys) for k, keys in mapping.items()}
            for dk in DATE_KEYS:
                if out.get(dk):
                    out[dk] = str(out[dk])[:10]
            ident = out.get("code") or out.get("name") or ""
            date = next((out[dk] for dk in DATE_KEYS if out.get(dk)), "")
            out.update({"_key": "%s-%s-%s-%s" % (sid, ident, date, idx), "src": sid})
            recs.append(out)
        return recs, {"source": sid, "n": len(recs), "report": report}
    _fetch.__name__ = "f_" + sid
    return _fetch


for _spec in SPECS:
    globals()["f_" + _spec[0]] = _make(_spec)


# ── HSF10 两个页面（本次实测返回 JSON）──────────────────
def _hsf10_payload(path, code):
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/%s/PageAjax?code=%s" % (path, code)
    return json.loads(_get(url))


def _hsf10_records(sid, path, sections, limit=6):
    recs, ok, err = [], 0, []
    for code in target_codes(8):
        sec = ("SH" if code[:3] in ("600", "601", "603", "605", "688") else "SZ") + code
        try:
            data = _hsf10_payload(path, sec)
            hit = 0
            for section in sections:
                val = data.get(section)
                if isinstance(val, dict) and isinstance(val.get("data"), list):
                    rows = val["data"]
                elif isinstance(val, list):
                    rows = val
                elif isinstance(val, dict):
                    rows = [val]
                else:
                    rows = []
                for idx, item in enumerate(rows[:limit]):
                    if not isinstance(item, dict):
                        continue
                    scalars = {k: v for k, v in item.items()
                               if isinstance(v, (int, float)) or (isinstance(v, str) and 0 < len(v) <= 40)}
                    hit += 1
                    recs.append({
                        "_key": "hsf10-%s-%s-%s-%s" % (path.lower(), code, section, idx),
                        "code": code, "section": section, "fields": scalars, "src": sid,
                    })
            ok += 1 if hit else 0
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": sid, "ok_codes": ok, "errors": err[:5]}


def f_em_hsf10_company_survey(ctx=None):
    """公司概况：发行信息/高管/曾用名等（JSON）。"""
    return _hsf10_records("em_hsf10_company_survey", "CompanySurvey",
                          ("jbzl", "fxxg", "ggcg", "zjlx"))


def f_em_hsf10_operations_required(ctx=None):
    """经营评述与经营数据（JSON，section 名已按实测校正）。"""
    return _hsf10_records("em_hsf10_operations_required", "OperationsRequired",
                          ("dstx", "zxzb", "zxzbOther", "tszb", "zxzbhq", "zxzx",
                           "zxgg", "ssbk", "hxtc", "jgyc", "ybzy", "zyzb", "gdrs",
                           "dzjy", "rzrq"))


# ── 研报列表 ─────────────────────────────────────────────
def f_em_reportapi_stock_report(ctx=None):
    url = ("https://reportapi.eastmoney.com/report/list?industryCode=*&pageSize=50"
           "&industry=*&rating=*&ratingChange=*&beginTime=2026-01-01&endTime=2026-12-31"
           "&pageNo=1&qType=0")
    data = json.loads(_get(url)).get("data") or []
    recs = []
    for idx, row in enumerate(data):
        recs.append({
            "_key": "report-%s-%s-%s" % (row.get("stockCode"), row.get("publishDate"), idx),
            "code": row.get("stockCode"),
            "name": row.get("stockName"),
            "title": row.get("title"),
            "org": row.get("orgSName") or row.get("orgName"),
            "publish_date": str(row.get("publishDate") or "")[:10],
            "src": "em_reportapi_stock_report",
        })
    return recs, {"source": "em_reportapi_stock_report", "n": len(recs)}


if __name__ == "__main__":
    names = [s[0] for s in SPECS] + [
        "em_hsf10_company_survey", "em_hsf10_operations_required",
        "em_reportapi_stock_report"]
    for n in names:
        try:
            recs, meta = globals()["f_" + n]()
            print(n, "records=", len(recs), json.dumps(meta, ensure_ascii=False)[:120])
        except Exception as e:
            print(n, "FAIL", type(e).__name__, str(e)[:90])
