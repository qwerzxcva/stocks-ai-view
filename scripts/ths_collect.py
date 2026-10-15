#!/usr/bin/env python3
# ths_collect.py — 同花顺全量接口采集器 (2026-10-15 逆向成果落地)
#
# 逆向结论 (全部本容器实测 200):
#   A. d.10jqka.com.cn/v6  — 行情/K线域 (Referer: d.10jqka.com.cn)
#      /v6/time/{code}/last.js              分时 (每分钟: 时间,价,累计量,均价,单分钟量)
#      /v6/line/{code}/01/last.js           日线最近 N 根 (date,open,close,high,low,vol,amount,amp)
#      /v6/line/{code}/01/all.js            日线全史 (start/total/num 元数据)
#      /v6/line/{code}/21/last.js           月线 (347 根)
#      code 前缀: hs_=A股, sh_=沪, sz_=深 (实测 hs_/sh_ 均可)
#   B. basic.10jqka.com.cn — F10 财务域 (Referer: basic.10jqka.com.cn)
#      /api/stock/finance/{code}_benefit.json  利润表  (45 指标 × 111 报告期, ~470KB)
#      /api/stock/finance/{code}_debt.json     资产负债表 (81 指标)
#      /api/stock/finance/{code}_cash.json     现金流量表 (75 指标)
#      结构: {flashData, fieldflashData}; flashData 内含
#            title(指标名) / report(报告期) / simple(单季) / year(年度) / *_yoy / simple_mom
#      注意: report[1] 是分隔空行, 真实数据从 report[2] 开始
#   C. basic.10jqka.com.cn/new/{code}/{page}.html — F10 页面静态数据 (GBK 编码!)
#      worth    yjycData 业绩预测 + 31 张表 (机构预测明细/评级)
#      holder   gdrsFlashData 股东人数 + 十大股东/十大流通股东 HTML 表 (含 orgid)
#      bonus    bonusData 分红 + 16 张表 (分红方案/增发/配股全史)
#      operate  9 张表 (主营构成/客户/供应商集中度)
#      event    21 张表 (高管增减持/公告日历/机构调研/担保/处罚)
#      field    4 张表 (行业排名)
#      capital  94 张表 (募资/项目投入/并购)
#      equity   astockconstrData1/2 股本结构
#      news     linkagedata 个股新闻全史 (seq/ctime/title/source)
#   D. q.10jqka.com.cn — 板块域
#      /thshy/index/field/199112/order/desc/page/{n}/ajax/1/   行业列表 (当前 401)
#      /gn/index/field/addtime/order/desc/page/{n}/ajax/1/     概念列表 (当前 401)
#      /thshy/detail/code/{bk}/page/{n}/                       行业成分股 (翻页规律已确认)
#      /gn/detail/code/{bk}/page/{n}/                          概念成分股
#      /v4/line/bk_{bk}/01/{year}.js                           板块日线 (d.10jqka)
#   E. news.10jqka.com.cn/tapp/news/push/stock                 7x24 快讯 (零签名)
#   F. zx.10jqka.com.cn/event/api/getharden/date/{d}/...       强势股 (已注册)
#
# 401 的域 (data.10jqka.com.cn 龙虎榜/新股/资金流/排名) 已加 v1_need_cookie 标记,
# 留待 GitHub Actions 出口或带 cookie 重试。
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HDRS = {
    "d": {"Referer": "http://d.10jqka.com.cn", "Host": "d.10jqka.com.cn"},
    "basic": {"Referer": "https://basic.10jqka.com.cn/", "Host": "basic.10jqka.com.cn"},
    "q": {"Referer": "http://q.10jqka.com.cn/", "Host": "q.10jqka.com.cn"},
    "news": {"Referer": "https://news.10jqka.com.cn/realtimenews.html", "Host": "news.10jqka.com.cn"},
    "zx": {"Referer": "http://zx.10jqka.com.cn/", "Host": "zx.10jqka.com.cn"},
}


def _get(url, domain="d", timeout=25, retries=3, encoding="gb18030"):
    h = dict(UA)
    h.update(HDRS.get(domain, {}))
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode(encoding, "replace")
        except Exception as e:
            last = e
            time.sleep(1.0 + i * 1.5)
    raise last


def _jsonp(text):
    """quotebridge_v6_line_hs_000063_01_last({...}) → dict"""
    m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", text, re.S)
    if not m:
        raise ValueError("not jsonp")
    return json.loads(m.group(1))


def _strip(s):
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").replace("&amp;", "&").strip()


# ══════════════════════════════════════════════════════════
# A. 行情/K线 (d.10jqka)
# ══════════════════════════════════════════════════════════
def fetch_kline(code, cycle="01", scope="last", prefix="hs_"):
    """cycle: 01=日线 21=月线; scope: last/all"""
    sym = f"{prefix}{code}"
    url = f"https://d.10jqka.com.cn/v6/line/{sym}/{cycle}/{scope}.js"
    d = _jsonp(_get(url, "d"))
    # line 系列顶层就是数据本体 (无 symbol 包裹); time 系列才有 {sym: {...}}
    v = d.get(sym) if sym in d else d
    rows = [r.split(",") for r in str(v.get("data", "")).split(";") if r]
    return {
        "symbol": sym, "name": v.get("name"), "start": v.get("start"),
        "total": v.get("total"), "num": v.get("num"), "rt": v.get("rt"),
        "market_type": v.get("marketType"), "today": v.get("today"),
        "rows": rows,
    }


KLINE_COLS = ["date", "open", "close", "high", "low", "volume", "amount",
              "amplitude", "chg_pct", "chg", "turnover"]
TIMESHARE_COLS = ["time", "price", "cum_volume", "avg_price", "minute_volume"]


def fetch_timeshare(code, prefix="hs_"):
    sym = f"{prefix}{code}"
    d = _jsonp(_get(f"https://d.10jqka.com.cn/v6/time/{sym}/last.js", "d"))
    v = d[sym]
    rows = [r.split(",") for r in str(v.get("data", "")).split(";") if r]
    return {"symbol": sym, "name": v.get("name"), "pre": v.get("pre"),
            "date": v.get("date"), "is_trading": v.get("isTrading"), "rows": rows}


# ══════════════════════════════════════════════════════════
# B. F10 三大报表 JSON (basic.10jqka)
# ══════════════════════════════════════════════════════════
def fetch_finance(code, statement="benefit"):
    """statement: benefit(利润表) / debt(资产负债) / cash(现金流)"""
    url = f"https://basic.10jqka.com.cn/api/stock/finance/{code}_{statement}.json"
    raw = _get(url, "basic", timeout=40)
    outer = json.loads(raw)
    fd = json.loads(outer["flashData"])
    title = fd["title"]
    metrics = [t[0] if isinstance(t, list) else t for t in title]

    def block(key):
        rows = fd.get(key) or []
        if not rows:
            return []
        periods = rows[0]
        # 每行 = 一个指标的全部期间值; 转成 {period: {metric: value}}
        out = []
        for i, row in enumerate(rows[1:]):
            if not any(x not in ("", None) for x in row):
                continue
            rec = {"_metric": metrics[i + 1] if i + 1 < len(metrics) else None}
            for p, val in zip(periods, row):
                rec[str(p)] = val
            out.append(rec)
        return out

    return {
        "code": code,
        "statement": statement,
        "metrics": metrics,
        "n_periods": len(fd.get("report", [[]])[0]) if fd.get("report") else 0,
        "report": block("report"),      # 报告期累计
        "simple": block("simple"),      # 单季度
        "year": block("year"),          # 年度
        "report_yoy": block("report_yoy"),
        "simple_yoy": block("simple_yoy"),
        "simple_mom": block("simple_mom"),
        "year_yoy": block("year_yoy"),
    }


# ══════════════════════════════════════════════════════════
# C. F10 页面 (basic.10jqka/new/{code}/{page}.html)
# ══════════════════════════════════════════════════════════
def fetch_f10_page(code, page):
    """返回 (inline_json_blocks, html_tables)"""
    url = f"https://basic.10jqka.com.cn/new/{code}/{page}.html"
    t = _get(url, "basic", timeout=35)
    blocks = {}
    for m in re.finditer(r'<div[^>]*id="([\w-]*[Dd]ata[\w-]*)"[^>]*>(.*?)</div>', t, re.S):
        body = m.group(2).strip()
        if body[:1] in "[{":
            try:
                blocks[m.group(1)] = json.loads(body)
            except Exception:
                pass
    tables = []
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", t, re.S):
        body = m.group(1)
        cap = re.search(r"<caption[^>]*>(.*?)</caption>", body, re.S)
        rows = []
        for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = [_strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1), re.S)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append({"caption": _strip(cap.group(1)) if cap else "", "rows": rows})
    return blocks, tables, len(t)


# ══════════════════════════════════════════════════════════
# D. 板块 (q.10jqka)
# ══════════════════════════════════════════════════════════
def fetch_board_constituents(kind, bk, max_pages=20):
    """kind: thshy / gn; 分页 /{kind}/detail/code/{bk}/page/{n}/"""
    out = []
    for p in range(1, max_pages + 1):
        url = f"http://q.10jqka.com.cn/{kind}/detail/code/{bk}/page/{p}/"
        try:
            t = _get(url, "q")
        except Exception as e:
            break
        i = t.find('id="maincont"')
        seg = t[i:i + 20000] if i > 0 else t[:20000]
        n = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)[1:]:
            cells = [_strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if cells and cells[0].isdigit():
                out.append(cells)
                n += 1
        if n == 0:
            break
        time.sleep(0.5)
    return out


def fetch_board_list(kind="thshy", max_pages=20):
    out = []
    for p in range(1, max_pages + 1):
        if kind == "thshy":
            url = f"http://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{p}/ajax/1/"
        else:
            url = f"http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/{p}/ajax/1/"
        try:
            t = _get(url, "q")
        except Exception as e:
            print(f"  board_list {kind} p{p}: {type(e).__name__}", file=sys.stderr)
            break
        n = 0
        for m in re.finditer(r"<a[^>]+href=\"[^\"]*detail/code/(\d+)/[^\"]*\"[^>]*>([^<]+)</a>", t):
            out.append({"code": m.group(1), "name": _strip(m.group(2))})
            n += 1
        if n == 0:
            break
        time.sleep(0.5)
    return out


# ══════════════════════════════════════════════════════════
# E/F. 快讯 / 强势股
# ══════════════════════════════════════════════════════════
def fetch_news_push(page=1):
    url = f"https://news.10jqka.com.cn/tapp/news/push/stock?page={page}&tag=&track=website"
    d = json.loads(_get(url, "news", encoding="utf-8"))
    return d.get("data", {}).get("list", [])


def fetch_strong_stocks(date=None):
    d = date or datetime.now(CST).strftime("%Y%m%d")
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{d}/orderby/date/orderway/desc/charset/GBK/"
    return _get(url, "zx")


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ths_out.json")
    ap.add_argument("--codes", default="000063,600519,000001,300750")
    ap.add_argument("--group", default="all",
                    choices=["all", "kline", "finance", "f10", "board", "news"])
    ap.add_argument("--board-kind", default="thshy")
    ap.add_argument("--board-code", default="881272")
    ap.add_argument("--board-pages", type=int, default=3)
    ap.add_argument("--statements", default="benefit,debt,cash")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    out = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
           "source": "ths_reverse_20261015", "groups": {}}

    if args.group in ("all", "kline"):
        g = {}
        for c in codes:
            rec = {}
            for cyc in ("01", "21"):
                try:
                    k = fetch_kline(c, cyc, "last")
                    rec[f"kline_{cyc}"] = {kk: vv for kk, vv in k.items() if kk != "rows"}
                    rec[f"kline_{cyc}_n"] = len(k["rows"])
                    rec[f"kline_{cyc}_head"] = k["rows"][:2]
                    with open(f"/tmp/ths_{c}_kline_{cyc}.json", "w", encoding="utf-8") as f:
                        json.dump(k, f, ensure_ascii=False)
                except Exception as e:
                    rec[f"kline_{cyc}_err"] = f"{type(e).__name__}"
                time.sleep(0.5)
            try:
                rec["timeshare"] = fetch_timeshare(c)
            except Exception as e:
                rec["timeshare_err"] = f"{type(e).__name__}"
            g[c] = rec
            time.sleep(0.5)
        out["groups"]["kline"] = g

    if args.group in ("all", "finance"):
        g = {}
        for c in codes:
            rec = {}
            for st in [s.strip() for s in args.statements.split(",") if s.strip()]:
                try:
                    d = fetch_finance(c, st)
                    rec[st] = {"metrics": len(d["metrics"]), "n_periods": d["n_periods"],
                               "report_rows": len(d["report"]), "year_rows": len(d["year"]),
                               "sample_metric": d["metrics"][2] if len(d["metrics"]) > 2 else None}
                    with open(f"/tmp/ths_{c}_{st}.json", "w", encoding="utf-8") as f:
                        json.dump(d, f, ensure_ascii=False)
                except Exception as e:
                    rec[st] = {"err": f"{type(e).__name__} {e}"}
                time.sleep(0.6)
            g[c] = rec
        out["groups"]["finance"] = g

    if args.group in ("all", "f10"):
        g = {}
        for c in codes:
            rec = {}
            for page in ["worth", "holder", "bonus", "operate", "event", "field", "capital", "equity", "news"]:
                try:
                    blocks, tables, ln = fetch_f10_page(c, page)
                    rec[page] = {"bytes": ln, "blocks": sorted(blocks.keys()),
                                 "tables": len(tables),
                                 "table_rows": [len(x["rows"]) for x in tables]}
                    with open(f"/tmp/ths_{c}_{page}.json", "w", encoding="utf-8") as f:
                        json.dump({"blocks": blocks, "tables": tables}, f, ensure_ascii=False)
                except Exception as e:
                    rec[page] = {"err": f"{type(e).__name__} {e}"}
                time.sleep(1.0)
            g[c] = rec
        out["groups"]["f10"] = g

    if args.group in ("all", "board"):
        try:
            lst = fetch_board_list(args.board_kind, 3)
            out["groups"]["board_list"] = {"kind": args.board_kind, "n": len(lst), "items": lst[:40]}
        except Exception as e:
            out["groups"]["board_list"] = {"err": f"{type(e).__name__} {e}"}
        try:
            cons = fetch_board_constituents(args.board_kind, args.board_code, args.board_pages)
            out["groups"]["board_constituents"] = {
                "kind": args.board_kind, "code": args.board_code,
                "n": len(cons), "head": cons[:5], "tail": cons[-3:]}
        except Exception as e:
            out["groups"]["board_constituents"] = {"err": f"{type(e).__name__} {e}"}

    if args.group in ("all", "news"):
        try:
            lst = fetch_news_push(1)
            out["groups"]["news_push"] = {"n": len(lst),
                                          "titles": [(x.get("title"), x.get("rtime")) for x in lst[:10]]}
        except Exception as e:
            out["groups"]["news_push"] = {"err": f"{type(e).__name__} {e}"}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:4000])
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
