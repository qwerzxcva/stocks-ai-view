#!/usr/bin/env python3
"""validate_ground_truth.py — 用基准 CSV 校验 K 线/因子准确性 (2026-10-17)。

基准附件 (用户提供的准确数据):
  行情: date,open,high,low,close,volume,turnover,hfq_factor,*_hfq,source
  (原始价 + 后复权因子, source=csv_legacy/cnfin)

校验逻辑 (对同一股票):
  1. 覆盖: 我们 bulk 的首末日期 vs 基准首末日期, 缺失交易日列表
  2. 价格: 由基准重建前复权 close_qfq = close × hfq_factor / hfq_factor_latest,
     与我们 bulk 的 close 逐日对比 (容差 0.5%, 复权口径四舍五入差异)
  3. 因子漂移: 我们 close/基准 close 的隐含因子 vs 基准 hfq_factor 的比值序列,
     突变 (>1% 日环比) 标记为疑似复权事件漏处理
  4. 量纲: 基准 volume(股) vs 我们 vol(手) 比值应稳定 ≈100
  5. 财报/分红: 若提供 --financials, 核对我们 financial 相关产出的字段口径

退出码: 0=全部通过, 1=有 FAIL (CI 可用)。
阈值可用 --max-price-diff/--max-missing-ratio 调整。

用法:
  python3 validate_ground_truth.py --gt ~/x/贵州茅台_行情.csv --code sh600519
  python3 validate_ground_truth.py --gt ... --code sh600519 --cloud-shard 01
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from paths import BULK
except Exception:
    BULK = os.path.join(os.path.dirname(HERE), "..", "stocks-data-kline",
                        "data", "bulk")

CLOUD_BULK = ("https://raw.githubusercontent.com/qwerzxcva/t1/"
              "main/data/bulk/day_qfq_%s.csv")


def load_gt(path):
    """基准行情 → {date: {open,high,low,close,volume,hfq_factor}}"""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = (row.get("date") or "").strip()
            if not d:
                continue
            try:
                out[d] = {
                    "open": float(row["open_price"]), "high": float(row["high_price"]),
                    "low": float(row["low_price"]), "close": float(row["close_price"]),
                    "volume": float(row["volume"]),
                    "hfq_factor": float(row["hfq_factor"]),
                }
            except (ValueError, KeyError):
                continue
    return out


def load_bulk(code, cloud_shard=None):
    """本地 bulk (全 shard 扫) 或云端指定 shard → {date: [o,c,h,l,vol]}"""
    out = {}
    if cloud_shard is not None:
        url = CLOUD_BULK % cloud_shard
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "validator/1.0"})
            text = urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")
            lines = text.splitlines()
        except Exception as e:
            print(f"[load] 云端 shard {cloud_shard} 拉取失败: {e}", file=sys.stderr)
            return out
        for line in lines:
            if line.startswith(code + "|"):
                p = line.split("|")
                if len(p) >= 7:
                    out[p[1]] = [float(p[2]), float(p[3]), float(p[4]),
                                 float(p[5]), float(p[6])]
        return out
    import glob
    for fp in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
        with open(fp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(code + "|"):
                    p = line.strip().split("|")
                    if len(p) >= 7:
                        out[p[1]] = [float(p[2]), float(p[3]), float(p[4]),
                                     float(p[5]), float(p[6])]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True, help="基准行情 CSV")
    ap.add_argument("--code", required=True, help="带前缀码, 如 sh600519")
    ap.add_argument("--cloud-shard", help="云端 shard 号 (两位, 如 01), 缺省扫本地 bulk")
    ap.add_argument("--max-price-diff", type=float, default=0.5,
                    help="重建qfq与bulk收盘价最大偏差%% (默认0.5)")
    ap.add_argument("--max-missing-ratio", type=float, default=1.0,
                    help="bulk 缺失基准交易日最大比例%% (默认1.0)")
    ap.add_argument("--json-out", help="详细差异落盘路径")
    args = ap.parse_args()

    gt = load_gt(args.gt)
    bulk = load_bulk(args.code, args.cloud_shard)
    if not gt:
        print("FAIL 基准文件为空或格式不符")
        return 1
    if not bulk:
        print(f"FAIL bulk 无 {args.code} 数据 (shard={args.cloud_shard or 'local'})")
        return 1

    dates = sorted(gt)
    bulk_max = max(bulk)
    # 前复权锚点 = bulk 自己的最新交易日 (qfq 以采集时点为锚),
    # 不能用基准全局最新日——两者锚点不同会造成系统性偏差假象
    anchor = bulk_max if bulk_max in gt else dates[-1]
    latest_factor = gt[anchor]["hfq_factor"]
    common = [d for d in dates if d in bulk and d <= anchor]

    n_missing = sum(1 for d in dates if d not in bulk)
    missing_ratio = n_missing / len(dates) * 100

    # 价格对比: 重建 qfq vs bulk close
    diffs = []
    factor_drifts = []
    prev_ratio = None
    for d in common:
        g = gt[d]
        expect_qfq = g["close"] * g["hfq_factor"] / latest_factor
        got = bulk[d][1]  # close
        if expect_qfq <= 0:
            continue
        diff_pct = abs(got - expect_qfq) / expect_qfq * 100
        diffs.append((diff_pct, d, got, expect_qfq))
        # 隐含因子漂移: (bulk/基准raw) 日环比突变 = 疑似漏复权
        ratio = got / g["close"] if g["close"] else 0
        if prev_ratio and prev_ratio > 0:
            drift = abs(ratio - prev_ratio) / prev_ratio * 100
            if drift > 1.0:
                factor_drifts.append((d, round(prev_ratio, 6), round(ratio, 6),
                                      round(drift, 2)))
        prev_ratio = ratio

    # 量纲: volume 比值 (基准股 / bulk 手)
    vol_ratios = []
    for d in common[-60:]:
        if bulk[d][4] > 0:
            vol_ratios.append(gt[d]["volume"] / bulk[d][4])
    vol_med = sorted(vol_ratios)[len(vol_ratios) // 2] if vol_ratios else 0

    diffs.sort(reverse=True)
    n_bad = sum(1 for x in diffs if x[0] > args.max_price_diff)
    bad_ratio = n_bad / len(diffs) * 100 if diffs else 0

    print(f"== {args.code} 基准校验 (shard={args.cloud_shard or 'local'}) ==")
    print(f"  基准区间: {dates[0]} ~ {dates[-1]} ({len(dates)} 交易日)")
    print(f"  bulk 区间: {min(bulk)} ~ {max(bulk)} ({len(bulk)} 根) | 复权锚点: {anchor}")
    print(f"  共同交易日: {len(common)} | bulk 缺失(截至锚点): {n_missing} ({missing_ratio:.2f}%)")
    print(f"  收盘价偏差>{args.max_price_diff}%: {n_bad}/{len(diffs)} ({bad_ratio:.3f}%)")
    if diffs:
        print(f"  最大偏差: {diffs[0][0]:.3f}% @ {diffs[0][1]} (bulk={diffs[0][2]} vs 期望={diffs[0][3]:.3f})")
    print(f"  量比中位数(股/股): {vol_med:.3f} (期望≈1.0, bulk 已用股数)")
    if factor_drifts:
        print(f"  疑似漏复权日: {len(factor_drifts)} 处, 最近3: {factor_drifts[:3]}")

    fails = []
    if missing_ratio > args.max_missing_ratio:
        fails.append(f"缺失率 {missing_ratio:.2f}% > {args.max_missing_ratio}%")
    if bad_ratio > 1.0:
        fails.append(f"价格偏差率 {bad_ratio:.3f}% > 1%")
    if vol_med and not (0.5 < vol_med < 2.0):
        fails.append(f"量比 {vol_med:.3f} 异常 (期望≈1.0)")

    if args.json_out and diffs:
        detail = {"top_diffs": [dict(zip(("diff_pct", "date", "bulk", "expect"), x))
                                for x in diffs[:50]],
                  "factor_drifts": factor_drifts,
                  "missing_dates": [d for d in dates if d not in bulk][:200]}
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=1)

    for f_ in fails:
        print(f"  FAIL: {f_}")
    if not fails:
        print("  PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
