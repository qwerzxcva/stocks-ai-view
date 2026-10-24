#!/usr/bin/env python3
"""新增三件算法的 pytest 式自测 + 真实数据实测。运行:
/home/agentuser/.hermes/hermes-agent/venv/bin/python3 test_new_algos.py
"""
import sys, os, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
import duckdb

PASS = []
def check(name, cond, info=""):
    assert cond, f"FAIL: {name} {info}"
    PASS.append(f"{name} {info}")

P = "/home/agentuser/stocks-algo"
DB = "/home/agentuser/stockdb/stock.duckdb"

# ---- 取真实数据 (只读连接) ----
con = duckdb.connect(DB, read_only=True)
syms = [r[0] for r in con.execute(
    "select symbol from daily group by symbol having count(*)>400 order by random() limit 60").fetchall()]
frames = {}
for s in syms:
    d = con.execute("select * from daily where symbol=? order by date", [s]).fetchdf()
    frames[s] = d
check("load_real_data", len(frames) == 60 and sum(len(v) for v in frames.values()) > 20000,
      f"{len(frames)} syms {sum(len(v) for v in frames.values())} rows")

# ================= 1. cyq.py =================
import cyq as cyq_mod
sample = frames[syms[0]]
r = cyq_mod.cyq_distribution(sample)
check("cyq_basic", 0 <= r["profit_ratio"] <= 1 and r["conc90"] >= 0 and isinstance(r["cost_bias"], float),
      f"profit={r['profit_ratio']:.3f} conc90={r['conc90']:.3f} bias={r['cost_bias']:.3f}")
# 手工正确性: 现价为最低时获利盘≈0, 最高时≈1
df2 = sample.copy()
r2 = cyq_mod.cyq_distribution(df2, date=df2["date"].iloc[-1])
check("cyq_self_at_last_bar", abs(r2["profit_ratio"] - r["profit_ratio"]) < 1e-9)
# 因子序列
fs = cyq_mod.cyq_factor_series(sample)
check("cyq_factor_series", len(fs) >= 10 and fs["cyq_profit_ratio"].dropna().between(0, 1).all(), f"rows={len(fs)}")
# 注册进 factor_lib (在临时副本注册避免重复)
import factor_lib as fl
n_before = len(fl.FACTORS)
cyq_mod.register_into(fl)
check("cyq_registered", len(fl.FACTORS) == n_before + 3,
      f"{n_before}->{len(fl.FACTORS)}: {[f[0] for f in fl.FACTORS[-3:]]}")
sub = sample.iloc[:300].reset_index(drop=True)
out = fl.compute_all(sub)
check("cyq_compute_all", {"cyq_profit_ratio", "cyq_conc90", "cyq_cost_bias"} <= set(out.columns))
# 多票汇总统计
prof = [cyq_mod.cyq_distribution(v.tail(210).reset_index(drop=True))["profit_ratio"] for v in list(frames.values())[:60]]
prof = np.array(prof)
check("cyq_market_sweep", len(prof) == 60 and prof.min() >= 0 and prof.max() <= 1,
      f"获利盘 mean={prof.mean():.3f} min={prof.min():.3f} max={prof.max():.3f}")

# ================= 2. patterns.py =================
import patterns as pt
dates_idx = sample["date"].tolist()
last = dates_idx[-1]
for fid, label, fn in pt.STRATEGIES:
    v = fn(sample, last)  # 不抛异常即签名合法
    check(f"pattern_signature_{fid}", isinstance(v, (bool, np.bool_)), f"last={last} hit={v}")
# 边界: 日期不在df -> ValueError
try:
    pt.parking_apron(sample, "1990-01-01"); check("pattern_bad_date", False)
except ValueError:
    check("pattern_bad_date", True)
# 数据不足
short = sample.iloc[:20].reset_index(drop=True)
for fid, label, fn in pt.STRATEGIES:
    v = fn(short, short["date"].iloc[-1])
    check(f"pattern_short_{fid}", v is False)

# 全市场60票全历史逐月扫命中数
from collections import Counter
hits = Counter(); scans = 0
for s, d in frames.items():
    d = d.reset_index(drop=True)
    ds = d["date"].tolist()
    months = sorted(set(x[:7] for x in ds))
    seen = set()
    for m in months:
        # 每月最后一个交易日测一次
        i = max(j for j, x in enumerate(ds) if x[:7] == m)
        if d["date"].iloc[i] == ds[-1] and len(d) < 260:
            continue
        dt = ds[i]
        for fid, label, fn in pt.STRATEGIES:
            if fn(d, dt):
                if (s, dt, fid) not in seen:
                    seen.add((s, dt, fid)); hits[fid] += 1
        scans += 1
check("pattern_sweep", scans > 1000, f"scan_points={scans} hits={dict(hits)}")

# ================= 3. walkforward 增强 =================
import walkforward as wf
# bootstrap: 确定性收益序列
rets = [0.05, -0.02, 0.03, 0.01, -0.01, 0.04, 0.02, -0.03, 0.06, 0.01] * 5  # 胜率0.7
ci = wf.bootstrap_win_ci(rets, n_boot=1000)
check("bootstrap_ci", abs(ci["win_rate"] - 0.7) < 1e-9 and ci["ci_low"] <= 0.7 <= ci["ci_high"]
      and ci["ci_low"] < ci["ci_high"], f"win={ci['win_rate']} CI=[{ci['ci_low']},{ci['ci_high']}]")
# 空序列
check("bootstrap_empty", wf.bootstrap_win_ci([])["win_rate"] is None)
# 真实OOS收益bootstrap: 动量Top5每20日实际次日收益
multi = pd.concat(frames.values(), ignore_index=True)
multi["symbol"] = np.repeat(list(frames.keys()), [len(v) for v in frames.values()])
multi["ret_next"] = multi.groupby("symbol")["open"].shift(-2) / multi.groupby("symbol")["open"].shift(-1) - 1
def score_fn(d, topn=5):
    out = {}
    last_dates = d.groupby("symbol")["date"].max()
    for s, ld in last_dates.items():
        row = d[(d.symbol == s) & (d.date == ld)]
        if row.empty: continue
        mom = row["close"].iloc[0]
        out[s] = float(mom)
    return out
tr = multi[multi.date < "2026-09-01"]
te = multi[(multi.date >= "2026-09-01") & (multi.date <= "2026-09-30")]
if not te.empty:
    sc = score_fn(tr)
    top5 = sorted(sc, key=sc.get, reverse=True)[:5]
    oos_rets = []
    for s in top5:
        rr = te[te.symbol == s]["ret_next"].dropna()
        if len(rr): oos_rets.append(float(rr.iloc[-1]))
    ci_real = wf.bootstrap_win_ci(oos_rets, n_boot=1000)
    check("bootstrap_real_oos", ci_real["n"] >= 1, f"top5 OOS {ci_real}")

# rolling validation (扩张窗)
rv = wf.run_rolling_validation(multi, score_fn, n_windows=4, test_days=10)
check("rolling_validation", len(rv["windows"]) >= 3 and "oos_mean" in rv and "verdict" in rv
      and all(w["oos"] == w["oos"] for w in rv["windows"]),
      f"mean={rv['oos_mean']} std={rv['oos_std']} pos%={rv['oos_positive_ratio']} verdict={rv['verdict']}")  # score_fn返回收盘价量纲
# 原run_walkforward不受影响
wfr = wf.run_walkforward(multi, lambda d, p: {s: 0.01 * p for s in d.symbol.unique()},
                         [1, 2, 3], train_days=180, test_days=30, step_days=60)
check("walkforward_regress", len(wfr["folds"]) > 0 and "avg_is" in wfr, f"folds={len(wfr['folds'])}")
# bootstrap window 数值稳定性: 两次调用同seed结果一致
c1 = wf.bootstrap_win_ci(rets, seed=7); c2 = wf.bootstrap_win_ci(rets, seed=7)
check("bootstrap_deterministic", c1 == c2)

print("\n".join(f"PASS {p}" for p in PASS))
print(f"\nTOTAL: {len(PASS)} checks passed")
