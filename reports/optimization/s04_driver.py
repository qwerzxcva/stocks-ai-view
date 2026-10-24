#!/usr/bin/env python3
"""S4 expanded-market backtest driver (T4). Direct reimplementation of the S4
breakout signal, event-level, no-lookahead. Read-only wrt strategies/engines;
writes results to /tmp/s04_expanded_results.json."""
import os, sys, time, math, json
from collections import defaultdict, deque

BASE = os.path.expanduser("~/workspace/stocks-ai-view")
STRAT = os.path.join(BASE, "scripts/engines/strategies")
ENG = os.path.normpath(os.path.join(STRAT, ".."))
sys.path.insert(0, STRAT); sys.path.insert(0, ENG)
IDX = os.path.expanduser("~/workspace/stocks-data-kline/data/index/history")
BULK = os.path.join(BASE, "data", "bulk")

ALL_SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "20", "30", "68"]
MAIN_PREFIXES = ("sh600", "sh601", "sh605", "sz000", "sz001", "sz002", "sz003")

def is_main(code):
    return code.startswith(MAIN_PREFIXES)
H = 10
PARAM_SETS = [
    {"name": "baseline",  "amp": 2.5, "vr": 3.0, "lo": 3.0, "hi": 6.0},
    {"name": "amp2.0",    "amp": 2.0, "vr": 3.0, "lo": 3.0, "hi": 6.0},
    {"name": "amp3.0",    "amp": 3.0, "vr": 3.0, "lo": 3.0, "hi": 6.0},
    {"name": "vr2.5",     "amp": 2.5, "vr": 2.5, "lo": 3.0, "hi": 6.0},
    {"name": "vr3.5",     "amp": 2.5, "vr": 3.5, "lo": 3.0, "hi": 6.0},
    {"name": "chg2.5-5",  "amp": 2.5, "vr": 3.0, "lo": 2.5, "hi": 5.0},
    {"name": "chg4-8",    "amp": 2.5, "vr": 3.0, "lo": 4.0, "hi": 8.0},
]
t_all = time.time()

# ---- per-shard global trading-day index (all codes incl index rows; fixed prefix-sum) ----
days = defaultdict(set)
print("[stage] scan shards for trade days", flush=True)
for sh in ALL_SHARDS:
    with open(os.path.join(BULK, f"day_qfq_{sh}.csv"), encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line[0] == '#' or not line.startswith(MAIN_PREFIXES):
                continue
            days[sh].add(line[9:19])
TDAYS = {sh: sorted(v) for sh, v in days.items()}
TPOS = {sh: {d: i for i, d in enumerate(ds)} for sh, ds in TDAYS.items()}
print("[stage] trade days: " + ", ".join(f"{sh}:{len(ds)}" for sh, ds in sorted(TDAYS.items())), flush=True)

# ---- index series ----
def read_idx(sym):
    bars = []
    with open(os.path.join(IDX, f"{sym}.txt"), encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not line.startswith(sym):
                continue
            p = line.strip().split("|")
            bars.append((p[1], float(p[3])))
    return bars

import regime_router as rr
b1 = read_idx("sh000001")
bars1 = [{"date": d, "open": c, "close": c, "high": c, "low": c, "vol": 0.0} for d, c in b1]
regime = {}
for i in range(len(bars1)):
    rg, *_ = rr.judge_regime(bars1, i)
    regime[bars1[i]["date"]] = rg
print(f"[stage] regimes: {len(regime)} days", flush=True)
b3 = read_idx("sh000300")
b3pos = {d: i for i, (d, c) in enumerate(b3)}

def bench_ret(d, h):
    j = b3pos.get(d)
    if j is None or j + h >= len(b3):
        return None
    return (b3[j + h][1] / b3[j][1] - 1) * 100

per_cfg = {(p["name"], h): [] for p in PARAM_SETS for h in (5, 10)}
maxd = ""
for sh in ALL_SHARDS:
    ds = TDAYS[sh]; dpos = TPOS[sh]; nd = len(ds)
    t0 = time.time()
    nstock = 0
    cur_code = None
    buf = []

    def process(code, rows):
        n = len(rows)
        if n < 80:
            return
        c = [r[2] for r in rows]; hh = [r[3] for r in rows]
        ll = [r[4] for r in rows]; v = [r[5] for r in rows]
        dates = [r[0] for r in rows]
        rowd = {dates[i]: i for i in range(n)}
        tpos = [dpos[d] for d in dates]
        chg = [None] * n; vr = [None] * n; amp5 = [None] * n; plat = [None] * n
        a5 = 0.0
        dq = deque()
        for i in range(n):
            if i >= 1 and c[i - 1] > 0:
                chg[i] = (c[i] / c[i - 1] - 1) * 100
                a5 += (hh[i] - ll[i]) / c[i - 1] * 100
                if i > 5 and c[i - 6] > 0:
                    a5 -= (hh[i - 5] - ll[i - 5]) / c[i - 6] * 100
                if i >= 4:
                    amp5[i] = a5 / 5.0
            if i >= 5:
                base5 = (v[i - 5] + v[i - 4] + v[i - 3] + v[i - 2] + v[i - 1]) / 5
                if base5 > 0:
                    vr[i] = v[i] / base5
            # platform = max high of prior 20 bars (exclude i)
            while dq and hh[dq[-1]] <= hh[i]:
                dq.pop()
            dq.append(i)
            while dq and dq[0] < i - 20:
                dq.popleft()
            if i >= 20:
                hidx = dq[0]
                if hidx == i:
                    hidx = dq[1] if len(dq) > 1 else None
                if hidx is not None:
                    plat[i] = hh[hidx]
        for i in range(20, n - H - 1):
            ch = chg[i]
            if ch is None or ch >= 9.8 or not (2.5 <= ch <= 8.0):
                continue
            r5 = vr[i]; a5v = amp5[i]; pl = plat[i]
            if r5 is None or a5v is None or pl is None:
                continue
            if c[i] <= pl:
                continue
            if not (c[i] >= (hh[i] + ll[i]) / 2 and ch > 0):
                continue
            tp = tpos[i]
            if tp + H >= nd:
                continue
            j5 = rowd.get(ds[tp + 5]); j10 = rowd.get(ds[tp + 10])
            if j5 is None or j10 is None:
                continue
            ret5 = (c[j5] / c[i] - 1) * 100
            ret10 = (c[j10] / c[i] - 1) * 100
            d = dates[i]
            b5 = bench_ret(d, 5); b10 = bench_ret(d, 10)
            rg = regime.get(d); yr = d[:4]
            for p in PARAM_SETS:
                if a5v >= p["amp"] or r5 <= p["vr"] or not (p["lo"] <= ch <= p["hi"]):
                    continue
                per_cfg[(p["name"], 5)].append((ret5, b5, rg, yr))
                per_cfg[(p["name"], 10)].append((ret10, b10, rg, yr))

    with open(os.path.join(BULK, f"day_qfq_{sh}.csv"), encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line[0] == '#' or not line.startswith(MAIN_PREFIXES):
                continue
            code = line[:8]
            if code != cur_code:
                if buf:
                    process(cur_code, buf)
                    nstock += 1
                cur_code = code
                buf = []
            p = line[9:].rstrip("\n").split("|")
            if len(p) < 6:
                continue
            try:
                buf.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
            except ValueError:
                continue
        if buf:
            process(cur_code, buf)
            nstock += 1
    maxd = max(maxd, ds[-1])
    print(f"[shard {sh}] stocks={nstock} shard={time.time()-t0:.0f}s cum={time.time()-t_all:.0f}s", flush=True)

# ---- stats ----
def wilson(w, n, z=1.96):
    if n == 0:
        return (None, None)
    p = w / n; den = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0, cen - half), 4), round(min(1, cen + half), 4))

def stats(rows):
    n = len(rows)
    if not rows:
        return {"n": 0}
    rets = [r[0] for r in rows]
    w = sum(1 for x in rets if x > 0)
    mean = sum(rets) / n
    sd = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5 if n > 1 else 0
    lo_, hi_ = wilson(w, n)
    nav = 1; peak = 1; mdd = 0
    for r in rets:
        nav *= 1 + r / 100; peak = max(peak, nav); mdd = min(mdd, (nav / peak - 1) * 100)
    pairs = [r for r in rows if r[1] is not None]
    ne = len(pairs)
    we = sum(1 for r in pairs if r[0] > r[1])
    bavg = sum(r[1] for r in pairs) / ne if ne else None
    ex = [r[0] - r[1] for r in pairs]
    emean = sum(ex) / ne if ne else None
    if ne and ne > 1:
        esd = (sum((x - emean) ** 2 for x in ex) / (ne - 1)) ** 0.5
        se = esd / math.sqrt(ne)
        eci = [round(emean - 1.96 * se, 3), round(emean + 1.96 * se, 3)]
    else:
        eci = None
    return {"n": n, "win_rate": round(w / n, 4), "wilson_ci": [lo_, hi_],
            "avg": round(mean, 3),
            "bench_avg": round(bavg, 3) if bavg is not None else None,
            "excess_pp": round(mean - bavg, 3) if bavg is not None else None,
            "excess_mean_pp": round(emean, 3) if emean is not None else None,
            "excess_ci95": eci,
            "excess_win_rate": round(we / ne, 4) if ne else None,
            "sharpe": round(mean / sd, 3) if sd > 0 else None,
            "maxdd": round(mdd, 2)}

out = {"meta": {"last_date": maxd, "benchmark": "sh000300", "regime_src": "sh000001",
                "pool": "主板全市场 sh600/601/605+sz000/001/002/003 全部主分片00-09/20/30/68(hash分布)",
                "horizons": [5, 10], "param_sets": PARAM_SETS},
       "results": {}}
for p in PARAM_SETS:
    nm = p["name"]; out["results"][nm] = {}
    for h in (5, 10):
        rows = per_cfg[(nm, h)]
        blk = {"all": stats(rows)}
        blk["by_regime"] = {rg: stats([r for r in rows if r[2] == rg])
                            for rg in ("bear", "range", "bull")
                            if any(r[2] == rg for r in rows)}
        yrs = sorted(set(r[3] for r in rows))
        blk["by_year"] = {y: stats([r for r in rows if r[3] == y]) for y in yrs}
        out["results"][nm][f"T+{h}"] = blk
        b = blk["all"]
        print(f"[res] {nm} T+{h}: n={b['n']} wr={b.get('win_rate')} ex={b.get('excess_pp')}pp eci={b.get('excess_ci95')}", flush=True)

fn = "/tmp/s04_expanded_results.json"
json.dump(out, open(fn, "w"), ensure_ascii=False)
print(f"[done] {time.time()-t_all:.0f}s -> {fn}", flush=True)
