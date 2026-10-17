#!/usr/bin/env python3
"""morning_review.py — early-session review of the current picks.

Pulls live quotes for every pick in today_picks.json and reports how each is
actually behaving in the first minutes of the session, alongside the market
breadth. Historical fact only; nothing here predicts the close.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA
except Exception:
    DATA = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def fetch(codes):
    out = {}
    for i in range(0, len(codes), 20):
        batch = codes[i:i + 20]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
        except Exception as e:
            log(f"fetch fail: {e}")
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
                    "open": float(p[5] or 0),
                    "chg_pct": float(p[32] or 0),
                    "high": float(p[33] or 0),
                    "low": float(p[34] or 0),
                    "volume": float(p[36] or 0),
                    "amount": float(p[37] or 0),
                    "turnover": float(p[38] or 0),
                    "time": p[30],
                }
            except (ValueError, IndexError):
                pass
    return out


def prefixed(code):
    c = str(code)
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith(("6", "5", "9")) else
            "bj" if c.startswith(("4", "8", "9")) else "sz") + c


def main():
    tp_path = os.path.join(DATA, "today_picks.json")
    if not os.path.exists(tp_path):
        log("today_picks.json missing")
        return 1
    tp = json.load(open(tp_path, encoding="utf-8"))

    # collect every code across the three pick groups
    groups = {}
    for g in ("signals", "values", "limit_up"):
        rows = (tp.get("picks") or {}).get(g) or []
        groups[g] = [r["code"] for r in rows]
    all_codes = [prefixed(c) for g in groups.values() for c in g]
    log(f"picks: {sum(len(v) for v in groups.values())} codes")

    # 回避榜单独测量 (2026-10-17): 不进 groups —— afternoon_picks 会把
    # groups 全部当候选池; 回避榜的验证口径是「命中率=下跌占比」。
    avoid_codes = [r["code"]
                   for r in ((tp.get("picks") or {}).get("avoid") or [])]
    for c in avoid_codes:
        all_codes.append(prefixed(c))
    log(f"avoid: {len(avoid_codes)} codes")

    q = fetch(all_codes)
    log(f"quotes: {len(q)}/{len(all_codes)}")

    # market breadth snapshot
    idx_codes = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688"]
    idx = fetch(idx_codes)
    log(f"indices: {len(idx)}")

    report = {"generated_at": datetime.now(CST).isoformat(timespec="seconds"),
              "indices": idx, "groups": {}}

    def _measure(rows, hit_is_up=True):
        n = len(rows)
        n_up = sum(1 for r in rows if r["chg_pct"] > 0)
        n_down = sum(1 for r in rows if r["chg_pct"] < 0)
        hits = n_up if hit_is_up else n_down
        out = {
            "n": n,
            "hit_rate_pct": round(hits / n * 100, 1) if n else None,
            "avg_ret_pct": round(sum(r["chg_pct"] for r in rows) / n, 2) if n else None,
            "rows": rows,
        }
        # 字段语义保持旧口径: 组统计的 n_up=上涨只数; 回避榜另加 n_down=下跌只数
        out["n_up" if hit_is_up else "n_down"] = hits
        if not hit_is_up:
            out["n_up"] = n_up
        return out

    for g, codes in groups.items():
        rows = []
        for c in codes:
            v = q.get(prefixed(c))
            if not v:
                continue
            rows.append({"code": c, "name": v["name"],
                         "price": v["price"], "prev_close": v["prev_close"],
                         "open": v["open"], "chg_pct": round(v["chg_pct"], 2),
                         "high": v["high"], "low": v["low"],
                         "turnover": v["turnover"], "time": v["time"]})
        rows.sort(key=lambda r: -r["chg_pct"])
        report["groups"][g] = _measure(rows)

    # 回避榜验证口径: 命中=下跌 (回避成功), 命中率越高回避榜越有效
    avoid_rows = []
    for c in avoid_codes:
        v = q.get(prefixed(c))
        if not v:
            continue
        avoid_rows.append({"code": c, "name": v["name"],
                           "price": v["price"],
                           "chg_pct": round(v["chg_pct"], 2),
                           "turnover": v["turnover"], "time": v["time"]})
    report["avoid"] = _measure(avoid_rows, hit_is_up=False)

    out = os.path.join(DATA, "morning_review.json")
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {out}")

    print()
    print("=== 大盘 (早盘) ===")
    for k, v in idx.items():
        print(f"  {v['name']:10s} {v['price']:>10.2f} {v['chg_pct']:+.2f}%  "
              f"成交{(v['amount']/1e8):.0f}亿  {v['time']}")
    print()
    for g, s in report["groups"].items():
        print(f"=== {g} (n={s['n']}) 命中率 {s['hit_rate_pct']}% 平均 {s['avg_ret_pct']}% ===")
        for r in s["rows"][:6]:
            print(f"  {r['code']} {r['name']:10s} {r['chg_pct']:+.2f}%  "
                  f"现价{r['price']} 换手{r['turnover']}%")
        if s["n"] > 6:
            print(f"  ... 共 {s['n']} 只")
        print()
    av = report.get("avoid") or {}
    if av.get("n"):
        print(f"=== avoid 回避榜 (n={av['n']}) 回避成功率(下跌占比) "
              f"{av['hit_rate_pct']}% 平均 {av['avg_ret_pct']}% ===")
        for r in av["rows"][:6]:
            print(f"  {r['code']} {r['name']:10s} {r['chg_pct']:+.2f}%")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
