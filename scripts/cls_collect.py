#!/usr/bin/env python3
# cls_collect.py — 财联社电报采集 (2026-10-16 签名逆向成功)
#
# 签名算法 (与项目旧 cls_sign.py 的差异: 必须包含 category):
#   1. 所有参数 (含空 category) 按 key 字母排序
#   2. 拼接为 key=value&key=value
#   3. sign = md5(sha1(上述串).hexdigest()).hexdigest()
#   4. 默认参数: app=CailianpressWeb, os=web, sv=7.7.5, rn=50
#
# 实测结论 (2026-10-16):
#   - rn 上限 50 (rn=60/70/80/99/100/150/200 全返回 0 条)
#   - rn=50 单次拿 50 条, id 跨度 74
#   - last_time / last_id / ctime / min_time / refresh_type / page / pageNo / p
#     **全部对分页无效** (5 页翻下来全是同样 20/50 条) — 只返回最新窗口
#   - category 白名单 6 个且服务端真过滤:
#     red(40条全B级) / announcement / watch / hk_us / fund(38C) / remind
#   - 单条 53 字段: brief/content/ctime/level(A|B|C)/id/subjects(含 plate_id
#     和 subject_name)/stock_list/audio_url/shareurl/reading_num/comment_num
#   - title 字段恒为空串 (电报正文在 brief/content)
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HDR = {"Referer": "https://www.cls.cn/telegraph", "Host": "www.cls.cn",
       "Accept": "application/json, text/plain, */*"}
URL = "https://www.cls.cn/v1/roll/get_roll_list"

CATEGORIES = ["", "red", "announcement", "watch", "hk_us", "fund", "remind"]


def cls_sign(params):
    """所有参数按 key 排序 → k=v& 拼接 → SHA1 hex → MD5 hex"""
    pairs = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    return hashlib.md5(hashlib.sha1(pairs.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def fetch(category="", rn=50, retries=3):
    p = {"app": "CailianpressWeb", "category": category, "os": "web", "rn": str(rn), "sv": "7.7.5"}
    p["sign"] = cls_sign(p)
    qs = "&".join(f"{k}={v}" for k, v in p.items())
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"{URL}?{qs}", headers=HDR), timeout=25)
            d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("errno") == 0:
                return (d.get("data") or {}).get("roll_data") or []
            last = f"errno={d.get('errno')} msg={d.get('msg')}"
        except Exception as e:
            last = f"{type(e).__name__} {e}"
        time.sleep(1.2 + i)
    print(f"[cls] {category or 'all'} 失败: {last}", file=sys.stderr)
    return []


def slim(row):
    """抽选股相关字段, 丢弃 53 个字段里的空/广告/社交字段"""
    return {
        "id": row.get("id"),
        "ctime": row.get("ctime"),
        "time": datetime.fromtimestamp(row.get("ctime") or 0, CST).strftime("%Y-%m-%d %H:%M:%S"),
        "level": row.get("level"),                       # A=重要 B=一般 C=普通
        "brief": row.get("brief") or "",
        "content": row.get("content") or "",
        "subjects": [s.get("subject_name") for s in (row.get("subjects") or [])],
        "plate_ids": [s.get("plate_id") for s in (row.get("subjects") or []) if s.get("plate_id")],
        "stocks": [{"code": s.get("stock_code") or s.get("code"),
                    "name": s.get("name")} for s in (row.get("stock_list") or [])],
        "reading_num": row.get("reading_num"),
        "comment_num": row.get("comment_num"),
        "share_num": row.get("share_num"),
        "audio_url": (row.get("audio_url") or [None])[0],
        "shareurl": row.get("shareurl") or "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cls_telegraph.json")
    ap.add_argument("--categories", default="")
    ap.add_argument("--rn", type=int, default=50)
    args = ap.parse_args()

    cats = [c.strip() for c in args.categories.split(",") if c.strip] if args.categories else [""]
    result = {"generated": datetime.now(CST).isoformat(timespec="seconds"),
              "source": "cls_reverse_20261016", "categories": {}}
    all_ids = set()
    for c in cats:
        rows = fetch(c, args.rn)
        slimmed = [slim(r) for r in rows]
        new = sum(1 for r in slimmed if r["id"] not in all_ids)
        all_ids.update(r["id"] for r in slimmed)
        result["categories"][c or "all"] = {"n": len(slimmed), "new": new, "rows": slimmed}
        print(f"[cls] {c or 'all':12s} {len(slimmed):3d} 条 (新 {new})", flush=True)
        time.sleep(0.8)

    result["total_unique"] = len(all_ids)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"total unique {len(all_ids)} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
