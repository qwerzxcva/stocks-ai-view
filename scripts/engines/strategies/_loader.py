#!/usr/bin/env python3
"""_loader.py — 策略共享 K 线加载器（独立实现，策略规则来自用户提供）。

从 s01_ema_strict.py 抽出的 load_kline_from_bulk（含复权基准不一致修正）：
  - 主分片（00-09/20/30/68）优先取全历史，作为复权基准段；
  - 增量分片（60/recent/extra*）只补主分片缺失的最新日期；
  - 按日期去重排序，输出升序 K 线。

K 线数据口径：data/bulk/day_qfq_*.csv 七列 code|date|open|close|high|low|vol，前复权。
s01 本体本次不改，后续可自行切换到本模块。
"""
from __future__ import annotations

import glob
import os

# 分片角色：全历史主分片（行数多，复权基准）vs 增量分片（行数少，补最新）。
# 复权基准不一致修正（R1 发现）：同一股票跨分片价格基准不同，规则=主分片优先，
# 增量分片仅用于补充主分片缺失的最新日期，且价格不与前复权历史混排直接比较——
# 策略只用序列内相对关系（EMA/乖离），混排时以主分片为基准段，增量段接在其后。
_HISTORY_SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
                   "20", "30", "68"]
_INCREMENTAL_SHARDS = ["60", "recent", "extra"]

DEFAULT_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data"))


def normalize_code(code: str) -> str:
    """600519 -> sh600519；000001 -> sz000001。已带前缀则原样返回。"""
    if not code.startswith(("sh", "sz", "bj")):
        pref = "sh" if code.startswith(("60", "68")) else (
            "sz" if code.startswith(("00", "30")) else "bj")
        code = pref + code
    return code


def _read_shard(path: str, target: str) -> dict:
    """读单个分片里某股票的全部行，返回 {date: (o,c,h,l,v)}。"""
    rows = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith(target):
                    p = line.rstrip("\n").split("|")
                    if len(p) >= 7:
                        try:
                            rows[p[1]] = (float(p[2]), float(p[3]), float(p[4]),
                                          float(p[5]), float(p[6]))
                        except ValueError:
                            continue
    except FileNotFoundError:
        pass
    return rows


def load_kline_from_bulk(data_dir: str, code: str) -> dict:
    """从 bulk 分片读取单只股票 K 线，主分片优先 + 增量补最新 + 按日期去重排序。
    code 如 sh600519 或 600519。返回升序 K 线。"""
    code = normalize_code(code)
    target = code + "|"
    bulk = os.path.join(data_dir, "bulk")

    # 1) 主分片：收集全历史（取行数最多的那个分片作为复权基准）
    hist_rows: dict = {}
    for shard in _HISTORY_SHARDS:
        rows = _read_shard(os.path.join(bulk, f"day_qfq_{shard}.csv"), target)
        if len(rows) > len(hist_rows):
            hist_rows = rows

    # 2) 增量分片：只补主分片没有的更新日期
    if hist_rows:
        latest_hist = max(hist_rows.keys())
        for shard in _INCREMENTAL_SHARDS:
            for path in glob.glob(os.path.join(bulk, f"day_qfq_{shard}*.csv")):
                rows = _read_shard(path, target)
                for d, v in rows.items():
                    if d > latest_hist and d not in hist_rows:
                        hist_rows[d] = v
    else:
        # 主分片没有，兜底全部增量分片
        for path in glob.glob(os.path.join(bulk, "day_qfq_*.csv")):
            hist_rows.update(_read_shard(path, target))

    dates = sorted(hist_rows.keys())
    return {
        "date": dates,
        "open": [hist_rows[d][0] for d in dates],
        "close": [hist_rows[d][1] for d in dates],
        "high": [hist_rows[d][2] for d in dates],
        "low": [hist_rows[d][3] for d in dates],
        "vol": [hist_rows[d][4] for d in dates],
    }


def slice_asof(kline: dict, asof: str) -> dict:
    """--asof 严格无前视：截断到 asof 当日（含）。asof 格式 YYYY-MM-DD。"""
    if not asof:
        return kline
    dates = kline.get("date", [])
    end = 0
    for i, d in enumerate(dates):
        if d <= asof:
            end = i + 1
        else:
            break
    return {k: v[:end] for k, v in kline.items()}


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="sh600519")
    ap.add_argument("--data", default=DEFAULT_DATA_DIR)
    ap.add_argument("--asof", default=None)
    args = ap.parse_args()
    kl = load_kline_from_bulk(args.data, args.code)
    if args.asof:
        kl = slice_asof(kl, args.asof)
    n = len(kl["date"])
    print(json.dumps({
        "code": normalize_code(args.code), "bars": n,
        "first": kl["date"][0] if n else None, "last": kl["date"][-1] if n else None,
        "last_close": kl["close"][-1] if n else None,
        "monotonic": all(kl["date"][i] < kl["date"][i + 1] for i in range(n - 1)),
    }, ensure_ascii=False, indent=2))
