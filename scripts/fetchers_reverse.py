#!/usr/bin/env python3
"""
fetchers_reverse.py — 逆向/新发现的数据源 fetcher (2026-10-15)

本文件放「原有 fetchers_extra 里没有、通过逆向或替代路径找到」的源。
已实测可用:

  1. sohu_hisHq       搜狐历史日K (东财 WAF 时的备援 K 线源)
                      https://q.stock.sohu.com/hisHq?code=cn_600519&start=..&end=..
                      实测茅台 2923B JSON, 含 [date, open, close, chg, chg%, low, high, vol, amount]
                      注意: 部分股票 503 (限流), 港股/美股 code 格式不同 (暂不支持)

  2. sina_industry    新浪行业板块 (同花顺 ths_industry 全 404 后的替代)
                      https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php
                      返回 var S_Finance_bankuai_sinaindustry = {...} JS 赋值,
                      需正则解析。含行业名/公司数/涨跌幅

  3. eastmoney_news   东财 7x24 快讯 (已修通, 从 fetchers_extra 迁来)
                      端点 /comm/web/getFastNewsList (不是 getNewsByColumns)
                      biz=web_724 + Referer: finance.eastmoney.com + req_trace
                      字段是 fastNewsList 不是 list

同花顺 dataapi/q.10jqka datacenter 系列实测全 404 (接口已下线), 不再尝试。
"""
import hashlib
import json
import os
import re
import sys
import time
import uuid
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from fetchers_extra import _jhttp, _jnum, _jstr, _bare, _default_codes
except Exception:
    _jhttp = _jnum = _jstr = _bare = _default_codes = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}


# ─── 1. 搜狐历史日 K (备援 K 线源) ────────────────────────────
def f_sohu_hisHq(ctx):
    """搜狐历史日线。东财 push2his 被 WAF 时的备援。

    返回 records: [{_key, _src, code, date, open, close, high, low, vol, amount}]
    """
    codes = (ctx.get("codes") if ctx else None) or []
    if not codes and _default_codes:
        codes = _default_codes(ctx or {}, limit=int((ctx or {}).get("kline_max_codes", 6) or 6))
    if not codes:
        codes = ["600519", "000001", "600036", "300750"]
    days = int((ctx or {}).get("kline_days", 120) or 120)
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days + 60)
    out, ok, errs = [], 0, []
    for code in codes[:12]:
        bare = _bare(code) if _bare else str(code)
        sym = "cn_" + bare
        u = ("https://q.stock.sohu.com/hisHq?code=%s&start=%s&end=%s"
             "&stat=1&order=D&period=d" % (sym, start.strftime("%Y%m%d"),
                                             end.strftime("%Y%m%d")))
        b = _jhttp(u, timeout=10, headers={"Referer": "https://q.stock.sohu.com/"}) if _jhttp else None
        if not b:
            errs.append("%s: 无响应(503限流?)" % bare)
            continue
        try:
            d = json.loads(b.decode("utf-8", "replace"))
        except Exception:
            errs.append("%s: 非JSON" % bare)
            continue
        if not isinstance(d, list) or not d:
            errs.append("%s: 空" % bare)
            continue
        hq = (d[0] or {}).get("hq") or []
        if not hq:
            errs.append("%s: 无hq" % bare)
            continue
        n = 0
        for row in hq:
            # row = [date, open, close, chg, chg%, low, high, vol, amount]
            if len(row) < 8:
                continue
            out.append({
                "_key": "%s|%s" % (bare, row[0]), "_src": "sohu_hisHq",
                "code": bare, "date": row[0],
                "open": _jnum(row[1]) if _jnum else row[1],
                "close": _jnum(row[2]) if _jnum else row[2],
                "low": _jnum(row[5]) if _jnum else row[5],
                "high": _jnum(row[6]) if _jnum else row[6],
                "vol": _jnum(row[7]) if _jnum else row[7],
                "amount": _jnum(row[8]) if _jnum and len(row) > 8 else None,
            })
            n += 1
        if n:
            ok += 1
        time.sleep(0.3)
    if not out:
        return [], {"reason": "; ".join(errs[:4]) or "搜狐无数据"}
    return out, {"source": "sohu_hisHq", "n": len(out), "codes_ok": ok}


# ─── 2. 新浪行业板块 (同花顺下线的替代) ───────────────────────
def f_sina_industry(ctx):
    """新浪行业板块。同花顺 dataapi 系列实测全 404 (2026-10-15), 改用新浪。

    页面返回 var S_Finance_bankuai_sinaindustry = {...}; 需正则剥出再解析。
    格式: {"new_blhy":"new_blhy,行业名,公司数,涨跌幅,...", ...}
    """
    u = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    b = _jhttp(u, timeout=10, headers={"Referer": "https://finance.sina.com.cn/"}) if _jhttp else None
    if not b:
        return [], {"reason": "新浪行业板块无响应"}
    txt = b.decode("utf-8", "gbk", "replace") if b[:3] != b"var" else b.decode("gbk", "replace")
    # 剥 var X = {...};
    m = re.search(r"=\s*\{(.*)\}\s*;?\s*$", txt.strip(), re.DOTALL)
    if not m:
        return [], {"reason": "新浪行业板块无 JS 赋值体"}
    body = "{" + m.group(1) + "}"
    # 内容是 key:"a,b,c,..." 的伪 JSON, 用正则逐项抠
    items = re.findall(r'"?([\w]+)"?\s*:\s*"([^"]*)"', body)
    if not items:
        return [], {"reason": "新浪行业板块解析失败"}
    out = []
    for key, val in items:
        parts = [p for p in val.split(",") if p]
        if len(parts) < 4:
            continue
        # new_blhy,行业名,公司数,涨跌幅,...
        name = parts[1] if len(parts) > 1 else key
        n_co = parts[2] if len(parts) > 2 else ""
        chg = parts[3] if len(parts) > 3 else ""
        try:
            chg_f = float(chg)
        except (TypeError, ValueError):
            chg_f = None
        out.append({
            "_key": key, "_src": "sina_industry", "code": key,
            "name": name, "industry": name,
            "n_companies": _jnum(n_co) if _jnum else n_co,
            "chg_pct": chg_f,
        })
    if not out:
        return [], {"reason": "新浪行业板块条目为空"}
    return out, {"source": "sina_industry", "n": len(out)}


# ─── 3. 东财 7x24 快讯 (修通版, 与 fetchers_extra 的同名实现等价) ──
def f_eastmoney_news_fixed(ctx):
    """东财 7x24 快讯 — 修通版。

    关键差异 (2026-10-15 实测):
      端点  /comm/web/getFastNewsList   (旧实现用 getNewsByColumns → 空)
      biz   web_724                     (旧实现 web7_24)
      Referer https://finance.eastmoney.com/  (旧实现不带 → 567)
      字段  data.fastNewsList           (旧实现取 data.list → 空)
    """
    u = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
         "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=30"
         "&req_trace=%s" % uuid.uuid4().hex)
    b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.eastmoney.com/"}) if _jhttp else None
    if not b:
        return [], {"reason": "东财快讯无响应"}
    try:
        d = json.loads(b.decode("utf-8")).get("data") or {}
        items = d.get("fastNewsList") or d.get("list") or []
    except Exception:
        return [], {"reason": "东财快讯非 JSON"}
    out = []
    for it in items:
        title = (it.get("title") or it.get("summary") or "").strip()
        if not title:
            continue
        h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
        out.append({
            "_key": h, "_src": "eastmoney_news", "title": _jstr(title, 200) if _jstr else title,
            "summary": _jstr(it.get("summary"), 300) if _jstr else it.get("summary"),
            "time": it.get("showTime") or it.get("sortEnd") or "",
            "url": it.get("url") or "",
        })
    if not out:
        return [], {"reason": "东财快讯列表空"}
    return out, {"source": "eastmoney_news", "n": len(out)}


# 注册表: name → (group, fn)
REVERSE_FETCHERS = [
    ("kline", "sohu_hisHq", f_sohu_hisHq),
    ("sector", "sina_industry", f_sina_industry),
    ("news", "eastmoney_news_fixed", f_eastmoney_news_fixed),
]
