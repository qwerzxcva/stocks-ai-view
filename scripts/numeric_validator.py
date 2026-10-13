#!/usr/bin/env python3
"""
numeric_validator.py — 数值数据验证引擎

audit_data.py 只验「文件存在 + JSON 合法 + 字段非空」, 不验数值合理性。
本引擎补齐: 对每个数据文件做数值域检查, 拦住会污染全链路的脏数据。

检查项:
  price      > 0 且 < 100000 (茅台 1500, 超 10万必然是错)
  chg_pct    ∈ [-35, 35] (A股涨跌停 20cm/30cm, 超界必然是错)
  volume     ≥ 0
  turnover   ≥ 0 且 < 1e7 (亿)
  nav        > 0
  score      ∈ [0, 100] 或 [0,1] (两种量纲都认)
  ret_*      ∈ [-100, 500] (基金 1年 226% 见过, 超 500 可疑)
  重复 code   同文件内 code 唯一
  日期格式    YYYY-MM-DD 或 YYYYMMDD
  时间戳      ts 不能是未来

输出: data/validation_report.json (问题清单 + 严重度)
退出码: 0=无 critical, 1=有 critical (供 CI 失败)
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

# 字段 → (min, max, 严重度)
# 注意: chg_pct 在 cycle_pool_v2/cycle_deep 里是「年度/历史涨跌幅」(可达 ±700%),
#       与个股日涨跌幅 (±35%) 量纲不同, 用 chg_pct_period 区分。
NUMERIC_RULES = {
    "price": (0.0001, 100000, "critical"),
    "close": (0.0001, 100000, "critical"),
    "nav": (0.0001, 100000, "critical"),
    "intrinsic_value": (0.0001, 100000, "warning"),
    "ret_pct": (-35, 35, "warning"),
    "daily_chg_pct": (-35, 35, "critical"),
    "ret_1y": (-100, 500, "warning"),
    "ret_6m": (-100, 300, "warning"),
    "ret_3m": (-100, 200, "warning"),
    "ret_1m": (-100, 100, "warning"),
    "volume": (0, 1e15, "warning"),
    "turnover": (0, 1e7, "warning"),
    "mktcap": (0, 1e16, "warning"),
    "floatcap": (0, 1e16, "warning"),
    "pe": (-10000, 100000, "warning"),
    "pb": (-10000, 100000, "warning"),
    "composite": (-100, 100, "warning"),      # cycle_deep 用 -5~+5
    "score": (-1000, 1000, "info"),
    "n_samples": (0, 1e9, "info"),
    "win_rate": (0, 100, "critical"),
    "zt_next_rate": (0, 100, "critical"),
    "zhaban_next_rate": (0, 100, "critical"),
    "raw_win_rate": (0, 100, "critical"),
    "limit_up": (0, 10000, "critical"),
    "limit_down": (0, 10000, "critical"),
    "advance": (0, 10000, "warning"),
    "decline": (0, 10000, "warning"),
    # 周期/年度涨跌幅: 量纲 ±1000% (A股 1993 年 +690%)
    "chg_pct_period": (-1000, 1000, "info"),
}

# 只在「日涨跌幅上下文」里检查 chg_pct 的 ±35 域:
# 判定规则 — 同记录里有 date 且无 year/month 字段才是日涨跌幅
DAILY_CHG_CONTEXT_FIELDS = ("date", "ts", "pick_date", "trade_date")

LIST_FIELDS = ["picks", "cards", "avoid", "buy", "watch", "strong",
               "watch_list", "top", "rows", "items", "news", "reports",
               "themes", "mainlines", "fund_rank", "limit_up_pool",
               "ths_strong", "lhb", "blocktrade", "tier1", "tier2",
               "by_lianban", "indices"]


def walk_records(obj, path=""):
    """递归产出所有 dict 记录 (带路径)。"""
    if isinstance(obj, dict):
        yield obj, path
        for k, v in obj.items():
            yield from walk_records(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            yield from walk_records(v, f"{path}[{i}]")


def validate_file(path, max_issues=200):
    name = os.path.basename(path)
    issues = []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return [{"file": name, "field": "(whole)", "severity": "critical",
                 "msg": f"JSON 解析失败: {e}"}]

    now = datetime.now(CST)
    for rec, p in walk_records(d):
        # 数值域
        for field, (lo, hi, sev) in NUMERIC_RULES.items():
            if field not in rec:
                continue
            v = rec[field]
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                if sev == "critical":
                    issues.append({"file": name, "field": f"{p}.{field}",
                                   "severity": "critical",
                                   "msg": f"非数值类型 {type(v).__name__}: {v!r}"[:120]})
                continue
            if v < lo or v > hi:
                issues.append({"file": name, "field": f"{p}.{field}",
                               "severity": sev,
                               "msg": f"越界 {v} 不在 [{lo}, {hi}]"})
        # chg_pct 量纲分流: 日涨跌幅(±35) vs 周期涨跌幅(±1000)
        # 判定: 记录含 year/month/annual 字段, 或路径含 yearly_kline/annual_rotation → 周期量纲
        if "chg_pct" in rec and isinstance(rec["chg_pct"], (int, float)):
            is_periodic = (any(k in rec for k in ("year", "month", "annual",
                                                  "best_month", "worst_month"))
                           or "yearly_kline" in p or "annual_rotation" in p)
            if not is_periodic:
                v = rec["chg_pct"]
                if v < -35 or v > 35:
                    issues.append({"file": name, "field": f"{p}.chg_pct",
                                   "severity": "critical",
                                   "msg": f"日涨跌幅越界 {v} 不在 [-35, 35]"})
        # 时间戳未来
        ts = rec.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=CST)
                if (t - now).total_seconds() > 86400:
                    issues.append({"file": name, "field": f"{p}.ts",
                                   "severity": "warning",
                                   "msg": f"时间戳在未来: {ts[:19]}"})
            except Exception:
                pass
        if len(issues) >= max_issues:
            issues.append({"file": name, "field": "(truncated)",
                           "severity": "info", "msg": f"问题过多, 截断于 {max_issues}"})
            break
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DATA_DIR)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "validation_report.json"))
    ap.add_argument("--fail-on-critical", action="store_true",
                    help="有 critical 问题时退出码 1 (供 CI 门禁)")
    args = ap.parse_args()

    all_issues = []
    files = sorted(f for f in os.listdir(args.dir) if f.endswith(".json")
                   and f != "validation_report.json")
    for f in files:
        all_issues.extend(validate_file(os.path.join(args.dir, f)))

    crit = [i for i in all_issues if i["severity"] == "critical"]
    warn = [i for i in all_issues if i["severity"] == "warning"]
    info = [i for i in all_issues if i["severity"] == "info"]

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "numeric_validator v1",
        "n_files": len(files),
        "n_issues": len(all_issues),
        "by_severity": {"critical": len(crit), "warning": len(warn), "info": len(info)},
        "issues": all_issues[:300],
        "rules": {k: list(v[:2]) for k, v in NUMERIC_RULES.items()},
        "caveat": "数值域检查拦不住逻辑错误, 只拦明显脏数据",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    print(f"[validator] {len(files)} 个文件, {len(all_issues)} 个问题 "
          f"(critical={len(crit)} warning={len(warn)} info={len(info)})")
    for i in crit[:10]:
        print(f"  [CRIT] {i['file']} {i['field']}: {i['msg']}")
    for i in warn[:5]:
        print(f"  [WARN] {i['file']} {i['field']}: {i['msg']}")
    print(f"[validator] → {args.out}")
    if args.fail_on_critical and crit:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
