#!/usr/bin/env python3
"""key_levels.py — 候选股关键价位引擎 (借鉴 tick-stock-panel 个股关键价位页)。

对当日各池候选股 (ai_picks / today_picks / ban_pick), 从本地 K 线计算一套
关键价位与偏离排名, 全部复用 indicators_ext 现有实现 (零新指标代码):

  pivot       经典 Floor 枢轴 (昨收 H/L/C → P/R1/S1/R2/S2), 当日锚点
  boll        布林 20/2 (中/上/下)
  keltner     短 (10,1.5) / 中 (20,2) / 长 (30,2.5) 三档上下轨
  atr14       ATR(14) 及 收盘 ±1 ATR 波动通道
  donchian20  20 日前高/前低 (donchian 上下轨)
  vegas       Vegas 隧道 EMA144/EMA169 (长趋势过滤)
  dev10_pct / dev10_rank   10 日 MA 偏离百分比 + 全市场百分位排名
  dev30_pct / dev30_rank   30 日 MA 偏离百分比 + 全市场百分位排名
  (偏离第 N 借鉴自盯盘页「十日偏离第26」; 排名用全市场单遍扫描)

输出: data/key_levels.json {ts, date, n, stocks: {code: {...}}}
消费: 前端个股关键价位面板 + price_alerts (点位提醒的自动生成层)。

用法: python3 key_levels.py [--dry-run]
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

import indicators_ext as IE
from paths import AI_VIEW_DATA as DATA_DIR
from engine_v5 import load_klines
from paths import BULK

OUT = os.path.join(DATA_DIR, "key_levels.json")


def prefixed(code):
    """裸码 → 带市场前缀。0/2/3 开头是深市, 不能先试 sh——
    000001 先试 sh 会命中上证指数 (与 stocks-ai-view 同前缀规则, morning_review 同款)。"""
    c = str(code or "")
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith(("6", "9"))
            else "bj" if c.startswith(("4", "8")) else "sz") + c


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def candidate_codes():
    """各池候选股裸码集合 (ai_picks 买榜+回避榜 / today_picks 三组 / ban_pick)。"""
    codes = set()

    def bare(c):
        c = str(c or "")
        return c[2:] if c[:2] in ("sh", "sz", "bj") else c

    ap = load("ai_picks.json")
    for p in (ap.get("picks") or []):
        codes.add(bare(p.get("code")))
    for a in (ap.get("avoid") or []):
        codes.add(bare(a.get("code")))
    tp = load("today_picks.json")
    pk = tp.get("picks") or {}
    for g in ("signals", "values", "limit_up", "avoid"):
        for p in (pk.get(g) or []):
            codes.add(bare(p.get("code")))
    bp = load("ban_pick.json")
    for p in (bp.get("picks") or []):
        codes.add(bare(p.get("code")))
    codes.discard("")
    return sorted(codes)


def _last(arr):
    v = arr[-1] if arr else None
    return round(v, 3) if isinstance(v, (int, float)) else None


def compute_one(bars):
    """bars: [date, open, close, high, low, vol] → 关键价位 dict 或 None。"""
    if len(bars) < 65:   # vegas 169 不可用也没关系, 其余指标需要 >30
        pass
    dates = [b[0] for b in bars]
    close = [b[2] for b in bars]
    high = [b[3] for b in bars]
    low = [b[4] for b in bars]
    if len(bars) < 30:
        return None

    px = close[-1]
    out = {"price": px, "kline_date": dates[-1]}

    # pivot: 经典口径用「昨日」H/L/C (pivot_points 逐日行, 取倒数第二日)
    if len(bars) >= 2:
        piv = IE.pivot_points(high[:-1], low[:-1], close[:-1])[-1]
        out["pivot"] = {k: piv[k] for k in ("pp", "r1", "s1", "r2", "s2")}

    mid, up, lo = IE.keltner(high, low, close, 10, 10, 1.5)
    mid20, up20, lo20 = IE.keltner(high, low, close, 20, 10, 2.0)
    mid30, up30, lo30 = IE.keltner(high, low, close, 30, 10, 2.5)
    # 布林 = 20 日 SMA ± 2σ (indicators_ext 无现成布林, 用 sma+std 手算两行)
    ma20 = IE.sma(close, 20)
    seg = close[-20:]
    mu = sum(seg) / len(seg)
    sd = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
    out["boll"] = {"mid": round(mu, 3), "up": round(mu + 2 * sd, 3),
                   "lo": round(mu - 2 * sd, 3)}
    out["keltner"] = {
        "short": {"mid": _last(mid), "up": _last(up), "lo": _last(lo)},
        "mid":   {"mid": _last(mid20), "up": _last(up20), "lo": _last(lo20)},
        "long":  {"mid": _last(mid30), "up": _last(up30), "lo": _last(lo30)},
    }
    a14 = IE.atr(high, low, close, 14)
    a14v = a14[-1]
    if a14v:
        out["atr14"] = round(a14v, 3)
        out["atr_channel"] = {"up": round(px + a14v, 3),
                              "lo": round(px - a14v, 3)}
    du, dm, dl = IE.donchian(high, low, 20)
    out["donchian20"] = {"up": _last(du), "mid": _last(dm), "lo": _last(dl)}
    if len(close) >= 170:
        e144 = IE.ema(close, 144)
        e169 = IE.ema(close, 169)
        out["vegas"] = {"ema144": _last(e144), "ema169": _last(e169)}
    out["ma20"] = round(mu, 3)

    def dev(n):
        if len(close) < n:
            return None
        m = sum(close[-n:]) / n
        return round((px - m) / m * 100, 2) if m else None

    out["dev10_pct"] = dev(10)
    out["dev30_pct"] = dev(30)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now(CST)

    codes = candidate_codes()
    print(f"[key_levels] 候选 {len(codes)} 只", flush=True)

    klines = load_klines(BULK)
    # 全市场偏离分布 (单遍扫描, 用于「十日偏离第 N」百分位排名)
    def devs_of(bars, n):
        if len(bars) < n:
            return None
        c = [b[2] for b in bars]
        m = sum(c[-n:]) / n
        return abs((c[-1] - m) / m * 100) if m else None

    all_d10, all_d30 = [], []
    for bars in klines.values():
        d10 = devs_of(bars, 10)
        d30 = devs_of(bars, 30)
        if d10 is not None:
            all_d10.append(d10)
        if d30 is not None:
            all_d30.append(d30)
    all_d10.sort()
    all_d30.sort()
    print(f"[key_levels] 全市场偏离分布: 10日 {len(all_d10)} / 30日 {len(all_d30)}",
          flush=True)

    # 60日新高/新低广度 (借鉴 tick-stock-panel 市场看板, 同一次 K 线遍历)
    n_h60 = n_l60 = 0
    for bars in klines.values():
        if len(bars) < 60:
            continue
        hi60 = max(b[3] for b in bars[-60:])
        lo60 = min(b[4] for b in bars[-60:])
        last_close = bars[-1][2]
        last_high = bars[-1][3]
        last_low = bars[-1][4]
        if last_high >= hi60:
            n_h60 += 1
        if last_low <= lo60:
            n_l60 += 1
    breadth = {
        "new_high_60": n_h60,
        "new_low_60": n_l60,
        "high_low_ratio": (round(n_h60 / n_l60, 2) if n_l60 else None),
        "n_scanned": sum(1 for bars in klines.values() if len(bars) >= 60),
    }
    print(f"[key_levels] 60日新高 {n_h60} / 新低 {n_l60} "
          f"(高低比 {breadth['high_low_ratio']})", flush=True)

    def rank(dev_abs, dist):
        """|偏离| 在全市场的百分位 (越大越靠前; 返回整数 N = 分位%)。"""
        if dev_abs is None or not dist:
            return None
        import bisect
        return round(bisect.bisect_left(dist, dev_abs) / len(dist) * 100)

    stocks = {}
    name_of = {}
    for d in (load("ai_picks.json").get("picks") or [],
              load("ai_picks.json").get("avoid") or [],
              *((load("today_picks.json").get("picks") or {}).get(g) or []
                for g in ("signals", "values", "limit_up", "avoid")),
              load("ban_pick.json").get("picks") or []):
        for p in d:
            c = str(p.get("code") or "")
            bare_c = c[2:] if c[:2] in ("sh", "sz", "bj") else c
            if p.get("name"):
                name_of[bare_c] = p.get("name")
    for code in codes:
        bars = klines.get(prefixed(code))
        if not bars:
            continue
        lv = compute_one(bars)
        if not lv:
            continue
        lv["name"] = name_of.get(code)
        if lv["dev10_pct"] is not None:
            lv["dev10_rank"] = rank(abs(lv["dev10_pct"]), all_d10)
        if lv["dev30_pct"] is not None:
            lv["dev30_rank"] = rank(abs(lv["dev30_pct"]), all_d30)
        stocks[code] = lv

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "engine": "key_levels (indicators_ext 复用, 零新指标代码)",
        "n": len(stocks),
        "breadth": breadth,
        "fields": {"pivot": "经典Floor枢轴(昨收HLC)",
                   "keltner": "短(10,1.5)/中(20,2)/长(30,2.5)三档",
                   "atr_channel": "收盘±1×ATR(14)",
                   "dev10_rank": "10日MA偏离|值|的全市场百分位(越大越极端)",
                   "breadth": "60日新高/新低广度 (高低比>1 偏多)"},
        "stocks": stocks,
    }
    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
        return 0
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"[key_levels] -> {OUT} ({len(stocks)} 只)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
