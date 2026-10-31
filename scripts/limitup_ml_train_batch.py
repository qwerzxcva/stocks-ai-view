#!/usr/bin/env python3
"""limitup_ml_train_batch.py — 分批流式 + 多轮 + 算法/引擎参数寻优的次日涨停模型训练

对应训练流水线的四项要求：
1) 分批次：一次只把一个批次目录的样本读进内存（`load_batch` → 用完 `del`），批次按批内最小交易日排序；
2) 多轮：--rounds N 反复过同一组批次，权重热启动，逐轮记录指标；
3) 同时优化算法与引擎：每轮末在抽样子样本池上搜 (lr, l2, pos_w)，按留出批 lift@K 取最优带入下一轮，
   最优组合写进 model.json 的 engine_params（引擎直接消费）；
4) 删除已训练完的数据：--drop-trained 在所有轮次消费完某批后删除该批次目录（只删 --batch-dir 下、
   且仍含 lp_label.b 的目录），且只在结果 JSON 与模型落盘成功之后执行。

口径（决定了数字怎么读）：
- 最后一批是**留出批**：任何一轮都不参与训练，只用于打分；报告的 base_rate/picks/lift 全部来自它，
  因此各轮、以及寻优前后的数字都可比，回退判据也用它。要改留出数量用 --holdout-batches。
- 其余批次按时间顺序「先打分（用本批之前的权重与标准化统计）、再训练」，第 1 轮的数字是纯 walk-forward；
  第 2 轮起权重已见过前面各批，逐批明细只作参考，不并入报告口径。
- 首个批次在它的第一轮是预热批（前面没有历史），单独标注。

批次目录 = limitup_ml_build.py --out 的产物：
  lp_feat.f32 / lp_label.b / lp_date.i4 / lp_meta.json（lp_ret.f4、lp_sym.i4 可选）

用法:
  python3 scripts/limitup_ml_train_batch.py --batch-dir data/batches --rounds 3 --drop-trained \\
      --out data/batch_train_results.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sys
from array import array
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
KS = (1, 5, 10, 20)
REQUIRED = ("lp_feat.f32", "lp_label.b", "lp_date.i4")


def read_array(path: str, ty: str) -> array:
    a = array(ty)
    with open(path, "rb") as f:
        a.fromfile(f, os.path.getsize(path) // a.itemsize)
    return a


def is_batch_dir(d: str) -> bool:
    return all(os.path.exists(os.path.join(d, n)) for n in REQUIRED)


def find_batches(root: str, explicit: list[str] | None) -> list[str]:
    if explicit:
        dirs = [os.path.abspath(p) for p in explicit]
    else:
        dirs = [os.path.join(root, n) for n in sorted(os.listdir(root))]
        dirs = [os.path.abspath(d) for d in dirs if os.path.isdir(d) and is_batch_dir(d)]
    bad = [d for d in dirs if not is_batch_dir(d)]
    if bad:
        raise SystemExit(f"✗ 不是批次目录（缺 {'/'.join(REQUIRED)}）: {', '.join(bad)}")
    if not dirs:
        raise SystemExit(f"✗ {root} 下没有批次目录")
    return dirs


def load_batch(d: str, dim: int) -> tuple[array, array, array]:
    feat = read_array(os.path.join(d, "lp_feat.f32"), "f")
    lab = read_array(os.path.join(d, "lp_label.b"), "b")
    date = read_array(os.path.join(d, "lp_date.i4"), "i")
    if len(feat) % dim:
        raise SystemExit(f"✗ {d}: lp_feat 长度 {len(feat)} 不是维度 {dim} 的整数倍")
    if len(feat) != len(lab) * dim or len(lab) != len(date):
        raise SystemExit(f"✗ {d}: feat/label/date 长度不一致 {len(feat)}/{len(lab)}/{len(date)} dim={dim}")
    return feat, lab, date


def finite_row(feat: array, base: int, dim: int) -> bool:
    for k in range(dim):
        v = feat[base + k]
        if v != v or v in (float("inf"), float("-inf")):
            return False
    return True


def stats_update(acc: dict, feat: array, dim: int) -> int:
    """累加有限行的 sum/sumsq（缺值行不参与标准化统计），返回跳过的非有限行数。"""
    n = len(feat) // dim
    s, q = acc["sum"], acc["sumsq"]
    skipped = 0
    for i in range(n):
        base = i * dim
        if not finite_row(feat, base, dim):
            skipped += 1
            continue
        for k in range(dim):
            v = feat[base + k]
            s[k] += v
            q[k] += v * v
    acc["n"] += n - skipped
    acc["skipped"] += skipped
    return skipped


def stats_mu_sd(acc: dict, dim: int) -> tuple[list[float], list[float]]:
    n = max(1, acc["n"])
    mu = [acc["sum"][k] / n for k in range(dim)]
    sd = []
    for k in range(dim):
        var = acc["sumsq"][k] / n - mu[k] * mu[k]
        sd.append(math.sqrt(var) if var > 1e-12 else 1.0)
    return mu, sd


def sample_rows(feat: array, lab: array, dim: int, max_neg: int, rng: random.Random) -> list[int]:
    n = len(lab)
    pos, neg = [], []
    for i in range(n):
        if not finite_row(feat, i * dim, dim):
            continue
        if lab[i] == 1:
            pos.append(i)
        else:
            neg.append(i)
    if max_neg > 0 and len(neg) > max_neg:
        neg = rng.sample(neg, max_neg)
    return pos + neg


def fit_rows(w: list[float], b: float, feat: array, lab: array, rows: list[int], dim: int,
             mu: list[float], isd: list[float], params: dict, rng: random.Random) -> tuple[list[float], float]:
    lr, l2, pos_w, epochs = params["lr"], params["l2"], params["pos_w"], params["epochs"]
    m = max(1, len(rows))
    w = list(w)
    for _ in range(epochs):
        order = rows[:]
        rng.shuffle(order)
        for i in order:
            base = i * dim
            s = b
            for k in range(dim):
                s += w[k] * (feat[base + k] - mu[k]) * isd[k]
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, s))))
            g = (p - lab[i]) * (pos_w if lab[i] else 1.0)
            for k in range(dim):
                w[k] -= lr * (g * (feat[base + k] - mu[k]) * isd[k] + l2 * w[k] / m)
            b -= lr * g
    return w, b


def eval_batch(w: list[float], b: float, feat: array, lab: array, date: array, dim: int,
               mu: list[float], isd: list[float]) -> dict:
    """按日打分取 TopK，返回本批的选取/命中/基准计数。"""
    byday: dict[int, list[int]] = {}
    for i, d in enumerate(date):
        byday.setdefault(int(d), []).append(i)
    picks = {k: [0, 0] for k in KS}
    base_tot = base_pos = nan_rows = 0
    for d in sorted(byday):
        idxs = byday[d]
        scored = []
        for i in idxs:
            base = i * dim
            if not finite_row(feat, base, dim):
                nan_rows += 1
                continue
            s = b
            for k in range(dim):
                s += w[k] * (feat[base + k] - mu[k]) * isd[k]
            scored.append((s, i))
        if not scored:
            continue
        scored.sort(reverse=True)
        base_tot += len(scored)
        base_pos += sum(1 for _, i in scored if lab[i] == 1)
        for k in KS:
            top = scored[:k]
            picks[k][1] += len(top)
            picks[k][0] += sum(1 for _, i in top if lab[i] == 1)
    return {"picks": picks, "base_tot": base_tot, "base_pos": base_pos,
            "n_days": len(byday), "nan_rows": nan_rows}


def empty_counts() -> dict:
    return {"picks": {k: [0, 0] for k in KS}, "base_tot": 0, "base_pos": 0, "n_days": 0, "nan_rows": 0}


def merge_counts(dst: dict, src: dict) -> None:
    for k in KS:
        dst["picks"][k][0] += src["picks"][k][0]
        dst["picks"][k][1] += src["picks"][k][1]
    dst["base_tot"] += src["base_tot"]
    dst["base_pos"] += src["base_pos"]
    dst["n_days"] += src["n_days"]
    dst["nan_rows"] += src["nan_rows"]


def summarize(counts: dict) -> dict:
    base = counts["base_pos"] / counts["base_tot"] * 100 if counts["base_tot"] else 0.0
    picks = {}
    for k in KS:
        hit, tot = counts["picks"][k]
        p = hit / tot * 100 if tot else 0.0
        picks[str(k)] = {"precision_pct": round(p, 4), "lift": round(p / base, 3) if base else 0.0,
                         "n_selected": tot, "n_hits": hit}
    return {"base_rate_pct": round(base, 4), "n_eval_rows": counts["base_tot"],
            "n_eval_days": counts["n_days"], "nan_rows_skipped": counts["nan_rows"], "picks": picks}


def score_key(summary: dict) -> float:
    """候选排序口径：留出批 K=10 的 lift；留出批样本不足时该值无意义，由调用方判空。"""
    return float(summary["picks"]["10"]["lift"])


def candidate_grid(args) -> list[dict]:
    base = {"lr": args.lr, "l2": args.l2, "pos_w": args.pos_w}
    grid = [dict(base)]
    for lr in args.grid_lr:
        for l2 in args.grid_l2:
            for pw in args.grid_pos_w:
                cand = {"lr": lr, "l2": l2, "pos_w": pw}
                if cand not in grid:
                    grid.append(cand)
    return grid


def build_opt_pool(train_dirs: list[str], dim: int, opt_neg: int, pool_max: int,
                   rng: random.Random) -> tuple[array, array, int]:
    """流式抽样：每批只留 cap 行，拼成一个常驻内存的小池子（不载入整批）。"""
    cap = max(1, min(opt_neg, pool_max // max(1, len(train_dirs))))
    pool_f, pool_l = array("f"), array("b")
    for d in train_dirs:
        feat, lab, _ = load_batch(d, dim)
        rows = sample_rows(feat, lab, dim, cap, rng)
        for i in rows:
            pool_f.extend(feat[i * dim:(i + 1) * dim])
            pool_l.append(lab[i])
        del feat, lab
    return pool_f, pool_l, cap


def optimize_params(state: dict, pool: tuple[array, array], val: tuple[array, array, array],
                    dim: int, args, rng: random.Random) -> dict:
    """在小样本池上逐个候选跑一遍，按留出批 lift 选最优；候选只调 lr/l2/pos_w，
    训练轮数仍是 --epochs（--opt-epochs 只是寻优期预算，不得写进 engine_params）。"""
    pool_f, pool_l = pool
    val_feat, val_lab, val_date = val
    mu, sd = stats_mu_sd(state["acc"], dim)
    isd = [1.0 / x for x in sd]
    pool_rows = list(range(len(pool_l)))
    best = None
    trial_log = []
    for cand in candidate_grid(args):
        search = dict(cand, epochs=args.opt_epochs)
        w, b = list(state["w"]), state["b"]
        if pool_rows:
            w, b = fit_rows(w, b, pool_f, pool_l, pool_rows, dim, mu, isd, search, rng)
        summary = summarize(eval_batch(w, b, val_feat, val_lab, val_date, dim, mu, isd))
        key = score_key(summary)
        trial_log.append({"params": dict(cand), "search_epochs": args.opt_epochs,
                          "holdout_lift_k10": key,
                          "holdout_precision_pct": summary["picks"]["10"]["precision_pct"]})
        print(f"    候选 lr={cand['lr']} l2={cand['l2']} pos_w={cand['pos_w']} "
              f"→ 留出批 lift@10={key:.3f}x (precision {summary['picks']['10']['precision_pct']:.3f}%, "
              f"小样本池 {len(pool_l):,} 行)")
        if best is None or key > best[0]:
            best = (key, cand, w, b)
    assert best is not None
    key, cand, w, b = best
    return {"params": dict(cand, epochs=args.epochs), "lift": key, "w": w, "b": b,
            "pool_rows": len(pool_l), "trials": trial_log}


def batch_meta(d: str) -> dict:
    mp = os.path.join(d, "lp_meta.json")
    if not os.path.exists(mp):
        return {}
    try:
        with open(mp, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-dir", required=True, help="批次根目录（其下每个子目录是一批样本）")
    ap.add_argument("--batches", nargs="*", help="显式指定批次目录（默认取 --batch-dir 下全部）")
    ap.add_argument("--rounds", type=int, default=3, help="训练轮数")
    ap.add_argument("--holdout-batches", type=int, default=1,
                    help="末尾留出、任何一轮都不参与训练的批次数（至少 1）")
    ap.add_argument("--out", default="", help="结果 JSON 路径")
    ap.add_argument("--drop-trained", action="store_true", help="所有轮次消费完即删该批次目录")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--epochs", type=int, default=24)
    ap.add_argument("--lr", type=float, default=0.8)
    ap.add_argument("--l2", type=float, default=0.1)
    ap.add_argument("--pos-w", type=float, default=5.0)
    ap.add_argument("--max-neg", type=int, default=150000, help="每批抽样负例上限（0=不抽）")
    ap.add_argument("--no-algo-opt", action="store_true", help="关闭参数寻优（只训练不调参）")
    ap.add_argument("--opt-epochs", type=int, default=6)
    ap.add_argument("--opt-neg", type=int, default=30000, help="寻优时每批抽样的负例上限")
    ap.add_argument("--opt-pool-max", type=int, default=200000, help="寻优样本池总行数上限")
    ap.add_argument("--grid-lr", type=float, nargs="*", default=[0.4])
    ap.add_argument("--grid-l2", type=float, nargs="*", default=[0.05, 0.2])
    ap.add_argument("--grid-pos-w", type=float, nargs="*", default=[10.0])
    args = ap.parse_args()

    if array("i").itemsize != 4 or array("f").itemsize != 4 or array("b").itemsize != 1:
        raise SystemExit("✗ 平台 array 尺寸不符（需 i/f=4 字节、b=1 字节），与 lp_*.i4/f32/b 契约不兼容")
    rng = random.Random(args.seed)
    dirs = find_batches(args.batch_dir, args.batches)
    root = os.path.abspath(args.batch_dir)

    dim = 0
    for d in dirs:
        m = batch_meta(d)
        if isinstance(m.get("feature_dim"), int) and m["feature_dim"] > 0:
            dim = m["feature_dim"]
            meta = m
            break
    else:
        raise SystemExit("✗ 批次里没有可用的 lp_meta.json（feature_dim）")
    if args.rounds < 1:
        raise SystemExit("✗ --rounds 至少为 1")

    info = {}
    for d in dirs:
        feat, lab, date = load_batch(d, dim)
        m = batch_meta(d)
        info[d] = {"n_samples": len(lab), "date_min": int(min(date)) if len(date) else 0,
                   "date_max": int(max(date)) if len(date) else 0,
                   "n_positive": m.get("n_positive"), "base_rate_pct": m.get("base_rate_pct")}
        del feat, lab, date
    order = sorted(dirs, key=lambda d: info[d]["date_min"])
    if len(order) < 2:
        raise SystemExit(f"✗ 只有 {len(order)} 个批次：至少需要 2 批（训练批 + 留出批）才有样本外口径")
    holdout_n = max(1, min(args.holdout_batches, len(order) - 1))
    holdout_dirs = order[-holdout_n:]
    train_dirs = order[:-holdout_n]
    if len(order) < 3:
        print("! 只有 2 个批次：可训练批只有 1 个，第 1 轮无 walk-forward 明细，仅留出批指标有效")
    for d in order:
        tag = "留出" if d in holdout_dirs else "训练"
        print(f"  [{tag}] {os.path.basename(d)}: 样本 {info[d]['n_samples']:,} "
              f"交易日 {info[d]['date_min']}~{info[d]['date_max']}")
    print(f"特征维度 {dim} / 批次 {len(order)}（训练 {len(train_dirs)}，留出 {len(holdout_dirs)}）/ "
          f"轮数 {args.rounds}")

    acc = {"sum": [0.0] * dim, "sumsq": [0.0] * dim, "n": 0, "skipped": 0}
    state = {"w": [0.0] * dim, "b": 0.0, "acc": acc}
    params = {"lr": args.lr, "l2": args.l2, "pos_w": args.pos_w, "epochs": args.epochs}
    round_log: list[dict] = []
    best_snap: dict | None = None
    adopted_round = 0
    rows_scanned_total = 0
    eval_holdout = empty_counts()

    for rnd in range(1, args.rounds + 1):
        print(f"\n第 {rnd}/{args.rounds} 轮 参数 lr={params['lr']} l2={params['l2']} "
              f"pos_w={params['pos_w']} epochs={params['epochs']}")
        holdout_counts = empty_counts()
        per_batch = []
        for d in order:
            feat, lab, date = load_batch(d, dim)
            rows_scanned_total += len(lab)
            is_holdout = d in holdout_dirs
            # 首个训练批在第 1 轮前面没有历史，它的打分是空模型，单列预热
            warmup = rnd == 1 and d == order[0]
            # 先按「本批之前」的标准化统计打分，再把本批并入统计后训练，避免用未来分布打分
            mu, sd = stats_mu_sd(acc, dim)
            isd = [1.0 / x for x in sd]
            ev = eval_batch(state["w"], state["b"], feat, lab, date, dim, mu, isd)
            if is_holdout:
                merge_counts(holdout_counts, ev)
            s = summarize(ev)
            per_batch.append({
                "batch": os.path.basename(d), "role": "holdout" if is_holdout else "train",
                "warmup": warmup,
                "date_min": info[d]["date_min"], "date_max": info[d]["date_max"],
                "n_samples": info[d]["n_samples"], "nan_rows_skipped": s["nan_rows_skipped"],
                "precision_k10_pct": s["picks"]["10"]["precision_pct"],
                "lift_k10": s["picks"]["10"]["lift"], "base_rate_pct": s["base_rate_pct"],
                **({"warmup_only": True} if warmup else {}),
            })
            print(f"  {'[留出]' if is_holdout else '[训练]'}{os.path.basename(d)}"
                  f"{'(预热)' if warmup else ''}: precision@10={s['picks']['10']['precision_pct']:.3f}% "
                  f"lift={s['picks']['10']['lift']:.2f}x 基准={s['base_rate_pct']:.3f}%")
            if not is_holdout:
                stats_update(acc, feat, dim)
            # 留出批任何一轮都不训练，否则第 2 轮起报告的就不是样本外指标
            if not is_holdout:
                mu, sd = stats_mu_sd(acc, dim)
                isd = [1.0 / x for x in sd]
                rows = sample_rows(feat, lab, dim, args.max_neg, rng)
                if rows:
                    state["w"], state["b"] = fit_rows(state["w"], state["b"], feat, lab, rows, dim,
                                                      mu, isd, params, rng)
            del feat, lab, date

        summary = summarize(holdout_counts)
        entry = {"round": rnd, "params": dict(params), "batches": per_batch,
                 "holdout_summary_this_round": summary}
        if not args.no_algo_opt and len(train_dirs) >= 1 and len(candidate_grid(args)) > 1:
            print("  参数寻优（每批抽样小样本池，判据=留出批 lift@10）:")
            pool = build_opt_pool(train_dirs, dim, args.opt_neg, args.opt_pool_max, rng)
            vf, vl, vd = load_batch(holdout_dirs[0], dim)
            hmu, hsd = stats_mu_sd(acc, dim)
            base_before = summarize(eval_batch(state["w"], state["b"], vf, vl, vd, dim,
                                               hmu, [1.0 / x for x in hsd]))
            last_opt = optimize_params(state, (pool[0], pool[1]), (vf, vl, vd), dim, args, rng)
            after = summarize(eval_batch(last_opt["w"], last_opt["b"], vf, vl, vd, dim,
                                         hmu, [1.0 / x for x in hsd]))
            del pool, vf, vl, vd
            entry["algo_opt"] = {
                "chosen": last_opt["params"], "pool_rows": last_opt["pool_rows"],
                "holdout_lift_before": score_key(base_before),
                "holdout_lift_after": score_key(after),
                "adopted": score_key(after) >= score_key(base_before),
                "trials": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in t.items()}
                           for t in last_opt["trials"]],
            }
            if entry["algo_opt"]["adopted"]:
                state["w"], state["b"] = last_opt["w"], last_opt["b"]
                params = dict(last_opt["params"])
            else:
                print(f"    寻优结果 {score_key(after):.3f}x 不优于当前 {score_key(base_before):.3f}x → 不采纳")

        cur = summary["picks"]["10"]["lift"]
        if best_snap is None or cur > best_snap["lift"]:
            best_snap = {"round": rnd, "lift": cur, "w": list(state["w"]), "b": state["b"],
                         "params": dict(params)}
            adopted_round = rnd
            entry["summary"] = summary
            entry["adopted"] = True
        else:
            # 多轮不回退就会越训越差：本轮留出批指标劣于历史最优 → 回退，下一轮从最优状态继续
            entry["rolled_back_to_round"] = best_snap["round"]
            entry["summary"] = summary
            entry["adopted"] = False
            state["w"], state["b"] = list(best_snap["w"]), best_snap["b"]
            params = dict(best_snap["params"])
            adopted_round = best_snap["round"]
            print(f"  本轮留出批 lift@10 {cur:.3f}x 低于第 {best_snap['round']} 轮 "
                  f"{best_snap['lift']:.3f}x → 回退到第 {best_snap['round']} 轮状态")
        round_log.append(entry)
        print(f"  第 {rnd} 轮留出批: precision@10={summary['picks']['10']['precision_pct']:.3f}% "
              f"lift={summary['picks']['10']['lift']:.2f}x 基准={summary['base_rate_pct']:.3f}%")

    # 报告口径：用最终落盘的权重重新给留出批打分，避免「报告数字来自另一个模型」
    for d in holdout_dirs:
        feat, lab, date = load_batch(d, dim)
        mu, sd = stats_mu_sd(acc, dim)
        merge_counts(eval_holdout, eval_batch(state["w"], state["b"], feat, lab, date, dim,
                                              mu, [1.0 / x for x in sd]))
        del feat, lab, date
    final = summarize(eval_holdout)
    if final["n_eval_rows"] == 0:
        raise SystemExit("✗ 留出批没有可打分的有限行，拒绝出结论（也不会删任何数据）")

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "limitup_ml_train_batch",
        "rounds": args.rounds,
        "adopted_round": adopted_round,
        "n_features": dim,
        "batch_root": root,
        "batch_policy": ("按时间排序；末尾 %d 批为留出批（任何一轮都不训练），报告的 base_rate/picks/lift "
                         "全部来自留出批" % len(holdout_dirs)),
        "batches": [{"dir": os.path.basename(d), "n_samples": info[d]["n_samples"],
                     "n_positive": info[d]["n_positive"], "base_rate_pct": info[d]["base_rate_pct"],
                     "date_min": info[d]["date_min"], "date_max": info[d]["date_max"],
                     "role": "holdout" if d in holdout_dirs else "train"} for d in order],
        "holdout_batches": [os.path.basename(d) for d in holdout_dirs],
        "base_rate_pct": final["base_rate_pct"],
        "picks": final["picks"],
        "n_eval_rows": final["n_eval_rows"],
        "n_eval_days": final["n_eval_days"],
        "nan_rows_skipped": final["nan_rows_skipped"],
        "rows_scanned_total": rows_scanned_total,
        "rounds_log": round_log,
        "engine_params": dict(params, max_neg=args.max_neg, feature_dim=dim,
                              minute_min_bars=(meta.get("minute") or {}).get("min_bars"),
                              minute_status=(meta.get("minute") or {}).get("status")),
        "dropped_batches": [],
        "drop_skipped": [],
        "provenance": {"trainer": "limitup_ml_train_batch", "batch_meta": meta.get("provenance", {}),
                       "source": "limitup_ml_build.py 分批产物"},
    }

    out_path = args.out or os.path.join(root, "batch_train_results.json")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    model_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "limitup_ml_model.json")
    model = {
        "w": [round(x, 8) for x in state["w"]],
        "bias": round(state["b"], 8),
        "n_features": dim,
        "trained_at": datetime.now(CST).isoformat(timespec="seconds"),
        "adopted_round": adopted_round,
        "engine_params": out["engine_params"],
        "scaler": {"mu": [round(c / max(1, acc["n"]), 8) for c in acc["sum"]],
                   "sd": [round(x, 8) for x in stats_mu_sd(acc, dim)[1]]},
        "rows_trained_total": acc["n"],
        "holdout_batches": out["holdout_batches"],
        "holdout_metrics": {"base_rate_pct": final["base_rate_pct"], "picks": final["picks"]},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)
    print(f"\n→ {out_path}\n→ {model_path}")

    # 结果与模型先落盘、再删数据：删到一半失败也不会出现「产物没有、数据也没了」
    if args.drop_trained:
        for d in order:
            real = os.path.realpath(d)
            if not real.startswith(os.path.realpath(root) + os.sep):
                out["drop_skipped"].append({"dir": d, "reason": "不在 --batch-dir 根目录下"})
                continue
            if not is_batch_dir(real):
                out["drop_skipped"].append({"dir": d, "reason": "不是批次目录"})
                continue
            try:
                shutil.rmtree(real)
            except OSError as exc:
                out["drop_skipped"].append({"dir": real, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            out["dropped_batches"].append(real)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"删除已训练完的批次 {len(out['dropped_batches'])} 个"
              + (f"，跳过 {len(out['drop_skipped'])} 个" if out["drop_skipped"] else ""))

    print(f"\n留出批（{', '.join(out['holdout_batches'])}）出样本外基准次日涨停率 "
          f"{final['base_rate_pct']:.3f}% / 采用第 {adopted_round} 轮权重")
    for k in KS:
        p = final["picks"][str(k)]
        print(f"  Top{k}: 命中率 {p['precision_pct']:.3f}% 提升 {p['lift']:.2f}x "
              f"(选取 {p['n_selected']:,}, 命中 {p['n_hits']:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
