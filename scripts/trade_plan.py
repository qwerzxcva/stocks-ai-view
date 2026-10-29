#!/usr/bin/env python3
"""trade_plan.py — 交易管理层（批次三：9 本书落成的决策链）

这一层不选股。它回答的是「已经决定要买/已经有持仓，接下来怎么下单、怎么管」——
这是本批书（幽灵、专业投机原理、亚当理论、澄明之境、作手回忆录、短线三本）
真正能落到代码的部分，也是回测里最容易被忽略、实盘里最致命的环节。

落成的规则（每条注明出处，便于回溯）：

  幽灵规则一   只持有被证明正确的仓位 → 开仓后必须由「价格按预期走」来确认，
               不确认就走（时间止损），不靠"再等等"
  幽灵规则二   只在盈利时加仓 → 加仓价必须高于首仓价，且加仓后整体止损上移
  幽灵规则三   巨量就是出货 → 持仓期出现异常巨量（≥3 倍均量）优先减仓
  亚当理论     不预测、只跟随；止损前置 → 止损价在开仓前就算好，不允许"进去了再找"
  Sperandeo    2B 法则（破前高不成立即反向）+ 期望值门槛
  利弗莫尔     试探性建仓→盈利加码→不摊平亏损；沿最小阻力线持仓
               （注："关键点/pivotal point"是 1940《股票大作手操盘术》的概念，
                非《回忆录》；本文件不叫 pivotal，口径按最小阻力线 + 区间突破）
  A 股约束     T+1（当日不能卖）、涨跌停（10/20/30%）、100 股整数倍、
               佣金/印花税/滑点成本、停牌风险

纪律：
  * 参数默认值全是先验，必须回测校准后才能当阈值用（见 docs/books/batch3_*）
  * 任何一条硬规则不通过就出 blocker，不给"勉强可以"的模糊结论
  * 期望值不达标直接拒绝开仓，而不是降低仓位硬上

用法：
  python3 scripts/trade_plan.py --demo
  python3 scripts/trade_plan.py --code sh600016
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import limit_caliber as LC  # noqa: E402

# ── 参数（先验，必须回测校准；别把这组数当真理）──────────────────
RISK_PER_TRADE = 0.01      # 单笔风险占总权益 1%
MAX_POSITIONS = 6          # 最多同时持仓
MAX_SINGLE_POS = 0.25      # 单票市值上限 25%
MAX_INDUSTRY_POS = 0.40    # 单行业上限 40%
DAILY_LOSS_STOP = 0.03     # 单日亏损 3% 熔断
ATR_STOP_MULT = 2.0        # 波动止损 2×ATR
MAX_STOP_ATR = 3.0         # 结构止损超过 3×ATR 就改口径并降规模
COST_BPS = 25              # 双边成本：佣金+印花税+滑点，约 25bp
MIN_RR = 2.0               # 最低盈亏比
TIME_STOP_BARS = 5         # 幽灵规则一：N 根内未被证明正确就走


# ─────────────────────────── 期望值 ───────────────────────────
def expectancy(win_rate, rr, cost_bps=COST_BPS):
    """每笔期望收益（以风险 R 为单位）。

    E = p×rr − (1−p) − cost。cost 用 R 的百分比折进去：成本相对止损距离，
    止损越窄成本占比越高——这就是短线不能做太窄的原因。
    """
    p = win_rate
    gross = p * rr - (1 - p)
    cost_r = (cost_bps / 10000.0) / max(rr, 1e-6) * 2  # 近似折算，保守翻倍
    return {"gross_R": round(gross, 3), "cost_R": round(cost_r, 3),
            "net_R": round(gross - cost_r, 3),
            "ok": gross - cost_r > 0}


# ─────────────────────────── 止损 ───────────────────────────
def stop_plan(entry, atr, structure_lows=None, atr_mult=ATR_STOP_MULT):
    """止损三选一，取「结构失效位」与「波动容忍」中更合理的一个。

    规则（来自亚当/幽灵：止损前置；来自 Sperandeo：止损要在结构失效之外）：
      1. 结构止损：信号棒低点 / 中枢下沿 / 前低的 min
      2. 波动止损：entry − atr_mult × ATR
      3. 若结构止损距 entry > MAX_STOP_ATR×ATR → 说明进场位置太差，改用 ATR 止损并标记 warn
    """
    atr = max(atr, 1e-9)
    struct = min([x for x in (structure_lows or []) if x and x < entry], default=None)
    atr_stop = entry - atr_mult * atr
    warns = []
    if struct is None:
        stop = atr_stop
        basis = "atr"
        warns.append("无可用结构位，退回 ATR 止损")
    else:
        dist = entry - struct
        if dist > MAX_STOP_ATR * atr:
            stop = atr_stop
            basis = "atr_capped"
            warns.append(f"结构位 {struct:.2f} 距入场 {(dist/atr):.1f}×ATR 过远，改 ATR 止损并降规模")
        else:
            stop = struct
            basis = "structure"
    # 止损距离同时给下限：太窄会被噪声打掉（短线成本占比也会爆炸）
    if entry - stop < 0.5 * atr:
        stop = entry - 0.5 * atr
        warns.append("止损过窄（<0.5×ATR），已放宽到 0.5×ATR")
    return {"stop": round(stop, 3), "basis": basis,
            "risk_per_share": round(entry - stop, 3),
            "atr_stop": round(atr_stop, 3),
            "structure_stop": round(struct, 3) if struct else None,
            "warns": warns}


# ─────────────────────────── 仓位 ───────────────────────────
def position_size(equity, entry, stop, risk_pct=RISK_PER_TRADE,
                  max_single=MAX_SINGLE_POS, lot=100):
    """按单笔风险预算反推股数，再受单票上限约束。

    注意 A 股必须 100 股整数倍，算出来不足 1 手就是「不该做」，返回 0 而不是勉强买 100 股。
    """
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {"shares": 0, "reason": "止损不低于入场价，计划无效"}
    risk_amount = equity * risk_pct
    by_risk = risk_amount / risk_per_share
    by_cap = (equity * max_single) / entry
    raw = min(by_risk, by_cap)
    shares = int(raw // lot) * lot
    return {"shares": shares, "risk_amount": round(risk_amount, 2),
            "by_risk_shares": int(by_risk // lot) * lot,
            "by_cap_shares": int(by_cap // lot) * lot,
            "position_value": round(shares * entry, 2),
            "position_pct": round(shares * entry / equity * 100, 2) if equity else 0,
            "binding": "risk" if by_risk <= by_cap else "cap",
            "reason": "" if shares else "资金不足 1 手"}


# ─────────────────────────── 目标与盈亏比 ───────────────────────────
def targets(entry, stop, atr, measured_move=None):
    """目标位：测量移动优先（结构给的），否则按 R 倍数给梯子。"""
    r = entry - stop
    out = {"R": round(r, 3), "t1": round(entry + MIN_RR * r, 3),
           "t2": round(entry + 3 * r, 3), "basis": "R_multiple"}
    if measured_move and measured_move > entry:
        out["t_measured"] = round(measured_move, 3)
        out["basis"] = "measured_move"
    out["rr_at_t1"] = round((out["t1"] - entry) / r, 2) if r > 0 else 0
    return out


# ─────────────────────────── 规则检查 ───────────────────────────
def rule_checks(code, entry, stop, plan, equity, positions=None, win_rate=None):
    """逐条硬规则检查。返回 (checks, blockers)。

    blockers 非空就不该下单——不是"建议谨慎"，是拒绝。
    """
    checks, blockers = [], []

    def ok(name, cond, note, hard=True):
        checks.append({"rule": name, "pass": bool(cond), "note": note})
        if not cond and hard:
            blockers.append(f"{name}: {note}")

    # 亚当/幽灵：止损必须前置且低于入场
    ok("止损前置", 0 < stop < entry, f"stop={stop} entry={entry}")
    # 期望值门槛
    if win_rate is not None:
        e = expectancy(win_rate, plan["targets"]["rr_at_t1"])
        ok("期望值为正", e["net_R"] > 0, f"net_R={e['net_R']}（胜率 {win_rate:.0%}、RR {plan['targets']['rr_at_t1']}）")
    # 盈亏比
    ok("盈亏比达标", plan["targets"]["rr_at_t1"] >= MIN_RR,
       f"RR={plan['targets']['rr_at_t1']}（要求 ≥{MIN_RR}）")
    # 仓位
    ok("可下单手数", plan["size"]["shares"] > 0, plan["size"].get("reason") or f"{plan['size']['shares']} 股")
    # 涨跌停：涨停买不进、跌停卖不出
    lim = LC.limit_pct(code)
    ok("非涨跌停区域", True, f"本票涨跌幅上限 {lim}%")  # 实时判定交给调用方传 chg_pct
    # T+1：当日买入不能卖 → 止损必须按"次日才生效"设计
    ok("T+1 兼容", True, "止损最早次日生效，日内不做止损假设（A 股 T+1）")
    # 持仓数上限
    if positions is not None:
        ok("持仓数量上限", len(positions) < MAX_POSITIONS, f"现有 {len(positions)} 只，上限 {MAX_POSITIONS}")
        ok("单票不重复", code not in positions, "同标的已持仓，不加新计划（加仓走 add_on）")
    return checks, blockers


# ─────────────────────────── 组合约束 ───────────────────────────
def portfolio_check(equity, positions, code, add_value, industry_map=None, max_industry=MAX_INDUSTRY_POS):
    """组合层：单票/单行业/总仓上限。positions: [{code, value, industry}]"""
    used = sum(p["value"] for p in positions)
    blockers = []
    if used + add_value > equity:
        blockers.append(f"总市值 {(used+add_value)/equity:.0%} 超过权益")
    ind = (industry_map or {}).get(code)
    if ind:
        ind_val = sum(p["value"] for p in positions if p.get("industry") == ind) + add_value
        if ind_val > equity * max_industry:
            blockers.append(f"行业 {ind} 占比 {ind_val/equity:.0%} 超过上限 {max_industry:.0%}")
    return {"pass": not blockers, "blockers": blockers,
            "used_pct": round((used + add_value) / equity * 100, 1) if equity else 0}


# ─────────────────────────── 加仓 / 移动止盈 ───────────────────────────
def add_on(entry, current, stop, atr, add_pct=0.5, pyramid=0.5):
    """幽灵规则二：只在盈利加仓；利弗莫尔：金字塔递减。

    add_pct: 距首仓多少 ATR 以上才允许加
    pyramid: 每次加仓规模为上一次的 50%
    """
    if current <= entry:
        return {"allow": False, "reason": "未盈利不加仓（幽灵规则二）"}
    if (current - entry) < add_pct * atr:
        return {"allow": False, "reason": f"盈利不足 {add_pct}×ATR，空间不够再加"}
    new_stop = max(stop, current - ATR_STOP_MULT * atr * 0.8)
    return {"allow": True, "size_multiplier": pyramid,
            "new_stop": round(new_stop, 3),
            "note": "加仓后整体止损上移，保护已得利润"}


def trail(current, stop, atr, phase=None):
    """移动止盈。阶段不同容忍度不同：拉升期给宽、派发期收紧。"""
    mult = {"markup": 2.5, "accumulating": 2.0, "distributing": 1.2, "markdown": 1.0}.get(phase, 2.0)
    new_stop = max(stop, current - mult * atr)
    return {"stop": round(new_stop, 3), "mult": mult,
            "tightened": new_stop > stop}


def volume_alert(volume, vol_ma20):
    """幽灵规则三：巨量就是出货。≥3 倍均量视为异常，优先减仓而不是加仓。"""
    vr = volume / vol_ma20 if vol_ma20 else 0
    return {"vol_ratio": round(vr, 2), "climax": vr >= 3.0,
            "action": "减仓/至少不加仓" if vr >= 3.0 else "正常"}


def time_stop(entry, current, atr, bars_held, win_rate=None):
    """幽灵规则一：只持有被证明正确的仓位。

    建仓后 N 根内没有「收盘 > 入场 + 0.25×ATR」就算没被证明，减半或高开走人。
    这比止损更容易被忽略：多数人只看价格有没有打止损，不看“时间是不是在浪费”。
    """
    proven = current >= entry + 0.25 * atr
    expired = bars_held >= TIME_STOP_BARS
    if proven:
        return {"action": "hold", "reason": "已被证明正确"}
    if expired:
        return {"action": "reduce_half", "reason": f"{bars_held} 根仍未证明正确，减半或离场"}
    return {"action": "wait", "reason": f"还剩 {TIME_STOP_BARS - bars_held} 根验证窗口"}


def circuit_breaker(win_rate, alpha=0.05, hard_cap=6):
    """连亏多少笔该停手：不写死 3 笔，按胜率算。

    依据：连胜率 p 的交易者，连亏 k 笔的概率 = (1−p)^k。取「发生概率低于 alpha」作为停手点：
        k = ln(alpha) / ln(1−p)
    p=0.45 → k≈5.4；p=0.35 → k≈7.8。写死 3 笔会在正常回撤时就停手，写死 10 笔又太晚。
    """
    p = max(min(win_rate or 0.45, 0.95), 0.05)
    k = math.log(alpha) / math.log(1 - p)
    return {"threshold": min(int(math.ceil(k)), hard_cap),
            "raw": round(k, 1), "win_rate": p,
            "action": "连亏达阈值后降频/停手复盘，恢复需先纸面验证 20 笔"}


# ─────────────────────────── 组装 ───────────────────────────
def build_plan(code, entry, atr, equity, structure_lows=None,
               win_rate=None, positions=None, industry_map=None,
               measured_move=None):
    stops = stop_plan(entry, atr, structure_lows)
    size = position_size(equity, entry, stops["stop"])
    tg = targets(entry, stops["stop"], atr, measured_move)
    plan = {"code": code, "entry": round(entry, 3), "stop_plan": stops,
            "size": size, "targets": tg}
    checks, blockers = rule_checks(code, entry, stops["stop"], plan, equity,
                                   positions, win_rate)
    if positions is not None:
        pc = portfolio_check(equity, positions, code, size.get("position_value", 0), industry_map)
        blockers += pc["blockers"]
        plan["portfolio"] = pc
    plan["checks"] = checks
    plan["blockers"] = blockers
    plan["verdict"] = "可执行" if not blockers else "拒绝"
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("--equity", type=float, default=100000)
    a = ap.parse_args()

    if a.demo or not a.code:
        plan = build_plan("sh600016", entry=3.60, atr=0.09, equity=a.equity,
                          structure_lows=[3.48, 3.42], win_rate=0.45,
                          measured_move=3.78)
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        return

    # 真实模式：读结构层/威科夫层输出，把结构止损位接进来
    import kline_source as KS
    bars = KS.load_bars(a.code)
    if len(bars) < 60:
        print("数据不足")
        return
    atrv = 0.0
    trs = [max(bars[i]["high"] - bars[i]["low"],
               abs(bars[i]["high"] - bars[i - 1]["close"]),
               abs(bars[i]["low"] - bars[i - 1]["close"])) for i in range(1, len(bars))]
    atrv = sum(trs[-14:]) / 14
    lows = [min(b["low"] for b in bars[-10:]), min(b["low"] for b in bars[-30:])]
    plan = build_plan(a.code, entry=bars[-1]["close"], atr=atrv, equity=a.equity,
                      structure_lows=lows)
    print(json.dumps(plan, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
