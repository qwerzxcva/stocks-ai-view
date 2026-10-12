#!/usr/bin/env python3
"""Verify all pool data files in stocks-ai-view/data/ have meaningful content."""
import json, os, sys

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
files = ["cycle", "sector_pool", "lowpool", "hotspot_infer", "multi_v4"]

ok_all = True
for f in files:
    path = os.path.join(DATA, f + ".json")
    if not os.path.exists(path):
        print(f"  {f}: MISSING file")
        ok_all = False
        continue
    try:
        d = json.load(open(path, encoding="utf-8"))
        if f == "cycle":
            has = len(d.get("macro_signals", {})) > 0
        elif f == "sector_pool":
            has = d.get("tier1_count", 0) > 0
        elif f == "lowpool":
            has = d.get("total_filtered", 0) > 0
        elif f == "hotspot_infer":
            has = d.get("strong_count", 0) > 0
        else:  # multi_v4
            has = d.get("tier_buy_count", 0) > 0
        status = "OK" if has else "EMPTY"
        if not has:
            ok_all = False
        print(f"  {f}: {status} | {list(d.keys())[:5]}")
    except Exception as e:
        print(f"  {f}: ERROR {e}")
        ok_all = False

print(f"\nAll files: {'PASS' if ok_all else 'FAIL'}")
sys.exit(0 if ok_all else 1)
