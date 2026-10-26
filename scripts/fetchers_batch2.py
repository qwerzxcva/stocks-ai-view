#!/usr/bin/env python3
"""fetchers_batch2.py — 第二批数据源（2026-10-26 全部实测 HTTP 200 有数据）。

实测清单：
  龙虎榜  RPT_DAILYBILLBOARD_DETAILSNEW / RPT_ORGANIZATION_TRADE_DETAILS /
          RPT_OPERATEDEPT_LIST_STATISTICS / RPT_OPERATEDEPT_TRADE_DETAILSNEW /
          RPT_RATEDEPT_RETURNT_RANKING
  事件    RPT_LIFTDAY_STA（解禁）/ RPTA_WEB_GETHGLIST_NEW（回购）/
          RPT_SHARE_HOLDER_INCREASE（增持）
  业绩    RPT_FCI_PERFORMANCEE（快报）/ RPT_PUBLIC_OP_NEWPREDICT（预告）
  腾讯K   newfqkline(A股) / hkfqkline(港股) / usfqkline(美股) / weekTrends(周线)

不可用（已实测，勿再试）：
  data.10jqka.com.cn/ifmarket/lhbyyb/... 返回 HTML 表格（需 HTML 解析，未接）
  cls.cn 电报需签名，返回 HTML（未突破，不绕过）

设计：东财报表统一走表驱动（DC_SPECS）；腾讯 K 线单独函数解析 JSONP。
返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import re
import sys
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetchers_f10 import target_codes, UA  # 复用代码池与请求头

DC = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
PAGE_SIZE = 30


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _dc_rows(report, extra=""):
    url = DC + "reportName=%s&columns=ALL&pageNumber=1&pageSize=%d%s" % (
        report, PAGE_SIZE, extra)
    payload = json.loads(_get(url))
    if not payload.get("success"):
        raise RuntimeError(payload.get("message") or "report failed")
    return (payload.get("result") or {}).get("data") or []


def _pick(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


# ── 东财报表规格：id / reportName / 排序 / 输出字段映射 ─────────────
DC_SPECS = [
    {
        "id": "em_lhb_detail", "report": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "extra": "&sortColumns=TRADE_DATE&sortTypes=-1",
        "map": {
            "code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR", "SECURITY_NAME"),
            "trade_date": ("TRADE_DATE",), "explain": ("EXPLAIN", "EXPLANATION"),
            "deal_amt": ("BILLBOARD_DEAL_AMT",), "deal_amt_ratio": ("DEAL_AMOUNT_RATIO",),
            "free_cap": ("FREE_MARKET_CAP",),
        },
    },
    {
        "id": "em_lhb_org_daily", "report": "RPT_ORGANIZATION_TRADE_DETAILS",
        "extra": "&sortColumns=TRADE_DATE&sortTypes=-1",
        "map": {
            "code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
            "trade_date": ("TRADE_DATE",), "close": ("CLOSE_PRICE",),
            "change_rate": ("CHANGE_RATE",),
        },
    },
    {
        "id": "em_lhb_yyb_statistics", "report": "RPT_OPERATEDEPT_LIST_STATISTICS",
        "extra": "",
        "map": {
            "yyb_code": ("OPERATEDEPT_CODE",), "yyb_name": ("OPERATEDEPT_NAME",),
            "cycle": ("STATISTICSCYCLE",), "amount": ("AMOUNT",),
            "onlist_times": ("SALES_ONLIST_TIMES",), "act_buy": ("ACT_BUY",),
        },
    },
    {
        "id": "em_lhb_yyb_trade_detail", "report": "RPT_OPERATEDEPT_TRADE_DETAILSNEW",
        "extra": "&sortColumns=TRADE_DATE,SECURITY_CODE&sortTypes=-1,1",
        "map": {
            "yyb_code": ("OPERATEDEPT_CODE",), "yyb_name": ("OPERATEDEPT_NAME",),
            "trade_date": ("TRADE_DATE",), "code": ("SECURITY_CODE",),
            "d1": ("D1_CLOSE_ADJCHRATE",), "d2": ("D2_CLOSE_ADJCHRATE",),
            "d3": ("D3_CLOSE_ADJCHRATE",),
        },
    },
    {
        "id": "em_lhb_yyb_return_ranking", "report": "RPT_RATEDEPT_RETURNT_RANKING",
        "extra": "&sortColumns=TOTAL_BUYER_SALESTIMES_1DAY,OPERATEDEPT_CODE&sortTypes=-1,1",
        "map": {
            "yyb_code": ("OPERATEDEPT_CODE",), "yyb_name": ("OPERATEDEPT_NAME",),
            "cycle": ("STATISTICSCYCLE",), "avg_inc_1d": ("AVERAGE_INCREASE_1DAY",),
            "rise_prob_1d": ("RISE_PROBABILITY_1DAY",),
            "buy_sell_times": ("TOTAL_BUYER_SALESTIMES_1DAY",),
        },
    },
    {
        "id": "em_restricted_unlock_day", "report": "RPT_LIFTDAY_STA",
        "extra": "&sortColumns=FREE_DATE&sortTypes=-1",
        "map": {
            "free_date": ("FREE_DATE",), "lift_org_num": ("LIFT_ORG_NUM",),
            "lift_num": ("LIFT_NUM",), "market_cap": ("MARKET_CAP",),
            "change_rate": ("CHANGE_RATE",),
        },
    },
    {
        "id": "em_repurchase_list", "report": "RPTA_WEB_GETHGLIST_NEW",
        "extra": "",
        "map": {
            "code": ("REPURCODE",), "amount_limit": ("REPURAMOUNTLIMIT",),
            "amount_lower": ("REPURAMOUNTLOWER",), "end_date": ("REPURENDDATE",),
            "num_cap": ("REPURNUMCAP",), "num_lower": ("REPURNUMLOWER",),
        },
    },
    {
        "id": "em_shareholder_increase", "report": "RPT_SHARE_HOLDER_INCREASE",
        "extra": "&sortColumns=NOTICE_DATE&sortTypes=-1",
        "map": {
            "code": ("SECURITY_CODE",), "holder": ("HOLDER_NAME",),
            "change_num": ("CHANGE_NUM",), "change_symbol": ("CHANGE_NUM_SYMBOL",),
            "after_rate": ("AFTER_CHANGE_RATE",), "notice_date": ("NOTICE_DATE",),
        },
    },
    {
        "id": "em_performance_express", "report": "RPT_FCI_PERFORMANCEE",
        "extra": "&sortColumns=NOTICE_DATE&sortTypes=-1",
        "map": {
            "code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
            "notice_date": ("NOTICE_DATE",), "report_date": ("REPORT_DATE",),
            "eps": ("BASIC_EPS",), "revenue": ("TOTAL_OPERATE_INCOME",),
            "net_profit": ("PARENT_NETPROFIT",),
            "revenue_yoy": ("YSTZ",), "profit_yoy": ("SJLTZ",),
        },
    },
    {
        "id": "em_performance_forecast", "report": "RPT_PUBLIC_OP_NEWPREDICT",
        "extra": "&sortColumns=NOTICE_DATE&sortTypes=-1",
        "map": {
            "code": ("SECURITY_CODE",), "name": ("SECURITY_NAME_ABBR",),
            "notice_date": ("NOTICE_DATE",), "report_date": ("REPORT_DATE",),
            "forecast_type": ("FORECASTTYPE", "PREDICT_TYPE"),
            "amp_lower": ("ADD_AMP_LOWER", "PREDICT_AMT_LOWER"),
            "amp_upper": ("ADD_AMP_UPPER", "PREDICT_AMT_UPPER"),
        },
    },
]


def _make_dc_fetcher(spec):
    def _fetch(ctx=None):
        rows = _dc_rows(spec["report"], spec.get("extra", ""))
        recs = []
        for idx, row in enumerate(rows):
            out = {}
            for out_key, src_keys in spec["map"].items():
                out[out_key] = _pick(row, *src_keys)
            code = out.get("code") or out.get("yyb_code") or ""
            date = str(out.get("trade_date") or out.get("notice_date")
                       or out.get("free_date") or out.get("end_date") or "")[:10]
            out.update({
                "_key": "%s-%s-%s-%s" % (spec["id"], code, date, idx),
                "src": spec["id"],
            })
            if out.get("trade_date"):
                out["trade_date"] = str(out["trade_date"])[:10]
            if out.get("notice_date"):
                out["notice_date"] = str(out["notice_date"])[:10]
            if out.get("free_date"):
                out["free_date"] = str(out["free_date"])[:10]
            recs.append(out)
        return recs, {"source": spec["id"], "n": len(recs), "report": spec["report"]}
    _fetch.__name__ = "f_" + spec["id"]
    return _fetch


for _spec in DC_SPECS:
    globals()[_fetch_name := "f_" + _spec["id"]] = _make_dc_fetcher(_spec)


# ── 腾讯 K 线（JSONP / 纯 JSON）──────────────────────────────
def _tencent_bars(name, url, code, var_prefix=None):
    text = _get(url)
    if var_prefix:
        text = text[text.find("{"):]
    data = json.loads(text).get("data") or {}
    node = data.get(code) or {}
    arr = node.get("qfqday") or node.get("day") or node.get("hfqday") or []
    recs = []
    for idx, row in enumerate(arr):
        if not isinstance(row, list) or len(row) < 5:
            continue
        recs.append({
            "_key": "%s-%s-%s" % (name, code, row[0]),
            "code": code,
            "date": row[0],
            "open": float(row[1]) if row[1] else None,
            "close": float(row[2]) if row[2] else None,
            "high": float(row[3]) if row[3] else None,
            "low": float(row[4]) if row[4] else None,
            "volume": float(row[5]) if len(row) > 5 and row[5] else None,
            "src": name,
        })
    return recs, {"source": name, "code": code, "n": len(recs)}


def f_tencent_newfqkline(ctx=None):
    """A股日K（newfqkline，JSONP 需裁前缀）。"""
    out, metas = [], []
    for code in target_codes(8):
        try:
            recs, meta = _tencent_bars(
                "tencent_newfqkline",
                "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
                "?_var=kline_dayqfq&param=sh%s,day,,,120,qfq&r=0.82" % code,
                "sh%s" % code, var_prefix=True)
            out.extend(recs); metas.append(meta)
        except Exception as e:
            metas.append({"code": code, "error": type(e).__name__})
        time.sleep(0.3)
    return out, {"source": "tencent_newfqkline", "codes": len(metas),
                 "errors": [m for m in metas if m.get("error")][:5]}


def f_tencent_hk_kline(ctx=None):
    """港股日K（hk00700 等）。"""
    return _tencent_bars(
        "tencent_hk_kline",
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk00700,day,,,120,qfq",
        "hk00700")


def f_tencent_us_kline(ctx=None):
    """美股日K（usAAPL 等）。"""
    return _tencent_bars(
        "tencent_us_kline",
        "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param=usAAPL,day,,,120,qfq",
        "usAAPL")


def f_tencent_index_weektrends(ctx=None):
    """指数周线（weekTrends，JSONP，data 为 [日期, 收盘]）。"""
    text = _get("https://web.ifzq.gtimg.cn/other/klineweb/klineWeb/weekTrends"
                "?code=sh000001&type=qfq&_var=trend_qfq&r=0.35")
    text = text[text.find("{"):]
    arr = (json.loads(text).get("data") or [])
    recs = []
    for idx, row in enumerate(arr[-120:]):
        if not isinstance(row, list) or len(row) < 2:
            continue
        recs.append({
            "_key": "tencent_index_weektrends-%s-%d" % (row[0], idx),
            "code": "sh000001",
            "date": row[0],
            "close": float(row[1]) if row[1] else None,
            "src": "tencent_index_weektrends",
        })
    return recs, {"source": "tencent_index_weektrends", "n": len(recs)}


if __name__ == "__main__":
    names = [s["id"] for s in DC_SPECS] + [
        "tencent_newfqkline", "tencent_hk_kline", "tencent_us_kline",
        "tencent_index_weektrends"]
    for name in names:
        fn = globals()["f_" + name]
        try:
            recs, meta = fn()
            print(name, "records=", len(recs), "meta=", json.dumps(meta, ensure_ascii=False)[:160])
        except Exception as e:
            print(name, "FAIL", type(e).__name__, str(e)[:100])
