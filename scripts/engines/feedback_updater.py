#!/usr/bin/env python3
"""
feedback_updater.py — 复盘反馈引擎 v1 (中枢层, 新增文件, 不改任何既有文件)

【用途】从历史复盘记录计算各引擎/信号源的滚动胜率与超额, 乘性更新集成权重,
  供 central_orchestrator.py 下一轮集成评分使用 (权重版本化落盘)。

【复盘数据源】(全部本地真实文件, 只读)
  1. DuckDB pick_log: ~/stockdb/picks.duckdb (表结构经 pick_tracker.py 核实:
     id/ts/pick_date/symbol/name/entry_price/kind/rule/next_date/
     ret_open_close/ret_open_high/ret_open_low/ret_t2/verdict/settled)
     kind ∈ {ai_pick, tailpick, ...} → 映射为引擎 key `picklog:<kind>`。
     收益口径: ret_open_close = 次日开盘买入→收盘 (T+1, %), 为表内既有口径。
  2. predictions 快照: data/predictions/engine_v5_*.json (656 个日快照,
     top[{code,score,...}]) → 引擎 key `engine_v5`。
     口径: 每日快照按 score 排序取高分组(前 --top-pct), 用分片日线自算
     T+1/T+5/T+10 收益(T 日收盘买入, 与 pick_log 的开盘买入口径不同,
     两套口径在输出 sources 块中分别写明, 不混算)。
  3. 基准: 同期全市场等权基准, 从分片日线(day_qfq_*.csv)自算
     (universe 每只票同窗口收益的等权平均), 无既有基线文件可复用,
     口径与快照口径一致(close→close); 超额 = 分组收益 − 基准收益。
  duckdb 模块不可用 → 自动降级为只用 predictions 口径, 标注 degraded。

【更新规则】(乘性更新)
  r_i  = 引擎 i 近 --lookback 窗口高分组的 T+1 胜率 − 同期基准胜率
         (T+5/T+10 收益与超额作证据附带输出)
  w_i ← w_i · exp(0.1 · (r_i − r̄))   (r̄ = 各引擎 r 均值)
  L1 归一化; 单引擎权重截断 [0.05, 0.4] (迭代至稳定); H 组(h:*) ≤ 0.02 硬上限。
  收敛判定: 读最近 10 个历史版本, 相邻两版全引擎 |Δw| 均 <1e-3 → converged=true。

【防过拟合与 cold start】
  --train-end YYYY-MM-DD: 只用 ≤train-end 且已 settled 的记录训练
    (settled 语义: pick_log.settled=1; predictions 口径 = T+1 收益可完整计算)。
  样本不足 (settled 记录总数 < --min-samples, 默认 30) → 拒绝更新,
  输出 reason=insufficient_samples, 不落盘新版本。当前 pick_log 为 0 行,
  唯一信号源为 predictions 快照 → 单引擎时 r̄=r_i, Δ=0, 权重数值不变,
  如实标注 single_engine_no_relative_update, 版本仅作链起点落盘。

【新增声明】新增文件 (2026-10-23), 不修改任何既有文件; 权重目录
  scripts/engines/weights/ 由本文件 mkdir 创建, 只写 v{n}.json 与 latest.json。
  无前视: 只用 asof/train-end 之前已 settled 的记录。标准库优先,
  duckdb 为可选依赖。不打印任何 token/凭据。

CLI:
  python3 feedback_updater.py                       # 默认 asof=今日, lookback=60
  python3 feedback_updater.py --asof 2026-10-16 --lookback 60
  python3 feedback_updater.py --train-end 2026-09-01 --dry-run
  python3 feedback_updater.py --out /tmp/w.json     # 额外写指定路径
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "feedback_updater.json"))
AIV_DATA = os.environ.get(
    "STOCKS_AI_VIEW_DATA",
    os.path.abspath(os.path.join(HERE, "..", "..", "data")))
KLINE_BASE = os.environ.get(
    "STOCKS_DATA_KLINE",
    os.path.expanduser("~/workspace/stocks-data-kline/data"))
BULK_DIR = os.path.join(KLINE_BASE, "bulk")
_STOCKDB = os.environ.get("STOCKS_STOCKDB") or os.path.expanduser("~/stockdb")
DUCKDB_PATH = os.path.join(_STOCKDB, "picks.duckdb")
WEIGHTS_DIR = os.path.join(HERE, "weights")

ENGINE = "feedback_updater v1"
LR = 0.1                    # 乘性更新学习率
W_MIN, W_MAX = 0.05, 0.4    # 单引擎权重截断
H_HARD_CAP = 0.02           # H 组(h:*) 硬上限, 写死
CONVERGE_ROUNDS = 10        # 连续 N 轮 |Δw|<1e-3 → converged
CONVERGE_EPS = 1e-3
SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
          "20", "30", "60", "68"]


# ─── 数据层 (只读) ──────────────────────────────────────────────
def load_picklog(train_end, lookback):
    """读 DuckDB pick_log 已 settled 记录, 返回 (rows, status)。
    duckdb 不可用/无库 → 降级空表。"""
    try:
        import duckdb
    except ImportError:
        return [], "degraded: duckdb module not installed, pick_log skipped"
    if not os.path.exists(DUCKDB_PATH):
        return [], f"degraded: {DUCKDB_PATH} missing, pick_log skipped"
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        rows = con.execute(
            """SELECT pick_date, symbol, kind, ret_open_close, ret_open_high,
                      ret_t2, verdict
               FROM pick_log
               WHERE settled=1 AND kind<>'bcard' AND pick_date<=?""",
            [train_end]).fetchall()
        con.close()
    except Exception as e:
        return [], f"degraded: pick_log query failed: {e}"
    out = []
    for pd, sym, kind, roc, roh, rt2, verdict in rows:
        out.append({"date": str(pd), "symbol": sym, "engine": f"picklog:{kind}",
                    "ret_t1": roc, "ret_t2": rt2, "verdict": verdict})
    if lookback and out:
        lo = (datetime.strptime(train_end, "%Y-%m-%d").date()
              - timedelta(days=lookback)).isoformat()
        out = [r for r in out if r["date"] >= lo]
    return out, f"ok ({len(out)} settled rows <= {train_end})"


def load_price_matrix():
    """扫分片日线: {code: [(date, close), ...] 升序} (全市场, 同时用于基准)。"""
    data = {}
    for shard in SHARDS:
        path = os.path.join(BULK_DIR, f"day_qfq_{shard}.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) != 7:
                    continue
                data.setdefault(p[0], []).append((p[1], float(p[3])))
    for v in data.values():
        v.sort()
    return data


def fwd_ret(series, idx_map, date, horizon):
    """series: [(date,close)] 升序; idx_map: {date: pos} 预建索引 (O(1) 查询)。
    ≤date 最近交易日收盘买入, T+horizon 卖出。数据不足 → None (未 settled)。"""
    pos = idx_map.get(date)
    if pos is None:
        # date 非交易日: 取前一交易日
        pos = None
        for i in range(len(series) - 1, -1, -1):
            if series[i][0] <= date:
                pos = i
                break
        if pos is None:
            return None
    if pos + horizon >= len(series):
        return None
    base = series[pos][1]
    if not base:
        return None
    return (series[pos + horizon][1] / base - 1) * 100


def snapshot_eval(engine_key, train_end, lookback, top_pct, prices, idx_maps):
    """predictions/<engine_key>_*.json 快照高分组 T+1/5/10 复盘 + 全市场等权基准。

    通用化: engine_v5 / tomorrow_candidate / morning_pick 共用同一口径
    (T 收盘→T+N 收盘, close→close; 快照 top 行均带 code+score)。
    返回 (stat, settled_picks, status)。"""
    snaps = {}
    prefix = engine_key + "_"
    for fp in glob.glob(os.path.join(AIV_DATA, "predictions",
                                     prefix + "*.json")):
        d = os.path.basename(fp)[len(prefix):-len(".json")]
        snaps[d] = fp
    if not snaps:
        return None, 0, f"no {prefix}*.json snapshots"
    dates = sorted(d for d in snaps if d <= train_end)
    dates = dates[-lookback:] if lookback else dates
    if not dates:
        return None, 0, f"no snapshot <= {train_end}"

    per_day, settled_picks = [], 0
    for d in dates:
        try:
            with open(snaps[d], encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue
        top = snap.get("top") or []
        if not top:
            continue
        n_top = max(1, int(len(top) * top_pct))
        g1, g5, g10, all1, all5, all10 = [], [], [], [], [], []
        for row in top[:n_top]:
            s = prices.get(row.get("code"))
            if not s:
                continue
            for h, acc in ((1, g1), (5, g5), (10, g10)):
                r = fwd_ret(s, idx_maps[row["code"]], d, h)
                if r is not None:
                    acc.append(r)
        settled_picks += len(g1)  # T+1 可算 = settled
        for code, s in prices.items():
            im = idx_maps[code]
            if d not in im:  # 基准只取当日实际有交易的票, O(1)
                continue
            for h, acc in ((1, all1), (5, all5), (10, all10)):
                r = fwd_ret(s, im, d, h)
                if r is not None:
                    acc.append(r)
        if g1 and all1:
            per_day.append({"date": d,
                            "group": {"t1": g1, "t5": g5, "t10": g10},
                            "bench": {"t1": all1, "t5": all5, "t10": all10}})
    if not per_day:
        return None, 0, "no settled snapshot day (T+1 computable)"

    def agg(key, field):
        vals = [x for day in per_day for x in day[key][field]]
        if not vals:
            return None
        return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4)}

    stat = {"days": len(per_day), "date_from": per_day[0]["date"],
            "date_to": per_day[-1]["date"], "top_pct": top_pct}
    for field in ("t1", "t5", "t10"):
        g, b = agg("group", field), agg("bench", field)
        stat[field] = {"group": g, "bench": b,
                       "excess": round(g["mean"] - b["mean"], 4)
                       if g and b else None}
    return stat, settled_picks, f"ok ({len(per_day)} settled snapshot days)"


# ─── 权重 I/O 与更新 ────────────────────────────────────────────
def load_weights_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("weights") or {}
    except Exception:
        return None


def next_version():
    vs = []
    for fp in glob.glob(os.path.join(WEIGHTS_DIR, "v*.json")):
        b = os.path.basename(fp)[1:-len(".json")]
        if b.isdigit():
            vs.append(int(b))
    return (max(vs) + 1) if vs else 1


def clamp_weights(w):
    """L1 归一 + 截断 [W_MIN, W_MAX] + H 组 ≤0.02, 迭代至稳定。"""
    w = {k: max(0.0, float(v)) for k, v in w.items()}
    for _ in range(20):
        tot = sum(w.values()) or 1.0
        w = {k: v / tot for k, v in w.items()}
        capped = {}
        for k, v in w.items():
            cap = H_HARD_CAP if k.startswith("h:") else W_MAX
            floor = min(W_MIN, cap)
            capped[k] = min(max(v, floor), cap)
        w = capped
    tot = sum(w.values()) or 1.0
    return {k: round(v / tot, 6) for k, v in w.items()}


def converged_recently():
    """最近 CONVERGE_ROUNDS+1 个版本相邻 |Δw| 全 < EPS → True。"""
    def _v(fp):
        b = os.path.basename(fp)[1:-5]
        return int(b) if b.isdigit() else 0
    files = sorted(glob.glob(os.path.join(WEIGHTS_DIR, "v*.json")), key=_v)
    if len(files) < CONVERGE_ROUNDS + 1:
        return False, f"only {len(files)} versions (<{CONVERGE_ROUNDS + 1})"
    prev = None
    for fp in files[-(CONVERGE_ROUNDS + 1):]:
        w = load_weights_file(fp)
        if w is None:
            return False, "version file unreadable"
        if prev is not None:
            keys = set(prev) | set(w)
            if any(abs(w.get(k, 0) - prev.get(k, 0)) >= CONVERGE_EPS
                   for k in keys):
                return False, "recent rounds not all <1e-3"
        prev = w
    return True, f"last {CONVERGE_ROUNDS} rounds |Δw|<{CONVERGE_EPS}"


# ─── 主流程 ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description=ENGINE + " — 复盘胜率 → 乘性权重更新 (版本化)")
    ap.add_argument("--asof", default=None,
                    help="复盘截止日 YYYY-MM-DD (默认今日); 只用 ≤asof 已 settled 记录")
    ap.add_argument("--lookback", type=int, default=60,
                    help="回看窗口 (pick_log 按自然日 / 快照按最近 N 个交易日)")
    ap.add_argument("--train-end", default=None,
                    help="训练窗截止 YYYY-MM-DD (防过拟合, 默认=asof; 不得晚于 asof)")
    ap.add_argument("--min-samples", type=int, default=30,
                    help="settled 记录数下限, 不足拒绝更新 (默认 30)")
    ap.add_argument("--top-pct", type=float, default=0.1,
                    help="predictions 高分组比例 (默认前 10%%)")
    ap.add_argument("--weights-file", default=None,
                    help="初始权重 JSON (默认 weights/latest.json, 缺则等权 cold_start)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算不落盘 (不写 weights/)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="结果额外写指定路径 ('--out -' 输出 stdout)")
    args = ap.parse_args()

    asof = args.asof or datetime.now(CST).date().isoformat()
    train_end = args.train_end or asof
    if train_end > asof:
        print(json.dumps({"engine": ENGINE,
                          "error": "train_end 不能晚于 asof (防前视)"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    degraded = []

    # ── 复盘统计 ──
    pick_rows, pick_status = load_picklog(train_end, args.lookback)
    if pick_status.startswith("degraded"):
        degraded.append(pick_status)
    sources = {
        "pick_log": {
            "status": pick_status, "db": DUCKDB_PATH,
            "convention": ("T+1 = 次日开盘买入→收盘 (ret_open_close, 表内既有口径); "
                           "表内远期口径仅 ret_t2, T+5/T+10 不可得, 如实缺席"),
            "excess_benchmark": ("同一 lookback 内全部 settled 记录平均 "
                                 "ret_open_close 胜率为基准 (自基准, 仅作分组相对比较)")},
        "engine_v5_predictions": {
            "dir": os.path.join(AIV_DATA, "predictions"),
            "convention": (f"T 日收盘买入 → T+1/5/10 收盘卖出; "
                           f"高分组 = 快照 score 前 {args.top_pct:.0%}; "
                           "基准 = 同期全市场等权收益(分片日线自算, 无既有基线文件)")},
        "tomorrow_candidate_snapshots": {
            "dir": os.path.join(AIV_DATA, "predictions"),
            "convention": "同 engine_v5 (predictions/tomorrow_candidate_*.json)"},
        "morning_pick_snapshots": {
            "dir": os.path.join(AIV_DATA, "predictions"),
            "convention": "同 engine_v5 (predictions/morning_pick_*.json)"},
    }

    # 价格矩阵只载一次, 多引擎快照复盘共用 (O(1) 索引预建)
    prices, idx_maps = None, None
    snap_stats, snap_settled = {}, {}
    for eng in ("engine_v5", "tomorrow_candidate", "morning_pick"):
        if prices is None and glob.glob(os.path.join(
                AIV_DATA, "predictions", eng + "_*.json")):
            prices = load_price_matrix()
            idx_maps = {c: {dt: i for i, (dt, _) in enumerate(s)}
                        for c, s in prices.items()}
        if prices is None:
            snap_stats[eng], snap_settled[eng] = None, 0
            sources[eng + "_predictions" if eng == "engine_v5"
                    else eng + "_snapshots"]["status"] = (
                f"no {eng}_*.json snapshots")
            continue
        st, n, msg = snapshot_eval(eng, train_end, args.lookback,
                                   args.top_pct, prices, idx_maps)
        snap_stats[eng], snap_settled[eng] = st, n
        sources[eng + "_predictions" if eng == "engine_v5"
                else eng + "_snapshots"]["status"] = msg

    pick_stats = {}
    by_kind = {}
    for r in pick_rows:
        by_kind.setdefault(r["engine"], []).append(r)
    bench_vals = [r["ret_t1"] for r in pick_rows if r["ret_t1"] is not None]
    bench_wr = (sum(1 for v in bench_vals if v > 0) / len(bench_vals)
                if bench_vals else None)
    for k, rows in by_kind.items():
        vals = [r["ret_t1"] for r in rows if r["ret_t1"] is not None]
        if not vals:
            continue
        pick_stats[k] = {
            "n": len(vals),
            "t1": {"group": {"n": len(vals),
                             "mean": round(sum(vals) / len(vals), 4),
                             "win_rate": round(sum(1 for v in vals if v > 0)
                                               / len(vals), 4)},
                   "bench": {"win_rate": round(bench_wr, 4)
                             if bench_wr is not None else None}}}

    engine_r, evidence = {}, {}
    for k, s in pick_stats.items():
        b = s["t1"]["bench"]["win_rate"]
        if b is not None:
            engine_r[k] = round(s["t1"]["group"]["win_rate"] - b, 4)
            evidence[k] = {"source": "pick_log", **s}
    for eng, ev in snap_stats.items():
        if ev and ev.get("t1") and ev["t1"]["group"] and ev["t1"]["bench"]:
            engine_r[eng] = round(ev["t1"]["group"]["win_rate"]
                                  - ev["t1"]["bench"]["win_rate"], 4)
            evidence[eng] = {"source": "predictions_snapshots", **ev}

    total_settled = (sum(s["n"] for s in pick_stats.values())
                     + sum(snap_settled.values()))

    # ── 样本不足拒绝更新 ──
    if total_settled < args.min_samples:
        _emit({
            "engine": ENGINE,
            "ts": datetime.now(CST).isoformat(timespec="seconds"),
            "asof": asof, "train_end": train_end, "lookback": args.lookback,
            "updated": False, "reason": "insufficient_samples",
            "settled_records": total_settled, "min_samples": args.min_samples,
            "engine_r": engine_r, "evidence": evidence,
            "sources": sources, "degraded": degraded or None}, args)
        return

    # ── 乘性更新 ──
    w0 = (load_weights_file(args.weights_file) if args.weights_file
          else load_weights_file(os.path.join(WEIGHTS_DIR, "latest.json")))
    cold_start = not w0
    keys = sorted(set(engine_r) | set(w0 or {}))
    if cold_start:
        w0 = {k: 1.0 / len(keys) for k in keys} if keys else {}
        degraded.append("无历史权重, 等权初始化 (cold_start=true)")

    r_bar = sum(engine_r.values()) / len(engine_r) if engine_r else 0.0
    single = len(engine_r) <= 1
    w_new = {}
    for k in keys:
        r = engine_r.get(k, r_bar)  # 本轮无复盘数据的引擎取 r̄ → 因子 1, 不动
        w_new[k] = w0.get(k, 1.0 / len(keys)) * math.exp(LR * (r - r_bar))
    w_new = clamp_weights(w_new)
    delta = {k: round(w_new.get(k, 0) - w0.get(k, 0), 6) for k in keys}
    conv, conv_note = converged_recently()

    if single:
        degraded.append("单引擎阶段: r̄=r_i, 乘性因子=1, 权重数值不变, "
                        "版本仅作链起点落盘 (single_engine_no_relative_update)")
    else:
        pass  # 各引擎历史胜率在下方 result["engine_win_rates_t1"] 输出

    result = {
        "engine": ENGINE,
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "asof": asof, "train_end": train_end, "lookback": args.lookback,
        "updated": not args.dry_run, "dry_run": bool(args.dry_run),
        "cold_start": cold_start, "settled_records": total_settled,
        "update_rule": (f"w_i ← w_i·exp({LR}×(r_i−r̄)), L1 归一, "
                        f"截断[{W_MIN},{W_MAX}], h:* ≤{H_HARD_CAP}; "
                        "r_i = T+1 胜率超额 (口径见 sources)"),
        "r_bar": round(r_bar, 4), "engine_r": engine_r,
        "engine_win_rates_t1": ({k: evidence[k]["t1"]["group"]["win_rate"]
                                 for k in evidence if k in engine_r}
                                if engine_r else None),
        "single_engine_no_relative_update": single,
        "weights_before": {k: round(v, 6) for k, v in w0.items()},
        "weights_after": w_new, "delta": delta,
        "converged": conv, "converge_note": conv_note,
        "evidence": evidence, "sources": sources,
        "degraded": degraded or None,
    }

    if not args.dry_run:
        try:
            os.makedirs(WEIGHTS_DIR, exist_ok=True)
            v = next_version()
            result["version"] = v
            payload = dict(result)
            payload["weights"] = w_new
            for name in (f"v{v}.json", "latest.json"):
                with open(os.path.join(WEIGHTS_DIR, name), "w",
                          encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                    f.write("\n")
            result["weights_dir"] = WEIGHTS_DIR
        except OSError as e:
            result["updated"] = False
            result["reason"] = f"weights_dir_write_blocked: {e}"
            result["degraded"] = (result.get("degraded") or []) + [
                "weights/ 目录写入失败, 退化为仅 --out 输出; 请检查目录租约/权限"]
    _emit(result, args)


def _emit(result, args):
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        d = os.path.dirname(os.path.abspath(args.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("->", args.out, file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
