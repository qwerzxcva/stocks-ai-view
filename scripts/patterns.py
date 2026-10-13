#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/patterns.py ═══
# 上游 commit: a2f0bbbb261e · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 适配: 2026-10-13 路径规范 (补 __main__ 冒烟入口, 原为纯库无 main 导致直接运行静默退出)
"""
四大形态策略硬规则移植 (myhhub/stock core/strategy/, 规则硬编码)
统一签名: fn(df, date) -> bool, df 为单票日K(date,open,high,low,close,volume)升序。
1. parking_apron        停机坪: 近15日有>9.5%放量上涨, 次日高开且收涨、收盘距开盘<3%
2. high_tight_flag      高而窄的旗形: 收盘/近24~10日最低>=1.9 且近两日连续涨幅>=9.5%
3. backtrace_ma250      回踩年线: 年线下→上突破, 缩量回踩(高点量/低点量>2, 低点价/高点价<0.8, 回撤<20%)
4. low_backtrace_increase 无大幅回撤: 60日涨幅>0.6 且无单日跌7%(允许一次洗盘)

冒烟: python3 patterns.py [--bulk DIR] [--code sz000610] [--out data/patterns_smoke.json]
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def _upto(df: pd.DataFrame, date) -> pd.DataFrame:
    idx = df.index[df["date"] == date]
    if len(idx) == 0:
        raise ValueError(f"date {date} not in df")
    return df.loc[: idx[0]].reset_index(drop=True)


def parking_apron(df: pd.DataFrame, date) -> bool:
    """停机坪: 近15日出现>9.5%放量上涨(量>5日均量2倍)的次日, 高开收涨且收盘距开盘<3% — 当日即符合。"""
    d = _upto(df, date)
    if len(d) < 16:
        return False
    today = d.iloc[-1]
    if not (today.open > d.iloc[-2].close and today.close > today.open
            and (today.close / today.open - 1) < 0.03):
        return False
    vol5 = d["volume"].shift(1).rolling(5).mean()
    big = d["close"].pct_change() > 0.095
    heavy = d["volume"] > 2 * vol5
    # 大涨日与今日为相邻两日(大涨日是昨日)
    return bool(big.iloc[-2] and heavy.iloc[-2])


def high_tight_flag(df: pd.DataFrame, date) -> bool:
    """高而窄的旗形: close / min(low[近10日..24日前]) >= 1.9 且最近两日涨幅均>=9.5%。"""
    d = _upto(df, date)
    if len(d) < 25:
        return False
    low24_10 = d["low"].iloc[-24:-9].min()
    if low24_10 <= 0 or d["close"].iloc[-1] / low24_10 < 1.9:
        return False
    r = d["close"].pct_change()
    return bool(r.iloc[-1] >= 0.095 and r.iloc[-2] >= 0.095)


def backtrace_ma250(df: pd.DataFrame, date) -> bool:
    """回踩年线: 突破年线后回踩, 低点价/突破后高点价<0.8且回撤<20%, 高点量/低点量>2。"""
    d = _upto(df, date)
    if len(d) < 260:
        return False
    ma250 = d["close"].rolling(250).mean()
    above = (d["close"] > ma250).values
    # 找最近一次自下而上的突破点
    brk = None
    for i in range(len(d) - 1, 0, -1):
        if above[i] and not above[i - 1]:
            brk = i
            break
    if brk is None or brk >= len(d) - 10:
        return False
    seg = d.iloc[brk:]
    hi_i = seg["high"].idxmax()
    hi_price = seg["high"].max()
    # 突破后须有一段在年线上运行
    if not above[hi_i]:
        return False
    low_seg = seg.loc[hi_i:]
    low_i = low_seg["low"].idxmin()
    low_price = low_seg["low"].min()
    if not (10 <= low_i - hi_i <= 50):
        return False
    # 缩量回踩: 低点价/高点价<0.8, 回撤<20%, 高点量/低点量>2
    if low_price / hi_price >= 0.8:
        return False
    drawdown = 1 - low_price / hi_price
    if drawdown >= 0.20:
        return False
    return bool(d["volume"].iloc[hi_i] / max(d["volume"].iloc[low_i], 1) > 2)


def low_backtrace_increase(df: pd.DataFrame, date) -> bool:
    """无大幅回撤: 60日涨幅>0.6 且期间无单日跌>7%(高开低走跌7%也算, 允许一次洗盘=两次违规内不算?). 硬规则: 无单日跌7%。"""
    d = _upto(df, date)
    if len(d) < 61:
        return False
    w = d.iloc[-60:]
    if w["close"].iloc[-1] / w["close"].iloc[0] - 1 <= 0.6:
        return False
    day_ret = w["close"].pct_change()
    if (day_ret < -0.07).any():
        return False
    # 高开低走: open>昨日close 且 close/open-1 < -7%
    hl = (w["open"] > w["close"].shift(1)) & (w["close"] / w["open"] - 1 < -0.07)
    return not bool(hl.any())


STRATEGIES = [
    ("parking_apron", "停机坪", parking_apron),
    ("high_tight_flag", "高而窄的旗形", high_tight_flag),
    ("backtrace_ma250", "回踩年线", backtrace_ma250),
    ("low_backtrace_increase", "无大幅回撤", low_backtrace_increase),
]


def load_one(bulk_dir: str, code: str) -> pd.DataFrame:
    """从 bulk day_qfq_*.csv 读单票日K (分隔符 '|' 或 ',' 自动识别)。"""
    import glob
    rows = []
    for p in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|") if "|" in line else line.split(",")
                if len(parts) < 6 or parts[0] != code:
                    continue
                try:
                    rows.append((parts[1], float(parts[2]), float(parts[3]),
                                 float(parts[4]), float(parts[5]), float(parts[6])))
                except ValueError:
                    continue
        if rows:
            break
    if not rows:
        raise ValueError(f"bulk 中找不到 {code}")
    rows.sort(key=lambda x: x[0])
    return pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="四大形态策略冒烟")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--code", default="sz000610", help="测试股票代码")
    ap.add_argument("--out", default=None, help="可选: 落盘 JSON")
    args = ap.parse_args()

    bulk = args.bulk or BULK
    df = load_one(bulk, args.code)
    d = df.date.iloc[-1]
    print(f"{args.code}: {len(df)} 根 ({df.date.iloc[0]} ~ {d})")
    res = {}
    for name, label, fn in STRATEGIES:
        try:
            res[name] = bool(fn(df, d))
            print(f"  {name:24s} {label:8s} -> {res[name]}")
        except Exception as e:
            res[name] = None
            print(f"  {name:24s} {label:8s} -> ERR {type(e).__name__}: {e}")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                       "code": args.code, "date": d, "bars": len(df), "results": res},
                      f, ensure_ascii=False, indent=1)
        print("->", args.out)
