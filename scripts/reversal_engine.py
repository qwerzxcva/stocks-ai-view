#!/usr/bin/env python3
"""reversal_engine.py — 反转结构层（书 #4《高级反转技术分析》MTR 状态机）

核心命题（全书最容易做错的一条）：**反转是过程，不是一根棒**。
四要件齐才成立：极值 → 破线 → 回抽 → 二次信号。
少任何一环都只是"看起来像顶/底"——那叫回调/反弹，不叫反转。
书里反复强调的第二条：**位置决定解读**，同一个反转棒在趋势末端与区间中部含义相反。

判据（日线可算版本，逐条标注近似方式，不要当成原书精确定义）：
1. 极值 extreme：近 20 根内的最高 high / 最低 low，且该点两侧各 3 根未越过（摆动点）
2. 破线 break：极值之后收盘跌破「极值到前一个反向摆动点」的连线外推值，
   或兜底跌破 MA10（趋势线在日线上不可靠，MA10 是可控近似）
3. 回抽 pullback：反向回撤到 极值↔破线低点 区间的 20%~70%，且未越过极值
4. 二次信号 second：回抽后重新朝反转方向推进，顶为 lower high、底为 higher low

另外补 #3《高级波段技术分析》的「失败突破」：
突破 20 日新高/新低后 3 根内收回 → failed_break（书里视为最强反向信号之一）。

纪律（写在代码里）：
1. 只用 t 及之前的数据，bars 已由 signal_validator 截断
2. 方向不写死：BULLISH_TAGS 只声明「若成立偏多」的候选，符号由 calibrate 实测决定
   （本项目在 A 股日线 5 日尺度已实测：多数「突破确认/形态确认」类信号反向）
3. 阶段标签与确认标签都输出，方便校准分层——只给最终结论会看不出哪一环失效

用法：
    python3 scripts/reversal_engine.py --code sh600017 -v
    python3 scripts/reversal_engine.py --validate --sample 200
    python3 scripts/reversal_engine.py --calibrate --sample 200
    python3 scripts/reversal_engine.py --all
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kline_source import list_codes, load_bars, load_all  # noqa: E402
import signal_validator as SV  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or os.path.join(os.path.dirname(HERE), "data")
CST = timezone(timedelta(hours=8))

SWING_K = 3          # 摆动点确认：两侧各 3 根
EXTREME_WIN = 20     # 极值搜索窗口
BREAK_LOOKBACK = 60  # 找「前一个反向摆动点」的回溯上限
BREAK_MAX_GAP = 15   # 破线必须发生在极值后 15 根内（跨度太长说明不是同一结构）
PULLBACK_MAX_GAP = 15  # 回抽必须在破线后 15 根内
CONFIRM_MAX_GAP = 12   # 二次信号必须在回抽后 12 根内（否则结构过期，只留阶段标签）
BREAK_MIN_ATR = 0.3    # 破线幅度下限：至少 0.3 ATR，避免均线缠绕的噪声被判成破线
PULLBACK_LO = 0.20
PULLBACK_HI = 0.70
MIN_BARS = 80

# 理论上偏多的标签（底部反转链全部环节 + 跌破失败）。
# 顶部链默认偏空、不列入：若实测为正，edge 会标 contrarian，这正是「看跌叙事失效」的表达。
BULLISH_TAGS = (
    "mtr:bottom_confirmed",
    "mtr:second_higher_low",
    "mtr:extreme_bottom",
    "mtr:break_up",
    "mtr:pullback_down",
    "mtr:failed_break_down",
)


# ─────────────────────────── 基元 ───────────────────────────
def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"])))
    return sum(trs) / len(trs)


def ma(bars, n):
    if len(bars) < n:
        return None
    return sum(b["close"] for b in bars[-n:]) / n


def swing_highs(bars, k=SWING_K):
    out = []
    for i in range(k, len(bars) - k):
        h = bars[i]["high"]
        if all(h >= bars[j]["high"] for j in range(i - k, i + k + 1)):
            out.append(i)
    return out


def swing_lows(bars, k=SWING_K):
    out = []
    for i in range(k, len(bars) - k):
        l = bars[i]["low"]
        if all(l <= bars[j]["low"] for j in range(i - k, i + k + 1)):
            out.append(i)
    return out


# ─────────────────────────── MTR 状态机 ───────────────────────────
def _top_reversal(bars, atrv):
    """顶部反转：极值→破线→回抽→二次信号。返回 (tags, detail)。"""
    n = len(bars)
    tags, detail = [], {}
    win_start = n - EXTREME_WIN
    # 1) 极值：近 20 根最高 high
    hi_off = max(range(EXTREME_WIN), key=lambda i: bars[win_start + i]["high"])
    hi_idx = win_start + hi_off
    hi = bars[hi_idx]["high"]
    # MTR 说的是**趋势末端**的反转，不是任意 20 日高点：
    # 1) 必须在 60 日新高附近（允许 1% 容差）——否则只是区间内的小高点
    look = bars[max(0, hi_idx - 60): hi_idx + 1]
    if hi < max(b["high"] for b in look) * 0.99:
        return tags, detail
    # 2) 前置上升趋势：极值前 10 根累计涨幅 ≥ 2%，否则无趋势可反转
    if hi_idx >= 10:
        pre = bars[hi_idx - 10]["close"]
        if pre and (bars[hi_idx]["close"] / pre - 1.0) * 100.0 < 2.0:
            return tags, detail
    sh = swing_highs(bars)
    if hi_idx not in sh:
        return tags, detail  # 不是摆动高点，不算极值
    detail["extreme_idx"], detail["extreme_high"] = hi_idx, round(hi, 3)
    tags.append("mtr:extreme_top")

    # 前一个反向摆动点（破线参考位）
    prev_lows = [i for i in swing_lows(bars) if hi_idx - BREAK_LOOKBACK <= i < hi_idx]
    if not prev_lows:
        return tags, detail
    ref_idx = prev_lows[-1]
    ref_low = bars[ref_idx]["low"]
    detail["break_ref_idx"], detail["break_ref_low"] = ref_idx, round(ref_low, 3)

    # 2) 破线：极值之后收盘跌破 极值→参考低点 连线外推，或 MA10
    slope = (ref_low - hi) / max(1, (hi_idx - ref_idx))
    brk_idx, brk_level = None, None
    m10 = []
    acc = 0.0
    for i in range(n):
        acc += bars[i]["close"]
        if i >= 10:
            acc -= bars[i - 10]["close"]
        m10.append(acc / 10.0 if i >= 9 else None)
    for j in range(hi_idx + 1, min(n, hi_idx + BREAK_MAX_GAP + 1)):
        line = hi + slope * (j - hi_idx)
        m = m10[j]
        ref = m if m is not None else line
        below_line = bars[j]["close"] < line - BREAK_MIN_ATR * atrv
        below_ma = m is not None and bars[j]["close"] < m - BREAK_MIN_ATR * atrv * 0.5
        if below_line or below_ma:
            brk_idx, brk_level = j, round(min(line, ref), 3)
            break
    if brk_idx is None:
        return tags, detail
    detail["break_idx"], detail["break_level"] = brk_idx, brk_level
    detail["last_idx"] = brk_idx
    tags.append("mtr:break_down")

    # 3) 回抽：破线后最高价回撤到 极值↔破线段低点 的 20%~70%，且未越过极值
    seg_low = min(b["low"] for b in bars[brk_idx: n]) if n > brk_idx else ref_low
    span = hi - seg_low
    if span <= 0:
        return tags, detail
    pb_hi_idx, pb_ok = None, False
    for j in range(brk_idx + 1, min(n, brk_idx + PULLBACK_MAX_GAP + 1)):
        b = bars[j]
        if b["high"] > hi:
            detail["invalidated"] = "回抽越过极值（趋势延续，非反转）"
            return tags, detail
        ratio = (b["high"] - seg_low) / span
        if PULLBACK_LO <= ratio <= PULLBACK_HI and pb_hi_idx is None:
            pb_ok, pb_hi_idx = True, j
    if not pb_ok:
        return tags, detail
    detail["pullback_idx"], detail["pullback_high"] = pb_hi_idx, round(bars[pb_hi_idx]["high"], 3)
    detail["last_idx"] = pb_hi_idx
    tags.append("mtr:pullback_up")

    # 结构过期：回抽后太久没有二次信号，只留阶段标签
    if n - 1 - pb_hi_idx > CONFIRM_MAX_GAP:
        detail["stale"] = f"回抽后 {n - 1 - pb_hi_idx} 根无二次信号"
        return tags, detail

    # 4) 二次信号：回抽后形成 lower high，且随后收盘跌破回抽低点
    pb_low = min(b["low"] for b in bars[pb_hi_idx: n])
    # 二次信号：回抽之后必须**真的没再创新高**且已回落，否则只是震荡
    after = bars[pb_hi_idx + 1:]
    pb_high = bars[pb_hi_idx]["high"]
    lower_high = len(after) >= 2 and max(b["high"] for b in after) < pb_high
    follow_through = bars[-1]["close"] < pb_low or (
        n >= 2 and bars[-1]["close"] < bars[-2]["close"] and bars[-1]["close"] < pb_low * 1.02
    )
    if lower_high:
        tags.append("mtr:second_lower_high")
    if lower_high and follow_through:
        tags.append("mtr:top_confirmed")
        detail["confirmed"] = True
        detail["last_idx"] = n - 1
    return tags, detail


def _bottom_reversal(bars, atrv):
    """底部反转：镜像逻辑。"""
    n = len(bars)
    tags, detail = [], {}
    win_start = n - EXTREME_WIN
    lo_off = min(range(EXTREME_WIN), key=lambda i: bars[win_start + i]["low"])
    lo_idx = win_start + lo_off
    lo = bars[lo_idx]["low"]
    # 底部镜像：60 日新低附近 + 前置下跌趋势
    look = bars[max(0, lo_idx - 60): lo_idx + 1]
    if lo > min(b["low"] for b in look) * 1.01:
        return tags, detail
    if lo_idx >= 10:
        pre = bars[lo_idx - 10]["close"]
        if pre and (bars[lo_idx]["close"] / pre - 1.0) * 100.0 > -2.0:
            return tags, detail
    sl = swing_lows(bars)
    if lo_idx not in sl:
        return tags, detail
    detail["extreme_idx"], detail["extreme_low"] = lo_idx, round(lo, 3)
    tags.append("mtr:extreme_bottom")

    prev_highs = [i for i in swing_highs(bars) if lo_idx - BREAK_LOOKBACK <= i < lo_idx]
    if not prev_highs:
        return tags, detail
    ref_idx = prev_highs[-1]
    ref_high = bars[ref_idx]["high"]
    detail["break_ref_idx"], detail["break_ref_high"] = ref_idx, round(ref_high, 3)

    slope = (ref_high - lo) / max(1, (lo_idx - ref_idx))
    m10 = []
    acc = 0.0
    for i in range(n):
        acc += bars[i]["close"]
        if i >= 10:
            acc -= bars[i - 10]["close"]
        m10.append(acc / 10.0 if i >= 9 else None)
    brk_idx, brk_level = None, None
    for j in range(lo_idx + 1, min(n, lo_idx + BREAK_MAX_GAP + 1)):
        line = lo + slope * (j - lo_idx)
        m = m10[j]
        ref = m if m is not None else line
        above_line = bars[j]["close"] > line + BREAK_MIN_ATR * atrv
        above_ma = m is not None and bars[j]["close"] > m + BREAK_MIN_ATR * atrv * 0.5
        if above_line or above_ma:
            brk_idx, brk_level = j, round(max(line, ref), 3)
            break
    if brk_idx is None:
        return tags, detail
    detail["break_idx"], detail["break_level"] = brk_idx, brk_level
    detail["last_idx"] = brk_idx
    tags.append("mtr:break_up")

    seg_high = max(b["high"] for b in bars[brk_idx: n])
    span = seg_high - lo
    if span <= 0:
        return tags, detail
    pb_lo_idx, pb_ok = None, False
    for j in range(brk_idx + 1, min(n, brk_idx + PULLBACK_MAX_GAP + 1)):
        b = bars[j]
        if b["low"] < lo:
            detail["invalidated"] = "回抽跌破极值（趋势延续，非反转）"
            return tags, detail
        ratio = (seg_high - b["low"]) / span
        if PULLBACK_LO <= ratio <= PULLBACK_HI and pb_lo_idx is None:
            pb_ok, pb_lo_idx = True, j
    if not pb_ok:
        return tags, detail
    detail["pullback_idx"], detail["pullback_low"] = pb_lo_idx, round(bars[pb_lo_idx]["low"], 3)
    detail["last_idx"] = pb_lo_idx
    tags.append("mtr:pullback_down")

    if n - 1 - pb_lo_idx > CONFIRM_MAX_GAP:
        detail["stale"] = f"回抽后 {n - 1 - pb_lo_idx} 根无二次信号"
        return tags, detail

    pb_high = max(b["high"] for b in bars[pb_lo_idx: n])
    # 二次信号：回抽之后必须真的没再创新低
    after = bars[pb_lo_idx + 1:]
    pb_lowv = bars[pb_lo_idx]["low"]
    higher_low = len(after) >= 2 and min(b["low"] for b in after) > pb_lowv
    follow_through = bars[-1]["close"] > pb_high or (
        n >= 2 and bars[-1]["close"] > bars[-2]["close"] and bars[-1]["close"] > pb_high * 0.98
    )
    if higher_low:
        tags.append("mtr:second_higher_low")
    if higher_low and follow_through:
        tags.append("mtr:bottom_confirmed")
        detail["confirmed"] = True
        detail["last_idx"] = n - 1
    return tags, detail


def failed_break(bars):
    """#3 高级波段技术分析：突破 20 日新高/新低后 3 根内收回 → 失败突破（强反向）。"""
    tags = []
    if len(bars) < EXTREME_WIN + 5:
        return tags
    n = len(bars)
    for back in range(1, 4):
        j = n - 1 - back  # 突破发生在 j 根
        if j < EXTREME_WIN:
            continue
        hi_prev = max(b["high"] for b in bars[j - EXTREME_WIN: j])
        lo_prev = min(b["low"] for b in bars[j - EXTREME_WIN: j])
        if bars[j]["high"] > hi_prev and bars[-1]["close"] < hi_prev:
            tags.append("mtr:failed_break_up")
        if bars[j]["low"] < lo_prev and bars[-1]["close"] > lo_prev:
            tags.append("mtr:failed_break_down")
    return tags


def analyze(bars, code=""):
    if not bars or len(bars) < MIN_BARS:
        return None
    atrv = atr(bars) or 1e-9
    t_tags, t_detail = _top_reversal(bars, atrv)
    b_tags, b_detail = _bottom_reversal(bars, atrv)
    # 方向互斥：同一窗口内可以先后出现顶与底，当下只认更近完成的那一套，
    # 同时打 top_confirmed + bottom_confirmed 会让下游拿到互相抵消的信号。
    if t_tags and b_tags:
        if t_detail.get("last_idx", -1) >= b_detail.get("last_idx", -1):
            b_tags = []
        else:
            t_tags = []
    fb = failed_break(bars)
    tags = list(dict.fromkeys(t_tags + b_tags + fb))
    last = bars[-1]
    return {
        "code": code,
        "date": last["date"],
        "close": round(last["close"], 3),
        "tags": tags,
        "atr": round(atrv, 3),
        "top": t_detail,
        "bottom": b_detail,
        "note": "MTR 四要件为日线近似：趋势线用 极值→前摆动点 外推 + MA10 兜底",
    }


def run_all(codes=None, out_path=None):
    codes = codes or list_codes()
    barset = load_all(codes)
    items, skipped = [], 0
    for code, bars in barset.items():
        try:
            r = analyze(bars, code)
        except Exception as e:
            skipped += 1
            print(f"skip {code}: {str(e)[:60]}", file=sys.stderr)
            continue
        if r:
            items.append(r)
        else:
            skipped += 1
    doc = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "reversal_engine/v1",
        "book": "#4 高级反转技术分析（MTR）+ #3 高级波段（失败突破）",
        "count": len(items),
        "items": items,
    }
    out_path = out_path or os.path.join(DATA_DIR, "reversal_signals.json")
    tmp = out_path + ".tmp"
    json.dump(doc, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return doc, skipped


def _tag_fn(view):
    r = analyze(view, "x")
    return r["tags"] if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args()

    if a.code:
        r = analyze(load_bars(a.code, limit=400), a.code)
        if not r:
            print("数据不足，无法分析")
            return
        if a.verbose:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print(f"{r['code']} {r['date']} close={r['close']} tags={r['tags']}")
        return

    if a.validate or a.calibrate:
        codes = list_codes()[: a.sample]
        samples = [(c, load_bars(c, limit=400)) for c in codes]
        samples = [(c, b) for c, b in samples if len(b) >= 120]
        rows = SV.validate(_tag_fn, samples, horizon=5, min_history=MIN_BARS, cut_step=2)
        if a.validate:
            SV.print_rows(rows, 30)
        if a.calibrate:
            table = SV.calibrate(rows, BULLISH_TAGS, min_n=100, min_t=1.5)
            p = os.path.join(DATA_DIR, "reversal_calibration.json")
            SV.write_calibration(p, table, meta={"sample": a.sample,
                                                 "engine": "reversal_engine/v1"}, horizon=5)
            print(f"校准表 → {p}（{len(table)} 个标签）")
            for k, v in sorted(table.items(), key=lambda kv: -abs(kv[1]["t"]))[:10]:
                print(f"  {k:28} n={v['n']:>5} fwd={v['avg_fwd']:>6.2f}% t={v['t']:>5.2f} {v['edge']}")
        return

    if a.all:
        doc, skipped = run_all()
        print(f"已写出 {doc['count']} 只（跳过 {skipped}）→ data/reversal_signals.json")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
