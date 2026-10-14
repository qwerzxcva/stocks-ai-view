#!/usr/bin/env python3
"""review_yesterday.py — compare yesterday's engine_v5 picks with today's quotes.

Writes data/review_today.json: per-pick entry price (yesterday's close), today's
price, the realised return, and the aggregate hit rate. Historical fact only;
it says nothing about tomorrow.
"""
import glob
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
DATA = "/root/workspace/stocks-ai-view/data"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def log(m):
    print(f"[review] {m}", flush=True)


def fetch_quotes(codes):
    out = {}
    for i in range(0, len(codes), 20):
        batch = codes[i:i + 20]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
        except Exception as e:
            log(f"quote fetch failed: {e}")
            continue
        for m in re.finditer(r"v_(\w+)=\"([^\"]*)\"", raw):
            p = m.group(2).split("~")
            if len(p) < 40:
                continue
            try:
                out[m.group(1)] = {
                    "name": p[1],
                    "price": float(p[3] or 0),
                    "prev_close": float(p[4] or 0),
                    "chg_pct": float(p[32] or 0),
                }
            except ValueError:
                pass
    return out


def main():
    files = sorted(glob.glob(os.path.join(DATA, "predictions", "engine_v5_*.json")))
    if len(files) < 2:
        log("need at least 2 prediction snapshots")
        return 1
    prev, cur = files[-2], files[-1]
    log(f"prev={os.path.basename(prev)} cur={os.path.basename(cur)}")

    y = json.load(open(prev, encoding="utf-8"))
    picks = y.get("top") or []
    if not picks:
        log("no picks in previous snapshot")
        return 1
    codes = [p["code"] for p in picks]
    quotes = fetch_quotes(codes)
    log(f"quotes: {len(quotes)}/{len(codes)}")

    rows = []
    for p in picks:
        q = quotes.get(p["code"])
        if not q or not q["prev_close"]:
            continue
        rows.append({
            "code": p["code"],
            "name": q["name"],
            "score": p.get("score"),
            "resonance": p.get("resonance"),
            "prev_close": q["prev_close"],
            "now": q["price"],
            "chg_pct": round(q["chg_pct"], 2),
        })
    rows.sort(key=lambda r: -r["chg_pct"])
    n = len(rows)
    up = sum(1 for r in rows if r["chg_pct"] > 0)
    payload = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "prev_snapshot": os.path.basename(prev),
        "n_picks": n,
        "n_up": up,
        "hit_rate_pct": round(up / n * 100, 1) if n else None,
        "avg_ret_pct": round(sum(r["chg_pct"] for r in rows) / n, 2) if n else None,
        "best": rows[:5],
        "worst": rows[-5:],
        "all": rows,
        "note": "Historical fact only. Says nothing about tomorrow.",
    }
    out = os.path.join(DATA, "review_today.json")
    json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {out}")
    log(f"hit rate {payload['hit_rate_pct']}% (n={n}), avg {payload['avg_ret_pct']}%")
    for r in rows[:8]:
        log(f"  {r['code']} {r['name']:10s} {r['chg_pct']:+.2f}% (score {r['score']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
