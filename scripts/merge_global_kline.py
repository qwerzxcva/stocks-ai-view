#!/usr/bin/env python3
"""
merge_global_kline.py — 把 collect_global_kline.py 的 9 标的增量合并进
data/global_kline.json (global_kline.py 的格式, 该文件由 GitCode 侧维护,
不能改 global_kline.py 本身, 所以用独立合并器)。

背景:
  global_kline.py (GitCode 镜像, 6 标的) 产出 symbols[sym]={n,bars,last}
  collect_global_kline.py (本项目, 9 标的) 产出 klines[code]=[{date,o,c,h,l,v}]
  两者格式不同, 且后者多了 4 只美股龙头 (AAPL/NVDA/TSLA/MSFT) 与恒指。

合并策略: 按 (sym, date) 去重, 后者覆盖前者 (本项目源更新)。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# collect_global_kline 的 code → global_kline 的 sym (统一命名)
CODE_MAP = {
    "usAAPL.OQ": "usAAPL", "usNVDA.OQ": "usNVDA",
    "usTSLA.OQ": "usTSLA", "usMSFT.OQ": "usMSFT",
    "hkHSI": "hkHSI", "hkHSCEI": "hkHSCEI", "hkHSTECH": "hkHSTECH",
    "hk00700": "hk00700", "hk09988": "hk09988",
    "usIXIC.OQ": "usIXIC", "usNDX.OQ": "usNDX",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "global_kline.json"))
    ap.add_argument("--src", default=os.path.join(DATA_DIR, "global_kline_src.json"),
                    help="collect_global_kline.py 的原始产出")
    args = ap.parse_args()

    # 读现有 (global_kline.py 格式)
    existing = {}
    if os.path.exists(args.out):
        try:
            for sym, v in (json.load(open(args.out, encoding="utf-8")).get("symbols") or {}).items():
                existing[sym] = {b[0]: b for b in (v.get("bars") or [])}
        except Exception:
            existing = {}

    # 读新源
    added = 0
    if os.path.exists(args.src):
        try:
            src = json.load(open(args.src, encoding="utf-8"))
        except Exception:
            src = {}
        for code, rows in (src.get("klines") or {}).items():
            sym = CODE_MAP.get(code, code)
            bucket = existing.setdefault(sym, {})
            for r in rows:
                bar = [r["date"], r["open"], r["close"], r["high"], r["low"], r.get("vol", 0)]
                if r["date"] not in bucket:
                    added += 1
                bucket[r["date"]] = bar

    symbols = {}
    for sym, bars in existing.items():
        if not bars:
            continue
        arr = [bars[k] for k in sorted(bars)]
        last = arr[-1]
        prev = arr[-2] if len(arr) > 1 else last
        chg = ((last[2] / prev[2] - 1) * 100) if prev[2] else 0
        symbols[sym] = {
            "n": len(arr),
            "bars": arr,
            "last": {"date": last[0], "close": last[2], "chg_pct": round(chg, 2)},
        }

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "symbols": symbols,
        "meta": {
            "sources": ["global_kline.py (GitCode 镜像, 6 指数)",
                        "collect_global_kline.py (本项目, 9 标的含 4 美股龙头)"],
            "merger": "merge_global_kline.py (按 sym+date 去重, 新源覆盖)",
            "caveat": "腾讯 fqkline 单次上限 640 根; Research reference only",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print("[merge] 标的 %d 个, 新增 K 线 %d 根" % (len(symbols), added))
    for sym, v in sorted(symbols.items()):
        print("  %-12s %4d 根  %s..%s  最新%s %.2f%%" % (
            sym, v["n"], v["bars"][0][0], v["bars"][-1][0],
            v["last"]["date"], v["last"]["chg_pct"]))
    print("[merge] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
