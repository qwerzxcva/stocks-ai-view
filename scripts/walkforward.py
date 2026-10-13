#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/walkforward.py ═══
# 上游 commit: 4866b80e246a · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (from paths import AI_VIEW_DATA; --out 落盘 data/walkforward.json)
"""
Walk-forward 滚动前推验证 (参考 tick-stock-panel backtest/walkforward)
用法: python3 walkforward.py --train-days 120 --test-days 20 --step-days 20
核心: 训练窗选参数 -> 紧邻测试窗样本外验证 -> 滚动. IS/OOS 退化比=过拟合信号.
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import random
import sys
from datetime import date, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


class Fold:
    def __init__(self, index, train_start, train_end, test_start, test_end):
        self.index, self.train_start, self.train_end = index, train_start, train_end
        self.test_start, self.test_end = test_start, test_end


def generate_folds(start, end, train_days, test_days, step_days):
    if min(train_days, test_days, step_days) <= 0:
        raise ValueError("窗口参数必须为正")
    folds, i = [], 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end + timedelta(days=1)   # 隔断前视泄漏
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start += timedelta(days=step_days)
    return folds


def bootstrap_win_ci(returns, n_boot=1000, win_threshold=0.0, seed=42, alpha=0.05):
    """Bootstrap 胜率 95% 置信区间: 对收益序列做 n_boot 次有放回重采样,
    每次算胜率(收益>win_threshold 的比例), 返回 (胜率点估计, ci_low, ci_high, 基准胜率阈值)。
    参考: 蒙特卡洛bootstrap对样本外收益的重采样置信区间 (pybroker式稳健检验)。"""
    r = [x for x in returns if x is not None and not pd.isna(x)]
    if not r:
        return {"win_rate": None, "ci_low": None, "ci_high": None, "n": 0}
    rng = random.Random(seed)
    n = len(r)
    point = sum(1 for x in r if x > win_threshold) / n
    wins = []
    for _ in range(n_boot):
        s = [r[rng.randrange(n)] for _ in range(n)]
        wins.append(sum(1 for x in s if x > win_threshold) / n)
    wins.sort()
    lo_i = int((alpha / 2) * n_boot)
    hi_i = min(n_boot - 1, int((1 - alpha / 2) * n_boot))
    return {"win_rate": round(point, 4), "ci_low": round(wins[lo_i], 4),
            "ci_high": round(wins[hi_i], 4), "n": n, "n_boot": n_boot}


def run_rolling_validation(df, score_fn, param_grid=None, n_windows=6, test_days=20):
    """pybroker式滚动 train/test 防过拟合验证:
    将时间轴切成 n_windows 段, 每段 train=前序全部 / test=紧邻20日(扩张窗 walk-forward),
    检验 OOS 收益是否随窗口扩张保持为正且稳定(标准差小)。无 param_grid 时用 score_fn 默认参数。"""
    dates = sorted(df["date"].unique())
    d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    total = (d1 - d0).days
    if total <= test_days * n_windows:
        raise ValueError("数据长度不足")
    results = []
    for w in range(1, n_windows + 1):
        tr_end = d0 + timedelta(days=int(total * w / n_windows))
        te_start = tr_end + timedelta(days=1)
        te_end = min(te_start + timedelta(days=test_days), d1)
        tr = df[(df.date >= d0.isoformat()) & (df.date <= tr_end.isoformat())]
        te = df[(df.date >= te_start.isoformat()) & (df.date <= te_end.isoformat())]
        if te.empty:
            continue
        if param_grid:
            best_p, best_v = None, -1e9
            for p in param_grid:
                sc = score_fn(tr, p)
                v = sum(sc.values()) / max(len(sc), 1)
                if v > best_v:
                    best_p, best_v = p, v
            sc_oos = score_fn(te, best_p)
        else:
            best_p = None
            sc_oos = score_fn(te)
        oos = sum(sc_oos.values()) / max(len(sc_oos), 1) if sc_oos else 0.0
        results.append({"window": w, "train_end": tr_end.isoformat(),
                        "test": f"{te_start}~{te_end}", "params": best_p, "oos": round(oos, 4)})
    oos_vals = [r["oos"] for r in results]
    mean = sum(oos_vals) / max(len(oos_vals), 1)
    std = (sum((x - mean) ** 2 for x in oos_vals) / max(len(oos_vals), 1)) ** 0.5
    return {"windows": results, "oos_mean": round(mean, 4), "oos_std": round(std, 4),
            "oos_positive_ratio": round(sum(1 for x in oos_vals if x > 0) / max(len(oos_vals), 1), 4),
            "verdict": "稳定" if std <= max(abs(mean), 0.01) and mean > 0 else "过拟合/不稳定风险"}


def run_walkforward(df, score_fn, param_grid, train_days=120, test_days=20, step_days=20,
                    return_detail=False):
    """
    df: 日线DataFrame(多票); score_fn(df_train, params) -> {symbol: score}
        pick_fn 在测试窗内按score排序取TopN, 返回测试窗实际收益(次日开盘买入口径由调用方保证)
    返回: folds明细 + OOS拼接收益 + IS/OOS退化比
    """
    dates = sorted(df["date"].unique())
    d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    folds = generate_folds(d0, d1, train_days, test_days, step_days)
    results = []
    for f in folds:
        tr = df[(df.date >= f.train_start.isoformat()) & (df.date <= f.train_end.isoformat())]
        te = df[(df.date >= f.test_start.isoformat()) & (df.date <= f.test_end.isoformat())]
        # 网格搜索: 训练窗内逐参数打分
        best_p, best_v = None, -1e9
        for p in param_grid:
            scores = score_fn(tr, p)
            v = sum(scores.values()) / max(len(scores), 1)
            if v > best_v:
                best_p, best_v = p, v
        scores_oos = score_fn(te, best_p)
        oos = sum(scores_oos.values()) / max(len(scores_oos), 1) if scores_oos else 0.0
        results.append({"fold": f.index, "params": best_p, "is": round(best_v, 4),
                        "oos": round(oos, 4), "degradation": round(best_v - oos, 4)})
    avg_is = sum(r["is"] for r in results) / max(len(results), 1)
    avg_oos = sum(r["oos"] for r in results) / max(len(results), 1)
    return {"folds": results, "avg_is": round(avg_is, 4), "avg_oos": round(avg_oos, 4),
            "verdict": "过拟合风险高" if avg_oos < avg_is * 0.4 else "可接受"}


# ─── 预测样本加载 (IS/OOS 计算) ────────────────────────────────
def _parse_kline_line(line):
    """解析 bulk K 线行 → (code_with_prefix, date, close); 失败返回 None。

    bulk 存在两种格式, 只按 '|' 全切分会漏掉逗号分隔的那批:
      sh600016|2026-10-12,3.5,3.56,3.59,3.5,3294332.0   (extra/recent)
      sz000610|2023-06-15|17.120|17.380|...            (00-09/stockdb/expand)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "|" in line:
        code, rest = line.split("|", 1)
        fields = rest.replace("|", ",").split(",")
    else:
        return None
    if len(fields) < 4:
        return None
    try:
        return code.strip(), fields[0], float(fields[3])
    except ValueError:
        return None


def load_bars(bulk_dir):
    """加载 bulk K 线 → {code(带前缀): [[date, close], ...]} 升序去重。"""
    data = {}
    seen = set()
    files = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = _parse_kline_line(line)
                if parsed is None:
                    continue
                code, d, close = parsed
                key = (code, d)
                if key in seen:
                    continue
                seen.add(key)
                data.setdefault(code, []).append((d, close))
    for c in data:
        data[c].sort(key=lambda r: r[0])
    return data


def load_prediction_samples(pred_dir, bulk_dir, horizon):
    """把 data/predictions/ 的快照摊平成可验证样本。

    返回 [{pred_date, code, direction, ret_pct}, ...]
    direction: 'bull' (指标看多) / 'bear' (指标看空)
    只用预测时点已知的信息; 未来 K 线不足的样本直接丢弃。
    """
    bars = load_bars(bulk_dir)
    idx = {c: {d: i for i, (d, _) in enumerate(v)} for c, v in bars.items()}
    samples = []
    for pf in sorted(glob.glob(os.path.join(pred_dir, "engine_v5*.json"))):
        try:
            d = json.load(open(pf, encoding="utf-8"))
        except Exception:
            continue
        top = d.get("top") or []
        pred_date = top[0].get("date") if top else None
        if not pred_date:
            continue
        for r in top:
            code = r.get("code")
            if not code or code not in idx:
                continue
            bar_idx = idx[code]
            if pred_date not in bar_idx:
                continue
            i = bar_idx[pred_date]
            j = i + horizon
            seq = bars[code]
            if j >= len(seq):
                continue
            entry = seq[i][1]
            exit_ = seq[j][1]
            if entry <= 0:
                continue
            ret = (exit_ / entry - 1) * 100
            bull, bear = int(r.get("bull") or 0), int(r.get("bear") or 0)
            direction = "bull" if bull > bear else ("bear" if bear > bull else "neutral")
            samples.append({"pred_date": pred_date, "code": code,
                            "direction": direction, "ret_pct": round(ret, 3),
                            "score": r.get("score")})
    samples.sort(key=lambda s: s["pred_date"])
    return samples


def _win_rate(rows, direction):
    """某方向的胜率 (%) 与样本数。看多=上涨命中, 看空=下跌命中。"""
    sub = [r for r in rows if r["direction"] == direction]
    if not sub:
        return None, 0
    if direction == "bull":
        hit = sum(1 for r in sub if r["ret_pct"] > 0)
    else:
        hit = sum(1 for r in sub if r["ret_pct"] < 0)
    return round(hit / len(sub) * 100, 2), len(sub)


def _slice(samples, d0, d1):
    return [s for s in samples if d0.isoformat() <= s["pred_date"] <= d1.isoformat()]


def evaluate_folds(folds, samples, horizon):
    """逐 fold 计算 IS(train 窗) / OOS(test 窗) 胜率与平均收益。"""
    per_fold = []
    for f in folds:
        tr = _slice(samples, f.train_start, f.train_end)
        te = _slice(samples, f.test_start, f.test_end)
        is_bull, is_bull_n = _win_rate(tr, "bull")
        is_bear, is_bear_n = _win_rate(tr, "bear")
        oos_bull, oos_bull_n = _win_rate(te, "bull")
        oos_bear, oos_bear_n = _win_rate(te, "bear")
        all_is = [r["ret_pct"] for r in tr]
        all_oos = [r["ret_pct"] for r in te]
        is_win = sum(1 for x in all_is if x > 0)
        oos_win = sum(1 for x in all_oos if x > 0)
        per_fold.append({
            "fold": f.index,
            "train_range": f"{f.train_start}~{f.train_end}",
            "test_range": f"{f.test_start}~{f.test_end}",
            "is_win_rate": round(is_win / len(all_is) * 100, 2) if all_is else None,
            "oos_win_rate": round(oos_win / len(all_oos) * 100, 2) if all_oos else None,
            "is_n": len(all_is),
            "oos_n": len(all_oos),
            "avg_ret": round(sum(all_oos) / len(all_oos), 3) if all_oos else None,
            "is_bull_win_rate": is_bull, "is_bull_n": is_bull_n,
            "is_bear_win_rate": is_bear, "is_bear_n": is_bear_n,
            "oos_bull_win_rate": oos_bull, "oos_bull_n": oos_bull_n,
            "oos_bear_win_rate": oos_bear, "oos_bear_n": oos_bear_n,
        })
    valid = [f for f in per_fold if f["is_win_rate"] is not None
             and f["oos_win_rate"] is not None and f["is_n"] >= 30 and f["oos_n"] >= 30]
    mean_is = (sum(f["is_win_rate"] for f in valid) / len(valid)) if valid else None
    mean_oos = (sum(f["oos_win_rate"] for f in valid) / len(valid)) if valid else None
    decay = round(mean_is - mean_oos, 2) if (mean_is is not None and mean_oos is not None) else None
    if mean_is is None or mean_oos is None:
        verdict = "insufficient_samples"
    elif mean_oos < mean_is - 10:
        verdict = "overfit_warning"
    elif mean_oos < mean_is - 5:
        verdict = "mild_decay"
    else:
        verdict = "stable"
    return per_fold, {
        "horizon_days": horizon,
        "n_folds_total": len(per_fold),
        "n_folds_valid": len(valid),
        "mean_is_win": round(mean_is, 2) if mean_is is not None else None,
        "mean_oos_win": round(mean_oos, 2) if mean_oos is not None else None,
        "oos_vs_is_decay": decay,
        "overfit_warning": verdict == "overfit_warning",
        "verdict": verdict,
        "min_samples_per_fold": 30,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward 滚动前推验证")
    ap.add_argument("--train-days", type=int, default=120)
    ap.add_argument("--test-days", type=int, default=20)
    ap.add_argument("--step-days", type=int, default=20)
    ap.add_argument("--start", default=None, help="起始日期 (默认取预测快照最早日期)")
    ap.add_argument("--end", default=None, help="结束日期 (默认取预测快照最晚日期)")
    ap.add_argument("--pred-dir", default=None, help="预测快照目录 (默认 DATA_DIR/predictions)")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--horizon", type=int, default=5, help="持有期 (交易日)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "walkforward.json"))
    a = ap.parse_args(argv)

    try:
        from paths import BULK
    except Exception:
        BULK = "/root/workspace/stocks-data-kline/data/bulk"
    bulk = a.bulk or BULK
    pred_dir = a.pred_dir or os.path.join(DATA_DIR, "predictions")

    pred_files = sorted(glob.glob(os.path.join(pred_dir, "engine_v5*.json")))
    if not pred_files:
        print(f"无预测快照: {pred_dir}/engine_v5*.json", file=sys.stderr)
        return 1

    def _fdate(p):
        try:
            return date.fromisoformat(os.path.basename(p).replace("engine_v5_", "").replace(".json", ""))
        except ValueError:
            return None
    fd = [x for x in (_fdate(p) for p in pred_files) if x]
    if not fd:
        print("快照文件名无法解析日期", file=sys.stderr)
        return 1
    d0 = date.fromisoformat(a.start) if a.start else min(fd)
    d1 = date.fromisoformat(a.end) if a.end else max(fd)

    folds = generate_folds(d0, d1, a.train_days, a.test_days, a.step_days)
    print(f"[wf] 预测快照 {len(pred_files)} 个, 区间 {d0}~{d1}, folds={len(folds)}", flush=True)
    samples = load_prediction_samples(pred_dir, bulk, a.horizon)
    print(f"[wf] horizon={a.horizon}d 可验证样本 {len(samples)} 条", flush=True)
    if not samples:
        print("[wf] 无可验证样本 (K 线未覆盖或 horizon 超出数据末端)", file=sys.stderr)
        return 1

    per_fold, summary = evaluate_folds(folds, samples, a.horizon)
    payload = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "params": {"train_days": a.train_days, "test_days": a.test_days,
                   "step_days": a.step_days, "start": d0.isoformat(),
                   "end": d1.isoformat(), "horizon_days": a.horizon},
        "n_samples": len(samples),
        "folds": per_fold,
        "summary": summary,
        "note": "IS=训练窗内预测胜率, OOS=紧邻测试窗样本外胜率; "
                "两者同口径(全方向上涨占比), OOS<IS-10pct 判 overfit_warning",
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print(f"[wf] {summary['n_folds_valid']}/{summary['n_folds_total']} folds 有效 "
          f"(每 fold IS/OOS 各 ≥30 样本)")
    print(f"[wf] mean IS={summary['mean_is_win']}%  mean OOS={summary['mean_oos_win']}%  "
          f"decay={summary['oos_vs_is_decay']}pct  判定={summary['verdict']}")
    for f in per_fold[-3:]:
        print(f"  fold{f['fold']}: train {f['train_range']} IS={f['is_win_rate']}%(n={f['is_n']}) | "
              f"test {f['test_range']} OOS={f['oos_win_rate']}%(n={f['oos_n']}) "
              f"avg_ret={f['avg_ret']}%")
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
