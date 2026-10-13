#!/usr/bin/env python3
"""
flow_divergence.py — 资金流-价格背离引擎 (自研, org3)

与通用指标的区别:
  MACD/RSI 只看价格自身的历史, 是「滞后」的。
  本引擎引入资金流维度, 计算两条加速度曲线的背离:

    价格加速度   A_p = (C_t - C_{t-5}) / 5  -  (C_{t-5} - C_{t-10}) / 5
    资金加速度   A_f = (M_t - M_{t-5}) / 5  -  (M_{t-5} - M_{t-10}) / 5
                 其中 M = 主力净流入 (元/日)

  背离度 D = sign(A_p) != sign(A_f) 且 |A_f| 显著
    D > 0  → 资金在进、价格在跌  → 「吸筹背离」, 潜在见底信号
    D < 0  → 资金在出、价格在涨  → 「派发背离」, 潜在见顶信号

第二维: 资金持续性 S = 近 10 日主力净流入为正的天数占比
  S 高 + D>0 → 信号强度高

第三维: 波动压缩 V = 近 20 日真实波幅 / 近 60 日真实波幅
  V < 0.8   → 波动收敛, 突破概率增

综合分 = 50 + 40*D_scaled + 20*S_scaled + 10*V_scaled
        (D 主导, S 确认, V 时机)

数据源:
  价格  本地K线 (腾讯前复权日K)
  资金  东财 push2his fflow/daykline (主力净流入)

输出: flow_divergence.json (供网站与多池算法消费)
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import BULK
except Exception:
    BULK = os.environ.get("STOCKS_KLINE_BULK") or "/root/workspace/stocks-data-kline/data/bulk"


def prefixed(code):
    c = str(code)
    if c.startswith(("sh", "sz", "bj")):
        return c
    c = c.zfill(6)
    if c.startswith(("60", "68", "90", "11", "13")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sz" + c


def secid(code):
    p = prefixed(code)
    return ("1." if p[:2] == "sh" else "0.") + p[2:]


def load_klines(bulk):
    data = {}
    for p in sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv"))):
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
                        [parts[0].strip(), float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def fetch_flow(code, n=60):
    """东财主力净流入序列 [{date, main_net}, ...] (按日期升序)"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={secid(code)}&lmt={n}&klt=101"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        klines = ((d.get("data") or {}).get("klines")) or []
        out = []
        for row in klines:
            f = row.split(",")
            if len(f) < 2:
                continue
            try:
                out.append({"date": f[0], "main_net": float(f[1])})
            except ValueError:
                continue
        out.sort(key=lambda x: x["date"])
        return out
    except Exception as e:
        return []


def _slope(series, i, span=5):
    """series[i] 附近 span 日的平均变化速度"""
    if i - 2 * span < 0 or i >= len(series):
        return None
    a = (series[i] - series[i - span]) / span
    b = (series[i - span] - series[i - 2 * span]) / span
    return a - b


def analyze(code, bars, flows):
    """单只股票的背离分析"""
    if len(bars) < 30 or len(flows) < 25:
        return None
    closes = [b[2] for b in bars]
    dates = [b[0] for b in bars]
    fmap = {f["date"]: f["main_net"] for f in flows}
    # 对齐: 只用有资金流数据的日期
    pairs = [(dates[i], closes[i], fmap.get(dates[i]))
             for i in range(len(bars))]
    pairs = [(d, c, m) for d, c, m in pairs if m is not None]
    if len(pairs) < 25:
        return None

    cs = [p[1] for p in pairs]
    ms = [p[2] for p in pairs]
    i = len(cs) - 1

    ap = _slope(cs, i, 5)
    af = _slope(ms, i, 5)
    if ap is None or af is None:
        return None

    # 归一化: 价格加速度用百分比, 资金加速度用日均净流入的百万分位
    ap_pct = ap / cs[i] * 100 if cs[i] else 0
    scale = max(1e5, sum(abs(x) for x in ms[-20:]) / 20)
    af_norm = af / scale

    # 背离判定
    diverge = 0
    if ap_pct < -0.02 and af_norm > 0.05:
        diverge = 1          # 吸筹: 价跌资金进
    elif ap_pct > 0.02 and af_norm < -0.05:
        diverge = -1         # 派发: 价涨资金出

    # 持续性: 近10日主力净流入为正天数占比
    recent10 = ms[-10:]
    persist = sum(1 for x in recent10 if x > 0) / len(recent10)

    # 波动压缩
    tr = []
    for j in range(1, len(bars)):
        tr.append(max(bars[j][3] - bars[j][4],
                      abs(bars[j][3] - bars[j - 1][2]),
                      abs(bars[j][4] - bars[j - 1][2])))
    v20 = sum(tr[-20:]) / max(1, len(tr[-20:]))
    v60 = sum(tr[-60:]) / max(1, len(tr[-60:]))
    compress = v20 / v60 if v60 else 1.0

    d_scaled = diverge * min(abs(af_norm) / 0.3, 1.0)
    score = 50 + 40 * d_scaled + 20 * (persist - 0.5) * 2 + 10 * (1 - min(compress, 1.5) / 1.5)
    score = round(max(0, min(100, score)), 1)

    if diverge > 0 and persist >= 0.6:
        signal = "吸筹背离"
    elif diverge < 0 and persist <= 0.4:
        signal = "派发背离"
    elif compress < 0.8:
        signal = "波动收敛"
    elif persist >= 0.7:
        signal = "资金持续流入"
    elif persist <= 0.3:
        signal = "资金持续流出"
    else:
        signal = "无显著背离"

    return {
        "code": code,
        "date": pairs[-1][0],
        "close": round(cs[-1], 2),
        "score": score,
        "signal": signal,
        "diverge": diverge,
        "price_accel_pct": round(ap_pct, 4),
        "flow_accel_norm": round(af_norm, 4),
        "flow_persist": round(persist, 2),
        "vol_compress": round(compress, 3),
        "n_flow_days": len(ms),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--codes", help="只算这些股票")
    args = ap.parse_args()

    bulk = args.bulk or BULK
    now = datetime.now(CST)
    print(f"[flow_divergence] {now:%Y-%m-%d %H:%M} CST")
    print(f"[flow_divergence] bulk={bulk}")

    klines = load_klines(bulk)
    print(f"[flow_divergence] {len(klines)} 只K线")

    if args.codes:
        want = [c.strip() for c in args.codes.split(",")]
        targets = [k for k in klines if any(k.endswith(w) or k == w for w in want)]
    else:
        # 取最近交易日有数据的股票, 按 K 线数量降序 (数据质量优先)
        latest = max((bars[-1][0] for bars in klines.values() if bars), default=None)
        targets = [k for k, b in klines.items() if b and b[-1][0] == latest]
        targets.sort(key=lambda k: -len(klines[k]))
        targets = targets[:args.limit]

    print(f"[flow_divergence] 分析 {len(targets)} 只", flush=True)

    results = []
    n_no_flow = 0
    for i, code in enumerate(targets):
        flows = fetch_flow(code, 60)
        if len(flows) < 25:
            n_no_flow += 1
            continue
        r = analyze(code, klines[code], flows)
        if r:
            results.append(r)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(targets)}  有效 {len(results)}  无资金流 {n_no_flow}", flush=True)
        time.sleep(0.12)

    results.sort(key=lambda x: -abs(x["score"] - 50))
    buy_side = [r for r in results if r["diverge"] > 0]
    sell_side = [r for r in results if r["diverge"] < 0]

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "flow_divergence (自研: 资金流-价格背离)",
        "n_scanned": len(targets),
        "n_scored": len(results),
        "n_no_flow": n_no_flow,
        "buy_side": buy_side[:30],
        "sell_side": sell_side[:30],
        "meta": {
            "formula": "score = 50 + 40*D + 20*(S-0.5)*2 + 10*(1-V/1.5)",
            "D": "价格加速度与资金加速度的背离 (sign 相反且 |A_f| 显著)",
            "S": "近10日主力净流入为正的天数占比 (持续性)",
            "V": "近20日真实波幅 / 近60日真实波幅 (压缩度, <0.8 意味突破临近)",
            "signals": {
                "吸筹背离": "价跌+资金进+持续流入 → 潜在见底",
                "派发背离": "价涨+资金出+持续流出 → 潜在见顶",
                "波动收敛": "V<0.8, 突破概率增",
            },
            "source": "价格: 腾讯前复权日K | 资金: 东财 push2his fflow",
            "caveat": "背离是统计规律非必然; 资金流数据为交易所口径, 不含隐性买单; "
                      "仅供研究不构成投资建议",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n=== 吸筹背离 TOP8 ===")
    for r in buy_side[:8]:
        print(f"  {r['code']} {r['date']} {r['close']:>8} score={r['score']:5.1f} "
              f"价加速={r['price_accel_pct']:+.4f}% 资加速={r['flow_accel_norm']:+.4f} "
              f"持续={r['flow_persist']} 压缩={r['vol_compress']}")
    print(f"\n=== 派发背离 TOP8 ===")
    for r in sell_side[:8]:
        print(f"  {r['code']} {r['date']} {r['close']:>8} score={r['score']:5.1f} "
              f"价加速={r['price_accel_pct']:+.4f}% 资加速={r['flow_accel_norm']:+.4f} "
              f"持续={r['flow_persist']}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
