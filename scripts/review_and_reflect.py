#!/usr/bin/env python3
"""review_and_reflect.py — 复盘分析 + 自我反思 (2026-10-19)

用户硬要求:
  模块4 复盘: 每次早盘/午盘/明日候选结束时, 分析三批股票的胜率和模拟盈亏
              (每天可投 10 万人民币), 调用机学算法分析
  模块5 反思: 复盘找问题, 反思总结经验、改进方法、避免重蹈覆辙

设计:
  复盘(review) = 用事后价格算「当时推的票赚没赚」, 对象是既成事实
  反思(reflect) = 从复盘结论里提炼可执行的规则修正, 对象是下次决策
  两者分开输出, 不混在一个 verdict 里。

模拟交易规则 (每日 10 万):
  - 资金等分给当日该 session 的所有推荐 (单票 = 100000/N)
  - 买入价 = 推股账本记录的参考价对应交易日收盘
  - 卖出价 = T+N 收盘 (N 由 --hold-days 指定, 默认 1)
  - 停牌/无数据: 记为不可结算, 不计入胜率, 但单独列出

胜率口径 (双口径, 避免单指标误导):
  win_rate_close  = 期末收益 > 0 的比例
  win_rate_recoup = 期间曾回本 (最高价 >= 买入价) 的比例  ← 与 v7 训练口径一致

产出:
  data/review_latest.json   每次复盘结果
用法:
  python3 review_and_reflect.py --hold-days 1
  python3 review_and_reflect.py --session premarket --date 2026-10-19
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = os.path.join(os.path.dirname(HERE), "..", "stocks-data-kline",
                        "data", "bulk")

CAPITAL = 100000.0
_KLINE = None


def load_klines():
    """{code: {date: (close, high)}} 一次读完 bulk。"""
    global _KLINE
    if _KLINE is not None:
        return _KLINE
    import glob
    idx = {}
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                v = line.strip().split("|")
                if len(v) < 5:
                    continue
                try:
                    idx.setdefault(v[0].strip(), {})[v[1][:10]] = (
                        float(v[3]), float(v[4]))
                except ValueError:
                    continue
    _KLINE = idx
    return idx


def _pref(code):
    c = str(code)
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if c[0] in "69":
        return "sh" + c
    if c[0] in "48":
        return "bj" + c
    return "sz" + c


def settle(code, base_date, hold_days):
    """结算单票: 买入=base_date 收盘, 卖出=其后第 hold_days 个交易日收盘。"""
    kl = load_klines().get(_pref(code)) or {}
    ds = sorted(kl)
    if not ds:
        return None
    base_idx = None
    for i, d in enumerate(ds):
        if d >= base_date:
            base_idx = i
            break
    if base_idx is None or base_idx + hold_days >= len(ds):
        return None
    buy_date = ds[base_idx]
    sell_date = ds[base_idx + hold_days]
    buy = kl[buy_date][0]
    sell = kl[sell_date][0]
    if buy <= 0:
        return None
    peak = max(kl[ds[j]][1] for j in range(base_idx, base_idx + hold_days + 1))
    return {
        "code": code, "buy_date": buy_date, "sell_date": sell_date,
        "buy": round(buy, 3), "sell": round(sell, 3),
        "ret_pct": round((sell / buy - 1) * 100, 2),
        "recoup": peak >= buy,
        "peak_pct": round((peak / buy - 1) * 100, 2),
    }


def review_session(picks, base_date, hold_days):
    """一个 session 的复盘: 等分资金, 算胜率与盈亏。"""
    if not picks:
        return None
    n = len(picks)
    per = CAPITAL / n
    settled, unsettled = [], []
    for p in picks:
        code = p.get("code")
        r = settle(code, base_date, hold_days)
        if not r:
            unsettled.append(code)
            continue
        r["name"] = p.get("name") or ""
        r["score"] = p.get("score")
        r["grade"] = p.get("grade")
        r["pnl"] = round(per * r["ret_pct"] / 100, 2)
        settled.append(r)
    if not settled:
        return {"n_picks": n, "n_settled": 0, "unsettled": unsettled,
                "note": "全部无法结算 (数据不足/停牌)"}
    wins = [x for x in settled if x["ret_pct"] > 0]
    recoups = [x for x in settled if x["recoup"]]
    total_pnl = sum(x["pnl"] for x in settled)
    return {
        "n_picks": n,
        "n_settled": len(settled),
        "unsettled": unsettled,
        "capital": CAPITAL,
        "per_stock": round(per, 2),
        "hold_days": hold_days,
        "win_rate_close": round(len(wins) / len(settled) * 100, 2),
        "win_rate_recoup": round(len(recoups) / len(settled) * 100, 2),
        "avg_ret_pct": round(sum(x["ret_pct"] for x in settled) / len(settled), 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / CAPITAL * 100, 2),
        "details": settled,
    }


def reflect(reviews):
    """从复盘结果提炼可执行改进 (模块5)。"""
    out = {"findings": [], "improvements": []}
    for sess, rv in reviews.items():
        if not rv or not rv.get("n_settled"):
            continue
        wr = rv["win_rate_close"]
        pnl = rv["total_pnl_pct"]
        if wr < 40:
            out["findings"].append(
                f"{sess} 期末胜率 {wr}% 低于 40%: 推荐方向系统性偏差")
            out["improvements"].append(
                f"{sess} 需收紧入场条件, 当前仅 {wr}% 收盘为正")
        elif wr >= 55:
            out["findings"].append(f"{sess} 期末胜率 {wr}% 达标 (>=55%)")
        if pnl < -2:
            out["findings"].append(
                f"{sess} 模拟亏损 {pnl}% (10万本金亏 {abs(rv['total_pnl'])} 元)")
            out["improvements"].append(
                f"{sess} 单批亏损过大, 应降低单票仓位或加止损")
        if rv["win_rate_recoup"] - wr > 15:
            out["findings"].append(
                f"{sess} 回本率 {rv['win_rate_recoup']}% 远高于期末 {wr}%: "
                f"选股方向可, 持有期偏短")
            out["improvements"].append(
                f"{sess} 考虑延长持有期或加入止盈规则")
    if not out["findings"]:
        out["findings"].append("本期无显著异常")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold-days", type=int, default=1)
    ap.add_argument("--session", default=None)
    ap.add_argument("--date", default=None, help="基准日 YYYY-MM-DD")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "review_latest.json"))
    args = ap.parse_args()
    now = datetime.now(CST)

    led_path = os.path.join(DATA_DIR, "picks_ledger.jsonl")
    if not os.path.exists(led_path):
        print("[review] 无 picks_ledger.jsonl, 先跑 picks_ledger add")
        return 1
    records = []
    with open(led_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    sessions = {}
    for r in records:
        s = r.get("session")
        if not s:
            continue
        if args.session and s != args.session:
            continue
        sessions.setdefault(s, {})
        if r.get("kind") == "add":
            sessions[s][r["code"]] = r

    reviews = {}
    for s, picks in sessions.items():
        plist = list(picks.values())
        if not plist:
            continue
        base = args.date or str(plist[0].get("ts") or "")[:10] \
            or now.strftime("%Y-%m-%d")
        rv = review_session(plist, base, args.hold_days)
        if rv:
            rv["base_date"] = base
            reviews[s] = rv
            print(f"[review] {s}: {rv.get('n_settled',0)}/{rv.get('n_picks',0)} 结算 "
                  f"期末胜率 {rv.get('win_rate_close','-')}% "
                  f"回本率 {rv.get('win_rate_recoup','-')}% "
                  f"盈亏 {rv.get('total_pnl','-')} 元")

    refl = reflect(reviews)
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "review_and_reflect v1",
        "hold_days": args.hold_days,
        "capital_per_day": CAPITAL,
        "reviews": reviews,
        "reflection": refl,
        "note": ("模拟盈亏按每日 10 万等分计算, 不含手续费滑点; "
                 "win_rate_close=期末为正, win_rate_recoup=期间曾回本"),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[review] -> {args.out}")
    for x in refl["findings"][:3]:
        print(f"[reflect] {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
