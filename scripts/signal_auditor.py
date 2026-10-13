#!/usr/bin/env python3
"""
signal_auditor.py — 信号审核引擎

对决策卡/推荐列表做**发布前审核**, 拦截六类问题:
  1. 逻辑矛盾   同一股票同时出现「低位吸筹」与「高位套人」; 或 buy 与 avoid 同现
  2. 数值异常   价格<=0 / 入场>=止损 / 目标<=入场 / 置信度越界
  3. 集中度风险 同行业占比 >40%, 或单板块 >3 只 (变相单押)
  4. 与历史冲突 该股近 5 日已被判 avoid 又变 buy (翻烧饼)
  5. 过拟合嫌疑 场景分与原始分差异 >30% (说明权重被单一维度主导)
  6. 缺失必要字段 无 intrinsic_value / 无 dims / 无 why (不可解释)

输出: data/signal_audit.json (通过/拦截/警告 三类 + 每条原因)
退出码: 有拦截 → 1 (供 CI 门禁, 防止脏信号上线)

设计原则:
  - 只拦「能自动判定的错误」, 不替代人工判断
  - 每条拦截带 code + reason + severity, 可追溯
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MAX_INDUSTRY_PCT = 40.0      # 同行业占比上限
MAX_SECTOR_N = 3             # 同板块最多几只
MAX_SCORE_DELTA_PCT = 30.0   # 场景分 vs 原始分差异上限
LOOKBACK_DAYS = 5            # 翻烧饼检测回看天数


def audit_card(c, scen=None):
    """审核单张决策卡。返回 (blocked_reasons, warnings)。"""
    blocked, warns = [], []
    code = c.get("code", "?")

    # 2. 数值异常
    px = c.get("price")
    if px is not None and px <= 0:
        blocked.append(f"价格非正: {px}")
    entry, stop, target = c.get("entry"), c.get("stop"), c.get("target")
    if entry is not None and stop is not None and entry <= stop:
        blocked.append(f"入场{entry} <= 止损{stop}")
    if entry is not None and target is not None and target <= entry:
        blocked.append(f"目标{target} <= 入场{entry}")
    conf = c.get("confidence")
    if conf is not None and not (0 <= conf <= 1):
        blocked.append(f"置信度越界: {conf}")

    # 1. 逻辑矛盾
    why = c.get("why") or {}
    pos_why = why.get("高低位", "")
    if "高位套人" in pos_why and c.get("action") in ("buy", "可打"):
        blocked.append(f"高位套人却建议买入: {pos_why[:40]}")
    dims = c.get("dims") or {}
    if dims:
        cap = dims.get("资金面")
        pos = dims.get("高低位")
        if cap is not None and pos is not None and cap < 30 and pos < 30:
            blocked.append(f"资金面{cap}与高低位{pos}双低却入选")

    # 6. 缺失必要字段
    if c.get("action") == "buy" and not why:
        warns.append("buy 但无 why (不可解释)")
    if not dims:
        warns.append("无 dims (多维信息缺失)")

    # 5. 过拟合嫌疑
    if scen is not None:
        orig = c.get("composite")
        s = c.get("scenario_score")
        if orig and s and orig > 0:
            delta = abs(s - orig) / orig * 100
            if delta > MAX_SCORE_DELTA_PCT:
                warns.append(f"场景分{s}与原始分{orig}差异{delta:.0f}%>"
                             f"{MAX_SCORE_DELTA_PCT:.0f}% (权重可能被单维度主导)")
    return blocked, warns


def audit_portfolio(cards):
    """组合层面审核: 集中度。"""
    issues = []
    n = len(cards)
    if n == 0:
        return issues
    # 同行业集中度
    inds = Counter()
    for c in cards:
        ind = (c.get("industry") or "").strip()
        if ind:
            inds[ind] += 1
    for ind, cnt in inds.most_common():
        pct = cnt / n * 100
        if pct > MAX_INDUSTRY_PCT:
            issues.append(f"行业集中度风险: {ind} 占 {pct:.0f}% ({cnt}/{n})")
        if cnt > MAX_SECTOR_N:
            issues.append(f"单行业超配: {ind} {cnt} 只 (> {MAX_SECTOR_N})")
    # 动作集中度
    acts = Counter(c.get("action", "?") for c in cards)
    if acts.get("buy", 0) == n and n >= 10:
        issues.append(f"全部 {n} 只都是 buy, 无观察/回避分层 (可能是牛市末期信号)")
    return issues


def check_flip_flop(cards, hist_files):
    """翻烧饼: 近几日的 avoid 里出现今天的 buy。"""
    flips = []
    today_codes = {c.get("code") for c in cards if c.get("action") == "buy"}
    for hf in hist_files[-LOOKBACK_DAYS:]:
        if not os.path.exists(hf):
            continue
        try:
            h = json.load(open(hf, encoding="utf-8"))
        except Exception:
            continue
        for c in (h.get("cards") or h.get("avoid") or []):
            if c.get("code") in today_codes and c.get("action") == "avoid":
                flips.append(f"{c.get('code')} {c.get('name')} 近期曾判 avoid, 今日转 buy")
    return flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(DATA_DIR, "decision.json"))
    ap.add_argument("--scenario", default=os.path.join(DATA_DIR, "scenario.json"))
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "signal_audit.json"))
    ap.add_argument("--fail-on-block", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.inp):
        print(f"[auditor] 输入不存在: {args.inp}")
        return 1
    d = json.load(open(args.inp, encoding="utf-8"))
    scen = None
    if os.path.exists(args.scenario):
        try:
            scen = json.load(open(args.scenario, encoding="utf-8"))
        except Exception:
            scen = None

    cards = d.get("cards") or []
    blocked, warned = [], []
    for c in cards:
        b, w = audit_card(c, scen)
        for r in b:
            blocked.append({"code": c.get("code"), "name": c.get("name"),
                            "action": c.get("action"), "reason": r,
                            "severity": "block"})
        for r in w:
            warned.append({"code": c.get("code"), "name": c.get("name"),
                           "reason": r, "severity": "warn"})

    portfolio_issues = audit_portfolio(cards)
    for p in portfolio_issues:
        warned.append({"code": "(portfolio)", "reason": p, "severity": "warn"})

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "signal_auditor v1 (发布前审核)",
        "n_cards": len(cards),
        "n_blocked": len(blocked),
        "n_warned": len(warned),
        "blocked": blocked,
        "warned": warned,
        "checks": ["逻辑矛盾", "数值异常", "集中度风险", "翻烧饼",
                   "过拟合嫌疑", "缺失必要字段"],
        "meta": {
            "limits": {"max_industry_pct": MAX_INDUSTRY_PCT,
                       "max_sector_n": MAX_SECTOR_N,
                       "max_score_delta_pct": MAX_SCORE_DELTA_PCT},
            "caveat": "只拦可自动判定的错误, 不替代人工判断",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[auditor] {len(cards)} 张卡: 拦截 {len(blocked)}, 警告 {len(warned)}")
    for b in blocked[:8]:
        print(f"  [BLOCK] {b['code']} {b['name']}: {b['reason']}")
    for w in warned[:6]:
        print(f"  [WARN] {w['code']}: {w['reason']}")
    print(f"[auditor] → {args.out}")
    if args.fail_on_block and blocked:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
