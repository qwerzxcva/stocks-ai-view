#!/usr/bin/env python3
"""
valuation_engine.py — 股票实际价值估算引擎 (每股内在价值 xxx 元)

给「AI 推荐 / 各股池 / 决策卡」的每只股票标注**实际价值 (元/股)**,
一眼看到「现价 vs 实际价值」的偏离 — 低估=安全垫, 高估=风险。

估值方法 (四法加权, 每个都可追溯):
  1. 行业内 PE 分位回归  : 同行业 PE 中位数 × EPS  (主法, 权重 0.35)
  2. 行业内 PB 分位回归  : 同行业 PB 中位数 × BPS  (权重 0.25)
  3. 股息折现 (简化 DDM) : 每股分红 / (要求回报率 - 永续增长) (权重 0.15)
  4. 全市场均值回归      : 全市场 PE/PB 中位数锚定 (权重 0.25, 兜底)

为什么要行业内比较 (而非统一中位数):
  A股银行 PE 4-6 / PB 0.5-0.8, 科技股 PE 40-80。用统一 PE=20 会让
  全部银行显示「低估 60%」、全部科技股「高估 100%」——那是行业差异,
  不是定价错误。行业内分位才能反映「相对同行」的贵贱。

数据源 (零鉴权, 本项目已有):
  腾讯快照 snapshot_*.json : price/pe/pb/mktcap (全市场 5278 只)
  industry_map_tv.json     : 裸码 → 英文 industry (5309 只, TradingView 口径)

用法:
  python3 valuation_engine.py --inject   # 注入 data/*.json 股池
  python3 valuation_engine.py --dry-run  # 只看不写
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import SNAPSHOT_GLOB, AI_VIEW_DATA, INDUSTRY_MAP
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")
    SNAPSHOT_GLOB = os.environ.get("STOCKS_SNAPSHOT_GLOB") or os.path.join(
        os.environ.get("STOCKS_LIVE_MARKET", ""), "snapshot_*.json")
    INDUSTRY_MAP = os.environ.get("STOCKS_INDUSTRY_MAP") or os.path.join(
        os.path.dirname(HERE), "..", "stocks-data-live", "data", "meta",
        "industry_map_tv.json")

# ─── 估值参数 (可解释的保守假设) ────────────────────────────────
REQUIRED_RETURN = 0.10      # 要求回报率 10%
PERPETUAL_G = 0.03          # 永续增长率 3%
MIN_PE, MAX_PE = 5.0, 60.0  # 有效 PE 区间 (防极值/亏损股)
MIN_PB, MAX_PB = 0.5, 8.0   # 有效 PB 区间
MIN_INDUSTRY_N = 5          # 行业内至少 5 只才用行业中位数 (否则回退全市场)

W_IND_PE, W_IND_PB, W_DDM, W_MKT = 0.35, 0.25, 0.15, 0.25


def load_snapshot():
    """估值输入: 优先 snapshot_*.json (本地开发), 没有则用 all_sources.json
    的 quote 组 (Actions 环境没有 snapshot, 但采集产出永远有)。

    返回 {symbol: record} — symbol 带 sh/sz/bj 前缀, 与 snapshot 一致。
    """
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if snaps:
        try:
            return json.load(open(snaps[-1], encoding="utf-8"))
        except Exception:
            pass
    # 降级: 从 all_sources.json 的 quote 组构造
    src = os.path.join(AI_VIEW_DATA, "all_sources.json")
    if not os.path.exists(src):
        return {}
    try:
        d = json.load(open(src, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in (d.get("data") or {}).get("quote") or []:
        sym = r.get("symbol") or r.get("code")
        if not sym:
            continue
        out[sym] = {
            "code": r.get("code"),
            "name": r.get("name"),
            "price": r.get("price"),
            "pe": r.get("pe"),
            "pb": r.get("pb"),
            "mktcap": r.get("mktcap"),
            "floatcap": r.get("floatcap"),
            "source": "all_sources.quote",
        }
    return out


def load_industry():
    """{裸码: industry}"""
    try:
        im = json.load(open(INDUSTRY_MAP, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for bare, v in im.items():
        if isinstance(v, dict):
            out[bare] = v.get("industry") or v.get("sector") or ""
    return out


def norm_code(code):
    c = str(code).strip()
    if c[:2] in ("sh", "sz", "bj"):
        return c, c[2:]
    c6 = c.zfill(6)
    if c6.startswith(("60", "68", "90")):
        return "sh" + c6, c6
    if c6.startswith(("43", "83", "87", "88")):
        return "bj" + c6, c6
    return "sz" + c6, c6


def _med(a):
    n = len(a)
    if not n:
        return None
    a = sorted(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2


def build_benchmarks(snap, ind_map):
    """算 全市场 + 各行业 的 PE/PB 中位数。

    返回 (mkt_pe, mkt_pb, {industry: {pe, pb, n}})
    """
    mkt_pe, mkt_pb = [], []
    by_ind = {}
    for sym, r in snap.items():
        try:
            pe = float(r.get("pe") or 0)
            pb = float(r.get("pb") or 0)
        except (TypeError, ValueError):
            continue
        bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        ind = ind_map.get(bare) or "_other"
        d = by_ind.setdefault(ind, {"pe": [], "pb": []})
        if MIN_PE <= pe <= MAX_PE:
            d["pe"].append(pe)
            mkt_pe.append(pe)
        if MIN_PB <= pb <= MAX_PB:
            d["pb"].append(pb)
            mkt_pb.append(pb)
    mkt = {"pe": _med(mkt_pe) or 15.0, "pb": _med(mkt_pb) or 1.8}
    bench = {}
    for ind, d in by_ind.items():
        if ind == "_other":
            continue
        pe_m = _med(d["pe"])
        pb_m = _med(d["pb"])
        n = len(d["pe"]) + len(d["pb"])
        if pe_m or pb_m:
            bench[ind] = {"pe": pe_m, "pb": pb_m, "n": n}
    return mkt, bench


def estimate_value(rec, bare, bench, mkt):
    """四法估值。返回 {value, methods, confidence, dev_pct, note}"""
    px = rec.get("price")
    if not px or px <= 0:
        return None
    try:
        pe = float(rec.get("pe") or 0)
        pb = float(rec.get("pb") or 0)
    except (TypeError, ValueError):
        pe = pb = 0
    b = bench.get(bare) or {}
    ind_pe = b.get("pe")
    ind_pb = b.get("pb")
    ind_n = b.get("n", 0)

    methods, weights = {}, {}

    def add(name, val, w):
        if val and 0 < val < px * 5:
            methods[name] = round(val, 3)
            weights[name] = w

    # 1) 行业内 PE 回归 (样本够才用)
    if pe > 0 and ind_pe and ind_n >= MIN_INDUSTRY_N:
        pe_c = min(MAX_PE, max(MIN_PE, pe))
        add("ind_pe", px * (ind_pe / pe_c), W_IND_PE)
    # 2) 行业内 PB 回归
    if pb > 0 and ind_pb and ind_n >= MIN_INDUSTRY_N:
        pb_c = min(MAX_PB, max(MIN_PB, pb))
        add("ind_pb", px * (ind_pb / pb_c), W_IND_PB)
    # 3) 股息折现 (ROE 反推分红)
    if pe > 0 and pb > 0:
        eps = px / pe
        bps = px / pb
        roe = eps / bps if bps > 0 else None
        if roe and roe > 0:
            dps = eps * 0.30                      # 假设 30% 派息率
            denom = REQUIRED_RETURN - PERPETUAL_G
            if denom > 0.02:
                add("ddm", dps / denom, W_DDM)
    # 4) 全市场均值回归 (兜底, 始终可用)
    devs = []
    if pe > 0:
        devs.append(mkt["pe"] / min(MAX_PE, max(MIN_PE, pe)))
    if pb > 0:
        devs.append(mkt["pb"] / min(MAX_PB, max(MIN_PB, pb)))
    if devs:
        k = min(2.0, max(0.5, sum(devs) / len(devs)))
        add("mkt_revert", px * k, W_MKT)

    if not methods:
        return {"value": None, "methods": {}, "confidence": 0, "dev_pct": None,
                "note": "无有效 PE/PB (亏损股或数据缺失)"}
    wsum = sum(weights.values())
    value = sum(methods[k] * weights[k] for k in methods) / wsum
    dev_pct = round((px - value) / value * 100, 1) if value else None
    return {"value": round(value, 2), "methods": methods,
            "confidence": round(len(methods) / 4.0, 2),
            "dev_pct": dev_pct, "note": f"{len(methods)}法合成"}


def inject_field(rec, val):
    rec["intrinsic_value"] = val.get("value")          # 实际价值 元/股
    rec["value_dev_pct"] = val.get("dev_pct")          # 偏离度 %
    rec["value_methods"] = val.get("methods") or {}
    rec["value_confidence"] = val.get("confidence", 0)
    rec["value_note"] = val.get("note", "")
    if val.get("value") and val.get("dev_pct") is not None:
        d = val["dev_pct"]
        rec["value_tag"] = ("深度低估" if d <= -25 else "低估" if d <= -10
                            else "合理" if d < 10 else "高估" if d < 30 else "严重高估")
    else:
        rec["value_tag"] = "无法估值"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "valuation.json"))
    args = ap.parse_args()

    snap = load_snapshot()
    if not snap:
        print("[valuation] 无快照数据")
        return 1
    ind_map = load_industry()
    # 每只股票的行业内基准: bench key 用裸码 → 但同 industry 共享基准
    mkt, ind_bench = build_benchmarks(snap, ind_map)
    # 转成 {裸码: 其行业的基准}
    bare_bench = {}
    for sym in snap:
        bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        ind = ind_map.get(bare)
        if ind and ind in ind_bench:
            bare_bench[bare] = ind_bench[ind]
    print(f"[valuation] 快照 {len(snap)} 只 | 全市场中位 PE={mkt['pe']} PB={mkt['pb']} "
          f"| 行业基准 {len(ind_bench)} 个", flush=True)

    TARGETS = [
        ("multidim.json", ["top"]),
        ("ai_pick_v4.json", ["picks"]),
        ("decision.json", ["cards", "avoid"]),
        ("ai_picks.json", ["picks"]),
        ("multi_v4.json", ["buy", "watch"]),
        ("lowpool.json", ["strong", "watch_list"]),
        ("ban_pick.json", ["picks"]),
        ("hotspot_infer.json", ["watch"]),
        ("latest.json", ["limit_up_pool", "fund_rank"]),
    ]
    cache = {}
    n_done = 0
    for fname, arr_keys in TARGETS:
        p = os.path.join(AI_VIEW_DATA, fname)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        changed = False
        for ak in arr_keys:
            arr = d.get(ak)
            if not isinstance(arr, list):
                continue
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                code = rec.get("code") or rec.get("symbol")
                if not code:
                    continue
                sym, bare = norm_code(code)
                if sym in cache:
                    val = cache[sym]
                else:
                    srec = snap.get(sym) or {}
                    val = estimate_value(srec, bare, bare_bench, mkt) if srec else None
                    cache[sym] = val
                if val:
                    inject_field(rec, val)
                    n_done += 1
                    changed = True
        if changed and not args.dry_run:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            print(f"  ✓ {fname}")

    # 估值表 (全快照)
    table = {}
    for sym, rec in snap.items():
        bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        v = estimate_value(rec, bare, bare_bench, mkt)
        if v and v.get("value"):
            table[sym] = {"name": rec.get("name"), "price": rec.get("price"),
                          "intrinsic_value": v["value"], "dev_pct": v["dev_pct"],
                          "methods": v["methods"], "confidence": v["confidence"]}
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "valuation v2 (行业内PE/PB分位 + DDM + 全市场回归)",
        "n": len(table),
        "market_medians": mkt,
        "n_industry_bench": len(ind_bench),
        "params": {"required_return": REQUIRED_RETURN, "perpetual_g": PERPETUAL_G,
                   "weights": {"ind_pe": W_IND_PE, "ind_pb": W_IND_PB,
                               "ddm": W_DDM, "mkt_revert": W_MKT},
                   "min_industry_n": MIN_INDUSTRY_N},
        "table": table,
        "meta": {
            "formula": "实际价值 = Σ(方法权重×方法价值); 偏离度=(现价-价值)/价值",
            "tags": "深度低估≤-25% / 低估≤-10% / 合理<10% / 高估<30% / 严重高估≥30%",
            "why_industry": "银行PE4-6/科技PE40-80, 统一中位数会把行业差异误判为定价错误",
            "caveat": "估值是统计锚定非精确内在价值; 亏损股仅用PB/DDM; 不构成投资建议",
        },
    }
    if not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    print(f"\n[valuation] 注入 {n_done} 条, 估值表 {len(table)} 只 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
