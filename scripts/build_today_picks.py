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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import empirical_edge

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
    try:
        out = {c: v.get("name", "") for c, v in
               json.load(open(p, encoding="utf-8"))["stocks"].items()}
    except Exception:
        out = {}
    if not out:
        # fallback: stock_names.json {"sz000001": "sz000001 平安银行"} — try both layouts
        for p2 in (os.path.join(DATA_DIR, "stock_names.json"),
                   os.path.join(DATA_DIR, "data", "stock_names.json"),
                   os.path.join(DATA_DIR, "search_index.json")):
            try:
                root = json.load(open(p2, encoding="utf-8"))
                if isinstance(root, dict):
                    for k, v in root.items():
                        if k in {"items", "meta", "ts", "generated_at", "schema_version"}:
                            continue
                        code = bare(k)
                        if not code or len(code) != 6:
                            continue
                        if isinstance(v, dict):
                            nm = v.get("name") or v.get("display_name") or ""
                        elif isinstance(v, str):
                            nm = v.split(" ", 1)[1] if " " in v else v
                        else:
                            continue
                        out[code] = nm
                    for row in root.get("items", []):
                        if isinstance(row, dict) and row.get("code"):
                            out[bare(row["code"])] = row.get("name") or row.get("display_name") or ""
                if out:
                    break
            except Exception:
                continue
    return out


MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002")
INDEX_CODES = {"000001", "000300", "000905", "000852", "399001", "399006", "399300", "399905", "399673"}


def bare(code):
    c = str(code)
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def is_recommendable_stock(code, name=""):
    code = bare(code)
    name = str(name or "")
    if not code.startswith(MAIN_BOARD_PREFIXES) or code in INDEX_CODES:
        return False
    if any(token in name.upper() for token in ("ETF", "LOF", "指数", "基金", "债", "转债")):
        return False
    if "ST" in name.upper() or "退" in name:
        return False
    return True


def buyability(code, latest, now=None):
    """Return current-session execution status; stale snapshots are not tradable evidence."""
    unknown = (None, "实时可成交状态缺失，需盘前/盘口复核")
    if any(bare(x.get("code")) == code
           for x in (latest.get("limit_up_pool") or []) if isinstance(x, dict)):
        return False, "最新涨停池标记为涨停，不列为普通可买候选"
    for key in ("halted", "suspended"):
        pool = latest.get(key) or []
        if isinstance(pool, dict):
            pool = pool.values()
        if any(bare(x.get("code") if isinstance(x, dict) else x) == code
               for x in pool):
            return False, "最新状态标记为停牌/暂停交易"

    records = []
    for pool in latest.values():
        if isinstance(pool, list):
            records.extend(x for x in pool if isinstance(x, dict)
                           and bare(x.get("code")) == code)
    for record in records:
        for key in ("halted", "suspended", "is_halted", "is_suspended"):
            if record.get(key) is True:
                return False, "最新状态标记为停牌/暂停交易"
        status = str(record.get("trade_status") or record.get("status")
                      or record.get("state") or "").lower()
        if status in ("halted", "suspended", "停牌", "暂停交易", "s"):
            return False, "最新状态标记为停牌/暂停交易"

    current_quote = any(record.get("price") not in (None, 0, "", "0")
                        and record.get("chg_pct") is not None
                        for record in records)
    snapshot_ts = latest.get("ts")
    if not current_quote or not snapshot_ts:
        return unknown
    try:
        snapshot_time = datetime.fromisoformat(str(snapshot_ts).replace("Z", "+00:00"))
        reference_time = now or datetime.now(CST)
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=CST)
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=CST)
        age_seconds = (reference_time - snapshot_time.astimezone(reference_time.tzinfo)).total_seconds()
    except (TypeError, ValueError):
        return unknown
    if age_seconds < 0 or age_seconds > 15 * 60:
        return None, "行情快照超过15分钟，候选仅观察；次日需重新检查涨停/停牌"
    return True, "15分钟内行情快照未标记涨停/停牌；下单前仍需复核盘口"


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
    # 2026-10-17 起带实证校准: reflect_latest 实测全部指标 +1 票次日胜率
    # 43-45% (负边际), 故每只 signal pick 标注 edge_pp / p_up, 看空对齐的
    # 移入 avoid 回避榜 (实测 -1 票下跌实现度 54-60%)。
    calibration = empirical_edge.load_edge_table()
    signals = []
    avoid = []
    for x in (v5.get("top") or [])[:60]:
        code = bare(x.get("code"))
        if not is_recommendable_stock(code, master.get(code)):
            continue  # 推荐只保留主板普通股票，不含ETF/指数/ST
        if x.get("price") in (None, 0) or x.get("score") is None or x.get("resonance") is None:
            continue
        cal = empirical_edge.calibrate(x.get("votes"), calibration) \
            if calibration else {"edge_pp": None, "dir_weight": None}
        aligned, bear_w = empirical_edge.bear_aligned(x.get("votes"), calibration) \
            if calibration else (False, 0.0)
        buyable, buyability_reason = buyability(code, lt)
        row = {
            "code": code,
            "name": master.get(code) or "",
            "price": x.get("price"),
            "date": x.get("date"),
            "score": x.get("score"),
            "resonance": x.get("resonance"),
            "bull": x.get("bull"), "bear": x.get("bear"),
            "edge_pp": cal["edge_pp"],
            "p_up": (round(50 + cal["edge_pp"], 1)
                     if cal["edge_pp"] is not None else None),
            "bear_aligned": aligned,
            "engine": "engine_v5",
            "buyable": buyable,
            "buyability_reason": buyability_reason,
            "why": [v.get("why") for v in (x.get("votes") or []) if v.get("vote") == 1][:4],
        }
        if aligned:
            row["bear_weight"] = bear_w
            avoid.append(row)
        else:
            signals.append(row)

    # ── gold-cell scanner picks: positive_signal_scan (2026-10-25) ──
    # G5_52w_lowvol 次日+2.93~3.17% (27个月次日全正), S1 52W突破次日+0.66%;
    # 负警报 A1/A2 命中的票强制进 avoid
    try:
        sys.path.insert(0, "/home/agentuser/stockdb/t1/alphas")
        from positive_signal_scan import scan_today
        _scan = scan_today()
        _in_sig = {r["code"] for r in signals}
        for code in [c[2:] for c in _scan.get("G5_52w_lowvol", [])]:
            if is_recommendable_stock(code, master.get(code)) and code not in _in_sig:
                signals.insert(0, {
                    "code": code, "name": master.get(code) or "",
                    "score": 0.95, "resonance": ["gold_cell_G5"],
                    "engine": "gold_scan",
                    "edge_pp": 2.9, "p_up": 93.0,
                    "buyable": True, "buyability_reason": "gold cell G5: 52W breakout + low vol",
                    "why": ["52W真突破+低波动 (G5金格子, 次日+2.93%/5日+7.43%, 27个月全正)"],
                })
                _in_sig.add(code)
        _alert = set(c[2:] for c in _scan.get("A1_unlock", []) + _scan.get("A2_zt_big_red", []))
        for r in list(signals):
            if r["code"] in _alert and r.get("engine") != "gold_scan":
                signals.remove(r)
                r["bear_aligned"] = True
                r["bear_weight"] = 1.0
                avoid.append(r)
    except Exception as _e:
        print(f"  gold_scan skipped: {_e}", file=sys.stderr)

    # ── value picks: ai_pick_v4 with valuation + position filters ──
    values = []
    for x in (v4.get("picks") or [])[:20]:
        code = bare(x.get("code"))
        name = x.get("name") or master.get(code) or ""
        if not is_recommendable_stock(code, name):
            continue
        buyable, buyability_reason = buyability(code, lt)
        values.append({
            "code": code,
            "name": name,
            "price": x.get("price"),
            "ret_pct": x.get("ret_pct"),
            "vratio": x.get("vratio"),
            "pos240_pct": x.get("pos240_pct"),
            "score": x.get("score"),
            "engine": "ai_pick_v4",
            "buyable": buyable,
            "buyability_reason": buyability_reason,
        })

    # 涨停仅用于观察/归因，不进入今日推荐，避免把不可追涨停标的误当买入候选。
    zt = []

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
            "signals": signals[:30],
            "values": values,
            "limit_up": zt,
            "avoid": avoid,
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
          f"limit_up {len(zt)} / avoid {len(avoid)}")
    print(f"  winrate horizons: " +
          ", ".join(f"{h}d={v.get('bull_win_rate')}%(n={v.get('n')})"
                    for h, v in sorted(winrate.items())))
    print(f"  overseas: {overseas['bias']} ({overseas['composite']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
