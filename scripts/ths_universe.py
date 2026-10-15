#!/usr/bin/env python3
# ths_universe.py — 构建同花顺采集的股票池
#
# 优先从 GitHub raw 拉取东财 clist 快照 (零请求同花顺), 失败则用内置核心样本。
# 只读远端, 不落地到本地仓库。
import argparse
import json
import os
import sys
import urllib.request

CORE = [
    "600519", "000001", "000063", "600036", "300750", "601318", "000858",
    "600030", "002594", "601899", "600900", "601166", "000002", "600887",
    "601988", "600276", "000725", "601012", "600309", "002415",
]


def _get(url, timeout=20, retries=2):
    for i in range(retries):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout)
            return r.read()
        except Exception:
            if i == retries - 1:
                return None
    return None


def build(limit=1500):
    """从 stocks-ai-view 的 shard 快照里抽全市场代码"""
    codes = set()
    # 尝试多个已知快照路径
    for url in [
        "https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/data/stock_names.json",
        "https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/data/shard_a.json",
    ]:
        b = _get(url)
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8", "replace"))
        except Exception:
            continue
        # 递归抽 6 位数字代码
        found = _extract_codes(d)
        if found:
            codes.update(found)
            print(f"  {url.split('/')[-1]}: +{len(found)}", file=sys.stderr)
        if len(codes) >= limit:
            break
    if not codes:
        codes.update(CORE)
    return sorted(codes)[:limit]


def _extract_codes(obj, depth=0):
    out = set()
    if depth > 6:
        return out
    if isinstance(obj, str):
        s = obj.strip()
        if len(s) == 6 and s.isdigit() and s[0] in "036":
            out.add(s)
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and len(k) == 6 and k.isdigit() and k[0] in "036":
                out.add(k)
            out |= _extract_codes(v, depth + 1)
        return out
    if isinstance(obj, list):
        for v in obj:
            out |= _extract_codes(v, depth + 1)
        return out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ths_universe.json")
    ap.add_argument("--limit", type=int, default=1500)
    args = ap.parse_args()
    codes = build(args.limit)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated": True, "n": len(codes), "codes": codes}, f)
    print(f"{len(codes)} codes -> {args.out}")
    print(",".join(codes[:20]), "...", file=sys.stderr)


if __name__ == "__main__":
    main()
