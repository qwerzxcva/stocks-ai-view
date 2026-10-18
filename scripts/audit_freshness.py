#!/usr/bin/env python3
"""
audit_freshness.py — 数据新鲜度审计: 逐个文件查「多旧了」, 不等用户反馈

背景 (2026-10-16 用户反馈):
  「有的功能没有数据, 有的数据没有更新, 从头到尾都显示这个过时的数据」
  排查发现三层问题:
    1. 采集/引擎只在盘中跑 (CST 09:02-15:57), 盘后 17 小时停摆
    2. nightly-backfill 一周只跑一次 (cron `0 18 * * 6`), 却负责
       reflect_latest/winrate_matrix/kline_sim/walkforward 等 12 个文件
    3. GitHub Actions schedule 对本仓有静默停跑 (scheduler 与 shard-collect
       实测停 10 小时无任何运行记录)
  根因是**没有自动发现机制** —— 全靠用户看到才知道。

本脚本做什么:
  扫 data/*.json, 按 ts/date 算年龄, 与每个文件的「期望更新频率」比对,
  超标的列为 STALE。产出 data/freshness_audit.json + 人读报告。

期望频率分档:
  realtime  15 分钟内    market_now / realtime_minute / hot_impact
  intraday 2 小时内      盘中类 (auction_engine / ticks / limitup_*)
  daily    26 小时内     日更类 (engine_v5 / multidim / today_picks / latest ...)
  weekly   8 天内        周更类 (reflect_latest / walkforward / kline_sim ...)
  static   无要求        映射表/清单 (industry_map_em / search_index ...)

用法:
  python3 audit_freshness.py
  python3 audit_freshness.py --fail-on-stale   # 有 STALE 则 exit 1 (CI 用)
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta, time

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

# 文件 → 期望频率档位 (未列出的按内容猜, 猜不出归 static)
TIERS = {
    # realtime (15 分钟)
    "market_now.json": "realtime", "realtime_minute.json": "realtime",
    "hot_impact.json": "realtime", "global_markets.json": "realtime",
    "moneyflow.json": "realtime",
    "market/amount_curve.json": "realtime",
    "market/speed_leaders.json": "realtime",
    "market/alert_events.json": "realtime",
    # intraday (2 小时)
    "auction_engine.json": "intraday", "ticks_latest.json": "intraday",
    "limitup_attribution.json": "intraday", "limitup_forecast.json": "intraday",
    "enrich.json": "intraday", "index_kline.json": "intraday",
    # daily (26 小时)
    "latest.json": "daily", "engine_v5.json": "daily", "multidim.json": "daily",
    "decision.json": "daily", "today_picks.json": "daily",
    "ai_pick_v4.json": "daily", "ai_picks.json": "daily",
    "premarket_picks.json": "daily", "midday_picks.json": "daily",
    "ban_prob.json": "daily", "ban_pick.json": "daily",
    "lowpool.json": "daily", "sector_pool.json": "daily",
    "cycle.json": "daily", "hot_news.json": "daily",
    "news_extra.json": "daily", "news_digest.json": "daily",
    "news_transmission.json": "daily", "daily_brief.json": "daily",
    "ai_review.json": "daily", "overseas_linkage.json": "daily",
    "engine_fusion.json": "daily", "hot_themes.json": "daily",
    "theme_lifecycle.json": "daily", "tape_structure.json": "intraday",
    "concept_strength.json": "intraday", "review_versions.json": "daily",
    "mainline.json": "daily", "cloud_analysis.json": "daily",
    "trend_strength.json": "daily", "bull_bear_debate.json": "daily",
    "factor_lab.json": "daily", "sector_engine.json": "daily",
    "market_phase.json": "daily", "scenario.json": "daily",
    "self_reflect.json": "daily", "rps_rotation.json": "daily",
    "weight_calibration.json": "daily", "all_sources.json": "daily",
    "shard_a.json": "daily", "entry_plans.json": "daily",
    "morning_review.json": "daily", "review_today.json": "daily",
    "stop_analysis.json": "daily", "overseas_live.json": "daily",
    "predict_algo.json": "daily", "risk_plan.json": "daily",
    "key_levels.json": "daily", "pick_high_track.json": "daily",
    "vango_articles.json": "daily",
    "shard_b.json": "intraday", "shard_c.json": "intraday",
    # weekly (8 天)
    "reflect_latest.json": "weekly", "winrate_matrix.json": "weekly",
    "kline_sim.json": "weekly", "walkforward.json": "weekly",
    "engine_weights.json": "weekly", "weights_history.json": "weekly",
    "edge_report.json": "weekly", "cycle_deep.json": "weekly",
    "cycle_pool_v2.json": "weekly", "weights_history.json": "weekly",
    "search_index.json": "static", "stock_names.json": "static",
}

THRESH_HOURS = {"realtime": 0.5, "intraday": 3, "daily": 30, "weekly": 8 * 24}


def ts_of(d):
    if isinstance(d, list):
        return max((t for row in d if isinstance(row, dict)
                    if (t := ts_of(row)) and parse_ts(t)), default=None)
    if not isinstance(d, dict):
        return None
    for node in (d, d.get("summary"), d.get("meta")):
        if not isinstance(node, dict):
            continue
        for key in ("ts", "timestamp", "generated_at", "generated", "rebuilt_at", "date", "data_date"):
            value = node.get(key)
            if isinstance(value, str) and parse_ts(value):
                return value
    return None


def parse_ts(v):
    """容错解析时间戳 → aware datetime (CST)。失败返回 None。"""
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(s[:len(fmt) + 6] if fmt.endswith("%z") else s[:19], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt
        except ValueError:
            continue
    # 纯日期 YYYYMMDD
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=CST)
        except ValueError:
            return None
    return None


MARKET_SESSION_FILES = {
    "market_now.json", "realtime_minute.json", "moneyflow.json",
    "global_markets.json", "hot_impact.json", "enrich.json",
    "index_kline.json", "auction_engine.json", "ticks_20261017.json",
}


def market_age_hours(since, until):
    since, until = since.astimezone(CST), until.astimezone(CST)
    if since >= until:
        return 0.0
    total = 0.0
    day = since.date()
    while day <= until.date():
        if day.weekday() < 5:
            for start, end in ((time(9, 15), time(11, 30)), (time(13), time(15, 5))):
                left = max(since, datetime.combine(day, start, CST))
                right = min(until, datetime.combine(day, end, CST))
                total += max(0.0, (right - left).total_seconds())
        day += timedelta(days=1)
    return total / 3600


def main():
    ap = argparse.ArgumentParser(description="数据新鲜度审计")
    ap.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "freshness_audit.json"))
    ap.add_argument("--fail-on-stale", action="store_true",
                    help="有 STALE 文件则 exit 1 (供 CI 门禁)")
    ap.add_argument("--report", default=os.path.join(AI_VIEW_DATA, "freshness_audit.md"))
    args = ap.parse_args()

    now = datetime.now(CST)
    rows = []
    for f in sorted(glob.glob(os.path.join(AI_VIEW_DATA, "*.json"))):
        name = os.path.basename(f)
        if name in ("freshness_audit.json",):
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            rows.append({"file": name, "status": "UNREADABLE",
                         "note": "JSON 解析失败"})
            continue
        raw_ts = ts_of(d)
        dt = parse_ts(raw_ts) if raw_ts else None
        tier = TIERS.get(name)
        size = os.path.getsize(f)

        # 无时间戳 → 按内容猜档位, 猜不出归 static
        if tier is None:
            if isinstance(d, dict) and any(k in d for k in ("map", "sources", "registry")):
                tier = "static"
            elif isinstance(d, list) and size > 500000:
                tier = "static"
            else:
                tier = "unknown"

        if tier == "static":
            rows.append({"file": name, "tier": tier, "ts": raw_ts,
                         "status": "STATIC", "note": "静态索引不以生成时间衡量"})
            continue
        if dt is None:
            rows.append({"file": name, "tier": tier, "ts": raw_ts,
                         "status": "NO_TS", "note": "无可解析生成时间, 无法判断新鲜度"})
            continue

        age_h = (now - dt).total_seconds() / 3600
        effective_age = market_age_hours(dt, now) if name in MARKET_SESSION_FILES else age_h
        if tier == "unknown":
            status = "UNKNOWN_TIER"
        else:
            thr = THRESH_HOURS[tier]
            if name in MARKET_SESSION_FILES and now.weekday() >= 5:
                status = "EXPECTED_CLOSED"
            else:
                status = "FUTURE_TS" if age_h < -0.01 else ("OK" if effective_age <= thr else "STALE")
        rows.append({"file": name, "tier": tier, "ts": raw_ts,
                     "age_hours": round(age_h, 1), "effective_age_hours": round(effective_age, 1),
                     "status": status, "threshold_h": THRESH_HOURS.get(tier)})

    stale = [r for r in rows if r["status"] == "STALE"]
    unreadable = [r for r in rows if r["status"] == "UNREADABLE"]
    no_ts = [r for r in rows if r["status"] == "NO_TS"]
    unknown = [r for r in rows if r["status"] == "UNKNOWN_TIER"]

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "audit_freshness.py",
        "n_files": len(rows),
        "n_stale": len(stale),
        "n_unreadable": len(unreadable),
        "n_no_ts": len(no_ts),
        "n_unknown_tier": len(unknown),
        "by_tier": {},
        "files": rows,
        "meta": {
            "tiers": {"realtime": "15分钟内", "intraday": "2小时内", "daily": "26小时内",
                      "weekly": "8天内", "static": "无要求 (映射表/清单)"},
            "thresholds_h": THRESH_HOURS,
            "why": "2026-10-16 用户反馈数据过时, 排查发现采集只在盘中跑 + "
                   "nightly-backfill 一周一次 + GH Actions schedule 静默停跑。"
                   "本脚本让陈旧可被自动发现, 不依赖用户反馈。",
            "caveat": "非交易时段 (周末/凌晨) 采集类文件必然变旧, 这是预期; "
                      "判 STALE 只说明「超过该档位的阈值」, 需结合是否交易时段看。"
                      "盘后/周末看 engine_v5 等日更文件为昨日数据是正常的, "
                      "真正的 STALE 是「交易日盘中仍不更新」。",
        },
    }
    for r in rows:
        payload["by_tier"].setdefault(r.get("tier", "?"), 0)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    lines = [
        "# 数据新鲜度审计", "",
        f"- 时间: {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"- 扫描: {len(rows)} 个文件 | STALE {len(stale)} | 无时间戳 {len(no_ts)} "
        f"| 解析失败 {len(unreadable)}", "",
        "| 文件 | 档位 | 时间戳 | 年龄(h) | 阈值(h) | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    order = {"STALE": 0, "UNREADABLE": 1, "NO_TS": 2, "FUTURE_TS": 2,
             "UNKNOWN_TIER": 3, "EXPECTED_CLOSED": 4, "STATIC": 5, "OK": 5}
    for r in sorted(rows, key=lambda x: (order.get(x["status"], 9),
                                         -(x.get("age_hours") or 0))):
        lines.append(
            f"| {r['file']} | {r.get('tier', '-')} | {str(r.get('ts'))[:19]} | "
            f"{r.get('age_hours', '-')} | {r.get('threshold_h', '-')} | "
            f"**{r['status']}** |")
    lines += ["", "## 说明", "",
              "- STALE = 超过该档位阈值。非交易时段采集类变旧是预期, 需结合",
              "  是否交易时段判断; 真正的故障是「交易日盘中仍不更新」。",
              "- EXPECTED_CLOSED = 周末或非交易时段，当前文件不应被当作实时故障。",
              "- 本报告由 `scripts/audit_freshness.py` 生成, 可加 `--fail-on-stale`",
              "  作为 CI 门禁。"]
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[freshness] {len(rows)} 文件 | STALE={len(stale)} NO_TS={len(no_ts)} "
          f"UNREADABLE={len(unreadable)}")
    for r in sorted(stale, key=lambda x: -(x.get("age_hours") or 0))[:15]:
        print(f"  STALE {r['file']:<28} {r['age_hours']:>7.1f}h "
              f"(阈值 {r['threshold_h']}h, {r['tier']})")
    print(f"  → {args.out}")
    print(f"  → {args.report}")

    if args.fail_on_stale and (stale or unreadable):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
