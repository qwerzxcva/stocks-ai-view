#!/usr/bin/env python3
"""
minute_features.py — 从分钟 parquet 聚合日内特征 (无未来函数)

输入:
  --minute  分钟 parquet 文件/目录 (含 symbol,date,hhmm,open,close,high,low,volume,amount)
  --daily   可选日线 parquet (symbol,date,close), 只做前收口径交叉校验 (basis_check), 不参与计算

输出:
  --out      特征 parquet (symbol,date + 各特征列, NaN 写成 null)
  --manifest 特征清单 JSON (每维定义/量纲/空值策略 + 覆盖统计)

只用当日及之前可得数据: 前收取自同一 symbol 在分钟数据里上一交易日的末根 close
(原始价, 不复权, 与分钟价同源), 其余全部由当日分钟 bar 派生。缺分钟数据的
(symbol,date) 不出行; 缺前收的行跳空类特征与 limitup_touch 写 null, 不做插值,
也不用日线反推分钟。

特征列 (18 维, 全部无量纲):
  open_gap / intraday_ret / intraday_range / close_position / tail_strength /
  morning_strength / am_pm_split / max_drawdown_intraday / max_rise_intraday /
  vol_share_first30 / vol_share_last30 / vwap_dev / up_min_ratio /
  big_move_min_count / ret_autocorr / realized_vol / gap_fill / limitup_touch

运行期依赖 pyarrow + pandas, 缺库直接报错退出 (退出码 2), 不静默降级。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

SESSION_FIRST = "09:31"
FIRST_BAR_MAX = "09:35"        # 覆盖判定: 首根不得晚于此 (时间优先, 不看纯根数)
LAST_BAR_MIN = "14:55"         # 覆盖判定: 末根不得早于此
AM_CUTOFF = "10:00"
TAIL_CUTOFF = "14:30"
FIRST30_BARS = 30
LAST30_BARS = 30
BIG_MOVE_PCT = 0.01          # 单分钟涨幅阈值
ZT_MAIN = 0.098              # 主板涨停阈值 (与 limitup_ml_build.zt_threshold 同口径)

FEATURES: list[tuple[str, str, str]] = [
    ("open_gap", "首根 open / 前一交易日收盘 - 1", "ratio"),
    ("intraday_ret", "末根 close / 首根 open - 1", "ratio"),
    ("intraday_range", "(当日 max high - min low) / 前收", "ratio"),
    ("close_position", "(末根 close - 当日 min low) / (当日 max high - min low)", "0~1"),
    ("tail_strength", "末根 close / 14:30 及之前最后一根 close - 1", "ratio"),
    ("morning_strength", "10:00 及之前最后一根 close / 首根 open - 1", "ratio"),
    ("am_pm_split", "下午收益 - 上午收益 (以 10:00 收盘为分界)", "ratio"),
    ("max_drawdown_intraday", "min(close / 当日累计最高 close - 1)", "ratio(<=0)"),
    ("max_rise_intraday", "max(close / 当日累计最低 close - 1)", "ratio(>=0)"),
    ("vol_share_first30", "前 30 根成交量 / 当日总成交量", "0~1"),
    ("vol_share_last30", "后 30 根成交量 / 当日总成交量", "0~1"),
    ("vwap_dev", "末根 close / 当日 VWAP - 1", "ratio"),
    ("up_min_ratio", "上涨分钟数 / 有效分钟收益数", "0~1"),
    ("big_move_min_count", f"单分钟涨幅 > {BIG_MOVE_PCT:.0%} 的分钟数", "count"),
    ("ret_autocorr", "分钟收益一阶自相关 (Pearson)", "-1~1"),
    ("realized_vol", "分钟收益标准差 (年化前)", "ratio"),
    ("gap_fill", "开盘缺口被回补的比例 (上跳看最低价, 下跳看最高价), 截断到 0~1", "0~1"),
    ("limitup_touch", "high 触及涨停阈值的分钟数 (主板 9.8%)", "count"),
]
FEATURE_COLS = [f[0] for f in FEATURES]


def zt_threshold(symbol: str) -> float:
    """与 limitup_ml_build.zt_threshold 同口径, 不 import 以免触发 paths 副作用。"""
    c = str(symbol).lower()
    bare = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if bare.startswith(("30", "68")):
        return 0.198
    if bare.startswith(("43", "83", "87", "88", "92")):
        return 0.298
    return ZT_MAIN


def collect_files(target: str, suffix: str = ".parquet") -> list[str]:
    t = os.path.abspath(os.path.expanduser(target))
    if os.path.isdir(t):
        return sorted(glob.glob(os.path.join(t, suffix)))
    if "*" in t:
        return sorted(glob.glob(t))
    return [t] if os.path.isfile(t) else []


def load_minute(files: list[str]) -> Any:
    import pandas as pd
    import pyarrow.parquet as pq

    frames = [pq.read_table(p).to_pandas() for p in files]
    df = pd.concat(frames, ignore_index=True)
    need = {"symbol", "date", "hhmm", "open", "close", "high", "low", "volume"}
    lack = sorted(need - set(df.columns))
    if lack:
        raise ValueError(f"分钟数据缺列 {lack}")
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = df["date"].astype(str)
    df["hhmm"] = df["hhmm"].astype(str)
    # 保证组内按时间升序: 首末根/累计量的定义都依赖顺序
    df = df.sort_values(["symbol", "date", "hhmm"], kind="mergesort").reset_index(drop=True)
    return df


def prev_close_map(minute: Any) -> Any:
    """(symbol, date) -> 同一 symbol 在分钟数据里上一交易日的末根 close。

    与分钟价同源同口径 (原始价, 不复权); 不再混用日线前复权收盘, 否则两源
    价基相差可达 10%+, 会把 open_gap/intraday_range/limitup_touch 整体带偏。
    """
    last = (minute.groupby(["symbol", "date"], sort=True, observed=True)["close"]
            .last().rename("last_close").reset_index())
    last["prev_close"] = last.groupby("symbol", sort=False)["last_close"].shift(1)
    s = last.set_index(["symbol", "date"])["prev_close"].astype("float64")
    s.name = "prev_close"
    return s


def basis_check(minute: Any, daily: Any) -> dict[str, Any]:
    """日线 vs 分钟的价基交叉校验 (只报告, 不参与任何特征计算)。

    日线是前复权、分钟是原始价, 重叠日上相对差 > 1e-3 的 (symbol,date) 占比
    即 mismatch_pct; 只用于发现两源错配, 不用日线修正分钟特征。
    """
    last = (minute.groupby(["symbol", "date"], sort=False, observed=True)["close"]
            .last().rename("minute_close").reset_index())
    d = daily[["symbol", "date", "close"]].copy()
    d["symbol"] = d["symbol"].astype(str)
    d = d.rename(columns={"close": "daily_close"}).drop_duplicates(["symbol", "date"])
    for frame in (last, d):
        frame["date"] = frame["date"].astype(str).str.replace("-", "", regex=False)
    merged = last.merge(d, on=["symbol", "date"], how="inner")
    n = int(len(merged))
    res: dict[str, Any] = {
        "overlap_rows": n,
        "mismatch_rows": 0,
        "mismatch_pct": None,
        "threshold": 1e-3,
        "pairs_gt_1e-6": None,
        "max_abs_diff_pct": None,
        "def": "重叠 (symbol,date) 上 |分钟末根close/日线close-1| > 1e-3 的占比, 仅校验不参与计算",
    }
    if not n:
        return res
    rel = (merged["minute_close"].astype("float64")
           / merged["daily_close"].astype("float64") - 1).abs()
    res["mismatch_rows"] = int((rel > res["threshold"]).sum())
    res["mismatch_pct"] = round(float((rel > res["threshold"]).mean()) * 100, 4)
    res["pairs_gt_1e-6"] = int((rel > 1e-6).sum())
    res["max_abs_diff_pct"] = round(float(rel.max()) * 100, 4)
    return res


def build(minute: Any, prev: Any, min_bars: int = 0) -> Any:
    import pandas as pd

    key = ["symbol", "date"]
    df = minute
    g = df.groupby(key, sort=False, observed=True)

    df["pos"] = g.cumcount()
    df["n_bars"] = g["close"].transform("size")
    df["ret"] = g["close"].pct_change()
    df["run_max"] = g["close"].cummax()
    df["run_min"] = g["close"].cummin()
    df["dd"] = (df["close"] / df["run_max"] - 1).astype("float32")
    df["rise"] = (df["close"] / df["run_min"] - 1).astype("float32")
    df["up"] = df["ret"] > 0
    df["big"] = df["ret"] > BIG_MOVE_PCT
    df["v_first30"] = df["volume"].where(df["pos"] < FIRST30_BARS, 0)
    df["v_last30"] = df["volume"].where(df["pos"] >= df["n_bars"] - LAST30_BARS, 0)

    agg = g.agg(
        first_open=("open", "first"),
        last_close=("close", "last"),
        max_high=("high", "max"),
        min_low=("low", "min"),
        vol_sum=("volume", "sum"),
        amt_sum=("amount", "sum"),
        max_dd=("dd", "min"),
        max_rise=("rise", "max"),
        up_cnt=("up", "sum"),
        big_cnt=("big", "sum"),
        v_first=("v_first30", "sum"),
        v_last=("v_last30", "sum"),
        ret_std=("ret", "std"),
        n_ret=("ret", "count"),
        n_bars=("close", "size"),
        first_hhmm=("hhmm", "first"),
        last_hhmm=("hhmm", "last"),
    )
    agg["prev_close"] = prev.reindex(agg.index).astype("float64")

    # 分钟收益一阶自相关: 只用组内相邻两根, 用矩和算 Pearson
    y = g["ret"].shift(-1)
    mask = df["ret"].notna() & y.notna()
    pairs = df.loc[mask, key].copy()
    pairs["x"] = df.loc[mask, "ret"].astype("float64")
    pairs["y"] = y[mask].astype("float64")
    pairs["xx"] = pairs["x"] ** 2
    pairs["yy"] = pairs["y"] ** 2
    pairs["xy"] = pairs["x"] * pairs["y"]
    sums = pairs.groupby(key, sort=False, observed=True).agg(
        n=("x", "size"), sx=("x", "sum"), sy=("y", "sum"),
        sxx=("xx", "sum"), syy=("yy", "sum"), sxy=("xy", "sum"))
    n = sums["n"].astype("float64")
    num = n * sums["sxy"] - sums["sx"] * sums["sy"]
    den = ((n * sums["sxx"] - sums["sx"] ** 2)
           * (n * sums["syy"] - sums["sy"] ** 2)).clip(lower=0) ** 0.5
    autocorr = (num / den.where(den > 0)).where(n >= 3)

    morning = df[df["hhmm"] <= AM_CUTOFF].groupby(key, sort=False)["close"].last()
    tail = df[df["hhmm"] <= TAIL_CUTOFF].groupby(key, sort=False)["close"].last()

    thr = df["symbol"].map({s: zt_threshold(s) for s in df["symbol"].unique()})
    df["prev_close_bc"] = prev.reindex(
        pd.MultiIndex.from_arrays([df["symbol"], df["date"]])).to_numpy()
    hit = df["high"] >= df["prev_close_bc"] * (1 + thr)
    touch = hit.groupby([df["symbol"], df["date"]], sort=False).sum()

    # 碎片日（上游 tier 模式只剩 5 根之类）算出的「日内特征」是伪值，整组丢弃。
    # 覆盖判定以时间域优先: 首根不晚于 09:35、末根不早于 14:55, 再看根数下限 ——
    # 低流动性票盘中无成交分钟会被上游省略, 只卡根数会系统性剔除这类票。
    complete = ((agg["n_bars"] >= min_bars)
                & (agg["first_hhmm"] <= FIRST_BAR_MAX)
                & (agg["last_hhmm"] >= LAST_BAR_MIN))
    excluded = agg[~complete]
    agg = agg[complete]

    out = pd.DataFrame(index=agg.index)
    pc = agg["prev_close"]
    o = agg["first_open"].astype("float64")
    c = agg["last_close"].astype("float64")
    hi = agg["max_high"].astype("float64")
    lo = agg["min_low"].astype("float64")

    out["open_gap"] = (o / pc - 1).where(pc > 0)
    out["intraday_ret"] = (c / o - 1).where(o > 0)
    out["intraday_range"] = ((hi - lo) / pc).where(pc > 0)
    out["close_position"] = ((c - lo) / (hi - lo)).where(hi > lo)
    mc = morning.reindex(out.index).astype("float64")
    tc = tail.reindex(out.index).astype("float64")
    out["tail_strength"] = (c / tc - 1).where(tc > 0)
    out["morning_strength"] = (mc / o - 1).where(o > 0)
    out["am_pm_split"] = ((c / mc - 1) - (mc / o - 1)).where((mc > 0) & (o > 0))
    out["max_drawdown_intraday"] = agg["max_dd"].astype("float64")
    out["max_rise_intraday"] = agg["max_rise"].astype("float64")
    vol = agg["vol_sum"].astype("float64")
    out["vol_share_first30"] = (agg["v_first"] / vol).where(vol > 0)
    out["vol_share_last30"] = (agg["v_last"] / vol).where(vol > 0)
    vwap = agg["amt_sum"].astype("float64") / vol.where(vol > 0)
    out["vwap_dev"] = (c / vwap - 1).where(vwap > 0)
    out["up_min_ratio"] = (agg["up_cnt"] / agg["n_ret"]).where(agg["n_ret"] > 0)
    out["big_move_min_count"] = agg["big_cnt"].astype("float64")
    out["ret_autocorr"] = autocorr.reindex(out.index).astype("float64")
    out["realized_vol"] = agg["ret_std"].astype("float64")
    gap = (o - pc).where(pc > 0)
    up_fill = ((o - lo) / gap).where(gap > 0)
    down_fill = ((hi - o) / (-gap)).where(gap < 0)
    out["gap_fill"] = (up_fill.where(gap > 0, down_fill).clip(0, 1)
                       .mask(gap == 0, 0.0))
    # 前收缺失时「是否触及涨停」不可知, 写 null 而不是 0
    out["limitup_touch"] = (touch.reindex(out.index).astype("float64")
                            .where(pc.notna()))

    excluded_by_date = {str(k): int(v) for k, v in
                        excluded.index.get_level_values("date").value_counts().items()}
    out = out.reset_index()
    return out[["symbol", "date", *FEATURE_COLS]], excluded_by_date


def write_parquet(df: Any, path: str) -> None:
    """NaN 显式写成 parquet null, 便于下游区分「缺失」与数值 0。"""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pa.Table.from_pandas(df, preserve_index=False)
    cols = []
    for name in table.column_names:
        col = table.column(name)
        if pa.types.is_floating(col.type):
            col = pc.if_else(pc.is_nan(col), pa.scalar(None, type=col.type), col)
        cols.append(col)
    table = pa.Table.from_arrays(cols, names=table.column_names)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".part"
    pq.write_table(table, tmp, compression="snappy")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="分钟 parquet -> 日内特征表")
    ap.add_argument("--minute", required=True, help="分钟 parquet 文件/目录/glob")
    ap.add_argument("--daily", default="", help="可选日线 parquet, 只做前收口径交叉校验")
    ap.add_argument("--out", required=True, help="特征 parquet 输出路径")
    ap.add_argument("--manifest", default="", help="特征清单 JSON (默认 <out>.json)")
    ap.add_argument("--min-bars", type=int, default=120,
                    help="每票每日最少分钟根数 (默认 120; 另需首根<=09:35 且末根>=14:55; 低于 30 报错)")
    args = ap.parse_args()

    if args.min_bars < 30:
        print(f"✗ --min-bars {args.min_bars} 太小: 碎片组的日内特征全是伪值, 下限 30",
              file=sys.stderr)
        return 2

    minute_files = collect_files(args.minute)
    daily_files = collect_files(args.daily) if args.daily else []
    if not minute_files:
        print(f"✗ 没有分钟文件: {args.minute}", file=sys.stderr)
        return 2
    if args.daily and not daily_files:
        print(f"✗ 没有日线文件: {args.daily}", file=sys.stderr)
        return 2

    try:
        import pandas as pd
        import pyarrow.parquet as pq

        minute = load_minute(minute_files)
        daily = (pd.concat([pq.read_table(p).to_pandas() for p in daily_files],
                           ignore_index=True) if daily_files else None)
    except ImportError as exc:
        print(f"✗ 需要 pyarrow + pandas: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"✗ 读取失败: {exc}", file=sys.stderr)
        return 2

    prev = prev_close_map(minute)
    feats, excluded_by_date = build(minute, prev, min_bars=args.min_bars)
    write_parquet(feats, args.out)

    total_groups = int(minute.groupby(["symbol", "date"], sort=False).ngroups)
    per_day = (feats.groupby("date", sort=True)
               .agg(rows=("symbol", "size"), symbols=("symbol", "nunique")))
    bars = (minute.groupby(["date", "symbol"], sort=False).size()
            .groupby(level=0).median())
    per_day_out = [{
        "date": str(d),
        "rows": int(per_day.loc[d, "rows"]),
        "symbols": int(per_day.loc[d, "symbols"]),
        "bars_per_sym_median": float(bars.loc[d]),
        "excluded_groups": int(excluded_by_date.get(str(d), 0)),
    } for d in per_day.index]

    manifest_path = args.manifest or (os.path.splitext(args.out)[0] + ".features.json")
    manifest = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "minute_features",
        "source": {
            "minute_files": minute_files,
            "daily_files": daily_files,
            "minute_rows": int(len(minute)),
            "minute_symbols": int(minute["symbol"].nunique()),
            "minute_dates": [per_day_out[0]["date"], per_day_out[-1]["date"]]
            if per_day_out else [None, None],
            "prev_close_def": "分钟自身上一交易日末根 close（原始价，不复权）",
        },
        "basis_check": basis_check(minute, daily) if daily is not None else None,
        "output": {"path": os.path.abspath(args.out), "rows": int(len(feats)),
                   "feature_dim": len(FEATURE_COLS)},
        "completeness": {
            "min_bars": args.min_bars,
            "groups_total": total_groups,
            "skipped_incomplete": total_groups - int(len(feats)),
            "policy": "要求首根<=09:35 且末根>=14:55 且根数>=min_bars, 不达标的整组丢弃, 不插值、不用日线反推",
            "excluded_by_date": {str(k): int(v)
                                 for k, v in sorted(excluded_by_date.items())},
        },
        "feature_cols": FEATURE_COLS,
        "feature_defs": [{"name": n, "definition": d, "unit": u, "null_policy": "缺失写 null, 不插值"}
                         for n, d, u in FEATURES],
        "null_policy": "NaN -> parquet null; 缺前收的行跳空类特征与 limitup_touch 为 null",
        "per_day": per_day_out,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    # 下游 limitup_ml_build.py 默认读同目录的 minute_features_manifest.json 发现列名
    compat = os.path.join(os.path.dirname(os.path.abspath(args.out)),
                          "minute_features_manifest.json")
    if os.path.abspath(compat) != os.path.abspath(manifest_path):
        with open(compat, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print(f"分钟 {len(minute):,} 行 / {manifest['source']['minute_symbols']} 只 / "
          f"{len(per_day_out)} 个交易日")
    print(f"跳过不完整组 {total_groups - len(feats):,} / {total_groups:,} "
          f"(min_bars={args.min_bars})")
    print(f"→ 特征 {len(feats):,} 行 × {len(FEATURE_COLS)} 维: {args.out}")
    print(f"→ 清单 {manifest_path}")
    for d in per_day_out:
        print(f"  {d['date']}  rows={d['rows']:>5}  symbols={d['symbols']:>5}  "
              f"bars/sym median={d['bars_per_sym_median']:.0f}  "
              f"excluded_groups={d['excluded_groups']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
