#!/usr/bin/env python3
"""
导出池子数据到 stocks-ai-view/data/ 供网站渲染
运行：python3 stocks-ai-view/scripts/export_pools.py

把以下 JSON 导出到 data/ 目录（网站 index.html 读取）：
  - cycle.json         周期池
  - sector_pool.json   板块池
  - lowpool.json       低位池
  - hotspot_infer.json 热点推演
  - multi_v4.json      综合扫描
"""
import json, os, glob, sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
AI_VIEW = os.path.dirname(HERE)
DATA_OUT = os.path.join(AI_VIEW, "data")

# 读取所有池子文件（取最新的）
POOL_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "stocks-data-live", "data", "pool"),
    os.path.join(AI_VIEW, "..", "stocks-data-live", "data", "pool"),
]

# 也尝试相对路径（容器内）
import subprocess
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in [os.path.join(root, "stocks-data-live", "data", "pool"),
          os.path.join("/root/workspace", "stocks-data-live", "data", "pool")]:
    if os.path.isdir(p):
        POOL_DIR_CANDIDATES.append(p)

pool_dir = None
for p in POOL_DIR_CANDIDATES:
    if os.path.isdir(p):
        pool_dir = p
        break

if not pool_dir:
    print("ERROR: pool dir not found", file=sys.stderr)
    sys.exit(1)

os.makedirs(DATA_OUT, exist_ok=True)

EXPORTS = {
    "cycle.json":         "cycle",
    "sector_pool.json":   "sector_pool",
    "lowpool.json":       "lowpool",
    "hotspot_infer.json": "hotspot_infer",
    "multi_v4.json":      ["_multi_v5", "_multi_v4"],
}

for out_name, prefix in EXPORTS.items():
    # prefix can be str or list[str] (try in order, first match with data wins)
    if isinstance(prefix, str):
        prefix_list = [prefix]
    else:
        prefix_list = prefix
    exported = False
    for p in prefix_list:
        files = sorted(glob.glob(os.path.join(pool_dir, f"*{p}*.json")))
        files = [f for f in files if "2026" in f]
        for f in reversed(files):  # latest first
            try:
                d = json.load(open(f, encoding="utf-8"))
                # multi_v4/v5: check has data
                if p in ("_multi_v5", "_multi_v4"):
                    if d.get("tier_buy_count", 0) > 0 or d.get("candidates", 0) > 0:
                        out_path = os.path.join(DATA_OUT, out_name)
                        with open(out_path, "w", encoding="utf-8") as out:
                            json.dump(d, out, ensure_ascii=False, indent=2)
                        print(f"  exported {os.path.basename(f)} -> {out_name}")
                        exported = True
                        break
                else:
                    out_path = os.path.join(DATA_OUT, out_name)
                    with open(out_path, "w", encoding="utf-8") as out:
                        json.dump(d, out, ensure_ascii=False, indent=2)
                    print(f"  exported {os.path.basename(f)} -> {out_name}")
                    exported = True
                    break
            except Exception as e:
                print(f"  ERROR {p}: {e}", file=sys.stderr)
                continue
        if exported:
            break
    if not exported:
        print(f"  SKIP: no valid file found for {prefix_list}", file=sys.stderr)

print("Done. data/ now has:", os.listdir(DATA_OUT))
