#!/usr/bin/env python3
"""Build the tape and structure snapshot used by the realtime summary.

The output is intentionally narrow:
- index 5-minute bars, with a volume spike compared with the session median;
- theme catalysts whose validity is counted in trading days, not guessed;
- weekly and 60-minute structure for the Shanghai Composite.
"""
import argparse
import json
import os
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def fetch_json(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def tencent_bars(code, period, count):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},{period},,,{count}"
    payload = fetch_json(url)
    node = ((payload.get("data") or {}).get(code) or {})
    rows = node.get(period) or node.get("day") or []
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append({"time": row[0], "open": float(row[1]), "close": float(row[2]),
                       "high": float(row[3]), "low": float(row[4]), "volume": float(row[5])})
    return parsed


def load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def minute_events(bars):
    if len(bars) < 12:
        return []
    baseline = statistics.median(bar["volume"] for bar in bars if bar["volume"] > 0) or 1
    events = []
    for index, bar in enumerate(bars):
        ratio = bar["volume"] / baseline
        if ratio < 2 or index == 0:
            continue
        move = (bar["close"] / bars[index - 1]["close"] - 1) * 100
        if abs(move) < 0.15:
            continue
        events.append({"time": bar["time"], "close": bar["close"], "move_pct": round(move, 3),
                       "volume_ratio": round(ratio, 2),
                       "label": "放量上攻" if move > 0 else "放量下压"})
    return events[-8:]


def event_timeline(attribution, sectors):
    """Merge observable intraday events without inventing an exact clock time.

    Limit-up rows carry the exchange seal time. Sector rows are snapshot facts,
    so they use the engine timestamp and are labeled as snapshot events.
    """
    def money(value):
        try:
            return f"{float(value) / 1e8:.2f}亿"
        except (TypeError, ValueError):
            return "—"

    limit_rows = []
    for item in (attribution.get("items") or [])[:24]:
        seal = str(item.get("seal_time") or "")
        if not seal:
            continue
        limit_rows.append({
            "time": seal.zfill(6),
            "kind": "limit_up",
            "code": item.get("code"),
            "name": item.get("name"),
            "label": item.get("seal_label") or item.get("seal_bucket") or "涨停",
            "detail": item.get("industry") or "",
            "source": "limitup_attribution",
        })
    snapshot = sectors.get("ts")
    sector_rows = []
    for item in (sectors.get("sectors") or [])[:12]:
        if item.get("grade") not in ("A", "B") and not item.get("n_zt"):
            continue
        sector_rows.append({
            "time": snapshot,
            "kind": "sector_snapshot",
            "code": item.get("code"),
            "name": item.get("sector"),
            "label": f"{item.get('grade')}级板块",
            "detail": f"涨停{item.get('n_zt') or 0} 主力净额{money(item.get('capital_net'))}",
            "source": "sector_engine",
        })
    return limit_rows + sector_rows[:8]


def catalyst_rows(news, today):
    rows = []
    for item in (news.get("items") or [])[:80]:
        title = item.get("title") or ""
        if not any(word in title for word in ("规划", "政策", "催化", "涨价", "订单", "突破")):
            continue
        stamp = str(item.get("time") or "")[:10]
        try:
            age = (today - datetime.strptime(stamp, "%Y-%m-%d").date()).days
        except ValueError:
            age = None
        rows.append({"time": item.get("time"), "title": title[:80], "age_days": age,
                     "status": "有效" if age is not None and age <= 5 else "过期或无日期",
                     "source": item.get("source")})
    return rows[:12]


def structure(weekly, hourly):
    def box(rows):
        if len(rows) < 20:
            return None
        sample = rows[-20:]
        return {"high": max(row["high"] for row in sample), "low": min(row["low"] for row in sample),
                "close": rows[-1]["close"], "bars": len(sample)}

    def divergence(rows):
        if len(rows) < 30:
            return "样本不足"
        first, last = rows[-30:-15], rows[-15:]
        price_up = max(row["high"] for row in last) > max(row["high"] for row in first)
        volume_down = statistics.mean(row["volume"] for row in last) < statistics.mean(row["volume"] for row in first)
        price_down = min(row["low"] for row in last) < min(row["low"] for row in first)
        volume_up = statistics.mean(row["volume"] for row in last) > statistics.mean(row["volume"] for row in first)
        if price_up and volume_down:
            return "价格新高但量能下降"
        if price_down and volume_up:
            return "价格新低但量能放大"
        return "未确认背离"

    return {"weekly_box": box(weekly), "hourly_box": box(hourly),
            "weekly_divergence": divergence(weekly), "hourly_divergence": divergence(hourly)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(DATA, "tape_structure.json"))
    args = parser.parse_args()
    minute = tencent_bars("sh000001", "m5", 48)
    weekly = tencent_bars("sh000001", "week", 80)
    hourly = tencent_bars("sh000001", "m60", 80)
    news = load("news_extra.json")
    timeline = event_timeline(load("limitup_attribution.json"), load("sector_engine.json"))
    payload = {"ts": datetime.now(CST).isoformat(timespec="seconds"), "engine": "tape_structure v2",
               "index": "sh000001", "minute_events": minute_events(minute),
               "event_timeline": timeline,
               "catalysts": catalyst_rows(news, datetime.now(CST).date()),
               "structure": structure(weekly, hourly),
               "meta": {"source": "Tencent 5-minute, weekly, 60-minute bars plus news_extra",
                        "behavior_rule": "A bullish headline with negative main-flow evidence is marked separately by news_transmission"}}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"[tape] events={len(payload['minute_events'])} catalysts={len(payload['catalysts'])} -> {args.out}")


if __name__ == "__main__":
    main()
