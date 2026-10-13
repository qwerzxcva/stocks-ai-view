#!/usr/bin/env python3
"""
engine_v5.py — 算法引擎 v5 (多指标共振 + 自适应权重 + 可证伪)

相比旧版的问题:
  - 权重是手工常数 (位置40+金叉20+RSI15...), 没有依据
  - 只用 MA/RSI 两三个指标, 信息利用率低
  - 无法回答"为什么选它"和"这个权重对不对"

v5 设计:
  1. 多指标共振: 16 个指标 (indicators_ext) 各自给出方向票 (-1/0/+1) 与强度
  2. 共振度: 同向票数 / 总票数, 分歧大的直接降权 (避免"指标打架还高分")
  3. 自适应权重: 用 optimizer.walk_forward 在历史K线上验证权重组合,
     样本外不过关的权重不用 (而非拍脑袋)
  4. 可解释: 每个分数都能拆到具体指标的贡献
  5. 可证伪: 输出预测 + 依据, 由 reflect 模块事后核对

评分公式:
  score = 100 * (Σ w_i · vote_i · strength_i) / (Σ w_i)
          × 共振系数 (0.6 + 0.4 × 同向率)
  - vote_i ∈ {-1, 0, +1}   指标方向
  - strength_i ∈ [0, 1]    该指标本次信号强度
  - w_i                    权重 (默认等权, 可由 optimizer 校准)

输出:
  {code, name, score, resonance, votes:[{indicator, vote, strength, why}], ...}
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import indicators_ext as I

# 指标 → (方向判断函数, 权重)
# 方向判断输入是指标序列与价格序列, 返回 (vote, strength, why)
def _last(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _prev(seq, n=1):
    vals = [v for v in seq if v is not None]
    return vals[-1 - n] if len(vals) > n else None


def build_votes(close, high, low, vol):
    """16 指标 → 投票列表"""
    votes = []

    def add(ind, vote, strength, why):
        votes.append({"indicator": ind, "vote": vote,
                      "strength": round(min(max(strength, 0), 1), 3), "why": why})

    # 1. SuperTrend (趋势)
    st, tr = I.supertrend(high, low, close)
    if _last(tr) is not None:
        t = _last(tr)
        add("SuperTrend", t, 0.9 if t != 0 else 0,
            f"趋势{'多头' if t>0 else '空头'}, 线值 {_last(st):.2f}" if _last(st) else "未形成")

    # 2. MA5 vs MA20 (金叉)
    ma5, ma20 = I.sma(close, 5), I.sma(close, 20)
    l5, l20 = _last(ma5), _last(ma20)
    if l5 and l20:
        dev = (l5 - l20) / l20 * 100
        v = 1 if dev > 0.3 else (-1 if dev < -0.3 else 0)
        add("MA5/MA20", v, min(abs(dev) / 3, 1), f"MA5={l5:.2f} MA20={l20:.2f} 偏离{dev:+.2f}%")

    # 3. RSI
    rsi = I._rsi(close)
    lr = _last(rsi)
    if lr:
        if lr < 30:
            add("RSI", 1, (30 - lr) / 30, f"RSI={lr} 超卖, 反弹概率增")
        elif lr > 70:
            add("RSI", -1, (lr - 70) / 30, f"RSI={lr} 超买, 回踩风险")
        elif 45 <= lr <= 60:
            add("RSI", 1, 0.4, f"RSI={lr} 强势区间")
        else:
            add("RSI", 0, 0.2, f"RSI={lr} 中性")

    # 4. MACD (EMA12-EMA26, 比较是否走强)
    e12, e26 = I.ema(close, 12), I.ema(close, 26)
    le, le26 = _last(e12), _last(e26)
    pe, pe26 = _prev(e12), _prev(e26)
    if le and le26 and pe and pe26:
        hist = le - le26
        ph = pe - pe26
        v = 1 if hist > 0 else -1
        s = min(abs(hist - ph) / max(abs(ph), 0.01), 1)
        add("MACD", v, max(s, 0.3), f"DIF={hist:+.3f} 前值{ph:+.3f} {'走强' if hist>ph else '走弱'}")

    # 5. KDJ (用 9日 RSV 近似 K 值)
    if len(close) >= 9:
        hn = max(high[-9:])
        ln = min(low[-9:])
        rsv = (close[-1] - ln) / (hn - ln) * 100 if hn > ln else 50.0
        v = 1 if rsv > 50 else -1
        add("KDJ(RSV)", v, abs(rsv - 50) / 50, f"RSV={rsv:.1f} (9日)")

    # 6. BOLL (布林带位置)
    mid, up, lo = I.keltner(high, low, close)   # Keltner 与 BOLL 同族
    lm, lu, ll = _last(mid), _last(up), _last(lo)
    if lm and lu and ll and lu > ll:
        pos = (close[-1] - ll) / (lu - ll)
        v = 1 if pos > 0.7 else (-1 if pos < 0.3 else 0)
        add("BOLL/Keltner", v, abs(pos - 0.5) * 2, f"带内位置 {pos*100:.0f}% (中轨{lm:.2f})")

    # 7. Donchian 位置
    du, dm, dl = I.donchian(high, low, 20)
    ldu = _last(du)
    if ldu:
        dev = (close[-1] - ldu) / ldu * 100
        v = 1 if dev > -1 else (-1 if dev < -8 else 0)
        add("Donchian20", v, min(abs(dev) / 8, 1), f"距20日高 {dev:+.2f}%")

    # 8. CMO
    cmo = I.cmo(close, 14)
    lc = _last(cmo)
    if lc is not None:
        v = 1 if lc > 20 else (-1 if lc < -20 else 0)
        add("CMO", v, min(abs(lc) / 50, 1), f"CMO={lc}")

    # 9. Vortex
    vip, vim = I.vortex(high, low, close)
    lvip, lvim = _last(vip), _last(vim)
    if lvip and lvim:
        v = 1 if lvip > lvim else -1
        add("Vortex", v, min(abs(lvip - lvim) * 5, 1), f"VI+={lvip} VI-={lvim}")

    # 10. Ultimate Oscillator
    uo = I.ultimate_osc(high, low, close)
    lu_ = _last(uo)
    if lu_:
        v = 1 if 30 < lu_ < 70 and lu_ > 50 else (-1 if lu_ >= 70 else 0)
        add("UltimateOsc", v, 0.4 if v else 0.2, f"UO={lu_}")

    # 11. Choppiness (趋势/震荡判别)
    ch = I.choppiness(high, low, close)
    lch = _last(ch)
    if lch:
        # 低 CHOP = 强趋势, 高 CHOP = 震荡 (震荡时不投票)
        if lch < 40:
            v = 1 if close[-1] > _last(I.sma(close, 20)) else -1
            add("Choppiness", v, (50 - lch) / 50, f"CHOP={lch} 趋势市, 顺势")
        else:
            add("Choppiness", 0, 0.1, f"CHOP={lch} 震荡市, 不追趋势信号")

    # 12. HullMA
    hm = I.hull_ma(close, 9)
    lhm = _last(hm)
    if lhm:
        v = 1 if close[-1] > lhm else -1
        add("HullMA", v, min(abs(close[-1] - lhm) / lhm * 100 / 2, 1),
            f"HMA={lhm:.2f} 价格{'上方' if close[-1]>lhm else '下方'}")

    # 13. Elder Force
    ef = I.elder_force(close, vol)
    lef = _last(ef)
    if lef is not None:
        v = 1 if lef > 0 else -1
        add("ElderForce", v, min(abs(lef) / (abs(lef) + 1e5), 1), f"EF={lef:.0f}")

    # 14. Connors RSI
    crs = I.connors_rsi(close)
    lcrs = _last(crs)
    if lcrs is not None:
        if lcrs < 20:
            add("ConnorsRSI", 1, (20 - lcrs) / 20, f"CRSI={lcrs} 极度超卖")
        elif lcrs > 80:
            add("ConnorsRSI", -1, (lcrs - 80) / 20, f"CRSI={lcrs} 极度超买")
        else:
            add("ConnorsRSI", 0, 0.2, f"CRSI={lcrs} 中性")

    # 15. A/D Line (累积派发)
    ad = I.ad_line(high, low, close, vol)
    if len(ad) >= 11 and ad[-1] is not None and ad[-11] is not None:
        d = ad[-1] - ad[-11]
        v = 1 if d > 0 else -1
        add("A/D Line", v, min(abs(d) / (abs(ad[-1]) + 1), 1),
            f"10日{'累积' if d>0 else '派发'} {d:+.0f}")

    # 16. Pivot (枢轴支撑)
    pp = I.pivot_points(high, low, close)
    if pp:
        p = pp[-1]
        c = close[-1]
        if c > p["pp"]:
            v = 1
            why = f"价格在枢轴 {p['pp']} 上方, 支撑 {p['s1']}"
        else:
            v = -1
            why = f"价格在枢轴 {p['pp']} 下方, 压力 {p['r1']}"
        add("Pivot", v, 0.4, why)

    # 17. VWMA
    vw = I.vwma(close, vol, 20)
    lvw = _last(vw)
    if lvw:
        v = 1 if close[-1] > lvw else -1
        add("VWMA", v, min(abs(close[-1] - lvw) / lvw * 100 / 2, 1),
            f"VWMA={lvw:.2f} (量均价)")

    return votes


def score_stock(close, high, low, vol, weights=None):
    """多指标共振评分。"""
    votes = build_votes(close, high, low, vol)
    if not votes:
        return None
    w = weights or {}
    num = den = 0.0
    bull = bear = 0
    for v in votes:
        wi = w.get(v["indicator"], 1.0)
        num += wi * v["vote"] * v["strength"]
        den += wi
        if v["vote"] > 0:
            bull += 1
        elif v["vote"] < 0:
            bear += 1
    directional = bull + bear
    resonance = (max(bull, bear) / directional) if directional else 0.0
    base = num / den if den else 0.0                     # [-1, 1]
    factor = 0.6 + 0.4 * resonance                       # 分歧降权
    score = round(50 + 50 * base * factor, 1)            # 映射到 0-100
    return {
        "score": score,
        "raw": round(base, 4),
        "resonance": round(resonance, 3),
        "bull": bull, "bear": bear, "neutral": len(votes) - directional,
        "votes": votes,
    }


# ─── 批量: 从本地K线扫全市场 ──────────────────────────────────────
def load_klines(bulk_dir, limit=None):
    import glob
    data = {}
    files = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    data.setdefault(code, []).append(
                        [parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
        if limit and len(data) >= limit:
            break
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default="/root/workspace/stocks-data-kline/data/bulk")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--min-score", type=float, default=60)
    args = ap.parse_args()

    klines = load_klines(args.bulk, args.limit)
    print(f"[engine_v5] 加载 {len(klines)} 只K线", flush=True)

    results = []
    for code, bars in klines.items():
        if len(bars) < 30:
            continue
        close = [b[2] for b in bars]
        high = [b[3] for b in bars]
        low = [b[4] for b in bars]
        vol = [b[5] for b in bars]
        r = score_stock(close, high, low, vol)
        if not r:
            continue
        r["code"] = code
        r["date"] = bars[-1][0]
        r["price"] = close[-1]
        r["bars"] = len(bars)
        results.append(r)

    results.sort(key=lambda x: -x["score"])
    now = datetime.now(CST)
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "v5 (多指标共振 + 自适应权重)",
        "n_scanned": len(klines),
        "n_scored": len(results),
        "indicators": 17,
        "formula": "score = 50 + 50 * (Σ w·vote·strength / Σ w) * (0.6 + 0.4*共振率)",
        "top": results[:50],
        "meta": {
            "resonance": "同向票数/方向票数, 分歧大时降权 (0.6~1.0 系数)",
            "weights": "默认等权; 可由 optimizer.py walk_forward 校准后写入",
            "caveat": "技术指标共振不等于必然上涨, 只反映多维度信号一致性; "
                      "样本外未验证的权重不应用于实盘",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n=== TOP 10 (共振度优先) ===")
    for r in results[:10]:
        print(f"  {r['code']} score={r['score']:5.1f} 共振={r['resonance']:.2f} "
              f"多{r['bull']}/空{r['bear']}/中{r['neutral']} 价={r['price']}")
    hi = [r for r in results if r["score"] >= args.min_score]
    print(f"\n≥{args.min_score}分: {len(hi)} 只")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
