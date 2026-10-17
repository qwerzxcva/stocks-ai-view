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
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from indicators_ext import ema

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


def volume_pressure(bars, window=12):
    """量能差: rolling up-bar volume minus down-bar volume, aligned with index close.

    On 5-minute bars this is the volume-differential series drawn against the
    index line; each point keeps its window sums so the front end can render
    the paired chart without re-deriving anything.
    """
    if len(bars) < window + 2:
        return None
    series = []
    up_sum = down_sum = 0.0
    for i, bar in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i else bar["open"]
        if bar["close"] >= prev_close:
            up_sum += bar["volume"]
        else:
            down_sum += bar["volume"]
        if i >= window:
            old = bars[i - window]
            old_prev = bars[i - window - 1]["close"] if i - window else old["open"]
            if old["close"] >= old_prev:
                up_sum -= old["volume"]
            else:
                down_sum -= old["volume"]
        if i < window - 1:
            continue
        total = up_sum + down_sum
        series.append({"time": bar["time"], "close": bar["close"],
                       "up_vol": round(up_sum, 1), "down_vol": round(down_sum, 1),
                       "diff": round(up_sum - down_sum, 1),
                       "diff_ratio": round((up_sum - down_sum) / total, 4) if total else 0.0})
    look = min(8, len(series) - 1)
    if look < 1:
        return None
    now, past = series[-1], series[-1 - look]
    close_up = now["close"] > past["close"]
    diff_up = now["diff"] > past["diff"]
    if close_up and not diff_up:
        alignment = "指数上行但量能差走弱，反弹动能存疑"
    elif not close_up and diff_up:
        alignment = "指数下行但量能差收敛，抛压减弱"
    elif close_up and diff_up:
        alignment = "量价同向向上"
    else:
        alignment = "量价同向向下"
    return {"window": window, "alignment": alignment, "series": series}


def macd_lines(closes, fast=12, slow=26, signal=9):
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    dif = [None if a is None or b is None else a - b for a, b in zip(fast_ema, slow_ema)]
    seed = next((v for v in dif if v is not None), 0.0)
    dea = ema([v if v is not None else seed for v in dif], signal)
    return dif, dea


def kdj_lines(rows, n=9):
    ks, ds = [], []
    k = d = 50.0
    for i, row in enumerate(rows):
        win = rows[max(0, i - n + 1):i + 1]
        low_n = min(r["low"] for r in win)
        high_n = max(r["high"] for r in win)
        rsv = 50.0 if high_n == low_n else (row["close"] - low_n) / (high_n - low_n) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        ks.append(k)
        ds.append(d)
    return ks, ds


def dmi_lines(rows, n=14):
    size = len(rows)
    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for i in range(1, size):
        high, low = rows[i]["high"], rows[i]["low"]
        prev_close, prev_high, prev_low = rows[i - 1]["close"], rows[i - 1]["high"], rows[i - 1]["low"]
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up, down = high - prev_high, prev_low - low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    atr = ema(tr, n)
    plus_s = ema(plus_dm, n)
    minus_s = ema(minus_dm, n)

    def ratio(num, den):
        return [None if a is None or not b else 100 * a / b for a, b in zip(num, den)]

    pdi = ratio(plus_s, atr)
    ndi = ratio(minus_s, atr)
    dx = [None if a is None or b is None or not (a + b) else 100 * abs(a - b) / (a + b)
          for a, b in zip(pdi, ndi)]
    adx = ema([v if v is not None else 0.0 for v in dx], n)
    return pdi, ndi, adx


def _swing_divergence(rows, indicator, window=60):
    """Compare price extremes of the two half-windows against the indicator at
    the same bars. Returns a label or None."""
    if len(rows) < window:
        return None
    seg = rows[-window:]
    ind = indicator[-window:]
    half = window // 2
    hi_a = max(range(half), key=lambda i: seg[i]["high"])
    hi_b = max(range(half, window), key=lambda i: seg[i]["high"])
    lo_a = min(range(half), key=lambda i: seg[i]["low"])
    lo_b = min(range(half, window), key=lambda i: seg[i]["low"])
    if ind[hi_a] is not None and ind[hi_b] is not None and \
            seg[hi_b]["high"] > seg[hi_a]["high"] and ind[hi_b] < ind[hi_a]:
        return "顶背离"
    if ind[lo_a] is not None and ind[lo_b] is not None and \
            seg[lo_b]["low"] < seg[lo_a]["low"] and ind[lo_b] > ind[lo_a]:
        return "底背离"
    return None


def indicator_snapshot(rows, label):
    """MACD / KDJ / DMI state plus swing divergence for one timeframe."""
    if not rows:
        return None
    closes = [r["close"] for r in rows]
    dif, dea = macd_lines(closes)
    ks, ds = kdj_lines(rows)
    pdi, ndi, adx = dmi_lines(rows)

    def tail(series):
        return round(series[-1], 3) if series[-1] is not None else None

    return {
        "timeframe": label,
        "macd": {"dif": tail(dif), "dea": tail(dea),
                 "divergence": _swing_divergence(rows, dif)},
        "kdj": {"k": tail(ks), "d": tail(ds),
                "divergence": _swing_divergence(rows, ds)},
        "dmi": {"pdi": tail(pdi), "ndi": tail(ndi), "adx": tail(adx),
                "divergence": _swing_divergence(rows, adx)},
    }


def structure(weekly, hourly, daily):
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

    return {"weekly_box": box(weekly), "hourly_box": box(hourly), "daily_box": box(daily),
            "weekly_divergence": divergence(weekly), "hourly_divergence": divergence(hourly)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(DATA, "tape_structure.json"))
    args = parser.parse_args()
    minute = tencent_bars("sh000001", "m5", 48)
    weekly = tencent_bars("sh000001", "week", 80)
    hourly = tencent_bars("sh000001", "m60", 80)
    daily = tencent_bars("sh000001", "day", 120)
    news = load("news_extra.json")
    timeline = event_timeline(load("limitup_attribution.json"), load("sector_engine.json"))
    payload = {"ts": datetime.now(CST).isoformat(timespec="seconds"), "engine": "tape_structure v3",
               "index": "sh000001", "minute_events": minute_events(minute),
               "volume_pressure": volume_pressure(minute),
               "event_timeline": timeline,
               "catalysts": catalyst_rows(news, datetime.now(CST).date()),
               "structure": structure(weekly, hourly, daily),
               "indicators": {"daily": indicator_snapshot(daily[-80:], "日线"),
                              "hourly": indicator_snapshot(hourly, "60分钟")},
               "meta": {"source": "Tencent 5-minute, daily, weekly, 60-minute bars plus news_extra",
                        "behavior_rule": "A bullish headline with negative main-flow evidence is marked separately by news_transmission"}}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"[tape] events={len(payload['minute_events'])} catalysts={len(payload['catalysts'])} -> {args.out}")


if __name__ == "__main__":
    main()
