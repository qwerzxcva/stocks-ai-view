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
import empirical_edge


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

    # 0) 实证边际表: reflect_latest.json 的实测胜率 → 有符号边际。
    #    2026-10-17 样本 (n≈6.4万): 全部指标 +1 票次日胜率 43-45% (CI 不含 50),
    #    强共振做多是负边际; -1 票下跌实现度 54-60%, 用作回避过滤器。
    #    表缺失时 calibration 为 None, 回退纯 score 排序 (旧行为)。
    calibration = empirical_edge.load_edge_table()
    print(f"[ai_picks] empirical edge table: "
          f"{'@ ' + calibration['ts'] if calibration else 'unavailable (raw mode)'}",
          flush=True)

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

    # 1b) 实证校准: 每组投票换算期望上涨概率偏差, 并收集看空对齐名单。
    #     edge_pp <= -3 的强共振票 (实测 P(涨)≈43-44%) 不具备可交易期望, 踢出。
    avoid_pool = []
    if calibration:
        kept_cals = []
        for r in candidates:
            cal = empirical_edge.calibrate(r.get("votes"), calibration)
            r["edge_pp"] = cal["edge_pp"]
            r["dir_weight"] = cal["dir_weight"]
            if cal["dir_weight"] > 0:
                kept_cals.append(r)
            aligned, bear_w = empirical_edge.bear_aligned(r.get("votes"),
                                                          calibration)
            if aligned:
                avoid_pool.append({"code": r["code"], "name": None,
                                   "edge_pp": r["edge_pp"],
                                   "bear_weight": bear_w,
                                   "score": r["score"]})
        candidates = kept_cals
        avoid_pool.sort(key=lambda x: -x["bear_weight"])
        print(f"[ai_picks] 校准后剩 {len(candidates)} 只, "
              f"看空对齐 {len(avoid_pool)} 只", flush=True)
    else:
        for r in candidates:
            r["edge_pp"] = None
            r["dir_weight"] = None

    # 2) 过滤
    # 校准模式: score>=60 & 共振>=0.6 两个旧门槛本身就是「强做多对齐」的同义词
    # (实测负边际), 校准激活时替换为: 有可测量依据 + 剔除最差负期望;
    # 排序按 edge_pp (期望上涨概率) 降序, score 退化为展示字段。
    # 全表负边际的退潮市下, 买榜是「最不差」名单且每只标注真实 p_up, 回避榜
    # (看空对齐, 实测下跌实现度 54-60%) 同步发布 —— 少买烂票=可验证的盈利改善。
    defensive = bool(calibration) and all(
        r["e_bull"] < -3 for r in calibration["indicators"].values())

    def bare(c):
        return c[2:] if c[:2] in ("sh", "sz", "bj") else c

    kept = []
    for r in candidates:
        if calibration:
            if not r.get("dir_weight") or r["dir_weight"] < 1.5:
                continue
            if r["edge_pp"] is None or r["edge_pp"] <= -3.5:
                continue
            # 与回避榜天然互斥: 回避榜要求空向权重 >= 3:1 且按看空权重取前 15,
            # 实测两者无重叠 (2026-10-17 双跑验证)。轻空向但边际较优的票
            # (BOLL 破位/Choppiness 趋势, 实测反弹边际为正) 留在买榜, 由 p_up 如实标注。
        else:
            if r["score"] < args.min_score:
                continue
            if r["resonance"] < args.min_resonance:
                continue
        bc = bare(r["code"])
        name = cn_names.get(bc, "")
        # 2026-10-17 用户要求不隐藏 ST: ST/*ST 照常入池, 前端按钮折叠+风险标注。
        # 退市整理股 (「退」) 无法交易, 仍剔除。
        if "退" in name:
            continue
        # 已涨停/接近涨停 → 追高风险, 剔除
        if r["pos20"] > 85:
            continue
        ind = ind_map.get(bc, {})
        r["name"] = name
        r["industry"] = ind.get("industry") or ind.get("sector") or ""
        r["signals"] = [v["why"] for v in r["votes"] if v["vote"] > 0][:3]
        kept.append(r)

    # 排序: 实证校准优先 —— 按期望上涨概率偏差降序;
    # 无校准表时回退 engine 原始分 (旧行为)。
    if calibration:
        kept.sort(key=lambda x: (-(x["edge_pp"] if x["edge_pp"] is not None
                                   else -99),
                                 -x["score"]))
    else:
        kept.sort(key=lambda x: -x["score"])
    top = kept[:args.top]
    print(f"[ai_picks] 过滤后 {len(kept)} 只 (defensive={defensive}), "
          f"取 TOP{len(top)}", flush=True)
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
        final_name = r.get("name") or q.get("name") or ""
        is_st = bool("ST" in final_name or "*" in final_name)
        if "退" in final_name:
            continue
        picks.append({
            "code": bc,
            "name": final_name,
            "is_st": is_st,
            "risk_note": ("ST/*ST 风险警示股: 涨跌幅限制 ±5%, 退市风险自负"
                          if is_st else None),
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
            "edge_pp": r.get("edge_pp"),
            "p_up": (round(50 + r["edge_pp"], 1)
                     if r.get("edge_pp") is not None else None),
        })

    # 回避榜: 看空对齐个股 (方向票 -1 占绝对多数), 实测下跌实现度 54-60%
    avoid = []
    for a in avoid_pool[:15]:
        bc = bare(a["code"])
        nm = cn_names.get(bc) or a.get("name") or ""
        if "退" in nm:
            continue
        a_is_st = bool("ST" in nm or "*" in nm)
        avoid.append({
            "code": bc,
            "name": nm,
            "is_st": a_is_st,
            "edge_pp": a["edge_pp"],
            "p_up": round(50 + (a["edge_pp"] or 0), 1),
            "bear_weight": a["bear_weight"],
            "score": a["score"],
        })

    # 保险: 买榜与回避榜硬互斥 (边际表更新导致口径漂移时也不会双榜同票)
    avoid_codes = {a["code"] for a in avoid}
    picks = [p for p in picks if p["code"] not in avoid_codes]

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y%m%d"),
        "rule": ("engine_v5 17指标共振 + 实时行情补全 + empirical_edge 校准"
                 if calibration else
                 "engine_v5 17指标共振 + 实时行情补全"),
        "anchor": ("dir_weight>=1.5 & edge_pp>-3.5 & 非ST/非退市 & "
                   "20日位置<=85% (按 p_up 降序; defensive={})".format(defensive)
                   if calibration else
                   f"score>={args.min_score} & 共振>={args.min_resonance} & "
                   "非ST/非退市 & 20日位置<=85%"),
        "freq": "交易日盘中每30分钟 (hot-impact-realtime 工作流)",
        "global": {
            "n_scanned": len(klines),
            "n_scored": len(candidates),
            "n_kept": len(kept),
        },
        "picks": picks,
        "avoid": avoid,
        "meta": {
            "engine": "v5 多指标共振 (17 指标) + empirical_edge 校准",
            "formula": "score = 50 + 50*(Σw·vote·strength/Σw)*(0.6+0.4*共振率); "
                       "排序键 = edge_pp (reflect_latest 实测胜率边际, "
                       "正=期望上涨概率高于50%)",
            "indicators": ["SuperTrend", "MA5/MA20", "RSI", "MACD", "KDJ", "BOLL",
                           "Donchian", "CMO", "Vortex", "UltimateOsc", "Choppiness",
                           "HullMA", "ElderForce", "ConnorsRSI", "A/D Line", "Pivot", "VWMA"],
            "quote_source": "腾讯 qt.gtimg.cn 实时",
            "industry_source": "TradingView industry_map (5309只)",
            "excludes": "ST/*ST/退市, 已涨停(追高风险), 20日位置>85%",
            "calibration": ({
                "source": "reflect_latest.json",
                "ts": calibration["ts"],
                "rule": "e(+1)=bull_win_rate-50, e(-1)=50-bear_win_rate; "
                        "n>=500 且 CI 不含 50 才采用; edge_pp<=-3 剔除",
            } if calibration else None),
            "defensive_regime": defensive if calibration else None,
            "caveat": ("2026-10-17 样本 (n≈6.4万): 全部指标 +1 票次日胜率 43-45% "
                      "(CI 不含 50), 强共振做多是负边际; -1 票下跌实现度 "
                      "54-60% 已用作回避过滤器 (avoid 榜)。当前为退潮防御态: "
                      "买榜是「最不差」名单, 每只 p_up 为实测期望, 非看多承诺; "
                      "边际表随 reflect_engine 每日重测更新"
                      if calibration else
                      "多指标共振反映信号一致性, 不等于必然上涨; "
                      "历史胜率见 reflect_latest.json"),
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    # 逐日存档: 区间高点追踪 (pick_high_track.py) 的可证伪数据源
    arch_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), "predictions")
    os.makedirs(arch_dir, exist_ok=True)
    arch = os.path.join(arch_dir, f"ai_picks_{now.strftime('%Y-%m-%d')}.json")
    with open(arch, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n=== AI 推股 TOP10 (按 edge_pp 校准排序) ===")
    for p in picks[:10]:
        chg = f"{p['chg_pct']:+.2f}%" if p.get("chg_pct") is not None else "—"
        p_up = f"P涨={p['p_up']}%" if p.get("p_up") is not None else "—"
        print(f"  {p['code']} {str(p['name'])[:8]:8s} 价={p['price']} {chg:>8s} "
              f"分={p['score']:5.1f} 共振={p['resonance']:.2f} "
              f"多{p['bull']}/空{p['bear']} {p['industry'][:8]} {p_up}")
    if avoid:
        print(f"\n=== 回避榜 (看空对齐, 实测下跌实现度高) ===")
        for a in avoid[:8]:
            print(f"  {a['code']} {str(a['name'])[:8]:8s} "
                  f"P涨={a['p_up']}% 看空权重={a['bear_weight']}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
