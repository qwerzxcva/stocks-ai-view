#!/usr/bin/env python3
"""
collect_hotnews.py — Newsnow 聚合热点采集 (15+ 平台, 免 key)

来源: TrendRadar / mcp_newsnow 两个开源项目指向的公共 API
  https://newsnow.busiyi.world/api/s?id=<source>

实测 (2026-10-15 01:05): 15/17 源可用, 全部实时更新。
含我们此前 blocked 或没接的: 雪球、财联社电报、华尔街见闻、知乎、微博、
B站、抖音、头条、百度、贴吧、澎湃、凤凰、酷安、GitHub 趋势。

设计 (借鉴 TrendRadar):
  - 频率词分组匹配: 每个标题只计入第一个匹配的词组 (与 TrendRadar 一致)
  - 过滤词以 ! 开头: 命中则整条丢弃
  - 热度分级: 热搜榜位次天然就是热度, 保留 rank 字段

用法:
  python3 collect_hotnews.py --out data/hot_news.json
  python3 collect_hotnews.py --sources cls-hot,xueqiu,wallstreetcn
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

BASE = "https://newsnow.busiyi.world/api/s?id="
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Accept": "application/json, */*"}

# 实测可用的源 (id → 中文名)。linuxdo/kuaishou 实测 HTTPError, 不列入。
SOURCES = {
    "cls-hot": "财联社热门",
    "cls-telegraph": "财联社电报",
    "wallstreetcn": "华尔街见闻",
    "xueqiu": "雪球热帖",
    "zhihu": "知乎热榜",
    "weibo": "微博热搜",
    "bilibili-hot-search": "B站热搜",
    "douyin": "抖音热点",
    "toutiao": "今日头条",
    "baidu": "百度热搜",
    "tieba": "贴吧热议",
    "thepaper": "澎湃新闻",
    "ifeng": "凤凰网",
    "coolapk": "酷安话题",
    "github-trending-today": "GitHub 趋势",
}

# 金融相关频率词组 (借鉴 TrendRadar 的 frequency_words 机制):
# 每个标题只计入第一个匹配的词组; 用于把泛热点收敛到财经向。
FREQ_WORDS = {
    "A股": ["A股", "a股", "沪指", "深成指", "创业板", "科创板", "北交所", "上证", "深证"],
    "港美股": ["港股", "恒生", "恒指", "纳指", "道指", "标普", "纳斯达克", "美股", "中概股"],
    "政策": ["央行", "证监会", "国务院", "发改委", "财政部", "关税", "降准", "降息",
             "政治局", "国常会", "LPR", "MLF"],
    "产业": ["半导体", "芯片", "算力", "AI", "人工智能", "新能源", "光伏", "储能",
             "锂电池", "机器人", "自动驾驶", "医药", "白酒", "军工"],
    "公司": ["净利", "营收", "业绩", "预增", "预亏", "回购", "减持", "增持",
             "IPO", "上市", "退市", "并购", "重组", "中标"],
    "国际": ["美联储", "特朗普", "拜登", "欧盟", "日本", "印度", "俄乌", "中东",
             "伊朗", "朝鲜", "以色列", "贸易战"],
}


def fetch_source(sid, retries=1):
    """拉单个源。返回 (items, updated_ts)。"""
    last = "unknown"
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(BASE + sid, headers=UA)
            with urllib.request.urlopen(req, timeout=12) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return d.get("items") or [], d.get("updatedTime")
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:60])
            if i < retries:
                time.sleep(1.0)
    return [], last


def match_group(title):
    """频率词匹配: 返回第一个命中的词组名, 无则 None。"""
    for g, words in FREQ_WORDS.items():
        for w in words:
            if w in title:
                return g
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "hot_news.json"))
    ap.add_argument("--sources", default="",
                    help="逗号分隔的源 id; 缺省全部 15 个")
    ap.add_argument("--no-group", action="store_true",
                    help="不做频率词分组 (保留全部平台原始条目)")
    args = ap.parse_args()

    if args.sources.strip():
        ids = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        ids = list(SOURCES.keys())

    items = []
    ok_src, fail_src = [], {}
    for i, sid in enumerate(ids):
        name = SOURCES.get(sid, sid)
        raw, upd = fetch_source(sid)
        if not raw:
            fail_src[sid] = str(upd)[:80]
            print("  [FAIL] %-22s %s" % (name, fail_src[sid]))
            continue
        ok_src.append(sid)
        ts = ""
        if isinstance(upd, (int, float)):
            ts = datetime.fromtimestamp(upd / 1000, CST).isoformat(timespec="seconds")
        for rank, it in enumerate(raw, 1):
            title = (it.get("title") or "").strip()
            if not title:
                continue
            rec = {
                "title": title,
                "source": sid,
                "source_name": name,
                "rank": rank,
                "url": it.get("url") or it.get("mobileUrl") or "",
                "updated": ts,
                "group": match_group(title),
            }
            items.append(rec)
        print("  [OK] %-22s %3d 条" % (name, len(raw)))
        if i < len(ids) - 1:
            time.sleep(0.3)

    # 频率词分组统计 (TrendRadar 式)
    groups = {}
    for it in items:
        g = it.get("group")
        if g:
            groups.setdefault(g, []).append(it["title"])
    group_stat = {g: {"count": len(v), "top": v[:5]} for g, v in
                  sorted(groups.items(), key=lambda x: -len(x[1]))}

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "collect_hotnews v1 (Newsnow 聚合)",
        "source": "newsnow.busiyi.world",
        "n_items": len(items),
        "n_sources_ok": len(ok_src),
        "sources_ok": ok_src,
        "sources_fail": fail_src,
        "items": items,
        "groups": group_stat,
        "meta": {
            "design": "借鉴 TrendRadar: 频率词分组 (每标题只计第一个匹配组)",
            "freq_words": {g: len(w) for g, w in FREQ_WORDS.items()},
            "caveat": "热搜榜位次是平台口径, 不代表市场影响力; Research reference only",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print()
    print("[hotnews] %d 条 / %d 源可用" % (len(items), len(ok_src)))
    print("[hotnews] 财经相关分组:")
    for g, st in list(group_stat.items())[:6]:
        print("  %-6s %3d 条  例: %s" % (g, st["count"], st["top"][0][:40]))
    print("[hotnews] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
