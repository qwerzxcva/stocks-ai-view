#!/usr/bin/env python3
"""
pead_engine.py — A 股 PEAD（财报后漂移）事件研究

移植自 GitCode stock_analysis/pead/（原版走 SEC EDGAR），数据源替换为
本项目已有的 A 股财报快照 data/fin/*.json（含 announce_date + eps），
价格取自 bulk 前复权日线。

事件定义:
  事件日 = announce_date 之后第一个交易日（收盘进场）
  SUE    = (EPS_t - EPS_{t-4}) / std(过去 window 季的同比差分)
  CAR_h  = 事件日起 h 个交易日的累计收益（个股口径，未剔市场）

数据边界（务必如实对待）:
  data/fin/ 目前只覆盖少量股票（约 20 只），事件数远低于全市场。
  输出 JSON 的 coverage 字段会写明实际覆盖，不做任何推算填充。

用法:
  python3 scripts/pead_engine.py
  python3 scripts/pead_engine.py --fin data/fin --bulk data/bulk --out data/pead_events.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")

HORIZONS = (5, 10, 20)
MIN_QUARTERS = 8


def load_fin(fin_dir: str) -> dict:
    """{code: [(announce_date, eps), ...]}，按公告日升序，只保留 eps 非空的季度。"""
    per_code: dict = {}
    if not os.path.isdir(fin_dir):
        return per_code
    for f in sorted(os.listdir(fin_dir)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(fin_dir, f), encoding="utf-8") as fh:
                d = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        code = d.get("code")
        if not code:
            continue
        rows = []
        for r in d.get("reports", []):
            ann, eps = r.get("announce_date"), r.get("eps")
            if ann and eps is not None:
                rows.append((ann, float(eps)))
        rows.sort(key=lambda x: x[0])
        per_code[code] = rows
    return per_code


def load_close_series(bulk_dir: str) -> dict:
    """{code: [(date_str, close), ...]} 按日期升序。"""
    series: dict = {}
    for p in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) < 7:
                    continue
                code, date, _o, c = parts[0], parts[1], parts[2], parts[3]
                try:
                    series.setdefault(code, []).append((date, float(c)))
                except ValueError:
                    continue
    for c in series:
        series[c].sort(key=lambda x: x[0])
    return series


def calc_sue(eps_values: list[float], window: int = 8) -> float | None:
    """SUE = (EPS_t - EPS_{t-4}) / std(过去 window 季同比差分)。"""
    if len(eps_values) < MIN_QUARTERS:
        return None
    diffs = [eps_values[i] - eps_values[i - 4] for i in range(4, len(eps_values))]
    if len(diffs) < 2:
        return None
    recent = diffs[-window:]
    sd = statistics.stdev(recent) if len(recent) > 1 else 0.0
    if sd == 0:
        return None
    return diffs[-1] / sd


def car_at(series: list[tuple[str, float]], event_date: str, horizon: int) -> float | None:
    """事件日之后 horizon 个交易日的累计收益。"""
    idx = None
    for i, (d, _) in enumerate(series):
        if d >= event_date:
            idx = i
            break
    if idx is None or idx + horizon >= len(series):
        return None
    p0 = series[idx][1]
    p1 = series[idx + horizon][1]
    if p0 <= 0:
        return None
    return p1 / p0 - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fin", default=os.path.join(DATA, "fin"))
    ap.add_argument("--bulk", default=os.path.join(DATA, "bulk"))
    ap.add_argument("--out", default=os.path.join(DATA, "pead_events.json"))
    ap.add_argument("--max-age-days", type=int, default=90,
                    help="只输出最近 N 天公告的事件（更早的公告漂移窗口已走完）")
    ap.add_argument("--max-events", type=int, default=4000,
                    help="输出事件条数上限（按公告日倒序取最新）")
    args = ap.parse_args()

    fin = load_fin(args.fin)
    if not fin:
        print(f"无财报数据: {args.fin}", file=sys.stderr)
        return 1
    print(f"财报覆盖股票 {len(fin)} 只")

    closes = load_close_series(args.bulk)
    print(f"日线覆盖股票 {len(closes)} 只")

    events = []
    matched = 0
    for code, series in fin.items():
        px = closes.get(code)
        if not px or len(series) < MIN_QUARTERS:
            continue
        matched += 1
        sue = calc_sue([e for _, e in series])
        if sue is None:
            continue
        ann = series[-1][0]
        ev = {
            "symbol": code,
            "announce_date": ann,
            "eps": series[-1][1],
            "sue": round(sue, 4),
            "n_quarters": len(series),
        }
        ok = False
        for h in HORIZONS:
            c = car_at(px, ann, h)
            ev[f"car_{h}d"] = round(c, 6) if c is not None else None
            ok = ok or (c is not None)
        if ok:
            events.append(ev)

    # 按 SUE 十分位分层（仅在有足够样本时才有意义）
    scored = [e for e in events if e.get("car_10d") is not None]
    deciles = []
    if len(scored) >= 20:
        ranked = sorted(scored, key=lambda e: -e["sue"])
        per = max(1, len(ranked) // 10)
        for i in range(10):
            chunk = ranked[i * per:(i + 1) * per] if i < 9 else ranked[9 * per:]
            if not chunk:
                continue
            deciles.append({
                "decile": i + 1,
                "n": len(chunk),
                "avg_sue": round(statistics.mean([e["sue"] for e in chunk]), 4),
                "win_rate_10d": round(
                    sum(1 for e in chunk if e["car_10d"] > 0) / len(chunk) * 100, 2),
                "avg_car_10d": round(statistics.mean([e["car_10d"] for e in chunk]), 4),
            })

    warnings = []
    if matched < 100:
        warnings.append(
            f"财报数据仅覆盖 {matched} 只股票（data/fin/），远低于全市场约 5000 只；"
            "PEAD 结论不具备统计代表性，仅作管线验证。")
    if len(scored) < 20:
        warnings.append(f"有效事件仅 {len(scored)} 个，无法做十分位分层。")

    # 事件按公告日倒序 + 只留最近 N 天。旧公告的 CAR 5/10/20 日窗口早已走完，
    # 拿它当当前信号就是拿旧充新。原实现 `events[:200]` 是**按股票代码顺序**截断的，
    # 实测 1409 只 10 月披露的票只进来 1 条（前 200 只恰好全是 8 月披露的），
    # 端上拿到的就全是两个月前的旧信号。
    events.sort(key=lambda e: e["announce_date"], reverse=True)
    cutoff = (datetime.now(CST) - timedelta(days=args.max_age_days)).strftime("%Y-%m-%d")
    recent = [e for e in events if e["announce_date"][:10] >= cutoff]
    out_events = (recent or events)[: args.max_events]
    if len(recent) < len(events):
        warnings.append(
            f"events 只输出最近 {args.max_age_days} 天公告的 {len(recent)} 条（全量 {len(events)} 条）；"
            f"更早的公告漂移窗口已走完，保留在 coverage/deciles 统计里但不逐条输出"
        )

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "pead_engine",
        "coverage": {
            "n_fin_symbols": len(fin),
            "n_price_symbols": len(closes),
            "n_matched": matched,
            "n_events": len(events),
            "n_with_returns": len(scored),
        },
        "summary": {
            "avg_sue": round(statistics.mean([e["sue"] for e in events]), 4) if events else None,
            "avg_car_10d": round(statistics.mean([e["car_10d"] for e in scored]), 4) if scored else None,
            "win_rate_10d": round(
                sum(1 for e in scored if e["car_10d"] > 0) / len(scored) * 100, 2) if scored else None,
        },
        "deciles": deciles,
        "events": out_events,
        "events_window": {
            "max_age_days": args.max_age_days,
            "cutoff": cutoff,
            "n_total": len(events),
            "n_output": len(out_events),
            "order": "announce_date desc",
        },
        "warnings": warnings,
        "provenance": {
            "source": "data/fin/*.json (财报) + data/bulk/day_qfq_*.csv (价格)",
            "origin": "GitCode stock_analysis/pead/",
            "car_note": "个股绝对收益，未剔除同期市场收益（非超额收益口径）",
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n事件 {len(events)} 个（有收益 {len(scored)}），匹配股票 {matched}")
    for w in warnings:
        print(f"  警告: {w}")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
