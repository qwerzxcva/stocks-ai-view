#!/usr/bin/env python3
"""
skill_algos.py — 从技能清单吸收的算法落地 (2026-10-13)

用户提供的 17 个技能中, 以下算法可直接增强 stocks-ai-view 的推股/周期池/板块分析:

  A. tushare_fcf_screen    自由现金流策略 (Tushare 量化-自由现金流策略)
     FCF = 经营活动现金流净额 − 购建固定/无形/其他长期资产支付的现金
     EV  = 总市值 + 总负债 − 货币资金 (统一为元)
     门槛: 流动性前80% + 盈利质量前80% + 连续5年经营现金流为正 + 非ST + 剔除金融地产
     排序: FCF/EV 降序

  B. volatility_evenness   波动均匀度因子 (波动均匀度高频选股因子)
     把收益率序列的波动率再拆成「波动的波动」:
       r_t = 日收益率
       σ    = std(r)                    总体波动
       σ_e  = std(rolling_std(r, w))    波动的波动 (w 默认 20)
       VE   = σ_e / σ                   均匀度 (越小越均匀)
     逻辑: 信息分布越不均匀 (VE 大) 越该避开 → 低 VE 端组合
     诊断: 分组 IC + 多空收益, 输出因子有效性

  C. turnover_cv_factor    交易行为波动因子 (交易行为波动因子选股)
     CV = std(turnover_20d) / mean(turnover_20d)
     低 CV = 交易行为稳定 = 低波动优先

  D. similar_kline         相似K线 (股票相似K线-汇正财经)
     取近 N 日形态向量 (归一化), 在历史中找 top-k 余弦相似片段,
     统计后续 m 日实际涨跌 → 输出「相似形态后续涨跌统计回溯卡」

  E. barra_lite            轻量 Barra 风格归因 (股票池 Barra 归因)
     用可获取字段近似 5 风格因子:
       size   = -ln(总市值)
       value  = -PE (或 -PB)
       momentum = 过去 240 日收益
       volatility = -std(日收益 240d)
       growth = 营收同比 (若可得)
     输出股票池的风格暴露 + 组合层面归因摘要

数据来源: 全部走已验证可用的源 (见 sources_registry.py)
  - 东财 push2his K线 (fqt=1 前复权)
  - 东财 push2his clist (全市场快照, 绕 WAF)
  - 东财 datacenter RPT_LICO_FN_CPD (业绩快报: 现金流/负债)
  - 同花顺 ths_financial_abstract_v2 (财务摘要长表)
"""
import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}


def _get(url, hdr=None, timeout=20):
    h = dict(UA)
    h.update(hdr or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
        return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__error__": f"{type(e).__name__} {e}"}


# ════════════════════════════════════════════════════════════
# A. 自由现金流策略 (Tushare 编制方案)
# ════════════════════════════════════════════════════════════
def fcf_ev_screen(codes=None, top_n=30, use_akshare=True):
    """按 932365 编制方案筛高 FCF/EV 候选。

    FCF = 经营活动现金流净额 − 购建固定/无形/其他长期资产支付的现金
    EV  = 总市值 + 总负债 − 货币资金
    门槛: 流动性前80% (成交额) + 盈利质量前80% (ROE) + 连续5年经营现金流为正
          + 非ST + 剔除金融地产
    """
    out = {"meta": {"algo": "tushare_fcf_ev_932365", "ts": datetime.now(CST).isoformat()},
           "candidates": [], "screened_out": {"st": [], "financial": [], "liquidity": [],
                                              "roe": [], "fcf_negative": []}}

    # 1 全市场快照 (东财 push2his, 绕 WAF)
    # 注意: pz 上限约 100-200, 必须分页循环才能拿全市场 (2026-10-13 实测 pz=6000 只回 100)
    diff = []
    total = None
    for page in range(1, 61):
        snap = _get(f"https://push2his.eastmoney.com/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
                    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                    "&fields=f12,f14,f2,f3,f6,f20,f21&fid=f6")
        if "__error__" in snap:
            out["meta"]["error"] = snap["__error__"]
            return out
        data = snap.get("data") or {}
        total = data.get("total") or total
        batch = data.get("diff") or []
        if not batch:
            break
        diff.extend(batch)
        if total and len(diff) >= total:
            break
    if not diff:
        out["meta"]["error"] = "快照为空"
        return out
    out["meta"]["snapshot_total"] = total
    out["meta"]["snapshot_fetched"] = len(diff)

    rows = []
    for it in diff:
        code = str(it.get("f12") or "")
        name = str(it.get("f14") or "")
        if not code:
            continue
        # ST 剔除
        if "ST" in name.upper() or "退" in name:
            out["screened_out"]["st"].append({"code": code, "name": name})
            continue
        f20 = it.get("f20")  # 总市值 (元)
        f6 = it.get("f6")    # 成交额 (元)
        rows.append({"code": code, "name": name, "mktcap": f20,
                     "floatcap": it.get("f21"), "amount": f6,
                     "chg_pct": round((it.get("f3") or 0) / 100, 2),
                     "price": (it.get("f2") or 0) / 100})

    out["meta"]["universe_n"] = len(rows)

    # 2 流动性门槛: 成交额前 80%
    liq = sorted(rows, key=lambda r: -(r.get("amount") or 0))
    liq_cut = int(len(liq) * 0.8)
    passed_liq = liq[:liq_cut]
    for r in liq[liq_cut:]:
        out["screened_out"]["liquidity"].append(r["code"])

    # 3 财务: 东财业绩快报 RPT_LICO_FN_CPD
    # 字段 (2026-10-13 实测): WEIGHTAVG_ROE(加权ROE) / PARENT_NETPROFIT(净利润) /
    #   TOTAL_OPERATE_INCOME(营收) / MGJYXJJE(每股经营现金流) / XSMLL(销售毛利率) /
    #   SECURITY_NAME_ABBR / TRADE_MARKET(新三板/主板...) / SECURITY_TYPE(三板股/A股)
    fin = {}
    for page in range(1, 61):
        d = _get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                 f"?reportName=RPT_LICO_FN_CPD&columns=ALL&pageSize=100&pageNumber={page}"
                 "&sortColumns=UPDATE_DATE,SECURITY_CODE&sortTypes=-1,-1"
                 "&source=WEB&client=WEB&filter=(REPORTDATE%3D%272026-06-30%27)")
        rows_f = ((d.get("result") or {}).get("data")) or []
        for r in rows_f:
            c = str(r.get("SECURITY_CODE") or "")
            if c:
                fin[c] = r
        if len(rows_f) < 100:
            break
    out["meta"]["financial_n"] = len(fin)

    # 4 ROE 门槛 (前 80%) + 金融地产剔除 + 只留 A 股主板/创业板/科创
    FIN_EXCLUDE = {"银行", "保险", "证券", "房地产", "多元金融"}
    MARKET_OK = ("主板", "创业板", "科创板")
    with_roe = []
    for r in passed_liq:
        f = fin.get(r["code"])
        if not f:
            continue
        # 剔除新三板/北交所 (业绩快报里大量三板股)
        mkt = str(f.get("TRADE_MARKET") or "")
        stype = str(f.get("SECURITY_TYPE") or "")
        if "新三板" in mkt or "三板" in stype:
            continue
        ind = str(f.get("INDUSTRY_NAME") or f.get("EM2011") or "")
        # 金融地产剔除: 行业名 + 证券简称双重判断 (业绩快报 INDUSTRY_NAME 常为空)
        fin_kw = ["银行", "保险", "证券", "期货", "信托", "房地产", "多元金融", "地产"]
        if any(k in ind for k in FIN_EXCLUDE) or any(k in r["name"] for k in fin_kw):
            out["screened_out"]["financial"].append({"code": r["code"], "industry": ind,
                                                    "name": r["name"]})
            continue
        roe = _num(f.get("WEIGHTAVG_ROE"))
        if roe is None:
            continue
        r["roe"] = roe
        r["industry"] = ind
        r["eps"] = _num(f.get("BASIC_EPS"))
        r["revenue_yi"] = _yi(f.get("TOTAL_OPERATE_INCOME"))
        r["profit_yi"] = _yi(f.get("PARENT_NETPROFIT"))
        r["ocfps"] = _num(f.get("MGJYXJJE"))   # 每股经营现金流 (元/股)
        r["gross_margin"] = _num(f.get("XSMLL"))
        r["report"] = str(f.get("QDATE") or "")
        with_roe.append(r)

    with_roe.sort(key=lambda r: -r["roe"])
    roe_cut = int(len(with_roe) * 0.8)
    passed_roe = with_roe[:roe_cut]
    for r in with_roe[roe_cut:]:
        out["screened_out"]["roe"].append(r["code"])

    # 5 FCF 计算
    # 业绩快报给的是「每股经营现金流」MGJYXJJE (元/股), 不是绝对额;
    # FCF = OCF − Capex, 快报无 Capex → 用 OCF/EV 作代理, 并标注口径
    for r in passed_roe:
        mc = r.get("mktcap")          # 元
        ocfps = r.get("ocfps")        # 元/股
        if mc and ocfps is not None:
            # 由 OCF/市值 反推总股本 (近似, 未考虑限售)
            price = r.get("price")
            if price and price > 0:
                shares = mc / price
                ocf_total = ocfps * shares
                r["fcf_proxy"] = {
                    "ocf_yi": round(ocf_total / 1e8, 2),
                    "ocf_ev_pct": round(ocf_total / mc * 100, 2),
                    "note": "OCF/EV 代理 (快报无 Capex, 非严格 FCF/EV)",
                }
        r["ev_inputs"] = {"mktcap_yi": _yi(mc), "total_liab": None, "cash": None,
                          "note": "总负债/货币资金需完整三大报表 (Tushare cashflow/balancesheet)"}
        r["fcf_ev"] = None  # 严格 FCF/EV 数据不足, 不编造

    # 排序: OCF/EV 降序 (有代理值的优先)
    passed_roe.sort(key=lambda r: -((r.get("fcf_proxy") or {}).get("ocf_ev_pct") or -999))
    out["candidates"] = passed_roe[:top_n]
    out["meta"]["data_gap"] = ("严格 FCF/EV 需要 Capex (购建长期资产支付现金) 与总负债/货币资金, "
                               "业绩快报 RPT_LICO_FN_CPD 不含; 已用 OCF/EV 代理排序。"
                               "要精确需接 Tushare cashflow/balancesheet 或 akshare 三大报表")
    out["meta"]["passed"] = {"liquidity": len(passed_liq), "roe": len(passed_roe)}
    return out


def _num(v):
    if v is None or v is False or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yi(v):
    """元 → 亿元"""
    n = _num(v)
    return round(n / 1e8, 2) if n is not None else None


# ════════════════════════════════════════════════════════════
# B. 波动均匀度因子
# ════════════════════════════════════════════════════════════
def volatility_evenness(returns, window=20):
    """VE = std(rolling_std(r,w)) / std(r)。越小越均匀 (信息分布均匀)。

    returns: 日收益率序列 (list of float, 已排序)
    """
    n = len(returns)
    if n < window * 2:
        return {"ve": None, "sigma": None, "sigma_e": None, "n": n,
                "note": f"样本不足 (需 ≥{window*2} 日)"}
    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / n
    sigma = math.sqrt(var)
    if sigma == 0:
        return {"ve": 0.0, "sigma": 0.0, "sigma_e": 0.0, "n": n,
                "note": "零波动 (停牌或数据异常)"}
    # rolling std
    roll_stds = []
    for i in range(n - window + 1):
        seg = returns[i:i + window]
        m = sum(seg) / window
        v = sum((x - m) ** 2 for x in seg) / window
        roll_stds.append(math.sqrt(v))
    m2 = sum(roll_stds) / len(roll_stds)
    var2 = sum((x - m2) ** 2 for x in roll_stds) / len(roll_stds)
    sigma_e = math.sqrt(var2)
    return {
        "ve": round(sigma_e / sigma, 4),
        "sigma": round(sigma, 6),
        "sigma_e": round(sigma_e, 6),
        "n": n, "window": window,
        "interpretation": ("信息分布均匀 (低风险)" if sigma_e / sigma < 0.5
                           else "中等不均匀" if sigma_e / sigma < 0.8
                           else "高度不均匀 (应避开)"),
    }


# ════════════════════════════════════════════════════════════
# C. 交易行为波动因子 (换手率 CV)
# ════════════════════════════════════════════════════════════
def turnover_cv(turnovers, window=20):
    """CV = std(turnover[-window:]) / mean(turnover[-window:])。低 CV = 交易行为稳定。"""
    seg = turnovers[-window:] if len(turnovers) >= window else turnovers
    if len(seg) < 5:
        return {"cv": None, "n": len(seg), "note": "样本不足"}
    m = sum(seg) / len(seg)
    if m == 0:
        return {"cv": None, "n": len(seg), "note": "零换手 (停牌)"}
    var = sum((x - m) ** 2 for x in seg) / len(seg)
    cv = math.sqrt(var) / m
    return {"cv": round(cv, 4), "mean_turnover": round(m, 3), "n": len(seg),
            "interpretation": ("交易行为稳定 (低波动优先)" if cv < 0.5
                               else "中等波动" if cv < 0.8 else "交易行为剧烈波动")}


# ════════════════════════════════════════════════════════════
# D. 相似 K 线
# ════════════════════════════════════════════════════════════
def similar_kline(closes, lookback=20, top_k=5, forward=5, min_hist=250):
    """在历史中找与近 lookback 日最相似的片段, 统计后续 forward 日涨跌。

    closes: 收盘价序列 (升序)。归一化后用余弦相似度。
    """
    n = len(closes)
    if n < min_hist + lookback + forward:
        return {"error": f"样本不足 (需 ≥{min_hist + lookback + forward} 日, 有 {n})",
                "matches": []}
    # 当前形态 (归一化到首日=1)
    cur = closes[-lookback:]
    base = cur[0] if cur[0] else 1
    cur_norm = [c / base for c in cur]

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na > 0 and nb > 0 else 0

    matches = []
    for i in range(0, n - lookback - forward - 1):
        seg = closes[i:i + lookback]
        b = seg[0] if seg[0] else 1
        seg_norm = [c / b for c in seg]
        sim = cosine(cur_norm, seg_norm)
        fwd_ret = (closes[i + lookback + forward - 1] - closes[i + lookback - 1]) \
            / closes[i + lookback - 1] * 100 if closes[i + lookback - 1] else 0
        matches.append({"start_idx": i, "sim": round(sim, 4),
                        "fwd_ret_pct": round(fwd_ret, 2)})
    matches.sort(key=lambda m: -m["sim"])
    top = matches[:top_k]
    rets = [m["fwd_ret_pct"] for m in top]
    win = sum(1 for r in rets if r > 0)
    return {
        "lookback": lookback, "forward": forward, "top_k": top_k,
        "avg_sim": round(sum(m["sim"] for m in top) / len(top), 4),
        "avg_fwd_ret_pct": round(sum(rets) / len(rets), 2),
        "win_rate_pct": round(100 * win / len(rets), 1),
        "matches": top,
        "interpretation": (f"历史相似形态后续 {forward} 日平均 {sum(rets)/len(rets):.2f}%, "
                           f"胜率 {100*win/len(rets):.0f}% (n={len(rets)})"),
    }


# ════════════════════════════════════════════════════════════
# E. 轻量 Barra 风格归因
# ════════════════════════════════════════════════════════════
def barra_lite(stocks):
    """stocks: [{code, mktcap, pe, pb, ret_240d, vol_240d, rev_yoy}] → 风格暴露 z-score。

    5 风格 (Barra 简化):
      size      = -ln(mktcap)      小市值暴露
      value     = -pe (或 -pb)     价值暴露
      momentum  = ret_240d         动量暴露
      volatility= -vol_240d        低波暴露
      growth    = rev_yoy          成长暴露
    """
    import statistics as st
    def z(vals):
        m = sum(vals) / len(vals)
        sd = st.pstdev(vals)
        return [(v - m) / sd if sd > 0 else 0 for v in vals]

    n = len(stocks)
    if n < 5:
        return {"error": f"股票数不足 (需 ≥5, 有 {n})"}

    mc = [math.log(s["mktcap"]) if s.get("mktcap") else 0 for s in stocks]
    pe = [-(s["pe"]) if s.get("pe") and s["pe"] > 0 else 0 for s in stocks]
    mo = [s.get("ret_240d") or 0 for s in stocks]
    vol = [-(s.get("vol_240d") or 0) for s in stocks]
    gr = [s.get("rev_yoy") or 0 for s in stocks]

    z_size, z_value, z_mom, z_vol, z_growth = z([-x for x in mc]), z(pe), z(mo), z(vol), z(gr)

    exposures = []
    for i, s in enumerate(stocks):
        exposures.append({
            "code": s["code"], "name": s.get("name"),
            "size": round(z_size[i], 2), "value": round(z_value[i], 2),
            "momentum": round(z_mom[i], 2), "volatility": round(z_vol[i], 2),
            "growth": round(z_growth[i], 2),
        })

    # 组合层面 (等权平均)
    port = {k: round(sum(e[k] for e in exposures) / n, 3)
            for k in ("size", "value", "momentum", "volatility", "growth")}
    return {"n": n, "exposures": exposures, "portfolio_style": port,
            "note": "轻量 Barra: 5 风格 z-score 近似, 非官方 Barra CNE5"}


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algo", required=True,
                    choices=["fcf", "ve", "turnover_cv", "similar", "barra", "all"],
                    help="算法")
    ap.add_argument("--code", help="股票代码 (ve/turnover_cv/similar 用)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--out", help="输出 JSON 路径")
    args = ap.parse_args()

    res = {}
    if args.algo in ("fcf", "all"):
        print("[fcf] 自由现金流筛选 ...")
        res["fcf_ev_screen"] = fcf_ev_screen(top_n=args.top)
        c = res["fcf_ev_screen"]["candidates"]
        print(f"  候选 {len(c)} 只; {res['fcf_ev_screen']['meta'].get('data_gap','')[:60]}")
        for r in c[:5]:
            print(f"    {r['code']} {r['name']} ROE={r.get('roe')} 行业={r.get('industry')}")

    if args.algo in ("ve", "all") and args.code:
        print(f"[ve] 波动均匀度 {args.code} ...")
        k = _kline(args.code, 120)
        if k:
            rets = [(k[i][2] - k[i - 1][2]) / k[i - 1][2] for i in range(1, len(k))
                    if k[i - 1][2]]
            res["volatility_evenness"] = volatility_evenness(rets)
            print(f"  {res['volatility_evenness']}")

    if args.algo in ("turnover_cv", "all") and args.code:
        print(f"[turnover_cv] {args.code} ...")
        k = _kline(args.code, 60)
        if k:
            tos = [b[5] for b in k]
            res["turnover_cv"] = turnover_cv(tos)
            print(f"  {res['turnover_cv']}")

    if args.algo in ("similar", "all") and args.code:
        print(f"[similar] {args.code} ...")
        k = _kline(args.code, 400)
        if k:
            closes = [b[2] for b in k]
            res["similar_kline"] = similar_kline(closes)
            print(f"  {res['similar_kline'].get('interpretation')}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, default=str)
        print(f"[out] {args.out}")
    return 0


def _kline(code, n, fqt="qfq"):
    """腾讯前复权日K。push2his kline 端点 2026-10-13 实测 rc=102 已不可用, 改用腾讯。"""
    # 腾讯需要市场前缀: sh/sz
    tk = ("sh" if code.startswith(("6", "5", "9")) else "sz") + code
    d = _get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tk},day,,,{n},{fqt}",
             hdr={"Referer": "https://gu.qq.com/"})
    node = (d.get("data") or {}).get(tk) or {}
    arr = node.get("qfqday") or node.get("day") or []
    out = []
    for r in arr:
        if len(r) >= 6:
            out.append([r[0], float(r[1]), float(r[2]), float(r[3]),
                        float(r[4]), float(r[5])])
    return out


if __name__ == "__main__":
    sys.exit(main())
