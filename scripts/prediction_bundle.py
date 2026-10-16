#!/usr/bin/env python3
"""
prediction_bundle.py — 统一预测发布包

把主快照、全球市场、RTSI、engine_v5、多维因子、红队辩论、历史胜率和
analysis_manifest 合成一个可验证输出。它不是另一个独立打分器，而是发布层：
所有结论都绑定同一批次和数据新鲜度。

输出:
  data/prediction_bundle.json
    today_action       今天可操作状态与候选
    next_day_candidates 明日上涨候选（条件概率，不写成确定预测）
    market_direction   明日大盘方向概率与改变判断的条件
    evidence           每项证据、时间戳、数据完整性
"""
import argparse
import json
import os
from datetime import datetime, timezone, timedelta
from horizon_features import build_features
from paths import BULK

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.dirname(HERE) + "/data"


def load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def norm(code):
    s = str(code or "").lower().strip()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        s = s[2:]
    return s.zfill(6)


def ts_of(data):
    if not isinstance(data, dict):
        return None
    for key in ("ts", "timestamp", "generated", "date", "data_date"):
        if isinstance(data.get(key), str):
            return data[key]
    return None


def age_hours(ts, now):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts[:25])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return round((now - dt).total_seconds() / 3600, 2)
    except (TypeError, ValueError):
        return None


def manifest_status(manifest, group):
    return ((manifest or {}).get("groups") or {}).get(group, {}).get("status", "unknown")


def index_direction(latest, global_markets):
    market = (latest or {}).get("market") or {}
    indices = market.get("indices") or []
    vals = [x.get("chg_pct") for x in indices if isinstance(x.get("chg_pct"), (int, float))]
    a_score = sum(vals) / len(vals) if vals else 0.0
    gvals = []
    for x in (global_markets or {}).get("indices") or []:
        v = x.get("chg_pct")
        if isinstance(v, (int, float)):
            gvals.append(v)
    g_score = sum(gvals) / len(gvals) if gvals else 0.0
    adv = market.get("advance") or 0
    dec = market.get("decline") or 0
    breadth = (adv - dec) / max(1, adv + dec) * 100
    # 方向分: A股当日强弱占 60%, 全球占 25%, 涨跌宽度占 15%。
    score = a_score * 12 + g_score * 5 + breadth * 0.15
    if score >= 8:
        direction = "up"
    elif score <= -8:
        direction = "down"
    else:
        direction = "range"
    # 只把启发式分数压到概率，输出中明确标注 heuristic。
    p_up = max(0.15, min(0.75, 0.50 + score / 100))
    p_down = max(0.15, min(0.75, 0.50 - score / 100))
    p_range = max(0.10, 1 - p_up - p_down)
    total = p_up + p_down + p_range
    return {
        "direction": direction,
        "probability": {
            "up": round(p_up / total, 3),
            "down": round(p_down / total, 3),
            "range": round(p_range / total, 3),
        },
        "heuristic_score": round(score, 2),
        "components": {
            "a_share_index_avg_pct": round(a_score, 3),
            "global_index_avg_pct": round(g_score, 3),
            "breadth_pct": round(breadth, 2),
        },
        "evidence_ts": ts_of(latest),
        "global_ts": ts_of(global_markets),
        "method": "启发式方向概率，不是已完成样本外校准的确定预测",
        "change_conditions": [
            "A股主要指数转为同步下跌且上涨家数快速收窄 → 下调 up 概率",
            "全球市场由负转正且A股成交额/上涨家数同步改善 → 上调 up 概率",
            "涨跌停结构恶化或跌停数量明显增加 → 将 direction 降级为 down/range",
        ],
    }


def build_candidates(latest, engine, multidim, trend, debate, manifest):
    e_rows = (engine or {}).get("top") or []
    m_rows = (multidim or {}).get("top") or []
    t_rows = ((trend or {}).get("rtsi") or {}).get("stocks") or []
    pm = load("premarket_picks.json") or {}
    midday = load("midday_picks.json") or {}
    sources = ("engine_v5", "multidim", "trend_strength", "premarket", "midday")
    rows = {
        "engine_v5": e_rows[:100],
        "multidim": m_rows[:100],
        "trend_strength": t_rows[:100],
        "premarket": sum(((pm.get("tiers") or {}).get(tier) or [] for tier in ("A_buy", "B_watch")), []),
        "midday": sum(((midday.get("afternoon") or {}).get(tier) or [] for tier in ("keep", "watch", "new_strong")), []),
    }
    union = {}
    for source in sources:
        for item in rows[source]:
            code = norm(item.get("code"))
            if len(code) != 6 or not code.isdigit():
                continue
            union.setdefault(code, {})[source] = item
    history = build_features(BULK, list(union)) if union else {"features": {}}
    b_idx = {norm(x.get("code")): x for x in (debate or {}).get("debates", [])}
    out = []
    for code, votes in union.items():
        e, m, r = (votes.get("engine_v5") or {}, votes.get("multidim") or {}, votes.get("trend_strength") or {})
        b = b_idx.get(code, {})
        score = float(e.get("score") or 0)
        rtsi = float(r.get("rtsi") or 50)
        composite = float(m.get("composite") or 50)
        direction = r.get("direction")
        r2 = float(r.get("r2") or 0)
        risk = ((b.get("risk") or {}).get("verdict") or "unknown")
        bonus = min(12, max(0, rtsi - 55)) if direction == "up" and r2 >= 0.5 else 0
        penalty = 18 if direction == "down" else 10 if risk == "回避" else 0
        final = max(0, min(100, score * 0.45 + composite * 0.35 + rtsi * 0.20 + bonus - penalty))
        feature = next((v for k, v in history["features"].items() if k.endswith(code)), {})
        supporting = list(votes)
        contradictory = ["RTSI down"] if direction == "down" else []
        if risk == "回避": contradictory.append("red-team veto")
        evidence = [f"{source}={getattr(value, 'score', value.get('score', value.get('composite', 'covered')))}" for source, value in votes.items()]
        data_quality = "sufficient" if feature.get("ma20") is not None and feature.get("last_date") else "insufficient_history"
        out.append({
            "code": code,
            "name": next((v.get("name") for v in votes.values() if v.get("name")), ""),
            "score": round(final, 1),
            "model_probability": None,
            "probability_status": "uncalibrated",
            "forecast_horizon": "next_trading_day",
            "predicted_direction": direction or "unknown",
            "expected_return_range": None,
            "ma20": feature.get("ma20"), "ma60": feature.get("ma60"),
            "ma120": feature.get("ma120"), "ma252": feature.get("ma252"),
            "history_days": feature.get("length", 0), "history_last_date": feature.get("last_date"),
            "data_quality": data_quality,
            "supporting_engines": supporting, "contradicting_engines": contradictory,
            "action": "candidate" if len(supporting) >= 2 and not contradictory and data_quality == "sufficient" and risk != "unknown" else "watch",
            "evidence": evidence,
            "data_ts": {"engine_v5": ts_of(engine), "multidim": ts_of(multidim),
                        "trend_strength": ts_of(trend), "debate": ts_of(debate)},
            "invalidation": ["最新主快照/引擎超过 freshness 门槛", "开盘跳空超过预设波动区间", "资金/趋势/红队反转"],
        })
    out.sort(key=lambda x: -x["score"])
    return out[:20]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA, "prediction_bundle.json"))
    args = ap.parse_args()
    now = datetime.now(CST)
    latest = load("latest.json") or {}
    market_now = load("market_now.json") or {}
    global_markets = load("global_markets.json") or {}
    engine = load("engine_v5.json") or {}
    multidim = load("multidim.json") or {}
    trend = load("trend_strength.json") or {}
    debate = load("bull_bear_debate.json") or {}
    cycle = load("cycle_deep.json") or {}
    manifest = load("analysis_manifest.json") or {}

    group_status = {k: manifest_status(manifest, k) for k in ("market_snapshot", "global_markets", "hot_news", "picks", "engines", "review")}
    candidates = build_candidates(latest, engine, multidim, trend, debate, manifest)
    direction = index_direction(latest, global_markets)
    if cycle.get("status") or cycle.get("composite") is not None:
        cstatus = cycle.get("status") or {}
        cscore = cycle.get("composite", cstatus.get("composite"))
        if isinstance(cscore, (int, float)):
            direction["cycle_deep_composite"] = cscore
            direction["cycle_deep_bias"] = cstatus.get("bias") or cycle.get("bias")
            if cscore <= -2:
                direction["probability"]["down"] = round(min(0.8, direction["probability"]["down"] + 0.05), 3)
                direction["probability"]["up"] = round(max(0.1, direction["probability"]["up"] - 0.05), 3)
            elif cscore >= 2:
                direction["probability"]["up"] = round(min(0.8, direction["probability"]["up"] + 0.05), 3)
                direction["probability"]["down"] = round(max(0.1, direction["probability"]["down"] - 0.05), 3)
            direction["method"] += "; 已联动 cycle_deep 周期综合分"
    engines_fresh = group_status.get("engines") == "fresh"
    market_fresh = group_status.get("market_snapshot") == "fresh"
    can_action = engines_fresh and market_fresh
    candidate_today = [x for x in candidates if x["action"] == "candidate"][:8]
    if not can_action:
        today_status = "no_fresh_buy_signal"
        today_reason = "主快照或引擎数据未满足新鲜度门槛，只输出观察候选，不把旧数据写成今日买入信号。"
        for x in candidate_today:
            x["action"] = "watch_stale_data"
    else:
        today_status = "candidate_only"
        today_reason = "规则引擎筛出候选；不是确定性买入指令。"

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "prediction_bundle.py",
        "snapshot_id": manifest.get("ts") or ts_of(latest),
        "trading_date": latest.get("date") or now.strftime("%Y%m%d"),
        "pipeline_status": manifest.get("pipeline_status", "unknown"),
        "group_status": group_status,
        "today_action": {
            "status": today_status,
            "reason": today_reason,
            "candidates": candidate_today,
        },
        "next_day_candidates": candidates[:12],
        "market_direction": direction,
        "evidence": {
            "latest_ts": ts_of(latest),
            "market_now_ts": ts_of(market_now),
            "global_ts": ts_of(global_markets),
            "engine_ts": ts_of(engine),
            "trend_ts": ts_of(trend),
            "debate_ts": ts_of(debate),
            "cycle_deep_ts": ts_of(cycle),
            "manifest_ts": manifest.get("ts"),
        },
        "meta": {
            "today": "today_action 只有在 market_snapshot 与 engines 均 fresh 时才允许 candidate；否则强制 watch_stale_data",
            "tomorrow": "next_day_candidates 是启发式候选，不是已完成样本外校准的确定涨幅预测",
            "market": "market_direction 是指数/全球/涨跌宽度启发式概率，必须结合 change_conditions 解读",
            "not_buy_signal": "输出用于研究和排序，不构成确定性交易指令",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[prediction_bundle] status={payload['pipeline_status']} today={today_status} "
          f"next={len(candidates)} direction={direction['direction']} -> {args.out}")


if __name__ == "__main__":
    main()
