#!/usr/bin/env python3
"""
multidim_engine.py — 多维因子引擎 (在 engine_v5 技术面之上叠加 8 个维度)

解决的核心问题:
  engine_v5 只有 17 个纯技术指标, 用户要求覆盖:
    基本面 / 消息面 / 资金面 / 技术面 / 情绪 / 政策面 / 板块面 / 市场面 /
    周期面 / 财报 / 新闻 / 涨跌高低位 / 暗盘资金 / 热点关联 / 高位套人 / 低位吸筹

维度与数据源 (全部零鉴权, 已有采集管道):
  技术面   engine_v5 17 指标共振分 (已有)
  资金面   东财 push2his fflow 个股资金流 (主力/超大单/大单/中单/小单)
           暗盘资金: 主力净流入 / 流通市值 (强度归一) + 连续性
  高低位   腾讯前复权K线: 20/60日区间分位 + 乖离 MA20
           高位套人: 位置>85% 且 乖离>15% (追高易套)
           低位吸筹: 位置<20% 且 主力净流入>0 且 RSI<40
  基本面   腾讯快照: PE/PB/总市值/流通市值 (已有 enrich)
  板块面   sector_pool.json tier1/tier2 (个股所属板块当日强度)
  市场面   market_now.json 涨跌家数/涨停数/成交额/赚钱效应
  情绪面   ban_prob.json 晋级率/封板率 + hot_impact 主题热度
  周期面   cycle_deep.json 五维打分 + 月度季节性
  消息面   news_digest 主题命中 + hot_impact 热点关联
  政策面   新闻 theme_of 命中政策关键词

输出:
  data/multidim.json — 每只股票的 8 维得分 + 综合分 + 因子明细
  (兼容 engine_v5.json 的 top 结构, 前端可直接渲染)

用法:
  python3 multidim_engine.py --out data/multidim.json --limit 300
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v5 as E
from paths import BULK, AI_VIEW_DATA

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://data.eastmoney.com/"}


# ─── 数据加载 ─────────────────────────────────────────────────────
def load_json(name, default=None):
    p = os.path.join(AI_VIEW_DATA, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def load_quote_index():
    """腾讯全市场快照 {裸码: {price, chg_pct, pe, pb, mktcap, ...}}"""
    out = {}
    from paths import SNAPSHOT_GLOB
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if not snaps:
        return out
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
    except Exception:
        return out
    for sym, rec in d.items():
        bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        out[bare] = rec
    return out


def load_sector_map():
    """个股 → 行业 (优先东财中文行业) + 板块强度/资金 (sector_pool + sector_moneyflow)

    历史坑: 原先用 industry_map_tv.json (TradingView 英文行业, 如
    "Electrical Products") 去查 sector_pool.json 的中文板块名 (如「油服工程」),
    两者永不匹配, dim_sector() 恒返回 None, 板块面被填成占位分 50。
    现在优先用 build_industry_map_em.py 产出的中文映射。
    """
    ind = {}
    # 1) 东财中文行业 (build_industry_map_em.py)
    em_map = load_json("industry_map_em.json") or {}
    for bare, name in (em_map.get("map") or {}).items():
        if name:
            ind[bare] = name
    n_em = len(ind)
    # 2) TradingView 英文行业兜底 (覆盖东财没收录的)
    from paths import INDUSTRY_MAP
    im = load_json_path(INDUSTRY_MAP) or {}
    for bare, v in (im.items() if isinstance(im, dict) else []):
        if isinstance(v, dict) and bare not in ind:
            ind[bare] = v.get("industry") or v.get("sector") or ""
    # 板块强度: sector_pool 的 tier 打分
    sp = load_json("sector_pool.json") or {}
    sector_strength = {}
    for tier in ("tier1", "tier2"):
        for s in sp.get(tier, []):
            sector_strength[s.get("sector", "")] = s.get("score", 0)
    # 板块资金: 主力净额(亿) — 有资金流入的板块额外加权
    smf = ((load_json("latest.json") or {}).get("sector_moneyflow") or {}).get("data") or []
    sector_money = {}
    for x in smf:
        nm = x.get("name")
        if nm:
            sector_money[nm] = {
                "main_net_yi": x.get("main_net_yi"),
                "net_pct": x.get("net_pct"),
                "chg": x.get("changePct"),
            }
    return ind, sector_strength, sector_money, n_em


def load_json_path(p):
    if not p or not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


# ─── 资金面 (东财个股资金流) ─────────────────────────────────────
def fetch_fflow(secid, lmt=10):
    """个股资金流日K。返回 [{date, main, super, large, medium, small}]"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={secid}&lmt={lmt}&klt=101"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=10).read()
        d = json.loads(body.decode("utf-8", "replace"))
        kl = (d.get("data") or {}).get("klines") or []
        out = []
        for s in kl:
            p = s.split(",")
            if len(p) >= 5:
                out.append({"date": p[0], "main": float(p[1]), "super": float(p[2]),
                            "large": float(p[3]), "medium": float(p[4]),
                            "small": float(p[5]) if len(p) > 5 else 0.0})
        return out
    except Exception:
        return []


def secid_of(bare):
    if bare.startswith(("60", "68", "90")):
        return "1." + bare
    return "0." + bare


# ─── 维度打分 (每个返回 0-100 分 + 说明) ─────────────────────────
def dim_capital(flow, mktcap):
    """资金面 + 暗盘资金: 主力净流入强度 + 连续性"""
    if not flow:
        return None, "无资金流数据"
    recent = flow[-5:]
    main_sum = sum(f["main"] for f in recent)
    super_sum = sum(f["super"] for f in recent)
    pos_days = sum(1 for f in recent if f["main"] > 0)
    cap = (mktcap or 1e10)
    intensity = main_sum / cap * 100          # 主力净流入占市值%
    score = 50
    if intensity > 1.0:
        score += 25
    elif intensity > 0.3:
        score += 15
    elif intensity < -1.0:
        score -= 25
    elif intensity < -0.3:
        score -= 12
    score += (pos_days - 2.5) * 6              # 连续性 ±
    return max(0, min(100, score)), (f"5日主力{main_sum/1e8:+.2f}亿 "
                                     f"({intensity:+.2f}%市值) 净流入{pos_days}/5天")


def dim_position(bars, rsi):
    """涨跌高低位: 20/60日分位 + 乖离; 高位套人/低位吸筹 标记"""
    if len(bars) < 60:
        return None, "K线不足"
    # bars 来自 engine_v5.load_klines: [date, open, close, high, low, vol]
    closes = [float(b[2]) for b in bars]
    last = closes[-1]
    w20 = closes[-20:]
    w60 = closes[-60:]
    pos20 = (last - min(w20)) / max(1e-9, (max(w20) - min(w20))) * 100
    pos60 = (last - min(w60)) / max(1e-9, (max(w60) - min(w60))) * 100
    ma20 = sum(closes[-20:]) / 20
    bias = (last / ma20 - 1) * 100 if ma20 else 0
    score = 50
    tag = ""
    if pos20 > 85 and bias > 15:
        score = 15
        tag = "⚠️高位套人"
    elif pos60 < 20 and bias > -8 and (rsi or 50) < 40:
        score = 85
        tag = "💎低位吸筹"
    elif pos20 < 25:
        score = 70
        tag = "低位区"
    elif pos20 > 80:
        score = 30
        tag = "高位区"
    else:
        score = 50 + (50 - pos20) * 0.2
        tag = f"位置{pos20:.0f}%"
    return max(0, min(100, score)), f"20日位{pos20:.0f}% 60日位{pos60:.0f}% 乖离{bias:+.1f}% {tag}"


def dim_fundamental(quote):
    """基本面: PE/PB/市值"""
    if not quote:
        return None, "无快照"
    pe = quote.get("pe")
    pb = quote.get("pb")
    mc = quote.get("mktcap") or 0
    score = 50
    if pe is not None and pe > 0:
        if pe < 15:
            score += 20
        elif pe < 30:
            score += 10
        elif pe > 80:
            score -= 15
    if pb is not None and pb > 0:
        if pb < 2:
            score += 10
        elif pb > 8:
            score -= 10
    if mc:
        if mc > 5e11:
            score += 8
        elif mc > 1e11:
            score += 5
        elif mc < 5e10:
            score -= 5
    return max(0, min(100, score)), f"PE={pe if pe is not None else '—'} PB={pb if pb is not None else '—'} 市值{(mc or 0)/1e8:.0f}亿"


def dim_sector(industry, sector_strength, sector_money=None):
    """板块面: 所属板块当日强度 + 主力资金净额

    强度分来自 sector_pool 的 tier 打分; 资金分来自 sector_moneyflow
    的主力净额(亿)。两者都没有才退回 50 (不再是恒定 50 — 中文行业映射
    接上后绝大多数股票都能命中)。
    """
    sector_money = sector_money or {}
    if not industry:
        return None, "无行业映射"
    s = sector_strength.get(industry)
    if s is None:
        # 模糊匹配 (包含关系): 「白酒Ⅲ」vs「白酒」这类命名差异
        for k, v in sector_strength.items():
            if k and (k in industry or industry in k):
                s = v
                break
    m = sector_money.get(industry)
    if m is None:
        for k, v in sector_money.items():
            if k and (k in industry or industry in k):
                m = v
                break
    # 强度分: 50 ± 强度*8 (上限 ±40)
    if s is not None:
        score = 50 + min(40, s * 8)
        why = "板块[%s] 强度%+.2f%%" % (industry, s)
    else:
        score = 50
        why = "板块[%s] 非热点梯队" % industry
    # 资金分: 主力净额每 1 亿元 +2 分, 上限 ±15; 净占比也参与
    if m and m.get("main_net_yi") is not None:
        bonus = max(-15, min(15, m["main_net_yi"] * 2))
        if m.get("net_pct") is not None:
            bonus += max(-5, min(5, m["net_pct"] * 0.5))
        score += bonus
        why += " · 主力净额%+.2f亿" % m["main_net_yi"]
    elif s is None:
        # 既无梯队强度也无资金数据, 才真的退回中性
        return 50, "板块[%s] 无强度/资金数据" % industry
    return max(0, min(100, score)), why


def dim_overseas(linkage_rows, global_markets, industry):
    """海外面: 个股所属板块/大盘 与 海外市场的联动强度 + 隔夜海外情绪

    这不是「海外涨了就加分」的单向判断, 而是两步:
      1) 找出与该标的最相关的海外驱动 (overseas_linkage.py 已算好 corr/beta)
      2) 用该驱动最新一日的涨跌 × beta, 折算成对 A 股当日的影响分
    高 beta 的票在海外大跌时同样被放大, 所以分值是双向的 (可正可负)。
    """
    if not linkage_rows:
        return None, "无海外关联数据"
    # 取与「大盘」(沪深300/上证) 相关度最高的海外驱动作为市场级传导
    mkt = [r for r in linkage_rows
           if r.get("target_name") in ("沪深300", "上证指数") and r.get("corr") is not None]
    mkt.sort(key=lambda r: -abs(r["corr"]))
    top = mkt[0] if mkt else None
    # 全球情绪: 各市场当日涨跌均值
    gs = (global_markets or {}).get("global_sentiment") or {}
    avg = gs.get("avg_chg_pct")
    if top is None and avg is None:
        return None, "无海外情绪/关联数据"
    score = 50.0
    parts = []
    if avg is not None:
        score += max(-20, min(20, avg * 4))
        parts.append("全球情绪 %+.2f%%" % avg)
    if top:
        b = top.get("beta") or 0
        c = top.get("corr") or 0
        # 传导系数: beta 已含弹性, 这里只把「最相关驱动」标出来供人判断
        parts.append("最相关: %s (corr %.2f / beta %.2f)" % (
            top.get("driver_name"), c, b))
        if top.get("overnight_corr") is not None and abs(top["overnight_corr"]) >= 0.15:
            parts.append("隔夜传导 corr %.2f" % top["overnight_corr"])
    return max(0, min(100, score)), " · ".join(parts)


def dim_market(market_now):
    """市场面: 涨跌家数/涨停/成交额/赚钱效应"""
    m = (market_now or {}).get("market") or {}
    adv = m.get("advance", 0)
    dec = m.get("decline", 0)
    tot = max(1, adv + dec)
    up_pct = m.get("up_pct", 50)
    zt = m.get("limit_up", 0)
    score = 50 + (up_pct - 50) * 0.8 + min(15, zt / 5)
    return max(0, min(100, score)), f"涨{adv}/跌{dec} 上涨占比{up_pct}% 涨停{zt}"


def dim_sentiment(ban_prob, hot_impact):
    """情绪面: 晋级率/封板率 + 主题热度"""
    base = (ban_prob or {}).get("base") or {}
    seal = base.get("seal_rate")
    promo = base.get("promo_rate")
    score = 50
    if seal is not None:
        score += (seal - 75) * 0.6
    if promo is not None:
        score += (promo - 30) * 0.5
    hi = hot_impact or {}
    reps = hi.get("reports") or []
    if reps:
        score += min(10, len(reps) * 2)
    return max(0, min(100, score)), f"封板率{seal if seal is not None else '—'}% 晋级率{promo if promo is not None else '—'}% 热点主题{len(reps)}个"


def dim_cycle(cycle_deep):
    """周期面: 五维综合分 (-5~+5 → 0-100)"""
    cd = cycle_deep or {}
    st = cd.get("status") or {}
    comp = st.get("composite")
    if comp is None:
        return None, "无周期深析"
    score = 50 + comp * 8
    return max(0, min(100, score)), f"周期综合{comp:+.1f} ({st.get('bias', '—')})"


def dim_news(title_themes, hot_impact, theme_keywords=None):
    """消息面 + 热点关联: 个股关联新闻主题数 + 是否命中热点"""
    themes = set(title_themes or [])
    score = 50
    n_hit = 0
    for r in (hot_impact or {}).get("reports") or []:
        t = str(r.get("theme", ""))
        if t and t in themes:
            n_hit += 1
    if n_hit:
        score += min(30, n_hit * 15)
    return max(0, min(100, score)), f"关联热点{n_hit}个 主题{sorted(themes)[:3]}"


# ─── 主流程 ───────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "multidim.json"))
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--no-flow", action="store_true", help="跳过资金流抓取 (快)")
    args = ap.parse_args()

    print(f"[multidim] {datetime.now(CST):%Y-%m-%d %H:%M} CST")
    klines = E.load_klines(BULK)
    print(f"[multidim] K线 {len(klines)} 只")

    quotes = load_quote_index()
    industry_map, sector_strength, sector_money, n_em = load_sector_map()
    market_now = load_json("market_now.json")
    linkage = load_json("overseas_linkage.json") or {}
    linkage_pairs = linkage.get("pairs") or []
    global_markets = load_json("global_markets.json")
    ban_prob = load_json("ban_prob.json")
    hot_impact = load_json("hot_impact.json")
    cycle_deep = load_json("cycle_deep.json")
    print(f"[multidim] 快照{len(quotes)} 板块强度{len(sector_strength)} "
          f"行业映射{len(industry_map)} (东财中文{n_em}) 板块资金{len(sector_money)} "
          f"海外关联{len(linkage_pairs)}")

    # 候选: 按成交额排序取 top N
    def liq(rows):
        try:
            b = rows[-1]
            return float(b[2]) * float(b[5])   # close*vol ≈ 成交额 (bars: [date,open,close,high,low,vol])
        except (ValueError, IndexError):
            return 0.0
    ranked = sorted(klines.items(), key=lambda kv: -liq(kv[1]))
    if args.limit:
        ranked = ranked[: args.limit]

    # 技术面分 (engine_v5)
    print(f"[multidim] 计算技术面共振 ...", flush=True)
    tech = {}
    for code, rows in ranked:
        bars = [b for b in rows if True]
        if len(bars) < 30:
            continue
        try:
            # bars: [date, open, close, high, low, vol] — 注意 b[2] 才是 close
            close = [float(b[2]) for b in bars]
            high = [float(b[3]) for b in bars]
            low = [float(b[4]) for b in bars]
            vol = [float(b[5]) for b in bars]
            bare = code[2:] if code[:2] in ("sh", "sz", "bj") else code
            r = E.score_stock(close, high, low, vol, code_bare=bare)
        except Exception:
            continue
        if r:
            # RSI 供低位吸筹判断
            rsi = None
            for v in r.get("votes", []):
                if v["indicator"] == "RSI":
                    rsi = v.get("value") if "value" in v else None
            tech[code] = {"score": r["score"], "votes": r["votes"],
                          "resonance": r["resonance"], "bars": bars, "rsi": rsi}

    # 资金面 (东财 fflow, 并发)
    flows = {}
    if not args.no_flow:
        print(f"[multidim] 抓取资金流 ({len(tech)} 只) ...", flush=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_fflow, secid_of(c[2:] if c[:2] in ("sh","sz","bj") else c), 10): c
                    for c in tech}
            for i, fut in enumerate(as_completed(futs)):
                c = futs[fut]
                flows[c] = fut.result()
                if (i + 1) % 100 == 0:
                    print(f"  {i+1}/{len(tech)}", flush=True)

    # 逐股多维打分
    results = []
    for code, t in tech.items():
        bare = code[2:] if code[:2] in ("sh", "sz", "bj") else code
        q = quotes.get(bare) or {}
        mc = q.get("mktcap")
        dims = {}
        dims["技术面"], why_t = t["score"], f"17指标共振 {t['score']}"
        s, w = dim_capital(flows.get(code), mc)
        dims["资金面"], why_c = (s if s is not None else 50), w
        s, w = dim_position(t["bars"], t.get("rsi"))
        dims["高低位"], why_p = (s if s is not None else 50), w
        s, w = dim_fundamental(q)
        dims["基本面"], why_f = (s if s is not None else 50), w
        s, w = dim_sector(industry_map.get(bare), sector_strength, sector_money)
        dims["板块面"], why_s = (s if s is not None else 50), w
        s, w = dim_market(market_now)
        dims["市场面"], why_m = s, w
        s, w = dim_sentiment(ban_prob, hot_impact)
        dims["情绪面"], why_e = s, w
        s, w = dim_cycle(cycle_deep)
        dims["周期面"], why_y = (s if s is not None else 50), w
        # 消息面: 从行业+个股名关联热点
        s, w = dim_news([], hot_impact)
        dims["消息面"], why_n = s, w
        # 海外面: 与海外市场的联动强度 + 隔夜海外情绪
        s, w = dim_overseas(linkage_pairs, global_markets, industry_map.get(bare))
        dims["海外面"], why_o = (s if s is not None else 50), w

        # 综合: 技术面权重最高 (0.25), 其余均分
        weights = {"技术面": 0.22, "资金面": 0.14, "高低位": 0.13, "基本面": 0.09,
                   "板块面": 0.10, "市场面": 0.08, "情绪面": 0.07,
                   "周期面": 0.05, "消息面": 0.05, "海外面": 0.07}
        composite = sum(dims[k] * w for k, w in weights.items() if k in dims)
        results.append({
            "code": bare, "name": q.get("name", ""),
            "price": q.get("price"), "chg_pct": q.get("chg_pct"),
            "composite": round(composite, 1),
            "dims": {k: round(v, 1) for k, v in dims.items()},
            "why": {"技术面": why_t, "资金面": why_c, "高低位": why_p,
                    "基本面": why_f, "板块面": why_s, "市场面": why_m,
                    "情绪面": why_e, "周期面": why_y, "消息面": why_n,
                    "海外面": why_o},
            "resonance": t["resonance"],
        })

    results.sort(key=lambda x: -x["composite"])
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "multidim v1 (9维: 技术/资金/高低位/基本面/板块/市场/情绪/周期/消息)",
        "n_scanned": len(results),
        "weights": {"技术面": 0.25, "资金面": 0.15, "高低位": 0.15, "基本面": 0.10,
                    "板块面": 0.10, "市场面": 0.08, "情绪面": 0.07,
                    "周期面": 0.05, "消息面": 0.05},
        "top": results[:60],
        "meta": {
            "sources": "engine_v5(技术) + 东财fflow(资金) + 腾讯快照(基本面) + "
                       "sector_pool(板块) + market_now(市场) + ban_prob/hot_impact(情绪/消息) + cycle_deep(周期)",
            "position_rule": "高位套人: 20日位>85%且乖离>15% | 低位吸筹: 60日位<20%且乖离>-8%且RSI<40",
            "caveat": "多维打分是统计加权, 非必然; 资金流为交易所口径不含隐性买单; 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n=== 多维 TOP10 ===")
    for r in results[:10]:
        print(f"  {r['code']} {r['name'][:6]:6s} 综合={r['composite']:5.1f} "
              f"技{r['dims']['技术面']:.0f} 资{r['dims']['资金面']:.0f} "
              f"位{r['dims']['高低位']:.0f} 基{r['dims']['基本面']:.0f}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
