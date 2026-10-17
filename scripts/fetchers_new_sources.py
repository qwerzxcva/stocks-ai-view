#!/usr/bin/env python3
"""fetchers_new_sources.py — 逆向调研确认的新源 (2026-10-17, 报告见 docs/research/)。

全部零鉴权实测可用:
  jrj_yaowen_daily   金融界按天要闻静态 JSON — yw{yyyy-MM-dd}.js, 一天一文件
  aigupiao_express   爱股票 7x24 快讯 — 带题材/个股标签, rec_time 为字符串需转 int
  sseinfo_qa         上证 e 互动董秘问答 — HTML feed item 解析 (◆ 后是回复)
  sseinfo_survey     上证 e 互动调研纪要 (type=30) — 周六为空属正常
  stockstar_forecast 证券之星业绩预告 — GB18030 编码必须读原始字节解码

返回口径与 collect_all_sources 一致: (records, meta), record 带 _key。
"""

import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://stock.jrj.com.cn/"}


def _get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_js_var(body):
    """金融界静态 js: 剥掉 'var newsdata=' 前缀取 JSON。"""
    m = re.search(r"=\s*(\{.*)", body, re.S)
    return m.group(1) if m else body


def f_jrj_yaowen_daily(ctx=None):
    """金融界每日要闻: 今天+昨天两个文件, 每条带分类与时间。"""
    now = datetime.now(CST)
    recs = []
    for i in (0, 1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        url = f"https://stockjs.jrj.com.cn/share/news/yaowen/yw{d}.js"
        try:
            body = _strip_js_var(_get(url))
            data = json.loads(body)
        except Exception:
            continue
        for it in (data.get("newsinfo") or []):
            t = (it.get("title") or "").strip()
            if len(t) < 6:
                continue
            recs.append({
                "_key": f"jrj-{it.get('iiid') or t[:24]}",
                "title": t[:120],
                "digest": (it.get("detail") or "")[:200],
                "ts": (it.get("makedate") or "").replace(" ", "T")[:19],
                "src": f"jrj-{it.get('infosource') or '金融界'}",
                "url": it.get("infourl") or "",
                "chnmb": it.get("chnmb", ""),
            })
    return recs, {"source": "jrj-yaowen", "freq": "daily", "zero_cred": True}


def f_aigupiao_express(ctx=None):
    """爱股票 7x24 快讯: 题材标签 + 关联个股 + 重要标记, 取最新 20 条。

    rec_time 在 JSON 里是字符串 (如 "1792250227"), 必须 int() 转换。
    """
    url = ("https://apis.aigupiao.com/Express/express_list/"
           "?before=0&source=pc&web_data=yes&number=20&express_show_type=1")
    try:
        d = json.loads(_get(url, headers={"User-Agent": UA["User-Agent"]}))
    except Exception as e:
        return [], {"error": str(e)}
    recs = []
    for day in (d.get("data") or {}).values():
        for it in (day.get("data") or []):
            t = re.sub(r"^【[^】]*】", "", (it.get("content") or "").strip())
            if len(t) < 6:
                continue
            stocks = []
            for s in (it.get("stock_infos") or []):
                if s.get("kind") == "stock" and s.get("code"):
                    stocks.append({"code": str(s["code"]).replace("USA", ""),
                                   "name": s.get("name") or ""})
            try:
                rt = int(float(it.get("rec_time") or 0))
            except (TypeError, ValueError):
                rt = 0
            recs.append({
                "_key": f"aigp-{it.get('id') or rt}",
                "title": t[:160],
                "digest": (it.get("content") or "")[:200],
                "ts": datetime.fromtimestamp(rt, CST).isoformat(timespec="seconds")
                if rt else "",
                "src": "aigupiao",
                "themes": [x.strip() for x in (it.get("theme") or "").split(",")
                           if x.strip()],
                "stocks": stocks[:5],
                "important": it.get("important") == "yes",
            })
    return recs, {"source": "aigupiao-express", "freq": "minute",
                  "zero_cred": True}


def _sseinfo_feeds(feed_type):
    url = (f"https://sns.sseinfo.com/ajax/feeds.do?type={feed_type}"
           "&pageSize=10&lastid=-1&show=1&page=1")
    try:
        return _get(url, headers={"User-Agent": UA["User-Agent"],
                                  "Referer": "https://sns.sseinfo.com/"})
    except Exception:
        return ""


_SSE_ITEM = r'<div class="m_feed_item".*?(?=<div class="m_feed_item"|$)'


def f_sseinfo_qa(ctx=None):
    """上证 e 互动董秘问答。

    实测结构: <a>：公司(代码)</a> + 问题文本 ... YYYY年MM月DD日 HH:MM ...,
    回复在最后一个 ◆ 之后 (开头是公司简称, 剥掉)。
    """
    html = _sseinfo_feeds(11) or _sseinfo_feeds(10)
    recs = []
    for item in re.findall(_SSE_ITEM, html, re.S):
        m = re.search(r'<a[^>]*>[:：]?\s*([^<（(]{2,20})\s*[(（](\d{6})[)）]</a>',
                      item)
        t = re.search(r'(\d{4})年(\d{2})月(\d{2})日\s*(\d{2}:\d{2})', item)
        if not m or not t:
            continue
        company, code = m.group(1).strip(), m.group(2)
        q = re.sub(r"<[^>]+>", "", item[m.end():t.start()]).strip()
        # 回复在 ◆ 之后的文本段里 (◆◆ 是回复区标记, 点赞/收藏是独立小段):
        # 取纯文本最长的段, 开头剥掉公司简称。
        def _seg_text(seg):
            seg = re.sub(r"<a[^>]*>.*?</a>", "", seg, flags=re.S)
            return re.sub(r"<[^>]+>", "", seg).strip()
        a = max((_seg_text(s) for s in item.split("◆")[1:]), key=len,
                default="")
        a = re.sub(r"^\s*" + re.escape(company) + r"\s*", "", a)
        if len(q) < 8 or len(a) < 4:
            continue
        recs.append({
            "_key": f"sseqa-{code}-{abs(hash(q)) % 10**8}",
            "code": code, "company": company,
            "question": q[:200], "answer": a[:300],
            "ts": f"{t.group(1)}-{t.group(2)}-{t.group(3)}T{t.group(4)}",
            "src": "sseinfo-qa",
        })
    return recs, {"source": "sseinfo-feeds", "type": 11, "zero_cred": True}


def f_sseinfo_survey(ctx=None):
    """上证 e 互动调研纪要 (投资者关系活动记录, type=30)。

    周六实测为空 (调研纪要只在交易日更新), 返回空属正常。
    """
    html = _sseinfo_feeds(30)
    recs = []
    for item in re.findall(_SSE_ITEM, html, re.S):
        m = re.search(
            r'<a[^>]*>[:：]?\s*([^<（(]{2,20})\s*[(（](\d{6})[)）]</a>(.*?)</div>',
            item, re.S)
        if not m:
            continue
        title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if len(title) < 4:
            continue
        recs.append({"_key": f"ssesy-{m.group(2)}-{abs(hash(title)) % 10**8}",
                     "code": m.group(2), "company": m.group(1).strip(),
                     "title": title[:160], "src": "sseinfo-survey"})
    return recs, {"source": "sseinfo-feeds", "type": 30, "zero_cred": True}


def f_stockstar_forecast(ctx=None):
    """证券之星业绩预告: HTML 表格首屏。

    页面是 GB18030 编码 (meta 声明 gb2312 不准), 必须读原始字节按 gb18030
    解码——utf-8 容错解码会把中文毁成不可逆的 '?'。
    域名偶发解析失败, 容错返回空 (meta 带 error)。
    """
    url = ("https://resource.stockstar.com/DataCenter/StockData/"
           "GetAchieveForecast.htm")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("gb18030", "replace")
    except Exception as e:
        return [], {"error": str(e), "note": "resource.stockstar.com 解析不稳"}
    recs = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)[1:]:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # cells: 序号/代码/简称/公告日期/报告期/预告类型/摘要/上年同期EPS/...
        if len(cells) < 7 or not re.fullmatch(r"\d{6}", cells[1] or ""):
            continue
        recs.append({"_key": f"ssfc-{cells[1]}-{cells[3]}",
                     "code": cells[1], "name": cells[2],
                     "ann_date": cells[3], "period": cells[4],
                     "forecast_type": cells[5],
                     "summary": cells[6][:200],
                     "prev_eps": cells[7] if len(cells) > 7 else "",
                     "src": "stockstar-forecast"})
    return recs, {"source": "stockstar-datacenter", "zero_cred": True}
