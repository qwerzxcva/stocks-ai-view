#!/usr/bin/env python3
"""
limitup_ml_build.py — A 股次日涨停特征构建器（bulk 数据适配版）

移植自 GitCode stock_analysis/limitup2/build.py，数据源改为 bulk 分片。
bulk 格式: code|date|open|close|high|low|vol  (code 带 sh/sz/bj 前缀)

A 股涨停阈值按交易所/板块区分:
  主板 (sh60/sz00)        9.8%
  创业板/科创 (sz30/sh68) 19.8%
  北交所 (bj43/83/87/92)  29.8%

落盘: data/lp_feat.f32 / lp_label.b / lp_ret.f4 / lp_date.i4 / lp_sym.i4 /
      lp_syms.json / lp_meta.json

可选 --minute-features <parquet> --minute-cols a,b,...：把 minute_features.py 产出的
日内特征按 (symbol, date) 追加到每行样本末尾；缺分钟数据的样本这些列填 NaN（不填 0），
meta 里记录 minute_coverage_pct。不传时行为与此前完全一致。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from array import array
from collections import deque
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
from paths import BULK, ai_view_data
DATA = ai_view_data()

NF = 15
MINUTE_COVERAGE_MIN = 20.0       # 分钟特征命中率低于此值(%) -> meta.minute.status=insufficient


def load_minute_features(path: str, cols_arg: str = ""):
    """读分钟日内特征 parquet -> (列名, {(symbol, date_int): 特征元组})。

    缺 pyarrow 时明确报错退出，不静默把缺失值当 0。
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("✗ --minute-features 需要 pyarrow: pip install pyarrow", file=sys.stderr)
        sys.exit(3)
    if not os.path.isfile(path):
        print(f"✗ --minute-features 不存在: {path}", file=sys.stderr)
        sys.exit(2)
    cols = [c.strip() for c in cols_arg.split(",") if c.strip()]
    if not cols:
        mf = os.path.join(os.path.dirname(os.path.abspath(path)), "minute_features_manifest.json")
        if os.path.exists(mf):
            with open(mf, encoding="utf-8") as f:
                cols = list(json.load(f).get("feature_cols") or [])
    table = pq.read_table(path)
    names = table.column_names
    if not cols:
        cols = [c for c in names if c not in ("symbol", "date")]
    missing = [c for c in cols if c not in names]
    if missing:
        print(f"✗ 分钟特征 parquet 缺列: {missing}", file=sys.stderr)
        sys.exit(2)
    d = table.select(["symbol", "date"] + cols).to_pydict()
    nan = float("nan")
    keyed = {}
    dmin = dmax = None
    for i, sym in enumerate(d["symbol"]):
        try:
            di = int(str(d["date"][i]).replace("-", "").replace("/", ""))
        except ValueError:
            continue
        keyed[(str(sym), di)] = tuple(nan if d[c][i] is None else float(d[c][i]) for c in cols)
        dmin = di if dmin is None or di < dmin else dmin
        dmax = di if dmax is None or di > dmax else dmax
    return cols, keyed, [dmin, dmax]


def zt_threshold(code: str) -> float:
    """按代码前缀返回涨停阈值（小数）。code 形如 sh600017 / sz300750 / bj430047。"""
    c = str(code).lower()
    if c.startswith("bj"):
        return 0.298
    bare = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if bare.startswith(("30", "68")):
        return 0.198
    if bare.startswith(("43", "83", "87", "88", "92")):
        return 0.298
    return 0.098


def load_bulk(bulk_dir: str, limit_shards: int = 0) -> dict:
    """读 bulk 分片 → {code: [(date, open, close, high, low, vol), ...]} 按日期升序。"""
    paths = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    if limit_shards:
        paths = paths[:limit_shards]
    data: dict = {}
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                code, date, o, c, h, lo, v = parts[:7]
                try:
                    data.setdefault(code, []).append(
                        (date, float(o), float(c), float(h), float(lo), float(v))
                    )
                except ValueError:
                    continue
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def roll_mean(x, w):
    n = len(x); out = [0.0] * n; s = 0.0
    for i in range(n):
        s += x[i]
        if i >= w:
            s -= x[i - w]
        out[i] = s / w if i >= w - 1 else 0.0
    return out


def roll_max(x, w):
    n = len(x); out = [0.0] * n; dq = deque()
    for i in range(n):
        while dq and x[dq[-1]] <= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        out[i] = x[dq[0]]
    return out


def roll_min(x, w):
    n = len(x); out = [0.0] * n; dq = deque()
    for i in range(n):
        while dq and x[dq[-1]] >= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        out[i] = x[dq[0]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=BULK)
    ap.add_argument("--out", default=DATA)
    ap.add_argument("--limit-shards", type=int, default=0, help="只读前 N 个分片（冒烟用）")
    ap.add_argument("--minute-features", default="",
                    help="分钟日内特征 parquet（可选，命中 (symbol,date) 追加到样本末尾）")
    ap.add_argument("--minute-cols", default="",
                    help="逗号分隔的分钟特征列名；省略时读同级 minute_features_manifest.json")
    ap.add_argument("--date-from", type=int, default=0,
                    help="样本日期下界 yyyymmdd（含）；0=不限。用于只跑分钟数据覆盖的窗口")
    ap.add_argument("--date-to", type=int, default=0,
                    help="样本日期上界 yyyymmdd（含）；0=不限")
    args = ap.parse_args()

    if not os.path.isdir(args.bulk):
        print(f"找不到 bulk 目录 {args.bulk}", file=sys.stderr)
        return 1

    print(f"加载 bulk 数据: {args.bulk}" + (f" (前 {args.limit_shards} 分片)" if args.limit_shards else ""))
    klines = load_bulk(args.bulk, args.limit_shards)
    syms = sorted(klines.keys())
    print(f"股票 {len(syms)} 只")

    feats = array("f"); labels = array("b"); rets = array("f")
    dates = array("i"); symidx = array("i"); symlist = []

    minute_cols = []
    minute_map = {}
    minute_dates = [None, None]
    minute_min_bars = None
    if args.minute_features:
        minute_cols, minute_map, minute_dates = load_minute_features(
            args.minute_features, args.minute_cols)
        mf = os.path.join(os.path.dirname(os.path.abspath(args.minute_features)),
                          "minute_features_manifest.json")
        if os.path.exists(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    minute_min_bars = (json.load(f).get("completeness") or {}).get("min_bars")
            except (OSError, ValueError):
                minute_min_bars = None
        print(f"分钟特征 {len(minute_cols)} 维, 覆盖 {len(minute_map):,} 个 (symbol,date)")
    nan_row = tuple(float("nan") for _ in minute_cols)
    matched_rows = 0
    minute_col_nulls = [0] * len(minute_cols)

    for s in syms:
        bars = klines[s]
        if len(bars) < 140:
            continue
        d, c, h, l, v = [], [], [], [], []
        for b in bars:
            d.append(int(b[0].replace("-", "")))
            c.append(b[2]); h.append(b[3]); l.append(b[4]); v.append(b[5])
        n = len(c)
        if n < 140:
            continue

        vm = roll_mean(v, 20); v5 = roll_mean(v, 5)
        rh = roll_max(h, 20); rl = roll_min(l, 20)
        c5 = roll_mean(c, 5); c20 = roll_mean(c, 20)

        thr = zt_threshold(s)
        si = len(symlist); symlist.append(s)
        cu = 0
        for i in range(126, n - 1):
            if args.date_from and d[i] < args.date_from:
                continue
            if args.date_to and d[i] > args.date_to:
                continue
            if c5[i] <= 0 or c20[i] <= 0 or vm[i] <= 0 or rh[i] <= 0 or rl[i] <= 0:
                continue
            r1 = c[i] / c[i - 1] - 1
            cu = cu + 1 if r1 > 0 else 0
            row = (
                r1,
                c[i] / c[i - 2] - 1,
                c[i] / c[i - 5] - 1,
                c[i] / c[i - 10] - 1,
                c[i] / c[i - 21] - 1,
                c[i] / c[i - 63] - 1,
                v[i] / vm[i],
                c[i] / rh[i],
                c[i] / rl[i],
                c[i] / c5[i],
                c[i] / c20[i],
                v5[i] / vm[i],
                (h[i] - l[i]) / c[i],
                min(cu, 10),
                c[i] / c[i - 126] - 1,
            )
            rn = c[i + 1] / c[i] - 1
            lab = 1 if (rn >= thr and r1 < thr) else 0
            if minute_cols:
                extra = minute_map.get((s, d[i]))
                if extra is None:
                    extra = nan_row
                else:
                    # 命中 (symbol,date) 后才算覆盖; 其中该列为空的计入 null 比
                    matched_rows += 1
                    for j, val in enumerate(extra):
                        if val != val:
                            minute_col_nulls[j] += 1
                feats.extend(row + extra)
            else:
                feats.extend(row)
            labels.append(lab)
            rets.append(rn)
            dates.append(d[i])
            symidx.append(si)

        if len(symlist) % 500 == 0:
            print(f"  ..{len(symlist)} 只, 样本 {len(labels):,}")

    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, "lp")
    open(base + "_feat.f32", "wb").write(feats.tobytes())
    open(base + "_label.b", "wb").write(labels.tobytes())
    open(base + "_ret.f4", "wb").write(rets.tobytes())
    open(base + "_date.i4", "wb").write(dates.tobytes())
    open(base + "_sym.i4", "wb").write(symidx.tobytes())
    json.dump(symlist, open(base + "_syms.json", "w", encoding="utf-8"), ensure_ascii=False)

    pos = sum(labels)
    nf = NF + len(minute_cols)
    meta = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "limitup_ml_build",
        "n_symbols": len(symlist),
        "n_samples": len(labels),
        "n_positive": pos,
        "base_rate_pct": round(pos / len(labels) * 100, 4) if labels else 0.0,
        "feature_dim": nf,
        "label_def": "次日涨幅 >= 板块涨停阈值 且 今日未涨停",
        "zt_thresholds": {"main": 0.098, "gem_star": 0.198, "bse": 0.298},
        "provenance": {"source": "bulk/day_qfq_*.csv", "origin": "GitCode limitup2/build.py"},
    }
    if minute_cols:
        coverage_pct = round(matched_rows / len(labels) * 100, 4) if labels else 0.0
        meta["minute_feature_cols"] = minute_cols
        meta["minute_coverage_pct"] = coverage_pct
        meta["minute"] = {
            "status": "ok" if coverage_pct >= MINUTE_COVERAGE_MIN else "insufficient",
            "coverage_pct": coverage_pct,
            "matched_rows": matched_rows,
            "n_samples": len(labels),
            "dates": minute_dates,
            "min_bars": minute_min_bars,
            "source": os.path.abspath(args.minute_features),
            "minute_col_null_pct": {
                c: (round(minute_col_nulls[j] / matched_rows * 100, 4)
                    if matched_rows else None)
                for j, c in enumerate(minute_cols)},
        }
        if meta["minute"]["status"] != "ok":
            print(f"! 警告: 分钟特征覆盖率 {coverage_pct:.2f}% < {MINUTE_COVERAGE_MIN:.0f}%,"
                  " minute.status=insufficient, 这些列不可用于训练")
        meta["provenance"]["minute_features"] = os.path.abspath(args.minute_features)
    json.dump(meta, open(base + "_meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n完成: 股票 {len(symlist)}, 样本 {len(labels):,}, 正例 {pos:,} ({meta['base_rate_pct']:.3f}%)")
    print(f"→ {base}_*.f32/.b/.f4/.i4 + _syms.json + _meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
