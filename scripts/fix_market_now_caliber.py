#!/usr/bin/env python3
"""fix_market_now_caliber.py — 统一 market_now 涨跌停口径 + 标注 stale 文件

问题 (2026-10-13 核查):
  market_now.json 涨停 89 vs 东财 tc 权威计数 67 → 口径不一致 (89 含北交所/宽口径)
  latest.json advance 2643 vs 实测 2831 → 盘中旧快照, 收盘后未刷新

修法:
  1. market_now: 保留原宽口径值到 market.limit_up_wide, 新增权威口径 market.limit_up_strict (=tc)
  2. latest.json: 补 stale 标记 + provenance, 让前端能识别并显示"数据滞后"
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
DATA_DIR = "/root/workspace/stocks-ai-view/data"


def _probe(url, timeout=15):
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, headers=dict(UA)), timeout=timeout)
        return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__error__": f"{type(e).__name__} {e}"}


def fetch_tc(today):
    """东财 tc 权威计数 (涨跌停/炸板)。"""
    out = {}
    for kind, key in [("ZT", "limit_up_strict"), ("DT", "limit_down_strict"),
                      ("ZB", "broken_strict")]:
        d = _probe(f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
                   f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                   f"&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={today}")
        if "__error__" not in d:
            tc = (d.get("data") or {}).get("tc")
            if tc is not None:
                out[key] = int(tc)
    return out


def main():
    today = datetime.now(CST).strftime("%Y%m%d")
    ts = datetime.now(CST).isoformat(timespec="seconds")
    print(f"=== 统一 market_now 涨跌停口径 {ts} ===")

    tc = fetch_tc(today)
    print(f"东财 tc 权威计数: {tc}")
    if not tc:
        print("tc 取不到, 中止", file=sys.stderr)
        return 1

    # 1. market_now.json
    p = os.path.join(DATA_DIR, "market_now.json")
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    m = d.setdefault("market", {})
    # 宽口径值挪到 _wide
    for wide, strict in [("limit_up", "limit_up_strict"),
                         ("limit_down", "limit_down_strict"),
                         ("broken", "broken_strict")]:
        if wide in m and strict in tc:
            m[wide + "_wide"] = m[wide]
            m[wide] = tc[strict]
            print(f"  {wide}: {m[wide + '_wide']} (宽口径) → {m[wide]} (tc 权威)")
    m["caliber_note"] = ("limit_up/down/broken = 东财 tc 权威计数; "
                         "*_wide = 原宽口径 (含北交所等)")
    d["ts"] = ts
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"[写入] {p}")

    # 2. latest.json — 标 stale + provenance
    p2 = os.path.join(DATA_DIR, "latest.json")
    with open(p2, encoding="utf-8") as f:
        d2 = json.load(f)
    mt = datetime.fromtimestamp(os.path.getmtime(p2), CST)
    age_h = (datetime.now(CST) - mt).total_seconds() / 3600
    d2["stale"] = {
        "is_stale": age_h > 2,
        "age_hours": round(age_h, 2),
        "threshold_hours": 2,
        "note": "本文件为盘中旧快照, 收盘后未刷新; "
                "页面应优先读 market_now.json (最新) 并显示数据时间",
        "checked_at": ts,
    }
    prov = d2.setdefault("provenance", {})
    prov["stamped_at"] = ts
    prov["stamped_by"] = "fix_market_now_caliber.py"
    m2 = d2.get("market") or {}
    prov["fields"] = {
        "market.limit_up": {"source": "eastmoney_push2ex_ztpool_tc",
                            "confidence": "high",
                            "snapshot_ts": d2.get("ts")},
        "market.advance": {"source": "eastmoney_zdfenbu",
                           "confidence": "high",
                           "snapshot_ts": d2.get("ts")},
        "themes": {"source": "local_cluster+ths_strong", "confidence": "medium",
                   "snapshot_ts": d2.get("ts")},
        "picks": {"source": "tsp_v4_multi_pool", "confidence": "medium",
                  "snapshot_ts": d2.get("ts")},
        "ai_picks": {"source": "engine_v5_17indicator", "confidence": "medium",
                     "snapshot_ts": d2.get("ts")},
        "news_recent": {"source": "sina_7x24+remote_news_fallback",
                        "confidence": "medium", "snapshot_ts": d2.get("ts")},
    }
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(d2, f, ensure_ascii=False, indent=2)
    print(f"[写入] {p2} (age={age_h:.1f}h, stale={d2['stale']['is_stale']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
