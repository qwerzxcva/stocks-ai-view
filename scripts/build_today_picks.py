#!/usr/bin/env python3
"""build_today_picks.py — consolidate today's engine output into one file.

Reads the outputs of engine_v5 / ai_pick_v4 / ban_pick / reflect_engine plus
the overseas impact model, and writes data/today_picks.json: the single file
the dashboard's "today's picks" panel reads.

Each pick carries its provenance so the reader can tell which engine produced
it and how strong the evidence is. Win rates are historical, not predictive.

Usage:
    python3 build_today_picks.py --force        # regenerate from engine files
    python3 build_today_picks.py --dry-run      # print only
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

DISCLAIMER = ("Rule engines on public data. Research reference only, "
              "not investment advice. Win rates are historical, "
              "not predictive.")


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def names_from_master():
    """code -> name, from the cloud warehouse master table if present."""
    p = os.path.join(DATA_DIR, "universe.json")
    if not os.path.exists(p):
        return {}
    try:
        return {c: v.get("name", "") for c, v in
                json.load(open(p, encoding="utf-8"))["stocks"].items()}
    except Exception:
        return {}


def bare(code):
    c = str(code)
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="rebuild even during trading hours")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    now = datetime.now(CST)
    trading = (now.weekday() < 5 and
               ((9 <= now.hour < 12) or (13 <= now.hour < 16)))
    if trading and not a.force:
        print("[today_picks] trading hours; pass --force to regenerate")
        return 0

    master = names_from_master()

    v5 = load("engine_v5.json") or {}
    v4 = load("ai_pick_v4.json") or {}
    bp = load("ban_pick.json") or {}
    rl = load("reflect_latest.json") or {}
    ov = load("overseas_impact.json") or {}
    mp = load("market_phase.json") or {}
    lt = load("latest.json") or {}

    # ── signal picks: engine_v5 resonance ──
    signals = []
    for x in (v5.get("top") or [])[:30]:
        code = bare(x.get("code"))
        signals.append({
            "code": code,
            "name": master.get(code) or "",
            "price": x.get("price"),
            "date": x.get("date"),
            "score": x.get("score"),
            "resonance": x.get("resonance"),
            "bull": x.get("bull"), "bear": x.get("bear"),
            "engine": "engine_v5",
            "why": [v.get("why") for v in (x.get("votes") or []) if v.get("vote") == 1][:4],
        })

    # ── value picks: ai_pick_v4 with valuation + position filters ──
    values = []
    for x in (v4.get("picks") or [])[:20]:
        code = bare(x.get("code"))
        values.append({
            "code": code,
            "name": x.get("name") or master.get(code) or "",
            "price": x.get("price"),
            "ret_pct": x.get("ret_pct"),
            "vratio": x.get("vratio"),
            "pos240_pct": x.get("pos240_pct"),
            "score": x.get("score"),
            "engine": "ai_pick_v4",
        })

    # ── limit-up picks: ban_pick with T+1 probabilities ──
    zt = []
    for x in (bp.get("picks") or []):
        zt.append({
            "code": bare(x.get("code")),
            "name": x.get("name"),
            "lianban": x.get("lianban"),
            "p_zt_next": x.get("p_zt_next"),
            "p_zhaban_next": x.get("p_zhaban_next"),
            "prior_gain_5d": x.get("prior_gain_5d"),
            "verdict": x.get("verdict"),
            "engine": "ban_pick",
        })

    # ── historical win rates (reflect_engine) ──
    analysis = (rl.get("analysis") or {})
    overall = analysis.get("overall") or {}
    winrate = {}
    for h, v in overall.items():
        winrate[h] = {
            "n": v.get("n"),
            "bull_win_rate": v.get("bull_win_rate"),
            "bull_ci": v.get("bull_win_rate_ci"),
            "bear_win_rate": v.get("bear_win_rate"),
            "avg_ret_pct": v.get("avg_ret_pct"),
        }
    ind_raw = analysis.get("indicators") or {}
    # indicators is {name: {...}}; normalize to a list with the name attached
    if isinstance(ind_raw, dict):
        ind_list = [dict(v, name=k) for k, v in ind_raw.items()]
    else:
        ind_list = list(ind_raw)
    top_indicators = sorted(
        [i for i in ind_list if (i.get("n") or i.get("bull_n") or 0) >= 100],
        key=lambda i: -(i.get("quality") or 0))[:5]

    # ── overseas context ──
    overseas = {
        "bias": ov.get("bias"),
        "composite": ov.get("composite"),
        "components": ov.get("components"),
        "vix_state": ov.get("vix_state"),
        "a50_chg_pct": ov.get("a50_chg_pct"),
        "guidance": ov.get("early_session_guidance"),
        "sector_transmission": (ov.get("sector_transmission") or [])[:8],
    }

    market = (lt.get("market") or {})
    phase = (lt.get("phase") or {})

    # ── yesterday's picks vs today (review_yesterday.py) ──
    rv = load("review_today.json") or {}
    review = {
        "prev_snapshot": rv.get("prev_snapshot"),
        "n_picks": rv.get("n_picks"),
        "n_up": rv.get("n_up"),
        "hit_rate_pct": rv.get("hit_rate_pct"),
        "avg_ret_pct": rv.get("avg_ret_pct"),
        "best": rv.get("best"),
        "worst": rv.get("worst"),
    }

    # ── entry plans (entry_planner.py via signal_hub) ──
    ep = load("entry_plans.json") or {}
    entry = {
        "n_candidates": ep.get("n_candidates"),
        "n_tradable": ep.get("n_tradable"),
        "n_rejected": ep.get("n_rejected"),
        "method": ep.get("method"),
        "rejected": ep.get("rejected"),
        "plans": ep.get("plans"),
    }

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "session": "盘中" if trading else ("盘前" if now.hour < 12 else "盘后"),
        "generated_by": "build_today_picks.py",
        "market": {
            "advance": market.get("advance"),
            "decline": market.get("decline"),
            "limit_up": market.get("limit_up"),
            "limit_down": market.get("limit_down"),
            "turnover_yi": (market.get("turnover") or {}).get("total_yi"),
            "phase": phase.get("stage"),
            "phase_desc": phase.get("desc"),
            "phase_score": phase.get("score"),
            "market_phase": (mp.get("phase") or {}),
        },
        "overseas": overseas,
        "picks": {
            "signals": signals,
            "values": values,
            "limit_up": zt,
        },
        "winrate": {
            "horizons": winrate,
            "top_indicators": top_indicators,
            "n_pred_files": rl.get("n_pred_files"),
            "n_records": rl.get("n_records"),
        },
        "review": review,
        "entry_plans": entry,
        "engines": {
            "engine_v5": v5.get("ts"),
            "ai_pick_v4": v4.get("ts"),
            "ban_pick": bp.get("ts"),
            "reflect_latest": rl.get("ts"),
            "overseas_impact": ov.get("ts"),
        },
        "disclaimer": DISCLAIMER,
    }

    if a.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
        return 0

    path = os.path.join(DATA_DIR, "today_picks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[today_picks] -> {path}")
    print(f"  signals {len(signals)} / values {len(values)} / "
          f"limit_up {len(zt)}")
    print(f"  winrate horizons: " +
          ", ".join(f"{h}d={v.get('bull_win_rate')}%(n={v.get('n')})"
                    for h, v in sorted(winrate.items())))
    print(f"  overseas: {overseas['bias']} ({overseas['composite']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
