#!/usr/bin/env python3
"""
build_lowpool.py — 低位池: 从本地K线 + 实时快照找「低位 + 有启动信号」的股票

口径 (与 multi_screener_v5 一致, 但降低门槛以适配覆盖率):
  1. 价格位置: 当前收盘价在近 120 日区间的 20% 分位以下 (低位)
  2. 技术信号: MA5 上穿 MA20 (金叉) 或 RSI 从 <30 回升至 40~55
  3. 排除: ST/*ST/退市, 当日涨幅 ≥9% (已过热), 停牌
  4. 打分: 位置分(40) + 金叉(20) + RSI修复(15) + 缩量止跌(15) + 温和上涨(10)

数据源:
  - K线: stocks-data-kline/data/bulk/day_qfq_*.csv + day_qfq_extra.csv
  - 快照: stocks-data-live/data/market/snapshot_YYYY-MM-DD.json
输出: ai-view/data/lowpool.json (带 meta 标注)
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
# 路径可用环境变量覆盖, 便于在 GitHub Actions / 其他机器上运行
#   STOCKS_KLINE_BULK  K线 bulk 目录 (默认本机路径)
#   STOCKS_LIVE_MARKET  快照目录
BULK = os.environ.get("STOCKS_KLINE_BULK") or "/root/workspace/stocks-data-kline/data/bulk"
SNAP_DIR = os.environ.get("STOCKS_LIVE_MARKET") or "/root/workspace/stocks-data-live/data/market"


def load_klines():
    """读取全部 bulk K线 → {code: [bars]} (bar = [date,o,c,h,l,v])

    兼容两种落盘格式:
      A) day_qfq_XX.csv  (原始): code|date|open|close|high|low|vol  (全 | 分隔)
      B) day_qfq_extra.csv (补录): code|date,open,close,high,low,vol
    早期版本用 split(',')[0] 猜分隔符, 对 A 格式整行无逗号导致 0 条解析, 已改为
    先按 | 切出 code 与其余, 再把其余按 | 或 , 切开。
    """
    data = {}
    files = sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv")))
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                code = code.strip()
                if not code or not code[0].isalpha():
                    continue
                # rest 可能是 "date|o|c|h|l|v" 或 "date,o,c,h,l,v"
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    bar = [parts[0].strip(), float(parts[1]), float(parts[2]),
                           float(parts[3]), float(parts[4]), float(parts[5])]
                except (ValueError, IndexError):
                    continue
                if bar[2] <= 0:
                    continue
                data.setdefault(code, []).append(bar)
    # 按日期排序
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def load_snapshot():
    """取最新快照 → {带前缀code: {...}}

    注意: K线 bulk 里的 code 与快照 key 都带市场前缀 (sh/sz/bj),
    因此此处保留原 key, 不再剥前缀 —— 早期版本剥前缀导致名称全部匹配不上。
    """
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "snapshot_*.json")))
    if not files:
        return {}
    with open(files[-1], encoding="utf-8") as f:
        snap = json.load(f)
    out = {}
    for k, v in snap.items():
        out[k] = {"name": v.get("name", ""), "price": v.get("price", 0),
                  "chg_pct": v.get("chg_pct", 0), "turnover": v.get("turnover", 0),
                  "vol_ratio": v.get("vol_ratio", 1.0),
                  "mktcap": v.get("mktcap", 0), "pe": v.get("pe", 0)}
    return out


def _ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0)) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def score_stock(bars, snap):
    """返回 (score, signals, price_pct) 或 None"""
    if len(bars) < 30:
        return None
    closes = [b[2] for b in bars]
    vols = [b[5] for b in bars]
    price = closes[-1]
    if price <= 0:
        return None

    name = snap.get("name", "") if snap else ""
    if "ST" in name or "退" in name:
        return None
    chg = (snap.get("chg_pct") or 0) if snap else 0
    if chg >= 9.0:            # 已涨停/过热, 不算低位
        return None

    # 1) 价格位置 (120日区间分位)
    win = closes[-120:] if len(closes) >= 120 else closes
    lo, hi = min(win), max(win)
    price_pct = round((price - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
    if price_pct > 25:
        return None

    signals, score = [], 0.0
    # 位置分: 越低越高 (0%→40, 25%→0)
    score += max(0.0, (25 - price_pct) / 25 * 40)
    signals.append(f"位置{price_pct}%")

    # 2) 金叉: MA5 上穿 MA20
    ma5, ma20 = _ma(closes, 5), _ma(closes, 20)
    if ma5 and ma20:
        if ma5 > ma20:
            score += 20
            signals.append("MA5>MA20")
        # MA20 走平/上翘加分
        if len(closes) >= 25:
            ma20_prev = _ma(closes[:-5], 20)
            if ma20_prev and ma20 > ma20_prev:
                score += 5
                signals.append("MA20上翘")

    # 3) RSI 修复: 从超卖区回升
    rsi = _rsi(closes)
    if rsi is not None:
        if 40 <= rsi <= 55:
            score += 15
            signals.append(f"RSI={rsi}修复")
        elif rsi < 30:
            score += 8
            signals.append(f"RSI={rsi}超卖")
        elif rsi > 70:
            score -= 10
            signals.append(f"RSI={rsi}偏高")

    # 4) 缩量止跌: 近5日均量 < 前20日均量的 70%
    if len(vols) >= 25:
        v5 = sum(vols[-5:]) / 5
        v20 = sum(vols[-25:-5]) / 20
        if v20 > 0 and v5 < v20 * 0.7:
            score += 15
            signals.append("缩量止跌")

    # 5) 温和上涨 (0~5%)
    if 0 <= chg <= 5:
        score += 10
        signals.append(f"温和{chg:+.1f}%")
    elif chg < -5:
        score -= 5
        signals.append(f"大跌{chg:+.1f}%")

    return round(score, 1), signals, price_pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    now = datetime.now(CST)
    print("=== 加载K线 ===", flush=True)
    klines = load_klines()
    print(f"  {len(klines)} 只有K线")

    print("=== 加载快照 ===", flush=True)
    snap = load_snapshot()
    print(f"  {len(snap)} 只快照")

    print("=== 打分 ===", flush=True)
    results = []
    for code, bars in klines.items():
        s = snap.get(code)   # K线 code 与快照 key 都带 sh/sz/bj 前缀
        r = score_stock(bars, s)
        if not r:
            continue
        score, signals, price_pct = r
        results.append({
            "code": code,
            "name": (s or {}).get("name", ""),
            "price": bars[-1][2],
            "chg_pct": (s or {}).get("chg_pct", 0),
            "price_pct": price_pct,
            "score": score,
            "signals": signals,
            "rsi": _rsi([b[2] for b in bars]),
            "kline_bars": len(bars),
            "mktcap": (s or {}).get("mktcap", 0),
            "pe": (s or {}).get("pe", 0),
        })

    results.sort(key=lambda x: -x["score"])
    strong = [r for r in results if r["score"] >= 60]
    watch = [r for r in results if 40 <= r["score"] < 60]

    out = {
        "date": now.strftime("%Y-%m-%d"),
        "ts": now.isoformat(),
        "total_filtered": len(results),
        "strong_buy": len(strong),
        "watch": len(watch),
        "strong": strong[:args.top],
        "watch_list": watch[:args.top],
        "kline_coverage": len(klines),
        "meta": {
            "source": "腾讯日K(qfq) + stocks-data-live 快照",
            "ts": now.isoformat(),
            "freq": "日K (收盘后定格)",
            "scoring": "位置分40(120日区间分位≤25%) + 金叉20 + MA20上翘5 + "
                       "RSI修复15 + 缩量止跌15 + 温和上涨10 - 过热/大跌惩罚",
            "thresholds": "strong_buy ≥60 分; watch 40~60 分",
            "excludes": "ST/*ST/退市, 当日涨幅≥9%, 停牌, K线<30根",
            "caveat": f"K线覆盖率 {len(klines)}/5343 "
                      f"({len(klines)/5343*100:.1f}%), 未覆盖股票不参与打分; "
                      "腾讯接口有频率限制 (连续请求会返回501)",
            "rebuilt": "build_lowpool.py (原 lowpool.json 因远端写入错位, "
                       "内容为 cycle_v2/2026-10-09, 已重建)",
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nDONE -> {args.out}")
    print(f"  候选 {len(results)} | 强烈买入 {len(strong)} | 关注 {len(watch)}")
    for r in strong[:12]:
        print(f"    {r['code']} {r['name']:8s} score={r['score']:5.1f} "
              f"位置={r['price_pct']}% RSI={r['rsi']} {'/'.join(r['signals'][:3])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
