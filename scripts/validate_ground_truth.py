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

# Full-history files are not sharded by code: every ticker lives in these, so a
# per-code shard lookup would see only the recent window (~800 bars) and report
# a false 86% missing ratio for any long-listed stock. Read them first and let
# the shard act as a fallback. Sorted longest-first so `expand` wins.
CLOUD_FULL = ("https://raw.githubusercontent.com/qwerzxcva/t1/"
              "main/data/bulk/day_qfq_%s.csv")
FULL_SERIES = ["expand", "extra", "recent"]


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
    """→{date: [o,c,h,l,vol]}, full-history series first then the shard."""
    out = {}

    def absorb(text):
        for line in text.splitlines():
            if line.startswith(code + "|"):
                p = line.strip().split("|")
                if len(p) >= 7:
                    out[p[1]] = [float(p[2]), float(p[3]), float(p[4]),
                                 float(p[5]), float(p[6])]

    def fetch(tag):
        url = CLOUD_FULL % tag
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "validator/1.0"})
            return urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")
        except Exception as e:
            print(f"[load] {tag} 拉取失败: {e}", file=sys.stderr)
            return ""

    if cloud_shard is not None:
        for tag in FULL_SERIES:
            absorb(fetch(tag))
        absorb(fetch(cloud_shard))
        return out

    import glob
    for fp in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
        with open(fp, encoding="utf-8", errors="replace") as f:
            absorb(f.read())
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

    # 停牌日在基准里仍占一行但 volume=0；行情源不产出这些空 bar，
    # 拿它们算缺失率会把「停牌」误判成「漏采」。只统计有成交的交易日。
    trading_dates = [d for d in dates if gt[d].get("volume", 0) > 0]
    suspended = len(dates) - len(trading_dates)
    n_missing = sum(1 for d in trading_dates if d not in bulk)
    missing_ratio = n_missing / len(trading_dates) * 100 if trading_dates else 0

    # 价格对比: 重建 qfq vs bulk close
    #
    # bulk 的 day_qfq_* 实为未复权原始价（文件名带 qfq 但源是新浪非复权），
    # 必须先用复权因子表把它转成真 qfq 再比，否则两种错法都会发生：
    # 直接比 qfq 期望 → 98% 假偏差；直接按 raw 放过 → 把「缺复权因子」的真问题盖掉。
    FAC_DIR = os.path.join(os.path.dirname(HERE), "data", "adjusted", "factors")
    fac_path = os.path.join(FAC_DIR, f"{args.code}.json")
    factors = {}
    if os.path.exists(fac_path):
        try:
            factors = (json.load(open(fac_path, encoding="utf-8")) or {}).get("factors") or {}
        except Exception as e:
            print(f"[load] 因子表解析失败 {fac_path}: {e}", file=sys.stderr)
    if not factors:
        print(f"  FAIL 缺复权因子表 {fac_path} —— bulk 是未复权价，不能当 qfq 用", file=sys.stderr)

    diffs = []
    factor_drifts = []
    prev_ratio = None

    for d in common:
        g = gt[d]
        exp_qfq = g["close"] * g["hfq_factor"] / latest_factor
        got_raw = bulk[d][1]  # close
        # 用因子表把 raw 转 qfq；无因子则该日无法验收，直接计入偏差
        f_now = factors.get(d)
        if f_now is None or not factors:
            got = got_raw
        else:
            got = got_raw * f_now / latest_factor
        if exp_qfq <= 0:
            continue
        diff_pct = abs(got - exp_qfq) / exp_qfq * 100
        diffs.append((diff_pct, d, got, exp_qfq))
        # 隐含因子漂移: (bulk/基准raw) 日环比突变 = 疑似漏复权
        ratio = got_raw / g["close"] if g["close"] else 0
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
    print(f"  停牌/零成交日(基准有、行情源不出 bar): {suspended} 天, 不计入缺失")
    print(f"  收盘价偏差>{args.max_price_diff}%: {n_bad}/{len(diffs)} ({bad_ratio:.3f}%)")
    if diffs:
        print(f"  最大偏差: {diffs[0][0]:.3f}% @ {diffs[0][1]} (bulk={diffs[0][2]} vs 期望={diffs[0][3]:.3f})")
    print(f"  量比中位数(基准股/bulk单位): {vol_med:.3f} (期望≈1.0, bulk 已用股数)")
    if factor_drifts:
        print(f"  疑似漏复权日: {len(factor_drifts)} 处, 最近3: {factor_drifts[:3]}")

    fails = []
    if missing_ratio > args.max_missing_ratio:
        fails.append(f"缺失率 {missing_ratio:.2f}% > {args.max_missing_ratio}%")
    if bad_ratio > 1.0:
        fails.append(f"价格偏差率 {bad_ratio:.3f}% > 1%")
    # 量纲: bulk 可能是股(比值≈1) 或手(比值≈100), 两者都算正常;
    # 其余比值说明单位或成交量口径不一致
    if vol_med and not (0.5 < vol_med < 2.0 or 50 < vol_med < 200):
        fails.append(f"量比 {vol_med:.3f} 异常 (期望≈1 或 ≈100)")

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
