#!/usr/bin/env python3
"""structure_engine.py — K 线结构识别层（信号引擎的上游）

为什么需要这一层：现有引擎（signal_hub / engine_v5 / ai_pick_v4 / multidim_engine）
全在「指标坐标系」里工作——均线、MACD、RSI、量比阈值。指标是价格的有损压缩，
同样的 MACD 金叉可能是趋势中继，也可能是区间顶部的诱多。缺的是**结构判据**：
这根 K 线在什么形态里、处于哪一段结构、量价是否配合。

本模块把下列方法论落成可判定规则（每处标注来源，便于回溯与替换）：

  蜡烛形态        《日本蜡烛图技术》(Nison)：锤子/上吊、射击之星/倒锤、吞没、孕线/十字孕线、
                  启明星/黄昏星、刺透/乌云盖顶、十字星细分（蜻蜓/墓碑/长腿）；
                  配 ATR 归一化 + 位置条件（书里最强调「形态本身无意义，位置决定意义」，
                  所以每条形态都带上下文判据）；孕线另带第三根 K 线确认
  价格行为        《高级趋势技术分析》《高级波段技术分析》《高级反转技术分析》
                  (Al Brooks)：趋势棒/十字棒/反转棒、HH-HL / LH-LL / 区间三态、
                  突破-回抽、失败突破、测量移动、二次入场
  缠论            《零基础看懂缠论》：包含关系处理 → 分型 → 笔 → 中枢 → 三类买卖点
                  + 背驰（同向两段力度比较）
  波浪            《艾略特波浪理论》：摆动序列上的 5 浪推动 / 3 浪调整启发式识别，
                  含三条硬性规则（2 浪不破 1 浪起点、3 浪非最短、4 浪不与 1 浪重叠）
  量价 / 威科夫   《量价操盘法》《威科夫操盘法》：量比、价量四象限、努力与结果背离、
                  无需求/上冲回落/停止量、吸筹-派发-拉升-下跌的阶段提示

纪律：
  * 全部只用 t 及之前的数据，不做未来函数
  * 形态/结构命中一律带 evidence（哪几根 K、哪个价、哪条规则），可复核
  * 不确定的地方给 confidence 而不是硬给结论；判不出来返回 unknown

用法：
  python3 scripts/structure_engine.py --all                 # 全市场写 data/structure_signals.json
  python3 scripts/structure_engine.py --code sh600016 -v    # 单只调试，打印明细
  python3 scripts/structure_engine.py --validate --sample 300  # 逐信号前向收益检验
"""
import argparse
import json
import os
import statistics
import sys

import signal_validator as SV
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or os.path.join(os.path.dirname(HERE), "data")
DUCKDB_PATH = os.environ.get("STOCKS_DB") or "/root/workspace/stocks-data-live/data/stocks.duckdb"
KLINE_DAY_DIR = os.environ.get("STOCKS_KLINE_DAY") or os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "day")
CST = timezone(timedelta(hours=8))

# ── 阈值集中在此，方便按回测结果调，不要散落在函数里 ───────────────────
PIN_SHADOW_RATIO = 2.0      # 影线 / 实体，Nison 的锤子线核心比例
PIN_BODY_IN_TAIL = 1.0 / 3  # 实体必须落在区间上/下 1/3
STRONG_BAR_BODY = 0.6       # 趋势棒：实体占全幅比例
DOJI_BODY_RATIO = 0.1       # 十字星：实体 / 全幅上限
DOJI_SHADOW_EQ = 0.5        # 长腿十字：短影 / 长影 ≥ 该值算上下均衡
SWING_K = 2                 # 摆动点分形强度：左右各 2 根
CHAN_MIN_GAP = 4            # 缠论笔：两个分型之间至少间隔 K 线数（含端点）
BREAKOUT_LOOKBACK = 20
RANGE_BAND = 0.06           # 区间判定：高低点幅度 / 中位价 < 6%
VOL_MA = 20


# ─────────────────────────────── 数据层 ────────────────────────────────
# 数据访问已抽到 kline_source.py（多引擎共用，避免复权口径分叉）。
# 这里只做别名转发，保持既有的 import 与调用点不变。
from kline_source import (DATA_DIR, DUCKDB_PATH, KLINE_DAY_DIR,  # noqa: E402
                          duck as _duck, list_codes, load_all, load_bars,
                          read_day_csv as _read_day_csv)
# ─────────────────────────────── 基础工具 ──────────────────────────────
def atr(bars, n=14):
    """Average True Range，Wilder 平滑。返回与 bars 等长的列表（前 n 个用累计均值补齐）。"""
    trs = []
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i < n:
            out.append(sum(trs) / len(trs))
        else:
            out.append((out[-1] * (n - 1) + tr) / n)
    return out


def body(b):
    return abs(b["close"] - b["open"])


def rng(b):
    return max(b["high"] - b["low"], 1e-9)


def upper_shadow(b):
    return b["high"] - max(b["open"], b["close"])


def lower_shadow(b):
    return min(b["open"], b["close"]) - b["low"]


def body_ratio(b):
    return body(b) / rng(b)


def swings(bars, k=SWING_K):
    """分形摆动点。返回 [(idx, 'H'|'L', price)]，严格左右各 k 根不创新高/低。"""
    out = []
    for i in range(k, len(bars) - k):
        hi, lo = bars[i]["high"], bars[i]["low"]
        left = bars[i - k:i]
        right = bars[i + 1:i + 1 + k]
        if all(hi > x["high"] for x in left) and all(hi > x["high"] for x in right):
            out.append((i, "H", hi))
        elif all(lo < x["low"] for x in left) and all(lo < x["low"] for x in right):
            out.append((i, "L", lo))
    return out


def vol_ma(bars, n=VOL_MA):
    out = []
    for i in range(len(bars)):
        s = bars[max(0, i - n + 1):i + 1]
        out.append(sum(x["volume"] for x in s) / max(len(s), 1) or 1e-9)
    return out


def _pct(a, b):
    return (a / b - 1.0) if b else 0.0


# ────────────────────── 一、蜡烛形态（Nison） ─────────────────────────
def candle_patterns(bars, atrv=None):
    """只在「有位置意义」的地方报形态：锤子/上吊必须在摆动低点附近，反之亦然。

    返回事件列表；每条带 pos（相对最近摆动点的方位）与 strength(0~1)。
    """
    if len(bars) < 10:
        return []
    atrv = atrv or atr(bars)
    sw = swings(bars)
    lows = [p for (_, t, p) in sw if t == "L"]
    highs = [p for (_, t, p) in sw if t == "H"]
    events = []
    i = len(bars) - 1  # 只判最新一根（增量场景）以及最后一根之前两根（三星形态）
    last = bars[i]
    atr_i = atrv[i] or 1e-9

    near_low = bool(lows) and min(abs(last["low"] - x) for x in lows[-3:]) < 0.5 * atr_i
    near_high = bool(highs) and min(abs(last["high"] - x) for x in highs[-3:]) < 0.5 * atr_i
    # 关键修正：swings() 的分型需要左右各 k 根确认，**最后一根永远不可能是摆动点**。
    # 只靠摆动点判位置，会让「刚刚创出新低的那根锤子线」永远不命中——实盘等于该形态失效。
    # 因此再补一条滚动窗口判据：与近 20 根的最低/最高距离在一个 ATR 内，就算处于低位/高位。
    win = bars[-21:]
    lo_w = min(b["low"] for b in win)
    hi_w = max(b["high"] for b in win)
    near_low = near_low or (last["low"] - lo_w) <= 1.0 * atr_i
    near_high = near_high or (hi_w - last["high"]) <= 1.0 * atr_i

    big_range = rng(last) >= 0.8 * atr_i
    b_ratio = body_ratio(last)

    # 锤子线 / 上吊线（同形态不同位置：低位=锤子，高位=上吊）
    # 上影的约束不能只看「≤ 0.5×实体」：小实体时 0.5×实体只有几分钱，真实锤子线会被漏判。
    # 因此容差同时绑全幅（≤ 8%），哪个宽松取哪个。
    max_up = max(0.5 * body(last), 0.08 * rng(last))
    max_dn = max(0.5 * body(last), 0.08 * rng(last))
    if (big_range and body(last) > 0 and lower_shadow(last) >= PIN_SHADOW_RATIO * body(last)
            and upper_shadow(last) <= max_up and b_ratio <= 0.45):
        if near_low:
            events.append({"kind": "hammer", "dir": "bull", "strength": 0.7,
                           "src": "蜡烛图技术/低位锤子", "note": "下影≥2倍实体且实体落在上1/3"})
        elif near_high:
            events.append({"kind": "hanging_man", "dir": "bear", "strength": 0.45,
                           "src": "蜡烛图技术/上吊线", "note": "同形态在高位转为派发含义"})

    # 射击之星 / 倒锤
    if (big_range and body(last) > 0 and upper_shadow(last) >= PIN_SHADOW_RATIO * body(last)
            and lower_shadow(last) <= max_dn and b_ratio <= 0.45):
        if near_high:
            events.append({"kind": "shooting_star", "dir": "bear", "strength": 0.7,
                           "src": "蜡烛图技术/射击之星", "note": "上影≥2倍实体，高位=拒绝"})
        elif near_low:
            events.append({"kind": "inverted_hammer", "dir": "bull", "strength": 0.4,
                           "src": "蜡烛图技术/倒锤", "note": "同形态在低位需次日确认"})

    # 吞没
    if i >= 1:
        p = bars[i - 1]
        if (last["close"] > last["open"] and p["close"] < p["open"]
                and last["open"] <= p["close"] and last["close"] >= p["open"]
                and body(last) > body(p)):
            events.append({"kind": "bullish_engulfing", "dir": "bull",
                           "strength": 0.65 if near_low else 0.45,
                           "src": "蜡烛图技术/看涨吞没", "note": "实体完全包住前一根阴线"})
        if (last["close"] < last["open"] and p["close"] > p["open"]
                and last["open"] >= p["close"] and last["close"] <= p["open"]
                and body(last) > body(p)):
            events.append({"kind": "bearish_engulfing", "dir": "bear",
                           "strength": 0.65 if near_high else 0.45,
                           "src": "蜡烛图技术/看跌吞没", "note": "实体完全包住前一根阳线"})

    # 孕线（趋势衰竭的预警，不是反转确认）
    # 十字孕线是特例：第二根是十字星且被前一根大实体包含——收敛更强，但依然要第三根确认
    if i >= 1:
        p = bars[i - 1]
        if p["high"] > last["high"] and p["low"] < last["low"]:
            if body(p) > 1.2 * atr_i:
                if b_ratio <= DOJI_BODY_RATIO:
                    events.append({"kind": "harami_cross", "dir": "neutral", "strength": 0.4,
                                   "src": "蜡烛图技术/十字孕线",
                                   "note": "前一根大实体被十字星完全包孕，必须等第三根 K 线确认"})
                else:
                    events.append({"kind": "harami", "dir": "neutral", "strength": 0.35,
                                   "src": "蜡烛图技术/孕线", "note": "前一根大实体被完全包孕，趋势暂歇"})

    # 十字星（多空均衡，出现在长趋势末端才有意义）
    # 细分为蜻蜓/墓碑/长腿：三者开收都接近，但影线方向含义相反，笼统报 doji 会把多空搞反
    if b_ratio <= DOJI_BODY_RATIO and big_range:
        us, ls = upper_shadow(last), lower_shadow(last)
        if ls >= PIN_SHADOW_RATIO * atr_i and us <= 0.1 * atr_i:
            events.append({"kind": "dragonfly_doji", "dir": "bull",
                           "strength": 0.6 if near_low else 0.3,
                           "src": "蜡烛图技术/蜻蜓十字", "note": "只有长下影：低位是拒绝下跌，高位只是滞涨"})
        elif us >= PIN_SHADOW_RATIO * atr_i and ls <= 0.1 * atr_i:
            events.append({"kind": "gravestone_doji", "dir": "bear",
                           "strength": 0.6 if near_high else 0.3,
                           "src": "蜡烛图技术/墓碑十字", "note": "只有长上影：高位是冲高失败，低位只是承接不足"})
        elif min(us, ls) >= DOJI_SHADOW_EQ * max(us, ls) and min(us, ls) >= 0.5 * atr_i:
            events.append({"kind": "long_legged_doji", "dir": "neutral",
                           "strength": 0.45 if (near_high or near_low) else 0.2,
                           "src": "蜡烛图技术/长腿十字", "note": "上下影都长：多空来回撕，方向不明"})
        else:
            events.append({"kind": "doji", "dir": "neutral",
                           "strength": 0.4 if (near_high or near_low) else 0.2,
                           "src": "蜡烛图技术/十字星", "note": "开盘收盘几乎相同"})

    # 刺透 / 乌云盖顶（两根反转组合）：只吃进前一根实体一半以上、未完全包住，强度低于吞没
    if i >= 1:
        p = bars[i - 1]
        mid = (p["open"] + p["close"]) / 2
        if (p["close"] < p["open"] and last["close"] > last["open"]
                and last["open"] < p["close"]
                and mid < last["close"] < p["open"]):
            events.append({"kind": "piercing", "dir": "bull",
                           "strength": 0.6 if near_low else 0.4,
                           "src": "蜡烛图技术/刺透形态", "note": "低开收过前阴线实体中点、未及开盘价"})
        if (p["close"] > p["open"] and last["close"] < last["open"]
                and last["open"] > p["close"]
                and p["open"] < last["close"] < mid):
            events.append({"kind": "dark_cloud", "dir": "bear",
                           "strength": 0.6 if near_high else 0.4,
                           "src": "蜡烛图技术/乌云盖顶", "note": "高开收破前阳线实体中点、未及开盘价"})

    # 第三根 K 线确认：孕线/十字孕线只是衰竭预警，收盘突破母体区间才算反转成立
    if i >= 2:
        mom, mid_bar = bars[i - 2], bars[i - 1]
        if mom["high"] > mid_bar["high"] and mom["low"] < mid_bar["low"] and body(mom) > 1.2 * atr_i:
            if last["close"] > mom["high"]:
                events.append({"kind": "harami_confirmed_bull", "dir": "bull", "strength": 0.6,
                               "src": "蜡烛图技术/孕线+第三根确认", "note": "母体被向上突破，衰竭转为反转"})
            elif last["close"] < mom["low"]:
                events.append({"kind": "harami_confirmed_bear", "dir": "bear", "strength": 0.6,
                               "src": "蜡烛图技术/孕线+第三根确认", "note": "母体被向下跌破，衰竭转为反转"})

    # 启明星 / 黄昏星（三根组合）
    if i >= 2:
        a, b_, c = bars[i - 2], bars[i - 1], bars[i]
        if (a["close"] < a["open"] and body(b_) < 0.4 * body(a)
                and c["close"] > c["open"] and c["close"] > (a["open"] + a["close"]) / 2):
            events.append({"kind": "morning_star", "dir": "bull", "strength": 0.7 if near_low else 0.5,
                           "src": "蜡烛图技术/启明星", "note": "长阴→小实体→长阳收过前一根中点"})
        if (a["close"] > a["open"] and body(b_) < 0.4 * body(a)
                and c["close"] < c["open"] and c["close"] < (a["open"] + a["close"]) / 2):
            events.append({"kind": "evening_star", "dir": "bear", "strength": 0.7 if near_high else 0.5,
                           "src": "蜡烛图技术/黄昏星", "note": "长阳→小实体→长阴收过前一根中点"})
    return events


# ────────────────── 二、价格行为结构（Al Brooks 三部曲） ───────────────
def price_action(bars, atrv=None):
    atrv = atrv or atr(bars)
    sw = swings(bars)
    last = bars[-1]
    out = {"trend": "unknown", "bar_type": None, "breakout": None,
           "failed_breakout": False, "measured_move": None, "score": 0, "evidence": []}

    hs = [p for (_, t, p) in sw if t == "H"][-3:]
    ls = [p for (_, t, p) in sw if t == "L"][-3:]

    if len(hs) >= 2 and len(ls) >= 2:
        hh = hs[-1] > hs[-2]
        hl = ls[-1] > ls[-2]
        if hh and hl:
            out["trend"] = "bull"
            out["score"] += 40
            out["evidence"].append("摆动高点抬高+摆动低点抬高 → 上升结构(HH+HL)")
        elif (not hh) and (not hl):
            out["trend"] = "bear"
            out["score"] -= 40
            out["evidence"].append("摆动高点降低+摆动低点降低 → 下降结构(LH+LL)")
        else:
            out["trend"] = "range"
            out["evidence"].append("高低点方向不一致 → 区间/震荡(80%时间的默认状态)")

    # 区间判定：近 BREAKOUT_LOOKBACK 根的高低幅度
    look = bars[-BREAKOUT_LOOKBACK:] if len(bars) >= BREAKOUT_LOOKBACK else bars
    hi, lo = max(b["high"] for b in look), min(b["low"] for b in look)
    mid = (hi + lo) / 2
    if mid and (hi - lo) / mid < RANGE_BAND:
        out["trend"] = "range" if out["trend"] != "unknown" else "range"
        out["evidence"].append(f"近{len(look)}根振幅{(hi-lo)/mid:.1%} < {RANGE_BAND:.0%} → 窄幅区间")

    # 棒型（Brooks：趋势棒/十字棒/反转棒）
    br = body_ratio(last)
    if br >= STRONG_BAR_BODY:
        out["bar_type"] = "trend_bar"
    elif br <= 0.25:
        out["bar_type"] = "doji_bar"
    if out["bar_type"] is None and len(bars) >= 2:
        p = bars[-2]
        if (last["low"] < p["low"] and last["close"] > p["close"] and br >= 0.5) or \
           (last["high"] > p["high"] and last["close"] < p["close"] and br >= 0.5):
            out["bar_type"] = "reversal_bar"

    # 突破 + 失败突破：收盘越过前 N 根极值
    prior = bars[-(BREAKOUT_LOOKBACK + 1):-1]
    if prior:
        ph = max(b["high"] for b in prior)
        pl = min(b["low"] for b in prior)
        if last["close"] > ph:
            out["breakout"] = "up"
            out["score"] += 25 if out["trend"] == "bull" else 10
            out["evidence"].append(f"收盘{last['close']:.2f} 突破前{len(prior)}根高点{ph:.2f}")
            out["measured_move"] = round(ph + (ph - pl), 3)  # 测量移动：区间高度上投
        elif last["close"] < pl:
            out["breakout"] = "down"
            out["score"] -= 25 if out["trend"] == "bear" else 10
            out["evidence"].append(f"收盘{last['close']:.2f} 跌破前{len(prior)}根低点{pl:.2f}")
            out["measured_move"] = round(pl - (ph - pl), 3)

        # 近 3 根内曾突破又收回 → 失败突破（Brooks 反复强调的陷阱）
        for j in range(max(0, len(bars) - 3), len(bars)):
            b = bars[j]
            if b["high"] > ph and b["close"] < ph:
                out["failed_breakout"] = True
                out["score"] -= 20
                out["evidence"].append(f"{b['date']} 突破高点后收回 → 失败突破(诱多)")
                break
            if b["low"] < pl and b["close"] > pl:
                out["failed_breakout"] = True
                out["score"] += 20
                out["evidence"].append(f"{b['date']} 跌破低点后收回 → 失败突破(诱空)")
                break

    return out


# ───────────────────────── 三、缠论 ────────────────────────────────────
def _merge_containment(bars):
    """缠论第一步：包含关系处理。返回 (合并后K线, 对应原始索引范围)。

    上升处理取高高、低高；下降处理取低低、高低（缠论原文口径）。
    """
    merged = []
    for i, b in enumerate(bars):
        cur = {"high": b["high"], "low": b["low"], "idx": [i]}
        if not merged:
            merged.append(cur)
            continue
        prev = merged[-1]
        contained = (cur["high"] <= prev["high"] and cur["low"] >= prev["low"]) or \
                    (cur["high"] >= prev["high"] and cur["low"] <= prev["low"])
        if contained:
            up = len(merged) < 2 or merged[-1]["high"] >= merged[-2]["high"]
            if up:
                prev["high"] = max(prev["high"], cur["high"])
                prev["low"] = max(prev["low"], cur["low"])
            else:
                prev["high"] = min(prev["high"], cur["high"])
                prev["low"] = min(prev["low"], cur["low"])
            prev["idx"].append(i)
        else:
            merged.append(cur)
    return merged


def chan_theory(bars, atrv=None):
    """缠论：分型 → 笔 → 中枢 → 三类买卖点 + 背驰。

    这里只做「日线级别」一种级别（本引擎没有分钟数据时的可用口径），
    多级别联立需要分钟 K 线，属后续项。
    """
    out = {"bi_count": 0, "last_bi_dir": None, "zhongshu": None,
           "buy_point": None, "divergence": None, "score": 0, "evidence": []}
    if len(bars) < 40:
        return out
    m = _merge_containment(bars)

    # 分型：合并 K 线上的顶/底（中间那根高/低点最高/最低）
    fractals = []
    for i in range(1, len(m) - 1):
        if m[i]["high"] > m[i - 1]["high"] and m[i]["high"] > m[i + 1]["high"]:
            fractals.append({"i": i, "type": "top", "px": m[i]["high"]})
        elif m[i]["low"] < m[i - 1]["low"] and m[i]["low"] < m[i + 1]["low"]:
            fractals.append({"i": i, "type": "bottom", "px": m[i]["low"]})

    # 笔：顶底交替 + 至少 CHAN_MIN_GAP 根合并K线 + 价差过一档
    bis = []
    for f in fractals:
        if not bis:
            bis.append(f)
            continue
        last = bis[-1]
        if f["type"] == last["type"]:
            # 同类分型取更极端者（顶更高/底更低）
            if (f["type"] == "top" and f["px"] > last["px"]) or \
               (f["type"] == "bottom" and f["px"] < last["px"]):
                bis[-1] = f
            continue
        if f["i"] - last["i"] >= CHAN_MIN_GAP:
            bis.append(f)
    out["bi_count"] = len(bis)
    if len(bis) < 4:
        return out
    out["last_bi_dir"] = "up" if bis[-1]["type"] == "top" else "down"

    # 中枢：连续 3 笔的重叠区间；ZG=min(高点), ZD=max(低点)
    zs = None
    for k in range(len(bis) - 3, 0, -1):
        seg = bis[k:k + 4]
        if len(seg) < 4:
            continue
        highs = [p["px"] for p in seg if p["type"] == "top"]
        lows = [p["px"] for p in seg if p["type"] == "bottom"]
        if not highs or not lows:
            continue
        zg, zd = min(highs), max(lows)
        if zg > zd:
            zs = {"zg": round(zg, 3), "zd": round(zd, 3),
                  "start": bars[m[seg[0]["i"]]["idx"][0]]["date"]}
            break
    if zs:
        out["zhongshu"] = zs
        out["evidence"].append(f"中枢 [{zs['zd']:.2f}, {zs['zg']:.2f}] 起于 {zs['start']}")
        lastpx = bars[-1]["close"]
        prev_bi = bis[-2]
        if lastpx > zs["zg"] and bis[-1]["type"] == "bottom" and prev_bi["type"] == "top":
            out["buy_point"] = "third"  # 三买：上破中枢后回抽不回中枢
            out["score"] += 30
            out["evidence"].append("三买：上破中枢后回抽未回中枢内")
        elif lastpx < zs["zd"] and bis[-1]["type"] == "top" and prev_bi["type"] == "bottom":
            out["buy_point"] = "third_sell"
            out["score"] -= 30
            out["evidence"].append("三卖：下破中枢后反抽未回中枢内")
        elif zs["zd"] <= lastpx <= zs["zg"]:
            out["evidence"].append("价格仍在中枢内 → 震荡处理，等三类买卖点")

    # 背驰：同向两段（最近一段 vs 前一同向段）力度比较，价格更长但量能更小 → 背驰
    ups = [b for b in bis if b["type"] == "top"]
    downs = [b for b in bis if b["type"] == "bottom"]
    if len(ups) >= 2 and len(bis) >= 5:
        a, b = ups[-2], ups[-1]
        amp_a = abs(a["px"] - downs[-3]["px"]) if len(downs) >= 3 else None
        amp_b = abs(b["px"] - downs[-1]["px"]) if downs else None
        if amp_a and amp_b and amp_b > amp_a * 1.05:
            out["divergence"] = "top_weak"
            out["score"] -= 15
            out["evidence"].append("上涨段幅度放大但（后续需量能确认）——顶部背驰观察位")
        if amp_a and amp_b and amp_b < amp_a * 0.7:
            out["divergence"] = "bottom_strong"
            out["score"] += 10
            out["evidence"].append("下跌段幅度收敛 → 空头衰竭")
    return out


# ─────────────────────── 四、艾略特波浪（启发式） ─────────────────────
def elliott(bars):
    """在摆动序列上找 5 浪推动 / 3 浪调整；带三条硬规则校验，只给近似结论。"""
    out = {"wave": None, "confidence": 0.0, "invalidation": None, "score": 0, "evidence": []}
    sw = swings(bars, k=2)
    if len(sw) < 6:
        return out
    pts = sw[-8:]
    # 需要 H,L,H,L,H 交替（从低点起算的推动）
    seq = [p for p in pts]
    types = "".join(p[1] for p in seq)
    if "LHLHLH" in types:
        k = types.index("LHLHLH")
        w = seq[k:k + 6]
        p0, p1, p2, p3, p4, p5 = [x[2] for x in w]
        w1, w3, w5 = p1 - p0, p3 - p2, p5 - p4
        r2 = (p1 - p2) / w1 if w1 else 0
        r4 = (p3 - p4) / w3 if w3 else 0
        ok = (p2 > p0) and (w3 >= min(w1, w5)) and (p4 > p1)
        if ok:
            out["wave"] = "impulse_up_w5"
            out["confidence"] = round(min(0.8, 0.35 + 0.15 * sum([0.3 <= r2 <= 0.75, 0.3 <= r4 <= 0.6]),), 2)
            out["invalidation"] = round(p0, 3)
            out["score"] += int(20 * out["confidence"] / 0.8)
            out["evidence"].append(
                f"上升5浪雏形: 2浪回撤{r2:.0%}/4浪回撤{r4:.0%}, 3浪非最短, 4浪未破1浪顶；失效位{p0:.2f}")
    if "HLHLHL" in types:
        k = types.index("HLHLHL")
        w = seq[k:k + 6]
        p0, p1, p2, p3, p4, p5 = [x[2] for x in w]
        w1, w3 = p0 - p1, p2 - p3
        r2 = (p2 - p1) / w1 if w1 else 0
        if p2 < p0 and w3 >= 0 and p4 < p1:
            out["wave"] = "impulse_down_w5"
            out["confidence"] = round(min(0.7, 0.3 + 0.2 * (0.3 <= r2 <= 0.75)), 2)
            out["invalidation"] = round(p0, 3)
            out["score"] -= int(20 * out["confidence"] / 0.7)
            out["evidence"].append(f"下降5浪雏形: 2浪反抽{r2:.0%}；失效位{p0:.2f}")
    return out


# ────────────────── 五、量价 / 威科夫阶段 ──────────────────────────────
def volume_price(bars):
    """量价关系 + VSA 式事件 + 威科夫阶段提示。

    核心判据来自「努力与结果」：成交量是努力，价格位移是结果，二者背离就是
    供给/需求异常（威科夫、量价操盘法共同的落点）。
    """
    out = {"vr": None, "quadrant": None, "phase": "unknown", "events": [],
           "score": 0, "evidence": []}
    if len(bars) < 30:
        return out
    vm = vol_ma(bars)
    last, prev = bars[-1], bars[-2]
    vr = last["volume"] / (vm[-1] or 1e-9)
    out["vr"] = round(vr, 2)
    chg = _pct(last["close"], prev["close"])
    up = chg > 0
    if up and vr >= 1.2:
        out["quadrant"] = "价升量增"
        out["score"] += 15
        out["evidence"].append(f"价升{chg:+.1%}+量比{vr:.2f} → 需求跟进")
    elif up and vr < 0.8:
        out["quadrant"] = "价升量减"
        out["score"] -= 5
        out["evidence"].append(f"价升{chg:+.1%}+量比{vr:.2f} → 无量上涨，持续性存疑")
    elif (not up) and vr >= 1.2:
        out["quadrant"] = "价跌量增"
        out["score"] -= 15
        out["evidence"].append(f"价跌{chg:+.1%}+量比{vr:.2f} → 供给涌出")
    else:
        out["quadrant"] = "价跌量减"
        out["evidence"].append(f"价跌{chg:+.1%}+量比{vr:.2f} → 抛压衰减")

    atrv = atr(bars)
    # 无需求：上涨 K 线但量能低于前两根，收盘落在中部（VSA no-demand）
    if up and last["volume"] < min(prev["volume"], bars[-3]["volume"]) and body_ratio(last) < 0.5:
        out["events"].append({"kind": "no_demand", "dir": "bear", "src": "威科夫/无需求"})
        out["score"] -= 10
    # 上冲回落：创近 10 根新高但收在下 1/3，且放量
    h10 = max(b["high"] for b in bars[-10:])
    if last["high"] >= h10 and (last["close"] - last["low"]) / rng(last) < 0.34 and vr >= 1.2:
        out["events"].append({"kind": "upthrust", "dir": "bear", "src": "威科夫/上冲回落"})
        out["score"] -= 20
    # 停止量：放量下跌但收盘远离最低（有人在接）
    if (not up) and vr >= 1.6 and (last["close"] - last["low"]) / rng(last) > 0.6:
        out["events"].append({"kind": "stopping_volume", "dir": "bull", "src": "威科夫/停止量"})
        out["score"] += 20
    # 努力与结果背离：巨量但几乎没位移
    if vr >= 1.8 and abs(chg) < 0.005 and rng(last) < 1.2 * atrv[-1]:
        out["events"].append({"kind": "effort_no_result", "dir": "neutral", "src": "威科夫/努力无结果"})
        out["evidence"].append("巨量但价格几乎不动 → 有人在吸/派，方向待定")

    # 阶段提示：近 30 根窄幅 + 量能结构
    look = bars[-30:]
    hi, lo = max(b["high"] for b in look), min(b["low"] for b in look)
    mid = (hi + lo) / 2 or 1e-9
    in_range = (hi - lo) / mid < 0.12
    if in_range:
        first_half = sum(b["volume"] for b in look[:15]) / 15
        second_half = sum(b["volume"] for b in look[15:]) / 15
        near_high = (bars[-1]["close"] - lo) / (hi - lo)
        if second_half < first_half * 0.85 and near_high > 0.6:
            out["phase"] = "accumulation"
            out["score"] += 10
            out["evidence"].append("区间内量能收敛且价格抬向区间上沿 → 吸筹特征")
        elif second_half > first_half * 1.15 and near_high < 0.4:
            out["phase"] = "distribution"
            out["score"] -= 10
            out["evidence"].append("区间内放量下移 → 派发特征")
    else:
        out["phase"] = "markup" if bars[-1]["close"] > look[0]["close"] else "markdown"
    return out


# ───────────────────────── 六、合成 ────────────────────────────────────
def analyze(bars, code=None):
    if len(bars) < 60:
        return None
    atrv = atr(bars)
    cand = candle_patterns(bars, atrv)
    pa = price_action(bars, atrv)
    chan = chan_theory(bars, atrv)
    ew = elliott(bars)
    vp = volume_price(bars)

    score = pa["score"] + chan["score"] + ew["score"] + vp["score"]
    for e in cand:
        score += int(12 * e["strength"]) * (1 if e["dir"] == "bull" else -1 if e["dir"] == "bear" else 0)
    score = max(-100, min(100, score))

    tags = []
    if pa["trend"] != "unknown":
        tags.append(f"trend:{pa['trend']}")
    if pa["breakout"]:
        tags.append(f"breakout:{pa['breakout']}")
    if pa["failed_breakout"]:
        tags.append("failed_breakout")
    if chan["buy_point"]:
        tags.append(f"chan:{chan['buy_point']}")
    if ew["wave"]:
        tags.append(f"ew:{ew['wave']}")
    if vp["phase"] != "unknown":
        tags.append(f"phase:{vp['phase']}")
    tags += [f"candle:{e['kind']}" for e in cand]

    return {
        "code": code,
        "date": bars[-1]["date"],
        "close": round(bars[-1]["close"], 3),
        "score": score,
        "bias": "bull" if score >= 20 else "bear" if score <= -20 else "neutral",
        "tags": tags,
        "candles": cand,
        "price_action": pa,
        "chan": chan,
        "elliott": ew,
        "volume_price": vp,
    }


def run_all(codes=None, out_path=None):
    import time as _t
    codes = codes or list_codes()
    t0 = _t.time()
    barset = load_all(codes)
    print(f"取数 {len(barset)}/{len(codes)} 只 · {_t.time()-t0:.1f}s", file=sys.stderr)
    rows, skipped = [], 0
    for code, bars in barset.items():
        try:
            r = analyze(bars, code)
            if r:
                rows.append(r)
            else:
                skipped += 1
        except Exception as e:  # 单只失败不影响整体
            skipped += 1
            print(f"skip {code}: {str(e)[:60]}", file=sys.stderr)
    rows.sort(key=lambda x: -x["score"])
    doc = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "structure_engine/v1",
        "books": ["日本蜡烛图技术", "高级趋势/波段/反转技术分析", "零基础看懂缠论",
                  "艾略特波浪理论", "量价操盘法", "威科夫操盘法"],
        "count": len(rows),
        "skipped": skipped,
        "items": rows,
    }
    out_path = out_path or os.path.join(DATA_DIR, "structure_signals.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    json.dump(doc, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return doc


def validate(sample=300, horizon=5, combos=True):
    """逐信号前向收益检验。委托 signal_validator 统一实现。

    以前各引擎各写一套统计，阈值与口径迟早分叉。现在检验逻辑只有一份，
    这里只负责回答「结构层要验哪些标签」。
    """
    codes = list_codes()[:sample]
    samples = [(c, load_bars(c, limit=400)) for c in codes]
    samples = [(c, b) for c, b in samples if len(b) >= 120]

    def tag_fn(view):
        r = analyze(view, "x")
        if not r:
            return []
        tags = [r["bias"]] + [t for t in r["tags"] if t.startswith(
            ("chan:", "ew:", "phase:", "breakout", "failed", "candle:"))]
        return list(dict.fromkeys(tags))

    rows = SV.validate(tag_fn, samples, horizon=horizon, min_history=80,
                       cut_step=1 if combos else 3, combo=combos)
    # CLI 与既有调用方用 avg_fwd_5d 这个键名，保留兼容
    for r in rows:
        r["avg_fwd_5d"] = r.pop("avg_fwd")
    return rows

BULLISH_TAGS = ("bull", "breakout:up", "chan:third", "phase:accumulation", "phase:markup",
                "ew:impulse_up_w5", "candle:hammer", "candle:bullish_engulfing")


def _is_bullish_key(key):
    """组合键（如 "breakout:up & phase:markup"）逐个 token 精确比对。

    必须按 token 而不是子串：「chan:third_sell」包含子串「chan:third」，
    用子串判断会把三卖错标成多头，方向直接反了。
    """
    toks = [t.strip() for t in key.split("&")]
    return any(t in BULLISH_TAGS for t in toks)


def calibrate(sample=120, horizon=5, min_n=120):
    """把逐标签的实测前向收益固定成校准表，供下游按实测权重使用。

    为什么要这一步：书上的判据方向在 A 股 5 日尺度上会翻车（突破追高跑输、破位反弹），
    如果下游硬编码先验方向，就是把书本教条直接接进交易。校准表让权重来自数据，
    且带 n 与 t 让人知道这个标签到底可不可信。
    """
    rows = validate(sample, horizon)
    table = {}
    for r in rows:
        if r["n"] < min_n:
            continue
        bull = _is_bullish_key(r["signal"])
        same_dir = r["avg_fwd_5d"] > 0 if bull else r["avg_fwd_5d"] < 0
        table[r["signal"]] = {
            "n": r["n"], "avg_fwd_5d": r["avg_fwd_5d"],
            "win_rate": r["win_rate"], "t": r["t"],
            "edge": "trend" if same_dir else "contrarian",
        }
    return table


def write_calibration(sample=120, out_path=None):
    table = calibrate(sample)
    doc = {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "engine": "structure_engine/calibration",
           "horizon_days": 5, "sample_codes": sample,
           "note": "edge=contrarian 表示该标签在 A 股 5 日尺度上方向与书本相反",
           "table": table}
    out_path = out_path or os.path.join(DATA_DIR, "structure_calibration.json")
    json.dump(doc, open(out_path + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(out_path + ".tmp", out_path)
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全市场生成 structure_signals.json")
    ap.add_argument("--code", help="单只调试")
    ap.add_argument("--validate", action="store_true", help="逐信号前向收益检验")
    ap.add_argument("--calibrate", action="store_true", help="生成 data/structure_calibration.json")
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.validate:
        rows = validate(a.sample)
        print(f"{'signal':30} {'n':>6} {'fwd5d%':>7} {'win%':>6} {'t':>6}")
        for r in rows:
            print(f"{r['signal']:30} {r['n']:>6} {r['avg_fwd_5d']:>7.2f} {r['win_rate']:>6.1f} {r['t']:>6.2f}")
        return

    if a.calibrate:
        doc = write_calibration(a.sample)
        t = doc["table"]
        print(f"校准表写入 data/structure_calibration.json，共 {len(t)} 个标签")
        for k, v in sorted(t.items(), key=lambda kv: -abs(kv[1]["t"]))[:10]:
            print(f"  {k:30} n={v['n']:>5} fwd5d={v['avg_fwd_5d']:>6.2f}% t={v['t']:>5.2f} {v['edge']}")
        return

    if a.code:
        bars = load_bars(a.code)
        r = analyze(bars, a.code)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return

    doc = run_all()
    print(f"已写出 {doc['count']} 只 → {os.path.join(DATA_DIR,'structure_signals.json')}"
          f"（跳过 {doc['skipped']}）")
    if a.verbose:
        for it in doc["items"][:8]:
            print(f"  {it['code']} score={it['score']:>4} {it['bias']:6} {' '.join(it['tags'][:5])}")


if __name__ == "__main__":
    main()
