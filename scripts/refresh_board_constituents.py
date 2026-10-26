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
MARKET_INDEX = ("https://raw.githubusercontent.com/jiegengxiangjianxue1/db-market/"
                "main/sector/stock_to_board.json")


def load_inverted_index():
    """云端库 stock_to_board.json → {bk: [(code, name)]}，作为限流时的等价兜底。

    诚实标注：这是反演结果（股票→板块 取逆），不是东财直接返回的成分股；
    覆盖不到的板块会返回空，由调用方标记 stale。
    """
    try:
        with urllib.request.urlopen(urllib.request.Request(MARKET_INDEX, headers=UA),
                                    timeout=40) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        print("[constituents] invert index 不可达: %s" % type(exc).__name__, file=sys.stderr)
        return {}, None
    inv = {}
    for code, node in (data.get("stocks") or {}).items():
        for kind_key in ("industry", "concept"):
            for bk in (node.get(kind_key) or []):
                inv.setdefault(str(bk), []).append(str(code))
    return inv, data.get("n_constituents_filled")


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
                if members or payload.get("data") is not None:
                    # data 非 None 但 diff 为空 = 接口确认该板块今日无成分，不是抓取失败
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
    ap.add_argument("--no-invert", action="store_true",
                    help="禁用云端库反演兜底（默认开启）")
    ap.add_argument("--timeout", type=int, default=15, help="单次请求超时秒")
    ap.add_argument("--attempts", type=int, default=4, help="每板块重试次数")
    ap.add_argument("--invert-only", action="store_true",
                    help="跳东财直取，只用云端库反演（限流时的快速全量模式）")
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

    # 已有当日文件的板块不重建（避免反演结果覆盖东财直取结果）
    kept = set()
    for bk, kind in jobs:
        p = os.path.join(OUT, "%s_constituents" % kind, "%s.json" % bk)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            ts = datetime.fromisoformat(str(d.get("generated_at") or ""))
            if d.get("n") and (datetime.now(CST) - ts).total_seconds() < 86400:
                kept.add((bk, kind))
            elif d.get("empty_confirmed") and (datetime.now(CST) - ts).total_seconds() < 86400:
                kept.add((bk, kind))
        except Exception:
            pass
    if kept:
        print("[constituents] keep today's %d boards" % len(kept))
    jobs = [j for j in jobs if j not in kept]

    now = datetime.now(CST).isoformat(timespec="seconds")
    ok, failed, total_members = 0, [], 0
    inv, _filled = load_inverted_index() if not args.no_invert else ({}, None)
    name_map = {}
    try:
        with open(os.path.join(ROOT, "data", "stock_names.json"), encoding="utf-8") as f:
            name_map = {str(k)[-6:]: v for k, v in json.load(f).items()}
    except Exception:
        pass
    derived = 0
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [] if args.invert_only else [pool.submit(_fetch, bk, kind, args.timeout, args.attempts) for bk, kind in jobs]
        for fut in as_completed(futures):
            bk, kind, members, err = fut.result()
            if members:
                results[(bk, kind)] = (members, "eastmoney push2delay clist fs=b:%s" % bk)
                ok += 1
                total_members += len(members)
            else:
                failed.append("%s(%s):%s" % (bk, kind, err or "empty"))
            time.sleep(0.05)

    # 限流兜底：用云端库反演补上东财没拿到的板块
    for bk, kind in jobs:
        if (bk, kind) in results:
            continue
        codes = inv.get(bk) or []
        if codes:
            members = [{"code": c, "name": name_map.get(c, "")} for c in codes]
            results[(bk, kind)] = (members, "db-market:stock_to_board (inverted)")
            derived += 1
            total_members += len(members)

    for (bk, kind), (members, source) in results.items():
        d = os.path.join(OUT, "%s_constituents" % kind)
        os.makedirs(d, exist_ok=True)
        payload = {
            "generated_at": now,
            "n": len(members),
            "empty_confirmed": len(members) == 0 and source.startswith("eastmoney"),
            "board": bk,
            "kind": kind,
            "source": source,
            "members": members,
        }
        with open(os.path.join(d, "%s.json" % bk), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    print("[constituents] ok=%d(inverted=%d) failed=%d members=%d at=%s"
          % (ok + derived, derived, len(failed) - derived, total_members, now))
    if failed:
        print("[constituents] failed sample: %s" % " | ".join(failed[:8]), file=sys.stderr)
    return 0 if (ok + derived) else 1


if __name__ == "__main__":
    sys.exit(main())
