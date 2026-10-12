#!/usr/bin/env python3
"""
fetch_klines_batch.py — 用腾讯接口批量补 K 线, 提升覆盖率 (低位池/算法依赖)

背景: 本地 bulk 只有 238/5344 只 (4.5%) 有K线, 导致低位池 0 只、
multi_v5 出 0 买入。nightly-kline 工作流未成功跑完。

策略:
  - 从 universe.csv 取全市场代码
  - 腾讯 web.ifzq.gtimg.cn fqkline 拉日K (qfq 前复权)
  - 并发 10 (遵循 a-share-data 技能规范: max_workers=8~12)
  - 每只独立异常捕获, 失败不阻断
  - 输出: /root/workspace/stocks-data-kline/data/bulk/day_qfq_extra.csv
          格式与现有 day_qfq_XX.csv 兼容: code|date,open,close,high,low,vol
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

UNIVERSE = "/root/workspace/stocks-data-kline/data/bulk/universe.csv"
OUT = "/root/workspace/stocks-data-kline/data/bulk/day_qfq_extra.csv"


def load_universe():
    out = []
    with open(UNIVERSE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            c = line.split("|")[0].strip()
            if c:
                out.append(c)
    return out


def load_have():
    """已覆盖的代码集合。"""
    have = set()
    base = os.path.dirname(UNIVERSE)
    for i in range(10):
        p = os.path.join(base, f"day_qfq_{i:02d}.csv")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                c = line.split(",")[0].split("|")[0].strip()
                if c and c[0].isalpha():
                    have.add(c)
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                c = line.split(",")[0].split("|")[0].strip()
                if c and c[0].isalpha():
                    have.add(c)
    return have


def fetch_one(code, count=120):
    """返回 (code, rows) ; rows = [[date,o,c,h,l,v], ...]"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{count},qfq")
    for attempt in range(2):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                          timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            node = (d.get("data") or {}).get(code) or {}
            arr = node.get("qfqday") or node.get("day") or []
            rows = []
            for r in arr:
                if len(r) < 6:
                    continue
                try:
                    rows.append([r[0], float(r[1]), float(r[2]),
                                 float(r[3]), float(r[4]), float(r[5])])
                except (ValueError, IndexError):
                    continue
            if rows:
                return code, rows
            return code, []
        except Exception:
            if attempt == 0:
                time.sleep(0.3)
    return code, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500,
                    help="最多补多少只 (默认1500, 约 3-4 分钟)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--count", type=int, default=120, help="每只取多少根日K")
    args = ap.parse_args()

    universe = load_universe()
    have = load_have()
    todo = [c for c in universe if c not in have][:args.limit]
    print(f"universe={len(universe)} 已覆盖={len(have)} 待补={len(todo)}", flush=True)

    if not todo:
        print("无需补充")
        return 0

    t0 = time.time()
    ok, fail, rows_out = 0, 0, {}
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c, args.count): c for c in todo}
        for fut in cf.as_completed(futs):
            code, rows = fut.result()
            done += 1
            if rows:
                rows_out[code] = rows
                ok += 1
            else:
                fail += 1
            if done % 200 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(todo)}  ok={ok} fail={fail}  "
                      f"{el:.0f}s  ({done/max(el,1):.1f}只/s)", flush=True)

    # 追加写入 (保持与现有格式兼容)
    with open(OUT, "a", encoding="utf-8") as f:
        for code, rows in rows_out.items():
            for r in rows:
                f.write(f"{code}|{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")

    el = time.time() - t0
    print(f"\nDONE: ok={ok} fail={fail} 耗时{el:.0f}s -> {OUT}")
    print(f"覆盖率: {(len(have)+ok)}/{len(universe)} "
          f"({(len(have)+ok)/len(universe)*100:.1f}%)")
    if fail:
        print(f"失败样例: {[futs[f] for f in list(futs)[:5] if f.done() and not f.result()[1]][:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
