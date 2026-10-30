#!/usr/bin/env python3
"""orderflow_proxy.py — 订单流的日线近似层（书 #9《订单流交易》）

## 先说清楚：这不是订单流

真正的订单流看的是**逐笔成交的买卖主动方向**（Delta、CVD、吸收、失衡、成交量分布 POC）。
本项目**没有 tick、没有 L2、没有分钟数据**（`intraday_events` 表 0 行），
所以下面这些标签全是**日线近似**，只能表达「当日多空推进的方向与力度」，
不能表达「哪一档被吃穿」「谁在吸收」。`known_limits()` 把缺口如实列出来。

## 近似怎么做的（每一条都写清代理量）

- `clv`（close location value）= (close - low) / (high - low) ∈ [-1, 1]
  收盘越靠上 → 当日买方掌控越强。这是 OHLC 里唯一能反映「收盘时刻谁占优」的量。
- `delta_proxy` = clv × volume：把收盘位置按成交量加权，作为「净买入压力」代理。
  与真 Delta 的差别：真 Delta 区分主动买/主动卖，这里只能用收盘位置猜。
- `delta_norm` = 近 5 日 Σdelta_proxy / (5 日均量)：归一化到 ≈[-1,1]，跨股可比。
- 吸收 absorb：放量（vol_ratio ≥ 2）但价格几乎不动（|chg| ≤ 1%）且收盘位置中性
  （|clv| ≤ 0.3）→ 大量成交被吃掉却没推进，书里叫 effort without result。
- 失衡 imbalance：连续 3 根 clv 同向且幅度大（> 0.6），配 delta_norm 同号。

## 与已有层的关系

- `structure_engine.volume_price()` 已经有量价四象限与地量天价
- `wyckoff_flow_engine` 已经有 effort/result 背离事件
本层不重复这两者，只补**收盘位置加权的方向代理**与**连续失衡序列**，
标签前缀 `flow:` 与它们区分。

## 用法
    python3 scripts/orderflow_proxy.py --code sz000701 -v
    python3 scripts/orderflow_proxy.py --validate --sample 200
    python3 scripts/orderflow_proxy.py --calibrate --sample 200
    python3 scripts/orderflow_proxy.py --all
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kline_source import list_codes  # noqa: E402
from limit_structure import load_raw  # noqa: E402
import signal_validator as SV  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or os.path.join(os.path.dirname(HERE), "data")
CST = timezone(timedelta(hours=8))

VOL_MA_N = 20
DELTA_WIN = 5
ABSORB_VOL = 2.0
ABSORB_CHG = 1.0
ABSORB_CLV = 0.3
IMB_CLV = 0.6
IMB_RUN = 3
MIN_BARS = 80

# 理论上偏多
BULLISH_TAGS = (
    "flow:imbalance_up",
    "flow:absorb_down",
    "flow:delta_up",
)


def known_limits():
    return {
        "true_delta": "需逐笔主动买卖方向，本项目无 tick",
        "cvd": "需逐笔累积成交量差",
        "poc_value_area": "需成交量分布（按价格档位）",
        "absorption_identity": "只能看出「量被吃掉」，看不出是主力还是散户",
        "iceberg_stop_run": "需 L2 挂单撤单序列",
    }


def clv(b):
    rng = b["high"] - b["low"]
    if rng <= 0:
        return 0.0
    return (b["close"] - b["low"]) / rng * 2.0 - 1.0


def vol_ma_at(cum, i, n=VOL_MA_N):
    if i + 1 < n:
        return None
    return (cum[i + 1] - cum[i + 1 - n]) / n


def analyze(bars, code=""):
    if not bars or len(bars) < MIN_BARS:
        return None
    n = len(bars)
    cum = [0.0]
    for b in bars:
        cum.append(cum[-1] + b["volume"])

    clvs = [clv(b) for b in bars]
    delta5 = []
    for i in range(n):
        if i < DELTA_WIN:
            delta5.append(None)
            continue
        acc = 0.0
        for j in range(i - DELTA_WIN + 1, i + 1):
            acc += clvs[j] * bars[j]["volume"]
        vma = vol_ma_at(cum, i)
        delta5.append(acc / (vma * DELTA_WIN) if vma else None)

    last = bars[-1]
    vma = vol_ma_at(cum, n - 1)
    vr = last["volume"] / vma if vma else None
    prev_close = bars[-2]["close"] if n > 1 else None
    chg = ((last["close"] / prev_close - 1.0) * 100.0) if prev_close else 0.0

    tags = []
    dnorm = delta5[-1]
    if dnorm is not None:
        if dnorm >= 0.3:
            tags.append("flow:delta_up")
        elif dnorm <= -0.3:
            tags.append("flow:delta_down")

    # 吸收：放量却推不动
    if vr is not None and vr >= ABSORB_VOL and abs(chg) <= ABSORB_CHG:
        if abs(clvs[-1]) <= ABSORB_CLV:
            if chg <= 0:
                tags.append("flow:absorb_down")   # 放量下跌但收在中部 → 有人接
            else:
                tags.append("flow:absorb_up")     # 放量上涨但收在中部 → 有人出

    # 连续失衡
    run_up = all(clvs[i] > IMB_CLV for i in range(n - IMB_RUN, n))
    run_dn = all(clvs[i] < -IMB_CLV for i in range(n - IMB_RUN, n))
    if run_up:
        tags.append("flow:imbalance_up")
    if run_dn:
        tags.append("flow:imbalance_down")

    # 量价背离（用现有量的口径，仅在本层标注，不重复 wyckoff 的事件）
    if vr is not None and vr >= 1.5 and abs(chg) < 0.5:
        tags.append("flow:volume_no_progress")

    return {
        "code": code,
        "date": last["date"],
        "close": round(last["close"], 3),
        "tags": list(dict.fromkeys(tags)),
        "detail": {
            "clv": round(clvs[-1], 3),
            "delta_norm_5d": round(dnorm, 3) if dnorm is not None else None,
            "vol_ratio": round(vr, 2) if vr else None,
            "chg_pct": round(chg, 2),
            "delta_series_5d": [round(x, 3) if x is not None else None for x in delta5[-5:]],
        },
        "known_limits": known_limits(),
        "note": "delta 由 close location × volume 代理，不是逐笔主动买卖；不可用于归因",
    }


def run_all(codes=None, out_path=None):
    codes = codes or list_codes()
    items, skipped = [], 0
    for code in codes:
        try:
            r = analyze(load_raw(code), code)
        except Exception as e:
            skipped += 1
            print(f"skip {code}: {str(e)[:60]}", file=sys.stderr)
            continue
        if r:
            items.append(r)
        else:
            skipped += 1
    doc = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "orderflow_proxy/v1",
        "book": "#9 订单流交易（日线近似，非真订单流）",
        "count": len(items),
        "known_limits": known_limits(),
        "items": items,
    }
    out_path = out_path or os.path.join(DATA_DIR, "orderflow_signals.json")
    tmp = out_path + ".tmp"
    json.dump(doc, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return doc, skipped


def _tag_fn(view):
    r = analyze(view, view[-1].get("code", "") if view else "")
    return r["tags"] if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args()

    if a.code:
        r = analyze(load_raw(a.code), a.code)
        if not r:
            print("数据不足，无法分析")
            return
        if a.verbose:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print(f"{r['code']} {r['date']} close={r['close']} tags={r['tags']}")
        return

    if a.validate or a.calibrate:
        codes = list_codes()[: a.sample]
        samples = []
        for c in codes:
            b = load_raw(c, limit=400)
            if len(b) >= 120:
                for x in b:
                    x["code"] = c
                samples.append((c, b))
        rows = SV.validate(_tag_fn, samples, horizon=5, min_history=MIN_BARS, cut_step=2)
        if a.validate:
            SV.print_rows(rows, 30)
        if a.calibrate:
            table = SV.calibrate(rows, BULLISH_TAGS, min_n=100, min_t=1.5)
            p = os.path.join(DATA_DIR, "orderflow_calibration.json")
            SV.write_calibration(p, table, meta={"sample": a.sample,
                                                 "engine": "orderflow_proxy/v1"}, horizon=5)
            print(f"校准表 → {p}（{len(table)} 个标签）")
            for k, v in sorted(table.items(), key=lambda kv: -abs(kv[1]["t"]))[:10]:
                print(f"  {k:30} n={v['n']:>5} fwd={v['avg_fwd']:>6.2f}% t={v['t']:>5.2f} {v['edge']}")
        return

    if a.all:
        doc, skipped = run_all()
        print(f"已写出 {doc['count']} 只（跳过 {skipped}）→ data/orderflow_signals.json")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
