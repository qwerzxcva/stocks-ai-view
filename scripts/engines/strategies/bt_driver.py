#!/usr/bin/env python3
"""bt_driver.py — 逐时点无前视回测驱动（M2 最终验收，无前视 asof 修正版）。

口径（与 scripts/walkforward.py 一致）：T 日收盘买入 → T+N 收盘计收益，信号只用 T 日及之前数据。
- 标的池：主板蓝筹/行业龙头 50 只（POOL），区间近 2 年（~490 交易日）。
- 信号：evaluate(...)["action"] in {buy, buy_low} 视为买入信号。
- 收益：close[T+N]/close[T]-1；基准=同期 sh000300（沪深300）日期对齐。
- 超额 pp：策略平均收益 - 同期基准平均收益（百分点）；Wilson 95% CI 对胜率。
- regime：regime_router.judge_regime 对 sh000001 历史逐日判定（bars[:i+1]，无未来函数）。

【逐时点无前视管线】每个回测时点 t：
  ① kline = _loader.slice_asof 截到 t（含），绝不传 t 之后的 bar；
  ② context = load_context(data, live, asof=t)，market_score/dt_count 由 _context
     按 asof 从上证指数历史 / bulk 主分片重算；无历史序列的快照源降级为 None；
     regime 键置 None（防前视），由本驱动判定的 regime_t 覆盖后注入；
  ③ regime_t = regime_router.judge_regime(指数 bars[:i+1])，写入 context['regime']
     后传入 evaluate（职责分离：_context 不判 regime，回测 regime 由驱动逐日给）。
- context 按信号日进程内缓存（同日 10 策略复用），dt_count 全市场统计表一次构建。
- 内存：单进程串行，仅载入 50 只池股票行（<2GB）。
用法：
  python3 bt_driver.py --verify       # 仅验证：不同时点 market_score 差异（无前视生效）
  python3 bt_driver.py --smoke        # 3 只 x 半年管线冒烟
  python3 bt_driver.py --full --out <reports>/backtest_raw.json   # 全量
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import math
import os
import sys
import time

STRAT_DIR = os.path.dirname(os.path.abspath(__file__))
ENG_DIR = os.path.normpath(os.path.join(STRAT_DIR, ".."))
DATA_DIR = os.path.normpath(os.path.join(STRAT_DIR, "..", "..", "..", "data"))
KLINE_INDEX_DIR = os.path.normpath(os.path.join(
    STRAT_DIR, "..", "..", "..", "..", "stocks-data-kline", "data", "index", "history"))
DEFAULT_OUT = os.path.normpath(os.path.join(
    STRAT_DIR, "..", "..", "..", "reports", "strategies", "backtest_raw.json"))

for p in (STRAT_DIR, ENG_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import _loader  # noqa: E402
import _context as ctx_mod  # noqa: E402
import regime_router as rr  # noqa: E402

# 主板 50 只蓝筹/行业龙头（沪深两市混合；排除 30/68/北交所，纯主板）
POOL = [
    "sh600519", "sh601318", "sh600036", "sh601166", "sh600900", "sh601398", "sh600030",
    "sh601888", "sh600276", "sh601899", "sh600309", "sh601012", "sh600585", "sh600887",
    "sh601857", "sh600028", "sh601088", "sh600031", "sh601668", "sh600048", "sh600104",
    "sh600690", "sh601601", "sh601628", "sh600016",
    "sz000001", "sz000002", "sz000063", "sz000333", "sz000651", "sz000725", "sz000858",
    "sz002415", "sz002594", "sz002304", "sz000776", "sz000895", "sz002142", "sz000568",
    "sz002027", "sz002230", "sz000100", "sz000166", "sz000625", "sz002001", "sz000963",
    "sz000338", "sz002050", "sz000538", "sz000069",
]
SMOKE_POOL = ["sh600519", "sz000001", "sh601318"]

STRATEGIES = [
    "s01_ema_strict", "s02_conservative_risk", "s03_turnover_relay",
    "s05_drawdown_cushion", "s06_strong_pullback",
    "s07_emotion_limitup", "s08_multifactor_resonance", "s09_boll_band",
    "s10_main_capital",
]

BUY_ACTIONS = {"buy", "buy_low"}
HORIZONS = (5, 10)
FULL_DAYS = 490
SMOKE_DAYS = 120
WARMUP = 300  # 指标预热（MA250 等），预热段不计信号

# 逐日无前视 context 池：{date: context}，同日全部策略共享
_CTX_POOL: dict = {}


def context_asof(date: str) -> dict:
    """取 asof=date 的无前视 context；进程内缓存，同日只构建一次。"""
    ctx = _CTX_POOL.get(date)
    if ctx is None:
        ctx = ctx_mod.load_context(asof=date)
        _CTX_POOL[date] = ctx
    return ctx


def load_pool_klines(codes):
    """一次性流式扫描分片，主分片优先 + 增量补最新（同 _loader 口径，批量版）。"""
    want = set(codes)
    bulk = os.path.join(DATA_DIR, "bulk")

    best_rows = {c: {} for c in codes}
    for shard in _loader._HISTORY_SHARDS:
        cur = {c: {} for c in codes}
        path = os.path.join(bulk, f"day_qfq_{shard}.csv")
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            continue
        with fh:
            for line in fh:
                code = line[:8]
                if code not in want:
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 7:
                    continue
                try:
                    cur[code][p[1]] = (float(p[2]), float(p[3]), float(p[4]),
                                       float(p[5]), float(p[6]))
                except ValueError:
                    continue
        for c in codes:
            if len(cur[c]) > len(best_rows[c]):
                best_rows[c] = cur[c]
    hist = best_rows
    latest = {c: (max(hist[c].keys()) if hist[c] else "") for c in codes}
    for shard in _loader._INCREMENTAL_SHARDS:
        for path in sorted(glob.glob(os.path.join(bulk, f"day_qfq_{shard}*.csv"))):
            try:
                fh = open(path, encoding="utf-8", errors="ignore")
            except FileNotFoundError:
                continue
            with fh:
                for line in fh:
                    code = line[:8]
                    if code not in want:
                        continue
                    p = line.rstrip("\n").split("|")
                    if len(p) < 7:
                        continue
                    if p[1] <= latest[code] or p[1] in hist[code]:
                        continue
                    try:
                        hist[code][p[1]] = (float(p[2]), float(p[3]), float(p[4]),
                                            float(p[5]), float(p[6]))
                    except ValueError:
                        continue

    out = {}
    for c in codes:
        rows = hist[c]
        dates = sorted(rows.keys())
        out[c] = {
            "date": dates,
            "open": [rows[d][0] for d in dates],
            "close": [rows[d][1] for d in dates],
            "high": [rows[d][2] for d in dates],
            "low": [rows[d][3] for d in dates],
            "vol": [rows[d][4] for d in dates],
        }
    return out


def read_index_txt(sym: str):
    """读 stocks-data-kline 指数日线 txt（code|date|open|close|high|low|vol），全历史。"""
    path = os.path.join(KLINE_INDEX_DIR, f"{sym}.txt")
    bars = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("|")
            if len(p) < 7:
                continue
            try:
                bars.append({"date": p[1], "open": float(p[2]), "close": float(p[3]),
                             "high": float(p[4]), "low": float(p[5]), "vol": float(p[6])})
            except ValueError:
                continue
    return bars


def build_index_regimes():
    """对 sh000001 全历史逐日判定 regime（bars[:i+1]，无未来函数），返回 {date: regime}。"""
    bars = read_index_txt("sh000001")
    regimes = {}
    for i in range(len(bars)):
        regime, _score, _conf, _ev = rr.judge_regime(bars, i)
        regimes[bars[i]["date"]] = regime
    return regimes, len(bars)


def build_bench300():
    bars = read_index_txt("sh000300")
    dates = [b["date"] for b in bars]
    closes = {b["date"]: b["close"] for b in bars}
    return dates, closes


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    p = wins / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def stats_block(rets):
    """rets: list[float]（%，按时间序）。返回 n/胜率/均值/CI/夏普/最大回撤。"""
    n = len(rets)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_ret_pct": None,
                "wilson_ci": [None, None], "sharpe": None, "maxdd_pct": None}
    wins = sum(1 for r in rets if r > 0)
    wr = wins / n
    lo, hi = wilson_ci(wins, n)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    sharpe = round(mean / sd, 3) if sd > 0 else None
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        nav *= (1 + r / 100.0)
        peak = max(peak, nav)
        mdd = min(mdd, (nav / peak - 1) * 100)
    return {"n": n, "win_rate": round(wr, 4), "avg_ret_pct": round(mean, 3),
            "wilson_ci": [lo, hi], "sharpe": sharpe, "maxdd_pct": round(mdd, 2)}


def verify_nolookahead():
    """无前视生效验证：不同时点的 market_score 必须不同；regime 判定无前视。"""
    bars = read_index_txt("sh000001")
    samples = [bars[-250]["date"], bars[-120]["date"], bars[-11]["date"], bars[-1]["date"]]
    rows = []
    for d in samples:
        ctx = ctx_mod.load_context(asof=d)
        i = next(j for j, b in enumerate(bars) if b["date"] == d)
        regime, score, _c, _e = rr.judge_regime(bars, i)
        rows.append({"asof": d,
                     "market_score": ctx.get("market_score"),
                     "regime_ctx": ctx.get("regime"),  # asof 模式下应为 None
                     "regime_bt": regime,
                     "nolookahead": ctx.get("_nolookahead")})
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    ms = [r["market_score"] for r in rows]
    ok = (len(set(ms)) > 1
          and all(r["regime_ctx"] is None and r["nolookahead"] for r in rows))
    print(f"[verify] market_score 随 asof 变化={len(set(ms)) > 1}，"
          f"context.regime=None & _nolookahead=True={ok} → {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--verify", action="store_true", help="仅无前视生效验证")
    ap.add_argument("--strategy", default=None, help="只跑单个策略（调试用）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    if not (args.smoke or args.full or args.verify):
        ap.error("指定 --smoke / --full / --verify")

    if args.verify:
        sys.exit(0 if verify_nolookahead() else 1)

    pool = SMOKE_POOL if args.smoke else POOL
    eval_days = SMOKE_DAYS if args.smoke else FULL_DAYS

    t0 = time.time()
    print(f"[bt] 加载 {len(pool)} 只股票 K 线…", flush=True)
    klines = load_pool_klines(pool)
    print(f"[bt] K 线就绪 {time.time()-t0:.1f}s", flush=True)

    regimes, n_idx_bars = build_index_regimes()
    bench_dates, bench = build_bench300()
    bench_pos = {d: i for i, d in enumerate(bench_dates)}
    print(f"[bt] 指数 regime 判定完成 sh000001 {n_idx_bars} 根", flush=True)

    # 预建全市场主板「每股每日跌幅」表（dt_count 数据源，一次扫描，全策略共享）
    t0 = time.time()
    ctx_mod._build_dt_chg_table(DATA_DIR)
    print(f"[bt] dt 跌停统计表就绪 {time.time()-t0:.1f}s", flush=True)

    strategies = [args.strategy] if args.strategy else STRATEGIES
    result = {"meta": {
        "title": "无前视 asof 修正版 — 逐时点回测",
        "pool_size": len(pool), "eval_days": eval_days, "horizons": list(HORIZONS),
        "bench": "sh000300", "mode": "smoke" if args.smoke else "full",
        "pool": pool,
        "note": "逐时点无前视：① kline slice_asof(t)；② context=load_context(asof=t)，"
                "market_score 由 sh000001 历史按 asof 重算（MA20/60 位置+20日动量，0-100），"
                "dt_count 由 bulk 主分片按 asof 统计主板跌幅≤-9.8%家数；"
                "③ regime_t 由 regime_router.judge_regime 对 sh000001 bars[:i+1] 逐日"
                "无前视判定后注入 context['regime']（asof 模式 _context 的 regime 置 None）；"
                "ban_prob/limitup_ml/moneyflow/lhb 无历史序列，asof 模式降级为 None。",
    }, "strategies": {}}

    for sname in strategies:
        mod = importlib.import_module(sname)
        t1 = time.time()
        sigs = []
        for code in pool:
            kl = klines.get(code) or {}
            dates = kl.get("date", [])
            closes = kl.get("close", [])
            if len(dates) < WARMUP + 20:
                continue
            start = max(WARMUP, len(dates) - eval_days - max(HORIZONS))
            end = len(dates) - max(HORIZONS) - 1  # 保证 T+10 有数据
            for i in range(start, end):
                d = dates[i]
                # ① kline 截到 t（等价 _loader.slice_asof(kl, d)，免字符串扫描）
                sub = {k: v[:i + 1] for k, v in kl.items()}
                # ② 无前视 context（asof=t），浅拷贝避免污染缓存池
                context = dict(context_asof(d))
                # ③ regime_t：t 时点指数无前视判定，注入 context
                context["regime"] = regimes.get(d)
                try:
                    r = mod.evaluate(code, sub, context)
                except Exception as exc:  # 策略内部错误不致命
                    print(f"[bt][warn] {sname} {code} {d}: {exc}", flush=True)
                    continue
                if r.get("action") not in BUY_ACTIONS:
                    continue
                ret = {h: (closes[i + h] / closes[i] - 1) * 100 for h in HORIZONS}
                bret = {}
                j = bench_pos.get(d)
                if j is not None:
                    for h in HORIZONS:
                        if j + h < len(bench_dates):
                            bret[h] = (bench[bench_dates[j + h]] / bench[d] - 1) * 100
                sigs.append({"date": d, "code": code,
                             "regime": regimes.get(d),
                             "ret5": ret[5], "ret10": ret[10],
                             "bench5": bret.get(5), "bench10": bret.get(10)})

        per = {}
        for h in HORIZONS:
            rk = f"ret{h}"
            bk = f"bench{h}"
            rets = [s[rk] for s in sigs if s.get(rk) is not None]
            brets = [s[bk] for s in sigs if s.get(rk) is not None and s.get(bk) is not None]
            st = stats_block(rets)
            bavg = sum(brets) / len(brets) if brets else None
            excess = round(st["avg_ret_pct"] - bavg, 3) if (st["avg_ret_pct"] is not None and bavg is not None) else None
            st["bench_avg_pct"] = round(bavg, 3) if bavg is not None else None
            st["excess_pp"] = excess
            by_regime = {}
            for rg in ("bear", "range", "bull", None):
                seg = [s for s in sigs if s["regime"] == rg and s.get(rk) is not None]
                if not seg:
                    continue
                sst = stats_block([s[rk] for s in seg])
                bseg = [s[bk] for s in seg if s.get(bk) is not None]
                ba = sum(bseg) / len(bseg) if bseg else None
                sst["bench_avg_pct"] = round(ba, 3) if ba is not None else None
                sst["excess_pp"] = round(sst["avg_ret_pct"] - ba, 3) if (sst["avg_ret_pct"] is not None and ba is not None) else None
                by_regime[rg or "unknown"] = sst
            st["by_regime"] = by_regime
            per[f"T+{h}"] = st
        result["strategies"][sname] = {
            "n_signals": len(sigs), "horizons": per,
            "elapsed_s": round(time.time() - t1, 1),
        }
        if args.full:
            result["strategies"][sname]["signals"] = sigs
        print(f"[bt] {sname}: n={len(sigs)} "
              f"T+5 excess={per['T+5']['excess_pp']}pp "
              f"({time.time()-t1:.0f}s, ctx池{len(_CTX_POOL)}日)", flush=True)

    if args.smoke:
        print(json.dumps({k: {h: v["horizons"][h] for h in v["horizons"]}
                          for k, v in result["strategies"].items()},
                         ensure_ascii=False, indent=1)[:3000])
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        print(f"[bt] 写出 {args.out} ({os.path.getsize(args.out)//1024}KB)", flush=True)


if __name__ == "__main__":
    main()
