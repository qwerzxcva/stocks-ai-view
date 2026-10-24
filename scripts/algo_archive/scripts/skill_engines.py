# 从新技能吸收的引擎 (2026-10-21)
# 来源: market-sentiment skill (情绪四阶段规则) + dual-trend-selector skill (GMMA+Ichimoku+ADX)

import pandas as pd
import numpy as np

# ============ 1. 市场情绪温度计 (engine 7) ============
def sentiment_phase(zt_count, dt_count, max_lianban, zhaban_rate):
    """规则来自 market-sentiment skill, 阈值原样保留
    返回: bingdian(冰点)/kangfen(亢奋)/fenqi(分歧)/neutral + 建议仓位上限"""
    if zt_count < 20 and dt_count > 10 and max_lianban < 3:
        return "bingdian", 0.2
    if zt_count > 80 and zhaban_rate < 0.15 and max_lianban > 5:
        return "kangfen", 1.0
    if zhaban_rate > 0.25:
        return "fenqi", 0.5
    return "neutral", 0.6

# ============ 2. 双趋势共振 (engine 8): GMMA+Ichimoku+ADX ============
def calc_adx(df, period=14):
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift(1)).abs(),
                    (df['low']-df['close'].shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period).mean()
    plus_dm = df['high'].diff(); minus_dm = -df['low'].diff()
    plus_dm[(plus_dm <= 0) | (plus_dm <= minus_dm)] = 0
    minus_dm[(minus_dm <= 0) | (minus_dm <= plus_dm)] = 0
    pdi = 100 * plus_dm.ewm(alpha=1/period).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1/period).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/period).mean()

def dual_trend_score(df, liq_th=1e8, adx_th=20):
    """输入 60 日前复权 OHLCV+amount DataFrame (升序)。返回 0-100 分与 S/A/B/C 级"""
    if len(df) < 60:
        return None
    if df['amount'].rolling(20).mean().iloc[-1] < liq_th:
        return {"score": 0, "grade": "REJECT", "reason": "流动性不足"}
    df = df.copy()
    df['adx'] = calc_adx(df)
    if df['adx'].iloc[-1] < adx_th:
        return {"score": 0, "grade": "REJECT", "reason": f"震荡市 ADX={df['adx'].iloc[-1]:.0f}"}
    for p in [3,5,8,10,12,15,30,35,40,45,50,60]:
        df[f'ema_{p}'] = df['close'].ewm(span=p, adjust=False).mean()
    s = df[[f'ema_{p}' for p in [3,5,8,10,12,15]]].mean(axis=1)
    l = df[[f'ema_{p}' for p in [30,35,40,45,50,60]]].mean(axis=1)
    disp = (s - l) / df['close']
    tenkan = (df['high'].rolling(9).max() + df['low'].rolling(9).min()) / 2
    kijun = (df['high'].rolling(26).max() + df['low'].rolling(26).min()) / 2
    sa = ((tenkan + kijun) / 2).shift(26)
    sb = ((df['high'].rolling(52).max() + df['low'].rolling(52).min()) / 2).shift(26)
    last = df.iloc[-1]
    score = 0
    if last['ema_15'] > last['ema_30'] and s.iloc[-1] > l.iloc[-1]: score += 30
    if disp.iloc[-1] > 0.02: score += 10
    if last['close'] > max(sa.iloc[-1], sb.iloc[-1]): score += 20
    if tenkan.iloc[-1] > kijun.iloc[-1]: score += 15
    if tenkan.iloc[-1] > kijun.iloc[-1] and tenkan.iloc[-2] <= kijun.iloc[-2]: score += 5
    if abs(sa.iloc[-1] - sb.iloc[-1]) / last['close'] > 0.03: score += 5
    a = last['adx']
    score += 20 if a > 40 else (15 if a > 30 else 10)
    if df['amount'].rolling(20).mean().iloc[-1] > 1e9: score += 5
    grade = "S" if score >= 90 else ("A" if score >= 75 else ("B" if score >= 60 else "C"))
    stop = max(min(sa.iloc[-1], sb.iloc[-1]), last['ema_60'])
    return {"score": score, "grade": grade, "stop_loss": round(stop, 2),
            "adx": round(a, 1), "disp": round(disp.iloc[-1], 4)}
