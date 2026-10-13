#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/factor_lib.py ═══
# 上游 commit: 175984648d3d · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 适配: 2026-10-13 路径规范 (补 __main__ 冒烟入口, 原为纯库无 main 导致直接运行静默退出)
"""
因子库 v1 (参考 tick-stock-panel factors/registry 移植, pandas版)
  每个因子: (id, 中文, 组, 计算函数df->Series)
  用于 scan_local 打分增强 + 周期规律归因

冒烟: python3 factor_lib.py [--bulk DIR] [--code sz000610] [--out data/factor_smoke.json]
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def _roll(s, n, fn):
    return getattr(s.rolling(n), fn)()


FACTORS = []

def factor(fid, label, group):
    def deco(fn):
        FACTORS.append((fid, label, group, fn))
        return fn
    return deco


@factor("momentum_5d", "5日动量", "动量")
def f(df): return df.close.pct_change(5)
@factor("momentum_10d", "10日动量", "动量")
def f(df): return df.close.pct_change(10)
@factor("momentum_20d", "20日动量", "动量")
def f(df): return df.close.pct_change(20)
@factor("momentum_60d", "60日动量", "动量")
def f(df): return df.close.pct_change(60)
@factor("ret_1d", "日涨跌幅", "动量")
def f(df): return df.close.pct_change()

@factor("ma20_bias", "MA20乖离", "均线")
def f(df): return df.close / df.close.rolling(20).mean() - 1
@factor("ma60_bias", "MA60乖离", "均线")
def f(df): return df.close / df.close.rolling(60).mean() - 1

@factor("rsi_14", "RSI14", "超买超卖")
def f(df):
    d = df.close.diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

@factor("macd_hist_pct", "MACD柱强度", "趋势")
def f(df):
    e12 = df.close.ewm(span=12).mean(); e26 = df.close.ewm(span=26).mean()
    dif = e12 - e26; dea = dif.ewm(span=9).mean()
    return (dif - dea) / df.close

@factor("vol_ratio_5d", "5日量比", "量价")
def f(df): return df.volume / df.volume.shift(1).rolling(5).mean()
@factor("vol_trend_5_10", "量能趋势", "量价")
def f(df): return df.volume.rolling(5).mean() / df.volume.rolling(10).mean() - 1
@factor("obv_trend_20d", "量能潮20日", "量价")
def f(df):
    s = np.sign(df.close.diff()).fillna(0) * df.volume
    return s.rolling(20).sum() / (df.volume.rolling(20).mean() * 20)

@factor("amplitude", "日振幅", "波动率")
def f(df): return (df.high - df.low) / df.close.shift(1)
@factor("vol_20d", "20日波动率", "波动率")
def f(df): return df.close.pct_change().rolling(20).std() * (243 ** 0.5)
@factor("downside_vol_20d", "20日下行波动", "波动率")
def f(df):
    r = df.close.pct_change()
    return np.minimum(r, 0).pow(2).rolling(20).mean() ** 0.5

@factor("close_position", "收盘位置", "价格位置")
def f(df): return (df.close - df.low) / (df.high - df.low).replace(0, np.nan)
@factor("gap_return", "开盘跳空", "价格位置")
def f(df): return df.open / df.close.shift(1) - 1
@factor("intraday_return", "日内收益", "价格位置")
def f(df): return df.close / df.open - 1
@factor("dist_high_60d", "距60日高", "价格位置")
def f(df): return df.close / df.high.rolling(60).max() - 1
@factor("position_240d", "一年位置", "价格位置")
def f(df):
    lo = df.low.rolling(240, min_periods=60).min()
    hi = df.high.rolling(240, min_periods=60).max()
    return (df.close - lo) / (hi - lo).replace(0, np.nan)

@factor("ret_skew_20d", "20日偏度", "收益形态")
def f(df): return df.close.pct_change().rolling(20).skew()
@factor("max_ret_20d", "20日最大涨幅", "收益形态")
def f(df): return df.close.pct_change().rolling(20).max()
@factor("up_days_20d", "20日上涨天数", "收益形态")
def f(df): return df.close.pct_change().gt(0).rolling(20).sum()
@factor("overnight_20d", "20日隔夜收益", "收益形态")
def f(df): return (df.open / df.close.shift(1) - 1).rolling(20).sum()
@factor("intraday_20d", "20日日内收益", "收益形态")
def f(df): return (df.close / df.open - 1).rolling(20).sum()

@factor("amihud_20d", "Amihud非流动性", "流动性")
def f(df):
    amt_yi = (df.close * df.volume) / 1e8
    return (df.close.pct_change().abs() / amt_yi.replace(0, np.nan)).rolling(20).mean()

@factor("zt_count_20d", "涨停基因20日", "涨停基因")
def f(df):
    zt = (df.close / df.close.shift(1) - 1) >= 0.095
    return zt.rolling(20).sum()
@factor("zt_count_60d", "涨停基因60日", "涨停基因")
def f(df):
    zt = (df.close / df.close.shift(1) - 1) >= 0.095
    return zt.rolling(60).sum()

@factor("vol_price_corr_20d", "量价相关20日", "量价")
def f(df):
    r = df.close.pct_change()
    return r.rolling(20).corr(df.volume.pct_change())


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """df列: date,open,close,high,low,volume (单票时序)。返回追加因子列。"""
    out = df.copy()
    for fid, label, group, fn in FACTORS:
        try:
            out[fid] = fn(out)
        except Exception:
            out[fid] = np.nan
    return out


def latest_snapshot(df: pd.DataFrame) -> dict:
    """最新一行全部因子值 {fid: value}"""
    full = compute_all(df)
    last = full.iloc[-1]
    return {fid: (None if pd.isna(last[fid]) else round(float(last[fid]), 4))
            for fid, _, _, _ in FACTORS}


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
    ap = argparse.ArgumentParser(description="因子库冒烟")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--code", default="sz000610", help="测试股票代码")
    ap.add_argument("--out", default=None, help="可选: 落盘 JSON")
    args = ap.parse_args()

    bulk = args.bulk or BULK
    df = load_one(bulk, args.code)
    snap = latest_snapshot(df)
    n_ok = sum(1 for v in snap.values() if v is not None)
    print(f"{args.code}: {len(df)} 根, 因子 {n_ok}/{len(snap)} 个非空")
    for fid, label, group, _ in FACTORS:
        print(f"  {fid:22s} {label:14s} [{group}] = {snap[fid]}")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                       "code": args.code, "bars": len(df),
                       "date": df.date.iloc[-1], "factors": snap},
                      f, ensure_ascii=False, indent=1)
        print("->", args.out)
