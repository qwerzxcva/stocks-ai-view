#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/rps_rotation.py ═══
# 上游 commit: bf9cf060c570 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (duckdb → DUCKDB_PATH; 数据源改 bulk CSV; --out 落盘)
"""
RPS 相对强度轮动 (参考 tick-stock-panel rps_rotation, pandas版)
用法: python3 rps_rotation.py [--bulk DIR] [--top 30] [--out data/rps_rotation.json]
对全市场每票计算 RPS(250/120/60/20日涨幅百分位), 输出强势股+强度趋势.

数据源 (本项目): stocks-data-kline bulk CSV (day_qfq_*.csv, code|date|open|close|high|low|vol)
  上游原版读 stockdb/stock.duckdb 的 daily 表 — 本项目无该库, 用 bulk 等价替代。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

WINDOWS = [("rps_250", 250), ("rps_120", 120), ("rps_60", 60), ("rps_20", 20)]


def load_closes(bulk_dir: str) -> pd.DataFrame:
    """bulk day_qfq CSV → 收盘价宽表 (index=date, columns=code)。
    分片分隔符不统一: day_qfq_00..09 用 '|', _extra/_recent 用 ',' (与 engine_v5.load_klines 同策略)。"""
    import glob
    files = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    if not files:
        raise FileNotFoundError(f"bulk 无 day_qfq_*.csv: {bulk_dir}")
    frames = []
    for p in files:
        try:
            recs = []
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|") if "|" in line else line.split(",")
                    if len(parts) < 6:
                        continue
                    try:
                        # CSV: code|date|open|close|high|low|vol → parts[3] 才是 close
                        recs.append((parts[0], parts[1], float(parts[3])))
                    except ValueError:
                        continue
            if recs:
                frames.append(pd.DataFrame(recs, columns=["code", "date", "close"]))
        except Exception as e:
            print(f"[rps] skip {os.path.basename(p)}: {e}", file=sys.stderr)
    if not frames:
        raise ValueError("bulk CSV 全部解析失败")
    all_df = pd.concat(frames, ignore_index=True)
    # bulk 分片里同一 (date, code) 可能重复 (跨分片文件重名/同票多行),
    # pivot 遇重复直接炸 (2026-10-17 CI 实测 87h 未更新)。去重取最后一行。
    all_df = all_df.drop_duplicates(subset=["date", "code"], keep="last")
    closes = all_df.pivot(index="date", columns="code", values="close").sort_index()
    return closes


def build(closes: pd.DataFrame) -> pd.DataFrame:
    out = []
    for wname, w in WINDOWS:
        ret = closes.pct_change(w).iloc[-1]
        rps = ret.rank(pct=True) * 100
        out.append(pd.DataFrame({wname: rps}))
    rps_df = pd.concat(out, axis=1)
    rps_df["date"] = closes.index[-1]
    rps_df = rps_df.reset_index()
    return rps_df.rename(columns={"code": "symbol"})


def top_strong(rps_df: pd.DataFrame, n=30) -> pd.DataFrame:
    rps_df = rps_df.dropna(subset=["rps_60"]).copy()
    rps_df["strong_score"] = rps_df[["rps_250", "rps_120", "rps_60", "rps_20"]].mean(axis=1)
    return rps_df.sort_values("strong_score", ascending=False).head(n)


def main():
    ap = argparse.ArgumentParser(description="RPS 相对强度轮动")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "rps_rotation.json"))
    args = ap.parse_args()

    bulk = args.bulk or BULK
    closes = load_closes(bulk)
    print(f"[rps] {closes.shape[1]} 只股票, {closes.shape[0]} 个交易日 ({closes.index[0]} ~ {closes.index[-1]})")
    rps = build(closes)
    top = top_strong(rps, args.top)
    print("=== RPS 强势 Top15 (四窗口均值) ===")
    print(top.head(15).round(1).to_string(index=False))

    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": f"bulk day_qfq ({os.path.basename(bulk)})",
        "date": str(closes.index[-1]),
        "n_universe": int(closes.shape[1]),
        "top": [{"symbol": r["symbol"], "strong_score": round(r["strong_score"], 1),
                 "rps_250": None if pd.isna(r["rps_250"]) else round(r["rps_250"], 1),
                 "rps_120": None if pd.isna(r["rps_120"]) else round(r["rps_120"], 1),
                 "rps_60": None if pd.isna(r["rps_60"]) else round(r["rps_60"], 1),
                 "rps_20": None if pd.isna(r["rps_20"]) else round(r["rps_20"], 1)}
                for _, r in top.iterrows()],
        "note": "RPS=区间涨幅在全市场百分位; 算法逻辑与上游一致, 仅换数据源",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
