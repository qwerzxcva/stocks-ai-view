#!/usr/bin/env python3
"""
collect_news_extra.py — 实时热点新闻多源采集 (逆向 + SSR 解析)

背景: 项目原有新闻源只有东财 (HTTP 567, 需 req_trace) 与新浪直播
(内容量少)。本脚本补三个方向:
  1. 联合早报 (zaobao.com) — SSR 直出, 解析 HTML 里的新闻列表
  2. 财联社电报 v1 — 需本地算签 (逆向中)
  3. 华尔街见闻 lives — 零鉴权 JSON

输出统一结构: [{title, time, url, source, summary?}]

用法: python3 collect_news_extra.py --out data/news_extra.json
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from cls_sign import cls_sign
except Exception:
    def cls_sign(p):
        return "", ""

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept": "text/html,application/json,*/*",
      "Accept-Language": "zh-CN,zh;q=0.9"}


def _decode(body):
    """服务器可能返回 gzip / deflate / br, 逐个试。"""
    if not body:
        return ""
    try:
        raw = body
        # gzip magic 1f 8b
        if raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw).decode("utf-8", "ignore")
        # deflate (zlib)
        if raw[:1] == b"\x78":
            try:
                return zlib.decompress(raw).decode("utf-8", "ignore")
            except Exception:
                pass
        return raw.decode("utf-8", "ignore")
    except Exception:
        try:
            return body.decode("utf-8", "ignore")
        except Exception:
            return ""


def get(url, referer=None, timeout=12, verbose=False):
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        txt = _decode(body)
        if verbose:
            print("  [get] %s -> %d 字节 (HTTP %s)" % (url[:60], len(txt), r.status))
        return txt
    except Exception as e:
        print("  [get] %s -> %s %s" % (url[:60], type(e).__name__, str(e)[:60]))
        return ""


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return s.strip()


# ─── 1. 联合早报 (SSR 解析) ───────────────────────────────────
def fetch_zaobao(limit=30):
    """联合早报中国频道。无公开 RSS (rss/*.xml 是 SPA fallback 返回
    'Page Not Found'), 所以解析 SSR HTML 里的文章链接 + 标题。
    """
    out = []
    for section in ("news/china", "news/singapore"):
        url = "https://www.zaobao.com/%s" % section
        html = get(url, referer="https://www.zaobao.com/", verbose=True)
        if not html:
            print("  [zaobao] %s 返回空 (可能被出口 IP 拦)" % section)
            continue
        print("  [zaobao] %s HTML %d 字节" % (section, len(html)))
        # SSR 里文章链接形如 /news/china/story20260804-9469069
        # 标题在 <a ...>text</a> 或 aria-label
        seen = set()
        for m in re.finditer(
                r'<a[^>]+href="(/news/[^"]+story\d+-\d+)"[^>]*>(.*?)</a>',
                html, re.DOTALL):
            path, inner = m.group(1), m.group(2)
            if path in seen:
                continue
            seen.add(path)
            title = strip_tags(inner)
            if len(title) < 6:
                # 标题可能不在 <a> 内, 退而取 aria-label / title 属性
                am = re.search(r'(?:aria-label|title)="([^"]{6,})"',
                               html[max(0, m.start() - 200):m.start()])
                title = strip_tags(am.group(1)) if am else ""
            if not title:
                continue
            out.append({
                "title": title,
                "url": "https://www.zaobao.com" + path,
                "source": "zaobao",
                "time": "",   # SSR 列表页无时间, 详情页才有 (下方补)
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # 列表页不带时间, 取前 8 条的详情页补发布时间 (详情页有 datePublished)
    for it in out[:8]:
        try:
            it["time"] = fetch_zaobao_detail_time(it["url"])
        except Exception:
            pass
    return out[:limit]


def fetch_zaobao_detail_time(url):
    """补一条详情页的发布时间 (列表页没有)。"""
    html = get(url, referer="https://www.zaobao.com/")
    if not html:
        return ""
    m = (re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
         or re.search(r'发布\s*/\s*(\d{4}年\d{1,2}月\d{1,2}日\s*\d{2}:\d{2})', html)
         or re.search(r'<time[^>]*datetime="([^"]+)"', html))
    return m.group(1) if m else ""


# ─── 2. 华尔街见闻 lives (零鉴权 JSON, 多频道) ───────────────────
WSCN_CHANNELS = ["a-stock-channel", "us-stock-channel", "hk-stock-channel"]


def fetch_wallstcn(limit=30):
    """api-one.wallstcn.com/apiv1/content/lives — 实测可用, 零鉴权。

    多频道: a-stock / us-stock / hk-stock (global 与 tech 实测超时或空)。
    字段极丰富: symbols(关联股票代码) / related_themes(关联题材) /
    fund_codes / is_calendar / score — 能直接关联到个股的舆情源。
    next_cursor 可翻页。
    """
    out = []
    for ch in WSCN_CHANNELS:
        url = ("https://api-one.wallstcn.com/apiv1/content/lives"
               "?channel=%s&limit=%d" % (ch, limit))
        body = get(url, referer="https://wallstreetcn.com/", verbose=True)
        if not body:
            continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for it in ((d.get("data") or {}).get("items")) or []:
            title = strip_tags(it.get("title") or "")
            content = strip_tags(it.get("content") or "")[:200]
            if not title and not content:
                continue
            ts = it.get("display_time") or it.get("created_at")
            syms = it.get("symbols") or []
            codes = []
            for s in syms:
                if isinstance(s, dict):
                    c = s.get("code") or s.get("symbol")
                    if c:
                        codes.append(str(c))
                elif s:
                    codes.append(str(s))
            # related_themes 是 [{id,title,key,...}] dict; symbols 在快讯流里
            # 实测为空, 所以关联维度靠 themes
            themes = []
            for t in (it.get("related_themes") or [])[:5]:
                if isinstance(t, dict):
                    tt = t.get("title")
                    if tt:
                        themes.append(str(tt))
                elif t:
                    themes.append(str(t))
            out.append({
                "title": title or content[:60],
                "summary": content,
                "url": ("https://wallstreetcn.com/live/%s" % it.get("id")
                        if it.get("id") else ""),
                "source": "wallstreetcn",
                "channel": ch,
                "time": (datetime.fromtimestamp(ts, CST).isoformat(timespec="seconds")
                         if isinstance(ts, (int, float)) else str(ts or "")),
                "codes": codes,
                "themes": themes,
                "is_calendar": bool(it.get("is_calendar")),
            })
    return out


# ─── 3. 财联社电报 (已逆向: /api/cache + sign=md5(sha1)) ───────
def fetch_cls(limit=20):
    """财联社电报。

    逆向结论 (2026-10-14, 浏览器抓真实请求 + JS 模块分析):
      真实接口是 /api/cache?name=telegraphList (不是 /v1/roll/get_roll_list)
      sign = md5(sha1(raw)), raw = 字典序 key=value 用 & 连接 (滤空值)
        用 5 组浏览器真实样本反推, 5/5 命中
      JS 依据: 模块 36498 (拼串) + 37371 (WebCrypto sha-1) + 81383 (md5)
      lastTime=0 返回最新 N 条; 传时间戳只返回该时刻之后的增量
    """
    params = {"app": "CailianpressWeb", "lastTime": "0",
              "name": "telegraphList", "os": "web", "sv": "8.7.9"}
    sign, _raw = cls_sign(params)
    q = dict(params)
    q["sign"] = sign
    url = "https://www.cls.cn/api/cache?" + urllib.parse.urlencode(q)
    body = get(url, referer="https://www.cls.cn/telegraph", verbose=True)
    if not body:
        return []
    try:
        d = json.loads(body)
    except Exception:
        print("  [cls] 非 JSON: %s" % body[:80])
        return []
    if d.get("errno"):
        print("  [cls] errno=%s %s" % (d.get("errno"), d.get("msg")))
        return []
    out = []
    for it in ((d.get("data") or {}).get("roll_data")) or []:
        ts = it.get("ctime")
        out.append({
            "title": strip_tags(it.get("title") or "")[:120],
            "summary": strip_tags(it.get("content") or "")[:300],
            "url": ("https://www.cls.cn/detail/%s" % it["id"]
                    if it.get("id") else ""),
            "source": "cls",
            "time": (datetime.fromtimestamp(ts, CST).isoformat(timespec="seconds")
                     if isinstance(ts, (int, float)) else ""),
        })
    return out[:limit]


def _cls_sign_legacy():
    """旧版 /v1/roll/get_roll_list 的签名占位 (已改用 /api/cache, 不再需要)。"""
    return ""


SOURCES = [
    ("zaobao", fetch_zaobao),
    ("wallstreetcn", fetch_wallstcn),
    ("cls", fetch_cls),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "news_extra.json"))
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    all_items = []
    per_source = {}
    for name, fn in SOURCES:
        print("[%s] fetching..." % name)
        try:
            items = fn(args.limit)
        except Exception as e:
            print("  [FAIL] %s %s" % (type(e).__name__, e))
            items = []
        per_source[name] = len(items)
        all_items.extend(items)
        print("  -> %d 条" % len(items))

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "collect_news_extra v1",
        "n_items": len(all_items),
        "per_source": per_source,
        "items": all_items,
        "meta": {
            "sources": [n for n, _ in SOURCES],
            "note": "zaobao SSR解析; wallstreetcn 零鉴权JSON; cls 需逆向算签(未完成则跳过)",
            "caveat": "新闻源仅作舆情参考, 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[news_extra] %d 条 -> %s" % (len(all_items), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
