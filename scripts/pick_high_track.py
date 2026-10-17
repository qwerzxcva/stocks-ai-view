#!/usr/bin/env python3
"""pick_high_track.py — 选出后 7 日区间高点追踪 (借鉴「AI 选股历史」页)。

参考站的口径: 展示每只选出标的在选出后 7 日内的区间最高点与可达涨幅。
与 review_yesterday (收盘对收盘胜率) 互补:
  - 收盘胜率 = 拿到手的概率 (严格)
  - 区间高点 = 理论可达空间 (上限, 需自己择时兑现)

数据源 (按优先级):
  1. data/predictions/ai_picks_YYYY-MM-DD.json   (校准后新口径, 2026-10-17 起)
  2. data/predictions/engine_v5_YYYY-MM-DD.json  (旧共振口径, 延续存档)
两者并存时同一天只用 ai_picks (新口径优先), 保证新旧口径不混测。

口径: entry = 快照里记录的现价 (当日盘中) 或 K 线收盘; high = 快照日次日
起的 7 个交易日内盘中最高价; ret = (high - entry) / entry。
快照日即今日的记录跳过 (尚无「次日之后」的 K 线)。

输出: data/pick_high_track.json
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paths import AI_VIEW_DATA as DATA_DIR
from engine_v5 import load_klines
from paths import BULK

WINDOW_DAYS = 7          # 选出后追踪窗口 (交易日)
TOP_N = 5                # 每天最多展示前 N 只
LOOKBACK_DAYS = 40       # 回看最近多少个快照文件


def bare(c):
    c = str(c or "")
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def prefixed(code):
    """裸码 → 带市场前缀。0/2/3 开头是深市, 先试 sh 会把 000001 解析成
    上证指数 (实测: 平安银行 entry 10.99 配上证 high 4175, 单只污染全表均值)。"""
    c = str(code or "")
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith(("6", "9"))
            else "bj" if c.startswith(("4", "8")) else "sz") + c


def load_snapshot(path):
    """读一个快照文件, 统一成 {date, picks: [{code,name,price}]}。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    name = os.path.basename(path)
    date = name.replace("ai_picks_", "").replace("engine_v5_", "") \
               .replace(".json", "")
    if name.startswith("ai_picks_"):
        picks = [{"code": bare(p.get("code")), "name": p.get("name"),
                  "price": p.get("price")}
                 for p in (d.get("picks") or [])]
        return {"date": date, "source": "ai_picks", "picks": picks}
    # engine_v5 旧口径: top 列表
    picks = [{"code": bare(p.get("code")), "name": None,
              "price": p.get("price")}
             for p in (d.get("top") or [])]
    return {"date": date, "source": "engine_v5", "picks": picks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    args = ap.parse_args()
    now = datetime.now(CST)

    pred_dir = os.path.join(DATA_DIR, "predictions")
    files = sorted(glob.glob(os.path.join(pred_dir, "*.json")),
                   key=os.path.getmtime)[-LOOKBACK_DAYS:]
    # 同一天 ai_picks 优先于 engine_v5 (新口径不混测)
    by_date = {}
    for f in files:
        s = load_snapshot(f)
        if not s or not s["picks"]:
            continue
        if s["date"] not in by_date or s["source"] == "ai_picks":
            by_date[s["date"]] = s
    snaps = sorted(by_date.values(), key=lambda x: x["date"])
    print(f"[high_track] 快照 {len(snaps)} 天", flush=True)

    klines = load_klines(BULK)
    today_str = now.strftime("%Y-%m-%d")

    days = []
    for s in snaps:
        if s["date"] >= today_str:
            continue
        rows = []
        for p in s["picks"][:TOP_N]:
            bars = klines.get(prefixed(p["code"]))
            if not bars:
                continue
            dates = [b[0] for b in bars]
            try:
                di = dates.index(s["date"])
            except ValueError:
                continue
            after = bars[di + 1:di + 1 + args.window]
            if not after:
                continue
            entry = p["price"] or bars[di][2]
            if not entry:
                continue
            hi_bar = max(after, key=lambda b: b[3])
            ret = round((hi_bar[3] - entry) / entry * 100, 2)
            rows.append({
                "code": p["code"], "name": p.get("name"),
                "entry": entry,
                "high_max": hi_bar[3], "high_date": hi_bar[0],
                "ret_pct": ret,
                "days_elapsed": len(after),
                "window_done": len(after) >= args.window,
            })
        if rows:
            rows.sort(key=lambda r: -r["ret_pct"])
            done = [r for r in rows if r["window_done"]]
            days.append({
                "date": s["date"], "source": s["source"],
                "picks": rows,
                "avg_ret_pct": round(sum(r["ret_pct"] for r in done) / len(done), 2)
                if done else None,
            })

    all_rows = [r for d in days for r in d["picks"] if r["window_done"]]
    stats = {
        "n": len(all_rows),
        "avg_ret_pct": (round(sum(r["ret_pct"] for r in all_rows)
                              / len(all_rows), 2) if all_rows else None),
        "pct_ge_3": (round(sum(1 for r in all_rows if r["ret_pct"] >= 3)
                           / len(all_rows) * 100, 1) if all_rows else None),
        "pct_ge_5": (round(sum(1 for r in all_rows if r["ret_pct"] >= 5)
                           / len(all_rows) * 100, 1) if all_rows else None),
        "pct_negative": (round(sum(1 for r in all_rows if r["ret_pct"] < 0)
                               / len(all_rows) * 100, 1) if all_rows else None),
    }

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "pick_high_track (选出后 7 交易日区间高点)",
        "method": "entry=快照现价, high=次日7个交易日内盘中最高价, "
                  "ai_picks 快照优先于 engine_v5 (新口径优先)",
        "stats_window_done": stats,
        "days": days,
    }
    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
        return 0
    path = os.path.join(DATA_DIR, "pick_high_track.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"[high_track] -> {path} | 已完成窗口 {stats['n']} 只, "
          f"平均可达 {stats['avg_ret_pct']}%, ≥3% {stats['pct_ge_3']}%, "
          f"≥5% {stats['pct_ge_5']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
