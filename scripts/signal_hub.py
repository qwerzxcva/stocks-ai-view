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
