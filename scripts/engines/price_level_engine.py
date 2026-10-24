#!/usr/bin/env python3
"""
price_level_engine.py — 价位引擎 v1 (指数+个股通用, 10 类关键价位)

【用途】对给定标的（个股或指数）基于真实日线 K 线计算 10 类关键价位：
  1. support        支撑位: 近 250 日分形低点聚类(±1.5%带宽) + 量价剖面成交密集区 POC
  2. resistance     阻力位: 同上, 分形高点聚类 + 上方 POC
  3. pressure       压力位: 现价上方最近密集区下沿/POC
  4. suppressed     承压位: 历史上价格上穿失败≥2 次的价位
  5. rebound        反弹位: 最近一波段黄金分割 0.382/0.5/0.618 + MA20/60 确认
  6. pullback       回调位: 同上(上升波段视角)
  7. pivot          枢轴买卖位: 经典 Pivot P=(H+L+C)/3, R1/S1/R2/S2 (上一交易日)
  8. round_number   整数关口位: 个股 10 倍数近端 / 指数 100 点网格
  9. chip           筹码位: 【降级】真实筹码分布数据缺失 → 成交量加权价格分布近似
                    (换手率衰减模型, 近期成交权重高), confidence=low, degraded=true
 10. fund           资金位: 【降级】资金流历史序列缺失 → 放量日(量>2×MA20) VWAP 链
                    degraded=true, 数据源适配层预留

【回测检验】--validate 模式: 对标的做滚动检验「价格触支撑位后 T+N 日方向命中率」
  vs 同期随机基准(随机抽 N 日窗口的上涨占比), 输出 n、胜率、Wilson 95% CI。

【数据来源】
  个股: ~/workspace/stocks-data-kline/data/bulk/day_qfq_{shard}.csv (前复权日线, 至 2026-10-16)
        注: 个别新补录标的在分片里只有最近若干根, 引擎自动合并
        day_qfq_expand.csv (长历史增量), 去重排序后使用
  指数: ~/workspace/stocks-data-kline/data/index/history/{code}.txt (至 2026-10-09)
  格式: code|date|open|close|high|low|vol (首行为 # 注释)

【降级说明】筹码分布与资金流历史为已知数据缺口, 对应价位标注 degraded=true /
  confidence=low, 后续接入真实数据源时仅需替换 chip/fund 段实现。

【新增声明】本文件为新增引擎 (2026-10-23), 不修改任何既有文件;
  与 tomorrow_candidate_engine / morning_pick_engine 零代码共享, 仅共享数据层只读读取。
  无外部 token/凭据, 不打印任何敏感信息。只用 T 日及之前数据, 无前视。

CLI:
  python3 price_level_engine.py --code sh600000            # 个股
  python3 price_level_engine.py --index sh000001           # 指数
  python3 price_level_engine.py --code sh600000 --validate # 触位命中率回测
  通用: --asof YYYY-MM-DD  --lookback 250  --out out.json (默认 stdout)
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "price_level_engine.json"))
KLINE_BASE = os.environ.get(
    "STOCKS_DATA_KLINE",
    os.path.expanduser("~/workspace/stocks-data-kline/data"))
BULK_DIR = os.path.join(KLINE_BASE, "bulk")
INDEX_DIR = os.path.join(KLINE_BASE, "index", "history")

ENGINE = "price_level_engine v1"


# ─── 数据层 (只读) ──────────────────────────────────────────────
def _norm_code(code):
    """裸码与 sh/sz 前缀码双向归一化, 返回 (bare, full)。"""
    c = str(code).strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c[2:], c
    bare = c.zfill(6)
    prefix = "sh" if bare.startswith(("60", "68", "9")) else "sz"
    return bare, prefix + bare


def _shard_for(bare):
    return f"day_qfq_{bare[:2]}.csv"


def _parse_line(line):
    parts = line.rstrip("\n").split("|")
    if len(parts) != 7:
        return None
    try:
        return {
            "date": parts[1],
            "open": float(parts[2]),
            "close": float(parts[3]),
            "high": float(parts[4]),
            "low": float(parts[5]),
            "vol": float(parts[6]),
        }
    except ValueError:
        return None


def load_bars(code=None, index=None):
    """读取单个标的日线, 按日期升序去重。返回 (bars, full_code, source_file)。

    个股: 主分片不足 MIN_BARS 时自动合并 day_qfq_expand.csv (长历史增量)。
    """
    if index:
        full = index.lower()
        path = os.path.join(INDEX_DIR, f"{full}.txt")
    else:
        bare, full = _norm_code(code)
        path = os.path.join(BULK_DIR, _shard_for(bare))
    if not os.path.exists(path):
        return None, full, None
    bars = []
    target = None if index else full
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            if target and not line.startswith(target + "|"):
                continue
            rec = _parse_line(line)
            if rec:
                bars.append(rec)
    if not index and len(bars) < 60:
        # 主分片覆盖不足 → 合并 expand 增量长历史
        exp = os.path.join(BULK_DIR, "day_qfq_expand.csv")
        if os.path.exists(exp):
            with open(exp, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.startswith(full + "|"):
                        continue
                    rec = _parse_line(line)
                    if rec:
                        bars.append(rec)
    seen, dedup = set(), []
    for r in sorted(bars, key=lambda r: r["date"]):
        if r["date"] not in seen:
            seen.add(r["date"])
            dedup.append(r)
    return dedup, full, path


def clip_asof(bars, asof):
    """只用 T(asof) 日及之前数据, 禁止前视。"""
    if not asof:
        return bars
    return [b for b in bars if b["date"] <= asof]


# ─── 指标工具 (纯标准库实现) ────────────────────────────────────
def sma(seq, n):
    if len(seq) < n:
        return None
    return sum(seq[-n:]) / n


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


# ─── 分形与聚类 ─────────────────────────────────────────────────
def fractals(bars, wing=2):
    """分形高/低点: 前后 wing 根内的局部极值。返回 (highs, lows), 元素 (idx, price)。"""
    highs, lows = [], []
    n = len(bars)
    for i in range(wing, n - wing):
        h, l = bars[i]["high"], bars[i]["low"]
        if all(bars[j]["high"] <= h for j in range(i - wing, i + wing + 1) if j != i):
            highs.append((i, h))
        if all(bars[j]["low"] >= l for j in range(i - wing, i + wing + 1) if j != i):
            lows.append((i, l))
    return highs, lows


def cluster(points, band_pct=0.015):
    """价位聚类: ±band_pct 带宽合并, 按触碰次数降序。返回 [{price, touches, ...}]。"""
    if not points:
        return []
    pts = sorted(points, key=lambda x: x[1])
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        base = sum(x[1] for x in cur) / len(cur)
        if abs(p[1] - base) / base <= band_pct:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)
    out = []
    for c in clusters:
        price = sum(x[1] for x in c) / len(c)
        out.append({"price": round(price, 3), "touches": len(c),
                    "last_touch_idx": max(x[0] for x in c)})
    out.sort(key=lambda d: (-d["touches"], d["price"]))
    return out


def volume_profile(bars, bins=40):
    """量价剖面: 价格分 bin, 每根 K 线量按其典型价归入, 返回 POC 与密集区列表。"""
    if not bars:
        return None, []
    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    if hi <= lo:
        return None, []
    width = (hi - lo) / bins
    vol_bins = [0.0] * bins
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        i = min(int((tp - lo) / width), bins - 1)
        vol_bins[i] += b["vol"]
    total = sum(vol_bins) or 1.0
    poc_i = max(range(bins), key=lambda i: vol_bins[i])
    poc = lo + (poc_i + 0.5) * width
    zones = []
    for i, v in enumerate(vol_bins):
        if v / total >= 0.03:  # 成交占比 ≥3% 视为密集区
            zones.append({"low": round(lo + i * width, 3),
                          "high": round(lo + (i + 1) * width, 3),
                          "poc": round(lo + (i + 0.5) * width, 3),
                          "vol_share": round(v / total, 4)})
    zones.sort(key=lambda z: -z["vol_share"])
    return round(poc, 3), zones


# ─── 10 类价位 ──────────────────────────────────────────────────
def compute_levels(bars, is_index=False, lookback=250):
    bars = bars[-lookback:] if len(bars) > lookback else bars
    if len(bars) < 30:
        return None
    cur = bars[-1]["close"]
    closes = [b["close"] for b in bars]
    fh, fl = fractals(bars)
    hi_clusters = cluster(fh)
    lo_clusters = cluster(fl)
    poc, zones = volume_profile(bars)

    above_h = [c for c in hi_clusters if c["price"] > cur]
    below_l = [c for c in lo_clusters if c["price"] < cur]
    support = sorted(below_l, key=lambda d: -d["price"])[:3]
    resistance = sorted(above_h, key=lambda d: d["price"])[:3]
    # 兜底: 现价处于窗口极值区时无历史分形可参考, 退回窗口最低/最高价
    if not support:
        wl = min(b["low"] for b in bars)
        support = [{"price": round(wl, 3), "touches": 1, "src": "window_low_fallback"}]
    if not resistance:
        wh = max(b["high"] for b in bars)
        resistance = [{"price": round(wh, 3), "touches": 1, "src": "window_high_fallback"}]
    # POC 补强: 下方 POC 视为支撑、上方 POC 视为阻力
    if poc:
        if poc < cur and all(abs(poc - s["price"]) / s["price"] > 0.005 for s in support):
            support.append({"price": poc, "touches": "poc", "src": "volume_profile"})
        elif poc > cur and all(abs(poc - r["price"]) / r["price"] > 0.005 for r in resistance):
            resistance.append({"price": poc, "touches": "poc", "src": "volume_profile"})

    # 压力位: 现价上方最近密集区下沿/POC
    up_zones = [z for z in zones if z["low"] > cur]
    up_zones.sort(key=lambda z: z["low"])
    pressure = ([{"price": z["low"], "poc": z["poc"], "vol_share": z["vol_share"]}
                 for z in up_zones[:2]] if up_zones else None)

    # 承压位: 价格上穿失败≥2 次
    suppressed = []
    for c in hi_clusters:
        fails = 0
        for i in range(len(bars) - 1):
            b = bars[i]
            if b["low"] < c["price"] < b["high"] and bars[i + 1]["close"] < c["price"]:
                fails += 1
        if fails >= 2 and c["price"] > cur * 0.98:
            suppressed.append({"price": c["price"], "failed_breaks": fails})
    suppressed.sort(key=lambda d: -d["failed_breaks"])

    # 反弹/回调位: 最近一波段黄金分割 + MA 确认
    n = len(bars)
    swing_hi_i = max(range(n), key=lambda i: bars[i]["high"])
    swing_lo_i = min(range(n), key=lambda i: bars[i]["low"])
    ma20, ma60 = sma(closes, 20), sma(closes, 60)
    fib = None
    if swing_hi_i != swing_lo_i:
        hi, lo = bars[swing_hi_i]["high"], bars[swing_lo_i]["low"]
        span = hi - lo
        if swing_hi_i > swing_lo_i:  # 上升波段 → 回调位
            fib = {
                "wave": "up", "wave_hi": hi, "wave_lo": lo,
                "pullback_382": round(hi - 0.382 * span, 3),
                "pullback_500": round(hi - 0.500 * span, 3),
                "pullback_618": round(hi - 0.618 * span, 3),
                "ma20": round(ma20, 3) if ma20 else None,
                "ma60": round(ma60, 3) if ma60 else None,
            }
        else:  # 下降波段 → 反弹位
            fib = {
                "wave": "down", "wave_hi": hi, "wave_lo": lo,
                "rebound_382": round(lo + 0.382 * span, 3),
                "rebound_500": round(lo + 0.500 * span, 3),
                "rebound_618": round(lo + 0.618 * span, 3),
                "ma20": round(ma20, 3) if ma20 else None,
                "ma60": round(ma60, 3) if ma60 else None,
            }

    # 枢轴买卖位 (基于上一交易日 H/L/C)
    b = bars[-1]
    p = (b["high"] + b["low"] + b["close"]) / 3
    pivot = {
        "P": round(p, 3), "R1": round(2 * p - b["low"], 3),
        "S1": round(2 * p - b["high"], 3),
        "R2": round(p + (b["high"] - b["low"]), 3),
        "S2": round(p - (b["high"] - b["low"]), 3),
        "based_on": b["date"],
    }

    # 整数关口位
    grid = 100 if is_index else 10
    base = math.floor(cur / grid) * grid
    round_numbers = {"grid": grid, "below": float(base), "above": float(base + grid)}

    # 筹码位【降级】: 换手率衰减的成交量加权价格分布
    decay = 0.97
    hi_all = max(x["high"] for x in bars)
    lo_all = min(x["low"] for x in bars)
    nbin = 30
    width = (hi_all - lo_all) / nbin if hi_all > lo_all else 1e-9
    vol_bins = {}
    for i, x in enumerate(bars):
        w = decay ** (len(bars) - 1 - i)
        tp = (x["high"] + x["low"] + x["close"]) / 3
        k = min(int((tp - lo_all) / width), nbin - 1)
        vol_bins[k] = vol_bins.get(k, 0.0) + x["vol"] * w
    tot = sum(vol_bins.values()) or 1.0
    chip_sorted = sorted(vol_bins.items(), key=lambda kv: -kv[1])
    chip_levels = [{"price": round(lo_all + (k + 0.5) * width, 3),
                    "weight": round(v / tot, 4)} for k, v in chip_sorted[:3]]
    chip = {
        "levels": chip_levels,
        "degraded": True, "confidence": "low",
        "note": "真实筹码分布数据缺失(已知缺口), 降级为换手率衰减成交量加权价格分布近似",
    }

    # 资金位【降级】: 放量日 VWAP 链
    v20 = [b2["vol"] for b2 in bars[-21:-1]] or [1.0]
    v20_avg = sum(v20) / len(v20) or 1.0
    fund_chain = []
    for x in bars[-60:]:
        if x["vol"] >= 2 * v20_avg and x["high"] > x["low"]:
            vwap = (x["high"] + x["low"] + x["close"]) / 3  # 日线近似 VWAP
            fund_chain.append({"date": x["date"], "vwap": round(vwap, 3),
                               "vol_ratio": round(x["vol"] / v20_avg, 2)})
    fund = {
        "chain": fund_chain[-5:],
        "degraded": True,
        "note": "资金流历史序列缺失(已知缺口), 降级为放量日(量≥2×MA20) VWAP 链",
    }

    # 抄底位: 支撑位 −1×ATR(14) 且 RSI<30 共振
    a14 = atr(bars, 14)
    r14 = rsi(closes, 14)
    bottom = None
    if a14 and support:
        s = support[0]["price"]
        bottom = {
            "price": round(s - a14, 3),
            "support": s, "atr14": round(a14, 3),
            "rsi14": round(r14, 2) if r14 is not None else None,
            "rsi_oversold": bool(r14 is not None and r14 < 30),
            "note": "支撑位−1×ATR(14); RSI<30 时为共振强信号",
        }

    return {
        "current": round(cur, 3),
        "support": support, "resistance": resistance,
        "pressure": pressure, "suppressed": suppressed[:3],
        "swing_fib": fib, "pivot": pivot,
        "round_number": round_numbers,
        "chip": chip, "fund": fund, "bottom_fishing": bottom,
    }


# ─── 回测检验 ───────────────────────────────────────────────────
def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(center - half, 4), round(center + half, 4))


def validate(bars, horizon=5, lookback=250):
    """滚动检验: 历史每个时点用当时可见数据算支撑位, 次日触及后 T+N 是否收涨。

    基准: 全部 T+N 窗口无条件上涨占比。无前视: 时点 t 的支撑位只用 ≤t 数据,
    判定用 t+1 (触位) 与 t+1+horizon (结果)。
    """
    n = len(bars)
    if n < 120:
        return {"error": "样本不足(需≥120根)", "touch_events": 0}
    events, up = 0, 0
    base_up, base_n = 0, 0
    for t in range(60, n - horizon - 1):
        window = bars[max(0, t - lookback):t + 1]
        _, fl = fractals(window)
        lo_clusters = [c for c in cluster(fl) if c["price"] < window[-1]["close"]]
        # 无条件基准
        base_n += 1
        if bars[t + horizon]["close"] > bars[t]["close"]:
            base_up += 1
        if not lo_clusters:
            continue
        sup = max(c["price"] for c in lo_clusters)
        a = atr(window, 14)
        tol = (a or window[-1]["close"] * 0.01) * 0.5
        nxt = bars[t + 1]
        if nxt["low"] <= sup + tol and nxt["high"] >= sup - tol:  # 次日触位
            events += 1
            if bars[t + 1 + horizon]["close"] > nxt["close"]:
                up += 1
    win = up / events if events else None
    base = base_up / base_n if base_n else None
    lo, hi = wilson_ci(up, events)
    return {
        "horizon_days": horizon,
        "touch_events": events,
        "wins": up,
        "win_rate": round(win, 4) if win is not None else None,
        "wilson95_ci": [lo, hi],
        "random_baseline_win_rate": round(base, 4) if base is not None else None,
        "edge_vs_baseline": round(win - base, 4) if (win is not None and base is not None) else None,
    }


def main():
    ap = argparse.ArgumentParser(description=ENGINE + " — 10 类关键价位计算与回测")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", help="个股代码, 裸码 600000 或 sh600000")
    g.add_argument("--index", help="指数代码, 如 sh000001")
    ap.add_argument("--asof", help="截止日期 YYYY-MM-DD, 只用该日及之前数据")
    ap.add_argument("--lookback", type=int, default=250, help="回看天数(默认 250)")
    ap.add_argument("--validate", action="store_true", help="触位命中率回测 vs 随机基准")
    ap.add_argument("--horizon", type=int, default=5, help="回测持有天数(默认 5)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 路径(默认 DEFAULT_OUT) ('--out -' 输出 stdout)")
    args = ap.parse_args()

    bars, full, path = load_bars(code=args.code, index=args.index)
    if not bars:
        print(json.dumps({"engine": ENGINE, "error": f"无数据: {full}"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    bars = clip_asof(bars, args.asof)
    if not bars:
        print(json.dumps({"engine": ENGINE, "error": "asof 之前无数据"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    result = {
        "engine": ENGINE,
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "code": full,
        "is_index": bool(args.index),
        "data_asof": bars[-1]["date"],
        "source": os.path.relpath(path, os.path.expanduser("~")) if path else None,
        "bars_used": len(bars[-args.lookback:]),
        "levels": compute_levels(bars, is_index=bool(args.index), lookback=args.lookback),
    }
    if args.validate:
        result["validate"] = validate(bars, horizon=args.horizon,
                                      lookback=args.lookback)

    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"written: {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
