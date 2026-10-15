#!/usr/bin/env python3
"""
trend_strength.py — 趋势强度指数族 (RTSI / TMA / MSCI)

借鉴来源: hengruiyun/AI-Stock-Master (AGPL-3.0)
  https://github.com/hengruiyun/AI-Stock-Master
  该仓库公开了三个核心算法的**数学模型与参数定义** (README "Core Algorithm
  Details"), 但未公开实现代码。AGPL-3.0 不允许复制代码, 因此这里按其公开的
  数学式**独立实现**, 不声称复刻其代码; 权重系数按我们的数据重新标定。

## 三个算法 (原仓库命名与数学式)

### RTSI — 个股趋势强度指数 (Individual Stock Trend Strength Index)
    RTSI = α1*TrendSlope + α2*Consistency + α3*Confidence + α4*Volume_Factor
      TrendSlope   = Σ(Pi - Pi-1)/n × Normalization_Factor   (归一化斜率)
      Consistency  = 1 - σ(returns)/μ(returns)               (趋势稳定性)
      Confidence   = R² × (1 - p_value)                      (回归显著性)
      Volume_Factor= log(Volume_ratio) × Weight              (量能参与度)

### TMA — 技术动量分析 (行业分析用)
    TMA = β1*RSI_Score + β2*MACD_Signal + β3*Momentum_Change
          + β4*Technical_Weight + β5*Volume_Profile
      RSI_Score       = 行业相对强度归一化
      MACD_Signal     = (MACD - Signal)/Historical_Range
      Momentum_Change = (当前动量 - 历史均值)/标准差
      Volume_Profile  = 量能分布 (放量位置)

### MSCI — 市场情绪综合指数 (Market Sentiment Composite Index)
    原仓库: 多维度情绪指标融合 (行为金融 + 市场微观结构 + 情绪分析)。
    本项目落地口径: 涨跌家数比 / 涨停跌停比 / 封板率 / 晋级率 /
    成交额变化 / 北向或主力资金 / 全球情绪 七项归一化加权。

## 为什么要做这三个 (而不是继续用 17 指标共振)

engine_v5 的 17 个指标是「平铺打分」, 每个指标各自为政; 而 RTSI 的核心思想是
**用回归的统计显著性来衡量趋势可信度** —— 一条 R²=0.9 的上升趋势远比
「7 个指标同时说涨」更可靠。这正好补上我们的盲区: 我们有信号数量, 没有信号质量。

## 数据来源
  bulk 日K (前复权) — 自身算 TrendSlope/R²/RSI/MACD/动量
  data/latest.json       — 市场面 (涨跌家数/涨停/封板率) 给 MSCI
  data/moneyflow.json    — 主力资金给 MSCI
  data/global_markets.json — 全球情绪给 MSCI

输出: data/trend_strength.json
  - rtsi: 每只股票的趋势强度 0-100 + 分量明细 + 趋势方向
  - tma:  每个行业/板块的技术动量
  - msci: 全市场情绪综合指数 0-100 + 七项明细 + 情绪定性

用法:
  python3 trend_strength.py --limit 500
"""
import argparse
import glob
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA
except Exception:
    BULK = "/root/workspace/stocks-data-kline/data/bulk"
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CODE = re.compile(r"^[a-z]{2}\d{6}$")

# 原仓库称这些系数由机器学习优化, 但未给数值。这里按数据特性手工标定,
# 并在输出里明确标注「权重为自定, 非原作者数值」, 不冒充。
RTSI_WEIGHTS = {"slope": 0.35, "consistency": 0.25, "confidence": 0.25, "volume": 0.15}
TMA_WEIGHTS = {"rsi": 0.20, "macd": 0.25, "momentum": 0.25, "weight": 0.15, "profile": 0.15}
MSCI_WEIGHTS = {"advance": 0.20, "limit_ratio": 0.18, "seal": 0.14, "promo": 0.14,
                "turnover": 0.12, "capital": 0.12, "global": 0.10}


def load(name):
    p = os.path.join(AI_VIEW_DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def load_klines():
    """bulk 日K → {code: {dates, open, close, high, low, vol}}"""
    out = {}
    files = sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv")))
    for f in files:
        try:
            for line in open(f, encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) < 7:
                    continue
                code = parts[0]
                if not RE_CODE.match(code):
                    continue
                if not RE_DATE.match(parts[1]):
                    continue
                try:
                    o, c, h, l, v = (float(parts[2]), float(parts[3]),
                                     float(parts[4]), float(parts[5]), float(parts[6]))
                except ValueError:
                    continue
                rec = out.setdefault(code, {"dates": [], "open": [], "close": [],
                                            "high": [], "low": [], "vol": []})
                d = parts[1]
                if d in rec["dates"]:
                    continue
                rec["dates"].append(d)
                rec["open"].append(o)
                rec["close"].append(c)
                rec["high"].append(h)
                rec["low"].append(l)
                rec["vol"].append(v)
        except Exception:
            continue
    cleaned = {}
    for code, rec in out.items():
        if len(rec["dates"]) < 30:
            continue
        order = sorted(range(len(rec["dates"])), key=lambda i: rec["dates"][i])
        for k in ("dates", "open", "close", "high", "low", "vol"):
            rec[k] = [rec[k][i] for i in order]
        cleaned[code] = rec
    return cleaned


# ─── 统计工具 ───────────────────────────────────────────────────────
def linreg(xs, ys):
    """最小二乘 → (slope, intercept, r2, p_value_approx)。n<3 返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    if sxx == 0:
        return None
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    if r2 >= 1:
        p = 0.0
    else:
        t = math.sqrt(max(0.0, r2)) * math.sqrt((n - 2) / max(1e-9, 1 - r2))
        p = 2 * (1 - 0.5 * (1 + math.erf(t / math.sqrt(2))))
    return slope, my - slope * mx, r2, p


def rsi(closes, end, period=14):
    if end < period:
        return None
    g = l = 0.0
    for j in range(end - period + 1, end + 1):
        ch = closes[j] - closes[j - 1]
        if ch > 0:
            g += ch
        else:
            l -= ch
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def macd_at(closes, i):
    """SMA 近似 EMA 的 MACD (DIF, DEA, hist)。i<25 返回 None。"""
    if i < 25:
        return None
    ma12 = sum(closes[i - 11:i + 1]) / 12
    ma26 = sum(closes[i - 25:i + 1]) / 26
    return ma12 - ma26


def macd_signal(closes, end):
    if end < 26:
        return None
    hist = macd_at(closes, end)
    difs = [d for d in (macd_at(closes, i) for i in range(end - 8, end + 1)) if d is not None]
    dea = sum(difs) / len(difs) if difs else hist
    return hist - dea, hist, dea


# ─── RTSI: 个股趋势强度 ────────────────────────────────────────────
def rtsi(rec, window=20):
    c = rec["close"]
    v = rec["vol"]
    n = len(c)
    if n < window + 1:
        return None
    end = n - 1
    xs = list(range(window))
    ys = c[end - window + 1:end + 1]

    reg = linreg(xs, ys)
    if not reg:
        return None
    slope, intercept, r2, p = reg

    avg_p = sum(ys) / window
    if avg_p <= 0:
        return None
    trend_slope = slope * window / avg_p * 100

    rets = [ys[i] - ys[i - 1] for i in range(1, len(ys))]
    mu = sum(rets) / len(rets)
    sd = (sum((x - mu) ** 2 for x in rets) / len(rets)) ** 0.5
    # Consistency: 原式为 1 - σ/μ, 但 μ 接近 0 时 cv 爆表使该维恒为 0
    # (实测 200 只全部 consistency=0, 25% 权重白丢)。改用两个稳健分量:
    #   a) 方向一致性 = 与趋势同向的天数占比 (涨趋势中收阳的天数)
    #   b) 波动收缩 = 1 - min(1, σ/|μ|) 只在校正后使用
    # 两者等权, 都比原式抗 μ≈0 的情形。
    same_dir = sum(1 for x in rets if (x > 0) == (trend_slope > 0)) / len(rets)
    dir_consistency = same_dir * 100
    if abs(mu) > 1e-9:
        vol_consistency = max(0.0, min(100.0, (1 - min(1.0, sd / abs(mu))) * 100))
    else:
        vol_consistency = 50.0
    consistency = 0.6 * dir_consistency + 0.4 * vol_consistency

    confidence = r2 * (1 - p) * 100

    v5 = sum(v[end - 4:end + 1]) / 5
    v20 = sum(v[end - 19:end + 1]) / 20
    ratio = (v5 / v20) if v20 > 0 else 1.0
    vol_score = max(0.0, min(100.0, 50 + math.log(ratio) * 40)) if ratio > 0 else 50.0

    slope_score = max(0.0, min(100.0, 50 + trend_slope * 3.0))
    total = (slope_score * RTSI_WEIGHTS["slope"]
             + consistency * RTSI_WEIGHTS["consistency"]
             + confidence * RTSI_WEIGHTS["confidence"]
             + vol_score * RTSI_WEIGHTS["volume"])

    direction = "up" if trend_slope > 0.5 else ("down" if trend_slope < -0.5 else "flat")
    return {
        "rtsi": round(total, 1),
        "direction": direction,
        "trend_slope_pct": round(trend_slope, 2),
        "consistency": round(consistency, 1),
        "dir_consistency": round(dir_consistency, 1),
        "vol_consistency": round(vol_consistency, 1),
        "confidence": round(confidence, 1),
        "r2": round(r2, 3),
        "p_value": round(p, 4) if p >= 0.0001 else 0.0001,
        "p_value_note": None if p >= 0.0001 else "<0.0001 (正态近似下溢, 非绝对零)",
        "volume_ratio": round(ratio, 2),
        "vol_score": round(vol_score, 1),
        "slope_score": round(slope_score, 1),
        "window": window,
        "close": c[end],
    }


# ─── TMA: 板块技术动量 ─────────────────────────────────────────────
def tma_for_sector(recs):
    if not recs:
        return None
    common = None
    for rec in recs:
        s = set(rec["dates"])
        common = s if common is None else (common & s)
    if not common or len(common) < 30:
        return None
    dates = sorted(common)
    idx = [{d: i for i, d in enumerate(r["dates"])} for r in recs]
    closes = [sum(r["close"][ix[d]] for r, ix in zip(recs, idx)) / len(recs) for d in dates]
    vols = [sum(r["vol"][ix[d]] for r, ix in zip(recs, idx)) / len(recs) for d in dates]

    end = len(closes) - 1
    if end < 26:
        return None

    r = rsi(closes, end)
    rsi_score = r if r is not None else 50.0

    m = macd_signal(closes, end)
    if m:
        hist, dif, dea = m
        hmax = 1e-9
        for i in range(max(26, end - 59), end + 1):
            mi = macd_signal(closes, i)
            if mi:
                hmax = max(hmax, abs(mi[0]))
        macd_score = max(0.0, min(100.0, 50 + hist / hmax * 50))
    else:
        macd_score = 50.0

    rets = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    w = 10
    if len(rets) < w + 20:
        return None
    cur_mom = sum(rets[-w:]) / w
    hist60 = rets[-60:-w] if len(rets) >= 60 else rets[:-w]
    hmu = sum(hist60) / len(hist60)
    hsd = (sum((x - hmu) ** 2 for x in hist60) / len(hist60)) ** 0.5
    if hsd == 0:
        mom_score = 50.0
    else:
        mom_score = max(0.0, min(100.0, 50 + (cur_mom - hmu) / hsd * 20))

    above = tot = 0
    for rec in recs:
        c = rec["close"]
        if len(c) < 20:
            continue
        tot += 1
        if c[-1] > sum(c[-20:]) / 20:
            above += 1
    tech_weight = (above / tot * 100) if tot else 50.0

    v5 = sum(vols[-5:]) / 5
    v20 = sum(vols[-20:]) / 20
    vratio = (v5 / v20) if v20 > 0 else 1.0
    profile_score = max(0.0, min(100.0, 50 + math.log(vratio) * 40)) if vratio > 0 else 50.0

    total = (rsi_score * TMA_WEIGHTS["rsi"] + macd_score * TMA_WEIGHTS["macd"]
             + mom_score * TMA_WEIGHTS["momentum"] + tech_weight * TMA_WEIGHTS["weight"]
             + profile_score * TMA_WEIGHTS["profile"])
    return {
        "tma": round(total, 1),
        "n_stocks": tot,
        "rsi": round(rsi_score, 1),
        "macd": round(macd_score, 1),
        "momentum": round(mom_score, 1),
        "technical_weight": round(tech_weight, 1),
        "volume_profile": round(profile_score, 1),
        "volume_ratio": round(vratio, 2),
        "chg_1d": round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) > 1 else None,
        "chg_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 20 else None,
    }


# ─── MSCI: 市场情绪综合指数 ────────────────────────────────────────
def msci(latest, global_markets=None):
    m = (latest or {}).get("market") or {}
    phase = (latest or {}).get("phase") or {}

    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    lu, ld = _int(m.get("limit_up")), _int(m.get("limit_down"))
    adv, dec = _int(m.get("advance")), _int(m.get("decline"))
    total = max(1, adv + dec)

    advance_score = adv / total * 100

    if ld == 0 and lu == 0:
        limit_score = 50.0
    else:
        ratio = (lu + 1) / (ld + 1)
        limit_score = max(0.0, min(100.0, 50 + math.log(ratio) * 18))

    seal = phase.get("seal_rate")
    seal_score = float(seal) if isinstance(seal, (int, float)) else 50.0

    promo = phase.get("promo_rate")
    promo_score = max(0.0, min(100.0, float(promo) * 300)) \
        if isinstance(promo, (int, float)) else 50.0

    turnover_score = 50.0
    to = m.get("turnover")
    if isinstance(to, dict):
        yi = to.get("total_yi")
        if isinstance(yi, (int, float)) and yi > 0:
            turnover_score = max(0.0, min(100.0, 50 + (yi / 16000 - 1) * 100))

    capital_score = 50.0
    mf = load("moneyflow.json")
    if mf:
        idx_flow = mf.get("index_flow")
        if isinstance(idx_flow, dict):
            net = idx_flow.get("main_net_today", idx_flow.get("main_net"))
            if isinstance(net, (int, float)) and net != 0:
                capital_score = max(0.0, min(100.0, 50 + net / 1e9 * 8))

    global_score = 50.0
    if global_markets:
        sent = global_markets.get("sentiment", global_markets.get("score"))
        if isinstance(sent, (int, float)):
            global_score = max(0.0, min(100.0, 50 + sent))

    dims = {
        "advance": round(advance_score, 1),
        "limit_ratio": round(limit_score, 1),
        "seal": round(seal_score, 1),
        "promo": round(promo_score, 1),
        "turnover": round(turnover_score, 1),
        "capital": round(capital_score, 1),
        "global": round(global_score, 1),
    }
    total_score = sum(dims[k] * MSCI_WEIGHTS[k] for k in MSCI_WEIGHTS)
    mood = ("贪婪/亢奋" if total_score >= 70 else "偏暖" if total_score >= 58
            else "中性" if total_score >= 42 else "偏冷" if total_score >= 30
            else "恐慌/冰点")

    return {
        "msci": round(total_score, 1),
        "mood": mood,
        "dims": dims,
        "raw": {"limit_up": lu, "limit_down": ld, "advance": adv, "decline": dec,
                "seal_rate": seal, "promo_rate": promo},
    }


def main():
    ap = argparse.ArgumentParser(description="趋势强度指数族 (RTSI/TMA/MSCI)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--rtsi-window", type=int, default=20)
    ap.add_argument("--sector-top", type=int, default=30)
    args = ap.parse_args()

    now = datetime.now(CST)
    out = args.out or os.path.join(AI_VIEW_DATA, "trend_strength.json")

    print("[trend_strength] 加载 bulk K线 ...")
    klines = load_klines()
    print(f"[trend_strength] {len(klines)} 只")

    latest = load("latest.json") or {}
    gm = load("global_markets.json")

    codes = sorted(klines.keys())
    if args.limit and len(codes) > args.limit:
        step = len(codes) // args.limit
        codes = codes[::step][:args.limit]
    rtsi_rows = []
    for code in codes:
        r = rtsi(klines[code], args.rtsi_window)
        if r:
            r["code"] = code
            rtsi_rows.append(r)
    rtsi_rows.sort(key=lambda x: -x["rtsi"])
    print(f"[trend_strength] RTSI {len(rtsi_rows)} 只, TOP: "
          + ", ".join(f"{x['code']}={x['rtsi']}({x['direction']})"
                      for x in rtsi_rows[:5]))

    im = load("industry_map_em.json") or {}
    smap = im.get("map") or {}
    groups = {}
    for code, rec in klines.items():
        bare = code[2:] if len(code) == 8 else code
        sec = smap.get(bare)
        if sec:
            groups.setdefault(sec, []).append(rec)
    cand = []
    for sec, recs in groups.items():
        if len(recs) < 3:
            continue
        t = tma_for_sector(recs[:60])
        if t:
            t["sector"] = sec
            cand.append(t)
    cand.sort(key=lambda x: -(x.get("chg_1d") or 0))
    tma_rows = sorted(cand[:args.sector_top], key=lambda x: -x["tma"])
    print(f"[trend_strength] TMA {len(tma_rows)} 板块 (候选 {len(cand)}), TOP: "
          + ", ".join(f"{x['sector']}={x['tma']}" for x in tma_rows[:5]))

    m = msci(latest, gm)
    print(f"[trend_strength] MSCI {m['msci']} ({m['mood']}) dims={m['dims']}")

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "trend_strength.py",
        "origin": "数学模型借鉴 hengruiyun/AI-Stock-Master (AGPL-3.0, 仅参考其公开的"
                  "数学式与参数定义, 未复制代码; 权重为本项目自定)",
        "rtsi": {"n": len(rtsi_rows), "window": args.rtsi_window,
                 "weights": RTSI_WEIGHTS, "stocks": rtsi_rows},
        "tma": {"n": len(tma_rows), "weights": TMA_WEIGHTS, "sectors": tma_rows},
        "msci": m,
        "meta": {
            "rtsi_formula": "RTSI = α1*TrendSlope + α2*Consistency + α3*Confidence"
                            " + α4*Volume_Factor",
            "tma_formula": "TMA = β1*RSI + β2*MACD + β3*Momentum + β4*TechWeight"
                           " + β5*VolumeProfile",
            "msci_formula": "MSCI = 涨跌家数比*0.20 + 涨跌停比*0.18 + 封板率*0.14"
                            " + 晋级率*0.14 + 成交额*0.12 + 主力资金*0.12 + 全球情绪*0.10",
            "rtsi_direction": "direction 按 trend_slope_pct 判 up/flat/down; "
                              "RTSI 分高但 direction=down 表示「下跌趋势明确」, "
                              "不是买入信号 —— 强度≠看多",
            "r2_caveat": "R² 基于 20 个样本点的回归, 即使 0.8 也不算高确定性 "
                         "(样本少, 单点影响大); p_value 用正态近似, <0.0001 时 "
                         "截断为 0.0001 并附 note, 不显示 0 以免误导为「完全确定」",
            "caveat": "α/β 权重为本项目自定 (原作者称机器学习优化但未公开数值); "
                      "MACD 用 SMA 近似 EMA (避免逐日递推, 对信号强度足够); "
                      "p_value 用正态近似 (未引 scipy); "
                      "板块价格序列为成分股等权平均, 非市值加权; "
                      "Consistency 已改稳健版: 原式 1-σ/μ 在 μ≈0 时恒为 0 "
                      "(实测 200 只全为 0, 25% 权重白丢), 现改为 "
                      "0.6*方向一致性 + 0.4*波动收缩",
            "source": "bulk 前复权日K + latest.json + moneyflow.json + global_markets.json",
        },
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
