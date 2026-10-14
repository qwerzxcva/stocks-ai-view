#!/usr/bin/env python3
"""close_review.py — end-of-day review of everything recommended today.

Answers one question with closed prices: did the picks make money?

It reviews three sets separately because they were produced at different times
and for different purposes:
  1. morning picks   (morning_review.json)  — the pre-open call
  2. afternoon picks (afternoon_picks.json) — the intraday revision
  3. the recommended subset (entry_plans.json) — what was actually flagged
     as tradable with a buy zone

Output: data/close_review.json
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


def collect(codes):
    """Live close for each code, with the day's range."""
    H.quotes(codes)
    out = {}
    for c in codes:
        q = H.quotes([c]).get(H.prefixed(c))
        if q:
            out[str(c)] = q
    return out


def summarise(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    up = sum(1 for r in rows if r["chg_pct"] > 0)
    return {
        "n": n,
        "n_up": up,
        "hit_rate_pct": round(up / n * 100, 1),
        "avg_ret_pct": round(sum(r["chg_pct"] for r in rows) / n, 2),
        "median_ret_pct": round(sorted(r["chg_pct"] for r in rows)[n // 2], 2),
        "best": max(r["chg_pct"] for r in rows),
        "worst": min(r["chg_pct"] for r in rows),
        "avg_turnover_pct": round(
            sum(r.get("turnover") or 0 for r in rows) / n, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    mr = load("morning_review.json") or {}
    af = load("afternoon_picks.json") or {}
    ep = load("entry_plans.json") or {}
    tp = load("today_picks.json") or {}
    rl = load("reflect_latest.json") or {}

    # ── the three sets ──
    morning_codes, afternoon_codes = [], []
    for g, s in (mr.get("groups") or {}).items():
        for r in (s.get("rows") or []):
            morning_codes.append(str(r["code"]))
    for p in (af.get("picks") or []):
        afternoon_codes.append(str(p["code"]))

    log(f"morning {len(morning_codes)}, afternoon {len(afternoon_codes)}")

    q_all = collect(sorted(set(morning_codes) | set(afternoon_codes)))
    log(f"closes: {len(q_all)}")

    morning_rows = []
    for g, s in (mr.get("groups") or {}).items():
        for r in (s.get("rows") or []):
            q = q_all.get(str(r["code"]))
            if not q:
                continue
            morning_rows.append({"code": r["code"], "name": q["name"],
                                 "group": g, "chg_pct": round(q["chg_pct"], 2),
                                 "close": q["price"],
                                 "turnover": q["turnover"],
                                 "morning_chg_pct": r.get("chg_pct")})
    afternoon_rows = []
    for p in (af.get("picks") or []):
        q = q_all.get(str(p["code"]))
        if not q:
            continue
        afternoon_rows.append({"code": p["code"], "name": q["name"],
                               "group": p.get("group"),
                               "chg_pct": round(q["chg_pct"], 2),
                               "close": q["price"],
                               "turnover": q["turnover"],
                               "afternoon_score": p.get("afternoon_score")})

    # ── what was actually tradable, with a zone ──
    plan_rows = []
    for p in (ep.get("plans") or []):
        q = q_all.get(str(p["code"]))
        if not q:
            continue
        zone = p.get("entry_zone") or [None, None]
        plan_rows.append({"code": p["code"], "name": q["name"],
                          "group": p.get("group"),
                          "chg_pct": round(q["chg_pct"], 2),
                          "close": q["price"], "turnover": q["turnover"],
                          "entry_zone": zone,
                          "stop_loss": p.get("stop_loss"),
                          "in_zone": (zone[0] is not None and
                                      zone[0] <= q["low"] or
                                      (zone[0] is not None and zone[1] is not None
                                       and q["low"] <= zone[1])),
                          "stop_hit": (p.get("stop_loss") is not None and
                                       q["low"] <= p["stop_loss"]),
                          "target_hit": (p.get("target") is not None and
                                         q["high"] >= p["target"])})

    # ── per-group breakdown for the morning set ──
    by_group = {}
    for r in morning_rows:
        by_group.setdefault(r["group"], []).append(r)
    group_stats = {g: summarise(rs) for g, rs in by_group.items()}

    # ── score-decile check: did a higher score actually mean a better return? ──
    scored = [r for r in morning_rows
              if r.get("morning_chg_pct") is not None]
    # use the engine_v5 score when available
    v5 = {str(x.get("code"))[2:] if str(x.get("code", ""))[:2] in ("sh", "sz", "bj")
          else str(x.get("code")): x.get("score")
          for x in ((load("engine_v5.json") or {}).get("top") or [])}
    for r in morning_rows:
        r["engine_v5_score"] = v5.get(r["code"])
    with_score = [r for r in morning_rows if r.get("engine_v5_score") is not None]
    score_check = None
    if len(with_score) >= 10:
        with_score.sort(key=lambda r: -(r["engine_v5_score"] or 0))
        k = len(with_score) // 2
        top, bot = with_score[:k], with_score[k:]
        score_check = {
            "n": len(with_score),
            "top_half_avg": round(sum(r["chg_pct"] for r in top) / len(top), 2),
            "bottom_half_avg": round(sum(r["chg_pct"] for r in bot) / len(bot), 2),
            "top_half_hit": round(sum(1 for r in top if r["chg_pct"] > 0) / len(top) * 100, 1),
            "bottom_half_hit": round(sum(1 for r in bot if r["chg_pct"] > 0) / len(bot) * 100, 1),
        }

    # ── tradability filter check ──
    rejected = (ep.get("rejected") or [])
    rej_rows = []
    for r in rejected:
        q = q_all.get(str(r["code"]))
        if q:
            rej_rows.append({"code": r["code"], "name": q["name"],
                             "chg_pct": round(q["chg_pct"], 2),
                             "reason": r.get("reason")})
    limitup_rej = [r for r in rej_rows if "limit" in str(r.get("reason"))]
    veto_rej = [r for r in rej_rows if "veto" in str(r.get("reason"))]

    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "close_review",
        "date": datetime.now(CST).strftime("%Y-%m-%d"),
        "market": {
            "phase": (tp.get("market") or {}).get("phase"),
            "phase_desc": (tp.get("market") or {}).get("phase_desc"),
            "advance": (tp.get("market") or {}).get("advance"),
            "decline": (tp.get("market") or {}).get("decline"),
        },
        "morning": {"summary": summarise(morning_rows),
                    "by_group": group_stats,
                    "rows": sorted(morning_rows, key=lambda r: -r["chg_pct"])},
        "afternoon": {"summary": summarise(afternoon_rows),
                      "rows": sorted(afternoon_rows, key=lambda r: -r["chg_pct"])},
        "tradable_plans": {
            "summary": summarise(plan_rows),
            "n_stop_hit": sum(1 for r in plan_rows if r["stop_hit"]),
            "n_target_hit": sum(1 for r in plan_rows if r["target_hit"]),
            "rows": sorted(plan_rows, key=lambda r: -r["chg_pct"]),
        },
        "score_effectiveness": score_check,
        "rejected_review": {
            "n": len(rej_rows),
            "n_limit_up": len(limitup_rej),
            "n_vetoed": len(veto_rej),
            "limit_up_avg": round(
                sum(r["chg_pct"] for r in limitup_rej) / len(limitup_rej), 2)
            if limitup_rej else None,
            "vetoed_avg": round(
                sum(r["chg_pct"] for r in veto_rej) / len(veto_rej), 2)
            if veto_rej else None,
            "rows": sorted(rej_rows, key=lambda r: -r["chg_pct"]),
        },
        "historical_winrate": {
            h: {"n": v.get("n"), "bull_win_rate": v.get("bull_win_rate"),
                "avg_ret_pct": v.get("avg_ret_pct")}
            for h, v in ((rl.get("analysis") or {}).get("overall") or {}).items()},
        "disclaimer": "Single-day result. Not predictive.",
    }

    # ── prior reviews: a single day means nothing without the history ──
    # pull the cloud archive first so the comparison is against every closed
    # day, not just the ones this machine happens to have seen
    hist_path = os.path.join(H.DATA_DIR, "review_history.jsonl")
    try:
        import urllib.request
        url = ("https://raw.githubusercontent.com/jiegengxiangjianxue1/"
               "db-market/main/market/review_history.jsonl")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        body = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        if body.strip():
            with open(hist_path, "w", encoding="utf-8") as f:
                f.write(body)
            log(f"pulled cloud archive: {body.count(chr(10))} rows")
    except Exception:
        pass
    prior = []
    if os.path.exists(hist_path):
        for line in open(hist_path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                prior.append(json.loads(line))
            except Exception:
                pass
    prior = [p for p in prior if p.get("date") != out["date"]]
    trend = None
    if len(prior) >= 3:
        recent = prior[-10:]
        hits = [p.get("hit_rate_buyable_pct") for p in recent
                if p.get("hit_rate_buyable_pct") is not None]
        avgs = [p.get("avg_ret_buyable_pct") for p in recent
                if p.get("avg_ret_buyable_pct") is not None]
        edges = [p.get("score_edge_pct") for p in recent
                 if p.get("score_edge_pct") is not None]
        if hits or avgs:
            trend = {
                "n_days": len(recent),
                "mean_hit_rate_pct": round(sum(hits) / len(hits), 1) if hits else None,
                "mean_avg_ret_pct": round(sum(avgs) / len(avgs), 2) if avgs else None,
                "days_above_50": sum(1 for h in hits if h > 50),
                "mean_score_edge_pct": round(sum(edges) / len(edges), 2) if edges else None,
                "recent": [{"date": p.get("date"),
                            "hit": p.get("hit_rate_buyable_pct"),
                            "avg": p.get("avg_ret_buyable_pct"),
                            "phase": p.get("market_phase")}
                           for p in recent],
            }
    out["prior_reviews"] = {"n": len(prior), "trend": trend}

    # ── what a hard stop would have done (stop_analysis.py) ──
    sa = load("stop_analysis.json") or {}
    stop_view = None
    if sa.get("no_stop") and sa.get("by_stop"):
        base_mean = sa["no_stop"].get("mean") or 0
        # only a stop that actually cut something can be judged; a level with
        # n_cut=0 is not a stop at all, it is the no-stop case renamed
        real = [(k, v) for k, v in sa["by_stop"].items()
                if (v.get("n_cut") or 0) > 0]
        best = None
        for k, v in real:
            if best is None or (v.get("mean") or -99) > (best[1].get("mean") or -99):
                best = (k, v)
        if best:
            helped = (best[1].get("mean") or -99) > base_mean
            stop_view = {
                "no_stop": sa["no_stop"],
                "best_stop": best[0],
                "best_stop_mean": best[1].get("mean"),
                "best_stop_n_cut": best[1].get("n_cut"),
                "by_stop": sa["by_stop"],
                "helped": helped,
                "verdict": (
                    f"a {best[0]} stop would have helped today "
                    f"({best[1].get('mean')}% vs {base_mean}%), cutting "
                    f"{best[1].get('n_cut')} picks"
                    if helped else
                    f"no stop level helped today: the best ({best[0]}) still "
                    f"trails no-stop ({best[1].get('mean')}% vs {base_mean}%), "
                    "because most stopped picks recovered by the close"),
            }
        else:
            stop_view = {"no_stop": sa["no_stop"], "by_stop": sa["by_stop"],
                         "verdict": "no stop level triggered today"}

    out["stop_analysis"] = stop_view

    if a.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
        return 0

    path = os.path.join(H.DATA_DIR, "close_review.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"-> {path}")

    print()
    print("=== MORNING (pre-open call) ===")
    for k, v in out["morning"]["summary"].items():
        print(f"  {k}: {v}")
    print("  by group:")
    for g, s in group_stats.items():
        print(f"    {g:10s} n={s['n']:>2} hit={s['hit_rate_pct']:>5}% "
              f"avg={s['avg_ret_pct']:+.2f}%")
    print()
    print("=== AFTERNOON (intraday revision) ===")
    for k, v in out["afternoon"]["summary"].items():
        print(f"  {k}: {v}")
    print()
    print("=== TRADABLE PLANS (with a buy zone) ===")
    s = out["tradable_plans"]["summary"]
    print(f"  n={s.get('n')} hit={s.get('hit_rate_pct')}% "
          f"avg={s.get('avg_ret_pct')}% median={s.get('median_ret_pct')}%")
    print(f"  stop hit: {out['tradable_plans']['n_stop_hit']}, "
          f"target hit: {out['tradable_plans']['n_target_hit']}")
    print()
    if score_check:
        print("=== DID A HIGHER SCORE HELP? ===")
        print(f"  n={score_check['n']}")
        print(f"  top half    avg={score_check['top_half_avg']:+.2f}% "
              f"hit={score_check['top_half_hit']}%")
        print(f"  bottom half avg={score_check['bottom_half_avg']:+.2f}% "
              f"hit={score_check['bottom_half_hit']}%")
    print()
    print("=== PRIOR REVIEWS (context for today) ===")
    pv = out.get("prior_reviews") or {}
    tr = pv.get("trend")
    if not tr:
        print(f"  archived days: {pv.get('n', 0)} (need >= 3 for a trend)")
    else:
        print(f"  archived days: {pv.get('n')}, trend over last {tr['n_days']}")
        print(f"  mean hit (buyable): {tr['mean_hit_rate_pct']}%  "
              f"days above 50%: {tr['days_above_50']}/{tr['n_days']}")
        print(f"  mean avg  (buyable): {tr['mean_avg_ret_pct']}%")
        print(f"  mean score edge: {tr['mean_score_edge_pct']}pp")
        print("  recent:")
        for r in (tr.get("recent") or [])[-6:]:
            print(f"    {r['date']} hit={r['hit']}% avg={r['avg']}% phase={r['phase']}")
    print()
    if stop_view:
        print("=== HARD STOP CHECK ===")
        print(f"  {stop_view['verdict']}")
        print()
    print("=== REJECTED ===")
    rv = out["rejected_review"]
    print(f"  limit-up ({rv['n_limit_up']}): avg {rv['limit_up_avg']}%")
    print(f"  vetoed   ({rv['n_vetoed']}): avg {rv['vetoed_avg']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
