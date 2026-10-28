#!/usr/bin/env python3
"""端到端验证: 按 CI 顺序跑一遍新增引擎适配步骤, 汇总退出码与产出。"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CMDS = [
    ("walkforward", ["scripts/walkforward.py", "--out", "data/walkforward.json"], "data/walkforward.json"),
    ("rps_rotation", ["scripts/rps_rotation.py", "--top", "30", "--out", "data/rps_rotation.json"], "data/rps_rotation.json"),
    ("market_phase", ["scripts/phase_runner.py", "--days", "90", "--out", "data/market_phase.json"], "data/market_phase.json"),
    ("patterns", ["scripts/patterns.py", "--out", "data/patterns_smoke.json"], "data/patterns_smoke.json"),
    ("factor_lib", ["scripts/factor_lib.py", "--out", "data/factor_smoke.json"], "data/factor_smoke.json"),
    ("cyq", ["scripts/cyq.py", "--out", "data/cyq_smoke.json"], "data/cyq_smoke.json"),
    ("predict_algo", ["scripts/predict_algo.py", "--local", "--out", "data/predict_algo.json"], "data/predict_algo.json"),
    ("pick_algo", ["scripts/pick_algo.py", "--top", "20", "--out", "data/pick_algo.json"], "data/pick_algo.json"),
    ("theme_to_stocks", ["scripts/theme_to_stocks.py", "--out", "data/theme_to_stocks.json"], "data/theme_to_stocks.json"),
    ("moneyflow", ["scripts/moneyflow_scan.py", "--out", "data/moneyflow/latest.json"], "data/moneyflow/latest.json"),
    ("global_kline", ["scripts/global_kline.py", "--out", "data/global_kline.json"], "data/global_kline.json"),
    ("global_panel", ["scripts/global_panel.py", "--out-dir", "data/global"], "data/global/latest.json"),
    ("strategy_card", ["scripts/strategy_card.py", "--out", "data/strategy_card.json"], "data/strategy_card.json"),
    # intraday_signal 已停用：停在 10-13、无任何 workflow 调度；
    # 盘中实时名单改由端上「开盘推股名单」承担（云端算不过来）
    ("engine_fusion", ["scripts/engine_fusion.py", "--out", "data/engine_fusion.json"], "data/engine_fusion.json"),
]

print(f"{'script':18s} {'rc':>3s}  {'json':6s} 内容")
print("-" * 78)
fails = []
for name, args, out in CMDS:
    t0 = time.time()
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, cwd=ROOT)
    dt = time.time() - t0
    exists = os.path.exists(os.path.join(ROOT, out))
    desc = ""
    if exists:
        try:
            d = json.load(open(os.path.join(ROOT, out), encoding="utf-8"))
            n = (d.get("n") or d.get("n_folds") or d.get("n_universe") or d.get("bars")
                 or len(d.get("flows", {})) or len(d.get("symbols", {}))
                 or len(d.get("candidates", [])) or len(d.get("top", []))
                 or len(d.get("themes", {})) or len(d.get("items", {})))
            desc = f"{n} 条"
        except Exception as e:
            desc = f"parse err: {e}"
    flag = "OK" if exists else "MISS"
    print(f"{name:18s} {r.returncode:3d}  {flag:6s} {desc}  ({dt:.0f}s)")
    if r.returncode != 0:
        fails.append((name, (r.stderr or r.stdout).strip().splitlines()[-1:]))

print()
print("FAILS:", fails if fails else "无 — 全部 rc=0")
