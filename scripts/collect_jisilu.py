#!/usr/bin/env python3
"""
collect_jisilu.py — 集思录 2026 新版接口采集 (LOF/ETF/可转债/新股)

接口路径 (2026-10-13 逆向确认, 老 .php 已全 404):
  /data/lof/index_lof_list/    指数 LOF
  /data/lof/stock_lof_list/    股票 LOF
  /data/etf/etf_list/          ETF (费率/管理人/申赎单位, 独家字段)
  /data/cbnew/cb_list_new/     可转债新券
  /data/cbnew/adjust_list/     可转债下修/强赎 (319 条)
  /data/cbnew/delisted/        可转债退市 (733 条)
  /data/new_stock/apply/       新股申购日历
  /data/new_stock/hkipo/       港股 IPO
  /data/new_stock/sub/         新债申购
  /data/new_stock/super/       超级申 (顶格申购)

鉴权: 匿名 session (先访问首页拿 kbzw__Session cookie) + Referer + X-Requested-With
分页: 每页 20 行 (etf/lof), config 在页面内联 JS; 用 page 参数翻页

用法:
  python3 collect_jisilu.py --out jisilu_20261013.json
  python3 collect_jisilu.py --out x.json --pages 3     # 每接口翻 3 页
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
BASE = "https://www.jisilu.cn"

# 接口 → (页面 Referer, 每页行数, 最大页建议)
ENDPOINTS = {
    "lof_index": ("/data/lof/index_lof_list/", "/data/lof/", 20, 5),
    "lof_stock": ("/data/lof/stock_lof_list/", "/data/lof/", 20, 5),
    "etf_list": ("/data/etf/etf_list/", "/data/etf/", 20, 5),
    "cb_list_new": ("/data/cbnew/cb_list_new/", "/data/cbnew/", 30, 4),
    "cb_adjust": ("/data/cbnew/adjust_list/", "/data/cbnew/", 20, 16),
    "cb_delisted": ("/data/cbnew/delisted/", "/data/cbnew/", 20, 37),
    "new_stock_apply": ("/data/new_stock/apply/", "/data/new_stock/", 100, 2),
    "new_stock_hkipo": ("/data/new_stock/hkipo/", "/data/new_stock/", 100, 2),
    "new_stock_sub": ("/data/new_stock/sub/", "/data/new_stock/", 100, 1),
    "new_stock_super": ("/data/new_stock/super/", "/data/new_stock/", 100, 2),
}


def _opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # 先访问首页拿匿名 session
    try:
        op.open(urllib.request.Request(BASE + "/", headers=dict(UA)), timeout=15).read()
    except Exception as e:
        print(f"[warn] 首页访问失败 {type(e).__name__}", file=sys.stderr)
    return op


def _fetch(op, path, referer, page=1, retries=2):
    url = f"{BASE}{path}?page={page}&___t={int(time.time() * 1000)}"
    h = dict(UA)
    h["Referer"] = BASE + referer
    h["X-Requested-With"] = "XMLHttpRequest"
    for i in range(retries):
        try:
            r = op.open(urllib.request.Request(url, headers=h), timeout=20)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                print(f"    [fail] {path} page{page} {type(e).__name__}", file=sys.stderr)
            time.sleep(0.5)
    return {}


def collect_endpoint(op, name, spec, max_pages, sleep_s=0.6):
    path, referer, per_page, _ = spec
    rows = []
    seen = set()
    for page in range(1, max_pages + 1):
        d = _fetch(op, path, referer, page)
        batch = d.get("rows") or []
        if not batch:
            break
        # page 参数对该站无效 (每页都返回全量) → 用 id 去重, 重复即停
        new = 0
        for r in batch:
            cell = r.get("cell", r)
            rid = r.get("id") or cell.get("bond_id") or cell.get("fund_id") \
                or cell.get("stock_cd") or json.dumps(cell, sort_keys=True, default=str)
            if rid in seen:
                continue
            seen.add(rid)
            cell["_id"] = r.get("id", "")
            rows.append(cell)
            new += 1
        print(f"  [{name}] page {page}: +{new} 行 (累计 {len(rows)})")
        if new == 0:
            print(f"  [{name}] 无新数据, 停止翻页")
            break
        time.sleep(sleep_s)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--pages", type=int, default=0,
                    help="每接口最大页数, 0=用建议值 (大表会慢)")
    ap.add_argument("--only", help="只采某个接口 (lof_index/etf_list/...)")
    args = ap.parse_args()

    op = _opener()
    out = {
        "timestamp": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "jisilu_2026",
        "container": "gha-ubuntu" if os.environ.get("GITHUB_ACTIONS") else "local",
        "endpoints": {},
    }

    targets = [args.only] if args.only else list(ENDPOINTS)
    for name in targets:
        spec = ENDPOINTS[name]
        max_pages = args.pages or spec[3]
        try:
            rows = collect_endpoint(op, name, spec, max_pages)
            out["endpoints"][name] = {
                "url": BASE + spec[0],
                "n": len(rows),
                "rows": rows,
            }
        except Exception as e:
            print(f"  [{name}] 失败 {type(e).__name__}: {e}", file=sys.stderr)
            out["endpoints"][name] = {"url": BASE + spec[0], "n": 0, "rows": [],
                                      "error": f"{type(e).__name__}: {e}"}

    total = sum(v["n"] for v in out["endpoints"].values())
    out["total_rows"] = total

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"[done] {total} 行 → {args.out}")
    for k, v in out["endpoints"].items():
        print(f"  {k:20s} {v['n']:5d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
