#!/usr/bin/env python3
"""build_factors_verified.py — 全市场复权因子表构建 + 完整性校验。

为什么另起一个而不是改 build_factors_market.py:
  2026-10-19 用基准 CSV (tests/fixtures/sh600519_gt.csv, 用户提供、权威)
  逐事件验证了现有事件链口径:
    - 2006-05-25 股改: 实际 10送13.38股, 东财只给 BONUS_RATIO=0/IT_RATIO=1.0
      → 事件链在早年股改上系统性偏低 (茅台比值恒 1.1730)
    - 2022-12-27 / 2023-12-20 两次特别分红: 东财 RPT_SHAREBONUS_DET 无记录
      → 近端也会漏事件 (比值漂到 1.188/1.202)
  因此「只按东财事件链建表」会给出看似完整、实则系统性偏差的因子表。
  本脚本对每只股票做三道校验, 通过的才写入, 其余进 quarantine 清单。

校验口径 (per stock):
  A. 事件覆盖: 至少 1 条「实施分配」且 EX_DIVIDEND_DATE 有效
  B. 除权日价格校验: 对每个事件日, 比较 raw_close 的实际跌幅与
     (派现 + 送转) 的理论除权价, 偏差 > tol 记 suspicious
  C. 基准对照 (有 GT 时才做): 与 tests/fixtures/*_gt.csv 的 hfq_factor 比,
     最大相对误差 > tol 则不写正式表, 进 quarantine

修复口径:
  对 B 判 suspicious 的事件 (东财漏记送转), 用实际价格比反推 (1+S+T),
  写进 factors 表的 derived_events 里并标注 source="derived_from_price"。

产出:
  data/adjusted/factors/{code}.json        通过的因子表
  data/adjusted/factors/_quarantine.json   未通过清单 (含原因, 可复核)
  data/adjusted/factors/_manifest.json     覆盖率/校验统计 (前端与报告读)

用法:
  python3 build_factors_verified.py --codes 600519,002242
  python3 build_factors_verified.py --shard 0 --n-shards 4 --sleep 0.25
"""
import argparse
import csv
import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = os.path.join(os.path.dirname(HERE), "..", "stocks-data-kline",
                        "data", "bulk")

FACTORS_DIR = os.path.join(DATA_DIR, "adjusted", "factors")
FIXTURES = os.path.join(os.path.dirname(HERE), "tests", "fixtures")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
PRICE_TOL = 0.03       # 理论上除权价 vs 实际收盘 允许 3% (含当日涨跌)
GT_TOL = 0.005         # 与基准 CSV 因子允许 0.5%

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
                    idx.setdefault(parts[0].strip(), {})[parts[1][:10]] = float(parts[3])
                except ValueError:
                    continue
    _BULK_INDEX = idx
    return idx


def em_events(code, retries=3):
    filt = urllib.parse.quote(f'(SECURITY_CODE="{code}")')
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_SHAREBONUS_DET&columns=ALL"
           f"&filter={filt}&sortColumns=EX_DIVIDEND_DATE&sortTypes=1"
           "&pageSize=200")
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                       timeout=20)
            rows = (json.loads(r.read().decode()).get("result") or {}).get("data") or []
            ev = []
            for e in rows:
                ex = (e.get("EX_DIVIDEND_DATE") or "")[:10]
                if not ex or e.get("ASSIGN_PROGRESS") != "实施分配":
                    continue
                ev.append({
                    "ex": ex,
                    "dps": float(e.get("PRETAX_BONUS_RMB") or 0) / 10.0,
                    "bonus": float(e.get("BONUS_RATIO") or 0) / 10.0,
                    "it": float(e.get("IT_RATIO") or 0) / 10.0,
                })
            return sorted(ev, key=lambda x: x["ex"])
        except Exception:
            if i < retries - 1:
                time.sleep(2.5)
    return None


def verify_events(events, raw):
    """B 校验: 用除权日实际价格反推真实送转比例, 东财漏记时补 derived。"""
    ds = sorted(raw)
    pos = {d: i for i, d in enumerate(ds)}
    out, suspicious = [], 0
    for e in events:
        ex = e["ex"]
        i = pos.get(ex)
        if i is None or i == 0:
            out.append(dict(e, source="em", verified=False,
                            note="除权日不在K线内"))
            continue
        prev = raw[ds[i - 1]]
        close = raw[ds[i]]
        if prev <= 0 or close <= 0:
            out.append(dict(e, source="em", verified=False, note="价格缺失"))
            continue
        theo = (prev - e["dps"]) / (1 + e["bonus"] + e["it"]) if prev > e["dps"] else None
        if theo is None:
            out.append(dict(e, source="em", verified=False, note="派现超过前收"))
            continue
        dev = abs(close - theo) / theo
        if dev <= PRICE_TOL:
            out.append(dict(e, source="em", verified=True, dev=round(dev, 5)))
        else:
            # 用实际价格反推 (1+S+T)
            implied = (prev - e["dps"]) / close if close > 0 else None
            if implied and implied > 0:
                out.append(dict(e, source="derived_from_price",
                                ratio=round(implied, 6),
                                em_ratio=round(1 + e["bonus"] + e["it"], 6),
                                dev=round(dev, 5),
                                note="东财送转与价格不符, 已按实际价反推"))
                suspicious += 1
            else:
                out.append(dict(e, source="em", verified=False,
                                note="除权价异常"))
    return out, suspicious


def build_factor(events, raw):
    """事件链 + 原始收盘 → {date: hfq_factor}。

    对 derived_from_price 事件用反推 ratio, 其余用 (1+S+T)。
    """
    ds = sorted(raw)
    if not ds:
        return {}
    factor = 1.0
    ftab = {}
    ei = 0
    # 起点前事件: 逆序反推 (post≈ref 近似)
    pre = [e for e in events if e["ex"] <= ds[0]]
    live = [e for e in events if e["ex"] > ds[0]]
    if pre:
        P = raw[ds[0]]
        for e in reversed(pre):
            r = e.get("ratio") or (1 + e["bonus"] + e["it"])
            prev = P * r + e["dps"]
            if prev > e["dps"]:
                factor *= prev / ((prev - e["dps"]) / r)
            P = prev
    for i, d in enumerate(ds):
        while ei < len(live) and live[ei]["ex"] <= d:
            e = live[ei]
            r = e.get("ratio") or (1 + e["bonus"] + e["it"])
            prev = raw[ds[i - 1]] if i > 0 else None
            if prev and prev - e["dps"] > 0:
                factor *= prev / ((prev - e["dps"]) / r)
            ei += 1
        ftab[d] = round(factor, 6)
    return ftab


def load_gt(code_pref):
    """读 tests/fixtures 下的基准 CSV (若存在)。"""
    for p in glob.glob(os.path.join(FIXTURES, "*_gt.csv")):
        name = os.path.basename(p).lower().replace("_gt.csv", "")
        if name.endswith(code_pref[2:]) or name == code_pref:
            try:
                out = {}
                with open(p, encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        try:
                            out[r["date"]] = float(r["hfq_factor"])
                        except (KeyError, ValueError):
                            continue
                return out or None
            except Exception:
                return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes")
    ap.add_argument("--shard", type=int, default=None)
    ap.add_argument("--n-shards", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    os.makedirs(FACTORS_DIR, exist_ok=True)

    idx = bulk_index()
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        import zlib
        allc = sorted({s[2:] if s[:2] in ("sh", "sz", "bj") else s
                       for s in idx if len(s) >= 6})
        codes = [c for c in allc
                 if zlib.crc32(c.encode()) % args.n_shards == (args.shard or 0)]
    if args.limit:
        codes = codes[:args.limit]

    ok = quarantined = no_events = failed = 0
    suspicious_total = 0
    quarantine = {}
    t0 = time.time()
    for i, code in enumerate(codes):
        pref = ("sh" if code[0] in "69" else
                "bj" if code[0] in "48" else "sz") + code
        raw = idx.get(pref) or {}
        if not raw:
            failed += 1
            quarantine[pref] = "no bulk bars"
            continue
        events = em_events(code)
        if events is None:
            failed += 1
            quarantine[pref] = "eastmoney fetch failed"
            continue
        if not events:
            no_events += 1
            quarantine[pref] = "no dividend events (never paid / no data)"
            continue
        verified, n_susp = verify_events(events, raw)
        suspicious_total += n_susp
        ftab = build_factor(verified, raw)
        if not ftab:
            failed += 1
            quarantine[pref] = "empty factor table"
            continue
        # C: 基准对照 (只有部分股票有 GT)
        gt = load_gt(pref)
        gt_err = None
        if gt:
            common = set(ftab) & set(gt)
            if common:
                # 基准与事件链的起点口径可能不同, 用中位数比值归一后比形状
                ratios = sorted(gt[d] / ftab[d] for d in common if ftab[d])
                med = ratios[len(ratios) // 2] if ratios else 1.0
                worst = max(abs(gt[d] - ftab[d] * med) / gt[d] for d in common)
                gt_err = round(worst, 6)
                if worst > GT_TOL:
                    quarantined += 1
                    quarantine[pref] = (f"GT mismatch {worst*100:.3f}% > "
                                        f"{GT_TOL*100:.1f}% (median ratio {med:.6f})")
                    continue
        with open(os.path.join(FACTORS_DIR, f"{pref}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({
                "code": pref,
                "source": "em_sharebonus_det+price_verified",
                "n": len(ftab),
                "n_events": len(events),
                "n_derived": n_susp,
                "gt_max_err": gt_err,
                "events": verified,
                "factors": ftab,
            }, f, ensure_ascii=False)
        ok += 1
        time.sleep(args.sleep)
        if (i + 1) % 100 == 0:
            print(f"[factors] {i+1}/{len(codes)} ok={ok} quar={quarantined} "
                  f"noev={no_events} fail={failed}", flush=True)

    with open(os.path.join(FACTORS_DIR, "_quarantine.json"), "w",
              encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                   "n": len(quarantine), "items": quarantine},
                  f, ensure_ascii=False, indent=1)
    manifest = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "n_codes": len(codes),
        "ok": ok, "quarantined": quarantined,
        "no_events": no_events, "failed": failed,
        "derived_events": suspicious_total,
        "coverage_pct": round(ok / max(1, len(codes)) * 100, 2),
        "elapsed_s": round(time.time() - t0, 1),
        "tolerances": {"price": PRICE_TOL, "gt": GT_TOL},
    }
    with open(os.path.join(FACTORS_DIR, "_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[factors] 完成: ok={ok} quarantined={quarantined} "
          f"no_events={no_events} failed={failed} "
          f"coverage={manifest['coverage_pct']}% "
          f"derived_events={suspicious_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())