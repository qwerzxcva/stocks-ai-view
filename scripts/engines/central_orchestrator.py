#!/usr/bin/env python3
"""
central_orchestrator.py — 中枢编排器 v1 (新增文件, 不改任何既有文件)

【四职责一体化】
  1. regime 判定: 子进程调同目录 regime_router.py, 解析其 --out 临时 JSON
     (临时文件放 /tmp), 取 regime/phase/regime_gate/routing(A-H 八组)。
  2. 路由: 按 routing 表决定本次运行哪些模块组; --groups 可覆盖。
     serial_before_all 组先行, parallel 组用线程池并发(各自仍是子进程),
     serial_after_X 组在其前置组完成后运行; 每组独立子进程 + 超时,
     单组失败/超时降级为 skipped 并在 degraded 中标注, 不中断整体。
     组 → 模块绑定 (显式映射, 本文件唯一事实源):
       A 大盘:    cycle_grid_tester + 指数价位(price_level --index)
       B 个股:    engine_v5(子进程 --bulk --limit N --main-board-only, 可 --no-v5 关闭)
                  + tomorrow_candidate_engine + morning_pick_engine + 个股价位
       C 异动:    market_anomaly_engine
       D 消息:    无本地引擎 (仅有快照数据) → skipped, 预留
       E 数据质检: 内联 shard 覆盖检查 (只读)
       F 资金:    历史资金流为已知缺口 → skipped, 预留
       G 行业:    行业共振已内嵌于 tomorrow_candidate 的 sector 维 → aggregated
       H 另类:    无 → skipped (权重硬上限 0.02 写死)
  3. 集成评分三轮:
     ① 引擎级: 同族引擎 ensembling, 权重 w_k = softmax(α·s_k),
        s_k = 该引擎近 N 日复盘 T+1 胜率超额 (来自 --weights-file,
        由 feedback_updater.py 维护); weights 文件缺失 → 等权 + cold_start=true。
     ② 维度级: 个股在引擎族内聚合维度 vote: 对口引擎权重 0.5, 其余辅助引擎
        均分余量 0.5; 跨族合成 D_j (j ∈ 个股/价位/异动/周期/外围) 时以
        regime_router routing 权重档为组权重, H 组硬上限 0.02 写死。
     ③ 终审: S = regime_gate × Σ v_j·D_j (v_j 归一化), 输出终审榜单 top N。
     LLM 裁决钩子: 若 scripts/engines/llm_pick_arbiter.py 存在则子进程调用
        (真实 CLI 已核实: --input - 读 stdin, 输入 {top30:[{code,S,维度分,regime}]},
        输出 stdout JSON {picks:[{code,adjust,...}], model_chain}, adjust 由其内部
        封顶 [-max,+max]=10, 本端再截断保险); 不存在 → 跳过, 输出标注 llm=skipped。
        本文件只 os.path.exists 判断 + subprocess, 不 import。S 为百分制
        (S = 100×gate×Σv·D), 与 arbiter 的 ±10 adjust 同量纲。
  4. 复盘反馈: 末尾子进程调同目录 feedback_updater.py (--out 放 /tmp) 刷新权重,
     生效于下一轮运行; 本轮评分用的是本轮启动时读到的权重版本。

【新增声明】新增文件 (2026-10-23), 不修改任何既有文件 (尤其 engine_v5.py、
  前端、data/、.github/workflows/); 对批 1 引擎(engine_v5 及各 *_engine.py)
  一律 subprocess + JSON 文件接口, 不 import, 无共享状态。
  主板过滤只裁输出层 (前缀 600/601/603/605/000/001/002, 兼容 sh/sz 前缀),
  打分覆盖全市场。只用 --asof 及之前数据, 无前视。标准库实现。

CLI:
  python3 central_orchestrator.py --asof 2026-10-16 --top 20
  python3 central_orchestrator.py --groups B,E --limit 300 --no-v5 --top 10
  python3 central_orchestrator.py --dry-run            # 只输出路由与权重
  python3 central_orchestrator.py --weights-file scripts/engines/weights/latest.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "central_orchestrator.json"))
AIV_DATA = os.environ.get(
    "STOCKS_AI_VIEW_DATA",
    os.path.abspath(os.path.join(HERE, "..", "..", "data")))
KLINE_BASE = os.environ.get(
    "STOCKS_DATA_KLINE",
    os.path.expanduser("~/workspace/stocks-data-kline/data"))
BULK_DIR = os.path.join(KLINE_BASE, "bulk")

ENGINE = "central_orchestrator v1"
MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002")
H_GROUP_HARD_CAP = 0.02
LLM_ARBITER = os.path.join(HERE, "llm_pick_arbiter.py")
DEFAULT_WEIGHTS_FILE = os.path.join(HERE, "weights", "latest.json")
DEFAULT_SCAN_LIMIT = 1500   # 抽样规模上限, 保 <3 分钟; 全市场传 --limit 0
ENSEMBLE_ALPHA = 1.0        # softmax(α·s_k), s_k 为 T+1 胜率超额

# 组 → 模块绑定 (显式映射)
GROUP_ENGINES = {
    "A": ["cycle_grid_tester", "price_level_index"],
    "B": ["engine_v5", "tomorrow_candidate", "morning_pick", "price_level"],
    "C": ["market_anomaly"],
    "D": [],   # 无本地消息引擎
    "E": ["shard_check"],
    "F": [],   # 资金流历史缺口
    "G": [],   # 行业共振内嵌于 tomorrow_candidate sector 维
    "H": [],   # 另类无
}

# 个股引擎族 (引擎级 ensembling 的第一轮作用域) 及其对口维度
STOCK_FAMILY = ["engine_v5", "tomorrow_candidate", "morning_pick"]
FAMILY_DIMS = {
    "engine_v5": {"ma", "rsi", "macd", "kdj", "volume", "boll", "trend"},
    "tomorrow_candidate": {"ma", "rsi", "macd", "volume", "sector",
                           "rel_strength"},
    "morning_pick": {"gap_env", "breakout", "pullback", "momentum",
                     "vol_energy", "moneyflow"},
}
# 复盘权重 key 映射: 本编排器引擎名 → feedback_updater 权重键
REVIEW_KEY = {"engine_v5": "engine_v5",
              "tomorrow_candidate": "tomorrow_candidate",  # 快照复盘已接入
              "morning_pick": "morning_pick"}              # 快照复盘已接入


# ─── 工具 ───────────────────────────────────────────────────────
def run_engine(cmd, timeout, tag):
    """子进程跑引擎, 返回 (dict|None, status)。stdout 有噪音时用 --out 文件。"""
    out_fd, out_path = tempfile.mkstemp(prefix=f"co_{tag}_", suffix=".json",
                                        dir="/tmp")
    os.close(out_fd)
    try:
        proc = subprocess.run(cmd + ["--out", out_path],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None, f"exit={proc.returncode}: {proc.stderr.strip()[:200]}"
        with open(out_path, encoding="utf-8") as f:
            return json.load(f), "ok"
    except subprocess.TimeoutExpired:
        return None, f"timeout>{timeout}s"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def norm_prefix(code):
    c = (code or "").lower()
    return c[2:] if c.startswith(("sh", "sz", "bj")) else c


def is_main_board(code):
    return norm_prefix(code).startswith(MAIN_BOARD_PREFIX)


def softmax_weights(scores, keys):
    """w_k = softmax(α·s_k); 缺失 s_k 的键取 0 (等温)。"""
    exps = {k: math_exp(ENSEMBLE_ALPHA * scores.get(k, 0.0)) for k in keys}
    tot = sum(exps.values()) or 1.0
    return {k: v / tot for k, v in exps.items()}


def math_exp(x):
    # 防溢出截断
    import math as _m
    return _m.exp(max(-30.0, min(30.0, x)))


# ─── 组执行器 ───────────────────────────────────────────────────
def exec_a(asof, args):
    out = {"dim_signals": {}}
    d, st = run_engine(["python3", os.path.join(HERE, "cycle_grid_tester.py"),
                        "--asof", asof, "--sims", str(args.sims)],
                       timeout=120, tag="A_cycle")
    if d:
        g = d.get("grid_hypothesis_test") or {}
        out["cycle"] = {"p_binom": g.get("binom_pvalue_upper_tail"),
                        "p_mc": g.get("monte_carlo_shift_pvalue"),
                        "hit_rate": g.get("hit_rate"),
                        "data_asof": (d.get("data_asof") or {}).get("index")}
        # 周期维度信号: 网格聚集显著(p<0.05)视为结构性证据, 方向中性
        p = g.get("monte_carlo_shift_pvalue")
        out["dim_signals"]["cycle"] = 0.2 if (p is not None and p < 0.05) else 0.0
    else:
        out["cycle_error"] = st
    d, st = run_engine(["python3", os.path.join(HERE, "price_level_engine.py"),
                        "--index", "sh000001", "--asof", asof],
                       timeout=60, tag="A_plevel")
    if d:
        lv = d.get("levels") or {}
        out["index_levels"] = {"current": lv.get("current"),
                               "support": lv.get("support"),
                               "resistance": lv.get("resistance"),
                               "data_asof": d.get("data_asof")}
    else:
        out["index_levels_error"] = st
    return out


def exec_b(asof, args, pool_codes):
    out = {"engine_picks": {}, "dim_signals": {}}
    if not args.no_v5:
        cmd = ["python3", os.path.abspath(os.path.join(HERE, "..",
                                                       "engine_v5.py")),
               "--limit", str(args.limit), "--main-board-only"]
        if args.no_ext:
            cmd.append("--no-ext")
        d, st = run_engine(cmd, timeout=args.timeout_engine, tag="B_v5")
        if d:
            out["engine_picks"]["engine_v5"] = {
                "top": d.get("top") or [],
                "data_asof": (d.get("meta") or {}).get("asof") or d.get("ts"),
                "score_field": "score", "code_field": "code"}
        else:
            out["engine_picks"]["engine_v5"] = {"error": st}
    d, st = run_engine(["python3", os.path.join(HERE,
                                                "tomorrow_candidate_engine.py"),
                        "--asof", asof, "--limit", str(args.limit),
                        "--top", str(args.candidate_top)],
                       timeout=args.timeout_engine, tag="B_tomorrow")
    if d:
        out["engine_picks"]["tomorrow_candidate"] = {
            "top": d.get("candidates") or [], "data_asof": d.get("data_asof"),
            "score_field": "score", "code_field": "code"}
    else:
        out["engine_picks"]["tomorrow_candidate"] = {"error": st}
    d, st = run_engine(["python3", os.path.join(HERE, "morning_pick_engine.py"),
                        "--date", asof, "--limit", str(args.limit),
                        "--top", str(args.candidate_top)],
                       timeout=args.timeout_engine, tag="B_morning")
    if d:
        out["engine_picks"]["morning_pick"] = {
            "top": d.get("picks") or [], "data_asof": d.get("data_asof"),
            "score_field": "score", "code_field": "code"}
    else:
        out["engine_picks"]["morning_pick"] = {"error": st}
    return out


def exec_c(asof, args):
    out = {"dim_signals": {}}
    d, st = run_engine(["python3", os.path.join(HERE,
                                                "market_anomaly_engine.py"),
                        "--asof", asof, "--limit", str(args.limit)],
                       timeout=args.timeout_engine, tag="C_anomaly")
    if d:
        out["anomalies"] = d.get("anomalies") or []
        out["data_asof"] = d.get("data_asof")
        # 异动维度信号: 高风险异动(index_gap_down/dt_flood/leader_crash) → 负
        neg = sum(1 for a in out["anomalies"]
                  if a.get("type") in ("index_gap_down", "dt_flood",
                                       "leader_crash"))
        pos = sum(1 for a in out["anomalies"]
                  if a.get("type") in ("index_gap_up", "zt_flood",
                                       "sector_surge", "dual_mainline"))
        out["dim_signals"]["anomaly"] = max(-1.0, min(1.0,
                                                      0.4 * (pos - neg)))
    else:
        out["anomalies_error"] = st
    return out


def exec_e(asof, args):
    """内联数据质检: 分片覆盖/新鲜度 (只读)。"""
    shards = sorted(glob.glob(os.path.join(BULK_DIR, "day_qfq_*.csv")))
    latest = None
    for fp in shards:
        try:
            with open(fp, "rb") as f:
                f.seek(max(0, os.path.getsize(fp) - 4096))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in reversed(tail.strip().split("\n")):
                p = line.split("|")
                if len(p) == 7:
                    latest = max(latest or "", p[1])
                    break
        except OSError:
            continue
    stale = latest is not None and latest < asof
    return {"shard_check": {"n_shards": len(shards), "bulk_latest": latest,
                            "stale_vs_asof": stale},
            "dim_signals": {"data_quality": -0.5 if stale else 0.0}}


# ─── LLM 裁决钩子 ───────────────────────────────────────────────
def _extract_json(text):
    """从可能被日志污染的 stdout 中提取最后一个平衡 JSON 对象 (深度计数)。"""
    end = len(text)
    while True:
        i = text.rfind("{", 0, end)
        if i < 0:
            return None
        depth, in_str, esc = 0, False, False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        break
        end = i  # 该候选解析失败, 向前找上一个 '{'


def llm_arbitrate(top30, regime):
    """llm_pick_arbiter.py 存在才调 (真实接口: --input - stdin → stdout
    {picks:[{code,adjust}], model_chain})。返回 (adjust_map, status, meta)。
    adjust 截断 [-10,+10] 保险 (arbiter 内部已封顶)。"""
    if not os.path.exists(LLM_ARBITER):
        return {}, "skipped", None
    payload = {"top30": top30, "regime": regime}
    try:
        # --out -: arbiter 默认 --out 写文件, 必须显式让 JSON 走 stdout;
        # 否则 stdout 只有进度日志行 → json.loads 必炸 (R3 llm 恒 error 根因)
        proc = subprocess.run(["python3", LLM_ARBITER,
                               "--input", "-", "--mode", "auto",
                               "--fallback", "rule", "--out", "-"],
                              input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return {}, f"error: exit={proc.returncode}", None
        try:
            raw = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # stdout 仍被污染 → 兜底提取最后一段平衡 JSON 对象
            raw = _extract_json(proc.stdout)
            if raw is None:
                return {}, "error: stdout not JSON (polluted)", None
        adj = {}
        for p in raw.get("picks") or []:
            try:
                adj[p["code"]] = max(-10.0, min(10.0, float(p["adjust"])))
            except (KeyError, TypeError, ValueError):
                continue
        mc = raw.get("model_chain") or {}
        status = ("ok:" + str(mc.get("actual"))) if not mc.get("degraded") \
            else ("degraded:" + str(mc.get("actual")))
        return adj, status, {"model_chain": mc}
    except Exception as e:
        return {}, f"error: {type(e).__name__}: {e}", None


# ─── 集成评分 ───────────────────────────────────────────────────
def aggregate_stock_family(engine_picks, review_weights):
    """第一轮(引擎级 softmax ensembling) + 维度级(对口 0.5/辅助均分)。

    返回 {code: {"score", "dim_votes": {dim: weighted_vote}, "engines": [...]}}
    """
    by_code = {}
    avail = []
    for eng in STOCK_FAMILY:
        blk = engine_picks.get(eng) or {}
        if blk.get("top"):
            avail.append(eng)
            for row in blk["top"]:
                code = row.get(blk["code_field"])
                if not code:
                    continue
                rec = by_code.setdefault(code, {"engines": {}, "votes": {}})
                rec["engines"][eng] = {"score": row.get(blk["score_field"]),
                                       "name": row.get("name")}
                for v in row.get("votes") or []:
                    if isinstance(v, dict) and "indicator" in v:
                        rec["votes"].setdefault(eng, {})[v["indicator"]] = {
                            "vote": v.get("vote", 0),
                            "strength": v.get("strength", 0.0)}
                    elif isinstance(v, (int, float)):  # engine_v5 votes dict
                        pass
                # engine_v5 votes 为 {indicator: vote_int} dict
                if isinstance(row.get("votes"), dict):
                    for ind, vv in row["votes"].items():
                        rec["votes"].setdefault(eng, {})[ind.lower()] = {
                            "vote": vv, "strength": 1.0 if vv else 0.2}
    if not avail:
        return {}, {"cold_start": True, "reason": "no stock engine output"}

    s_k = {}   # 复盘胜率超额 → softmax
    cold = review_weights is None
    for eng in avail:
        rk = REVIEW_KEY.get(eng)
        s_k[eng] = (review_weights or {}).get(rk, 0.0) if rk else 0.0
    w_eng = softmax_weights(s_k, avail)

    result = {}
    for code, rec in by_code.items():
        # 引擎级: 该票在出席引擎中的归一化 score 加权
        num = den = 0.0
        for eng in avail:
            e = rec["engines"].get(eng)
            if e and isinstance(e.get("score"), (int, float)):
                num += w_eng[eng] * e["score"] / 100.0
                den += w_eng[eng]
        base = num / den if den else 0.0
        # 维度级: 合并各引擎 vote, 对口引擎 0.5, 辅助均分 0.5
        all_dims = {}
        for eng, vmap in rec["votes"].items():
            others = [e for e in rec["votes"] if e != eng]
            for dim, v in vmap.items():
                primary = dim in FAMILY_DIMS.get(eng, set())
                if primary:
                    w = 0.5
                else:
                    w = 0.5 / max(1, len(others)) if others else 0.5
                d = all_dims.setdefault(dim, {"num": 0.0, "den": 0.0})
                d["num"] += w * v["vote"] * v["strength"]
                d["den"] += w
        dim_votes = {k: round(v["num"] / v["den"], 4) if v["den"] else 0.0
                     for k, v in all_dims.items()}
        result[code] = {"score": round(base * 100, 2),
                        "dim_votes": dim_votes,
                        "engines": rec["engines"]}
    meta = {"cold_start": cold, "ensemble_weights": {k: round(v, 4)
            for k, v in w_eng.items()},
            "alpha": ENSEMBLE_ALPHA,
            "review_winrate_excess": {k: round(v, 4) for k, v in s_k.items()}}
    return result, meta


def final_rank(by_code, group_outs, routing, gate, top, main_board_only):
    """终审: S = regime_gate × Σ v_j·D_j。

    组权重 v_j 取自 routing.weight (已含 gate 档); H ≤0.02 写死。
    只在实际运行/有信号的组内归一 (skipped 组不入分母, 避免稀释)。
    """
    # 组 → 归一化组权重 (仅实际出席组)
    raw = {}
    for g in group_outs:
        cfg = (routing or {}).get(g) or {}
        w = cfg.get("weight", 0.0)
        if g == "H":
            w = min(w, H_GROUP_HARD_CAP)
        raw[g] = w
    tot = sum(raw.values()) or 1.0
    vg = {g: w / tot for g, w in raw.items()}

    dim_sig = {}
    for g, out in group_outs.items():
        for k, val in (out.get("dim_signals") or {}).items():
            dim_sig.setdefault(g, {})[k] = val

    rows = []
    for code, rec in by_code.items():
        # D_stock: 个股族分 (0-100 → [-1,1] 近似: (score-50)/50)
        d_stock = (rec["score"] - 50.0) / 50.0
        s_parts = {"stock": d_stock}
        for g, sigs in dim_sig.items():
            for k, val in sigs.items():
                s_parts[f"{g}:{k}"] = val
        # 汇总: Σ v_j·D_j, 个股族归 B 组权重
        total = vg.get("B", 0.0) * d_stock
        used = {"B": d_stock}
        for g, sigs in dim_sig.items():
            if not sigs:
                continue
            dj = sum(sigs.values()) / len(sigs)
            total += vg.get(g, 0.0) * dj
            used[g] = round(dj, 4)
        S = 100.0 * gate * total  # 百分制, 与 LLM adjust ±10 同量纲
        rows.append({"code": code, "S_raw": round(total, 4),
                     "S": round(S, 2), "gate": gate,
                     "dim_votes": rec["dim_votes"],
                     "group_contrib": used,
                     "engines": rec["engines"],
                     "main_board": is_main_board(code)})
    rows.sort(key=lambda r: -r["S"])
    if main_board_only:
        rows = [r for r in rows if r["main_board"]]
    return rows[:top], vg


# ─── 主流程 ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description=ENGINE + " — regime 判定 / 路由 / 三轮集成评分 / 复盘反馈")
    ap.add_argument("--asof", default=None,
                    help="截止交易日 YYYY-MM-DD (默认本地数据最新日)")
    ap.add_argument("--top", type=int, default=20, help="终审榜单只数 (默认 20)")
    ap.add_argument("--groups", default=None,
                    help="覆盖路由, 如 'A,B,E'; 默认按 regime_router routing 全组")
    ap.add_argument("--weights-file", default=DEFAULT_WEIGHTS_FILE,
                    help="复盘权重 JSON (feedback_updater 产出; 缺则等权 cold_start)")
    ap.add_argument("--limit", type=int, default=DEFAULT_SCAN_LIMIT,
                    help=f"全市场扫描抽样上限 (0=全市场; 默认 {DEFAULT_SCAN_LIMIT}, "
                         "保 <3 分钟)")
    ap.add_argument("--candidate-top", type=int, default=30,
                    help="个股族各引擎各自输出只数 (默认 30)")
    ap.add_argument("--price-levels-top", type=int, default=10,
                    help="终审入池后补价位的只数 (默认 10, 0=不补)")
    ap.add_argument("--no-v5", action="store_true",
                    help="跳过 engine_v5 子进程 (省时间)")
    ap.add_argument("--no-ext", action="store_true",
                    help="engine_v5 --no-ext (禁用外部数据源增强)")
    ap.add_argument("--no-feedback", action="store_true",
                    help="跳过末尾 feedback_updater 调用")
    ap.add_argument("--dry-run", action="store_true",
                    help="只输出路由与权重, 不跑任何引擎子进程")
    ap.add_argument("--dry-keep-tmp", action="store_true",
                    help="保留 /tmp/co_* 引擎输出临时文件 (调试用; 默认用完即删)")
    ap.add_argument("--sims", type=int, default=200,
                    help="cycle_grid_tester Monte Carlo 次数 (默认 200)")
    ap.add_argument("--timeout-engine", type=int, default=180,
                    help="单引擎子进程超时秒 (默认 180)")
    ap.add_argument("--all-boards", action="store_true",
                    help="终审榜单不裁主板 (默认只输出主板)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 路径 (默认 DEFAULT_OUT) ('--out -' 输出 stdout)")
    args = ap.parse_args()
    scan_limit = args.limit or None  # 0 → None = 全市场

    degraded = []
    ts0 = datetime.now(CST)

    # ── 职责 1: regime 判定 ──
    asof = args.asof or ts0.date().isoformat()
    rr, st = run_engine(["python3", os.path.join(HERE, "regime_router.py"),
                         "--asof", asof, "--phase-lookback", "60",
                         "--explain"],
                        timeout=180, tag="regime")
    if not rr:
        print(json.dumps({"engine": ENGINE, "error": f"regime_router: {st}"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    asof = rr.get("asof") or asof
    regime, gate = rr.get("regime"), rr.get("gate_applied", 1.0)
    routing = rr.get("routing") or {}

    # ── 职责 2: 路由 ──
    if args.groups:
        groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]
        route_src = "cli_override"
    else:
        groups = [g for g, cfg in routing.items()
                  if GROUP_ENGINES.get(g) or g in ("E",)]
        route_src = "regime_router.routing"
    routing_record = {"source": route_src, "groups_planned": groups,
                      "runs": {}}

    # 权重 (引擎级 ensembling 用)
    review_w = None
    wf = args.weights_file
    if wf and os.path.exists(wf):
        try:
            review_w = (json.load(open(wf, encoding="utf-8"))
                        .get("engine_r") or {})
        except Exception as e:
            degraded.append(f"weights file unreadable: {e}")
    if review_w is None:
        degraded.append(f"无复盘权重文件 ({wf}), 引擎级 ensembling 等权 "
                        "(cold_start=true)")

    if args.dry_run:
        result = {
            "engine": ENGINE, "ts": ts0.isoformat(timespec="seconds"),
            "dry_run": True, "asof": asof,
            "regime": {"regime": regime, "regime_score": rr.get("regime_score"),
                       "confidence": rr.get("confidence"),
                       "phase": rr.get("phase"), "gate": gate,
                       "routing": routing},
            "routing": routing_record,
            "groups_binding": {g: GROUP_ENGINES.get(g, []) for g in groups},
            "weights": {"file": wf if (wf and os.path.exists(wf)) else None,
                        "engine_r": review_w, "cold_start": review_w is None},
            "llm": "skipped" if not os.path.exists(LLM_ARBITER) else "present",
            "data_asof": rr.get("data_asof"),
            "degraded": degraded or None,
        }
        _emit(result, args)
        return

    # ── 组执行: serial_before_all → parallel → serial_after_* ──
    group_outs = {}
    before = [g for g in groups
              if (routing.get(g) or {}).get("mode") == "serial_before_all"
              and GROUP_ENGINES.get(g)]
    middle = [g for g in groups
              if (routing.get(g) or {}).get("mode", "parallel") == "parallel"
              and GROUP_ENGINES.get(g)]
    after = [g for g in groups
             if (routing.get(g) or {}).get("mode", "").startswith("serial_after")
             and GROUP_ENGINES.get(g)]
    skipped = [g for g in groups
               if g not in before + middle + after]
    for g in skipped:
        routing_record["runs"][g] = {"status": "skipped",
                                     "reason": "no local engine bound "
                                     "(D 消息/F 资金/G 内嵌/H 另类)",
                                     "engines": GROUP_ENGINES.get(g, [])}

    exec_map = {"A": exec_a, "B": exec_b, "C": exec_c, "E": exec_e}

    def _run_group(g):
        fn = exec_map.get(g)
        if not fn:
            return g, {"dim_signals": {}}
        try:
            if g == "B":
                return g, fn(asof, args, None)
            return g, fn(asof, args)
        except Exception as e:
            return g, {"error": f"{type(e).__name__}: {e}", "dim_signals": {}}

    for g in before:  # 串行前置 (如 bear 市 E 质检/A 风控)
        t0 = datetime.now(CST)
        g, out = _run_group(g)
        group_outs[g] = out
        routing_record["runs"][g] = {
            "status": "error" if out.get("error") else "ok",
            "mode": "serial_before_all",
            "elapsed_s": (datetime.now(CST) - t0).total_seconds(),
            "engines": GROUP_ENGINES.get(g, [])}

    with ThreadPoolExecutor(max_workers=max(1, len(middle))) as ex:
        for g, out in ex.map(_run_group, middle):
            group_outs[g] = out
            err = out.get("error")
            eng_errs = {k: v.get("error") for k, v in
                        (out.get("engine_picks") or {}).items()
                        if isinstance(v, dict) and v.get("error")}
            routing_record["runs"][g] = {
                "status": "error" if err else
                          ("partial" if eng_errs else "ok"),
                "mode": "parallel",
                "engine_errors": eng_errs or None,
                "engines": GROUP_ENGINES.get(g, [])}

    for g in after:
        g, out = _run_group(g)
        group_outs[g] = out
        routing_record["runs"][g] = {
            "status": "error" if out.get("error") else "ok",
            "mode": (routing.get(g) or {}).get("mode"),
            "engines": GROUP_ENGINES.get(g, [])}

    # ── 职责 3: 三轮集成评分 ──
    engine_picks = (group_outs.get("B") or {}).get("engine_picks") or {}
    by_code, r1_meta = aggregate_stock_family(engine_picks, review_w)
    rounds = {"engine_level": r1_meta, "dim_level": None, "final": None}

    if by_code:
        # 维度级摘要
        dims_seen = {}
        for rec in by_code.values():
            for d in rec["dim_votes"]:
                dims_seen[d] = dims_seen.get(d, 0) + 1
        rounds["dim_level"] = {
            "rule": "对口引擎权重 0.5, 辅助引擎均分余量 0.5; H 组 ≤0.02 写死",
            "dims_observed": dims_seen}

        rows, vg = final_rank(by_code, group_outs, routing, gate,
                              top=args.candidate_top,
                              main_board_only=False)  # 打分全市场
        # LLM 裁决钩子 (输入 top30 全市场口径)
        top30 = [{"code": r["code"], "S": r["S"],
                  "dim_votes": r["dim_votes"], "regime": regime}
                 for r in rows[:30]]
        llm_adj, llm_status, llm_meta = llm_arbitrate(top30, regime)
        for r in rows:
            adj = llm_adj.get(r["code"])
            if adj is not None:
                r["llm_adjust"] = adj
                r["S"] = round(r["S"] + adj, 2)  # 百分制同量纲相加
        if llm_adj:
            rows.sort(key=lambda r: -r["S"])

        # 价位补充 (终审入池前列, 并发子进程)
        if args.price_levels_top > 0 and rows:
            pl_codes = [r["code"] for r in rows[:args.price_levels_top]]
            pl = {}
            with ThreadPoolExecutor(max_workers=min(5, len(pl_codes))) as ex:
                for code, lv in ex.map(
                        lambda c: (c, run_engine(
                            ["python3", os.path.join(HERE,
                                                     "price_level_engine.py"),
                             "--code", c, "--asof", asof],
                            timeout=30, tag=f"pl_{c}")[0]),
                        pl_codes):
                    if lv and lv.get("levels"):
                        l = lv["levels"]
                        pl[code] = {"current": l.get("current"),
                                    "support": l.get("support"),
                                    "resistance": l.get("resistance"),
                                    "pivot": l.get("pivot"),
                                    "bottom_fishing": l.get("bottom_fishing")}
            for r in rows:
                if r["code"] in pl:
                    r["price_levels"] = pl[r["code"]]

        if not args.all_boards:
            rows_out = [r for r in rows if r["main_board"]][:args.top]
        else:
            rows_out = rows[:args.top]
        for r in rows_out:
            r.pop("main_board", None)
        rounds["final"] = {
            "formula": "S = 100 × regime_gate × Σ v_j·D_j (百分制, 与 LLM adjust ±10 同量纲)",
            "regime_gate": gate,
            "llm_detail": llm_meta,
            "group_weights_v": {k: round(v, 4) for k, v in vg.items()},
            "llm": llm_status,
            "llm_adjust_applied": len(llm_adj),
            "scored_universe": len(by_code),
            "output_filter": "main_board_only" if not args.all_boards
                             else "all_boards"}
    else:
        rows_out = []
        llm_status = "skipped"
        rounds["final"] = {"error": "no stock family output",
                           "llm": llm_status}
        degraded.append("个股族引擎全部缺席, 终审榜单为空")

    # ── 职责 4: 复盘反馈 (刷新权重, 生效于下一轮) ──
    fb_status = "skipped (--no-feedback)"
    if not args.no_feedback:
        fb, st = run_engine(["python3", os.path.join(HERE,
                                                     "feedback_updater.py"),
                             "--asof", asof],
                            timeout=600, tag="feedback")
        fb_status = ("ok" if fb and fb.get("updated") else
                     (f"no_update: {fb.get('reason')}" if fb else
                      f"error: {st}"))
        if fb and fb.get("reason") == "insufficient_samples":
            degraded.append("复盘样本不足, 权重未更新 "
                            f"(settled={fb.get('settled_records')})")

    result = {
        "engine": ENGINE,
        "ts": ts0.isoformat(timespec="seconds"),
        "elapsed_s": (datetime.now(CST) - ts0).total_seconds(),
        "asof": asof,
        "regime": {"regime": regime, "regime_score": rr.get("regime_score"),
                   "confidence": rr.get("confidence"), "phase": rr.get("phase"),
                   "phase_cn": rr.get("phase_cn"), "gate": gate,
                   "routing": routing,
                   "evidence": rr.get("evidence")},
        "routing": routing_record,
        "groups": {g: {"engines": GROUP_ENGINES.get(g, []),
                       "output_keys": sorted((group_outs.get(g) or {}).keys())}
                   for g in groups},
        "market_context": {
            "anomalies": (group_outs.get("C") or {}).get("anomalies"),
            "index_levels": (group_outs.get("A") or {}).get("index_levels"),
            "cycle": (group_outs.get("A") or {}).get("cycle"),
            "shard_check": (group_outs.get("E") or {}).get("shard_check")},
        "rounds": rounds,
        "top": rows_out,
        "llm": llm_status,
        "feedback_updater": fb_status,
        "data_asof": {"regime_router": rr.get("data_asof"),
                      "engines": {eng: (blk.get("data_asof") if isinstance(blk, dict)
                                        else None)
                                  for eng, blk in engine_picks.items()}},
        "degraded": degraded or None,
    }
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
