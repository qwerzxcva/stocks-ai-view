#!/usr/bin/env python3
"""sync_mirror_shards.py — 把 B/C 镜像仓的分片数据拉回 A 仓。

背景 (2026-10-17): 三账号分片采集里, B 仓 (jiegengxiangjianxue) 跑
shard b (东财 push2 实时系), C 仓 (dufuwuxin) 跑 shard c (交易所/同花顺),
数据各自落在自己仓的 data/shard_<x>.json。A 仓 (本仓) 此前没有任何通道
消费 B/C 的分片数据 —— 采集了也用不上。本脚本就是那个汇聚通道。

机制: raw.githubusercontent 拉取 (公开仓, 零鉴权, 无 CORS 问题),
与本地比对, 变了才写; 提交由调用方 workflow 负责。

用法: python3 sync_mirror_shards.py [--dry-run]
退出码: 0=成功(无论是否有更新), 1=全部源失败。
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MIRRORS = {
    # jiegengxiangjianxue10 was suspended; its mirror was taken over by
    # jiegengxiangjianxue, so the raw host must follow the same move.
    "b": "https://raw.githubusercontent.com/jiegengxiangjianxue/"
         "stocks-ai-view-b/main/data/shard_b.json",
    "c": "https://raw.githubusercontent.com/dufuwuxin/"
         "stocks-ai-view-c/main/data/shard_c.json",
}
UA = {"User-Agent": "stocks-ai-view-sync/1.0"}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now(CST)

    n_ok = n_changed = n_fail = 0
    for shard, url in MIRRORS.items():
        dest = os.path.join(DATA_DIR, f"shard_{shard}.json")
        try:
            remote = fetch(url)
        except Exception as e:
            print(f"[sync] shard {shard}: 拉取失败 {e}", file=sys.stderr)
            n_fail += 1
            continue
        n_ok += 1
        r_ts = (remote.get("shard_meta") or {}).get("generated") or remote.get("ts")
        local = {}
        if os.path.exists(dest):
            try:
                local = json.load(open(dest, encoding="utf-8"))
            except Exception:
                local = {}
        l_ts = (local.get("shard_meta") or {}).get("generated") or local.get("ts")
        if local and r_ts == l_ts and r_ts is not None:
            print(f"[sync] shard {shard}: 已是最新 ({r_ts})")
            continue
        if local and r_ts is None and l_ts is None:
            # 两边都无时间戳: 比内容, 防止每次空跑都写盘触发提交
            def _strip(d):
                d = {k: v for k, v in d.items()
                     if k not in ("synced_from", "synced_at")}
                return json.dumps(d, sort_keys=True, ensure_ascii=False)
            if _strip(local) == _strip(remote):
                print(f"[sync] shard {shard}: 内容一致 (无时间戳)")
                continue
        # 防回退: 远端时间戳更旧时不覆盖 (镜像停摆时不会把旧数据当新数据)
        if local and r_ts and l_ts and str(r_ts) < str(l_ts):
            print(f"[sync] shard {shard}: 远端更旧 ({r_ts} < {l_ts}), 跳过")
            continue
        if args.dry_run:
            print(f"[sync] shard {shard}: would update {l_ts} -> {r_ts}")
            n_changed += 1
            continue
        remote["synced_from"] = url.rsplit("/data/", 1)[0]
        remote["synced_at"] = now.isoformat(timespec="seconds")
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(remote, f, ensure_ascii=False)
        os.replace(tmp, dest)
        n_changed += 1
        print(f"[sync] shard {shard}: 更新 {l_ts} -> {r_ts}")

    print(f"[sync] 完成: {n_ok} 源成功, {n_changed} 个更新, {n_fail} 个失败")
    return 0 if n_ok else 1


if __name__ == "__main__":
    sys.exit(main())
