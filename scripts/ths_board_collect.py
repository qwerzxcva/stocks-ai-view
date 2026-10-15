#!/usr/bin/env python3
# ths_board_collect.py — 同花顺板块专项采集 (成分股 + 板块K线)
#
# 逆向结论 (2026-10-15 实测):
#   成分股: http://q.10jqka.com.cn/{thshy|gn}/detail/code/{bk}/page/{n}/   每页 20 只, 14 字段
#   板块K线: https://d.10jqka.com.cn/v4/line/bk_{bk}/01/{year}.js          按年分文件
#   板块列表: akshare 封装 (ths_industry_list / ths_concept_list) — q.10jqka 列表接口已 401
#
# 坑: 概念板块的 bk 内部码与列表码不一致 (bk_301558 404),
#     所以概念成分股的 bk 码必须从概念列表页的 detail/code/ 链接里取。
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
H_Q = {"Referer": "http://q.10jqka.com.cn/", "Host": "q.10jqka.com.cn"}
H_D = {"Referer": "http://d.10jqka.com.cn", "Host": "d.10jqka.com.cn"}


def _get(url, hdr, timeout=25, retries=3, encoding="gb18030"):
    h = dict(UA)
    h.update(hdr)
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
            return r.read().decode(encoding, "replace")
        except Exception as e:
            last = e
            time.sleep(1.0 + i)
    raise last


def _strip(s):
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").replace("&amp;", "&").strip()


def _jsonp(text):
    m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", text, re.S)
    if not m:
        raise ValueError("not jsonp")
    return json.loads(m.group(1))


CONST_COLS = ["rank", "code", "name", "price", "chg_pct", "chg", "speed",
              "turnover", "volratio", "amplitude", "amount", "float_shares",
              "float_mktcap", "pe"]


def fetch_constituents(kind, bk, max_pages=20):
    out = []
    for p in range(1, max_pages + 1):
        url = f"http://q.10jqka.com.cn/{kind}/detail/code/{bk}/page/{p}/"
        try:
            t = _get(url, H_Q)
        except Exception as e:
            print(f"  {kind}/{bk} p{p}: {type(e).__name__}", file=sys.stderr)
            break
        i = t.find('id="maincont"')
        seg = t[i:i + 40000] if i > 0 else t[:40000]
        n = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)[1:]:
            cells = [_strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if cells and cells[0].isdigit():
                row = dict(zip(CONST_COLS, cells))
                row["board_code"] = bk
                row["board_kind"] = kind
                out.append(row)
                n += 1
        if n == 0:
            break
        time.sleep(0.5)
    return out


def fetch_board_kline(bk, year):
    url = f"https://d.10jqka.com.cn/v4/line/bk_{bk}/01/{year}.js"
    try:
        d = _jsonp(_get(url, H_D))
    except Exception as e:
        return None
    rows = [r.split(",") for r in str(d.get("data", "")).split(";") if r]
    return {"board_code": bk, "year": year, "rows": rows}


def fetch_board_list_akshare(kind="thshy"):
    """akshare 封装拿板块列表 (q.10jqka 列表接口已 401)"""
    try:
        import akshare as ak
        if kind == "thshy":
            df = ak.stock_board_industry_name_ths()
        else:
            df = ak.stock_board_concept_name_ths()
        return [{"code": str(r[1]), "name": str(r[0])} for r in df.itertuples(index=False)]
    except Exception as e:
        print(f"  akshare {kind} failed: {type(e).__name__} {e}", file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ths_board.json")
    ap.add_argument("--kinds", default="thshy,gn")
    ap.add_argument("--pages", type=int, default=3, help="每个板块翻几页 (20只/页)")
    ap.add_argument("--max-boards", type=int, default=30, help="最多采多少个板块")
    ap.add_argument("--kline-years", default="", help="板块K线年份, 空=当前年")
    args = ap.parse_args()

    year = args.kline_years or str(datetime.now(CST).year)
    result = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
              "source": "ths_board_reverse_20261015", "kinds": {}}

    for kind in [k.strip() for k in args.kinds.split(",") if k.strip()]:
        boards = fetch_board_list_akshare(kind)
        print(f"[{kind}] akshare list: {len(boards)} boards", file=sys.stderr)
        rec = {"n_boards": len(boards), "boards": boards[:args.max_boards],
               "constituents": {}, "kline": {}}
        if not boards:
            result["kinds"][kind] = rec
            continue
        for b in boards[:args.max_boards]:
            bk, name = b["code"], b["name"]
            try:
                cons = fetch_constituents(kind, bk, args.pages)
                rec["constituents"][bk] = {"name": name, "n": len(cons), "items": cons}
                print(f"  {kind} {bk} {name}: {len(cons)} stocks", file=sys.stderr)
            except Exception as e:
                rec["constituents"][bk] = {"name": name, "err": f"{type(e).__name__}"}
            time.sleep(0.6)
            try:
                kl = fetch_board_kline(bk, year)
                # 概念板块 (gn) 的 K 线在 d.10jqka 域不存在 (实测 404), 只采行业板块
                rec["kline"][bk] = kl if kl else {"err": "404 (概念板块无K线)"}
            except Exception as e:
                rec["kline"][bk] = {"err": f"{type(e).__name__}"}
            time.sleep(0.6)
        result["kinds"][kind] = rec

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    tot_c = sum(len(v.get("constituents", {})) for v in result["kinds"].values())
    tot_s = sum(x.get("n", 0) for v in result["kinds"].values()
                for x in v.get("constituents", {}).values() if isinstance(x.get("n"), int))
    print(f"boards={tot_c} stocks={tot_s} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
