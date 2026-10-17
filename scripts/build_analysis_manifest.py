#!/usr/bin/env python3
"""
build_analysis_manifest.py — 统一分析发布清单

每轮数据/引擎发布后生成唯一的 analysis_manifest.json，记录：
- 交易日与当前会话
- 每个页面数据文件的时间戳、年龄、状态
- 对应算法/引擎是否成功
- stale 文件列表

它是前端唯一的“这页数据到底新不新”的事实来源，避免 latest.json 已更新但
premarket/daily-brief/复盘/全球市场仍悄悄停在旧版本。
"""
import json
import os
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

GROUPS = {
    "market_snapshot": ["latest.json", "market_now.json"],
    "global_markets": ["global_markets.json", "global_kline.json", "overseas_linkage.json", "overseas_impact.json"],
    "hot_news": ["hot_news.json", "news_extra.json", "hot_themes.json", "mainline.json", "recent_mainlines.json", "hot_impact.json"],
    "picks": ["premarket_picks.json", "midday_picks.json", "ai_pick_v4.json", "today_picks.json", "prediction_bundle.json"],
    "engines": ["engine_v5.json", "multidim.json", "decision.json", "trend_strength.json", "auction_engine.json", "bull_bear_debate.json", "pool_audit.json", "horizon_features.json"],
    "review": ["daily_brief.json", "ai_review.json", "reflect_latest.json", "winrate_matrix.json", "kline_sim.json", "walkforward.json"],
}

EXPECTED_HOURS = {
    "market_snapshot": 2,
    "global_markets": 2,
    "hot_news": 4,
    "picks": 8,
    "engines": 8,
    "review": 30,
}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return None


def get_ts(data):
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for key in ("ts", "timestamp", "generated", "generated_at", "date", "data_date"):
        value = data.get(key)
        if isinstance(value, str) and len(value) >= 8:
            return value
    for key in ("ts", "rebuilt_at"):
        value = meta.get(key)
        if isinstance(value, str) and len(value) >= 8:
            return value
    return None


def parse_ts(value):
    if not value:
        return None
    s = str(value)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            text = s if "%z" in fmt else s[:19]
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def main():
    now = datetime.now(CST)
    stale = []
    missing = []
    no_timestamp = []
    groups = {}

    for group, names in GROUPS.items():
        group_items = {}
        threshold = EXPECTED_HOURS[group]
        for name in names:
            path = os.path.join(DATA, name)
            data = load(path) if os.path.exists(path) else None
            raw_ts = get_ts(data)
            dt = parse_ts(raw_ts)
            if not os.path.exists(path):
                status = "missing"
                missing.append(name)
                age = None
            elif not raw_ts or not dt:
                status = "no_timestamp"
                no_timestamp.append(name)
                age = None
            else:
                age = round((now - dt).total_seconds() / 3600, 2)
                status = "fresh" if 0 <= age <= threshold else "stale"
                if status == "stale":
                    stale.append(name)
            group_items[name] = {
                "ts": raw_ts,
                "age_hours": age,
                "threshold_hours": threshold,
                "status": status,
                "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            }
        group_status = "fresh"
        if any(v["status"] == "missing" for v in group_items.values()):
            group_status = "missing"
        elif any(v["status"] in ("stale", "no_timestamp") for v in group_items.values()):
            group_status = "stale"
        groups[group] = {"status": group_status, "files": group_items}

    latest = load(os.path.join(DATA, "latest.json")) or {}
    manifest = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "build_analysis_manifest.py",
        "trading_date": latest.get("date") or now.strftime("%Y%m%d"),
        "session_state": latest.get("session_state") or "unknown",
        "pipeline_status": "fresh" if not stale and not missing and not no_timestamp else "partial",
        "stale_count": len(stale),
        "missing_count": len(missing),
        "no_timestamp_count": len(no_timestamp),
        "stale_files": stale,
        "missing_files": missing,
        "no_timestamp_files": no_timestamp,
        "groups": groups,
        "meta": {
            "purpose": "统一页面数据与算法引擎版本状态, 防止局部更新造成旧数据静默展示",
            "thresholds_hours": EXPECTED_HOURS,
            "caveat": "非交易时段的市场快照可合法归属上一交易日; stale 表示超过该组期望刷新频率",
        },
    }
    out = os.path.join(DATA, "analysis_manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[manifest] {manifest['pipeline_status']} stale={len(stale)} missing={len(missing)} -> {out}")


if __name__ == "__main__":
    main()
