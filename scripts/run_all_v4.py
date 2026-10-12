#!/usr/bin/env python3
"""
run_all_v4.py — 多轮审核 v4 统一入口
在 stocks-ai-view/scripts/ 下运行，输出到 data/ 目录（网站直接读取）

流程:
  1. ai_screener.py    → latest.json (主数据，含涨停/热点/指数)
  2. hotspot_infer.py  → data/hotspot_infer.json
  3. pool_cycle.py     → data/cycle.json
  4. pool_sector.py    → data/sector_pool.json
  5. pool_lowpos.py    → data/lowpool.json
  6. multi_screener_v4.py → data/multi_v4.json
  7. 更新 latest.json 追加 multi_v4.summary 和 pools 字段供前端渲染

用法:
  cd stocks-ai-view/scripts && python3 run_all_v4.py [--date YYYY-MM-DD]
"""
import os, sys, json, subprocess, time, shutil
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")
TOOLS_DIR = os.path.join(REPO_ROOT, "..", "stocks", "tools-repo", "tools")

# Fallback paths (various project layouts)
for _alt in [
    os.path.join(REPO_ROOT, "..", "stocks-tools-repo", "tools"),
    os.path.join(REPO_ROOT, "..", "..", "stocks-tools-repo", "tools"),
    "/root/workspace/stocks/tools-repo/tools",
]:
    if os.path.isdir(_alt):
        TOOLS_DIR = _alt
        break

os.makedirs(DATA_DIR, exist_ok=True)


def run_script(name, script_path, args=None, timeout=180):
    """Run a Python script and return True on success."""
    if not os.path.exists(script_path):
        print(f"  SKIP {name}: {script_path} not found")
        return False
    cmd = [sys.executable, script_path]
    if args:
        cmd.extend(args)
    print(f"[{name}] Running ...", flush=True)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=os.path.dirname(script_path))
        elapsed = time.time() - t0
        # Print last few lines of output
        out_lines = r.stdout.strip().split("\n")
        for line in out_lines[-5:]:
            print(f"  {line}")
        if r.returncode != 0:
            err = r.stderr.strip().split("\n")[-3:]
            print(f"  [WARN] rc={r.returncode}: {'; '.join(err)}")
            return False
        print(f"  [OK] {elapsed:.1f}s", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] after {timeout}s", file=sys.stderr)
        return False


def update_latest_with_pools(date_str):
    """Read latest.json and append pool summaries for frontend.
    Pool data comes from stocks-data-live/data/pool/ (written by tools scripts)."""
    latest_path = os.path.join(DATA_DIR, "latest.json")
    if not os.path.exists(latest_path):
        print("  WARN: latest.json not found, skipping summary merge")
        return

    with open(latest_path, encoding="utf-8") as f:
        latest = json.load(f)

    # Try to find pool data from local data/ files first (exported by export_pools.py)
    # Fallback: read from stocks-data-live/data/pool/
    import glob
    live_pool = None
    for p in [os.path.join("..", "..", "stocks-data-live", "data", "pool"),
              "/root/workspace/stocks-data-live/data/pool"]:
        if os.path.isdir(p):
            live_pool = os.path.abspath(p)
            break

    pools = {}
    for name, fname in [("cycle", "cycle.json"), ("sector_pool", "sector_pool.json"),
                         ("lowpool", "lowpool.json"), ("hotspot_infer", "hotspot_infer.json"),
                         ("multi_v4", "multi_v4.json")]:
        # Prefer local data/ file
        local_path = os.path.join(DATA_DIR, fname)
        live_fname = None
        # Find in live pool dir (try v5 first, fallback to v4)
        if live_pool:
            prefix_list = {"cycle": ["cycle"], "sector_pool": ["sector_pool"],
                          "lowpool": ["lowpool"], "hotspot_infer": ["hotspot_infer"],
                          "multi_v4": ["_multi_v5", "_multi_v4"]}
            prefix = prefix_list.get(name, [name])
            found = None
            for p in prefix:
                files = sorted(glob.glob(os.path.join(live_pool, f"*{p}*.json")))
                files = [f for f in files if "2026" in f]
                if files:
                    found = files[-1]
                    break
            if found:
                live_fname = found

        source = local_path if os.path.exists(local_path) else live_fname
        if not source or not os.path.exists(source):
            continue

        try:
            with open(source, encoding="utf-8") as f:
                d = json.load(f)
            if name == "cycle":
                pools[name] = {
                    "market_phase": d.get("market_phase", "unknown"),
                    "macro_signals": d.get("macro_signals", {}),
                    "top_sectors": d.get("top_sectors", [])[:5],
                }
            elif name == "sector_pool":
                pools[name] = {
                    "tier1_count": d.get("tier1_count", 0),
                    "tier2_count": d.get("tier2_count", 0),
                    "tier1": d.get("tier1", [])[:10],
                    "market_phase": d.get("market_phase", ""),
                }
            elif name == "lowpool":
                pools[name] = {
                    "strong_buy": d.get("strong_buy", 0),
                    "watch": d.get("watch", 0),
                    "strong": d.get("strong", [])[:20],
                    "total_filtered": d.get("total_filtered", 0),
                }
            elif name == "hotspot_infer":
                pools[name] = {
                    "strong_count": d.get("strong_count", 0),
                    "watch_count": d.get("watch_count", 0),
                    "strong": d.get("strong", [])[:20],
                    "themes_processed": d.get("themes_processed", 0),
                    "by_theme": d.get("by_theme", {}),
                }
            elif name == "multi_v4":
                pools[name] = {
                    "tier_buy_count": d.get("tier_buy_count", 0),
                    "tier_watch_count": d.get("tier_watch_count", 0),
                    "tier_avoid_count": d.get("tier_avoid_count", 0),
                    "buy": d.get("buy", [])[:30],
                    "market_phase": d.get("market_phase", ""),
                }
        except Exception as e:
            print(f"  WARN: {name} merge failed: {e}", file=sys.stderr)

    latest["pools"] = pools
    latest["meta"] = {**(latest.get("meta") or {}), "algo": "tsp-v4-multi-pool", "ts_engine": datetime.now(CST).isoformat()}

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    print(f"  Updated latest.json with pools: {list(pools.keys())}")


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(CST).strftime("%Y-%m-%d")
    print(f"=== run_all_v4 {date_str} ===\n", flush=True)

    results = {}

    # Step 1: Core screener (涨停票 + 热点 + 指数)
    results["core"] = run_script("AI 选股引擎", os.path.join(SCRIPT_DIR, "ai_screener.py"),
                                  ["--out", os.path.join(DATA_DIR, "latest.json")])

    # Step 2: Cycle pool
    results["cycle"] = run_script("周期池", os.path.join(TOOLS_DIR, "pool_cycle.py"), [date_str])

    # Step 3: Sector pool
    results["sector"] = run_script("板块池", os.path.join(TOOLS_DIR, "pool_sector.py"), [date_str])

    # Step 4: Low position pool
    results["low"] = run_script("低位池", os.path.join(TOOLS_DIR, "pool_lowpos.py"), [date_str])

    # Step 5: Hotspot inference
    results["hotspot"] = run_script("热点推演", os.path.join(TOOLS_DIR, "hotspot_infer.py"), [date_str])

    # Step 6: Multi-screener v5 (quality-first with K-line priority)
    results["multi"] = run_script("综合扫描v5", os.path.join(TOOLS_DIR, "multi_screener_v5.py"), [date_str])

    # Step 6b: Postmortem (check 3-day-old picks)
    from datetime import timedelta as _td
    check_date = (datetime.now(CST) - _td(days=3)).strftime("%Y-%m-%d")
    pm_script = os.path.join(TOOLS_DIR, "postmortem.py")
    if os.path.exists(pm_script):
        results["postmortem"] = run_script("复盘(3天前)", pm_script, ["--days", "3", check_date], timeout=120)

    # Step 7: Merge pool summaries into latest.json (reads from live pool dir + local data/)
    update_latest_with_pools(date_str)

    # Also export to local data/ for static serving
    export_script = os.path.join(SCRIPT_DIR, "export_pools.py")
    if os.path.exists(export_script):
        run_script("导出池子数据", export_script, [])

    # Step 8: Slim latest.json (drop fields the frontend never reads, -33% bytes)
    slim_script = os.path.join(SCRIPT_DIR, "slim_latest.py")
    if os.path.exists(slim_script):
        run_script("瘦身 latest.json", slim_script, [])

    # Summary
    print(f"\n=== 完成 {date_str} ===")
    for k, v in results.items():
        print(f"  {k}: {'OK' if v else 'FAIL'}")

    if not any(results.values()):
        print("\n全部失败，退出码 1", file=sys.stderr)
        sys.exit(1)

    # Count available pools
    pool_files = ["cycle.json", "sector_pool.json", "lowpool.json", "hotspot_infer.json", "multi_v4.json"]
    ok = sum(1 for f in pool_files if os.path.exists(os.path.join(DATA_DIR, f)))
    print(f"\n数据文件: {ok}/{len(pool_files)} 就绪 -> data/ 目录")


if __name__ == "__main__":
    main()
