#!/usr/bin/env python3
"""fetch_vango_articles.py — 拉取 fandy6158.github.io/stock-web-vango 公开文章流。

数据源 (零鉴权公开 GET, 站点未登录用户本就可见的元数据):
  https://vango-member-hhjebqnncv.cn-hangzhou.fcapp.run/articles
  静态兜底: https://fandy6158.github.io/stock-web-vango/data/articles.json

字段纪律 (与项目公开数据红线一致):
  - 只存公开可见字段: title / visibility / minLevel / published / 日期。
  - visibility=public 的文章可存 publicBody (站点对未登录用户的公开预览)。
  - members 级文章绝不存 privateBody, 哪怕接口返回空也不声明可读。
  - 不探测付费估值接口, 不绕过会员门槛。

频率: 每日 1 次足够 (站点日更盘前贴), 高频轮询无意义且给对方云函数压力。

用法: python3 fetch_vango_articles.py [--out data/vango_articles.json] [--dry-run]
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(ROOT, "data")

FC_API = "https://vango-member-hhjebqnncv.cn-hangzhou.fcapp.run/articles"
STATIC_FALLBACK = "https://fandy6158.github.io/stock-web-vango/data/articles.json"

# 只存的字段白名单; privateBody / auth 相关一律不取
KEEP_PUBLIC = ("id", "title", "visibility", "minLevel", "published", "publicBody")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "stocks-ai-view-vango-sync/1.0 (public metadata only)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def extract_date(title):
    """从标题提取 20261018 格式的日期。"""
    import re
    m = re.search(r"(20\d{6})", title or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "vango_articles.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    d = None
    src_used = None
    try:
        d = _get(FC_API)
        src_used = "fc_api"
    except Exception as e:
        print(f"[vango] fc api 失败 {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
        try:
            d = _get(STATIC_FALLBACK)
            src_used = "static_fallback"
        except Exception as e2:
            print(f"[vango] 静态兜底也失败: {e2}", file=sys.stderr)

    items = (d or {}).get("items") or []
    out_items = []
    n_public = 0
    for it in items:
        rec = {k: it.get(k) for k in KEEP_PUBLIC if k in it}
        rec["date"] = extract_date(it.get("title"))
        if rec.get("visibility") == "public" and rec.get("publicBody"):
            n_public += 1
        out_items.append(rec)

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "source": "fandy6158/stock-web-vango (公开文章元数据, 零鉴权)",
        "source_url": FC_API,
        "source_used": src_used,
        "n_total": len(out_items),
        "n_public_body": n_public,
        "field_policy": "仅公开字段; members 文章只存标题/等级元数据, 不存私有正文",
        "items": out_items,
    }

    if args.dry_run:
        print(json.dumps({**out, "items": out_items[:3]}, ensure_ascii=False, indent=1))
        print(f"... 共 {len(out_items)} 条 ({n_public} 条公开正文)", file=sys.stderr)
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[vango] -> {args.out} ({len(out_items)} 条, {n_public} 条公开正文, src={src_used})")
    return 0


if __name__ == "__main__":
    sys.exit(main())