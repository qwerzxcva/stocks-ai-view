#!/usr/bin/env python3
# fetchers_reverse2.py - 第二批逆向成果 (2026-10-15)

# 子代理探测 + 本人复核后落地的 5 个源:
# 1. toutiao_hot_v2    头条热榜 (零签名)
# 2. em_ann_np_v2      东财公告 (正确域 np-anotice-stock)
# 3. szse_list         深交所证券列表
# 4. sse_announcement  上交所公告
# 5. remote_news_fixed 远端 news 仓
import datetime
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from fetchers_extra import _jhttp, _jnum, _jstr, _bare, _default_codes
except Exception:
    _jhttp = _jnum = _jstr = _bare = _default_codes = None

REMOTE_NEWS_RAW = ('https://raw.githubusercontent.com/qwerzxcva/stocks-data-news/'
                   'main/news/{date}.md')


def f_toutiao_hot_v2(ctx):
    u = 'https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc'
    b = _jhttp(u, timeout=10, retries=1) if _jhttp else None
    if not b:
        return [], {'reason': 'toutiao no response'}
    try:
        d = json.loads(b.decode('utf-8', 'replace'))
    except Exception:
        return [], {'reason': 'toutiao non-JSON'}
    out = []
    for rank, it in enumerate(d.get('data') or [], 1):
        title = (it.get('Title') or '').strip()
        if not title:
            continue
        h = hashlib.md5(title[:40].encode()).hexdigest()[:12]
        out.append({
            '_key': h, '_src': 'toutiao_hot',
            'title': _jstr(title, 200) if _jstr else title,
            'url': it.get('Url') or '', 'rank': rank,
            'hot_value': _jnum(it.get('HotValue')) if _jnum else it.get('HotValue'),
        })
    return out, {'source': 'toutiao_hot', 'n': len(out)}


def f_em_ann_np_v2(ctx):
    codes = (ctx.get('codes') if ctx else None) or []
    if not codes and _default_codes:
        codes = _default_codes(ctx or {}, limit=5)
    if not codes:
        codes = ['600519', '000001', '600036']
    out, errs = [], []
    for code in codes[:6]:
        bare = _bare(code) if _bare else str(code)
        u = ('https://np-anotice-stock.eastmoney.com/api/security/ann'
             '?page_index=1&page_size=5&ann_type=A&client_source=web'
             '&f_node=0&s_code=%s' % bare)
        b = _jhttp(u, timeout=10,
                   headers={'Referer': 'https://data.eastmoney.com/'},
                   retries=1) if _jhttp else None
        if not b:
            errs.append(bare + ': no response')
            continue
        try:
            d = json.loads(b.decode('utf-8', 'replace'))
        except Exception:
            errs.append(bare + ': non-JSON')
            continue
        for it in (d.get('data') or {}).get('list') or []:
            title = (it.get('title') or '').strip()
            if not title:
                continue
            h = hashlib.md5((bare + title[:40]).encode()).hexdigest()[:12]
            out.append({
                '_key': h, '_src': 'em_ann_np', 'code': bare,
                'title': _jstr(title, 200) if _jstr else title,
                'date': (it.get('notice_date') or '')[:10],
                'url': 'https://data.eastmoney.com/notices/detail/%s/%s.html' % (
                    bare, it.get('art_code', '')),
            })
        time.sleep(0.2)
    if not out:
        return [], {'reason': '; '.join(errs[:4]) or 'em_ann_np no data'}
    return out, {'source': 'em_ann_np', 'n': len(out)}


SZSE_CATALOGS = {
    'szse_a_list': '1110',
    'szse_fund_list': '1105',
    'szse_index_list': '1803',
}


def f_szse_list(ctx):
    which = (ctx or {}).get('catalog', 'szse_a_list')
    cid = SZSE_CATALOGS.get(which, '1110')
    u = ('https://www.szse.cn/api/report/ShowReport?SHOWTYPE=JSON'
         '&CATALOGID=%s&TABKEY=tab1&random=0.5' % cid)
    b = _jhttp(u, timeout=10, headers={'Referer': 'https://www.szse.cn/'},
               retries=1) if _jhttp else None
    if not b:
        return [], {'reason': 'szse no response'}
    try:
        d = json.loads(b.decode('utf-8', 'replace'))
    except Exception:
        return [], {'reason': 'szse non-JSON'}
    rows = []
    if isinstance(d, list) and len(d) > 1 and isinstance(d[1], dict):
        rows = d[1].get('data') or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get('zqdm') or r.get('code') or r.get('agdm') or '').strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            '_key': code, '_src': 'szse_list', 'code': code,
            'name': _jstr(r.get('zqmc') or r.get('agjc'), 40) if _jstr else (r.get('zqmc') or ''),
            'catalog': which,
        })
    if not out:
        return [], {'reason': 'szse entries empty'}
    return out, {'source': 'szse_list', 'n': len(out), 'catalog': which}


def f_sse_announcement(ctx):
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
    u = ('https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do'
         '?jsonCallBack=x&isPagination=true&pageHelp.pageSize=25'
         '&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.endPage=1'
         '&START_DATE=%s&END_DATE=%s&TITLE=&BULLETIN_TYPE=&stockType='
         % (start, today.strftime('%Y-%m-%d')))
    b = _jhttp(u, timeout=10, headers={
        'Referer': 'https://www.sse.com.cn/disclosure/listedinfo/announcement/'},
        retries=1) if _jhttp else None
    if not b:
        return [], {'reason': 'sse no response'}
    txt = b.decode('utf-8', 'replace')
    d = None
    try:
        d = json.loads(txt)
    except Exception:
        m = re.search(r'=\s*(\{.*\})\s*;?\s*$', txt.strip(), re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(1))
            except Exception:
                d = None
    if not d:
        return [], {'reason': 'sse non-JSON'}
    rows = (d.get('pageHelp') or {}).get('data') or d.get('result') or []
    if not isinstance(rows, list):
        return [], {'reason': 'sse data not list'}
    out = []
    for r in rows[:50]:
        if not isinstance(r, dict):
            continue
        title = (r.get('TITLE') or r.get('title') or '').strip()
        if not title:
            continue
        code = str(r.get('SECURITY_CODE') or r.get('stockcode') or '')
        h = hashlib.md5((code + title[:40]).encode()).hexdigest()[:12]
        out.append({
            '_key': h, '_src': 'sse_announcement', 'code': code,
            'title': _jstr(title, 200) if _jstr else title,
            'date': (r.get('SSEDATE') or r.get('date') or '')[:10],
        })
    if not out:
        return [], {'reason': 'sse empty'}
    return out, {'source': 'sse_announcement', 'n': len(out)}


NEWS_LINE = re.compile(r'^[-*]\s*\[?(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?)\]?\s*(.+)\$')


def f_remote_news_fixed(ctx):
    best = []
    for delta in range(0, 3):
        day = (datetime.date.today() - datetime.timedelta(days=delta)).strftime('%Y-%m-%d')
        u = REMOTE_NEWS_RAW.format(date=day)
        b = _jhttp(u, timeout=10, retries=1) if _jhttp else None
        if not b:
            continue
        rows = []
        for line in b.decode('utf-8', 'replace').split(chr(10)):
            m = NEWS_LINE.match(line.strip())
            if m:
                rows.append({
                    '_key': hashlib.md5(m.group(2)[:40].encode()).hexdigest()[:12],
                    '_src': 'remote_news',
                    'title': _jstr(m.group(2), 200) if _jstr else m.group(2),
                    'time': m.group(1),
                    'date': day,
                })
        if rows:
            best = rows
            break
    if not best:
        return [], {'reason': 'remote news no content'}
    return best, {'source': 'remote_news', 'n': len(best)}


REVERSE2_FETCHERS = [
    ('news', 'toutiao_hot_v2', f_toutiao_hot_v2),
    ('news', 'em_ann_np_v2', f_em_ann_np_v2),
    ('quote', 'szse_list', f_szse_list),
    ('event', 'sse_announcement', f_sse_announcement),
    ('news', 'remote_news_fixed', f_remote_news_fixed),
]
