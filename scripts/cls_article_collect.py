#!/usr/bin/env python3
# cls_article_collect.py — 财联社文章全文采集 (2026-10-16 破甲)
#
# 破甲结论:
#   /detail/{id} 页面 (Next.js SSR) 的 __NEXT_DATA__.props.pageProps.articleDetail
#   免签返回完整文章, 含 column/isFree/level/title/content/brief/ctime 等 22 字段。
#   - column=电报   content 短 (平均 200 字符), 与 v1 列表等价
#   - column=文章   content 长 (实测 883~2866 字符), **这是高价值部分**
#   - isFree=True 全部免签 (实测 12/12 首页文章全 True, 未见 isFree=False)
#   - VIP 专享 (is_vip=1 / isFree=False) 不通过电报列表暴露, category=vip/depth/
#     neican/premium 等全部静默降级为空 category (返回同样 50 条, 0 独有)
#
# 文章 id 来源 (列表 API 全 404, 只能反查):
#   1. 电报 v1 返回的 shareurl (https://api3.cls.cn/share/article/{id})
#   2. 首页 https://www.cls.cn/ 的 detail/{id} 链接
#   3. 已知 id 邻域扫描 (id 连续, 每 10 个约 1 篇是文章)
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
H = {"Referer": "https://www.cls.cn/", "Host": "www.cls.cn"}


def _get(url, timeout=25, retries=3):
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=dict(UA, **H)), timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_detail(aid):
    """从 /detail/{id} 抽 articleDetail (含完整 content)"""
    t = _get(f"https://www.cls.cn/detail/{aid}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', t, re.S)
    if not m:
        return None
    pp = json.loads(m.group(1))["props"]["pageProps"]
    return pp.get("articleDetail")


def ids_from_home():
    """首页 detail/{id} 链接"""
    t = _get("https://www.cls.cn/")
    return list(dict.fromkeys(re.findall(r'/detail/(\d+)', t)))


def slim(a):
    return {
        "id": a.get("id"),
        "ctime": a.get("ctime"),
        "time": datetime.fromtimestamp(a.get("ctime") or 0, CST).strftime("%Y-%m-%d %H:%M:%S"),
        "column": (a.get("column") or {}).get("columnName"),
        "level": a.get("level"),
        "is_free": a.get("isFree"),
        "title": a.get("title") or "",
        "brief": a.get("brief") or "",
        "content": a.get("content") or "",
        "author": a.get("author") or "",
        "reading_num": a.get("readingNum"),
        "comment_num": a.get("commentNum"),
        "images": a.get("images") or [],
        "assoc_video_title": a.get("assocVideoTitle") or "",
        "assoc_video_url": a.get("assocVideoUrl") or "",
        "assoc_article_url": a.get("assocArticleUrl") or "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cls_articles.json")
    ap.add_argument("--ids", default="", help="逗号分隔文章 id; 留空则从首页反查")
    ap.add_argument("--scan", type=int, default=0, help="额外扫描 id 邻域的篇数")
    args = ap.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    if not ids:
        try:
            ids = ids_from_home()
            print(f"首页反查 {len(ids)} 个 id", file=sys.stderr)
        except Exception as e:
            print(f"首页反查失败: {type(e).__name__} {e}", file=sys.stderr)

    if args.scan and ids:
        base = int(ids[0])
        ids += [str(base - i) for i in range(1, args.scan + 1)]

    articles, telegraphs, failed = [], [], []
    for aid in ids:
        try:
            a = fetch_detail(aid)
        except Exception as e:
            failed.append({"id": aid, "err": f"{type(e).__name__}"})
            print(f"  id={aid} ERR {type(e).__name__}", file=sys.stderr)
            time.sleep(2.0)
            continue
        if not a:
            failed.append({"id": aid, "err": "no articleDetail"})
            time.sleep(1.2)
            continue
        s = slim(a)
        if s["column"] == "文章":
            articles.append(s)
            print(f"  [文章] id={aid} len={len(s['content']):5d} {s['title'][:40]}", file=sys.stderr)
        else:
            telegraphs.append(s)
            print(f"  [电报] id={aid} len={len(s['content']):5d}", file=sys.stderr)
        time.sleep(1.5)

    result = {
        "generated": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "cls_article_reverse_20261016",
        "n_articles": len(articles),
        "n_telegraphs": len(telegraphs),
        "n_failed": len(failed),
        "articles": articles,
        "telegraphs": telegraphs,
        "failed": failed,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"文章 {len(articles)} / 电报 {len(telegraphs)} / 失败 {len(failed)} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
