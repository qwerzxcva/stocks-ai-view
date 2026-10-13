#!/usr/bin/env python3
"""
update_klines_recent.py — 增量补录最近 N 天K线 (修复本地K线滞后)

问题: 本地 bulk K线最后一天是 2026-10-09, 而实时快照已有 10-12,
      导致复盘/回测缺少最新数据, reflect_engine 无法验证。

方案: 对每只已有K线的股票, 从腾讯拉最近 N 根, 只把本地没有的日期追加进去。
      不动已有数据 (避免破坏历史), 只追加增量。

用法:
  python3 update_klines_recent.py --days 5            # 补最近5根的新日期
  python3 update_klines_recent.py --days 5 --limit 500
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}
from paths import BULK
OUT = os.path.join(BULK, "day_qfq_recent.csv")


def load_existing():
    """已有K线的 {code: last_date}"""
    last = {}
    files = sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv")))
    for p in files:
        if os.path.basename(p) == "day_qfq_recent.csv":
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 1:
                    continue
                d = parts[0].strip()
                if d > last.get(code, ""):
                    last[code] = d
    # recent 文件也算
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if parts:
                    d = parts[0].strip()
                    if d > last.get(code, ""):
                        last[code] = d
    return last


def fetch_recent(code, n=5):
    """拉最近 n 根, 返回 [[date,o,c,h,l,v], ...]"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
    for _ in range(2):
        try:
            b = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(b.decode("utf-8"))
            node = (d.get("data") or {}).get(code) or {}
            arr = node.get("qfqday") or node.get("day") or []
            rows = []
            for r in arr:
                if len(r) < 6:
                    continue
                try:
                    rows.append([r[0], float(r[1]), float(r[2]),
                                 float(r[3]), float(r[4]), float(r[5])])
                except ValueError:
                    continue
            return rows
        except Exception:
            time.sleep(0.4)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    last = load_existing()
    codes = sorted(last.keys())[:args.limit]
    print(f"[update] 已有K线 {len(last)} 只, 本次检查 {len(codes)} 只", flush=True)
    print(f"[update] 最新日期分布: "
          f"{ {d: sum(1 for v in last.values() if v==d) for d in sorted(set(last.values()))[-5:]} }",
          flush=True)

    appended = {}
    t0 = time.time()

    def work(code):
        rows = fetch_recent(code, args.days)
        if not rows:
            return code, []
        cutoff = last.get(code, "")
        new = [r for r in rows if r[0] > cutoff]
        return code, new

    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, c) for c in codes]
        for fut in cf.as_completed(futs):
            code, new = fut.result()
            done += 1
            if new:
                appended[code] = new
            if done % 200 == 0:
                print(f"  {done}/{len(codes)}  新增 {len(appended)} 只 "
                      f"({time.time()-t0:.0f}s)", flush=True)

    if not appended:
        print("无新增数据 (已是最新)")
        return 0

    # 追加写入 (格式与 extra 一致: code|date,o,c,h,l,v)
    n_rows = 0
    with open(OUT, "a", encoding="utf-8") as f:
        for code, rows in appended.items():
            for r in rows:
                f.write(f"{code}|{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")
                n_rows += 1

    # 新的日期分布
    new_dates = sorted({r[0] for rows in appended.values() for r in rows})
    print(f"\nDONE: {len(appended)} 只新增, {n_rows} 行 → {OUT}")
    print(f"  新增日期: {new_dates}")
    print(f"  耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
