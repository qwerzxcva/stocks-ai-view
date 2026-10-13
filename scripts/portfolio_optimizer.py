#!/usr/bin/env python3
"""
portfolio_optimizer.py — 组合优化引擎 (风险预算下的权重再分配)

risk_engine 只做「单票风险预算 + 集中度上限」, 没有组合层面的最优权重:
  - 同行业扎堆时只是统一 ×0.7, 不保证行业暴露降到上限内
  - 单票超限后直接截断 (min(w, 25%)), 截断掉的权重不回补, 总仓位会缩水
  - 完全没有考虑两两相关性

本引擎补上 (全部可解释, 不引入 cvxpy 等求解器):
  1. 初始权重 = risk_engine 的风险平价权重 (risk_per_trade / vol)
  2. 行业暴露压缩: 某行业合计 > 40% 时该行业所有权重 ×0.8, 迭代至满足或 20 轮
  3. 单票上限: > 25% 的部分按比例分给未超限的票 (总权重守恒)
  4. 高相关惩罚: 20 日收益相关系数 > 0.7 的两票, 合计权重不超过 35%
  5. 输出 {weights, sector_exposure, iterations, converged}

输入: data/risk_plan.json (每票仓位 + 年化波动率)
      data/decision.json (cards, 取 industry; 缺失时回退 INDUSTRY_MAP)
      + stocks-data-kline bulk (算 20 日收益相关系数)
输出: data/portfolio_plan.json
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK, INDUSTRY_MAP
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = "/root/workspace/stocks-data-kline/data/bulk"
    INDUSTRY_MAP = "/root/workspace/stocks-data-live/data/meta/industry_map_tv.json"

MAX_SINGLE_POS = 0.25        # 单票仓位上限
MAX_SECTOR_POS = 0.40        # 单行业暴露上限
SECTOR_SHRINK = 0.8          # 行业超限时该行业权重缩放系数
MAX_ITERATIONS = 20          # 行业压缩最大轮数
CORR_WINDOW = 20             # 相关系数窗口 (交易日)
CORR_THRESHOLD = 0.7         # 高相关阈值
MAX_CORR_PAIR_POS = 0.35     # 高相关两票合计权重上限


# ─── bulk K 线解析 (两种格式) ────────────────────────────────────
def _parse_kline_line(line):
    """解析一行 bulk K 线 → (裸码, date, close); 失败返回 None。

    bulk 存在两种格式, 只按 '|' 全切分会漏掉逗号分隔的那批:
      sh600016|2026-10-12,3.5,3.56,3.59,3.5,3294332.0   (extra/recent)
      sz000610|2023-06-15|17.120|17.380|...            (00-09/stockdb/expand)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "|" not in line:
        return None
    code, rest = line.split("|", 1)
    fields = rest.replace("|", ",").split(",")
    if len(fields) < 4:
        return None
    code = code.strip()
    bare = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    try:
        return bare, fields[0], float(fields[3])
    except ValueError:
        return None


def load_close_series(codes, window=CORR_WINDOW):
    """加载指定股票最近 window 个交易日的收盘价序列 → {裸码: [close, ...]}"""
    want = {str(c).zfill(6) for c in codes}
    rows = defaultdict(list)
    seen = set()
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = _parse_kline_line(line)
                if parsed is None:
                    continue
                bare, d, close = parsed
                if bare not in want:
                    continue
                key = (bare, d)
                if key in seen:
                    continue
                seen.add(key)
                rows[bare].append((d, close))
    out = {}
    for bare, series in rows.items():
        series.sort(key=lambda r: r[0])
        if len(series) < window:
            continue
        out[bare] = [c for _, c in series[-window:]]
    return out


def corr(a, b):
    """皮尔逊相关系数; 样本不足或零方差返回 None。"""
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return num / (va * vb) ** 0.5


def load_industry_map():
    """TradingView 行业映射 → {裸码: industry}; 读不到返回 {}"""
    if not os.path.exists(INDUSTRY_MAP):
        return {}
    try:
        m = json.load(open(INDUSTRY_MAP, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for code, meta in (m.items() if isinstance(m, dict) else []):
        if isinstance(meta, dict):
            out[str(code).zfill(6)] = meta.get("industry") or meta.get("sector") or "unknown"
    return out


def resolve_industries(codes, imap):
    """每只票的行业: 查 INDUSTRY_MAP (TV), 查不到记 unknown"""
    out = {}
    for c in codes:
        code = str(c).zfill(6)
        out[code] = (imap.get(code) or "").strip() or "unknown"
    return out


# ─── 组合优化主流程 ─────────────────────────────────────────────
def optimize(weights, industries, corr_pairs):
    """迭代重加权。返回 (weights, iterations, converged, log)

    weights:     {code: 初始权重} (risk_engine 风险平价权重, 已归一化)
    industries:  {code: industry}
    corr_pairs:  [(code_a, code_b, corr), ...] 高相关对
    """
    w = dict(weights)
    log = []
    total0 = sum(w.values())
    if total0 <= 0:
        return w, 0, True, ["初始权重全为 0, 跳过优化"]

    # 步骤 1-2: 行业暴露迭代压缩
    iterations = 0
    converged = True
    for it in range(1, MAX_ITERATIONS + 1):
        iterations = it
        exposure = defaultdict(float)
        for code, weight in w.items():
            exposure[industries.get(code, "unknown")] += weight
        over = {ind: v for ind, v in exposure.items() if v > MAX_SECTOR_POS + 1e-9}
        if not over:
            log.append(f"iter{it}: 行业暴露全部 ≤ {MAX_SECTOR_POS:.0%}")
            break
        for ind, v in over.items():
            for code in [c for c in w if industries.get(c, "unknown") == ind]:
                w[code] *= SECTOR_SHRINK
            log.append(f"iter{it}: 行业 {ind} 暴露 {v:.1%} > 上限, 权重 ×{SECTOR_SHRINK}")
    else:
        exposure = defaultdict(float)
        for code, weight in w.items():
            exposure[industries.get(code, "unknown")] += weight
        still = {ind: v for ind, v in exposure.items() if v > MAX_SECTOR_POS + 1e-9}
        if still:
            converged = False
            log.append(f"{MAX_ITERATIONS} 轮后仍未收敛: "
                       + ", ".join(f"{k}={v:.1%}" for k, v in still.items()))

    # 归一化到行业压缩后的总和, 再进入单票上限 (保证总仓位不被压缩改变)
    s = sum(w.values())
    if s > 0:
        w = {c: x / s for c, x in w.items()}

    # 步骤 3: 单票 > 25% 的部分按比例分给未超限的票
    for _ in range(MAX_ITERATIONS):
        over = {c: v for c, v in w.items() if v > MAX_SINGLE_POS + 1e-9}
        if not over:
            break
        excess = sum(v - MAX_SINGLE_POS for v in over.values())
        for c in over:
            w[c] = MAX_SINGLE_POS
        under = {c: v for c, v in w.items() if v < MAX_SINGLE_POS - 1e-9}
        room = sum(MAX_SINGLE_POS - v for v in under.values())
        if room <= 0 or not under:
            log.append(f"单票上限截断 {excess:.1%} (无未超限票可承接, 权重损失)")
            converged = False
            break
        for c, v in under.items():
            w[c] = v + excess * (MAX_SINGLE_POS - v) / room
        log.append(f"单票上限: {len(over)} 只超 {MAX_SINGLE_POS:.0%}, "
                   f"{excess:.1%} 按比例分给 {len(under)} 只未超限票")

    # 步骤 4: 高相关惩罚 (corr > 0.7 的两票合计 ≤ 35%)
    for a, b, c in corr_pairs:
        if a not in w or b not in w:
            continue
        pair_sum = w[a] + w[b]
        if pair_sum <= MAX_CORR_PAIR_POS + 1e-9:
            continue
        scale = MAX_CORR_PAIR_POS / pair_sum
        released = pair_sum - MAX_CORR_PAIR_POS
        w[a] *= scale
        w[b] *= scale
        # 释放的权重按比例分给其余票 (维持总仓位)
        others = [k for k in w if k not in (a, b)]
        others_sum = sum(w[k] for k in others)
        if others and others_sum > 0:
            for k in others:
                w[k] += released * w[k] / others_sum
        log.append(f"高相关 {a}~{b} corr={c:.2f}: 合计 {pair_sum:.1%} → "
                   f"{MAX_CORR_PAIR_POS:.0%}, 释放 {released:.1%} 分给其余 {len(others)} 只")

    return w, iterations, converged, log


def main(argv=None):
    ap = argparse.ArgumentParser(description="组合优化 (风险预算 + 行业暴露 + 相关性)")
    ap.add_argument("--risk-plan", default=None, help="risk_plan.json 路径 (默认 DATA_DIR/risk_plan.json)")
    ap.add_argument("--decision", default=None, help="decision.json 路径 (默认 DATA_DIR/decision.json)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "portfolio_plan.json"))
    a = ap.parse_args(argv)

    risk_path = a.risk_plan or os.path.join(DATA_DIR, "risk_plan.json")
    dec_path = a.decision or os.path.join(DATA_DIR, "decision.json")
    if not os.path.exists(risk_path):
        print(f"[port] 无 {risk_path} (先跑 risk_engine.py)")
        return 1

    risk = json.load(open(risk_path, encoding="utf-8"))
    decision = json.load(open(dec_path, encoding="utf-8")) if os.path.exists(dec_path) else {}
    plan = risk.get("plan") or []
    if not plan:
        print("[port] risk_plan 无持仓计划")
        return 1

    # 行业: 先取卡片自带 industry, 其余查 INDUSTRY_MAP (decision 与 risk_plan
    # 可能不同步, 故对 plan 里每一只都查一遍映射)
    imap = load_industry_map()
    card_ind = {}
    for c in (decision or {}).get("cards") or []:
        ind = (c.get("industry") or "").strip()
        if ind:
            card_ind[str(c.get("code", "")).zfill(6)] = ind
    industries = resolve_industries([p.get("code") for p in plan], imap)
    for code, ind in card_ind.items():
        if code in industries:
            industries[code] = ind

    # 初始权重: risk_engine 风险平价权重, 归一化到 1
    raw = {str(p["code"]).zfill(6): float(p.get("suggested_position") or 0) for p in plan}
    s = sum(raw.values())
    if s <= 0:
        print("[port] risk_plan 权重全为 0")
        return 1
    weights = {c: v / s for c, v in raw.items()}
    vols = {str(p["code"]).zfill(6): float(p.get("annual_vol") or 0) for p in plan}
    names = {str(p.get("code", "")).zfill(6): p.get("name") for p in plan}

    # 相关性: 只用 buy 池内的票, n ≤ 20 可 O(n²)
    series = load_close_series(list(weights.keys()))
    codes = sorted(weights.keys())
    corr_pairs = []
    corr_matrix = {}
    for i, ca in enumerate(codes):
        if ca not in series:
            continue
        for cb in codes[i + 1:]:
            if cb not in series:
                continue
            c = corr(series[ca], series[cb])
            if c is None:
                continue
            corr_matrix[f"{ca}|{cb}"] = round(c, 3)
            if c > CORR_THRESHOLD:
                corr_pairs.append((ca, cb, c))
    print(f"[port] 相关系数: {len(corr_matrix)} 对可用, {len(corr_pairs)} 对 > {CORR_THRESHOLD}")

    opt_w, iterations, converged, log = optimize(weights, industries, corr_pairs)

    sector_exposure = defaultdict(float)
    for code, weight in opt_w.items():
        sector_exposure[industries.get(code, "unknown")] += weight

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "portfolio_optimizer v1 (风险平价 + 行业暴露 + 高相关惩罚)",
        "n_positions": len(opt_w),
        "weights": {c: round(v, 4) for c, v in sorted(opt_w.items(), key=lambda kv: -kv[1])},
        "weights_detail": [
            {"code": c, "name": names.get(c), "industry": industries.get(c, "unknown"),
             "weight": round(opt_w[c], 4), "weight_pct": f"{opt_w[c]*100:.1f}%",
             "annual_vol": vols.get(c)}
            for c in sorted(opt_w, key=lambda k: -opt_w[k])
        ],
        "sector_exposure": {k: round(v, 4) for k, v in
                            sorted(sector_exposure.items(), key=lambda kv: -kv[1])},
        "sector_exposure_pct": {k: f"{v*100:.1f}%" for k, v in
                                sorted(sector_exposure.items(), key=lambda kv: -kv[1])},
        "iterations": iterations,
        "converged": converged,
        "corr_matrix": corr_matrix,
        "high_corr_pairs": [{"a": x, "b": y, "corr": round(z, 3)} for x, y, z in corr_pairs],
        "limits": {"max_single": MAX_SINGLE_POS, "max_sector": MAX_SECTOR_POS,
                   "corr_threshold": CORR_THRESHOLD,
                   "max_corr_pair_pos": MAX_CORR_PAIR_POS},
        "total_weight": round(sum(opt_w.values()), 4),
        "steps": log,
        "meta": {
            "method": "初始=risk_engine风险平价权重; 行业>40%×0.8迭代; 单票>25%按比例再分配; "
                      "corr>0.7两票合计≤35%",
            "industry_source": "decision.cards.industry → INDUSTRY_MAP (TV) → unknown",
            "corr_window": f"{CORR_WINDOW} 日收盘价收益, 皮尔逊",
            "caveat": "可解释启发式, 非凸优化; 相关性用历史窗口,  regime 切换时会失效",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[port] {len(opt_w)} 只, 迭代 {iterations} 轮, converged={converged}, "
          f"总权重 {out['total_weight']}")
    print(f"[port] 行业暴露: " + ", ".join(f"{k}={v*100:.1f}%" for k, v in
                                        sorted(sector_exposure.items(), key=lambda kv: -kv[1])[:6]))
    for c, v in list(sorted(opt_w.items(), key=lambda kv: -kv[1]))[:5]:
        print(f"  {c} {names.get(c) or ''} {v*100:.1f}% ({industries.get(c, 'unknown')})")
    for line in log:
        print(f"  · {line}")
    print(f"[port] → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
