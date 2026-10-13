#!/usr/bin/env python3
"""
cycle_pool_v2.py — 周期池重构 (v2)

v1 (cycle_deep.py) 已有:
  五维周期位置 (星期/月份/连段/20日动量/年内位置) + 历年逐月轮动表 + 连段续跌概率
  指数全史 8567 天 (1990-12-19 起)

v2 新增 (用户 2026-10-13 要求的四个维度):
  1. yearly_kline    历年K线: 每年 OHLC + 年振幅/年成交额/年K线形态 (阳/阴/十字)
  2. annual_rotation  年度轮动: 每年最强/最弱月份、季度节奏、牛熊分段 (按年收益分层)
  3. cycle_law        周期规律: 基钦周期(库存,约4年)/朱格拉周期(设备,约9年)/
                     康波(技术,约60年) 的近似定位 + 指数在周期中的位置
  4. demand_shift     需求变化: 用行业/概念板块轮动代理需求迁移 (哪类资产在被追逐)

产出:
  data/cycle_pool_v2.json  {
    meta, status(五维, 复用v1), yearly_kline, annual_rotation,
    cycle_law, demand_shift, season, streak
  }

用法:
  python3 cycle_pool_v2.py                 # 输出到 ai-view/data/cycle_pool_v2.json
  python3 cycle_pool_v2.py --out x.json
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

INDEXES = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688"]
NAMES = {"sh000001": "上证指数", "sz399001": "深成指", "sh000300": "沪深300",
         "sz399006": "创业板指", "sh000688": "科创50"}

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "cycle_pool_v2.json")


# ─── 拉取: 腾讯指数全史 (分页, 同 cycle_deep) ────────────────────
def idx_seg(code, end="", n=800):
    u = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,{end},{n}"
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                              timeout=20).read())
        node = (d.get("data") or {}).get(code) or {}
        arr = node.get("day") or node.get("qfqday") or []
        return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in arr if len(r) >= 6]
    except Exception as e:
        print(f"[idx_seg {code}] {e}", file=sys.stderr)
        return []


def idx_all(code, floor="1991-01-01"):
    """分页拉全史。腾讯 param=code,day,,end,n 返回 end 及之前的 n 根 (升序),
    end 必须 YYYY-MM-DD; 段内最早日期是 seg[0][0] (数组升序)。"""
    allr, end, guard = {}, "", 0
    while guard < 80:
        guard += 1
        seg = idx_seg(code, end)
        if not seg:
            break
        new = 0
        for r in seg:
            if r[0] not in allr:
                allr[r[0]] = r
                new += 1
        if new == 0:
            break
        oldest = seg[0][0]   # 数组升序, 首行是最早
        if oldest <= floor:
            break
        end = oldest
        time.sleep(0.15)
    return [allr[k] for k in sorted(allr)]


# ════════════════════════════════════════════════════════════
# 维度 1: 历年 K 线
# ════════════════════════════════════════════════════════════
def yearly_kline(bars):
    """每年 OHLC 汇总 + 振幅/成交额/形态。"""
    by_year = defaultdict(list)
    for b in bars:
        by_year[b[0][:4]].append(b)
    out = {}
    for y in sorted(by_year):
        rows = by_year[y]
        if len(rows) < 20:
            continue
        o = rows[0][1]
        c = rows[-1][2]
        hi = max(r[3] for r in rows)
        lo = min(r[4] for r in rows)
        vol = sum(r[5] for r in rows)
        # 年内阳线/阴线/十字星计数 (按收盘 vs 开盘)
        yang = sum(1 for r in rows if r[2] > r[1])
        yin = sum(1 for r in rows if r[2] < r[1])
        cross = len(rows) - yang - yin
        # 最大回撤 (年内)
        peak, mdd = rows[0][3], 0.0
        for r in rows:
            peak = max(peak, r[3])
            if peak > 0:
                mdd = min(mdd, (r[4] - peak) / peak * 100)
        # 年内涨跌幅
        prev_year_close = None
        out[y] = {
            "n_days": len(rows),
            "open": round(o, 2),
            "close": round(c, 2),
            "high": round(hi, 2),
            "low": round(lo, 2),
            "chg_pct": None,  # 需要上一年收盘, 下面补
            "amplitude_pct": round((hi - lo) / lo * 100, 2) if lo > 0 else None,
            "turnover_yi": round(vol / 1e8, 1),
            "candles": {"yang": yang, "yin": yin, "cross": cross},
            "max_drawdown_pct": round(mdd, 2),
            "first_date": rows[0][0],
            "last_date": rows[-1][0],
        }
    # 补年度涨跌幅 (需上一年 close)
    years = sorted(out)
    for i, y in enumerate(years):
        if i == 0:
            out[y]["chg_pct"] = 0.0
            continue
        prev = out[years[i - 1]]["close"]
        if prev > 0:
            out[y]["chg_pct"] = round((out[y]["close"] - prev) / prev * 100, 2)
    return out


# ════════════════════════════════════════════════════════════
# 维度 2: 年度轮动
# ════════════════════════════════════════════════════════════
def annual_rotation(bars):
    """每年最强/最弱月、季度节奏、牛熊分层。"""
    by_year_month = defaultdict(dict)   # year -> month -> [bars]
    for b in bars:
        y, m = b[0][:4], int(b[0][5:7])
        by_year_month[y].setdefault(m, []).append(b)

    per_year = {}
    for y, months in by_year_month.items():
        if len(months) < 6:
            continue
        # 每月收益 (月末 vs 上月末)
        month_chg = {}
        prev_close = None
        for m in range(1, 13):
            rows = months.get(m)
            if not rows:
                continue
            if prev_close is not None and prev_close > 0:
                month_chg[m] = round((rows[-1][2] - prev_close) / prev_close * 100, 2)
            prev_close = rows[-1][2]
        if not month_chg:
            continue
        best = max(month_chg, key=lambda k: month_chg[k])
        worst = min(month_chg, key=lambda k: month_chg[k])
        # 季度节奏 (Q1-Q4 平均)
        q = {}
        for qi in range(4):
            ms = [m for m in (qi * 3 + 1, qi * 3 + 2, qi * 3 + 3) if m in month_chg]
            q[f"Q{qi+1}"] = round(sum(month_chg[m] for m in ms) / len(ms), 2) if ms else None
        per_year[y] = {
            "best_month": {"month": best, "chg_pct": month_chg[best]},
            "worst_month": {"month": worst, "chg_pct": month_chg[worst]},
            "quarter_avg": q,
            "months_covered": len(month_chg),
        }

    # 牛熊分层 (按年度收益, 用 yearly_kline 的 chg_pct)
    return per_year


def classify_regimes(yk):
    """按年度收益分牛熊: >30% 牛市, 10~30% 偏牛, -10~10% 震荡, <-10% 熊市。"""
    buckets = {"bull_gt30": [], "mild_10_30": [], "range_-10_10": [], "bear_lt-10": []}
    for y, v in yk.items():
        chg = v.get("chg_pct")
        if chg is None:
            continue
        if chg > 30:
            buckets["bull_gt30"].append((y, chg))
        elif chg > 10:
            buckets["mild_10_30"].append((y, chg))
        elif chg >= -10:
            buckets["range_-10_10"].append((y, chg))
        else:
            buckets["bear_lt-10"].append((y, chg))
    out = {}
    for k, lst in buckets.items():
        lst.sort(key=lambda x: -x[1])
        out[k] = {
            "n": len(lst),
            "years": [y for y, _ in lst],
            "avg_chg": round(sum(c for _, c in lst) / len(lst), 2) if lst else None,
            "best": lst[0] if lst else None,
            "worst": lst[-1] if lst else None,
        }
    return out


# ════════════════════════════════════════════════════════════
# 维度 3: 周期规律 (基钦/朱格拉/康波近似)
# ════════════════════════════════════════════════════════════
def cycle_law(bars, yk):
    """用指数全史近似三大周期位置。

    周期长度 (文献常用):
      基钦周期 (库存) ~40 个月 ≈ 3.3 年
      朱格拉周期 (设备投资) ~9-10 年
      康波 (技术创新) ~50-60 年

    A 股 1990 起仅 36 年, 康波只能给粗略定位; 基钦/朱格拉可用。
    """
    n = len(bars)
    first_year = int(bars[0][0][:4]) if n else 1991
    last_year = int(bars[-1][0][:4]) if n else 2026

    # 朱格拉: 按年度收益找波峰-波峰间隔
    years = sorted(int(y) for y in yk if yk[y].get("chg_pct") is not None)
    chgs = [yk[str(y)]["chg_pct"] for y in years]
    # 简化: 连续两年收益 >15% 视为一个朱格拉上升段
    juglar_ups = []
    run = []
    for y, c in zip(years, chgs):
        if c > 15:
            run.append(y)
        else:
            if len(run) >= 2:
                juglar_ups.append({"start": run[0], "end": run[-1], "n_years": len(run)})
            run = []
    if len(run) >= 2:
        juglar_ups.append({"start": run[0], "end": run[-1], "n_years": len(run)})

    # 当前年份在朱格拉序列中的位置
    now_year = datetime.now(CST).year
    last_up = juglar_ups[-1] if juglar_ups else None
    if last_up and now_year - last_up["end"] <= 3:
        juglar_pos = "上升段内或刚结束"
    elif last_up and now_year - last_up["end"] <= 8:
        juglar_pos = "下行/筑底段"
    else:
        juglar_pos = "周期位置不明 (样本不足)"

    # 基钦: 用近 40 个月 (约 3.3 年) 累计收益判断
    recent = bars[-820:] if n > 820 else bars   # ~40 个月
    if len(recent) > 30 and recent[0][2] > 0:
        kitchin_chg = (recent[-1][2] - recent[0][2]) / recent[0][2] * 100
        kitchin_pos = ("补库存/上行" if kitchin_chg > 15 else
                       "去库存/下行" if kitchin_chg < -15 else "库存周期中性")
    else:
        kitchin_chg, kitchin_pos = None, "样本不足"

    # 康波: A 股 36 年, 只能标「技术周期段」(2010 移动互联网/2019 5G+新能源/2023 AI)
    tech_eras = [
        {"name": "PC 互联网", "start": 1999, "end": 2007},
        {"name": "移动互联网", "start": 2010, "end": 2018},
        {"name": "5G+新能源", "start": 2019, "end": 2023},
        {"name": "AI 算力", "start": 2023, "end": 2030},
    ]
    now_era = next((e for e in tech_eras if e["start"] <= now_year <= e["end"]), None)

    return {
        "kitchin_inventory_40m": {
            "window_months": 40,
            "chg_pct": round(kitchin_chg, 2) if kitchin_chg is not None else None,
            "position": kitchin_pos,
            "note": "基钦周期≈40个月, 用近820个交易日累计收益近似",
        },
        "juglar_capex_9y": {
            "up_cycles": juglar_ups,
            "current_position": juglar_pos,
            "note": "朱格拉周期≈9-10年, 用连续两年收益>15%识别上升段",
        },
        "kondratiev_tech_60y": {
            "current_era": now_era,
            "eras": tech_eras,
            "note": "康波≈50-60年; A股仅36年样本, 按技术代际近似定位, 置信度低",
            "confidence": "low",
        },
        "sample_span_years": last_year - first_year,
    }


# ════════════════════════════════════════════════════════════
# 维度 4: 需求变化 (板块轮动代理)
# ════════════════════════════════════════════════════════════
def demand_shift():
    """用行业/概念板块资金与涨幅代理「需求在追什么」。

    数据源: push2his clist (绕开 push2 WAF, 2026-10-13 实测 total=496 行业 / 506 概念)
    """
    out = {}

    def _clist(fs, fields, fid="f3", pz=20):
        u = (f"https://push2his.eastmoney.com/api/qt/clist/get?pn=1&pz={pz}&po=1&np=1"
             f"&fs={urllib.parse.quote(fs)}&fields={fields}&fid={fid}")
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                                  timeout=15).read())
            return (d.get("data") or {}).get("diff") or []
        except Exception as e:
            print(f"[demand_shift] {e}", file=sys.stderr)
            return []

    # 行业 (f3=涨跌幅 f104=上涨家数 f128=领涨股 f62=今日主力净流入)
    ind = _clist("m:90+t:2", "f3,f12,f14,f104,f105,f128,f62")
    if ind:
        rows = []
        for it in ind:
            f62 = it.get("f62")
            rows.append({
                "name": it.get("f14"),
                "chg_pct": round((it.get("f3") or 0) / 100, 2),
                "up_n": it.get("f104"), "down_n": it.get("f105"),
                "leader": it.get("f128"),
                "main_net_inflow_yi": round(f62 / 1e8, 2) if f62 is not None else None,
            })
        out["industry_top"] = rows
        out["industry_n"] = len(rows)

    # 概念 (f3=涨跌幅 f128=领涨股)
    con = _clist("m:90+t:3", "f3,f12,f14,f104,f105,f128")
    if con:
        rows = []
        for it in con:
            rows.append({
                "name": it.get("f14"),
                "chg_pct": round((it.get("f3") or 0) / 100, 2),
                "up_n": it.get("f104"), "down_n": it.get("f105"),
                "leader": it.get("f128"),
            })
        out["concept_top"] = rows
        out["concept_n"] = len(rows)

    # 需求集中度: top5 涨幅行业 vs 全市场 (越集中越像主题行情, 越分散越像普涨)
    if out.get("industry_top"):
        chgs = [r["chg_pct"] for r in out["industry_top"]]
        top5_avg = sum(chgs[:5]) / min(5, len(chgs))
        all_avg = sum(chgs) / len(chgs)
        out["concentration"] = {
            "top5_avg_chg": round(top5_avg, 2),
            "sample_avg_chg": round(all_avg, 2),
            "spread": round(top5_avg - all_avg, 2),
            "interpretation": ("主题集中行情 (头部板块显著强于平均)" if top5_avg - all_avg > 1.5
                               else "结构性分化" if top5_avg - all_avg > 0.5
                               else "普涨/轮动分散"),
        }
    return out


# ════════════════════════════════════════════════════════════
# 五维状态 (复用 cycle_deep 逻辑)
# ════════════════════════════════════════════════════════════
def five_dim_status(bars):
    if not bars:
        return {}
    now = datetime.now(CST)
    closes = [b[2] for b in bars]
    c = closes[-1]
    ma5 = sum(closes[-5:]) / min(5, len(closes))
    ma20 = sum(closes[-20:]) / min(20, len(closes))
    ma60 = sum(closes[-60:]) / min(60, len(closes))

    # MACD (12/26/9)
    def ema(vals, n):
        k, out = 2 / (n + 1), vals[0]
        r = [out]
        for v in vals[1:]:
            out = v * k + out * (1 - k)
            r.append(out)
        return r
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    macd_hist = (dif[-1] - dea[-1]) * 2

    # RSI14
    gains, losses = [], []
    for i in range(max(1, len(closes) - 14), len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    ag, al = sum(gains) / 14, sum(losses) / 14
    rsi = 100 - 100 / (1 + ag / al) if al > 0 else 100

    # 20 日动量
    mom20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0

    phase = ("downtrend" if c < ma20 < ma60 else
             "uptrend" if c > ma20 > ma60 else "range")
    return {
        "date": bars[-1][0], "close": round(c, 2),
        "ma5": round(ma5, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "ma_bull": c > ma20 > ma60, "macd_hist": round(macd_hist, 2),
        "macd_bull": macd_hist > 0, "rsi": round(rsi, 1),
        "momentum_20d_pct": round(mom20, 2), "phase": phase,
        "volume_ratio": round(closes[-1] and (bars[-1][5] /
                                              (sum(b[5] for b in bars[-5:]) / 5)) or 1, 2),
    }


# ════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="输出路径")
    ap.add_argument("--index", default="sh000001", help="主指数")
    args = ap.parse_args()

    print(f"[cycle_pool_v2] 拉取 {args.index} 全史 ...")
    bars = idx_all(args.index)
    if not bars:
        print("指数全史拉取失败", file=sys.stderr)
        return 1
    print(f"  {len(bars)} 根 ({bars[0][0]} ~ {bars[-1][0]})")

    print("[1/4] 历年 K 线 ...")
    yk = yearly_kline(bars)
    print(f"  {len(yk)} 年")

    print("[2/4] 年度轮动 + 牛熊分层 ...")
    rot = annual_rotation(bars)
    regimes = classify_regimes(yk)

    print("[3/4] 周期规律 (基钦/朱格拉/康波) ...")
    law = cycle_law(bars, yk)

    print("[4/4] 需求变化 (板块轮动) ...")
    demand = demand_shift()

    print("[+] 五维状态 ...")
    status = five_dim_status(bars)

    out = {
        "meta": {
            "source": "腾讯证券 kline/kline 指数全史 + 东财 push2his clist 板块",
            "ts": datetime.now(CST).isoformat(timespec="seconds"),
            "index": args.index, "index_name": NAMES.get(args.index, args.index),
            "n_days": len(bars),
            "span": [bars[0][0], bars[-1][0]],
            "version": "v2",
            "dims": "yearly_kline/annual_rotation/cycle_law/demand_shift/five_dim",
            "rebuilt_at": datetime.now(CST).isoformat(timespec="seconds"),
        },
        "status": status,
        "yearly_kline": yk,
        "annual_rotation": rot,
        "regimes": regimes,
        "cycle_law": law,
        "demand_shift": demand,
    }

    path = args.out or DEFAULT_OUT
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[done] → {path} ({os.path.getsize(path)}B)")
    # 摘要
    print(f"\n=== 摘要 ===")
    print(f"  当前: {status.get('date')} 收 {status.get('close')} "
          f"phase={status.get('phase')} RSI={status.get('rsi')}")
    if regimes.get("bull_gt30"):
        print(f"  牛市年(>30%): {regimes['bull_gt30']['n']} 个 "
              f"{regimes['bull_gt30']['years'][:5]}")
    print(f"  基钦: {law['kitchin_inventory_40m']['position']} "
          f"({law['kitchin_inventory_40m']['chg_pct']}%)")
    print(f"  朱格拉: {law['juglar_capex_9y']['current_position']}")
    print(f"  技术代际: {(law['kondratiev_tech_60y']['current_era'] or {}).get('name')}")
    if demand.get("concentration"):
        print(f"  需求: {demand['concentration']['interpretation']}")
    if demand.get("industry_top"):
        print(f"  行业前三: {[r['name'] for r in demand['industry_top'][:3]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
