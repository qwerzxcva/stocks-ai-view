#!/usr/bin/env python3
"""
indicators_ext.py — 扩展技术指标库 (移植自 StockSharp/StockSharp, C# → Python)

来源: github.com/StockSharp/StockSharp  Algo.Indicators (10871 star, 190+ 指标)
移植原则: 保持原算法口径不变, 输入统一为 OHLCV 列表, 输出等长序列 (前 N 位为 None)。

已移植:
  ATR            平均真实波幅 (AverageTrueRange)
  SuperTrend     超级趋势 (含趋势方向)
  Keltner        肯特纳通道
  Donchian       唐奇安通道
  ZigZag         之字转向 (去噪)
  CMO            Chande 动量振荡器
  ChoppinessIndex 震荡指数 (趋势/区间判别)
  Vortex         涡旋指标
  UltimateOsc    终极振荡器
  MassIndex      质量指数
  PivotPoints    枢轴点 (支撑/阻力)
  HullMA         Hull 移动平均线
  VWMA          成交量加权移动平均
  ElderForce     Elder 力量指数
  ConnorsRSI     Connors RSI (多周期合成)
  AccumulationDistributionLine  累积/派发线

用法:
  python3 indicators_ext.py --demo      # 内置演示 (正弦+噪声)
  from indicators_ext import supertrend, atr, keltner
"""
import argparse
import json
import math
import sys


# ─── 基础工具 ─────────────────────────────────────────────────────
def _n(values):
    return [None] * len(values)


def sma(values, n):
    """简单移动平均, 前 n-1 位 None。"""
    out = _n(values)
    if len(values) < n:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema(values, n):
    """指数移动平均, 用前 n 位均值做种子 (与原版一致)。"""
    out = _n(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def _true_range(high, low, close):
    """真实波幅序列 (首位置 None)。"""
    out = [None]
    for i in range(1, len(close)):
        tr = max(high[i] - low[i],
                 abs(high[i] - close[i - 1]),
                 abs(low[i] - close[i - 1]))
        out.append(tr)
    return out


# ─── ATR ──────────────────────────────────────────────────────────
def atr(high, low, close, n=14):
    """平均真实波幅 (StockSharp AverageTrueRange: Wilder 平滑)。"""
    tr = _true_range(high, low, close)
    out = _n(close)
    if len(close) < n + 1:
        return out
    # Wilder: 首个 ATR = 前 n 个 TR 的均值
    seed = [t for t in tr[1:n + 1] if t is not None]
    if len(seed) < n:
        return out
    prev = sum(seed) / n
    out[n] = prev
    for i in range(n + 1, len(close)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


# ─── SuperTrend ───────────────────────────────────────────────────
def supertrend(high, low, close, n=10, multiplier=3.0):
    """超级趋势指标。

    返回 (st, trend): st 为趋势线值, trend ∈ {1 多头, -1 空头}。
    移植自 StockSharp SuperTrend.cs (含 finalUpper/finalLower band 记忆逻辑)。
    """
    st, trend = _n(close), [0] * len(close)
    a = atr(high, low, close, n)
    prev_upper = prev_lower = prev_st = None
    prev_close = None
    prev_trend = 0

    for i in range(len(close)):
        if a[i] is None:
            continue
        hl2 = (high[i] + low[i]) / 2
        bu, bl = hl2 + multiplier * a[i], hl2 - multiplier * a[i]

        fu = bu if (prev_upper is None or bu < prev_upper or (prev_close is not None and prev_close > prev_upper)) else prev_upper
        fl = bl if (prev_lower is None or bl > prev_lower or (prev_close is not None and prev_close < prev_lower)) else prev_lower

        if prev_st is None:
            st[i] = fl if close[i] >= hl2 else fu
            trend[i] = 1 if close[i] >= hl2 else -1
        else:
            if prev_trend == 1:
                st[i] = fl
                trend[i] = -1 if close[i] < fl else 1
            else:
                st[i] = fu
                trend[i] = 1 if close[i] > fu else -1
        prev_upper, prev_lower, prev_st, prev_close, prev_trend = fu, fl, st[i], close[i], trend[i]
    return st, trend


# ─── Keltner Channels ─────────────────────────────────────────────
def keltner(high, low, close, ema_n=20, atr_n=10, k=2.0):
    """肯特纳通道: 中轨=EMA, 上下轨=EMA ± k*ATR。"""
    mid = ema(close, ema_n)
    a = atr(high, low, close, atr_n)
    up, lo = _n(close), _n(close)
    for i in range(len(close)):
        if mid[i] is None or a[i] is None:
            continue
        up[i] = mid[i] + k * a[i]
        lo[i] = mid[i] - k * a[i]
    return mid, up, lo


# ─── Donchian Channels ────────────────────────────────────────────
def donchian(high, low, n=20):
    """唐奇安通道: 上轨=N日最高, 下轨=N日最低, 中轨=均值。"""
    up, lo, mid = _n(high), _n(low), _n(high)
    for i in range(n - 1, len(high)):
        h = max(high[i - n + 1:i + 1])
        l = min(low[i - n + 1:i + 1])
        up[i], lo[i] = h, l
        mid[i] = (h + l) / 2
    return up, mid, lo


# ─── ZigZag ───────────────────────────────────────────────────────
def zigzag(high, low, pct=5.0):
    """之字转向: 只保留反转超过 pct% 的拐点。

    返回 (is_pivot, pivot_type): pivot_type ∈ {1 顶, -1 底, 0 无}。
    实现: 状态机, 交替寻找「更高的顶」与「更低的底」, 反向波动超过 pct% 时
    把上一极值点标记为拐点并切换状态。
    """
    is_p, ptype = [0] * len(high), [0] * len(high)
    if len(high) < 3:
        return is_p, ptype

    # 初始化: 以第一个有效区间决定起始方向
    state = 1                # 1=当前在找顶部(记录最高), -1=找底部(记录最低)
    ext_i, ext_v = 0, high[0]

    for i in range(1, len(high)):
        if state == 1:
            # 寻找更高的高点
            if high[i] >= ext_v:
                ext_v, ext_i = high[i], i
            # 从最高点回撤超过 pct% → 该最高点是顶部拐点
            if ext_v > 0 and (ext_v - low[i]) / ext_v * 100 >= pct:
                is_p[ext_i], ptype[ext_i] = 1, 1
                state = -1
                ext_v, ext_i = low[i], i
        else:
            # 寻找更低的低点
            if low[i] <= ext_v or ext_v <= 0:
                ext_v, ext_i = low[i], i
            # 从最低点反弹超过 pct% → 该最低点是底部拐点
            if ext_v > 0 and (high[i] - ext_v) / ext_v * 100 >= pct:
                is_p[ext_i], ptype[ext_i] = 1, -1
                state = 1
                ext_v, ext_i = high[i], i
    return is_p, ptype


# ─── CMO (Chande Momentum Oscillator) ─────────────────────────────
def cmo(close, n=14):
    """Chande 动量振荡器: (涨幅和-跌幅和)/(涨幅和+跌幅和) × 100"""
    out = _n(close)
    if len(close) < n + 1:
        return out
    for i in range(n, len(close)):
        su = sd = 0.0
        for j in range(i - n + 1, i + 1):
            d = close[j] - close[j - 1]
            if d > 0:
                su += d
            else:
                sd -= d
        tot = su + sd
        out[i] = round(100 * (su - sd) / tot, 2) if tot else 0.0
    return out


# ─── Choppiness Index ─────────────────────────────────────────────
def choppiness(high, low, close, n=14):
    """震荡指数: 100*log10(ΣTR / (maxH-minL)) / log10(n)

    高值 (>61.8) = 震荡/无趋势; 低值 (<38.2) = 强趋势。
    """
    out = _n(close)
    tr = _true_range(high, low, close)
    for i in range(n, len(close)):
        seg = tr[i - n + 1:i + 1]
        if any(t is None for t in seg):
            continue
        sum_tr = sum(seg)
        hh = max(high[i - n + 1:i + 1])
        ll = min(low[i - n + 1:i + 1])
        rng = hh - ll
        if rng <= 0 or sum_tr <= 0:
            continue
        out[i] = round(100 * math.log10(sum_tr / rng) / math.log10(n), 2)
    return out


# ─── Vortex Indicator ─────────────────────────────────────────────
def vortex(high, low, close, n=14):
    """涡旋指标: VI+ 与 VI-, VI+>VI- 为上升趋势。"""
    tr = _true_range(high, low, close)
    vmp, vmm = _n(close), _n(close)
    vip, vim = _n(close), _n(close)
    for i in range(n, len(close)):
        svmp = svmm = str_ = 0.0
        for j in range(i - n + 1, i + 1):
            vm = abs(high[j] - low[j - 1])
            vmmv = abs(low[j] - high[j - 1])
            svmp += vm
            svmm += vmmv
            str_ += tr[j] or 0
        if str_ <= 0:
            continue
        vmp[i], vmm[i] = svmp / str_, svmm / str_
        vip[i] = round(svmp / str_, 4)
        vim[i] = round(svmm / str_, 4)
    return vip, vim


# ─── Ultimate Oscillator ──────────────────────────────────────────
def ultimate_osc(high, low, close, n1=7, n2=14, n3=28):
    """终极振荡器: 三周期 bp/tr 加权平均 (Williams, 4:2:1)。"""
    out = _n(close)
    tr = _true_range(high, low, close)
    bp = [None] * len(close)
    for i in range(1, len(close)):
        tl = min(low[i], close[i - 1])
        bp[i] = close[i] - tl
    for i in range(n3, len(close)):
        if tr[i] is None or bp[i] is None:
            continue
        # 停牌/一字板日 high==low==prev_close, tr 和可为 0; 分母为 0 时
        # 跳过该点而不是抛异常终止整只股票 (2026-10-19 全市场实测)。
        d1 = sum(tr[j] for j in range(i - n1 + 1, i + 1))
        d2 = sum(tr[j] for j in range(i - n2 + 1, i + 1))
        d3 = sum(tr[j] for j in range(i - n3 + 1, i + 1))
        if not d1 or not d2 or not d3:
            continue
        s1 = sum(bp[j] for j in range(i - n1 + 1, i + 1)) / d1
        s2 = sum(bp[j] for j in range(i - n2 + 1, i + 1)) / d2
        s3 = sum(bp[j] for j in range(i - n3 + 1, i + 1)) / d3
        out[i] = round(100 * (4 * s1 + 2 * s2 + s3) / 7, 2)
    return out


# ─── Mass Index ───────────────────────────────────────────────────
def mass_index(high, low, n=9, n2=25):
    """质量指数: Σ_{n2}( EMA_n(HL) / EMA_n(EMA_n(HL)) )

    MassIndex 正常范围 ~0~30+, >27 预示反转 (Bulkowski)。
    实现要点: 两级 EMA 会缩短序列, 比值序列需与 OHLC 长度对齐
    (比值序列第 i 位对应原序列第 i+offset 根), 最后对 n2 项求和。
    """
    hl = [(h + l) / 2 for h, l in zip(high, low)]
    e1 = ema(hl, n)
    # 去掉 None 后做第二级 EMA; e1 前 (n-1) 位为 None
    e1_valid = [x for x in e1 if x is not None]      # 长度 = len(hl) - (n-1)
    e2 = ema(e1_valid, n)                            # 长度 = len(hl) - 2(n-1)

    # e2[j] 由 e1_valid[j + (n-1)] 推出 → 比值序列同长于 e2
    ratio = []
    for j in range(len(e2)):
        if e2[j] is None or e2[j] == 0:
            ratio.append(None)
            continue
        k = j + (n - 1)
        ratio.append(e1_valid[k] / e2[j] if k < len(e1_valid) else None)

    out = _n(high)
    for j in range(n2 - 1, len(ratio)):
        seg = ratio[j - n2 + 1:j + 1]
        if any(x is None for x in seg):
            continue
        out[j + 2 * (n - 1)] = round(sum(seg), 2)
    return out


# ─── Pivot Points ─────────────────────────────────────────────────
def pivot_points(high, low, close):
    """枢轴点 (经典/Floor): PP=(H+L+C)/3, R1/S1/R2/S2/R3/S3。"""
    out = []
    for i in range(len(close)):
        pp = (high[i] + low[i] + close[i]) / 3
        r1 = 2 * pp - low[i]
        s1 = 2 * pp - high[i]
        r2 = pp + (high[i] - low[i])
        s2 = pp - (high[i] - low[i])
        r3 = high[i] + 2 * (pp - low[i])
        s3 = low[i] - 2 * (high[i] - pp)
        out.append({"pp": round(pp, 2), "r1": round(r1, 2), "s1": round(s1, 2),
                    "r2": round(r2, 2), "s2": round(s2, 2),
                    "r3": round(r3, 2), "s3": round(s3, 2)})
    return out


# ─── Hull MA ──────────────────────────────────────────────────────
def hull_ma(values, n=9):
    """Hull 移动平均: WMA(2*WMA(x, n/2) - WMA(x, n), sqrt(n))"""
    def wma(vals, period):
        out = _n(vals)
        for i in range(period - 1, len(vals)):
            s = wsum = 0.0
            for k in range(period):
                wgt = period - k
                s += vals[i - period + 1 + k] * wgt
                wsum += wgt
            out[i] = s / wsum if wsum else None
        return out

    half = max(1, n // 2)
    a = wma(values, half)
    b = wma(values, n)
    raw = [(2 * a[i] - b[i]) if (a[i] is not None and b[i] is not None) else None
           for i in range(len(values))]
    rawc = [x for x in raw if x is not None]
    res = wma(rawc, int(math.sqrt(n)))
    # 对齐回原长度
    off = len(raw) - len(rawc)
    out = _n(values)
    for i, v in enumerate(res):
        out[i + off] = v
    return out


# ─── VWMA ─────────────────────────────────────────────────────────
def vwma(close, volume, n=20):
    """成交量加权移动平均。"""
    out = _n(close)
    for i in range(n - 1, len(close)):
        sv = sum(volume[i - n + 1:i + 1])
        if sv <= 0:
            continue
        out[i] = sum(close[i - n + 1:i + 1][k] * volume[i - n + 1 + k]
                     for k in range(n)) / sv
    return out


# ─── Elder Force Index ────────────────────────────────────────────
def elder_force(close, volume, n=13):
    """Elder 力量指数: EMA( (close-prev_close)*volume )"""
    ef = [None]
    for i in range(1, len(close)):
        ef.append((close[i] - close[i - 1]) * volume[i])
    efc = [x for x in ef if x is not None]
    r = ema(efc, n)
    off = len(ef) - len(efc)
    out = _n(close)
    for i, v in enumerate(r):
        out[i + off] = v
    return out


# ─── Connors RSI ──────────────────────────────────────────────────
def connors_rsi(close, rsi_n=3, streak_n=2, rank_n=100):
    """Connors RSI: (RSI(streak, streak_n) + RSI(close, rsi_n) + ROC rank) / 3

    streak: 连涨/连跌天数序列。
    rank: 当日涨幅在过去 rank_n 日的百分位。
    """
    def rsi_of(vals, period):
        out = _n(vals)
        if len(vals) < period + 1:
            return out
        g = l = 0.0
        for i in range(1, period + 1):
            d = vals[i] - vals[i - 1]
            if d > 0:
                g += d
            else:
                l -= d
        ag, al = g / period, l / period
        out[period] = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)
        for i in range(period + 1, len(vals)):
            d = vals[i] - vals[i - 1]
            ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
            al = (al * (period - 1) + (-d if d < 0 else 0)) / period
            out[i] = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)
        return out

    rsi_c = rsi_of(close, rsi_n)
    # streak
    streak = [0] * len(close)
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            streak[i] = streak[i - 1] + 1 if streak[i - 1] >= 0 else 1
        elif close[i] < close[i - 1]:
            streak[i] = streak[i - 1] - 1 if streak[i - 1] <= 0 else -1
        else:
            streak[i] = 0
    rsi_s = rsi_of(streak, streak_n)
    # rank
    out = _n(close)
    for i in range(rank_n, len(close)):
        roc = (close[i] - close[i - 1]) / close[i - 1] if close[i - 1] else 0
        hist = [(close[j] - close[j - 1]) / close[j - 1] for j in range(i - rank_n + 1, i + 1)]
        rank = 100.0 * sum(1 for h in hist if h < roc) / len(hist)
        parts = [x for x in (rsi_s[i], rsi_c[i], rank) if x is not None]
        if len(parts) == 3:
            out[i] = round(sum(parts) / 3, 2)
    return out


# ─── Accumulation/Distribution Line ──────────────────────────────
def _rsi(closes, period=14):
    """标准 RSI (Wilder 平滑), 返回与输入等长的序列 (前 period 位 None)。"""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0)) / period
        out[i] = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)
    return out


def ad_line(high, low, close, volume):
    """累积/派发线 (A/D Line): Σ(CLV × volume), CLV=((C-L)-(H-C))/(H-L)"""
    out = [None] * len(close)
    cum = 0.0
    for i in range(len(close)):
        rng = high[i] - low[i]
        clv = ((close[i] - low[i]) - (high[i] - close[i])) / rng if rng else 0
        cum += clv * volume[i]
        out[i] = round(cum, 2)
    return out


# ═══ 借鉴 kand-ta/kand (Apache-2.0 OR MIT) 的特色指标 ══════════════
# kand 是 Rust 写的技术指标库, 除 TA-Lib 标准指标外还实现了几个进阶指标。
# 这里按其公开文档的**定义与参数**独立实现 (Apache-2.0/MIT 允许借鉴),
# 不复用其 Rust 代码 (我们是纯 Python, 且不想引入二进制依赖)。

# ─── Vegas Tunnel (维加斯隧道) ─────────────────────────────────────
def vegas_tunnel(close, ema_n=144, tunnel_n=169):
    """Vegas Tunnel = EMA(144) 与其上下各 4% 的包络, 构成「隧道」。

    用法 (维加斯四要素):
      价格在隧道上方 → 多头趋势
      价格在隧道下方 → 空头趋势
      价格在隧道内     → 震荡, 无方向
    判断依据是「是否突破整条隧道」而非单条 EMA, 因此比单 EMA 少假信号。

    返回 (ema_line, upper, lower, position) — position: above/inside/below
    数据不足 (少于 ema_n) 时返回全 None。
    """
    n = len(close)
    if n < ema_n:
        return None, None, None, None
    e = ema(close, ema_n)
    ema_now = e[-1]
    if ema_now is None or ema_now <= 0:
        return None, None, None, None
    upper = ema_now * 1.04        # 隧道上下沿 = EMA ± 4%
    lower = ema_now * 0.96
    c = close[-1]
    pos = "above" if c > upper else ("below" if c < lower else "inside")
    return round(ema_now, 3), round(upper, 3), round(lower, 3), pos


# ─── Camarilla Levels (卡玛拉枢纽位) ──────────────────────────────
def camarilla_levels(high, low, close):
    """Camarilla 水平位 (日内支撑/阻力)。

    基于前一日 H/L/C 计算 8 个价位: H1-H5 (上方阻力) 与 L1-L5 (下方支撑)。
    特点: H4/L4 是「强反转位」, H3/L3 是「突破确认位」。
    乘数取自 Camarilla 原始定义 (1.0834/1.0583 等)。
    """
    if not (high and low and close):
        return None
    h, l, c = high[-1], low[-1], close[-1]
    rng = h - l
    if rng <= 0:
        return None
    h5 = (h / l) * c if l > 0 else c + rng * 2
    return {
        "H5": round(h5, 3),
        "H4": round(c + rng * 1.1 / 2, 3),
        "H3": round(c + rng * 1.1 / 4, 3),
        "H2": round(c + rng * 1.1 / 6, 3),
        "H1": round(c + rng * 1.1 / 12, 3),
        "L1": round(c - rng * 1.1 / 12, 3),
        "L2": round(c - rng * 1.1 / 6, 3),
        "L3": round(c - rng * 1.1 / 4, 3),
        "L4": round(c - rng * 1.1 / 2, 3),
        "L5": round(c - (h5 - c), 3),
        "pivot": round((h + l + c) / 3, 3),
        "range": round(rng, 3),
        "basis": "Camarilla (Nick Stott 1989): R/S = C ± (H-L)×1.1/{12,6,4,2}; "
                 "H5=(H/L)×C, L5=C-(H5-C); H4/L4=突破位, H3/L3=反转位",
    }


# ─── Heikin Ashi (平均K线/因果K线) ───────────────────────────────
def heikin_ashi(open_, high, low, close):
    """平均K线 (Heikin Ashi)。

    HA 是把普通 K 线做平滑后的「另一套价格」:
      HA_Close = (O + H + L + C) / 4
      HA_Open  = (上一根 HA_Open + 上一根 HA_Close) / 2  (首根用 (O+C)/2)
      HA_High  = max(H, HA_Open, HA_Close)
      HA_Low   = min(L, HA_Open, HA_Close)
    连续同色 HA (无下影的阳线 / 无上影的阴线) 表示趋势强。

    返回 (ha_open, ha_high, ha_low, ha_close) 四个等长列表。
    """
    n = len(close)
    if n == 0:
        return [], [], [], []
    ha_o, ha_h, ha_l, ha_c = [], [], [], []
    for i in range(n):
        c = (open_[i] + high[i] + low[i] + close[i]) / 4
        o = (open_[i] + close[i]) / 2 if i == 0 else (ha_o[-1] + ha_c[-1]) / 2
        ha_o.append(o)
        ha_c.append(c)
        ha_h.append(max(high[i], o, c))
        ha_l.append(min(low[i], o, c))
    return ha_o, ha_h, ha_l, ha_c


# ─── MFI (Money Flow Index 资金流量指标) ──────────────────────────
def mfi(high, low, close, volume, n=14):
    """MFI = 量加权版 RSI (又称「成交量 RSI」)。

    TP = (H + L + C) / 3
    量能流 = TP × volume
    MFI = 100 - 100/(1 + 正流量和/负流量和)
    与 RSI 的差别: 考虑了成交量, 因此「缩量上涨」在 MFI 上不会虚高。
    """
    out = []
    for i in range(len(close)):
        if i < n:
            out.append(None)
            continue
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            tp = (high[j] + low[j] + close[j]) / 3
            tp_prev = (high[j - 1] + low[j - 1] + close[j - 1]) / 3 if j > 0 else tp
            flow = tp * (volume[j] or 0)
            if tp > tp_prev:
                pos += flow
            elif tp < tp_prev:
                neg += flow
        out.append(round(100 - 100 / (1 + pos / neg), 2) if neg > 0 else
                   (100.0 if pos > 0 else None))
    return out


# ─── OBV (On Balance Volume 能量潮) ───────────────────────────────
def obv(close, volume):
    """OBV = 累加「带方向的成交量」。

    收盘涨 → +volume; 收盘跌 → -volume; 平 → 0。
    用途: 价涨 OBV 也涨 = 量价配合; 价涨 OBV 跌 = 量价背离 (上涨无量)。
    """
    out = []
    cum = 0.0
    for i in range(len(close)):
        if i > 0:
            if close[i] > close[i - 1]:
                cum += volume[i] or 0
            elif close[i] < close[i - 1]:
                cum -= volume[i] or 0
        out.append(round(cum, 2))
    return out


# ─── demo ─────────────────────────────────────────────────────────
def _demo():
    import random
    random.seed(7)
    n = 120
    base, close = 100.0, []
    for i in range(n):
        base += math.sin(i / 9.0) * 1.6 + random.uniform(-1.2, 1.2)
        close.append(round(base, 2))
    high = [round(c + random.uniform(0.4, 2.0), 2) for c in close]
    low = [round(c - random.uniform(0.4, 2.0), 2) for c in close]
    vol = [random.randint(80000, 400000) for _ in range(n)]

    print("=== indicators_ext 演示 (正弦+噪声, 120根) ===\n")
    st, tr = supertrend(high, low, close)
    print(f"SuperTrend   最后值={st[-1]}  趋势={'多头' if tr[-1]==1 else '空头'}")
    mid, up, lo = keltner(high, low, close)
    print(f"Keltner      中轨={mid[-1]}  上轨={up[-1]}  下轨={lo[-1]}")
    du, dm, dl = donchian(high, low)
    print(f"Donchian     上轨={du[-1]}  中轨={dm[-1]}  下轨={dl[-1]}")
    print(f"ATR(14)      ={atr(high, low, close)[-1]}")
    print(f"CMO(14)      ={cmo(close)[-1]}")
    print(f"Choppiness   ={choppiness(high, low, close)[-1]}  (>61.8震荡 / <38.2趋势)")
    vip, vim = vortex(high, low, close)
    print(f"Vortex       VI+={vip[-1]}  VI-={vim[-1]}  {'上升' if vip[-1]>vim[-1] else '下降'}")
    print(f"UltimateOsc  ={ultimate_osc(high, low, close)[-1]}")
    print(f"MassIndex    ={mass_index(high, low)[-1]}  (>27 反转预警)")
    pp = pivot_points(high, low, close)[-1]
    print(f"Pivot        PP={pp['pp']}  R1={pp['r1']}  S1={pp['s1']}")
    print(f"HullMA(9)    ={hull_ma(close)[-1]}")
    print(f"VWMA(20)     ={vwma(close, vol)[-1]}")
    print(f"ElderForce   ={elder_force(close, vol)[-1]}")
    print(f"ConnorsRSI   ={connors_rsi(close)[-1]}")
    print(f"A/D Line     ={ad_line(high, low, close, vol)[-1]}")
    isp, pt = zigzag(high, low, pct=5.0)
    pivots = [(i, pt[i]) for i in range(len(isp)) if isp[i]]
    print(f"ZigZag 5%    拐点数={len(pivots)}  {pivots[:6]}")
    print("\n全部指标计算完成, 无异常。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo or True:
        _demo()
