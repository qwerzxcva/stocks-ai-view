#!/usr/bin/env python3
"""build_picks_midday.py -- T 日 11:40 的午盘推股名单（闭环里的 E）。

## 在闭环里的位置

    09:30–11:30  端上：开盘推股名单每轮更新，同时把每轮结果写进
                 `开盘推股历史数据/<日期>.json`
    11:40        本脚本：读那份真实推送历史 + 消息面 → 午盘名单（12:00 后固定）

## 为什么不能再用 today_picks 派生

原来的 midday_picks.json 是 `today_picks_derived`（等于把早上那份换个名字），
和「早盘之后真实发生了什么」无关。这里有盘中推送历史时，就以**实际被推过的票**
为基础打分；没有时降级为候选源派生，并在 caveats 里写明「无盘中数据」。

## 输入

  <push-dir>/<date>.json    端上盘中推送记录（缺了就走降级）
  data/today_picks.json     统一候选源（补名字、补入场计划）
  data/news_impact.json     消息面影响（可选，缺了不硬编）

## 输出

  data/picks_midday.json

用法：
  python3 scripts/build_picks_midday.py --push-dir 开盘推股历史数据 --out-dir data
  python3 scripts/build_picks_midday.py --date 2026-10-29
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002")
LIMIT_PCT = 9.6
TOP_N = 10


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_news_db(path):
    """端上消息库 → {code: {"label": "受益"/"受损"}}。

    消息库按条存（benefit/harm 两个数组），午盘这里只需要「某只票今天有没有被消息
    站在利好/利空一边」，所以拍平成 code → label；同一只票既有受益又有受损时以「受损」
    为准——宁可让用户在标签上多看一眼风险。
    """
    raw = load_json(path)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for row in raw.get("items") or []:
        if not isinstance(row, dict):
            continue
        for code in row.get("benefit") or []:
            out.setdefault(code, {"label": "受益"})
        for code in row.get("harm") or []:
            out[code] = {"label": "受损"}
    return out


def is_main_board(code):
    return isinstance(code, str) and len(code) == 6 and code.startswith(MAIN_BOARD_PREFIXES)


def is_st(name):
    return isinstance(name, str) and "ST" in name.upper()


def plan_by_code(today):
    plans = ((today or {}).get("entry_plans") or {}).get("plans") or []
    return {p.get("code"): p for p in plans if isinstance(p, dict) and p.get("code")}


def name_of(today, code):
    """从 today_picks 的各池里找名字。"""
    src = (today or {}).get("picks") or {}
    for key in ("signals", "values", "limit_up"):
        for it in src.get(key) or []:
            if it.get("code") == code and it.get("name"):
                return it["name"]
    plan = plan_by_code(today).get(code) or {}
    return plan.get("name") or ""


def aggregate_rounds(push):
    """把一天的多轮推送聚合成 {code: {rounds, last_score, last_why, first_ts, last_ts}}。"""
    rounds = (push or {}).get("rounds") or []
    agg = {}
    for r in rounds:
        ts = r.get("ts")
        for p in r.get("picks") or []:
            code = p.get("code")
            if not code:
                continue
            e = agg.setdefault(code, {
                "rounds": 0,
                "last_score": None,
                "last_why": [],
                "first_ts": ts,
                "last_ts": ts,
            })
            e["rounds"] += 1
            if isinstance(p.get("score"), (int, float)):
                e["last_score"] = p["score"]
            if p.get("why"):
                e["last_why"] = p["why"][:3]
            e["last_ts"] = ts
    return agg


def from_push(agg, today, news, top):
    """有推送历史：按「被推次数 + 最新得分」排，次数多说明盘中反复确认过。"""
    plans = plan_by_code(today)
    out = []
    for code, e in agg.items():
        if not is_main_board(code):
            continue
        name = name_of(today, code)
        if is_st(name):
            continue
        tags = [f"盘中推送 {e['rounds']} 轮"]
        ni = news.get(code) if isinstance(news, dict) else None
        if isinstance(ni, dict) and ni.get("label"):
            tags.append(f"消息面 {ni['label']}")
        plan = plans.get(code) or {}
        out.append({
            "code": code,
            "name": name,
            "score": e["last_score"],
            "rounds": e["rounds"],
            "tags": tags,
            "why": e["last_why"],
            "first_ts": e["first_ts"],
            "last_ts": e["last_ts"],
            "plan": {
                "entry_zone": plan.get("entry_zone"),
                "stop_loss": plan.get("stop_loss"),
                "target": plan.get("target"),
            } if plan else None,
        })
    out.sort(key=lambda x: (x.get("rounds") or 0, x.get("score") or 0), reverse=True)
    return out[:top]


def from_today(today, news, top, today_limit=None):
    """无推送历史：降级为候选源派生（明确标注，不冒充盘中午盘分析）。"""
    today_limit = today_limit or set()
    src = (today or {}).get("picks") or {}
    plans = plan_by_code(today)
    out = []
    for it in (src.get("signals") or []):
        code = it.get("code")
        if not code or not is_main_board(code):
            continue
        if code in today_limit:
            continue
        plan = plans.get(code) or {}
        name = it.get("name") or plan.get("name") or ""
        if is_st(name):
            continue
        pct = it.get("pctChg")
        if isinstance(pct, (int, float)) and pct >= LIMIT_PCT:
            continue
        tags = ["无盘中推送记录，按收盘候选派生"]
        ni = news.get(code) if isinstance(news, dict) else None
        if isinstance(ni, dict) and ni.get("label"):
            tags.append(f"消息面 {ni['label']}")
        out.append({
            "code": code,
            "name": name,
            "score": it.get("score"),
            "rounds": 0,
            "tags": tags,
            "why": (it.get("why") or [])[:3],
            "first_ts": None,
            "last_ts": None,
            "plan": {
                "entry_zone": plan.get("entry_zone"),
                "stop_loss": plan.get("stop_loss"),
                "target": plan.get("target"),
            } if plan else None,
        })
    out.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    return out[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push-dir", default="开盘推股历史数据")
    ap.add_argument("--news-db-dir", default="消息数据库", help="端上消息库目录（端上每次开 App 持续写入）")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--date", default=None, help="默认今天（CST）")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    date = args.date or now.strftime("%Y-%m-%d")
    today = load_json(os.path.join(args.out_dir, "today_picks.json"))
    push_path = os.path.join(args.push_dir, f"{date}.json")
    push = load_json(push_path)
    news_raw = None
    news = load_news_db(os.path.join(args.news_db_dir, f"{date}.json"))
    if not news:
        news_raw = load_json(os.path.join(args.out_dir, "news_impact.json"))
    if isinstance(news_raw, dict):
        for row in news_raw.get("items") or news_raw.get("news") or []:
            if isinstance(row, dict) and row.get("code"):
                news[row["code"]] = row

    caveats = []

    caveats = []
    if not today:
        print("today_picks.json 缺失：午盘名单无从生成", file=sys.stderr)
        return 0

    if push:
        agg = aggregate_rounds(push)
        picks = from_push(agg, today, news, args.top)
        source = f"端上盘中推送历史（{len(agg)} 只出现过，取 {len(picks)} 只）"
        if not picks:
            caveats.append("推送历史存在但没有主板候选（可能全是 ST 或涨停）")
    else:
        # 没有推送历史时，仍要拿今日涨停名单排掉涨停股（signals 的 pctChg 常是 null，判不出来）
        lim = load_json(os.path.join(args.out_dir, "limit_history.json")) or {}
        last = (lim.get("days") or [{}])[-1]
        today_limit = set(last.get("codes") or [])
        picks = from_today(today, news, args.top, today_limit)
        source = "今日无盘中推送记录 → 降级为收盘候选派生"
        caveats.append(f"{push_path} 不存在：午盘名单没有盘中数据支撑，不是二次分析结果")
    if not news:
        caveats.append("news_impact 缺失：消息面标签不可得")

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "date": date,
        "session": "午盘",
        "kind": "midday",
        "generated_by": "build_picks_midday.py",
        "source": source,
        "note": "午盘名单 12:00 后固定；盘中继续由端上的开盘推股名单实时更新",
        "n": len(picks),
        "caveats": caveats,
        "picks": picks,
    }

    if args.dry_run:
        print(source)
        print("n=", len(picks), [p["code"] for p in picks[:5]])
        for c in caveats:
            print("  说明:", c)
        return 0

    path = os.path.join(args.out_dir, "picks_midday.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"午盘 {len(picks)} 只（{os.path.getsize(path)/1024:.1f}KB）｜{source}")
    for c in caveats:
        print("  说明:", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
