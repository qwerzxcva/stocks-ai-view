#!/usr/bin/env python3
"""fetchers_ths.py — 同花顺（2026-10-26 实测）。

JSON / JSONP（实测 200 有数据）：
  ths_kline_day     d.10jqka.com.cn/v6/line/hs_{code}/01/last.js    JSONP
  ths_kline_month   .../21/last.js                                  JSONP
  ths_timeshare     d.10jqka.com.cn/v6/time/hs_{code}/last.js       JSONP
  ths_news_push     news.10jqka.com.cn/tapp/news/push/stock         JSON
  ths_strong_stocks zx.10jqka.com.cn/event/api/getharden/{date}/... JSON
  ths_industry      q.10jqka.com.cn/interface/stock/thshy/...js     JSONP

HTML 表格（服务端渲染，实测 200 含 <table>）：
  ths_f10_{operate,holder,bonus,worth,capital,equity,event,field,news}
  ths_board_constituents

未接（需专门解析/其它原因）：
  ths_finance_{benefit,debt,cash}  嵌套 double-encoded JSON，未做专门解析
  ths_kzz_all / ths_lhb_yyb / ths_ipo_xgsr_html  纯展示页，需分页+表格，下轮
  ths_financial_abstract*         依赖 akshare（Python 库，手机端不可用）

返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetchers_f10 import target_codes  # noqa

CST = timezone(timedelta(hours=8))
UA = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"),
    "Referer": "https://basic.10jqka.com.cn/",
}
TAG = re.compile(r"<[^>]+>")
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)


def _text(html):
    s = TAG.sub(" ", html)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


def _get(url, gbk=False, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        raw = r.read()
    return raw.decode("gbk" if gbk else "utf-8", "replace")


def _jsonp(text):
    i = text.find("(")
    j = text.rfind(")")
    return json.loads(text[i + 1:j]) if i >= 0 and j > i else {}


# ── JSON / JSONP ────────────────────────────────────────
def f_ths_kline_day(ctx=None):
    recs, err = [], []
    for code in target_codes(8):
        try:
            data = _jsonp(_get("https://d.10jqka.com.cn/v6/line/hs_%s/01/last.js" % code))
            for line in (data.get("data") or "").split(";")[:200]:
                p = line.split(",")
                if len(p) < 6:
                    continue
                recs.append({
                    "_key": "ths-kday-%s-%s" % (code, p[0]), "code": code,
                    "date": p[0], "open": float(p[1]), "high": float(p[2]),
                    "low": float(p[3]), "close": float(p[4]), "volume": float(p[5]),
                    "src": "ths_kline_day",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "ths_kline_day", "errors": err[:5]}


def f_ths_kline_month(ctx=None):
    recs, err = [], []
    for code in target_codes(8):
        try:
            data = _jsonp(_get("https://d.10jqka.com.cn/v6/line/hs_%s/21/last.js" % code))
            for line in (data.get("data") or "").split(";")[:120]:
                p = line.split(",")
                if len(p) < 6:
                    continue
                recs.append({
                    "_key": "ths-kmonth-%s-%s" % (code, p[0]), "code": code,
                    "date": p[0], "open": float(p[1]), "high": float(p[2]),
                    "low": float(p[3]), "close": float(p[4]), "volume": float(p[5]),
                    "src": "ths_kline_month",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "ths_kline_month", "errors": err[:5]}


def f_ths_timeshare(ctx=None):
    recs, err = [], []
    for code in target_codes(6):
        try:
            data = _jsonp(_get("https://d.10jqka.com.cn/v6/time/hs_%s/last.js" % code))
            node = data.get("hs_%s" % code) or {}
            for idx, line in enumerate((node.get("data") or "").split(";")[:300]):
                p = line.split(",")
                if len(p) < 3:
                    continue
                recs.append({
                    "_key": "ths-time-%s-%s" % (code, p[0]), "code": code,
                    "time": p[0], "price": float(p[1]), "volume": float(p[2]),
                    "src": "ths_timeshare",
                })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "ths_timeshare", "errors": err[:5]}


def f_ths_news_push(ctx=None):
    data = json.loads(_get("https://news.10jqka.com.cn/tapp/news/push/stock?page=1&tag=&track=website"))
    rows = ((data.get("data") or {}).get("list")) or []
    recs = []
    for idx, row in enumerate(rows[:50]):
        recs.append({
            "_key": "ths-news-%s" % (row.get("id") or idx),
            "id": row.get("id"), "title": _text(str(row.get("title") or ""))[:160],
            "digest": _text(str(row.get("digest") or row.get("short") or ""))[:200],
            "ts": row.get("rtime") or row.get("ctime"), "url": row.get("url"),
            "src": "ths_news_push",
        })
    return recs, {"source": "ths_news_push", "n": len(recs)}


def f_ths_strong_stocks(ctx=None):
    day = datetime.now(CST).strftime("%Y-%m-%d")
    url = ("http://zx.10jqka.com.cn/event/api/getharden/date/%s/orderby/date/orderway/desc/"
           "charset/GBK/" % day)
    text = _get(url, gbk=True)
    data = json.loads(text)
    rows = data.get("data") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    recs = []
    for idx, row in enumerate(rows[:60]):
        if not isinstance(row, dict):
            continue
        recs.append({
            "_key": "ths-strong-%s-%s" % (row.get("code") or idx, day),
            "code": row.get("code"), "name": row.get("name"),
            "reason": row.get("reason") or row.get("thematic"),
            "date": row.get("date") or day, "src": "ths_strong_stocks",
        })
    return recs, {"source": "ths_strong_stocks", "n": len(recs)}


def f_ths_industry(ctx=None):
    text = _get("http://q.10jqka.com.cn/interface/stock/thshy/zdf/desc/1/quote/1.js", gbk=True)
    data = json.loads(text[text.find("(") + 1:text.rfind(")")]) if "(" in text else json.loads(text)
    rows = data.get("data") or data.get("list") or []
    recs = []
    for idx, row in enumerate(rows[:80]):
        if not isinstance(row, dict):
            continue
        recs.append({
            "_key": "ths-industry-%s" % (row.get("code") or idx),
            "code": row.get("code"), "name": row.get("name") or row.get("plate_name"),
            "chg_pct": row.get("zdf") or row.get("change"), "src": "ths_industry",
        })
    return recs, {"source": "ths_industry", "n": len(recs)}


# ── HTML 表格通用解析 ───────────────────────────────────
def _tables(html, max_tables=6, max_rows=12):
    out = []
    for ti, body in enumerate(TABLE.findall(html)[:max_tables]):
        rows = []
        for tr in ROW.findall(body)[:max_rows]:
            cells = [_text(c) for c in CELL.findall(tr)]
            cells = [c for c in cells if c]
            if cells:
                rows.append(cells[:8])
        if rows:
            out.append(rows)
    return out


def _html_pages(sid, url_tpl, codes_limit=4, max_tables=4):
    recs, err, ok = [], [], 0
    for code in target_codes(codes_limit):
        try:
            html = _get(url_tpl % code, gbk=False, timeout=25)
            tables = _tables(html, max_tables=max_tables)
            if tables:
                ok += 1
            for ti, rows in enumerate(tables):
                for ri, cells in enumerate(rows):
                    recs.append({
                        "_key": "%s-%s-t%d-r%d" % (sid, code, ti, ri),
                        "code": code, "table": ti, "row": ri, "cells": cells,
                        "src": sid,
                    })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.4)
    return recs, {"source": sid, "ok_codes": ok, "errors": err[:3]}


def f_ths_f10_operate(ctx=None):
    return _html_pages("ths_f10_operate", "https://basic.10jqka.com.cn/new/%s/operate.html")


def f_ths_f10_holder(ctx=None):
    return _html_pages("ths_f10_holder", "https://basic.10jqka.com.cn/new/%s/holder.html")


def f_ths_f10_bonus(ctx=None):
    return _html_pages("ths_f10_bonus", "https://basic.10jqka.com.cn/new/%s/bonus.html")


def f_ths_f10_worth(ctx=None):
    return _html_pages("ths_f10_worth", "https://basic.10jqka.com.cn/new/%s/worth.html")


def f_ths_f10_capital(ctx=None):
    return _html_pages("ths_f10_capital", "https://basic.10jqka.com.cn/new/%s/capital.html")


def f_ths_f10_equity(ctx=None):
    return _html_pages("ths_f10_equity", "https://basic.10jqka.com.cn/new/%s/equity.html")


def f_ths_f10_event(ctx=None):
    return _html_pages("ths_f10_event", "https://basic.10jqka.com.cn/new/%s/event.html")


def f_ths_f10_field(ctx=None):
    return _html_pages("ths_f10_field", "https://basic.10jqka.com.cn/new/%s/field.html")


def f_ths_f10_news(ctx=None):
    return _html_pages("ths_f10_news", "https://basic.10jqka.com.cn/new/%s/news.html")


def f_ths_board_constituents(ctx=None):
    """同花顺行业板块成分股（881121 示例行业，分页 1-2）。"""
    recs, err = [], []
    for page in (1, 2):
        try:
            html = _get("http://q.10jqka.com.cn/thshy/detail/code/881121/page/%d/" % page,
                        gbk=True, timeout=25)
            for ti, rows in enumerate(_tables(html, max_tables=1, max_rows=20)):
                for ri, cells in enumerate(rows):
                    recs.append({
                        "_key": "ths-cons-881121-p%d-r%d" % (page, ri),
                        "board": "881121", "page": page, "cells": cells,
                        "src": "ths_board_constituents",
                    })
        except Exception as e:
            err.append("p%d:%s" % (page, type(e).__name__))
        time.sleep(0.5)
    return recs, {"source": "ths_board_constituents", "n": len(recs), "errors": err[:3]}


if __name__ == "__main__":
    names = ["ths_kline_day", "ths_kline_month", "ths_timeshare", "ths_news_push",
             "ths_strong_stocks", "ths_industry", "ths_f10_operate", "ths_f10_holder",
             "ths_f10_bonus", "ths_f10_worth", "ths_f10_capital", "ths_f10_equity",
             "ths_f10_event", "ths_f10_field", "ths_f10_news", "ths_board_constituents"]
    for n in names:
        try:
            recs, meta = globals()["f_" + n]()
            print(n, "records=", len(recs), json.dumps(meta, ensure_ascii=False)[:110])
        except Exception as e:
            print(n, "FAIL", type(e).__name__, str(e)[:90])
