#!/usr/bin/env python3
"""资金流与价格背离：只计算按交易日对齐、有连续窗口证据的信号。"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import BULK
except Exception:
    BULK = os.environ.get("STOCKS_KLINE_BULK") or "/root/workspace/stocks-data-kline/data/bulk"


def prefixed(code):
    c = str(code)
    if c.startswith(("sh", "sz", "bj")):
        return c
    c = c.zfill(6)
    if c.startswith(("60", "68", "90", "11", "13")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sz" + c


def secid(code):
    p = prefixed(code)
    return ("1." if p[:2] == "sh" else "0.") + p[2:]


def load_klines(bulk):
    data = {}
    paths = [path for path in sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv")))
             if os.path.basename(path) not in ("day_qfq_recent.csv",)]
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    data.setdefault(code, []).append(
                        [parts[0].strip(), float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
    for code, bars in data.items():
        by_date = {bar[0]: bar for bar in bars}
        data[code] = [by_date[date] for date in sorted(by_date)]
    return data


def fetch_flow_detail(code, n=60):
    """Return (rows, rejection_reason); source failures never synthesize flow."""
    urls = [
        ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
         f"?secid={secid(code)}&lmt={n}&klt=101"
         "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"),
        ("https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
         f"?secid={secid(code)}&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&lmt={n}&klt=101"),
    ]
    payload, saw_empty = None, False
    for url in urls:
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
            candidate = json.loads(body.decode("utf-8"))
        except Exception:
            continue
        klines = ((candidate.get("data") or {}).get("klines")) or []
        if len(klines) >= 25:
            payload = candidate
            break
        saw_empty = saw_empty or not klines
        payload = payload or candidate
    if payload is None or len(((payload.get("data") or {}).get("klines")) or []) < 25:
        sina = fetch_sina_flow(code, n)
        if sina[0]:
            return sina
    if payload is None:
        return [], "source_unavailable"
    klines = ((payload.get("data") or {}).get("klines")) or []
    if not klines:
        return [], "source_empty" if saw_empty else "source_short_window"
    rows = []
    for row in klines:
        fields = row.split(",")
        if len(fields) < 2:
            continue
        try:
            rows.append({"date": fields[0], "main_net": float(fields[1])})
        except ValueError:
            continue
    rows.sort(key=lambda item: item["date"])
    return (rows, None) if rows else ([], "invalid_rows")


def fetch_sina_flow(code, n=60):
    """Sina historical main-flow fallback. Amounts stay in source units."""
    symbol = prefixed(code)
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={n}&sort=opendate&asc=0&daima={symbol}")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        rows = json.loads(body.decode("utf-8"))
    except Exception:
        return [], "source_unavailable"
    if not isinstance(rows, list) or len(rows) < 25:
        return [], "source_short_window"
    parsed = []
    for row in rows:
        try:
            parsed.append({"date": str(row["opendate"])[:10], "main_net": float(row["netamount"])})
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item["date"])
    return (parsed, None) if len(parsed) >= 25 else ([], "invalid_rows")


def fetch_flow(code, n=60):
    return fetch_flow_detail(code, n)[0]


def _slope(series, index, span=5):
    if index - 2 * span < 0 or index >= len(series):
        return None
    return ((series[index] - series[index - span]) -
            (series[index - span] - series[index - 2 * span])) / span


def _flow_map(flows):
    mapped = {}
    for row in flows:
        try:
            mapped[str(row["date"])[:10]] = float(row["main_net"])
        except (KeyError, TypeError, ValueError):
            continue
    return mapped


def analyze_detail(code, bars, flows):
    """Return (result, reason); missing trading sessions are never compressed."""
    if not bars:
        return None, "no_kline"
    bars = sorted({bar[0]: bar for bar in bars}.values(), key=lambda bar: bar[0])
    if len(bars) < 30:
        return None, "insufficient_kline_days"
    flow_by_date = _flow_map(flows)
    if not flow_by_date:
        return None, "insufficient_flow_source"
    dates = [bar[0] for bar in bars]
    overlap = [date for date in dates if date in flow_by_date]
    if len(overlap) < 25:
        return None, "insufficient_aligned_days"
    # The latest K-line date, not the latest common date, anchors the signal.
    # A stale flow source cannot silently produce an older signal.
    if dates[-1] not in flow_by_date:
        return None, "flow_not_current"
    required = dates[-20:]
    if any(date not in flow_by_date for date in required):
        return None, "flow_gap_in_window"
    closes = [bar[2] for bar in bars[-20:]]
    funds = [flow_by_date[date] for date in required]
    ap = _slope(closes, len(closes) - 1)
    af = _slope(funds, len(funds) - 1)
    close = closes[-1]
    ap_pct = ap / close * 100 if close else 0.0
    scale = max(1e5, sum(abs(value) for value in funds) / len(funds))
    af_norm = af / scale
    persist = sum(value > 0 for value in funds[-10:]) / 10
    raw_side = 1 if ap_pct < -0.02 and af_norm > 0.05 else (-1 if ap_pct > 0.02 and af_norm < -0.05 else 0)
    diverge = raw_side if (raw_side > 0 and persist >= 0.6) or (raw_side < 0 and persist <= 0.4) else 0

    true_ranges = []
    for index in range(1, len(bars)):
        true_ranges.append(max(bars[index][3] - bars[index][4],
                               abs(bars[index][3] - bars[index - 1][2]),
                               abs(bars[index][4] - bars[index - 1][2])))
    v20 = sum(true_ranges[-20:]) / max(1, len(true_ranges[-20:]))
    v60 = sum(true_ranges[-60:]) / max(1, len(true_ranges[-60:]))
    compress = v20 / v60 if v60 else 1.0
    if diverge > 0:
        signal = "吸筹背离"
    elif diverge < 0:
        signal = "派发背离"
    elif compress < 0.8:
        signal = "波动收敛（非资金背离）"
    elif persist >= 0.7:
        signal = "资金持续流入"
    elif persist <= 0.3:
        signal = "资金持续流出"
    else:
        signal = "无显著背离"
    score = 50 + 40 * diverge * min(abs(af_norm) / 0.3, 1.0) + 40 * (persist - 0.5) + 10 * (1 - min(compress, 1.5) / 1.5)
    return {
        "code": code, "date": dates[-1], "close": round(close, 2),
        "score": round(max(0, min(100, score)), 1), "signal": signal,
        "diverge": diverge, "price_accel_pct": round(ap_pct, 4),
        "flow_accel_norm": round(af_norm, 4), "flow_persist": round(persist, 2),
        "vol_compress": round(compress, 3), "n_flow_days": len(flow_by_date),
        "n_aligned_days": len(overlap), "flow_date_start": min(flow_by_date),
        "flow_date_end": max(flow_by_date),
        "provenance": {"source": "本地前复权日K + 东财主力净流入", "confidence": "observed", "snapshot_ts": dates[-1]},
    }, None


def analyze(code, bars, flows):
    return analyze_detail(code, bars, flows)[0]


def _coverage(bars, flows):
    dates = {bar[0] for bar in bars}
    flow_dates = set(_flow_map(flows))
    return {"kline_days": len(dates), "flow_days": len(flow_dates),
            "aligned_days": len(dates & flow_dates),
            "kline_date_start": min(dates) if dates else None,
            "kline_date_end": max(dates) if dates else None,
            "flow_date_start": min(flow_dates) if flow_dates else None,
            "flow_date_end": max(flow_dates) if flow_dates else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bulk", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--codes", help="只算这些股票")
    args = parser.parse_args()
    now = datetime.now(CST)
    klines = load_klines(args.bulk or BULK)
    if args.codes:
        want = [code.strip() for code in args.codes.split(",")]
        targets = [code for code in klines if any(code.endswith(item) or code == item for item in want)]
    else:
        latest = max((bars[-1][0] for bars in klines.values() if bars), default=None)
        targets = [code for code, bars in klines.items()
                   if len(bars) >= 30 and bars[-1][0] == latest]
        targets.sort(key=lambda code: -klines[code][-1][5])
        targets = targets[:args.limit]
    results, rejection_reasons, totals = [], Counter(), Counter()
    coverage_samples = []
    for index, code in enumerate(targets):
        flows, error = fetch_flow_detail(code, 60)
        coverage = _coverage(klines[code], flows)
        coverage_samples.append({"code": code, **coverage})
        totals.update({key: coverage[key] for key in ("kline_days", "flow_days", "aligned_days")})
        result, reason = analyze_detail(code, klines[code], flows)
        if error:
            reason = error
        if result and not error:
            results.append(result)
        else:
            rejection_reasons[reason] += 1
        if (index + 1) % 50 == 0:
            print(f"[flow_divergence] {index+1}/{len(targets)} 有效 {len(results)} 拒绝 {sum(rejection_reasons.values())}", flush=True)
        time.sleep(0.12)
    results.sort(key=lambda row: -abs(row["score"] - 50))
    buy_side = [row for row in results if row["diverge"] > 0]
    sell_side = [row for row in results if row["diverge"] < 0]
    n_confirmed = len(buy_side) + len(sell_side)
    n_no_flow = sum(rejection_reasons[key] for key in ("source_unavailable", "source_empty", "invalid_rows", "insufficient_flow_source"))
    flow_source_coverage = (len(targets) - n_no_flow) / len(targets) if targets else 0.0
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "flow_divergence (自研: 资金流-价格背离)",
        "status": "insufficient" if flow_source_coverage < 0.8 or not results else ("no_confirmed_divergence" if not n_confirmed else "ok"),
        "n_scanned": len(targets), "n_scored": len(results),
        "n_confirmed_divergence": n_confirmed, "n_no_flow": n_no_flow,
        "n_rejected": sum(rejection_reasons.values()),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "coverage": {"totals": dict(totals), "by_code": coverage_samples,
                     "flow_source_ratio": round(flow_source_coverage, 4), "minimum_flow_source_ratio": 0.8,
                     "rule": "exact trading-day match; latest day and all 20 recent sessions must have funds"},
        "buy_side": buy_side[:30], "sell_side": sell_side[:30],
        "meta": {
            "formula": "score = 50 + 40*D + 40*(S-0.5) + 10*(1-V/1.5)",
            "D": "价格与资金加速度反向，且资金持续性确认",
            "S": "近10个交易日主力净流入为正的天数占比",
            "V": "近20日真实波幅/近60日真实波幅; 不单独作为背离证据",
            "source": "价格: 本地前复权日K | 资金: 东财 fflow，不足25日时回退新浪 MoneyFlow.ssl_qsfx_zjlrqs",
            "provenance": {"source": "本地前复权日K + 东财/新浪主力净流入", "confidence": "observed", "snapshot_ts": now.isoformat(timespec="seconds")},
        },
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=1)
    print(f"[flow_divergence] {len(targets)} scanned, {len(results)} scored, {n_confirmed} confirmed, {out['status']}; → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
