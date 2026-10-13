#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/cyq.py ═══
# 上游 commit: f7c7162a30da · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 适配: 2026-10-13 路径规范 (补 __main__ 冒烟入口, 原为纯库无 main 导致直接运行静默退出)
"""
筹码分布 CYQ 纯Python实现 (移植 myhhub/stock core/kline/cyq.py 思路)
算法: 逐日将当日成交量按三角分布摊到价格档(顶点=均价, 向high/low线性衰减),
     历史筹码按当日换手率衰减(1 - A*turnover), 累加得当前筹码分布。
输出指标: 获利盘比例(profit_ratio) / 90%筹码集中度(conc90) / 成本乖离(cost_bias)。

冒烟: python3 cyq.py [--bulk DIR] [--code sz000610] [--out data/cyq_smoke.json]
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

ACCURACY = 150      # 价格档数
RANGE_DAYS = 120    # 参与衰减计算的回看天数
CYQ_DAYS = 210      # 至少需要的K线数


def cyq_distribution(df: pd.DataFrame, date=None) -> dict:
    """df: 单票日K(date,open,high,low,close,volume) 按日期升序。
    返回指定date的筹码分布及指标; date=None用最后一行。"""
    df = df.reset_index(drop=True)
    if date is not None:
        idx = df.index[df["date"] == date]
        if len(idx) == 0:
            raise ValueError(f"date {date} not in df")
        df = df.loc[: idx[0]]
    if len(df) < 2:
        raise ValueError("need >=2 bars")
    d = df.tail(max(RANGE_DAYS, 2)).reset_index(drop=True)
    low_all = float(d["low"].min()); high_all = float(d["high"].max())
    if high_all <= low_all:
        raise ValueError("bad price range")
    step = (high_all - low_all) / ACCURACY
    grid = low_all + (np.arange(ACCURACY) + 0.5) * step
    chips = np.zeros(ACCURACY)
    for _, row in d.iterrows():
        o, h, l, c, v = float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume)
        avg = (o + h + l + c) / 4.0
        if h <= l:
            k = min(ACCURACY - 1, max(0, int((avg - low_all) / step)))
            day = np.zeros(ACCURACY); day[k] = 1.0
        else:
            day = np.zeros(ACCURACY)
            for j in range(ACCURACY):
                p = grid[j]
                if l <= p <= avg:
                    day[j] = (p - l) / (avg - l)
                elif avg < p <= h:
                    day[j] = (h - p) / (h - avg)
            s = day.sum()
            if s > 0:
                day /= s
        turnover = v / max(d["volume"].mean(), 1.0)
        decay = min(1.0, max(0.0, 1.0 - turnover))  # 保留比例
        chips = chips * decay + day * v
    total = chips.sum()
    if total <= 0:
        raise ValueError("empty chips")
    chips /= total
    close = float(d["close"].iloc[-1])
    profit = float(np.clip(chips[grid < close].sum(), 0.0, 1.0))  # 获利盘: 成本低于现价
    cum = np.cumsum(chips)
    lo = grid[np.searchsorted(cum, 0.05)]
    hi = grid[np.searchsorted(cum, 0.95)]
    cost = float((grid * chips).sum())
    return {
        "date": d["date"].iloc[-1],
        "close": close,
        "profit_ratio": profit,                                  # 获利盘比例
        "conc90": float((hi - lo) / max(cost, 1e-9)),            # 90%筹码集中度(区间宽/成本)
        "cost": cost,
        "cost_bias": close / cost - 1.0,                         # 成本乖离(现价/平均成本-1)
        "chips": chips, "grid": grid,
    }


def cyq_factor_series(df: pd.DataFrame) -> pd.DataFrame:
    """按日滚动输出三列因子(每5日抽样避免O(n^2)): date, cyq_profit_ratio, cyq_conc90, cyq_cost_bias"""
    rows = []
    n = len(df)
    if n < 30:
        return pd.DataFrame()
    start = max(30, min(CYQ_DAYS, n // 2))  # 历史不足210日时降级起点
    for i in range(start, n, 5):
        try:
            r = cyq_distribution(df.iloc[: i + 1])
        except ValueError:
            continue
        rows.append({"date": r["date"], "cyq_profit_ratio": r["profit_ratio"],
                     "cyq_conc90": r["conc90"], "cyq_cost_bias": r["cost_bias"]})
    return pd.DataFrame(rows)


# ---- factor_lib 接入 ----
def register_into(factor_lib):
    """向 factor_lib 注册 cyq 因子(因子库为df->Series接口, cyq逐日抽样后前向填充对齐)。"""
    import pandas as _pd
    fl = factor_lib

    def _make(attr):
        def fn(df):
            s = cyq_factor_series(df)
            if s.empty:
                return _pd.Series(np.nan, index=df.index)
            out = df[["date"]].merge(s, on="date", how="left")[attr]
            return _pd.Series(out.values, index=df.index).ffill()
        return fn

    fl.factor("cyq_profit_ratio", "获利盘比例(CYQ)", "筹码")(_make("cyq_profit_ratio"))
    fl.factor("cyq_conc90", "90%筹码集中度(CYQ)", "筹码")(_make("cyq_conc90"))
    fl.factor("cyq_cost_bias", "成本乖离(CYQ)", "筹码")(_make("cyq_cost_bias"))


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
    ap = argparse.ArgumentParser(description="CYQ 筹码分布冒烟")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--code", default="sz000610", help="测试股票代码")
    ap.add_argument("--out", default=None, help="可选: 落盘 JSON")
    args = ap.parse_args()

    bulk = args.bulk or BULK
    df = load_one(bulk, args.code)
    r = cyq_distribution(df.tail(210))
    series = cyq_factor_series(df)
    print(f"{args.code}: {len(df)} 根, 末日 {r['date']}")
    print(f"  获利盘比例 profit_ratio = {r['profit_ratio']:.3f}")
    print(f"  90%集中度 conc90      = {r['conc90']:.3f}")
    print(f"  平均成本 cost          = {r['cost']:.2f} (现价 {r['close']:.2f})")
    print(f"  成本乖离 cost_bias     = {r['cost_bias']:+.3f}")
    print(f"  滚动因子序列           = {len(series)} 个抽样点")
    if args.out:
        payload = {"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                   "code": args.code, "bars": len(df), "date": r["date"],
                   "profit_ratio": round(r["profit_ratio"], 4),
                   "conc90": round(r["conc90"], 4),
                   "cost": round(r["cost"], 4),
                   "cost_bias": round(r["cost_bias"], 4),
                   "series_points": len(series)}
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("->", args.out)
