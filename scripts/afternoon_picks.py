#!/usr/bin/env python3
"""afternoon_picks.py — afternoon-session pick engine.

The morning list was built before the open, so it could not know how the tape
actually behaved. This engine runs on the morning's realised data and answers a
different question: given that the morning already happened, what is actionable
in the afternoon session.

It keeps the morning snapshot untouched (data/morning_review.json) and writes
its own file, so the dashboard can show both side by side.

Inputs (all already produced by the earlier engines):
  morning_review.json   how the morning picks actually traded
  today_picks.json      the morning candidate set + market context
  entry_plans.json      entry zones from the morning
  engine_v5.json / ai_pick_v4.json / ban_pick.json / reflect_latest.json
  overseas_impact.json

Output: data/afternoon_picks.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signal_hub as H

DISCLAIMER = ("Rule engine output on public data. Research reference only, "
              "not investment advice. Win rates are historical, not predictive.")


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def load(name):
    p = os.path.join(H.DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tp = load("today_picks.json") or {}
    mr = load("morning_review.json") or {}
    ep = load("entry_plans.json") or {}
    rv = load("review_today.json") or {}

    morning_groups = (mr.get("groups") or {})
    morning_rows = {}
    for g, s in morning_groups.items():
        for r in (s.get("rows") or []):
            morning_rows[str(r["code"])] = {**r, "group": g,
                                            "morning_hit": (r["chg_pct"] or 0) > 0}

    log(f"morning picks: {len(morning_rows)}")

    # ── rebuild the candidate set from the morning picks ──
    codes = list(morning_rows.keys())
    H.quotes(codes)
    H.klines(codes)

    plans_by_code = {str(p["code"]): p for p in (ep.get("plans") or [])}

    rows = []
    for code in codes:
        m = morning_rows[code]
        check = H.cross_check(code)
        quote = H.quotes([code]).get(H.prefixed(code))
        if not quote:
            continue
        plan = plans_by_code.get(code) or {}
        rows.append({
            "code": code,
            "name": quote["name"],
            "group": m["group"],
            "morning_chg_pct": m.get("chg_pct"),
            "morning_hit": m["morning_hit"],
            "price": quote["price"],
            "prev_close": quote["prev_close"],
            "chg_pct": round(quote["chg_pct"], 2),
            "high": quote["high"],
            "low": quote["low"],
            "turnover": quote["turnover"],
            "amount_yi": round(quote["amount"] / 1e8, 2) if quote["amount"] else None,
            "tradable": check["tradable"],
            "tradability_reason": check["tradability_reason"],
            "vetoes": [w for w in check["warnings"]
                       if w != "overseas bearish, size down"],
            "engine_v5_score": (check["engine_v5"] or {}).get("score"),
            "engine_v5_bull_bear": (
                f"{check['engine_v5']['bull']}/{check['engine_v5']['bear']}"
                if check["engine_v5"] else None),
            "ban_pick_verdict": (check["ban_pick"] or {}).get("verdict"),
            "p_zt_next": (check["ban_pick"] or {}).get("p_zt_next"),
            "p_zhaban_next": (check["ban_pick"] or {}).get("p_zhaban_next"),
            "pos240_pct": (check["ai_pick_v4"] or {}).get("pos240_pct"),
            "entry_zone": plan.get("entry_zone"),
            "stop_loss": plan.get("stop_loss"),
            "target": plan.get("target"),
            "risk_pct": plan.get("risk_pct"),
            "reward_pct": plan.get("reward_pct"),
            # the morning row's own low, needed by the afternoon scoring
            "morning_low": m.get("low"),
        })

    tradable = [r for r in rows if r["tradable"] and not r["vetoes"]]
    log(f"tradable now: {len(tradable)}/{len(rows)}")

    # ── afternoon-specific reasoning ──
    # The afternoon question is not "does this look good" but "is there an entry
    # left today". Three cases matter:
    #   A. morning gainer still below its zone ceiling  -> chase risk, demote
    #   B. morning loser that held support              -> second-chance entry
    #   C. flat / untraded with volume building         -> cleanest entry
    for r in tradable:
        mg = r["morning_chg_pct"] or 0
        cg = r["chg_pct"]
        zone = r["entry_zone"]
        reasons = []
        score = 0.0

        if zone and r["price"] > zone[1]:
            score -= 2.0
            reasons.append(f"price {r['price']} above the morning zone "
                           f"ceiling {zone[1]}")
        elif zone and zone[0] <= r["price"] <= zone[1]:
            score += 1.5
            reasons.append("inside the morning entry zone")

        if mg > 3:
            score -= 1.0
            reasons.append(f"already up {mg:+.2f}% this morning, chase risk")
        elif mg < -3:
            # a real reclaim needs the price to have come back off the low,
            # not merely to still be down. Compare now vs the morning low.
            low_ref = r["morning_low"] or r["low"]
            off_low = ((r["price"] - low_ref) / low_ref * 100) if low_ref else 0
            if off_low >= 1.5 and cg > mg:
                score += 1.5
                reasons.append(f"morning low {mg:+.2f}%, now {cg:+.2f}% "
                               f"({off_low:+.1f}% off the low), reclaimed")
            elif off_low >= 0.5:
                score += 0.0
                reasons.append(f"morning low {mg:+.2f}%, only {off_low:+.1f}% "
                               f"off the low, no reclaim yet")
            else:
                score -= 1.0
                reasons.append(f"morning low {mg:+.2f}% and still at the low, "
                               f"support not holding")
        elif -1 <= mg <= 1:
            score += 1.0
            reasons.append("flat in the morning, no chase premium")

        if r["turnover"] >= 5:
            score += 0.5
            reasons.append(f"active, turnover {r['turnover']}%")
        elif r["turnover"] < 0.5:
            score -= 1.0
            reasons.append(f"thin, turnover {r['turnover']}%")

        if r["engine_v5_bull_bear"]:
            b, s2 = r["engine_v5_bull_bear"].split("/")
            if int(b) >= 3 * max(1, int(s2)):
                score += 0.5
                reasons.append(f"votes {r['engine_v5_bull_bear']}")

        if r["p_zhaban_next"] and r["p_zhaban_next"] >= 15:
            score -= 1.0
            reasons.append(f"break prob {r['p_zhaban_next']}%")

        if r["pos240_pct"] is not None and r["pos240_pct"] < 30:
            score += 0.5
            reasons.append(f"low 240d position {r['pos240_pct']}%")

        r["afternoon_score"] = round(score, 2)
        r["afternoon_reasons"] = reasons

    tradable.sort(key=lambda r: -r["afternoon_score"])

    rejected = [{
        "code": r["code"], "name": r["name"], "group": r["group"],
        "chg_pct": r["chg_pct"],
        "reason": r["tradability_reason"] if not r["tradable"]
                  else "cross-engine veto: " + "; ".join(r["vetoes"]),
    } for r in rows if not (r["tradable"] and not r["vetoes"])]

    ov = H.overseas_bias()
    market = (tp.get("market") or {})
    winrate = ((tp.get("winrate") or {}).get("horizons") or {})

    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "afternoon_picks",
        "session": "afternoon",
        "date": datetime.now(CST).strftime("%Y-%m-%d"),
        "market": {
            "phase": market.get("phase"),
            "phase_desc": market.get("phase_desc"),
            "advance": market.get("advance"),
            "decline": market.get("decline"),
            "limit_up": market.get("limit_up"),
            "turnover_yi": market.get("turnover_yi"),
        },
        "overseas": {"bias": ov.get("bias"), "composite": ov.get("composite"),
                     "vix_state": ov.get("vix_state"),
                     "guidance": ov.get("guidance")},
        "morning_summary": {
            g: {"n": s.get("n"), "hit_rate_pct": s.get("hit_rate_pct"),
                "avg_ret_pct": s.get("avg_ret_pct")}
            for g, s in morning_groups.items()},
        "yesterday_review": {
            "n_picks": rv.get("n_picks"), "n_up": rv.get("n_up"),
            "hit_rate_pct": rv.get("hit_rate_pct"),
            "avg_ret_pct": rv.get("avg_ret_pct")},
        "winrate_horizons": winrate,
        "n_candidates": len(rows),
        "n_actionable": len(tradable),
        "n_rejected": len(rejected),
        "rejected": rejected,
        "picks": tradable[:15],
        "method": ("afternoon score = morning zone position + chase penalty + "
                   "support reclaim + turnover + vote margin + 240d position"),
        "disclaimer": DISCLAIMER,
    }

    if a.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
        return 0

    path = os.path.join(H.DATA_DIR, "afternoon_picks.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {path}")
    log(f"actionable {len(tradable)} / {len(rows)}")

    print()
    print("=== afternoon picks ===")
    for r in tradable[:12]:
        z = r["entry_zone"] or ["?", "?"]
        print(f"  {r['code']} {r['name']:10s} [{r['group']:8s}] "
              f"分{r['afternoon_score']:>5.1f}  现价{r['price']:>7.2f} "
              f"今{r['chg_pct']:+6.2f}% 早{r['morning_chg_pct']:+6.2f}%  "
              f"买区{z[0]:.2f}-{z[1]:.2f} 止损{r['stop_loss']} 换手{r['turnover']}%")
    if len(tradable) > 12:
        print(f"  ... {len(tradable)} actionable total")
    print()
    print("=== rejected ===")
    for r in rejected[:10]:
        print(f"  {r['code']} {r.get('name',''):10s} {r.get('chg_pct','')}  {r['reason']}")
    if len(rejected) > 10:
        print(f"  ... {len(rejected)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
