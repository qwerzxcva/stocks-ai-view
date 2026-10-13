#!/usr/bin/env python3
"""
sync_data_layers.py — 数据分层同步 (小文件进 ai-view, 大文件进 data-live)

背景 (2026-10-13 用户要求):
  「本地不需要这些数据, 本地站的数据也是实时查询 GitHub/gitcode 的数据库」
  「数据都提交到 Git 上了吗?」

问题: data/adjusted/bulk/ 有 5268 只 × qfq+hfq = 10536 个 JSON (81MB),
      全塞进 ai-view 仓会让 clone/pages 部署很重; 而页面只需要读
      用户点开的那几只 → 应该按需 fetch。

分层:
  Layer 1 (热, 页面直接读, 进 stocks-ai-view/data/):
    latest/market_now/hot_impact/enrich/ai_picks/engine_v5/ban_pick/
    ban_prob/flow_divergence/cycle_pool_v2/index_kline/...  (< 500KB 每个)
  Layer 2 (温, 按需读, 进 stocks-data-live/data/research/):
    全市场复权 bulk, 集思录 1690 行, catalog, level2
  Layer 3 (原始快照, 只进 data-live):
    snapshot_*.json (1.5MB/天), all_sources.json (1.4MB)

GitHub raw 直读 (CORS *):
  https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/data/latest.json
  https://raw.githubusercontent.com/qwerzxcva/stocks-data-live/main/data/research/...

用法:
  python3 sync_data_layers.py --check     # 只看分层状态, 不推
  python3 sync_data_layers.py --push      # 分层推送 (ai-view + data-live)
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
AI_VIEW = "/root/workspace/stocks-ai-view"
DATA_LIVE = "/root/workspace/stocks-data-live"

# Layer 1: 页面消费的热文件 (在 ai-view/data/ 下)
LAYER1_FILES = [
    "latest.json", "market_now.json", "hot_impact.json", "enrich.json",
    "ai_picks.json", "engine_v5.json", "ban_pick.json", "ban_prob.json",
    "flow_divergence.json", "cycle_pool_v2.json", "cycle.json", "cycle_deep.json",
    "index_kline.json", "lowpool.json", "sector_pool.json", "multidim.json",
    "kline_sim.json", "reflect_latest.json", "daily_brief.json",
    "winrate_matrix.json", "decision.json", "hotspot_infer.json",
]

# Layer 1 里单文件大小上限 (超过就移到 Layer 2)
LAYER1_MAX_BYTES = 800 * 1024   # 800KB


def _run(cmd, cwd=None, check=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"[cmd-fail] {' '.join(cmd)}\n{r.stderr[:300]}", file=sys.stderr)
    return r


def check():
    print("=== Layer 1 (热, 进 ai-view) ===")
    l1_ok, l1_missing, l1_too_big = [], [], []
    for f in LAYER1_FILES:
        p = os.path.join(AI_VIEW, "data", f)
        if not os.path.exists(p):
            l1_missing.append(f)
            continue
        sz = os.path.getsize(p)
        if sz > LAYER1_MAX_BYTES:
            l1_too_big.append((f, sz))
        else:
            l1_ok.append((f, sz))
    for f, sz in l1_ok:
        print(f"  ✅ {f:28s} {sz/1024:>8.0f}KB")
    for f, sz in l1_too_big:
        print(f"  ⚠️ {f:28s} {sz/1024:>8.0f}KB (> {LAYER1_MAX_BYTES//1024}KB, 应移 Layer 2)")
    for f in l1_missing:
        print(f"  ❌ {f:28s} 不存在")

    print("\n=== Layer 2 (温, 进 data-live) ===")
    bulk = os.path.join(AI_VIEW, "data", "adjusted", "bulk")
    if os.path.isdir(bulk):
        files = [f for f in os.listdir(bulk) if f.endswith(".json")]
        total = sum(os.path.getsize(os.path.join(bulk, f)) for f in files)
        print(f"  bulk 复权: {len(files)} 个文件, {total/1024/1024:.1f}MB")
    research = os.path.join(DATA_LIVE, "data", "research")
    if os.path.isdir(research):
        files = [f for f in os.listdir(research) if f.endswith(".json")]
        total = sum(os.path.getsize(os.path.join(research, f)) for f in files)
        print(f"  research:  {len(files)} 个文件, {total/1024/1024:.1f}MB")

    print("\n=== git 状态 ===")
    r = _run(["git", "status", "--short", "data/"], cwd=AI_VIEW)
    print(r.stdout[:600] or "  (ai-view data/ 干净)")
    r2 = _run(["git", "status", "--short", "data/"], cwd=DATA_LIVE)
    print(r2.stdout[:600] or "  (data-live data/ 干净)")
    return 0


def push():
    print("=== 分层推送 ===")
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    # 1 ai-view: Layer 1 热文件
    r = _run(["git", "add"] + [f"data/{f}" for f in LAYER1_FILES], cwd=AI_VIEW)
    r = _run(["git", "diff", "--cached", "--quiet"], cwd=AI_VIEW)
    if r.returncode != 0:
        _run(["git", "commit", "-m", f"chore(data): layer-1 hot data {ts}"], cwd=AI_VIEW)
        print("  [ai-view] Layer 1 已提交")
    else:
        print("  [ai-view] Layer 1 无变化")

    # 2 data-live: bulk 复权 + research
    _run(["git", "add", "data/"], cwd=DATA_LIVE)
    r = _run(["git", "diff", "--cached", "--quiet"], cwd=DATA_LIVE)
    if r.returncode != 0:
        _run(["git", "commit", "-m", f"chore(data): layer-2 bulk + research {ts}"],
             cwd=DATA_LIVE)
        print("  [data-live] Layer 2 已提交")
    else:
        print("  [data-live] Layer 2 无变化")

    for name, path in [("ai-view", AI_VIEW), ("data-live", DATA_LIVE)]:
        r = _run(["git", "pull", "--rebase", "origin", "main"], cwd=path)
        r = _run(["git", "push", "origin", "main"], cwd=path)
        status = "✅" if r.returncode == 0 else "❌"
        print(f"  [{name}] push {status}")
        if r.returncode != 0:
            print(f"    {r.stderr[:200]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    if args.push:
        return push()
    return check()


if __name__ == "__main__":
    sys.exit(main())
