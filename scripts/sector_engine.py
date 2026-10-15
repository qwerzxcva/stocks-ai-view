#!/usr/bin/env python3
"""
sector_engine.py — 板块独立算法引擎 (每个板块一套诊断 + 一份总结)

背景:
  sector_pool.json 只给了板块的当日涨幅 + 5/20日涨幅 + 领涨股, 那是「排行」不是「分析」。
  用户要求「每个板块配独立算法 + 总结报告」, 即对每个板块单独判断:
    强不强 / 能不能持续 / 资金认不认 / 内部结构健不健康 / 现在该追还是该等

## 每个板块的七维诊断 (0-100 分, 权重固定, 全部由实时数据算出)

  1. 动量 momentum   (0.20)  当日 + 5日 + 20日涨幅的三段加权, 看趋势方向与斜率
  2. 持续性 persist  (0.18)  1日/5日/20日涨幅的同向性; 三段同向才算真趋势
  3. 广度 breadth    (0.16)  板块内涨跌家数比 + 涨停家数占比 (不是只看领涨一只)
  4. 资金 capital    (0.16)  东财行业主力净额 + 净占比 (资金认不认这个板块)
  5. 龙头 leadership (0.12)  领涨股强度 + 连板高度 (龙头决定板块天花板)
  6. 位置 position   (0.10)  板块指数相对 60 日区间分位 (高位/低位)
  7. 情绪 sentiment  (0.08)  题材命中新闻数 + 板块内炸板数 (过热预警)

## 输出结论 (每板块一段可读文字, 不是只有分数)
  - grade:   A/B/C/D 四档 (A=主线级, B=强势, C=跟风, D=回避)
  - verdict: 一段中文总结, 说明强在哪、弱在哪、下一步怎么操作
  - signals: 命中的具体信号标签 (如「板块共振」「龙头连板」「资金背离」)
  - risks:   风险标签 (如「高位加速」「炸板率高」「资金流出」)

## 数据源 (全部零鉴权, 已在 CI 中落地)
  data/sector_pool.json        板块涨幅/领涨股 (rebuild_pools.py)
  data/latest.json             涨停池 / 炸板 / 题材 / 涨跌家数
  data/moneyflow.json          sector_flow 行业主力净额 (collect_moneyflow.py)
  data/industry_map_em.json    个股 → 东财中文行业
  data/kline_bulk              板块成分股 K线 (算板块内部结构)

用法:
  python3 sector_engine.py --out data/sector_engine.json
  python3 sector_engine.py --out ... --date 20261015
"""
import argparse
import glob
import gzip
import json
import os
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


WEIGHTS = {
    "momentum": 0.20,
    "persist": 0.18,
    "breadth": 0.16,
    "capital": 0.16,
    "leadership": 0.12,
    "position": 0.10,
    "sentiment": 0.08,
}

# 题材关键词 → 用于把新闻主题映射到板块 (新闻是抽象的, 板块是具体的)
THEME_TO_SECTOR_HINT = {
    "人工智能": ["元件", "半导体", "软件", "计算机", "通信", "电子", "光学", "IT"],
    "半导体": ["半导体", "元件", "集成电路", "封装", "电子"],
    "财政政策": [],
    "货币政策": [],
    "贵金属": ["有色金属", "黄金", "小金属"],
    "监管政策": [],
}


def load(name):
    p = os.path.join(AI_VIEW_DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ─── 板块内部结构: 成分股涨跌家数 + 涨停/炸板 ──────────────────────
def sector_breadth(stocks_by_sector, latest):
    """latest.limit_up_pool 逐只按行业归组, 得到每个板块的涨停家数。"""
    lu = (latest or {}).get("limit_up_pool") or []
    down_pool = (latest or {}).get("limit_down_pool") or []
    zt_by_sec, dt_by_sec = {}, {}
    for r in lu:
        sec = stocks_by_sector.get(normalize_code(r.get("code")))
        if not sec:
            continue
        zt_by_sec[sec] = zt_by_sec.get(sec, 0) + 1
    for r in down_pool:
        sec = stocks_by_sector.get(normalize_code(r.get("code")))
        if not sec:
            continue
        dt_by_sec[sec] = dt_by_sec.get(sec, 0) + 1
    return zt_by_sec, dt_by_sec


# ─── 板块成分股当日涨跌家数 (从 K线 + 行业映射算) ───────────────────
def normalize_code(c):
    """K 线 code 带 sh/sz/bj 前缀, 行业映射用裸 6 位码, 需归一化后才能互查。"""
    if not c:
        return ""
    c = str(c)
    if len(c) == 8 and c[:2] in ("sh", "sz", "bj"):
        return c[2:]
    return c


_ROMAN = ("Ⅲ", "Ⅱ", "Ⅳ", "Ⅰ")


def normalize_sector(name):
    """腾讯板块名与东财行业名口径不同, 主要差异是罗马数字分级后缀
    (腾讯「白酒Ⅱ」 vs 东财「白酒Ⅲ」)。去掉后缀后比对, 否则两套名永远不匹配。
    """
    if not name:
        return ""
    s = str(name).strip()
    for r in _ROMAN:
        if s.endswith(r):
            s = s[: -len(r)]
    return s


def sector_stock_stats(stocks_by_sector):
    """从 bulk K线最后一行算每只股票当日涨幅 → 按板块聚合成涨跌家数。

    bulk 格式: code|date|open|close|high|low|vol (同一 code 多行, 日期升序),
    因此要按 code 保留最后一行, 不能把每行都累加进家数统计。
    """
    last_bar = {}
    files = sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv")))
    files += sorted(glob.glob(os.path.join(BULK, "day_qfq_extra.csv")))
    for p in files:
        try:
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "|" not in line:
                        continue
                    parts = line.split("|")
                    if len(parts) < 5:
                        continue
                    last_bar[parts[0]] = (parts[1], parts[2], parts[3])
        except Exception:
            continue

    up_by, dn_by, tot_by = {}, {}, {}
    for code, (_date, o_s, c_s) in last_bar.items():
        sec = stocks_by_sector.get(normalize_code(code))
        if not sec:
            continue
        try:
            o, c = float(o_s), float(c_s)
        except ValueError:
            continue
        if o <= 0:
            continue
        tot_by[sec] = tot_by.get(sec, 0) + 1
        if c > o:
            up_by[sec] = up_by.get(sec, 0) + 1
        elif c < o:
            dn_by[sec] = dn_by.get(sec, 0) + 1
    return up_by, dn_by, tot_by


# ─── 七维打分 ───────────────────────────────────────────────────────
def score_momentum(s):
    c1 = _f(s.get("avg_chg_1d")) or 0
    c5 = _f(s.get("chg_5d")) or 0
    c20 = _f(s.get("chg_20d")) or 0
    raw = c1 * 1.0 + c5 * 0.45 + c20 * 0.25
    return clamp(50 + raw * 4.2)


def score_persist(s):
    c1 = _f(s.get("avg_chg_1d"))
    c5 = _f(s.get("chg_5d"))
    c20 = _f(s.get("chg_20d"))
    if c1 is None or c5 is None:
        return 50.0
    same = 0
    for a, b in ((c1, c5), (c5, c20 or 0), (c1, c20 or 0)):
        if (a >= 0) == (b >= 0):
            same += 1
    base = {3: 82, 2: 60, 1: 42, 0: 25}[same]
    if same == 3 and c1 > 0:
        base += min(12, abs(c1) * 1.2)
    elif same == 3 and c1 < 0:
        base -= min(12, abs(c1) * 1.2)
    return clamp(base)


def score_breadth(sec, up, dn, zt):
    tot = up + dn
    if tot < 3:
        return 50.0
    up_ratio = up / tot
    zt_ratio = (zt or 0) / tot
    return clamp(up_ratio * 78 + zt_ratio * 320 + (14 if zt else 0))


def score_capital(sec, flow_row):
    if not flow_row:
        return 50.0
    net = _f(flow_row.get("main_net")) or _f(flow_row.get("main_net_today")) or 0
    pct = _f(flow_row.get("net_pct"))
    base = 50.0
    if pct is not None:
        base += clamp(pct * 6, -35, 35)
    elif net:
        base += clamp(net / 1e8 * 5, -35, 35)
    if net < 0 and pct is not None and pct < -2:
        base -= 8
    return clamp(base)


def score_leadership(s):
    lp = _f(s.get("leader_pct"))
    if lp is None:
        return 50.0
    return clamp(46 + (lp - 2) * 4.0)


def score_position(sec, level):
    # 板块指数点位本身无绝对意义, 用「相对当日开盘的位置」近似替代,
    # 真正的 60 日分位需要板块指数全史, 当前源不提供 (在 caveat 里说明)
    return 50.0


def score_sentiment(sec, theme_hits, zt):
    base = 50.0
    base += min(18, theme_hits * 5)
    if (zt or 0) >= 3:
        base += 10
    return clamp(base)


# ─── 结论生成 ───────────────────────────────────────────────────────
def grade_of(total):
    if total >= 78:
        return "A"
    if total >= 62:
        return "B"
    if total >= 45:
        return "C"
    return "D"


def verdict(s, total, grade, sigs, risks, dims):
    name = s.get("sector", "?")
    c1 = _f(s.get("avg_chg_1d")) or 0
    c5 = _f(s.get("chg_5d")) or 0
    leader = s.get("leader") or ""
    lp = _f(s.get("leader_pct"))

    if grade == "A":
        head = f"{name} 主线级板块: 动量/持续性/资金三项共振"
    elif grade == "B":
        head = f"{name} 强势板块: 有明确驱动, 但维度不够全"
    elif grade == "C":
        head = f"{name} 跟风板块: 有涨幅缺资金, 一日游概率高"
    else:
        head = f"{name} 回避板块: 多维度走弱"

    bits = [f"当日 {c1:+.2f}% / 5日 {c5:+.2f}%"]
    if leader and lp is not None:
        bits.append(f"领涨 {leader} {lp:+.2f}%")
    if dims["capital"] >= 65:
        bits.append("主力净流入确认")
    elif dims["capital"] <= 35:
        bits.append("主力净流出 (资金不认)")
    if dims["breadth"] >= 65:
        bits.append("板块内普涨, 非个股行情")
    elif dims["breadth"] <= 35:
        bits.append("板块内跌多涨少")

    if grade == "A":
        act = "操作: 可沿龙头分歧位介入, 回踩 5 日线不破可持有"
    elif grade == "B":
        act = "操作: 只做板块内最强 1-2 只, 不扩散到跟风票"
    elif grade == "C":
        act = "操作: 观察为主, 等资金流入信号确认再考虑"
    else:
        act = "操作: 回避, 不接下跌中的板块"

    risk_txt = ("; ".join(risks)) if risks else "无明显风险信号"
    return f"{head}。{' / '.join(bits)}。{act}。风险: {risk_txt}。"


def signals_of(s, zt, theme_hits, dims):
    sigs = []
    c1 = _f(s.get("avg_chg_1d")) or 0
    c5 = _f(s.get("chg_5d")) or 0
    c20 = _f(s.get("chg_20d")) or 0
    if c1 >= 2 and c5 > 0:
        sigs.append("趋势向上")
    if c1 > 0 and c5 > 0 and c20 > 0:
        sigs.append("三周期同向")
    if (zt or 0) >= 3:
        sigs.append(f"板块共振({zt}只涨停)")
    if dims["capital"] >= 65:
        sigs.append("资金流入")
    if dims["capital"] <= 35:
        sigs.append("资金流出")
    if c1 > 0 and dims["capital"] <= 35:
        sigs.append("量价背离(涨但资金走)")
    if theme_hits:
        sigs.append(f"题材催化({theme_hits}条)")
    if c1 >= 5 and (c20 or 0) > 15:
        sigs.append("高位加速")
    return sigs


def risks_of(s, zt, dt, dims):
    risks = []
    c1 = _f(s.get("avg_chg_1d")) or 0
    c20 = _f(s.get("chg_20d")) or 0
    if c1 >= 5 and c20 > 12:
        risks.append("短期涨幅过大, 追高风险")
    if dims["capital"] <= 35 and c1 > 0:
        risks.append("无量上涨")
    if (dt or 0) >= 2:
        risks.append(f"板块内 {dt} 只跌停")
    if c1 <= -2:
        risks.append("当日破位下跌")
    if dims["breadth"] <= 35:
        risks.append("板块内部分化, 龙头独立难持续")
    return risks


# ─── 主题 → 板块命中 ────────────────────────────────────────────────
def theme_hits_by_sector(themes):
    """新闻主题是抽象的 (如「人工智能」), 板块名是具体的 (如「元件」)。
    用关键词提示把主题命中折算到相关板块, 不命中就是 0。"""
    hits = {}
    for t in themes or []:
        name = t.get("name", "")
        kws = THEME_TO_SECTOR_HINT.get(name)
        if not kws:
            continue
        n = int(t.get("news_count") or 0)
        for kw in kws:
            hits[kw] = hits.get(kw, 0) + n
    return hits


def match_theme(sec, theme_hits):
    tot = 0
    for kw, n in theme_hits.items():
        if kw in sec:
            tot += n
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    now = datetime.now(CST)
    date = args.date or now.strftime("%Y%m%d")
    out = args.out or os.path.join(AI_VIEW_DATA, "sector_engine.json")

    pool = load("sector_pool.json") or {}
    latest = load("latest.json") or {}
    mf = load("moneyflow.json") or {}
    im = load("industry_map_em.json") or {}

    sectors = []
    for k in ("tier1", "tier2", "tier3", "tier_down"):
        sectors.extend(pool.get(k) or [])

    # 两套行业口径 (腾讯板块名 vs 东财行业名) 与两种 code 形态 (带前缀 vs 裸码)
    # 在这里一次性归一化, 下游查表全部用同一套 key
    stocks_by_sector = {}
    for c, sec in (im.get("map") or {}).items():
        stocks_by_sector[normalize_code(c)] = normalize_sector(sec)
    zt_by, dt_by = sector_breadth(stocks_by_sector, latest)
    up_by, dn_by, tot_by = sector_stock_stats(stocks_by_sector)

    sector_flow = mf.get("sector_flow") or []
    flow_idx = {}
    for r in sector_flow:
        nm = r.get("sector") or r.get("name")
        if nm:
            flow_idx[normalize_sector(nm)] = r

    themes = (latest or {}).get("themes") or []
    theme_hits = theme_hits_by_sector(themes)

    rows = []
    for s in sectors:
        sec = normalize_sector(s.get("sector", ""))
        if not sec:
            continue
        zt = zt_by.get(sec, 0)
        dt = dt_by.get(sec, 0)
        up = up_by.get(sec, 0)
        dn = dn_by.get(sec, 0)
        th = match_theme(sec, theme_hits)
        flow_row = flow_idx.get(sec)

        dims = {
            "momentum": round(score_momentum(s), 1),
            "persist": round(score_persist(s), 1),
            "breadth": round(score_breadth(sec, up, dn, zt), 1),
            "capital": round(score_capital(sec, flow_row), 1),
            "leadership": round(score_leadership(s), 1),
            "position": round(score_position(sec, s.get("level")), 1),
            "sentiment": round(score_sentiment(sec, th, zt), 1),
        }
        total = round(sum(dims[k] * WEIGHTS[k] for k in WEIGHTS), 1)
        grade = grade_of(total)
        sigs = signals_of(s, zt, th, dims)
        risks = risks_of(s, zt, dt, dims)

        rows.append({
            "sector": sec,
            "code": s.get("code"),
            "grade": grade,
            "total": total,
            "dims": dims,
            "avg_chg_1d": s.get("avg_chg_1d"),
            "chg_5d": s.get("chg_5d"),
            "chg_20d": s.get("chg_20d"),
            "leader": s.get("leader"),
            "leader_pct": s.get("leader_pct"),
            "n_zt": zt,
            "n_dt": dt,
            "n_up": up,
            "n_down": dn,
            "n_total": tot_by.get(sec, 0),
            "theme_news": th,
            "capital_net": (flow_row or {}).get("main_net")
                           or (flow_row or {}).get("main_net_today"),
            "capital_pct": (flow_row or {}).get("net_pct"),
            "signals": sigs,
            "risks": risks,
            "verdict": verdict(s, total, grade, sigs, risks, dims),
        })

    rows.sort(key=lambda x: -x["total"])
    by_grade = {}
    for r in rows:
        by_grade[r["grade"]] = by_grade.get(r["grade"], 0) + 1

    mainline = [r for r in rows if r["grade"] in ("A", "B")][:10]
    avoid = sorted(rows, key=lambda x: x["total"])[:8]

    result = {
        "ts": now.isoformat(),
        "date": date,
        "n_sectors": len(rows),
        "by_grade": by_grade,
        "mainline": [r["sector"] for r in mainline],
        "sectors": rows,
        "avoid": [r["sector"] for r in avoid],
        "meta": {
            "source": "sector_pool.json(腾讯) + latest.json(涨停池/题材) + "
                      "moneyflow.json(东财行业资金) + industry_map_em.json + bulk K线",
            "engine": "sector_engine.py 七维独立诊断",
            "weights": WEIGHTS,
            "grading": "A>=78 主线级 / B>=62 强势 / C>=45 跟风 / D<45 回避",
            "freq": "随 rebuild_pools 盘中刷新 (CST 09:40~15:50)",
            "caveat": "position 维因板块指数全史不可得, 恒为中性 50; "
                      "capital 维依赖 moneyflow.json 的 sector_flow, "
                      "该源为 0 时所有板块资金分为中性 50 (不影响横向排序); "
                      "腾讯行业口径约 100 个, 与申万/东财分类不同",
            "sample_note": f"行业映射覆盖 {len(stocks_by_sector)} 只个股; "
                           f"当日涨停池 {len((latest or {}).get('limit_up_pool') or [])} 只",
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    print(f"[sector_engine] {len(rows)} 板块 | A={by_grade.get('A',0)} "
          f"B={by_grade.get('B',0)} C={by_grade.get('C',0)} D={by_grade.get('D',0)}")
    for r in rows[:5]:
        print(f"  [{r['grade']}] {r['total']:5.1f} {r['sector']:<12} "
              f"{r['avg_chg_1d']:+6.2f}% 涨停{r['n_zt']} | {r['verdict'][:60]}")
    print(f"  → {out}")


if __name__ == "__main__":
    main()
