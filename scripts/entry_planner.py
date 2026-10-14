#!/usr/bin/env python3
"""entry_planner.py — turn ranked picks into executable entry plans.

Two problems this solves:

1. Picks that already hit limit-up cannot be bought. The old filter
   (ret >= 0.095) only caught a close AT limit-up; a stock that gapped to
   +9.9% and closed +6% still passed. This module pulls live quotes and
   rejects anything whose current price is at or within 1% of its limit,
   plus anything already up too far to chase.

2. No entry price was ever given. A score says "this looks good" but the
   reader still has to decide where to buy. This module computes a concrete
   entry zone from the stock's own volatility: the prior close, the day's
   VWAP, and an ATR-based band. Each plan carries a stop and a target so
   the risk is explicit.

Outputs data/entry_plans.json, consumed by the dashboard's picks panel.
"""
import argparse
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

# shared analysis layer: quotes/klines cache, tradability, cross-engine verdicts
import signal_hub as H

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
DISCLAIMER = ("Rule engine output on public data. Research reference only, "
              "not investment advice. Levels are computed, not guaranteed.")


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


# ── live quotes ───────────────────────────────────────────────
def prefixed(code):
    c = str(code)
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith(("6", "5", "9")) else
            "bj" if c.startswith(("4", "8", "9")) else "sz") + c


def fetch_quotes(codes):
    """Tencent qt; returns {prefixed_code: {...}}."""
    out = {}
    for i in range(0, len(codes), 20):
        url = "https://qt.gtimg.cn/q=" + ",".join(codes[i:i + 20])
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
                out[m.group(1)] = {
                    "name": p[1],
                    "price": float(p[3] or 0),
                    "prev_close": float(p[4] or 0),
                    "open": float(p[5] or 0),
                    "high": float(p[33] or 0),
                    "low": float(p[34] or 0),
                    "chg_pct": float(p[32] or 0),
                    "volume": float(p[36] or 0),
                    "amount": float(p[37] or 0),
                    "turnover": float(p[38] or 0),
                    "pe": float(p[39] or 0),
                    "amplitude": float(p[43] or 0),
                    "limit_up": float(p[47] or 0),
                    "limit_down": float(p[48] or 0),
                    "avg_price": float(p[45] or 0) if len(p) > 45 else 0,
                    "time": p[30],
                }
            except (ValueError, IndexError):
                pass
    return out


# ── local klines for ATR ──────────────────────────────────────
def load_klines(codes):
    want = set()
    for c in codes:
        c = str(c)
        want.add(c)
        want.add(c[2:] if c[:2] in ("sh", "sz", "bj") else c)
        want.add(prefixed(c))
    out = {}
    import glob
    for p in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "|" not in line or line.startswith("#"):
                    continue
                parts = line.strip().split("|")
                if len(parts) < 6 or parts[0] not in want:
                    continue
                try:
                    out.setdefault(parts[0], []).append(
                        [parts[1], float(parts[2]), float(parts[3]),
                         float(parts[4]), float(parts[5]),
                         float(parts[6]) if len(parts) > 6 else 0.0])
                except ValueError:
                    pass
    for s in out:
        out[s].sort(key=lambda r: r[0])
    return out


def atr(rows, n=14):
    """Average true range over the last n bars."""
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(len(rows) - n, len(rows)):
        h, l, pc = rows[i][3], rows[i][4], rows[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def limit_pct(code):
    c = str(code)
    bare = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if bare.startswith(("30", "301")):
        return 0.20          # ChiNext
    if bare.startswith("688"):
        return 0.20          # STAR
    if bare.startswith("bj") or bare.startswith(("43", "83", "87", "88", "92")):
        return 0.30          # BSE
    return 0.10              # main board


def is_tradable(code, q):
    """Reject anything that cannot realistically be bought now."""
    c = str(code)
    if q["price"] <= 0 or q["prev_close"] <= 0:
        return False, "no quote"
    lim = limit_pct(c)
    up = q["prev_close"] * (1 + lim)
    # within 1% of the limit, or already AT/above it
    if q["price"] >= up * 0.99:
        return False, f"at limit-up ({q['chg_pct']:+.2f}%)"
    if q["chg_pct"] >= lim * 100 * 0.9:
        return False, f"near limit ({q['chg_pct']:+.2f}%)"
    # a gap far above the prior close is a chase, not an entry
    if q["chg_pct"] >= 7.0:
        return False, f"already up {q['chg_pct']:+.2f}%, chase risk"
    if q["turnover"] < 0.05:
        return False, "no liquidity"
    return True, "ok"


def plan_entry(code, q, a):
    """Entry zone from prior close + VWAP + ATR band.

    The zone is anchored to today's real price action, not to yesterday: the
    floor is a pullback toward VWAP/prev close, the ceiling is capped just
    above the current price so the plan is actually executable. A zone that
    sits entirely below the current price is useless as an entry.
    """
    px, pc = q["price"], q["prev_close"]
    vwap = q.get("avg_price") or px
    lo = min(pc, vwap, px) - a * 0.5
    hi = max(pc, vwap, px) + a * 0.3
    # never propose buying more than 2% above the live price (chase guard)
    hi = min(hi, px * 1.02)
    lo, hi = round(lo, 2), round(hi, 2)
    if lo > hi:
        lo, hi = hi, lo
    entry = round((lo + hi) / 2, 2)
    stop = round(entry - a * 1.2, 2)
    target = round(entry + a * 2.4, 2)
    risk = (entry - stop) / entry * 100 if entry else None
    return {
        "entry_zone": [lo, hi],
        "entry_ref": entry,
        "stop_loss": stop,
        "target": target,
        "risk_pct": round(risk, 2) if risk is not None else None,
        "reward_pct": round((target - entry) / entry * 100, 2) if entry else None,
        "rr_ratio": round((target - entry) / (entry - stop), 2) if entry and entry > stop else None,
        "atr": round(a, 3),
        "atr_pct": round(a / px * 100, 2) if px else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tp_path = os.path.join(DATA_DIR, "today_picks.json")
    if not os.path.exists(tp_path):
        log("today_picks.json missing; run build_today_picks.py first")
        return 1
    tp = json.load(open(tp_path, encoding="utf-8"))

    groups = (tp.get("picks") or {})
    codes = []
    meta = {}
    for g in ("signals", "values", "limit_up"):
        for r in (groups.get(g) or []):
            code = str(r["code"])
            if code in meta:
                # same stock can appear in several groups; keep the first and
                # remember the overlap so the plan is not emitted twice
                meta[code].setdefault("also_in", []).append(g)
                continue
            codes.append(code)
            meta[code] = {"group": g, "score": r.get("score"),
                          "name": r.get("name")}
    log(f"candidates: {len(codes)}")

    # warm the shared cache once; cross_check reuses it
    H.quotes(codes)
    H.klines(codes)
    log(f"hub warmed for {len(codes)} codes")

    plans, rejected = [], []
    for code in codes:
        check = H.cross_check(code)
        quote = H.quotes([code]).get(H.prefixed(code))
        if not quote:
            rejected.append({"code": code, "reason": "no quote"})
            continue
        if not check["tradable"]:
            rejected.append({"code": code, "name": quote["name"],
                             "group": meta[code]["group"],
                             "reason": check["tradability_reason"],
                             "chg_pct": round(quote["chg_pct"], 2)})
            continue
        # cross-engine vetoes: a strong score does not override a
        # contradiction from another engine
        vetoes = [w for w in check["warnings"]
                  if w not in ("overseas bearish, size down",)]
        av = check["atr"] or max((quote["high"] - quote["low"]) or 0.0,
                                 quote["price"] * 0.015)
        plan = plan_entry(code, quote, av)
        plan.update({
            "code": code, "name": quote["name"],
            "group": meta[code]["group"],
            "score": meta[code]["score"],
            "also_in": meta[code].get("also_in"),
            "price": quote["price"],
            "prev_close": quote["prev_close"],
            "chg_pct": round(quote["chg_pct"], 2),
            "turnover": quote["turnover"],
            "pe": quote["pe"] if quote["pe"] else None,
            "amplitude": quote["amplitude"],
            "time": quote["time"],
            # cross-engine context, so the reader sees why this passed
            "engine_v5_score": (check["engine_v5"] or {}).get("score"),
            "engine_v5_bull_bear": (
                f"{check['engine_v5']['bull']}/{check['engine_v5']['bear']}"
                if check["engine_v5"] else None),
            "ban_pick_verdict": (check["ban_pick"] or {}).get("verdict"),
            "pos240_pct": (check["ai_pick_v4"] or {}).get("pos240_pct"),
            "overseas_bias": check["overseas_bias"],
            "vetoes": vetoes,
            "actionable": not vetoes,
        })
        (plans if not vetoes else rejected).append(
            plan if not vetoes else {
                "code": code, "name": quote["name"],
                "group": meta[code]["group"], "chg_pct": plan["chg_pct"],
                "reason": "cross-engine veto: " + "; ".join(vetoes)})

    # values first (built for a weak tape), then by score; plans whose
    # risk is wider than 8% are demoted because a 1.2*ATR stop that far away
    # usually means the ATR is inflated, not that the trade is good
    plans.sort(key=lambda p: (p["group"] != "values",
                              (p["risk_pct"] or 99) > 8.0,
                              -(p["score"] if isinstance(p["score"], (int, float)) else 0)))
    log(f"tradable {len(plans)}, rejected {len(rejected)}")

    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "entry_planner",
        "n_candidates": len(codes),
        "n_tradable": len(plans),
        "n_rejected": len(rejected),
        "rejected": rejected,
        "plans": plans,
        "method": ("entry zone = min(prev_close, vwap) - 0.5*ATR .. "
                   "max(prev_close, vwap) + 0.3*ATR; "
                   "stop = entry - 1.2*ATR; target = entry + 2.4*ATR"),
        "disclaimer": DISCLAIMER,
    }

    if a.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
        return 0

    path = os.path.join(DATA_DIR, "entry_plans.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {path}")

    print()
    print("=== rejected (cannot buy now) ===")
    for r in rejected[:12]:
        print(f"  {r['code']} {r.get('name',''):10s} {r.get('chg_pct','')}  {r['reason']}")
    if len(rejected) > 12:
        print(f"  ... {len(rejected)} total")
    print()
    print("=== entry plans ===")
    for p in plans[:12]:
        print(f"  {p['code']} {p['name']:10s} [{p['group']:8s}] "
              f"现价{p['price']:>7.2f} 买区{p['entry_zone'][0]:.2f}-{p['entry_zone'][1]:.2f} "
              f"止损{p['stop_loss']:.2f} 目标{p['target']:.2f} "
              f"风险{p['risk_pct']}% 收益{p['reward_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
