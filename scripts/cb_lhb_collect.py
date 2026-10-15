#!/usr/bin/env python3
# cb_lhb_collect.py — 可转债 + 龙虎榜营业部采集 (2026-10-16 破甲)
#
# 四个突破点 (全部实测):
#  1. ths_ipo_kzz          同花顺可转债全量 969 只 x 19 字段 (765KB JSON, 免签)
#  2. ths_lhb_yyb          同花顺龙虎榜营业部 type 1/2/3, 翻页生效, 每页 20 家
#  3. em_lhb_detail        东财龙虎榜明细 39 字段 (pages=53890)
#  4. jisilu_cb_realtime   集思录可转债实时 63 字段 (含溢价率/转股价值/双低值/评级)
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HDR = {
    "ths_d": {"Referer": "http://data.10jqka.com.cn/", "Host": "data.10jqka.com.cn"},
    "jl": {"Referer": "https://www.jisilu.cn/data/cbnew/", "Host": "www.jisilu.cn"},
    "em_d": {"Referer": "https://data.eastmoney.com/", "Host": "datacenter-web.eastmoney.com"},
}


def _get(url, domain, timeout=35, retries=3, enc="utf-8"):
    h = dict(UA)
    h.update(HDR.get(domain, {}))
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
            return r.read().decode(enc, "replace")
        except Exception as e:
            last = e
            time.sleep(1.2 + i)
    raise last


def _strip(s):
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").replace("&amp;", "&").strip()


# ══════════════════════════════════════════════════════════
# 1. 同花顺可转债全量
# ══════════════════════════════════════════════════════════
def fetch_ths_kzz():
    """同花顺 ipo/kzz — 可转债 969 只 x 19 字段"""
    d = json.loads(_get("https://data.10jqka.com.cn/ipo/kzz/", "ths_d"))
    return d.get("list") or []


# ══════════════════════════════════════════════════════════
# 2. 同花顺龙虎榜营业部
# ══════════════════════════════════════════════════════════
LHB_TYPES = {
    "1": {"tab": "btcz", "field": "xsjs", "desc": "榜上榜次数"},
    "2": {"tab": "sbcs", "field": "sbcs", "desc": "上榜次数(胜)"},
    "3": {"tab": "zjsl", "field": "zgczje", "desc": "总成交金额"},
}


def fetch_ths_lhb(lhb_type="1", pages=5):
    """同花顺龙虎榜营业部 — 每页 20 家, 含上榜次数/成功率"""
    cfg = LHB_TYPES[lhb_type]
    out = []
    for p in range(1, pages + 1):
        u = (f"https://data.10jqka.com.cn/ifmarket/lhbyyb/type/{lhb_type}"
             f"/tab/{cfg['tab']}/field/{cfg['field']}/sort/desc/page/{p}/")
        try:
            t = _get(u, "ths_d", enc="gb18030")
        except Exception as e:
            print(f"  lhb type{lhb_type} p{p}: {type(e).__name__}", file=sys.stderr)
            break
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)
        n = 0
        for tr in rows[1:]:
            cells = [_strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if cells and cells[0].isdigit():
                out.append(cells)
                n += 1
        if n == 0:
            break
        time.sleep(0.6)
    return {"type": lhb_type, "desc": cfg["desc"], "n": len(out), "rows": out}


# ══════════════════════════════════════════════════════════
# 3. 东财龙虎榜明细
# ══════════════════════════════════════════════════════════
def fetch_em_lhb(page_size=100, page_number=1):
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
         f"&pageSize={page_size}&pageNumber={page_number}"
         "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
    d = json.loads(_get(u, "em_d"))
    res = d.get("result") or {}
    return res.get("data") or [], res.get("pages")


# ══════════════════════════════════════════════════════════
# 4. 集思录可转债实时
# ══════════════════════════════════════════════════════════
def fetch_jisilu_cb(page=1):
    u = (f"https://www.jisilu.cn/data/cbnew/cb_list_new/"
         f"?___jsl=LST&rp={page}")
    d = json.loads(_get(u, "jl"))
    return d.get("rows") or []


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cb_lhb.json")
    ap.add_argument("--group", default="all",
                    choices=["all", "cb", "lhb", "em_lhb"])
    ap.add_argument("--lhb-pages", type=int, default=5)
    ap.add_argument("--em-pages", type=int, default=1)
    args = ap.parse_args()

    out = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
           "source": "cb_lhb_reverse_20261016"}

    if args.group in ("all", "cb"):
        try:
            kzz = fetch_ths_kzz()
            out["ths_kzz"] = {"n": len(kzz), "rows": kzz}
            print(f"[ths_kzz] {len(kzz)} 只", flush=True)
        except Exception as e:
            out["ths_kzz"] = {"err": f"{type(e).__name__} {e}"}
        time.sleep(0.8)
        try:
            cb = fetch_jisilu_cb()
            out["jisilu_cb"] = {"n": len(cb), "rows": cb}
            print(f"[jisilu_cb] {len(cb)} 只", flush=True)
        except Exception as e:
            out["jisilu_cb"] = {"err": f"{type(e).__name__} {e}"}
        time.sleep(0.8)

    if args.group in ("all", "lhb"):
        out["ths_lhb"] = {}
        for t in LHB_TYPES:
            try:
                r = fetch_ths_lhb(t, args.lhb_pages)
                out["ths_lhb"][t] = r
                print(f"[ths_lhb type{t}] {r['n']} 家 ({r['desc']})", flush=True)
            except Exception as e:
                out["ths_lhb"][t] = {"err": f"{type(e).__name__} {e}"}
            time.sleep(0.8)

    if args.group in ("all", "em_lhb"):
        try:
            rows, pages = fetch_em_lhb()
            out["em_lhb"] = {"n": len(rows), "pages": pages, "rows": rows}
            print(f"[em_lhb] {len(rows)} 条, pages={pages}", flush=True)
        except Exception as e:
            out["em_lhb"] = {"err": f"{type(e).__name__} {e}"}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"-> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
