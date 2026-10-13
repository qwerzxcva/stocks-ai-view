#!/usr/bin/env python3
"""
slim_predictions.py - 瘦身预测快照 (107M -> ~10M)

问题: 649 个快照 x 平均 162KB = 107M, GitHub 仓膨胀。
      每个快照含 top100 只股票的完整 votes (17指标 x why 字符串),
      但 reflect_engine 只用 code/date/score/resonance/bull/bear + indicator->vote。

方案: votes 从 list[{indicator,vote,strength,why},...] 瘦成 dict{indicator: vote}
用法: python3 slim_predictions.py --dir data/predictions [--dry-run]
"""
import argparse
import json
import os
import sys

KEEP_TOP = ("code", "name", "price", "date", "score", "resonance",
            "bull", "bear", "bars", "ts")


def slim_one(path, dry_run=False):
    d = json.load(open(path, encoding="utf-8"))
    top = d.get("top") or []
    new_top = []
    for r in top:
        nr = {k: r.get(k) for k in KEEP_TOP if k in r}
        votes = r.get("votes")
        if isinstance(votes, list):
            nr["votes"] = {v.get("indicator"): v.get("vote") for v in votes
                           if isinstance(v, dict) and v.get("indicator")}
        elif isinstance(votes, dict):
            nr["votes"] = votes
        new_top.append(nr)
    d["top"] = new_top
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.dir) if f.endswith(".json"))
    total_before = sum(os.path.getsize(os.path.join(args.dir, f)) for f in files)
    print("[slim] %d snapshots, before %.1fM" % (len(files), total_before / 1e6))
    total_after = 0
    for i, f in enumerate(files):
        sz = slim_one(os.path.join(args.dir, f), args.dry_run)
        total_after += sz
        if (i + 1) % 100 == 0:
            print("  %d/%d ..." % (i + 1, len(files)))
    if args.dry_run:
        print("[slim] (dry-run) after ~%.1fM" % (total_after / 1e6))
    else:
        print("[slim] after %.1fM (-%.0f%%)" % (
            total_after / 1e6, (total_before - total_after) / total_before * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
