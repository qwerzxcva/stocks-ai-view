#!/usr/bin/env python3
"""
anomaly_detector.py — 异常检测引擎

数据跳变/分布漂移/因子失效自动告警。numeric_validator 验「单点数值」,
本引擎验「时序与分布」—— 后者才是因子失效的早期信号。

检测项:
  1. 数据断更   某文件 ts 距现在 > 阈值 (交易时段 40min, 非交易 20h)
  2. 数值跳变   相邻交易日 price 跳变 > 25% 且非涨跌停可解释
  3. 分布漂移   某指标今日分布与过去 20 日均值偏离 > 3 个标准差
  4. 因子失效   某指标最近 N 日的 vote 与收益相关性符号翻转
  5. 覆盖率下跌 K 线覆盖数比昨日少 > 5% (数据源出问题)

输出: data/anomaly_report.json (按严重度分级)
"""
import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = "/root/workspace/stocks-data-kline/data/bulk"

TRADING_STALE_MIN = 40
NONTRADING_STALE_H = 20


def is_trading_now():
    now = datetime.now(CST)
    return now.weekday() < 5 and ((9 <= now.hour < 12) or (13 <= now.hour < 16))


def check_staleness():
    """1. 数据断更检测。"""
    now = datetime.now(CST)
    stale = []
    trading = is_trading_now()
    limit_min = TRADING_STALE_MIN if trading else NONTRADING_STALE_H * 60
    for f in glob.glob(os.path.join(DATA_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        ts = d.get("ts")
        if not ts or not isinstance(ts, str):
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=CST)
        except Exception:
            continue
        age_min = (now - t).total_seconds() / 60
        name = os.path.basename(f)
        # 只监控核心文件
        if name in ("market_now.json", "latest.json", "enrich.json",
                    "multidim.json", "decision.json"):
            if age_min > limit_min:
                stale.append({
                    "type": "staleness", "file": name,
                    "age_min": round(age_min, 1),
                    "limit_min": limit_min,
                    "severity": "critical" if trading else "warning",
                    "msg": f"{name} {age_min:.0f} 分钟未更新 (阈值 {limit_min}min)",
                })
    return stale


def check_kline_coverage():
    """5. K 线覆盖率下跌。"""
    n_codes = set()
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                n_codes.add(line.split("|", 1)[0])
    n = len(n_codes)
    hist_path = os.path.join(DATA_DIR, "kline_coverage_history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path, encoding="utf-8"))
        except Exception:
            hist = []
    issues = []
    if hist:
        prev = hist[-1].get("n", 0)
        if prev > 0 and n < prev * 0.95:
            issues.append({
                "type": "coverage_drop", "prev": prev, "now": n,
                "severity": "critical",
                "msg": f"K线覆盖 {prev} → {n} (-{(prev-n)/prev*100:.0f}%), 数据源可能出问题",
            })
    hist.append({"ts": datetime.now(CST).isoformat(timespec="seconds"), "n": n})
    hist = hist[-60:]
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(hist, open(hist_path, "w", encoding="utf-8"))
    return issues, n


def check_distribution_drift():
    """3. 分布漂移: multidim TOP 票的综合分分布 vs 历史。"""
    path = os.path.join(DATA_DIR, "multidim.json")
    if not os.path.exists(path):
        return []
    try:
        md = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    top = md.get("top") or []
    if len(top) < 10:
        return []
    comps = [r.get("composite", 0) for r in top if r.get("composite") is not None]
    if len(comps) < 10:
        return []
    mean = statistics.mean(comps)
    hist_path = os.path.join(DATA_DIR, "multidim_score_history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path, encoding="utf-8"))
        except Exception:
            hist = []
    issues = []
    if len(hist) >= 5:
        past_means = [h["mean"] for h in hist[-20:]]
        past_mean = statistics.mean(past_means)
        past_std = statistics.pstdev(past_means) if len(past_means) > 1 else 0
        if past_std > 0:
            z = (mean - past_mean) / past_std
            if abs(z) > 3:
                issues.append({
                    "type": "distribution_drift", "z": round(z, 2),
                    "now_mean": round(mean, 1), "past_mean": round(past_mean, 1),
                    "severity": "warning",
                    "msg": f"TOP票综合分均值 {mean:.1f} vs 历史 {past_mean:.1f} "
                           f"(z={z:.1f}), 分布漂移",
                })
    hist.append({"ts": datetime.now(CST).isoformat(timespec="seconds"),
                 "mean": round(mean, 2)})
    hist = hist[-60:]
    json.dump(hist, open(hist_path, "w", encoding="utf-8"))
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "anomaly_report.json"))
    args = ap.parse_args()

    issues = []
    issues += check_staleness()
    cov_issues, n_codes = check_kline_coverage()
    issues += cov_issues
    issues += check_distribution_drift()

    crit = [i for i in issues if i.get("severity") == "critical"]
    warn = [i for i in issues if i.get("severity") == "warning"]

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "anomaly_detector v1 (时序/分布异常)",
        "kline_coverage": n_codes,
        "n_issues": len(issues),
        "by_severity": {"critical": len(crit), "warning": len(warn)},
        "issues": issues,
        "checks": ["数据断更", "K线覆盖率下跌", "分布漂移"],
        "meta": {
            "limits": {"trading_stale_min": TRADING_STALE_MIN,
                       "nontrading_stale_h": NONTRADING_STALE_H},
            "todo": "数值跳变/因子失效待接 (需历史快照对比)",
            "caveat": "异常告警是数据质量门禁, 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[anomaly] K线覆盖 {n_codes} | 异常 {len(issues)} 个 "
          f"(critical={len(crit)} warning={len(warn)})")
    for i in issues[:10]:
        print(f"  [{i.get('severity','?').upper()}] {i.get('msg', i.get('type'))}")
    print(f"[anomaly] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
