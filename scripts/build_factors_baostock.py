#!/usr/bin/env python3
"""build_factors_baostock.py — 用 baostock 权威复权因子建全市场因子表。

为什么用 baostock 而不是东财事件链 (2026-10-19 实测):
  基准 CSV (tests/fixtures/sh600519_gt.csv, 用户提供的权威数据) 逐日对照:
    来源                 600519 终值因子   结构一致性
    基准 CSV              8.8816          -
    东财事件链             7.1360          ✗ 缺 2022-12-27 / 2023-12-20 两次
                                              特别分红 + 2006 股改漏记送股
    baostock 复权因子      7.6693          ✓ 31 个跳变日与基准 30 个几乎全同
                                              每个区间内比值完全恒定 (波动 0.0000%)
  即 baostock 因子链形状正确, 与基准只差股改口径的一个常数 (1.158),
  前复权归一化 (qfq = raw × f / f_latest) 后该常数抵消, 不影响指标。

数据源: baostock.query_adjust_factor (免费, 需 login, 单只 1-2s)
字段: backAdjustFactor = 后复权因子 (除权日当天切到新值)
产出: data/adjusted/factors/{pref}.json, 结构与 build_factors_from_gt 同构:
      {"code","source","n","n_events","factors":{date: factor}}

用法:
  python3 build_factors_baostock.py --codes 600519,002242
  python3 build_factors_baostock.py --shard 0 --n-shards 4 --sleep 0.15
  python3 build_factors_baostock.py --resume    # 跳过已建表的
"""
import argparse
import glob
import json
import os
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = os.path.join(os.path.dirname(HERE), "..", "stocks-data-kline",
                        "data", "bulk")

FACTORS_DIR = os.path.join(DATA_DIR, "adjusted", "factors")
_BULK_INDEX = None


def bulk_index():
    global _BULK_INDEX
    if _BULK_INDEX is not None:
        return _BULK_INDEX
    idx = {}
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                parts = line.strip().split("|")
                if len(parts) < 4:
                    continue
                try:
                    idx.setdefault(parts[0].strip(), set()).add(parts[1][:10])
                except (IndexError, ValueError):
                    continue
    _BULK_INDEX = idx
    return idx


def bs_code(bare):
    """裸码 -> baostock 代码 (sh./sz./bj.)。"""
    if bare[0] in "69":
        return "sh." + bare
    if bare[0] in "48" or bare[:2] in ("43", "83", "87", "88"):
        return "bj." + bare
    return "sz." + bare


def fetch_factors(bs, bare, retries=3):
    """返回 {date: backAdjustFactor} 或 None (网络失败)。"""
    code = bs_code(bare)
    for i in range(retries):
        try:
            rs = bs.query_adjust_factor(code=code,
                                        start_date="1990-01-01",
                                        end_date="2030-01-01")
            if rs.error_code != "0":
                return {}          # 无数据 (次新股/无分红), 不是失败
            out = {}
            while rs.next():
                r = rs.get_row_data()
                # [code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor]
                d, back = r[1], r[3]
                try:
                    out[d] = float(back)
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return None


def expand_to_dates(events, dates):
    """把除权日因子展开到每个交易日 (除权日当天切新值)。"""
    if not events or not dates:
        return {}
    keys = sorted(events)
    out = {}
    j = 0
    cur = None
    for d in sorted(dates):
        while j < len(keys) and keys[j] <= d:
            cur = events[keys[j]]
            j += 1
        if cur is None:
            # 首条事件之前: 用首条值 (归一化后无影响)
            cur = events[keys[0]]
        out[d] = cur
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--n-shards", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="跳过已有因子表的股票")
    args = ap.parse_args()
    os.makedirs(FACTORS_DIR, exist_ok=True)

    idx = bulk_index()
    all_bare = sorted({s[2:] if s[:2] in ("sh", "sz", "bj") else s
                       for s in idx if len(s) >= 6})
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        codes = [c for c in all_bare
                 if zlib.crc32(c.encode()) % args.n_shards == args.shard]
    if args.resume:
        codes = [c for c in codes
                 if not os.path.exists(os.path.join(
                     FACTORS_DIR, (bs_code(c).replace(".", "")) + ".json"))]
    if args.limit:
        codes = codes[:args.limit]

    print(f"[bs-factors] 待处理 {len(codes)} 只", flush=True)
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        print(f"[bs-factors] login 失败: {lg.error_msg}", file=sys.stderr)
        return 1

    ok = no_ev = fail = 0
    t0 = time.time()
    try:
        for i, bare in enumerate(codes):
            pref = bs_code(bare).replace(".", "")
            dates = idx.get(pref) or set()
            events = fetch_factors(bs, bare)
            if events is None:
                fail += 1
                continue
            if not events:
                no_ev += 1
                continue
            ftab = expand_to_dates(events, dates)
            if not ftab:
                fail += 1
                continue
            with open(os.path.join(FACTORS_DIR, f"{pref}.json"), "w",
                      encoding="utf-8") as f:
                json.dump({
                    "code": pref,
                    "source": "baostock.query_adjust_factor/backAdjustFactor",
                    "n": len(ftab),
                    "n_events": len(events),
                    "event_dates": sorted(events)[-10:],
                    "factors": ftab,
                }, f)
            ok += 1
            if (i + 1) % 100 == 0:
                el = time.time() - t0
                print(f"[bs-factors] {i+1}/{len(codes)} ok={ok} noev={no_ev} "
                      f"fail={fail} {el:.0f}s "
                      f"(eta {el/(i+1)*(len(codes)-i-1)/60:.0f}min)",
                      flush=True)
            time.sleep(args.sleep)
    finally:
        bs.logout()

    el = time.time() - t0
    manifest_path = os.path.join(FACTORS_DIR, "_manifest_baostock.json")
    prev = {}
    if os.path.exists(manifest_path):
        try:
            prev = json.load(open(manifest_path))
        except Exception:
            prev = {}
    total_tables = len(glob.glob(os.path.join(FACTORS_DIR, "*.json")))
    manifest = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "batch": {"n_codes": len(codes), "ok": ok, "no_events": no_ev,
                  "failed": fail, "elapsed_s": round(el, 1)},
        "total_factor_tables": total_tables,
        "universe": len(all_bare),
        "coverage_pct": round(total_tables / max(1, len(all_bare)) * 100, 2),
        "source": "baostock backAdjustFactor (权威, 与 GT 结构一致)",
        "previous_batches": prev.get("previous_batches", [])[-5:] + [{
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "ok": ok, "n": len(codes)}],
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[bs-factors] 完成 ok={ok} noev={no_ev} fail={fail} "
          f"总表数={total_tables}/{len(all_bare)} "
          f"覆盖 {manifest['coverage_pct']}% ({el:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())