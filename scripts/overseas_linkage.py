#!/usr/bin/env python3
"""
overseas_linkage.py — 海外市场关联性引擎

算清「谁跟谁联动、联多紧、跟多快、隔夜跟不跟」:
  1. 股票 vs 板块 / 股票 vs 大盘 (A股指数)
  2. 股票 vs 全球股市 (纳指/恒指/日经/KOSPI/费半 ...)
  3. 板块 vs 大盘 / 大盘 vs 全球股市 (大A 与 韩股/美股 ...)

对每一对 (target, driver) 输出:
  corr    皮尔逊相关系数 (日收益)
  beta    回归斜率 (target = alpha + beta * driver)
  r2      解释力
  lag     最优滞后天数 (-2..+2): +1 表示 driver 涨一天后 target 才跟
  overnight 隔夜跟涨系数: 用 driver 当日涨跌 预测 target 次日开盘缺口
  样本量、时间窗

数据源 (全部本地 JSON, 不发请求):
  data/index_kline.json     A股指数日K (sh000001 等)
  data/global_kline.json    全球指数/个股日K (symbols)
  data/klineshare_hq.json   全市场个股日K (若存在)
  data/latest.json          search_index (快照价) 兜底

用法:
  python3 overseas_linkage.py --out data/overseas_linkage.json
  python3 overseas_linkage.py --drivers usIXIC,hkHSI --targets sh000001
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA = os.path.join(HERE, "..", "data")

# 驱动市场 → 中文名 (报告里要可读)
DRIVER_NAMES = {
    "usIXIC": "纳斯达克", "usINX": "标普500", "usDJI": "道琼斯",
    "hkHSI": "恒生指数", "hkHSTECH": "恒生科技", "hkHSCEI": "国企指数",
    "usSOX": "费城半导体", "usNVDA": "英伟达", "usAAPL": "苹果",
    "usTSLA": "特斯拉", "usMSFT": "微软", "hk00700": "腾讯",
    "hk09988": "阿里",
}
TARGET_NAMES = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sh000300": "沪深300",
    "sz399006": "创业板指", "sh000688": "科创50",
}


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def bars_from_global(sym, doc):
    """global_kline.json: klines[code] = [{date, open, close, high, low, vol}]

    注意顶层键是 klines 不是 symbols; code 带交易所后缀
    (usDJI.OQ / usIXIC.OQ / hkHSI), 而 DRIVER_NAMES 用不带后缀的
    key (usDJI), 所以这里按「去掉后缀」和「原样」两种方式都试。
    """
    klines = (doc or {}).get("klines") or {}
    bars = None
    for key in (sym, sym + ".OQ"):
        if key in klines:
            bars = klines[key]
            break
    if bars is None:
        # 反向: sym 是不带后缀的简写, 扫一遍找前缀匹配
        for key, v in klines.items():
            if key.split(".")[0] == sym:
                bars = v
                break
    if not bars:
        return {}
    out = {}
    for b in bars:
        if not b or b.get("close") is None:
            continue
        out[str(b.get("date"))] = float(b["close"])
    return out


def bars_from_index(sym, doc):
    """index_kline.json: indices[sym] = [{date, close}]"""
    arr = ((doc or {}).get("indices") or {}).get(sym) or []
    out = {}
    for b in arr:
        if not b or b.get("close") is None:
            continue
        out[str(b.get("date"))] = float(b["close"])
    return out


def rets(px):
    """价格序列 -> 日收益序列 (按日期对齐后逐日算)"""
    dates = sorted(px.keys())
    out = {}
    for i in range(1, len(dates)):
        p0, p1 = px[dates[i - 1]], px[dates[i]]
        if p0 and p0 > 0:
            out[dates[i]] = (p1 - p0) / p0
    return out, dates


def pearson(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def linreg(xs, ys):
    """返回 (beta, alpha, r2)"""
    n = len(xs)
    if n < 8:
        return None, None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, None, None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    alpha = my - beta * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return beta, alpha, r2


def pair_stats(tgt_rets, drv_rets, tgt_dates, drv_dates):
    """对齐 target 与 driver 的日收益, 算同日内与各滞后的相关/贝塔。"""
    common = [d for d in tgt_dates if d in drv_rets and d in tgt_rets]
    if len(common) < 20:
        return None
    # 同日内
    xs = [drv_rets[d] for d in common]
    ys = [tgt_rets[d] for d in common]
    c0 = pearson(xs, ys)
    b0, a0, r20 = linreg(xs, ys)
    # 滞后: driver 前 k 日 预测 target 今日
    best_lag, best_c = 0, c0 if c0 is not None else -9
    lag_rows = {}
    for k in (-2, -1, 1, 2):
        # k=+1: driver 在 t-1, target 在 t
        pairs = [(drv_rets.get(tgt_dates[i - k] if 0 <= i - k < len(tgt_dates) else None),
                  tgt_rets[tgt_dates[i]])
                 for i in range(len(tgt_dates))
                 if tgt_dates[i] in tgt_rets]
        pairs = [(a, b) for a, b in pairs if a is not None]
        if len(pairs) < 20:
            continue
        ck = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        if ck is not None:
            lag_rows["lag%d" % k] = round(ck, 3)
            if abs(ck) > abs(best_c):
                best_c, best_lag = ck, k
    # 隔夜: driver 当日涨跌 → target 次日涨跌 (只对跨市场有意义)
    pairs = []
    for i in range(1, len(tgt_dates)):
        d_prev = tgt_dates[i - 1]
        if d_prev in drv_rets and tgt_dates[i] in tgt_rets:
            pairs.append((drv_rets[d_prev], tgt_rets[tgt_dates[i]]))
    on_c = pearson([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 20 else None
    on_b = linreg([p[0] for p in pairs], [p[1] for p in pairs])[0] if len(pairs) >= 20 else None
    return {
        "n": len(common),
        "corr": round(c0, 3) if c0 is not None else None,
        "beta": round(b0, 3) if b0 is not None else None,
        "r2": round(r20, 3) if r20 is not None else None,
        "best_lag": best_lag,
        "lag_corr": round(best_c, 3) if best_c is not None else None,
        "lag_rows": lag_rows,
        "overnight_corr": round(on_c, 3) if on_c is not None else None,
        "overnight_beta": round(on_b, 3) if on_b is not None else None,
    }


def strength_label(c, b):
    """关联强度分级 (双向解读: 高相关既是弹性也是风险传导)"""
    if c is None:
        return "未知", "样本不足"
    a = abs(c)
    if a >= 0.7:
        return "极强", "几乎同步: 涨跌都被对方主导, 分散不了风险"
    if a >= 0.5:
        return "强", "明显联动: 用对方做择时信号有效"
    if a >= 0.3:
        return "中", "有联动但自有逻辑更强"
    if a >= 0.15:
        return "弱", "基本独立, 偶发共振"
    return "极弱", "几乎无关"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA, "overseas_linkage.json"))
    ap.add_argument("--drivers", default="",
                    help="逗号分隔; 空=用 global_kline 全部 symbols")
    ap.add_argument("--targets", default="",
                    help="逗号分隔; 空=用 index_kline 全部指数")
    args = ap.parse_args()

    gk = load("global_kline.json")
    ik = load("index_kline.json")

    drivers = [d.strip() for d in args.drivers.split(",") if d.strip()]
    if not drivers:
        # 默认用 global_kline 里全部标的; 归一成不带后缀的简写
        # (usDJI.OQ -> usDJI) 以便对上 DRIVER_NAMES
        seen = []
        for code in ((gk.get("klines") or {}).keys()):
            short = code.split(".")[0]
            if short not in seen:
                seen.append(short)
        drivers = seen
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if not targets:
        targets = list((ik.get("indices") or {}).keys())

    # 预取收益序列
    drv_px = {d: bars_from_global(d, gk) for d in drivers}
    tgt_px = {t: bars_from_index(t, ik) for t in targets}
    drv_r, tgt_r = {}, {}
    for d in drivers:
        if drv_px[d]:
            drv_r[d] = rets(drv_px[d])
    for t in targets:
        if tgt_px[t]:
            tgt_r[t] = rets(tgt_px[t])

    rows = []
    for t in targets:
        if t not in tgt_r:
            continue
        tr, td = tgt_r[t]
        for d in drivers:
            if d not in drv_r or d == t:
                continue
            dret, dd = drv_r[d]
            st = pair_stats(tr, dret, td, dd)
            if not st:
                continue
            lvl, note = strength_label(st["corr"], st["beta"])
            rows.append({
                "target": t,
                "target_name": TARGET_NAMES.get(t, t),
                "driver": d,
                "driver_name": DRIVER_NAMES.get(d, d),
                "level": lvl,
                "note": note,
                **st,
            })

    rows.sort(key=lambda r: -abs(r["corr"] or 0))

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "overseas_linkage v1 (相关/贝塔/滞后/隔夜)",
        "n_targets": len([t for t in targets if t in tgt_r]),
        "n_drivers": len([d for d in drivers if d in drv_r]),
        "n_pairs": len(rows),
        "window": "共同交易日对齐; 最少 20 个样本",
        "method": {
            "corr": "皮尔逊相关系数 (日收益)",
            "beta": "target = alpha + beta*driver 的回归斜率",
            "r2": "回归解释力",
            "best_lag": "使 |corr| 最大的滞后天数 (+1=driver 涨一天后 target 才跟)",
            "overnight_corr": "driver 当日涨跌 与 target 次日涨跌 的相关 (跨市场传导)",
        },
        "caveat": "相关不等于因果; 高 beta 意味着下跌时也被放大。Research reference only.",
        "pairs": rows,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[linkage] -> %s (%d 对)" % (args.out, len(rows)))
    for r in rows[:12]:
        print("  %-8s ← %-10s corr=%-6s beta=%-6s lag=%+d 隔夜corr=%s" % (
            r["target_name"], r["driver_name"], r["corr"], r["beta"],
            r["best_lag"], r["overnight_corr"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
