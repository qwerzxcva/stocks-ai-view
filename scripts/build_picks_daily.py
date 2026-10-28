#!/usr/bin/env python3
"""build_picks_daily.py -- T 日收盘后的两份定稿候选：A 超短线 / B 短中线。

## 在闭环里的位置

    15:30 / 16:40  本脚本：全 A 收盘分析 → A（T+1 超短线）+ B（短中线，定稿）
    次日 09:00     端上：拉这两份 + 本地实时消息/竞价 → 早盘推股名单（09:30 冻结）
    盘中           端上：全市场实时 + 早盘名单 → 开盘推股名单（可变）

## 为什么从 today_picks 派生而不另起一套

today_picks.json 已经是「统一推股」的汇总（signals / values / limit_up / avoid /
entry_plans），再写一套独立选股只会让两份名单给相反建议。这里只做三件事：
筛选（主板、非涨停）、分类（短线 / 中线）、打标签（连板、板块、外围、周期）。

## 红线（AGENTS.md）

- 只推主板：600/601/603/605/000/001/002；688/300/301 不出现
- 不含当日涨停（pctChg >= 9.6）：涨停股只观察，不进推荐
- 缺数据标 null / 写进 caveats，不拿 0 冒充

输出：
  data/picks_ultra_short.json   A：T+1 超短线
  data/picks_swing.json         B：短中线

用法：
  python3 scripts/build_picks_daily.py
  python3 scripts/build_picks_daily.py --out-dir data --top 15
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002")
LIMIT_PCT = 9.6          # >= 这个涨幅按涨停处理，不进推荐
TOP_N = 15


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def is_main_board(code):
    return isinstance(code, str) and len(code) == 6 and code.startswith(MAIN_BOARD_PREFIXES)


def board_stats(limit_book, recent_days=5):
    """从 limit_history 算出三样东西：今日涨停集合 / 当前连板数 / 近 N 日涨停次数。

    今日涨停必须单独拿出来：红线是「不含当日涨停股」（涨停价买不进，推荐等于空话），
    而「妖股」指的是**近期反复涨停、今日还没封**那类，两者不能混为一谈。
    """
    days = (limit_book or {}).get("days") or []
    if not days:
        return set(), {}, {}
    today = set(days[-1].get("codes") or [])
    recent = days[-recent_days:]
    recent_count = {}
    for d in recent:
        for c in d.get("codes") or []:
            recent_count[c] = recent_count.get(c, 0) + 1
    streak = {}
    for c in today:
        n = 1
        for i in range(len(days) - 2, -1, -1):
            if c in set(days[i].get("codes") or []):
                n += 1
            else:
                break
        streak[c] = n
    return today, streak, recent_count


def sector_of(sector_map):
    """{code: 板块名}，兼容两种常见结构。"""
    if not isinstance(sector_map, dict):
        return {}
    rows = sector_map.get("stocks") or sector_map.get("map") or sector_map
    if not isinstance(rows, dict):
        return {}
    out = {}
    for code, v in rows.items():
        if isinstance(v, str):
            out[code] = v
        elif isinstance(v, dict):
            out[code] = v.get("sector") or v.get("industry") or v.get("name")
    return {k: v for k, v in out.items() if isinstance(v, str) and v}


def plan_by_code(today):
    plans = ((today or {}).get("entry_plans") or {}).get("plans") or []
    return {p.get("code"): p for p in plans if isinstance(p, dict) and p.get("code")}


def pct_of(item):
    """当日涨幅；signals 里可能叫 pctChg/chg_pct/ret_pct。"""
    for k in ("pctChg", "chg_pct", "ret_pct", "pct"):
        v = item.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def is_st(name):
    """ST/*ST：涨跌幅 5% 限制 + 退市风险，短线逻辑与普通主板不同，不进推荐。"""
    return isinstance(name, str) and "ST" in name.upper()


def ultra_short_picks(today, stats, sectors, plans, top):
    """A：从 signals（短线信号）+ limit_up（涨停基因）里筛，打标签。"""
    today_limit, streak, recent_count = stats
    src = ((today or {}).get("picks") or {})
    pool = list(src.get("signals") or []) + list(src.get("limit_up") or [])
    out = []
    skipped = {"non_main": 0, "limit_up": 0, "st": 0, "no_code": 0}
    for it in pool:
        code = it.get("code")
        if not code:
            skipped["no_code"] += 1
            continue
        if not is_main_board(code):
            skipped["non_main"] += 1
            continue
        # signals 里的 name 经常是空串，真名字在 entry_plans：先补再判 ST
        plan = plans.get(code) or {}
        name = it.get("name") or plan.get("name") or ""
        if is_st(name):
            skipped["st"] += 1
            continue
        # 今日涨停：红线排除（不是「连板」标签能洗白的）
        if code in today_limit:
            skipped["limit_up"] += 1
            continue
        pct = pct_of(it)
        if pct is not None and pct >= LIMIT_PCT:
            skipped["limit_up"] += 1
            continue

        tags = []
        # 妖股/异动：近期反复涨停但今天没封，是「即将再启动」的候选而不是追高
        hits = recent_count.get(code, 0)
        if hits >= 2:
            tags.append(f"近 5 日 {hits} 次涨停")
        boards = streak.get(code)
        if boards:
            tags.append(f"{boards} 连板")
        sec = sectors.get(code)
        if sec:
            tags.append(sec)
        res = it.get("resonance")
        if isinstance(res, (int, float)) and res >= 0.8:
            tags.append("多引擎共振")
        for w in (it.get("why") or [])[:2]:
            if isinstance(w, str):
                tags.append(w)

        plan = plans.get(code) or {}
        out.append({
            "code": code,
            "name": name,
            "score": it.get("score"),
            "pctChg": pct,
            "engine": it.get("engine"),
            "tags": tags,
            "why": it.get("why") or [],
            "plan": {
                "entry_zone": plan.get("entry_zone"),
                "stop_loss": plan.get("stop_loss"),
                "target": plan.get("target"),
                "rr_ratio": plan.get("rr_ratio"),
            } if plan else None,
            "risk": it.get("buyability_reason"),
        })
    out.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    return out[:top], skipped


def swing_picks(today, stats, sectors, plans, top):
    """B：从 values（价值/趋势）里筛，给「距趋势」的周期位置。"""
    today_limit, streak, recent_count = stats
    src = ((today or {}).get("picks") or {})
    out = []
    skipped = {"non_main": 0, "limit_up": 0, "st": 0, "no_code": 0}
    for it in (src.get("values") or []):
        code = it.get("code")
        if not code:
            skipped["no_code"] += 1
            continue
        if not is_main_board(code):
            skipped["non_main"] += 1
            continue
        plan = plans.get(code) or {}
        name = it.get("name") or plan.get("name") or ""
        if is_st(name):
            skipped["st"] += 1
            continue
        if code in today_limit:
            skipped["limit_up"] += 1
            continue
        pct = pct_of(it)
        if pct is not None and pct >= LIMIT_PCT:
            skipped["limit_up"] += 1
            continue

        pos = it.get("pos240_pct")
        tags = []
        sec = sectors.get(code)
        if sec:
            tags.append(sec)
        boards = streak.get(code)
        if boards:
            tags.append(f"{boards} 连板")
        if isinstance(pos, (int, float)):
            # 240 日位置：低位（<30）适合左侧，中位（30-70）趋势跟随，高位（>85）追高风险
            if pos < 30:
                tags.append(f"240日低位 {pos:.0f}%")
            elif pos <= 70:
                tags.append(f"240日中位 {pos:.0f}%")
            elif pos > 85:
                tags.append(f"240日高位 {pos:.0f}%")
        vratio = it.get("vratio")
        if isinstance(vratio, (int, float)) and vratio >= 1.3:
            tags.append(f"量比 {vratio:.1f}")

        plan = plans.get(code) or {}
        out.append({
            "code": code,
            "name": name,
            "score": it.get("score"),
            "pctChg": pct,
            "pos240_pct": pos,
            "vratio": vratio,
            "engine": it.get("engine"),
            "tags": tags,
            "why": it.get("why") or [],
            "plan": {
                "entry_zone": plan.get("entry_zone"),
                "stop_loss": plan.get("stop_loss"),
                "target": plan.get("target"),
                "rr_ratio": plan.get("rr_ratio"),
            } if plan else None,
        })
    out.sort(key=lambda x: (x.get("score") or 0, x.get("pos240_pct") or 0), reverse=True)
    return out[:top], skipped


def write(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = load_json(os.path.join(args.out_dir, "today_picks.json"))
    if not today:
        print("today_picks.json 缺失或不可读：收盘候选无从派生", file=sys.stderr)
        return 0     # 不写半成品

    limits_raw = load_json(os.path.join(args.out_dir, "limit_history.json"))
    stats = board_stats(limits_raw)
    sectors = sector_of(load_json(os.path.join(args.out_dir, "stock_sector_map.json")))
    plans = plan_by_code(today)

    ts = datetime.now(CST).isoformat(timespec="seconds")
    date = today.get("date") or datetime.now(CST).strftime("%Y-%m-%d")
    overseas = today.get("overseas") or {}
    market = today.get("market") or {}

    caveats = []
    if not (stats[0] or stats[1]):
        caveats.append("limit_history 缺失：涨停排除与妖股标签不可得")
    if not sectors:
        caveats.append("stock_sector_map 缺失：板块标签不可得")
    if not plans:
        caveats.append("entry_plans 缺失：入场/止损/目标不可得")
    if not (today.get("picks") or {}).get("values"):
        caveats.append("today_picks.values 为空：短中线候选本次为空")

    a_picks, a_skip = ultra_short_picks(today, stats, sectors, plans, args.top)
    b_picks, b_skip = swing_picks(today, stats, sectors, plans, args.top)

    base = {
        "ts": ts,
        "date": date,
        "session": "盘后",
        "generated_by": "build_picks_daily.py",
        "source": "today_picks.json（统一推股，派生）",
        "market": {
            "phase": market.get("phase"),
            "phase_score": market.get("phase_score"),
            "limit_up": market.get("limit_up"),
            "limit_down": market.get("limit_down"),
            "advance": market.get("advance"),
            "decline": market.get("decline"),
        },
        "overseas": {
            "bias": overseas.get("bias"),
            "composite": overseas.get("composite"),
            "guidance": overseas.get("guidance"),
        },
        "caveats": caveats,
    }

    a_payload = dict(base)
    a_payload.update({
        "kind": "ultra_short",
        "horizon": "T+1",
        "note": "超短线：次日开盘可执行，端上 09:00 起二次分析后进早盘推股名单",
        "n": len(a_picks),
        "skipped": a_skip,
        "picks": a_picks,
    })
    b_payload = dict(base)
    b_payload.update({
        "kind": "swing",
        "horizon": "swing",
        "note": "短中线：按周期位置与趋势临近度筛，不追求次日动手",
        "n": len(b_picks),
        "skipped": b_skip,
        "picks": b_picks,
    })

    if args.dry_run:
        print("A:", len(a_picks), [p["code"] for p in a_picks[:5]])
        print("B:", len(b_picks), [p["code"] for p in b_picks[:5]])
        return 0

    sa = write(os.path.join(args.out_dir, "picks_ultra_short.json"), a_payload)
    sb = write(os.path.join(args.out_dir, "picks_swing.json"), b_payload)
    print(f"A 超短线 {len(a_picks)} 只（{sa/1024:.1f}KB）｜B 短中线 {len(b_picks)} 只（{sb/1024:.1f}KB）")
    for c in caveats:
        print("  说明:", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
