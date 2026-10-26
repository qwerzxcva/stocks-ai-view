#!/usr/bin/env python3
"""refresh_board_constituents.py — 重建板块成分股（当日）。

背景：App 的 api/sectors.json 原来只读 db-market 仓库的 sector/{kind}_constituents/{bk}.json，
上游停更时旧文件会被当成当日数据（2026-10-26 实测 200 个板块全部超 2 天）。
本脚本用东财 push2delay 的 clist 接口按板块重建成分股，写入本仓
data/sector/{kind}_constituents/{bk}.json，作为 App 的首选来源，db-market 只作兜底。

用法: python3 scripts/refresh_board_constituents.py [--limit N] [--kinds industry,concept]
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "sector")
CST = timezone(timedelta(hours=8))
UA = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
HOSTS = ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com")


def _fetch(bk, kind, timeout=15, attempts=4):
    last_err = None
    for attempt in range(attempts):
        for host in HOSTS:
            url = (host + "/api/qt/clist/get?pn=1&pz=400&po=1&np=1&fltt=2&invt=2"
                   "&fid=f3&fs=b:%s&fields=f12,f14" % bk)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                    payload = json.loads(r.read().decode("utf-8", "replace"))
                diff = ((payload.get("data") or {}).get("diff")) or {}
                rows = list(diff.values()) if isinstance(diff, dict) else list(diff)
                members = [{"code": str(x.get("f12")), "name": str(x.get("f14"))}
                           for x in rows if x.get("f12") and x.get("f14")]
                if members:
                    return bk, kind, members, None
            except Exception as e:
                last_err = "%s:%s" % (host.split("//")[1], type(e).__name__)
            time.sleep(0.5)
        time.sleep(0.8 * (attempt + 1))  # 东财会瞬断限流，退避后重试
    return bk, kind, [], last_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个板块（调试用）")
    ap.add_argument("--kinds", default="industry,concept")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    with open(os.path.join(ROOT, "data", "api", "sectors.json"), encoding="utf-8") as f:
        api = json.load(f)

    jobs = []
    for kind, key in (("industry", "industries"), ("concept", "concepts")):
        if kind not in args.kinds:
            continue
        for board in api.get(key) or []:
            bk = board.get("bk")
            if bk:
                jobs.append((str(bk), kind))
    if args.limit:
        jobs = jobs[:args.limit]

    now = datetime.now(CST).isoformat(timespec="seconds")
    ok, failed, total_members = 0, [], 0
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_fetch, bk, kind) for bk, kind in jobs]
        for fut in as_completed(futures):
            bk, kind, members, err = fut.result()
            if members:
                results[(bk, kind)] = members
                ok += 1
                total_members += len(members)
            else:
                failed.append("%s(%s):%s" % (bk, kind, err or "empty"))
            time.sleep(0.05)

    for (bk, kind), members in results.items():
        d = os.path.join(OUT, "%s_constituents" % kind)
        os.makedirs(d, exist_ok=True)
        payload = {
            "generated_at": now,
            "n": len(members),
            "board": bk,
            "kind": kind,
            "source": "eastmoney push2delay clist fs=b:%s" % bk,
            "members": members,
        }
        with open(os.path.join(d, "%s.json" % bk), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    print("[constituents] ok=%d failed=%d members=%d at=%s" % (ok, len(failed), total_members, now))
    if failed:
        print("[constituents] failed sample: %s" % " | ".join(failed[:8]), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
