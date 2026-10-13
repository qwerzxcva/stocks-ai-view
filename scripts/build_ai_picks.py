#!/usr/bin/env python3
"""
build_ai_picks.py — AI 推股 (实时, 多指标共振)

替代旧的 ai_picks.json (远端 social_scan 生成, 停在 2026-10-12 15:30)。

数据流:
  本地K线 (2577只) → engine_v5 17指标共振 → 过滤/排序 → ai_picks.json
  + 腾讯实时行情补现价/涨跌/市值/PE
  + TV 行业映射补 industry (5309只)

与 engine_v5 的区别:
  engine_v5 输出全市场评分 (研究用)
  ai_picks 只输出「当日可操作」的头部标的 (给网站的 AI 推荐 tab)
    - 剔除 ST/退市/停牌
    - 剔除已涨停 (追高风险)
    - 剔除 20 日区间位置 >85% (追高)
    - 要求共振度 >=0.6 (指标不打架)
    - 综合分 >=60

输出字段与前端 renderPicks 对齐:
  code/name/price/chg_pct/score/resonance/bull/bear/industry/mktcap/
  signals(前3条 why)/date/ts

用法:
  python3 build_ai_picks.py --out data/ai_picks.json --limit 3000
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine_v5 import score_stock, load_klines
from collect_all_sources import fetch_tencent_quote
from paths import BULK, INDUSTRY_MAP, SNAPSHOT_GLOB


def load_industry():
    if not os.path.exists(INDUSTRY_MAP):
        return {}
    try:
        return json.load(open(INDUSTRY_MAP, encoding="utf-8"))
    except Exception:
        return {}


def load_cn_names():
    """腾讯快照: code → 中文名"""
    out = {}
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if not snaps:
        return out
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
        for k, v in d.items():
            bare = k[2:] if k[:2] in ("sh", "sz", "bj") else k
            out[bare] = v.get("name", "")
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--min-score", type=float, default=60)
    ap.add_argument("--min-resonance", type=float, default=0.6)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    now = datetime.now(CST)
    print(f"[ai_picks] {now:%Y-%m-%d %H:%M CST}", flush=True)

    klines = load_klines(BULK, args.limit)
    print(f"[ai_picks] K线 {len(klines)} 只", flush=True)

    ind_map = load_industry()
    cn_names = load_cn_names()

    # 1) 多指标共振评分
    candidates = []
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
        # 20日区间位置
        seg = close[-20:]
        lo, hi = min(seg), max(seg)
        r["pos20"] = round((close[-1] - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
        candidates.append(r)

    print(f"[ai_picks] 评分完成 {len(candidates)} 只", flush=True)

    # 2) 过滤
    def bare(c):
        return c[2:] if c[:2] in ("sh", "sz", "bj") else c

    kept = []
    for r in candidates:
        if r["score"] < args.min_score:
            continue
        if r["resonance"] < args.min_resonance:
            continue
        bc = bare(r["code"])
        name = cn_names.get(bc, "")
        if "ST" in name or "退" in name:
            continue
        # 已涨停/接近涨停 → 追高风险, 剔除
        if r["pos20"] > 85:
            continue
        ind = ind_map.get(bc, {})
        r["name"] = name
        r["industry"] = ind.get("industry") or ind.get("sector") or ""
        r["signals"] = [v["why"] for v in r["votes"] if v["vote"] > 0][:3]
        kept.append(r)

    kept.sort(key=lambda x: -x["score"])
    top = kept[:args.top]
    print(f"[ai_picks] 过滤后 {len(kept)} 只, 取 TOP{len(top)}", flush=True)

    # 统一成裸码: K线 bulk 的 code 带 sh/sz/bj 前缀, 而快照/行业映射用裸码
    def bare(c):
        return c[2:] if c[:2] in ("sh", "sz", "bj") else c

    # 3) 腾讯实时行情补全 (现价/涨跌/市值/PE)
    quotes = {}
    if top:
        q, _meta = fetch_tencent_quote([r["code"] for r in top])  # 返回 (records, meta)
        quotes = {bare(x["code"]): x for x in q if isinstance(x, dict) and "code" in x}
        print(f"[ai_picks] 实时行情 {len(quotes)}/{len(top)}", flush=True)

    picks = []
    for r in top:
        bc = bare(r["code"])
        q = quotes.get(bc, {})
        picks.append({
            "code": bc,
            "name": r.get("name") or q.get("name") or "",
            "price": q.get("price") or r["price"],
            "chg_pct": q.get("chg_pct"),
            "score": r["score"],
            "resonance": r["resonance"],
            "bull": r["bull"], "bear": r["bear"],
            "pos20": r["pos20"],
            "industry": r.get("industry", ""),
            "mktcap": q.get("mktcap"),
            "pe": q.get("pe"),
            "turnover": q.get("turnover"),
            "signals": r.get("signals", []),
            "kline_date": r["date"],
        })

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y%m%d"),
        "rule": "engine_v5 17指标共振 + 实时行情补全",
        "anchor": f"score>={args.min_score} & 共振>={args.min_resonance} & 非ST/非退市 & 20日位置<=85%",
        "freq": "交易日盘中每30分钟 (hot-impact-realtime 工作流)",
        "global": {
            "n_scanned": len(klines),
            "n_scored": len(candidates),
            "n_kept": len(kept),
        },
        "picks": picks,
        "meta": {
            "engine": "v5 多指标共振 (17 指标)",
            "formula": "score = 50 + 50*(Σw·vote·strength/Σw)*(0.6+0.4*共振率)",
            "indicators": ["SuperTrend", "MA5/MA20", "RSI", "MACD", "KDJ", "BOLL",
                           "Donchian", "CMO", "Vortex", "UltimateOsc", "Choppiness",
                           "HullMA", "ElderForce", "ConnorsRSI", "A/D Line", "Pivot", "VWMA"],
            "quote_source": "腾讯 qt.gtimg.cn 实时",
            "industry_source": "TradingView industry_map (5309只)",
            "excludes": "ST/*ST/退市, 已涨停(追高风险), 20日位置>85%",
            "caveat": "多指标共振反映信号一致性, 不等于必然上涨; "
                      "历史胜率见 reflect_latest.json (当前样本不足以下定论)",
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n=== AI 推股 TOP10 ===")
    for p in picks[:10]:
        chg = f"{p['chg_pct']:+.2f}%" if p.get("chg_pct") is not None else "—"
        print(f"  {p['code']} {str(p['name'])[:8]:8s} 价={p['price']} {chg:>8s} "
              f"分={p['score']:5.1f} 共振={p['resonance']:.2f} "
              f"多{p['bull']}/空{p['bear']} {p['industry'][:8]}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
