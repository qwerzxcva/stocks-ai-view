#!/usr/bin/env python3
"""audit_data.py — 数据文件健全性审计 (空/旧/缺字段/损坏 逐一标出)"""
import json, os, glob, sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
now = datetime.now(CST)

# 前端 render 函数 → 期望的数据字段 (用于交叉核对)
EXPECT = {
    "latest.json": ["market", "limit_up_pool", "limit_down_pool", "fund_rank",
                    "news_recent", "ths_strong", "lhb", "blocktrade",
                    "cycle_v2", "commentary", "index_kline", "pools",
                    "ai_picks", "kline_sim", "global_kline", "ths_reason",
                    "tdx_qiangchou", "pick_stats"],
    "market_now.json": ["market", "limit_up_pool", "limit_down_pool",
                        "tx_sector_ranking", "ths_strong", "fund_rank", "margin"],
    "enrich.json": ["ths_strong", "lhb", "blocktrade"],
    "hot_impact.json": ["reports"],
    "ban_pick.json": ["picks"],
    "ban_prob.json": ["base", "sample"],
    "flow_divergence.json": ["alerts"],
    "cycle_deep.json": ["signal", "dims"],
    "cycle.json": ["macro_signals"],
    "sector_pool.json": ["tier1"],
    "lowpool.json": ["strong", "weak"],
    "multi_v4.json": ["buy", "watch"],
    "engine_v5.json": ["top"],
    "reflect_latest.json": ["analysis"],
    "winrate_matrix.json": ["horizons"],
    "kline_sim.json": ["by_horizon", "top_signals"],
    "daily_brief.json": ["text"],
    "cloud_analysis.json": ["summary", "tomorrow"],
    "search_index.json": None,
}

def age_str(ts):
    if not ts:
        return "-"
    try:
        s = str(ts)
        if "T" in s:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            t = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=CST)
        h = (now - t).total_seconds() / 3600
        if h < 0:
            return "FUTURE"
        if h < 2:
            return f"{h:.1f}h"
        if h < 48:
            return f"{h:.0f}h"
        return f"{h/24:.0f}d"
    except Exception:
        return "?"

print(f"审计时间: {now:%Y-%m-%d %H:%M} CST\n")
issues = []
for f in sorted(glob.glob("data/*.json")):
    name = os.path.basename(f)
    size = os.path.getsize(f)
    if size < 10:
        issues.append((name, "EMPTY FILE", ""))
        print(f"[空文件] {name} ({size}B)")
        continue
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception as e:
        issues.append((name, "CORRUPT", str(e)))
        print(f"[损坏] {name}: {e}")
        continue
    if not isinstance(d, dict):
        print(f"[非常规] {name}: 非dict ({type(d).__name__})")
        continue
    ts = d.get("ts") or d.get("date") or ""
    age = age_str(ts)
    flag = "  "
    # 检查期望字段
    exp = EXPECT.get(name)
    missing = []
    empty_l = []
    if exp:
        for k in exp:
            if k not in d:
                missing.append(k)
            elif isinstance(d[k], list) and len(d[k]) == 0:
                empty_l.append(k)
            elif isinstance(d[k], dict) and len(d[k]) == 0:
                empty_l.append(k)
    old = ""
    if "FUTURE" not in age and age not in ("-", "?"):
        # 交易时段外数据超 24h 视为过时
        try:
            hrs = (now - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(CST)).total_seconds()/3600
            if hrs > 24:
                old = f" 过时({age})"
                flag = "!"
        except Exception:
            pass
    mark = f"{name:26s} ts={str(ts)[:19]:19s} age={age:>6s}{old}"
    if missing or empty_l:
        mark += "  " + (" ".join(f"[缺:{','.join(missing)}]" for m in (['missing'] if missing else [])) + " " + " ".join(f"[空:{','.join(empty_l)}]" for e in (['empty'] if empty_l else [])))
    if missing or empty_l or old:
        flag = "!"
    print(f"{flag} {mark}")
    if missing:
        issues.append((name, "MISSING", ",".join(missing)))
    if empty_l:
        issues.append((name, "EMPTY", ",".join(empty_l)))
    if old:
        issues.append((name, "STALE", f"age={age}"))

print(f"\n=== 问题清单 ({len(issues)}) ===")
for name, kind, detail in issues:
    print(f"  [{kind}] {name}: {detail}")
