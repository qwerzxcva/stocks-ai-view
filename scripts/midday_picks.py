#!/usr/bin/env python3
"""
midday_picks.py — 午盘推股引擎 (11:30 收盘后 / 13:00 开盘前产出)

用户核心诉求: 「午盘开盘前推股给我, 看收盘能赚多少」。
上午盘已走完, 本引擎用上午的真实量价数据修正早盘计划:

与 premarket_picks 的分工:
  premarket_picks (09:15 前) — 基于隔夜数据 + 昨收, 给 A/B/C 三级清单
  本引擎 midday_picks (11:30 后) — 基于上午实盘, 做三件事:
    1. 早盘计划验证: 早盘推荐的票上午实际表现 (打靶/止损/震荡)
    2. 上午盘面复盘: 涨跌家数/炸板率/主线是否切换/量能对比
    3. 下午修正清单: 保留/剔除/新增, 带下午入场区间

关键逻辑 (来自 reflect_engine 的历史结论):
  - 上午冲高但资金背离 → 下午大概率回落, 剔除
  - 上午缩量震荡但资金流入 → 下午可能启动, 提升
  - 早盘已涨停 → 不可买入, 移入「明日观察」
  - 早盘推荐的票上午已跌破止损 → 确认失败, 记录教训

输出 data/midday_picks.json
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


def load(name, default=None):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def morning_review():
    """上午盘面复盘。"""
    out = {}
    mn = load("market_now.json") or {}
    lt = load("latest.json") or {}
    m = mn.get("market") or lt.get("market") or {}
    out["market"] = {
        "advance": m.get("advance"), "decline": m.get("decline"),
        "limit_up": m.get("limit_up"), "limit_down": m.get("limit_down"),
        "broken": m.get("broken"), "seal_rate": m.get("seal_rate"),
        "turnover_yi": (m.get("turnover") or {}).get("total_yi"),
        "ts": mn.get("ts") or lt.get("ts"),
    }
    # 连板梯队 (上午高度)
    lad = mn.get("ladder") or lt.get("ladder") or {}
    tiers = lad.get("tiers") if isinstance(lad, dict) else None
    if tiers:
        out["ladder"] = [{"boards": t.get("boards"), "count": t.get("count"),
                          "codes": (t.get("codes") or [])[:5]} for t in tiers[:5]]
    # 主线
    ml = lt.get("mainlines") or []
    out["mainlines"] = [{"name": x.get("name"), "score": x.get("score")}
                        for x in ml[:5]] if ml else []
    return out


def verify_premarket():
    """验证早盘计划: 早盘推荐的票上午表现如何。"""
    pm = load("premarket_picks.json")
    if not pm:
        return None
    mn = load("market_now.json") or {}
    lt = load("latest.json") or {}
    # 现价表: limit_up_pool + ths_strong + picks
    quotes = {}
    for src in (mn, lt):
        for r in (src.get("limit_up_pool") or []):
            if r.get("code"):
                quotes[str(r["code"])] = r
        for r in (src.get("picks") or {}).get("signals") or [] if isinstance(src.get("picks"), dict) else []:
            if r.get("code"):
                quotes.setdefault(str(r["code"]), r)

    results = []
    for tier in ("A_buy", "B_watch"):
        for r in (pm.get("tiers") or {}).get(tier) or []:
            code = str(r.get("code") or "")
            q = quotes.get(code)
            entry = (r.get("entry_zone") or [None, None])[0]
            target = r.get("target")
            stop = r.get("stop")
            cur = q.get("price") if q else None
            chg = q.get("chg_pct") if q else None
            verdict = "no_data"
            if cur and entry:
                if chg is not None and chg >= 9.8:
                    verdict = "limit_up_unbuyable"
                elif target and cur >= target:
                    verdict = "target_hit"
                elif stop and cur <= stop:
                    verdict = "stopped_out"
                elif cur > entry * 1.02:
                    verdict = "in_profit"
                elif cur < entry * 0.98:
                    verdict = "underwater"
                else:
                    verdict = "flat"
            results.append({
                "code": code, "name": r.get("name"), "tier": tier,
                "score": r.get("score"),
                "entry_zone": r.get("entry_zone"), "stop": stop, "target": target,
                "am_price": cur, "am_chg_pct": chg, "verdict": verdict,
                "limit_up": bool(q and q.get("lianban")),
            })
    return {
        "n_checked": len(results),
        "verdicts": {v: sum(1 for r in results if r["verdict"] == v)
                     for v in {r["verdict"] for r in results}},
        "detail": results,
        "gate_was": (pm.get("gate") or {}).get("state"),
        "global_score_was": (pm.get("global") or {}).get("score"),
    }


def afternoon_list():
    """下午修正清单。"""
    pm = load("premarket_picks.json") or {}
    mn = load("market_now.json") or {}
    v = verify_premarket()
    vd = {r["code"]: r for r in (v or {}).get("detail") or []}

    keep, drop, watch = [], [], []
    for tier in ("A_buy", "B_watch"):
        for r in (pm.get("tiers") or {}).get(tier) or []:
            code = str(r.get("code") or "")
            rec = dict(r)
            am = vd.get(code)
            if am:
                rec["am_verdict"] = am["verdict"]
                rec["am_price"] = am["am_price"]
                # 上午已涨停 → 下午买不到, 移入明日观察
                if am["verdict"] == "limit_up_unbuyable":
                    watch.append({**rec, "why_moved": "上午已涨停, 下午无法买入, 列入明日观察"})
                    continue
                # 上午已止损 → 逻辑被证伪, 剔除
                if am["verdict"] == "stopped_out":
                    drop.append({**rec, "why_moved": "上午已跌破止损, 入场逻辑被证伪"})
                    continue
                # 上午冲高 + 无资金数据 → 降级观察 (不追高)
                if am["verdict"] == "in_profit" and (am.get("am_chg_pct") or 0) > 6:
                    watch.append({**rec, "why_moved": "上午已冲高 >6%%, 不追高, 等回踩"})
                    continue
            keep.append(rec)

    # 上午新出现的强势股 (涨停池里非早盘推荐的)
    pm_codes = {str(r.get("code")) for t in ("A_buy", "B_watch", "C_avoid")
                for r in (pm.get("tiers") or {}).get(t) or []}
    new_strong = []
    for r in (mn.get("limit_up_pool") or [])[:15]:
        c = str(r.get("code") or "")
        if c and c not in pm_codes and (r.get("lianban") or 0) >= 2:
            new_strong.append({
                "code": c, "name": r.get("name"), "price": r.get("price"),
                "chg_pct": r.get("chg_pct"), "lianban": r.get("lianban"),
                "industry": r.get("industry"),
                "note": "上午新晋连板, 早盘未覆盖 — 下午接力需看炸板率",
            })

    return {
        "keep": keep[:12],
        "drop": drop[:8],
        "watch": watch[:8],
        "new_strong": new_strong[:8],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "midday_picks.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    pm = load("premarket_picks.json")
    v = verify_premarket()
    lst = afternoon_list()
    mr = morning_review()

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "midday_picks v1 (上午实盘验证 + 下午修正)",
        "date": now.strftime("%Y-%m-%d"),
        "session": "midday",
        "morning_review": mr,
        "premarket_verification": v,
        "afternoon": lst,
        "gate": (pm or {}).get("gate"),
        "meta": {
            "flow": "09:15 premarket_picks → 11:30 本引擎验证 → 13:00 下午执行 keep 清单",
            "verdict_rules": "limit_up_unbuyable→明日观察 / stopped_out→剔除 / "
                             "in_profit>6%→不追高 / 其余保留",
            "caveat": "Research reference only, not investment advice.",
        },
    }

    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("[midday] 上午盘面: 涨%s/跌%s 涨停%s 炸板%s" % (
        mr["market"].get("advance"), mr["market"].get("decline"),
        mr["market"].get("limit_up"), mr["market"].get("broken")))
    if v:
        print("[midday] 早盘计划验证 (%d 只): %s" % (
            v["n_checked"], json.dumps(v["verdicts"], ensure_ascii=False)))
    print("[midday] 下午清单: 保留%d / 剔除%d / 观察%d / 新晋连板%d" % (
        len(lst["keep"]), len(lst["drop"]), len(lst["watch"]), len(lst["new_strong"])))
    print("[midday] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
