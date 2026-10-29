#!/usr/bin/env python3
"""factor_composite.py — 多因子横截面合成（v1）。

## 定位

云端此前只有「单票时序打分」（engine_v5）与「逐维加权」（multidim），没有真正的
**横截面因子合成**：同一批股票按同一组因子做 z-score，再合成一个名次分。缺这一步，
就会把「绝对涨跌幅」当成「相对强弱」，牛市里所有票都高、熊市里全低，排序失去分辨力。

## 口径

    raw_i = 因子原值（见 FACTORS）
    w_i   = winsorize(raw, pct=2%) → z-score（横截面，均值 0 方差 1）
    z     = Σ w_i · z_i       （权重来自 engine_spec.PRODUCTS[factor_composite].params）
    score = 50 + 10·z 截断到 [0,100]

因子方向已统一为「越大越好」（low_vol 取负、turnover 取负）。

## 端上

只依赖 K 线，`availability: [cloud, device]`；端上按同一份 params 与同一套
winsorize/zscore 复刻，黄金向量由 engine_spec 机制覆盖（若纳入）。
"""
import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import limit_caliber as LC  # noqa: E402

SPEC = os.path.join(os.path.dirname(HERE), "data", "engine_spec.json")
VERSION = "factor_composite_v1"
DEFAULT_PARAMS = {
    "winsor_pct": 2.0,
    "min_bars": 80,
    "weights": {"mom20": 0.25, "mom60": 0.20, "low_vol": 0.20,
                "turnover": 0.15, "dev_ma20": 0.20},
}


def load_params():
    """权重/参数从 spec 注册表取，脚本内不写第二份。"""
    try:
        with open(SPEC, encoding="utf-8") as f:
            spec = json.load(f)
        for p in spec.get("products") or []:
            if p.get("id") == "factor_composite":
                return {**DEFAULT_PARAMS, **(p.get("params") or {})}, spec.get("spec_version")
    except Exception:
        pass
    return DEFAULT_PARAMS, None


def read_bulk(bulk_dir, max_codes=4200, tail=140):
    """code → [(date, close, high, low, vol)]，只留最近 tail 根。"""
    out = {}
    for path in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or "|" not in line:
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                code = LC.bare(parts[0])
                if not LC.is_main_board(code):
                    continue
                try:
                    row = (parts[1], float(parts[2]), float(parts[3]),
                           float(parts[4]), float(parts[5]))
                except ValueError:
                    continue
                out.setdefault(code, []).append(row)
        if len(out) >= max_codes:
            break
    return {c: r[-tail:] for c, r in out.items() if len(r) >= tail // 2}


def factor_values(rows, min_bars):
    """单票因子原值（越大越好）。"""
    close = [r[1] for r in rows]
    vol = [r[4] for r in rows]
    n = len(close)
    if n < min_bars:
        return None
    ret = [(close[i] / close[i - 1] - 1) for i in range(1, n) if close[i - 1] > 0]
    if len(ret) < 60:
        return None
    mom20 = close[-1] / close[-21] - 1 if close[-21] else None
    mom60 = close[-1] / close[-61] - 1 if close[-61] else None
    vol_std = statistics.pstdev(ret[-60:]) if len(ret) >= 60 else None
    ma20 = sum(close[-20:]) / 20
    dev = close[-1] / ma20 - 1 if ma20 else None
    # 换手代理: 近 5 日均量 / 近 60 日均量 - 1，越高越拥挤 → 取负
    v5 = sum(vol[-5:]) / 5
    v60 = sum(vol[-60:]) / 60
    turn = (v5 / v60 - 1) if v60 else None
    if None in (mom20, mom60, vol_std, dev, turn):
        return None
    return {"mom20": mom20, "mom60": mom60, "low_vol": -(vol_std or 0),
            "turnover": -turn, "dev_ma20": dev, "close": close[-1], "date": rows[-1][0]}


def winsorize(vals, pct):
    s = sorted(vals)
    k = max(1, int(len(s) * pct / 100.0))
    lo, hi = s[k - 1], s[-k]
    return [min(max(v, lo), hi) for v in vals]


def zscore(vals):
    mu = statistics.fmean(vals)
    sd = statistics.pstdev(vals) or 1e-9
    return [(v - mu) / sd for v in vals]


def main():
    ap = argparse.ArgumentParser(description="多因子横截面合成")
    ap.add_argument("--bulk", default=None)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "data", "factor_composite.json"))
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--limit", type=int, default=4200)
    args = ap.parse_args()

    params, spec_ver = load_params()
    bulk = args.bulk or os.path.normpath(os.path.join(HERE, "..", "..", "stocks-data-kline", "data", "bulk"))
    if not os.path.isdir(bulk):
        print(f"[factor_composite] bulk 不存在: {bulk}", file=sys.stderr)
        return 2
    rows_by_code = read_bulk(bulk, args.limit)
    feats = {}
    for code, rows in rows_by_code.items():
        f = factor_values(rows, params["min_bars"])
        if f:
            feats[code] = f
    if not feats:
        print("[factor_composite] 无可用样本", file=sys.stderr)
        return 1

    names = [k for k in params["weights"]]
    zcols = {}
    for nm in names:
        vals = [feats[c][nm] for c in feats]
        zcols[nm] = dict(zip(feats, zscore(winsorize(vals, params["winsor_pct"]))))

    results = []
    for code, f in feats.items():
        z = sum(params["weights"][nm] * zcols[nm][code] for nm in names)
        results.append({
            "code": code, "z": round(z, 4),
            "score": round(max(0.0, min(100.0, 50 + 10 * z)), 1),
            "price": f["close"], "date": f["date"],
            "factors": {nm: round(f[nm], 5) for nm in names},
        })
    results.sort(key=lambda r: -r["score"])
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": VERSION, "version": VERSION,
        "spec_version": spec_ver,
        "n_scanned": len(rows_by_code), "n_scored": len(results),
        "weights": params["weights"],
        "top": results[: args.top],
        "meta": {"winsor_pct": params["winsor_pct"], "min_bars": params["min_bars"],
                 "formula": "score = 50 + 10*Σw·z（横截面 z-score，winsor 2%）",
                 "n_main_board_only": True},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[factor_composite] {len(results)} 只，top1 {out['top'][0]['code']} "
          f"{out['top'][0]['score']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
