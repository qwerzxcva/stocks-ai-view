#!/usr/bin/env python3
"""signal_hub.py — the shared analysis layer every pick engine calls.

Before this, each engine read its own files and produced its own list, and the
dashboard stitched them together afterwards. The consequence was that no engine
could see the others' verdicts, so a stock could be a strong signal pick and a
limit-up trap at the same time (which is exactly what happened on 2026-10-14:
the signal list and the limit-up list overlapped).

This module is the single place that:
  1. loads live quotes once and shares them with every caller
  2. exposes the reusable pieces of each engine (engine_v5 votes, ban_pick
     probabilities, ai_pick_v4 position filters, overseas bias)
  3. cross-checks them: a pick is only actionable if the other engines do not
     contradict it

Engines import it; it never imports them (no cycle).
"""
import glob
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = os.environ.get("STOCKS_KLINE_BULK") or os.path.join(
        os.path.dirname(HERE), "..", "stocks-data-kline", "data", "bulk")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

_QUOTE_CACHE = {}
_KLINE_CACHE = {}


def log(m):
    print(f"[hub {datetime.now(CST):%H:%M:%S}] {m}", flush=True)


# ── shared data access ────────────────────────────────────────
def prefixed(code):
    c = str(code)
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith(("6", "5", "9")) else
            "bj" if c.startswith(("4", "8", "9")) else "sz") + c


def bare_of(code):
    c = str(code)
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def quotes(codes, refresh=False):
    """Live quotes, cached for the whole process so N callers cost 1 fetch."""
    keys = [prefixed(c) for c in codes]
    if refresh:
        _QUOTE_CACHE.clear()
    missing = [k for k in keys if k not in _QUOTE_CACHE]
    if missing:
        for i in range(0, len(missing), 20):
            batch = missing[i:i + 20]
            url = "https://qt.gtimg.cn/q=" + ",".join(batch)
            try:
                req = urllib.request.Request(url, headers=UA)
                raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
            except Exception as e:
                log(f"quote fetch failed: {e}")
                continue
            for m in re.finditer(r"v_(\w+)=\"([^\"]*)\"", raw):
                p = m.group(2).split("~")
                if len(p) < 45:
                    continue
                try:
                    _QUOTE_CACHE[m.group(1)] = {
                        "name": p[1], "price": float(p[3] or 0),
                        "prev_close": float(p[4] or 0), "open": float(p[5] or 0),
                        "high": float(p[33] or 0), "low": float(p[34] or 0),
                        "chg_pct": float(p[32] or 0),
                        "volume": float(p[36] or 0), "amount": float(p[37] or 0),
                        "turnover": float(p[38] or 0), "pe": float(p[39] or 0),
                        "amplitude": float(p[43] or 0),
                        "limit_up": float(p[47] or 0),
                        "limit_down": float(p[48] or 0),
                        "avg_price": float(p[45] or 0) if len(p) > 45 else 0,
                        "time": p[30],
                    }
                except (ValueError, IndexError):
                    pass
    return {k: _QUOTE_CACHE.get(k) for k in keys if k in _QUOTE_CACHE}


def klines(codes):
    """Daily qfq bars for the given codes, cached."""
    want = set()
    for c in codes:
        c = str(c)
        want.update({c, prefixed(c), bare_of(c)})
    if not _KLINE_CACHE:
        for p in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "|" not in line or line.startswith("#"):
                        continue
                    parts = line.strip().split("|")
                    if len(parts) < 6 or parts[0] not in want:
                        continue
                    try:
                        _KLINE_CACHE.setdefault(parts[0], []).append(
                            [parts[1], float(parts[2]), float(parts[3]),
                             float(parts[4]), float(parts[5]),
                             float(parts[6]) if len(parts) > 6 else 0.0])
                    except ValueError:
                        pass
        for s in _KLINE_CACHE:
            _KLINE_CACHE[s].sort(key=lambda r: r[0])
    out = {}
    for c in codes:
        for k in (str(c), prefixed(c), bare_of(c)):
            if k in _KLINE_CACHE:
                out[str(c)] = _KLINE_CACHE[k]
                break
    return out


def load_json(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


# ── tradability (used by every engine) ────────────────────────
def limit_pct(code):
    b = bare_of(code)
    if b.startswith(("30", "301")):
        return 0.20
    if b.startswith("688"):
        return 0.20
    if b.startswith(("43", "83", "87", "88", "92")) or bare_of(code)[:2] == "bj":
        return 0.30
    return 0.10


def tradability(code, q=None):
    """(ok, reason, detail). Shared so every engine rejects the same way."""
    q = q or quotes([code]).get(prefixed(code))
    if not q:
        return False, "no quote", {}
    lim = limit_pct(code)
    up = q["prev_close"] * (1 + lim) if q["prev_close"] > 0 else 0
    d = {"chg_pct": round(q["chg_pct"], 2), "price": q["price"],
         "limit_price": round(up, 2), "turnover": q["turnover"]}
    if q["price"] <= 0 or q["prev_close"] <= 0:
        return False, "no quote", d
    if up > 0 and q["price"] >= up * 0.99:
        return False, "at limit-up", d
    if q["chg_pct"] >= lim * 100 * 0.9:
        return False, "near limit", d
    if q["chg_pct"] >= 7.0:
        return False, "chase risk", d
    if q["turnover"] < 0.05:
        return False, "no liquidity", d
    return True, "ok", d


# ── reusable engine pieces ────────────────────────────────────
_STRUCT = {"loaded": False, "by_code": {}, "meta": {}}


def structure_view(code):
    """K 线结构层（structure_engine.py）的判读结果。

    注意用法：结构层输出的是**描述**，不是方向。回测（2026-10、日线、5 日持有）显示
    A 股短周期由均值回归主导——breakout:up 的 5 日前向收益为负，三卖信号反而向上。
    所以这里只提供标签与分数，方向决策必须在调用方结合量能/资金流再定，
    不要把 score 直接当买卖信号用。

    缺数据时返回 None（不编造默认值），调用方需自行 fallback。
    """
    if not _STRUCT["loaded"]:
        d = load_json("structure_signals.json") or {}
        _STRUCT["by_code"] = {bare_of(x.get("code", "")): x for x in (d.get("items") or [])}
        _STRUCT["meta"] = {"generated_at": d.get("generated_at"),
                           "engine": d.get("engine"), "count": d.get("count")}
        _STRUCT["loaded"] = True
    return _STRUCT["by_code"].get(bare_of(code))


def structure_tags(code):
    """只要标签列表，方便拼理由。"""
    v = structure_view(code)
    return (v or {}).get("tags") or []


_CALIB = {"loaded": False, "table": {}, "meta": {}}


def structure_expectancy(code, min_n=120, min_t=1.5):
    """用实测校准表把结构标签换算成「5 日期望收益」以及它到底是顺势还是反向。

    为什么不让调用方直接看 bias：回测（见 docs/STRUCTURE_ENGINE.md）显示 A 股日线
    5 日尺度由均值回归主导，`breakout:up` 的前向收益为负、`chan:third_sell` 反而向上。
    硬把 bias 当方向就是接书本教条。这里只用过 t 检验的标签，并在返回里明说 edge 类型。

    返回 None = 无可用校准或标签都不可信（宁可不给，也不编一个分数）。
    """
    if not _CALIB["loaded"]:
        d = load_json("structure_calibration.json") or {}
        _CALIB["table"] = d.get("table") or {}
        _CALIB["meta"] = {"generated_at": d.get("generated_at"),
                          "horizon_days": d.get("horizon_days")}
        _CALIB["loaded"] = True
    table = _CALIB["table"]
    if not table:
        return None
    tags = structure_tags(code)
    if not tags:
        return None
    # 先试组合键（单标签信息量远低于组合），再退到单标签
    cands = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            for key in (f"{a} & {b}", f"{b} & {a}"):
                if key in table:
                    cands.append((key, table[key]))
    for a in tags:
        if a in table:
            cands.append((a, table[a]))
    cands = [(k, v) for k, v in cands if v["n"] >= min_n and abs(v["t"]) >= min_t]
    if not cands:
        return None
    key, best = max(cands, key=lambda kv: abs(kv[1]["t"]))
    return {"basis": key, "n": best["n"], "t": best["t"],
            "edge_5d_pct": best["avg_fwd_5d"], "win_rate": best["win_rate"],
            "edge": best["edge"], "horizon_days": _CALIB["meta"].get("horizon_days", 5)}


def v5_votes(code):
    """engine_v5's indicator votes for one code, from its last output."""
    d = load_json("engine_v5.json") or {}
    want = bare_of(code)
    for x in (d.get("top") or []):
        if bare_of(x.get("code")) == want:
            return {"score": x.get("score"), "resonance": x.get("resonance"),
                    "bull": x.get("bull"), "bear": x.get("bear"),
                    "votes": x.get("votes")}
    return None


def coverage_report():
    """Which engine outputs are present and how many rows each carries.

    cross_check() silently returns None for a missing engine, which makes a
    half-populated warehouse look like a clean "no signal". This surfaces it.
    """
    out = {}
    for name, key, count_of in (
            ("engine_v5", "top", lambda d: len(d.get("top") or [])),
            ("ai_pick_v4", "picks", lambda d: len(d.get("picks") or [])),
            ("ban_pick", "picks", lambda d: len(d.get("picks") or [])),
            ("reflect_latest", "n_records", lambda d: d.get("n_records")),
            ("overseas_impact", "bias", lambda d: d.get("bias")),
            ("entry_plans", "plans", lambda d: len(d.get("plans") or [])),
            ("close_review", "date", lambda d: d.get("date")),
    ):
        d = load_json(f"{name}.json")
        if not d:
            out[name] = {"present": False}
            continue
        try:
            n = count_of(d)
        except Exception:
            n = None
        out[name] = {"present": True, "ts": d.get("ts") or d.get("generated_at"),
                     "count": n}
    return out


def ban_pick_prob(code):
    """ban_pick's T+1 probability for one code."""
    d = load_json("ban_pick.json") or {}
    for x in (d.get("picks") or []):
        if bare_of(x.get("code")) == bare_of(code):
            return {"lianban": x.get("lianban"),
                    "p_zt_next": x.get("p_zt_next"),
                    "p_zhaban_next": x.get("p_zhaban_next"),
                    "verdict": x.get("verdict")}
    return None


def v4_position(code):
    """ai_pick_v4's position/valuation view for one code."""
    d = load_json("ai_pick_v4.json") or {}
    for x in (d.get("picks") or []):
        if bare_of(x.get("code")) == bare_of(code):
            return {"pos240_pct": x.get("pos240_pct"),
                    "vratio": x.get("vratio"), "score": x.get("score")}
    return None


def overseas_bias():
    """The overseas impact model's verdict on today's tape."""
    d = load_json("overseas_impact.json") or {}
    return {"bias": d.get("bias"), "composite": d.get("composite"),
            "vix_state": d.get("vix_state"),
            "guidance": d.get("early_session_guidance"),
            "sector_transmission": d.get("sector_transmission") or []}


def atr(rows, n=14):
    if not rows or len(rows) < n + 1:
        return None
    trs = []
    for i in range(len(rows) - n, len(rows)):
        h, l, pc = rows[i][3], rows[i][4], rows[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


# ── cross-engine verdict ──────────────────────────────────────
def cross_check(code):
    """Combine every engine's view of one code into one verdict.

    Returns a dict with the per-engine stances plus an overall
    'actionable' flag. The point is the contradiction check: a code that
    engine_v5 loves but that is sitting at limit-up is NOT actionable, and a
    code whose T+1 break probability is high is downgraded regardless of score.

    engines_missing lists the engine outputs that were absent, so a
    half-populated warehouse is visible instead of reading as "no signal".
    """
    coverage = coverage_report()
    missing = [k for k, v in coverage.items() if not v.get("present")]
    q = quotes([code]).get(prefixed(code))
    ok, reason, detail = tradability(code, q)
    kl = klines([code]).get(str(code)) or []
    av = atr(kl)
    if not av and q:
        av = max((q["high"] - q["low"]) or 0.0, q["price"] * 0.015)

    v5 = v5_votes(code)
    bp = ban_pick_prob(code)
    v4 = v4_position(code)

    warnings = []
    if not ok:
        warnings.append(reason)
    if bp and bp.get("p_zhaban_next") and bp["p_zhaban_next"] >= 15:
        warnings.append(f"break prob {bp['p_zhaban_next']}%")
    if bp and bp.get("verdict") == "回避":
        warnings.append("ban_pick says avoid")
    if v4 and (v4.get("pos240_pct") or 0) >= 60:
        warnings.append(f"240d position {v4['pos240_pct']}% high")
    if v5 and (v5.get("bear") or 0) > (v5.get("bull") or 0):
        warnings.append(f"more bear than bull votes {v5['bull']}/{v5['bear']}")

    ov = overseas_bias()
    if ov["bias"] == "bearish" and not warnings:
        warnings.append("overseas bearish, size down")

    return {
        "code": bare_of(code),
        "tradable": ok,
        "tradability_reason": reason,
        "quote": detail,
        "atr": round(av, 3) if av else None,
        "engine_v5": v5,
        "ban_pick": bp,
        "ai_pick_v4": v4,
        "overseas_bias": ov["bias"],
        "warnings": warnings,
        "actionable": ok and not warnings,
        "engines_missing": missing,
    }


def actionable_codes(codes):
    """Split codes into actionable / rejected, with reasons."""
    good, bad = [], []
    for c in codes:
        r = cross_check(c)
        (good if r["actionable"] else bad).append(r)
    return good, bad


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", action="append")
    ap.add_argument("--codes", help="comma separated")
    a = ap.parse_args()
    cs = list(a.code or [])
    if a.codes:
        cs += [x for x in a.codes.split(",") if x]
    if not cs:
        ap.print_help()
        sys.exit(0)
    for c in cs:
        print(json.dumps(cross_check(c), ensure_ascii=False, indent=1))


# ── 量价微结构层（wyckoff_flow_engine.py） ────────────────────────
_WYCK = {"loaded": False, "by_code": {}, "meta": {}}


def wyckoff_view(code):
    """威科夫事件/阶段 + 三角洲时间窗。缺失返回 None。

    用途提示：阶段是描述，不是买卖。校准表（wyckoff_calibration.json）显示
    A 股里 `wyckoff:ut`（上冲回落）5 日收益反而为正——是洗盘不是派发。
    所以下游要看 edge 标签，不要看到 ut 就喊空。
    """
    if not _WYCK["loaded"]:
        d = load_json("wyckoff_flow_signals.json") or {}
        _WYCK["by_code"] = {bare_of(x.get("code", "")): x for x in (d.get("items") or [])}
        _WYCK["meta"] = {"generated_at": d.get("generated_at"), "count": d.get("count")}
        _WYCK["loaded"] = True
    return _WYCK["by_code"].get(bare_of(code))


def wyckoff_tags(code):
    return (wyckoff_view(code) or {}).get("tags") or []


_WYCK_CALIB = {"loaded": False, "table": {}}


def wyckoff_expectancy(code, min_n=100, min_t=1.5):
    """威科夫层的实测期望，逻辑同 structure_expectancy。"""
    if not _WYCK_CALIB["loaded"]:
        _WYCK_CALIB["table"] = (load_json("wyckoff_calibration.json") or {}).get("table") or {}
        _WYCK_CALIB["loaded"] = True
    table = _WYCK_CALIB["table"]
    tags = wyckoff_tags(code)
    if not table or not tags:
        return None
    cands = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            for key in (f"{a} & {b}", f"{b} & {a}"):
                if key in table:
                    cands.append((key, table[key]))
    for a in tags:
        if a in table:
            cands.append((a, table[a]))
    cands = [(k, v) for k, v in cands
             if v.get("n", 0) >= min_n and abs(v.get("t", 0)) >= min_t]
    if not cands:
        return None
    key, best = max(cands, key=lambda kv: abs(kv[1]["t"]))
    return {"basis": key, "n": best["n"], "t": best["t"], "edge_5d_pct": best["avg_fwd"],
            "win_rate": best["win_rate"], "edge": best["edge"]}


# ── 题材层（theme_engine.py） ──────────────────────────────────
def theme_context():
    """板块生命周期（描述市场环境用）。

    仅当 mapping_health().ok 时，才允许把题材阶段归因到个股；
    当前两套分类体系映射率只有 3.3% → 只能当环境，不能当个股理由。
    """
    import theme_engine as TE
    v = TE.theme_view()
    if not v.get("available"):
        return None
    h = TE.mapping_health(view=v)
    return {"as_of": v.get("as_of"), "stage_counts": v.get("stage_counts"),
            "top": v["boards"][:10], "mapping_ok": bool(h.get("ok")),
            "mapping_note": h.get("reason")}


# ── 基本面闸门（fundamental_filter.py） ─────────────────────────
def fundamental_gate(code):
    """None 表示「未检查」，不是「通过」。报告里不得写成基本面良好。"""
    try:
        import fundamental_filter as FF
        return FF.gate(code)
    except Exception as e:
        return {"status": None, "reason": f"基本面层不可用: {str(e)[:60]}"}


# ── 统一上下文入口 ────────────────────────────────────────────
def market_context(code):
    """把结构/威科夫/题材/基本面四层拼成一个 dict，供各引擎和报告统一取用。

    设计原则：**缺哪层就明说缺**，不返回默认中性值。下游拿到 None 必须自己决定
    是拒绝出信号还是降级，不能假装"没有坏消息就是好消息"。
    """
    sv = structure_view(code)
    wv = wyckoff_view(code)
    return {
        "code": code,
        "structure": {"tags": (sv or {}).get("tags"), "score": (sv or {}).get("score"),
                      "bias": (sv or {}).get("bias"),
                      "expectancy": structure_expectancy(code)},
        "wyckoff": {"tags": (wv or {}).get("tags"),
                    "stage": ((wv or {}).get("wyckoff") or {}).get("stage"),
                    "delta": (wv or {}).get("delta"),
                    "expectancy": wyckoff_expectancy(code)},
        "theme": theme_context(),
        "fundamental": fundamental_gate(code),
        "missing": [name for name, val in
                    (("structure", sv), ("wyckoff", wv)) if not val],
    }


# ─────────────── 多书信号层（反转 / 涨停 / 订单流）与多因子打分 ───────────────
# 这三层是 2026-10-30 按书单补的（#4/#3 反转与波段、#15/#29/#31 涨停结构、#9 订单流近似）。
# 纪律与结构/威科夫层一致：**只给描述与实测期望值，方向从校准表取，不写书本先验**。

_LAYER_CACHE = {}


def _calib(name):
    """校准表懒加载缓存（按文件名）。"""
    if name not in _LAYER_CACHE:
        d = load_json(name) or {}
        _LAYER_CACHE[name] = {"table": d.get("table") or {},
                              "meta": d.get("meta") or {}}
    return _LAYER_CACHE[name]


def _signals(name):
    """某引擎 signals 产物，按 bare code 索引。"""
    key = "__sig__" + name
    if key not in _LAYER_CACHE:
        d = load_json(name) or {}
        _LAYER_CACHE[key] = {bare_of(x.get("code", "")): x for x in (d.get("items") or [])}
    return _LAYER_CACHE[key]


def _fwd(v):
    """校准表字段兼容：结构层旧表用 avg_fwd_5d，其余四张用 avg_fwd。"""
    if v is None:
        return None
    return v.get("avg_fwd", v.get("avg_fwd_5d"))


_LAYER_FILES = {
    "structure": ("structure_signals.json", "structure_calibration.json"),
    "wyckoff": ("wyckoff_flow_signals.json", "wyckoff_calibration.json"),
    "reversal": ("reversal_signals.json", "reversal_calibration.json"),
    "limit": ("limit_signals.json", "limit_calibration.json"),
    "orderflow": ("orderflow_signals.json", "orderflow_calibration.json"),
}


def layer_view(layer, code):
    """某层的原始判读结果（标签/明细）。未生成产物时返回 None，不编造。"""
    if layer not in _LAYER_FILES:
        raise ValueError(f"unknown layer: {layer}")
    sig_file = _LAYER_FILES[layer][0]
    return _signals(sig_file).get(bare_of(code))


def layer_expectancy(layer, code, min_n=100, min_t=1.5):
    """把某层标签换算成实测 5 日期望收益。没命中可过检标签 → None。"""
    if layer not in _LAYER_FILES:
        raise ValueError(f"unknown layer: {layer}")
    cal_file = _LAYER_FILES[layer][1]
    table = _calib(cal_file)["table"]
    if not table:
        return None
    v = layer_view(layer, code)
    tags = (v or {}).get("tags") or []
    if not tags:
        return None
    cands = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            for key in (f"{a} & {b}", f"{b} & {a}"):
                if key in table:
                    cands.append((key, table[key]))
    for a in tags:
        if a in table:
            cands.append((a, table[a]))
    cands = [(k, x) for k, x in cands if x["n"] >= min_n and abs(x["t"]) >= min_t]
    if not cands:
        return None
    key, best = max(cands, key=lambda kv: abs(kv[1]["t"]))
    return {
        "layer": layer, "basis": key, "n": best["n"], "t": best["t"],
        "edge_5d_pct": _fwd(best), "win_rate": best["win_rate"],
        "edge": best["edge"],
        "horizon_days": _calib(cal_file)["meta"].get("horizon_days") or 5,
    }


def confluence_score(code, min_n=100, min_t=1.5, clamp_pct=3.0):
    """#24《模型理论：富致中源模型》的落地：多维打分，权重全部取自实测校准表。

    为什么这样打分（与书本先验的关系）：
      模型本身给的是「多维度综合评估」的框架，没有给权重；书本权重搬过来在 A 股
      短周期也不成立（本项目多处实测已证明方向会反）。因此维度只负责**收集证据**，
      权重与方向一律从校准表取：
        层分值 = 该层命中的最佳标签的 5 日期望收益（%）
        层权重 = min(|t|, 4) / 4       —— t 越大越可信，4 封顶避免单层霸榜
        总分   = Σ(层分 × 层权重) / Σ(层权重)，再线性映射到 0~100
      没有任何一层命中可过检标签 → 返回 None（不给分，不编 0）。

    分数含义：50 = 无优势；>50 表示按实测该股 5 日期望为正，越大越强。
    """
    dims = []
    for layer in _LAYER_FILES:
        e = layer_expectancy(layer, code, min_n=min_n, min_t=min_t)
        if e and e["edge_5d_pct"] is not None:
            w = min(abs(e["t"]), 4.0) / 4.0
            dims.append({**e, "weight": round(w, 3)})
    if not dims:
        return None
    num = sum(d["edge_5d_pct"] * d["weight"] for d in dims)
    den = sum(d["weight"] for d in dims)
    exp5 = num / den if den else 0.0
    score = 50.0 + 50.0 * max(-1.0, min(1.0, exp5 / clamp_pct))
    return {
        "code": bare_of(code),
        "score": round(score, 1),
        "expected_5d_pct": round(exp5, 2),
        "dimensions": sorted(dims, key=lambda d: -abs(d["t"])),
        "method": "层分×|t|权重归一；无过检标签的层不参与",
    }


def book_signal_context(code):
    """三层新引擎的合并视图（给推股理由用）。缺产物时如实写进 missing。"""
    out = {"code": bare_of(code), "layers": {}, "missing": []}
    for layer in ("reversal", "limit", "orderflow"):
        v = layer_view(layer, code)
        if v is None:
            out["missing"].append(layer)
            continue
        out["layers"][layer] = {
            "tags": v.get("tags"),
            "expectancy": layer_expectancy(layer, code),
            "note": v.get("note"),
        }
    return out

