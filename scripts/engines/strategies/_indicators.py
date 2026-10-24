#!/usr/bin/env python3
"""_indicators.py — 策略共享技术指标库（纯标准库，无第三方依赖）。

K 线数据口径：data/bulk/day_qfq_*.csv 七列 code|date|open|close|high|low|vol。
本库只依赖收盘价/最高价/最低价/成交量序列，全部前复权。

数据缺口说明（如实标注降级）：
  - 换手率：K线无流通股本，用「当日量 / 近20日均量」的量比近似替代换手活跃度。
  - 量比：当日量 / 近5日均量（标准定义）。
  - 主力资金/五档盘口/分时：K线无此数据，相关策略因子降级为「量价行为代理」，
    各策略 evaluate 时通过 context.get("has_intraday") 判断是否启用，默认关闭并标注。

所有函数输入为按时间升序的 list[float]，返回与输入等长的 list（前段不足窗口为 None）。
"""
from __future__ import annotations

from typing import List, Optional


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """指数移动平均。前 period-1 个为 None。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return out
    k = 2.0 / (period + 1)
    # 用前 period 个的 SMA 作为 EMA 种子
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)  # type: ignore[operator]
    return out


def sma(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def kdj(high: List[float], low: List[float], close: List[float],
        period: int = 9, k_smooth: int = 3, d_smooth: int = 3
        ) -> dict:
    """KDJ。返回 {'k':[..], 'd':[..], 'j':[..]}，前段 None。"""
    n = len(close)
    k_arr: List[Optional[float]] = [None] * n
    d_arr: List[Optional[float]] = [None] * n
    j_arr: List[Optional[float]] = [None] * n
    rsv: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        hh = max(high[i - period + 1:i + 1])
        ll = min(low[i - period + 1:i + 1])
        rsv[i] = 50.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100.0
    # K = RSV 的 k_smooth 日 EMA 式平滑（SMA 简化，常规 KDJ 用 1/3 平滑）
    kv = 50.0
    dv = 50.0
    for i in range(n):
        if rsv[i] is None:
            continue
        kv = (2.0 / k_smooth) * rsv[i] + (1 - 2.0 / k_smooth) * kv if k_smooth else rsv[i]
        # 标准 KDJ：K = 2/3*前K + 1/3*RSV
        kv = (2.0 / 3.0) * kv + (1.0 / 3.0) * rsv[i]
        dv = (2.0 / 3.0) * dv + (1.0 / 3.0) * kv
        k_arr[i] = kv
        d_arr[i] = dv
        j_arr[i] = 3.0 * kv - 2.0 * dv
    return {"k": k_arr, "d": d_arr, "j": j_arr}


def rsi(close: List[float], period: int = 6) -> List[Optional[float]]:
    """RSI（Wilder 平滑）。"""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = close[i] - close[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        ch = close[i] - close[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def boll(close: List[float], period: int = 20, num_std: float = 2.0) -> dict:
    """布林带。返回 {'mid','up','low'}。"""
    n = len(close)
    mid: List[Optional[float]] = [None] * n
    up: List[Optional[float]] = [None] * n
    low: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        win = close[i - period + 1:i + 1]
        m = sum(win) / period
        var = sum((x - m) ** 2 for x in win) / period
        sd = var ** 0.5
        mid[i] = m
        up[i] = m + num_std * sd
        low[i] = m - num_std * sd
    return {"mid": mid, "up": up, "low": low}


def macd(close: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD。返回 {'dif','dea','hist'}。hist = (dif-dea)*2。"""
    ef = ema(close, fast)
    es = ema(close, slow)
    n = len(close)
    dif: List[Optional[float]] = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            dif[i] = ef[i] - es[i]  # type: ignore[operator]
    # DEA 是 DIF 的 signal 日 EMA（对 None 段跳过）
    dif_vals = [d if d is not None else 0.0 for d in dif]
    dea_raw = ema(dif_vals, signal)
    dea: List[Optional[float]] = [None] * n
    hist: List[Optional[float]] = [None] * n
    for i in range(n):
        if dif[i] is not None and dea_raw[i] is not None:
            dea[i] = dea_raw[i]
            hist[i] = (dif[i] - dea_raw[i]) * 2.0  # type: ignore[operator]
    return {"dif": dif, "dea": dea, "hist": hist}


def vol_ratio(vol: List[float], period: int = 5) -> List[Optional[float]]:
    """量比 = 当日量 / 近 period 日均量（不含当日）。"""
    n = len(vol)
    out: List[Optional[float]] = [None] * n
    for i in range(period, n):
        base = sum(vol[i - period:i]) / period
        out[i] = vol[i] / base if base > 0 else None
    return out


def bias(close: List[float], ma_values: List[Optional[float]]) -> List[Optional[float]]:
    """乖离率 (close - ma)/ma*100%。ma_values 通常为 ema 输出。"""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        m = ma_values[i]
        if m is not None and m > 0:
            out[i] = (close[i] - m) / m * 100.0
    return out


def pct_chg(close: List[float]) -> List[Optional[float]]:
    """日涨跌幅 %。"""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    for i in range(1, n):
        if close[i - 1] > 0:
            out[i] = (close[i] / close[i - 1] - 1.0) * 100.0
    return out


def consecutive_limit_up(close: List[float], board: str = "main") -> List[int]:
    """连板数序列。涨停判定：主板>=9.8%, 创业/科创>=19.8%, 北交所>=29.8%（无涨跌停字段，用收盘涨幅近似）。
    board: main/star/bse。返回每日连板计数（非涨停日为 0）。"""
    thr = {"main": 9.8, "star": 19.8, "bse": 29.8}.get(board, 9.8)
    chg = pct_chg(close)
    out = [0] * len(close)
    for i in range(1, len(close)):
        c = chg[i]
        if c is not None and c >= thr:
            out[i] = out[i - 1] + 1
        else:
            out[i] = 0
    return out
