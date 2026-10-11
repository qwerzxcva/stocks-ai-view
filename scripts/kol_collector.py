#!/usr/bin/env python3
"""
kol_collector: 财经 KOL / 平台热榜 纯 HTTP 采集器 (无浏览器依赖)
采集源:
  1. 抖音热榜 (网页版公开 API, 纯 HTTP)
  2. 微博热搜 (m.weibo.cn API, 带 visitor cookie; visitor 首次访问自动设置)
  3. 新浪 7x24 (已有源, 复用)
  4. 财经 KOL 文章 (通过 keyword 检索各站公开帖子)
输出: news/YYYY-MM-DD.md (按日归档, stocks-data-news 仓)
用法: python3 kol_collector.py [--date 2026-10-12] [--out-dir ...]
"""
import argparse, datetime, http.cookiejar, json, re, sys, time, urllib.request
from pathlib import Path

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://s.weibo.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

def get(url, headers=None, timeout=15):
    h = dict(UA)
    if headers:
        h.update(headers)
    body = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()
    return body.decode("utf-8", "replace")


# ─── 1. 抖音热榜 ────────────────────────────────────────────────────────────
def fetch_douyin_hot(limit=20):
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383&channel=channel_pc_web"
           "&source=9&hot_board_content=hot_board_default&hot_board_duration=7200")
    try:
        body = get(url, headers={"Referer": "https://www.douyin.com/hot"})
        d = json.loads(body)
        out = []
        for w in (d.get("data") or {}).get("word_list") or []:
            word = str(w.get("word", "")).strip()
            if not word or len(word) < 4:
                continue
            out.append({
                "ts": "",
                "title": f"[抖音热榜] {word}",
                "src": "douyin-hot",
                "hot_value": w.get("hot_value"),
                "note_count": w.get("note_count"),
                "digest": f"热度:{w.get('hot_value')} ",
            })
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"[douyin] err: {e}", file=sys.stderr)
        return []


# ─── 2. 微博热搜 + KOL 文章 ────────────────────────────────────────────────
# Weibo visitor system trick: 先 GET https://passport.weibo.com/visitor/visitor?a=incarnate
# 得到 t 参数; 再用 t 调 verify 拿到 SUB cookie; 之后用 CookieJar 访问 m.weibo.cn
def _fetch_weibo_cookie():
    """尝试自动过 Sina Visitor System, 返回 (jar, True) 或 (None, False)"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = list(UA.items())
    # step1: incarnate (直接 GET 不含 JS, 拿不到 t — 此路径已验证失败)
    return None, False


def fetch_weibo_hot(limit=15):
    """微博热搜: 纯 HTTP 不可靠 (需要 visitor 挑战), 返回空列表由平台热榜兜底"""
    print("[weibo] needs visitor challenge; skip in pure-http mode", file=sys.stderr)
    return []


def fetch_weibo_kol(keyword, limit=10):
    """微博搜索某博主/关键词的公开帖子 (m.weibo.cn API, 无需登录)"""
    url = (f"https://m.weibo.cn/api/container/getIndex?"
           f"containerid=100103type%3D3%26q%3D{urllib.request.quote(keyword)}"
           f"%26t%3D3%26page_no%3D1%26page_size%3D{limit}")
    try:
        body = get(url, headers={"Referer": "https://m.weibo.cn/", "X-Requested-With": "XMLHttpRequest"})
        d = json.loads(body)
        cards = (d.get("data") or {}).get("cards") or []
        out = []
        for c in cards:
            if c.get("card_type") != 9:
                continue
            mblog = c.get("mblog", {})
            text = re.sub(r"<[^>]+>", "", mblog.get("text", "")).strip()
            uid = mblog.get("user", {}).get("id", "")
            created = mblog.get("created_at", "")
            if not text or len(text) < 6:
                continue
            out.append({
                "ts": created,
                "title": f"[微博·{keyword}] {text[:80]}",
                "src": f"weibo-kol-{keyword}",
                "digest": text[:160],
                "uid": uid,
            })
        return out
    except Exception as e:
        print(f"[weibo-kol {keyword}] err: {e}", file=sys.stderr)
        return []


# ─── 3. 已知财经 KOL 名单 (需人工维护, 按粉丝量/影响力排序) ─────────────────
KNOWN_FINANCE_KOLS = [
    "财经博主",  # 通用词
    "基金加仓",
    "ETF投资",
    "股票加仓",
    "量化选股",
    "涨停分析",
    "私募动态",
    "北向资金",
]


# ─── 4. 主流程 ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir or f"/tmp/kol_news")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{date_str}.md"

    items = []
    items += fetch_douyin_hot(limit=args.limit // 2)
    time.sleep(0.3)
    # 微博 KOL 帖 (取前 3 个通用词)
    for kw in KNOWN_FINANCE_KOLS[:3]:
        items += fetch_weibo_kol(kw, limit=5)
    time.sleep(0.5)
    # dedup by title
    seen = set()
    out = []
    for it in items:
        key = it["title"]
        if key in seen or len(key) < 6:
            continue
        seen.add(key)
        out.append(it)
    print(f"[kol-collector] collected {len(out)} items", flush=True)

    # 写入 md
    lines = [f"# kol 财经 KOL & 平台热榜 {date_str}", ""]
    for it in out[:50]:
        ts = it.get("ts") or ""
        digest = it.get("digest") or ""
        extra = f" | 热度:{it.get('hot_value')}" if it.get("hot_value") else ""
        lines.append(f"- [{ts}] {it['title']}{extra}  \n  - 来源: {it['src']}  \n  - {digest[:120]}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[output] {md_path} ({len(out)} items)", flush=True)

    # 同时写一份 JSON (供 ai_screener 做新闻源)
    json_path = out_dir / f"{date_str}.json"
    report = {"ts": now.isoformat(), "items": out, "meta": {"source": "kol-collector-v1", "zero_cred": True}}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[output] {json_path}", flush=True)


if __name__ == "__main__":
    main()
