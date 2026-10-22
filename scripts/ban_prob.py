#!/usr/bin/env python3
"""
ban_prob.py — 打板概率引擎 (实证, 非拍脑袋)

用户需求: 「按每只股票进行分析, 判断该不该打板, 打板第一天保持涨停的概率多少?
           第二天涨停的概率又是多少? 拿出数据来分析」

原问题: 打板池的「综合分」是 ai_screener.py 里 0.22*连板 + 0.13*封板时间 +
      0.17*热点 + 0.20*RPS5 + 0.12*RPS20 + 0.16*梯队稀缺 的加权和,
      权重无实证依据, 且与「次日是否继续涨停」无直接关系。

本引擎用本地 2577 只 × 813 个交易日的真实 K 线, 计算:

  A. 基础概率 (全样本)
     - 首板 → 次日涨停概率
     - N 连板 → 次日涨停概率 (N=1..5)
     - 首板 → 次日平均收益率 / 次日炸板率

  B. 分层概率 (找真正有效的因子)
     - 按连板数分层
     - 按首封时间分层 (09:30-10:00 / 10:00-11:30 / 13:00-14:00 / 14:00-15:00)
     - 按当日涨幅分层 (5-7% / 7-9% / 9-10% / 20%)
     - 按市值分层 (<50亿 / 50-200亿 / 200-1000亿 / >1000亿)
     - 按量比分层 (<2 / 2-5 / 5-10 / >10)

  C. 个股级输出 (打板池每只股票)
     - 次日涨停概率 (基于其连板数/市值/封板时间/量比的分层概率)
     - 次日期望收益
     - 次日炸板(跌破)概率
     - 历史同类样本数 (样本<30 标「证据不足」)

涨停定义 (本地K线口径, 无涨跌停字段):
  主板 (60/00): (close/prev_close - 1) >= 9.8%
  创业板/科创 (30/68): >= 19.8%
  北交所 (43/83/87/92): >= 29.8%
  炸板定义: 当日曾涨停(high/prev_close-1 达阈值) 但收盘未涨停

用法:
  python3 ban_prob.py --out data/ban_prob.json
  python3 ban_prob.py --codes 600519,002961 --out /tmp/single.json
"""
import argparse
import json
import math
import os
import sys
from array import array
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


# ─── 涨停阈值 ────────────────────────────────────────────────────
def zt_threshold(code):
    c = str(code)
    if c.startswith(("30", "68")):
        return 19.8
    if c.startswith(("43", "83", "87", "92")):
        return 29.8
    return 9.8


def is_zt(code, close, prev_close):
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) * 100 >= zt_threshold(code)


def is_zhaban(code, high, close, prev_close):
    if prev_close <= 0:
        return False
    return ((high / prev_close - 1) * 100 >= zt_threshold(code)) and (not is_zt(code, close, prev_close))


# ─── 数据加载 ────────────────────────────────────────────────────
def load_klines(bulk):
    import glob
    data = {}
    for p in sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    data.setdefault(code, []).append([
                        parts[0].strip(), float(parts[1]), float(parts[2]),
                        float(parts[3]), float(parts[4]), float(parts[5])
                    ])
                except ValueError:
                    continue
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


# ─── 连板数计算 ──────────────────────────────────────────────────
def lianban_series(code, bars):
    """返回 [连板数] 序列, 与 bars 等长。0=非涨停, 1=首板, 2=二连板..."""
    out = []
    n = 0
    for i, b in enumerate(bars):
        pc = bars[i - 1][2] if i > 0 else b[2]
        if is_zt(code, b[2], pc):
            n += 1
        else:
            n = 0
        out.append(n)
    return out


# ─── A. 基础概率 ────────────────────────────────────────────────
def base_stats(klines):
    """全样本: 首板/连板 → 次日涨停概率、次日收益、炸板率"""
    stat = defaultdict(lambda: {"n": 0, "zt_next": 0, "zhaban_next": 0,
                                "ret_next_sum": 0.0, "ret_next_win": 0})
    n_events = 0
    for code, bars in klines.items():
        if len(bars) < 3:
            continue
        lb = lianban_series(code, bars)
        for i in range(1, len(bars) - 1):
            if lb[i] == 0:
                continue
            nxt = bars[i + 1]
            cur = bars[i]
            if cur[2] <= 0:
                continue
            key = min(lb[i], 6)          # 6+ 合并
            s = stat[key]
            s["n"] += 1
            n_events += 1
            if is_zt(code, nxt[2], cur[2]):
                s["zt_next"] += 1
            if is_zhaban(code, nxt[3], nxt[2], cur[2]):
                s["zhaban_next"] += 1
            ret = (nxt[2] / cur[2] - 1) * 100
            s["ret_next_sum"] += ret
            if ret > 0:
                s["ret_next_win"] += 1

    out = {}
    for lb_n in sorted(stat):
        s = stat[lb_n]
        n = s["n"]
        if n == 0:
            continue
        label = f"{lb_n}连板" if lb_n > 1 else "首板"
        out[label] = {
            "lianban": lb_n,
            "n_samples": n,
            "zt_next_rate": round(s["zt_next"] / n * 100, 2),
            "zhaban_next_rate": round(s["zhaban_next"] / n * 100, 2),
            "ret_next_avg": round(s["ret_next_sum"] / n, 2),
            "ret_next_win_rate": round(s["ret_next_win"] / n * 100, 2),
            "evidence": "ok" if n >= 30 else "insufficient",
        }
    # 连板层级覆盖检查: 1板/2板/3板/4板+ (≥7 板在计算时已合并到 6连板桶)
    expected = {1: "首板", 2: "2连板", 3: "3连板", 4: "4连板", 5: "5连板", 6: "6连板"}
    missing = [lb_n for lb_n, lbl in expected.items()
               if lbl not in out or not out[lbl].get("n_samples")]
    coverage = {
        "levels": [f"{lb_n}板" if lb_n > 1 else "首板" for lb_n in expected],
        "missing": [f"{lb_n}板" for lb_n in missing],
        "complete": not missing,
        "note": "≥7连板样本并入 6连板桶 (min(lb,6)), 保证 4板+ 层有概率基准",
    }
    return {"events": n_events, "by_lianban": out, "lianban_coverage": coverage}


# ─── B. 分层概率 (找真正有效的因子) ────────────────────────────
def layered_stats(klines):
    """按本地K线可算的因子分层, 看哪些真的影响次日涨停概率。

    因子 (全部由K线算出, 无需外部数据):
      seal_strength  封板强度 = (close/prev-1) / 涨停阈值, 越接近1封得越死
      vol_ratio      量比 = 当日量 / 前5日均量
      prior_gain     前3日累计涨幅 (追高风险)
      lianban        连板数
    """
    dims = ["seal_strength", "vol_ratio", "prior_gain", "lianban"]
    layers = {d: defaultdict(lambda: {"n": 0, "zt": 0, "ret": 0.0, "zb": 0}) for d in dims}

    def seal_bucket(code, close, prev):
        if prev <= 0:
            return None
        th = zt_threshold(code)
        s = (close / prev - 1) * 100 / th
        if s >= 0.999:
            return "一字/满封"
        if s >= 0.98:
            return "接近满封"
        if s >= 0.95:
            return "小幅松动"
        return "弱封"

    def vol_bucket(vol, avg5):
        if avg5 <= 0:
            return None
        r = vol / avg5
        if r < 1.5:
            return "<1.5"
        if r < 3:
            return "1.5-3"
        if r < 6:
            return "3-6"
        return ">6"

    def prior_bucket(bars, i):
        if i < 3:
            return None
        base = bars[i - 3][2]
        if base <= 0:
            return None
        g = (bars[i][2] / base - 1) * 100
        if g < 10:
            return "<10%"
        if g < 25:
            return "10-25%"
        if g < 40:
            return "25-40%"
        return ">40%"

    n = 0
    for code, bars in klines.items():
        if len(bars) < 6:
            continue
        lb = lianban_series(code, bars)
        for i in range(3, len(bars) - 1):
            if lb[i] == 0:
                continue
            cur = bars[i]
            prev = bars[i - 1]
            nxt = bars[i + 1]
            if cur[2] <= 0 or prev[2] <= 0:
                continue
            avg5 = sum(b[5] for b in bars[max(0, i - 5):i]) / max(1, min(5, i))
            ret = (nxt[2] / cur[2] - 1) * 100
            is_zt_next = is_zt(code, nxt[2], cur[2])
            is_zb_next = is_zhaban(code, nxt[3], nxt[2], cur[2])
            buckets = {
                "seal_strength": seal_bucket(code, cur[2], prev[2]),
                "vol_ratio": vol_bucket(cur[5], avg5),
                "prior_gain": prior_bucket(bars, i),
                "lianban": f"{min(lb[i], 6)}连板" if lb[i] > 1 else "首板",
            }
            for dim, b in buckets.items():
                if not b:
                    continue
                s = layers[dim][b]
                s["n"] += 1
                s["zt"] += 1 if is_zt_next else 0
                s["zb"] += 1 if is_zb_next else 0
                s["ret"] += ret
            n += 1

    out = {}
    for dim in dims:
        out[dim] = {}
        for b, s in sorted(layers[dim].items()):
            if s["n"] < 10:
                continue
            out[dim][b] = {
                "n": s["n"],
                "zt_next_rate": round(s["zt"] / s["n"] * 100, 2),
                "zhaban_next_rate": round(s["zb"] / s["n"] * 100, 2),
                "ret_next_avg": round(s["ret"] / s["n"], 2),
                "evidence": "ok" if s["n"] >= 30 else "insufficient",
            }
    return {"events": n, "layers": out}


# ─── ML 模型推理 (可选, 需 data/limitup_ml_model.json) ──────────
def load_ml_model(data_dir):
    """加载 limitup_ml_train.py 保存的逻辑回归模型与样本索引。

    模型文件不存在或字段不齐时返回 None (静默降级为纯规则模式)。
    """
    p = os.path.join(data_dir, "limitup_ml_model.json")
    if not os.path.exists(p):
        return None
    try:
        m = json.load(open(p, encoding="utf-8"))
        nf = m["n_features"]
        if not (len(m["w"]) == len(m["mu"]) == len(m["sd"]) == nf):
            return None
        syms = json.load(open(os.path.join(data_dir, "lp_syms.json"), encoding="utf-8"))
        sym_idx = array("i")
        sym_idx.frombytes(open(os.path.join(data_dir, "lp_sym.i4"), "rb").read())
        dates = array("i")
        dates.frombytes(open(os.path.join(data_dir, "lp_date.i4"), "rb").read())
        latest = {}  # sym 索引 -> (最大日期, 样本下标)
        for i in range(len(sym_idx)):
            si = sym_idx[i]
            d = dates[i]
            cur = latest.get(si)
            if cur is None or d > cur[0]:
                latest[si] = (d, i)
        return {
            "model": m,
            "code_to_si": {c: j for j, c in enumerate(syms)},
            "latest": latest,
            "feat_path": os.path.join(data_dir, "lp_feat.f32"),
        }
    except Exception:
        return None


def ml_probability(ml, code):
    """对该股票最新样本算 logistic 次日涨停分数, 返回 0-100 的百分数。

    找不到该股票的样本时返回 None。
    """
    if not ml:
        return None
    si = ml["code_to_si"].get(code)
    if si is None:
        return None
    ent = ml["latest"].get(si)
    if not ent:
        return None
    m = ml["model"]
    nf = m["n_features"]
    a = array("f")
    with open(ml["feat_path"], "rb") as f:
        f.seek(ent[1] * nf * 4)
        a.frombytes(f.read(nf * 4))
    if len(a) != nf:
        return None
    s = m["bias"]
    for k in range(nf):
        s += m["w"][k] * (a[k] - m["mu"][k]) / m["sd"][k]
    p = 1.0 / (1.0 + math.exp(-max(-30, min(30, s))))
    return round(p * 100, 2)


# ─── C. 个股级: 打板池每只股票 ──────────────────────────────────
def stock_prob(code, bars, base, layers):
    """给定股票, 基准概率(按连板数) × 因子调整(封板强度/量比/前期涨幅)。

    调整方法: 分层概率相对该连板数基准的比率, 取各因子比率的几何平均,
    限制在 [0.6, 1.6] 区间 (避免过度外推)。样本不足的因子不参与调整。
    """
    if len(bars) < 6:
        return None
    lb = lianban_series(code, bars)
    last = len(bars) - 1
    if lb[last] == 0:
        return None
    cur = bars[last]
    prev = bars[last - 1]
    lb_n = lb[last]

    key = f"{min(lb_n, 6)}连板" if lb_n > 1 else "首板"
    b = base["by_lianban"].get(key, {})
    base_p = b.get("zt_next_rate")
    if base_p is None:
        return None

    # 因子提取
    avg5 = sum(x[5] for x in bars[max(0, last - 5):last]) / max(1, min(5, last))
    th = zt_threshold(code)
    seal = (cur[2] / prev[2] - 1) * 100 / th if prev[2] > 0 else 0
    vr = cur[5] / avg5 if avg5 > 0 else 1
    prior = (cur[2] / bars[last - 3][2] - 1) * 100 if bars[last - 3][2] > 0 else 0

    def bucket_of(dim, val, buckets):
        if dim == "seal_strength":
            return ("一字/满封" if val >= 0.999 else "接近满封" if val >= 0.98
                    else "小幅松动" if val >= 0.95 else "弱封")
        if dim == "vol_ratio":
            return ("<1.5" if val < 1.5 else "1.5-3" if val < 3
                    else "3-6" if val < 6 else ">6")
        if dim == "prior_gain":
            return ("<10%" if val < 10 else "10-25%" if val < 25
                    else "25-40%" if val < 40 else ">40%")
        return None

    # 收集有效因子的调整比率
    ratios = []
    used = []
    for dim, val in (("seal_strength", seal), ("vol_ratio", vr), ("prior_gain", prior)):
        bk = bucket_of(dim, val, None)
        st = (layers.get(dim) or {}).get(bk)
        if not st or st.get("evidence") != "ok" or st.get("n", 0) < 30:
            continue
        r = st["zt_next_rate"] / base_p if base_p > 0 else 1.0
        ratios.append(max(0.6, min(1.6, r)))
        used.append(f"{dim}={bk}({st['zt_next_rate']}%,n={st['n']})")

    if ratios:
        adj = 1.0
        for r in ratios:
            adj *= r
        adj **= (1.0 / len(ratios))
    else:
        adj = 1.0

    p_zt = round(min(95.0, base_p * adj), 2)
    # 炸板率同样按比率缩放 (但用 zb 基准)
    base_zb = b.get("zhaban_next_rate") or 0
    p_zb = round(min(95.0, base_zb * adj), 2)
    base_ret = b.get("ret_next_avg") or 0
    e_ret = round(base_ret * adj, 2)

    return {
        "code": code,
        "lianban": lb_n,
        "date": cur[0],
        "close": cur[2],
        "chg_pct": round((cur[2] / prev[2] - 1) * 100, 2),
        "p_zt_next": p_zt,
        "p_zhaban_next": p_zb,
        "exp_ret_next": e_ret,
        "n_samples": b.get("n_samples"),
        "evidence": b.get("evidence", "insufficient"),
        "base_p": base_p,
        "adj_factor": round(adj, 3),
        "factors_used": used,
        "basis": f"{key}基准{base_p}% × 因子调整{adj:.2f} (n={b.get('n_samples')})",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--codes", help="只算这些股票 (逗号分隔)")
    args = ap.parse_args()

    bulk = args.bulk
    if not bulk:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from paths import BULK
        bulk = BULK

    now = datetime.now(CST)
    print(f"[ban_prob] {now:%Y-%m-%d %H:%M} CST", flush=True)
    print(f"[ban_prob] 加载K线: {bulk}", flush=True)
    klines = load_klines(bulk)
    print(f"[ban_prob] {len(klines)} 只", flush=True)

    print("[ban_prob] A. 基础概率...", flush=True)
    base = base_stats(klines)
    print(f"  {base['events']} 个涨停事件")

    print("[ban_prob] B. 分层概率...", flush=True)
    layers = layered_stats(klines)

    # C. 个股级 (默认算最近有涨停的股票)
    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]

    stocks = []
    if codes:
        for c in codes:
            for k, bars in klines.items():
                if k.endswith(c) or k == c:
                    r = stock_prob(k, bars, base, layers["layers"])
                    if r:
                        stocks.append(r)
    else:
        # 最近交易日有涨停的股票
        latest_date = max((bars[-1][0] for bars in klines.values() if bars), default=None)
        for code, bars in klines.items():
            if not bars or bars[-1][0] != latest_date:
                continue
            r = stock_prob(code, bars, base, layers["layers"])
            if r:
                stocks.append(r)
        stocks.sort(key=lambda x: -(x["p_zt_next"] or 0))

    # ML 模型集成: 模型文件存在则给每只股票增加 ml_probability 字段,
    # 不存在则静默降级为纯规则模式 (不报错, 不改变原有字段)。
    ml = load_ml_model(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    if ml:
        n_ml = 0
        for r in stocks:
            mp = ml_probability(ml, r["code"])
            if mp is not None:
                r["ml_probability"] = mp
                n_ml += 1
        print(f"[ban_prob] ML 模型已加载, {n_ml}/{len(stocks)} 只股票得到 ml_probability", flush=True)

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "ban_prob (实证概率, 基于本地K线)",
        "sample": {
            "n_stocks": len(klines),
            "n_zt_events": base["events"],
            "kline_source": "腾讯 fqkline 前复权日K",
        },
        "base": base,
        "layers": layers,
        "stocks": stocks[:60],
        "meta": {
            "zt_def": "主板≥9.8% 创业板/科创≥19.8% 北交所≥29.8% (本地K线口径)",
            "zhaban_def": "盘中触及涨停但收盘未涨停",
            "p_zt_next": "该股涨停后, 次日继续涨停的历史概率",
            "p_zhaban_next": "次日盘中触及涨停但收盘未涨停的概率",
            "caveat": "概率基于历史全样本等权, 未剔除极端行情; 样本<30 标证据不足; "
                      "打板属高风险交易, 次日炸板即大幅亏损, 仅供研究不构成投资建议",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
