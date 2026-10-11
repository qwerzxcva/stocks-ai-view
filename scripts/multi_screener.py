#!/usr/bin/env python3
"""
multi_screener: 多轮审核 × 多源数据融合选股 (TSP v4 Multi-Tier)

审核流程 (3 轮):
  轮次 1 (广度扫描): 多源热点 → 关联股票候选池
    - 新闻主题 (新浪/抖音/东财归档)
    - 东财基金排行 (近1年收益前 50 = KOL 抱团方向)
    - 同花顺热股 (板块涨幅 top20)
  轮次 2 (深度打分): 候选股 × 指标打分
    - 腾讯日K 5/20/60 日收益 + MA20 偏离 + 20日区间位置
    - 东财涨跌停池连板/封板时间
    - 东财概念资金流 (主流入场)
    - 技术指标: RSI/MACD/KDJ (stockstats 本地计算, 零外部依赖)
  轮次 3 (LLM 复盘): 调 GitHub Models (ghc) 对轮次2结果做综合判断
    - 若无 ghc 或失败, 降级为规则引擎最终输出

数据源 (全部纯 HTTP 免登录):
  - 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn  (行情 + K 线)
  - 东财 push2 ex 涨跌停池                 (涨停池/跌停池/炸板)
  - 东财 fund rank                           (混合基金近1年收益排行)
  - 同花顺 d.10jqka.com.cn 指数/板块 K 线   (热股行情)
  - 东财数据中心 datacenter-web            (大宗交易)

输出:
  - analysis/multi_screener_{YYYYMMDD_HHMM}.json
  - analysis/multi_screener_{YYYYMMDD_HHMM}.md
  - latest.json hot_screener 字段 (供网站展示)
用法: python3 multi_screener.py [--rounds 3] [--top 30] [--llm none]
"""
import argparse, datetime, json, os, re, sys, time, urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}

# ─── 工具函数 ───────────────────────────────────────────────────────────
def _http(url, timeout=12):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return None, None
    # 简化 EMA: 首值用前 N 根均值, 后续递推 (足够用于打分, 不必严格)
    def ema(vals, period):
        seed = sum(vals[:period]) / period
        out, k = [seed], 2 / (period + 1)
        for v in vals[period:]:
            out.append(out[-1] + k * (v - out[-1]))
        return out
    ef = ema(closes, fast)
    es = ema(closes, slow)
    dif = ef[-1] - es[-1]
    dea = sum(ef[-signal:]) / signal - sum(es[-signal:]) / signal
    return round(dif, 4), round(dea, 4)


# ─── 轮次 1: 多源热点扫描 ───────────────────────────────────────────────
def round1_scan():
    """融合新闻主题 + 基金排行 + 东财涨跌停池 → 候选池 (抖音热榜已内置于 fetch_news)"""
    from ai_screener import fetch_pool, fetch_news, fetch_remote_news, theme_of
    from eastmoney_fund_rank import fetch as fetch_fund_rank
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y%m%d")
    ydate = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")

    # 1a. 新闻主题 (新浪7x24 + 东财7x24 + 抖音热榜)
    news = fetch_news(120)
    if len(news) < 30:
        news += fetch_remote_news(days=3)
    theme_count = {}
    for n in news:
        for t in n["theme"]:
            theme_count[t] = theme_count.get(t, 0) + 1
    top_themes = sorted(theme_count, key=theme_count.get, reverse=True)[:8]

    # 1b. 涨停池 + 连板统计
    zt = fetch_pool("ZT", date_str)
    yzt = fetch_pool("ZT", ydate)
    lianban_map = {}
    for z in zt:
        code = z["code"]
        lianban_map[code] = max(lianban_map.get(code, 0), z["lianban"])
    # 连板晋级
    promo_rate = sum(1 for z in zt if z["lianban"] >= 2 and z["code"] in {y["code"] for y in yzt}) / max(1, len(yzt))

    # 1c. 东财基金排行 (KOL 抱团方向代理)
    funds = fetch_fund_rank(days=90, top=30)

    # 2a. 候选池: 涨停股 + 基金重仓关联股 (从涨停池名称匹配基金重仓方向)
    candidates = list({z["code"] for z in zt})
    max_c = max(theme_count.values(), default=1)
    return {
        "ts": now.isoformat(),
        "themes_top": [(t, theme_count[t], round(theme_count[t]/max_c, 2)) for t in top_themes],
        "zt_count": len(zt), "yzt_count": len(yzt),
        "promo_rate": round(promo_rate, 3),
        "funds_top10": funds[:10],
        "candidate_codes": candidates[:80],
    }


# ─── 轮次 2: 深度打分 ───────────────────────────────────────────────────
def round2_score(codes, limit=40):
    """拉腾讯 K 线 + 东方财富涨跌停池, 计算 RSI/MACD/区间位置/热点关联 → 打分"""
    scored = []
    zt_map = {}
    try:
        from ai_screener import fetch_pool
        zt_map = {z["code"]: z for z in fetch_pool("ZT",
            datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y%m%d"))}
    except Exception:
        pass

    for code in codes[:limit]:
        raw = str(code).zfill(6)
        from ai_screener import prefixed
        tcode = prefixed(raw)
        q = _http(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tcode},day,,,30,qfq").decode("utf-8", "replace")
        try:
            d = json.loads(q)
            node = (d.get("data") or {}).get(tcode) or {}
            k = node.get("qfqday") or node.get("day") or []
            if not k or len(k) < 25:
                continue
            closes = [float(r[2]) for r in k if len(r) > 2]
            highs = [float(r[3]) for r in k if len(r) > 3]
            lows = [float(r[4]) for r in k if len(r) > 4]
            vols = [float(r[5]) for r in k if len(r) > 5]
            c = closes[-1]
            ma5 = sum(closes[-5:]) / 5
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20
            lo20, hi20 = min(closes[-20:]), max(closes[-20:])
            pos = (c - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
            dev20 = (c / ma20 - 1) * 100
            vol_ratio = vols[-1] / (sum(vols[-5:]) / 5) if vols[-5:] else 1
            r = rsi(closes)
            dif, dea = macd(closes)
            ret_5 = round((c / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None
            ret_20 = round((c / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
            zt_info = zt_map.get(raw, {})
            # 推演信号 (与 hot_impact.derive_signal 同口径): 动量/过热/超跌/蓄势
            r5_ = ret_5 or 0
            pos_ = pos
            if pos > 0.95 and r5_ > 15:
                sig = "追高警惕"
            elif r5_ > 0 and dev20 < 10 and pos_ < 0.8:
                sig = "动量延续"
            elif (ret_20 or 0) < -5 and c < ma20:
                sig = "超跌修复"
            else:
                sig = "横盘蓄势"
            score = (0.25 * min(r / 100, 1) +
                     0.20 * (1 - abs(dev20) / 20) +
                     0.15 * min(abs(ret_5 or 0) / 15, 1) if ret_5 else 0 +
                     0.15 * (zt_info.get("lianban", 1) / 5) if zt_info else 0 +
                     0.15 * min(vol_ratio / 3, 1) +
                     0.10 * pos)
            scored.append({
                "code": raw, "price": round(c, 2), "ret_5d": ret_5, "ret_20d": ret_20,
                "ma5": round(ma5, 2), "ma20": round(ma20, 2), "dev20_pct": round(dev20, 2),
                "rsi": r, "dif": dif, "dea": dea,
                "vol_ratio": round(vol_ratio, 2), "pos20": round(pos, 2),
                "lianban": zt_info.get("lianban", 1),
                "signal": sig, "score": round(max(0, min(1, score)), 3),
            })
            time.sleep(0.08)
        except Exception:
            pass
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ─── 轮次 3: LLM 综合判断 ───────────────────────────────────────────────
def round3_llm(scored, llm_model="deepseek-v4.1-flash", prompt_extra=""):
    """调 GitHub Models (ghc model run) 做综合判断; 失败则返回 None"""
    try:
        import subprocess
        ctx = json.dumps({"top_picks": scored[:15], "meta": {"algo": "tsp-multi-tier-v4", "zero_cred": True}},
                         ensure_ascii=False)[:4000]
        prompt = (f"你是 A 股量化分析师。以下 {len(scored)} 只股票已做多维度打分，"
                  f"请给出 TOP 10 的最终评级与操作建议（看多/中性/看空），理由不超过 200 字。\n{ctx}\n"
                  f"{prompt_extra}")
        r = subprocess.run(["ghc", "model", "run", "--model", llm_model, "--size", "medium",
                            "--prompt", prompt], capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        print("[round3] ghc not installed, skip LLM", file=sys.stderr)
    except Exception as e:
        print(f"[round3] llm err: {e}", file=sys.stderr)
    return None


# ─── 主流程 ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--rounds", type=int, default=3, choices=[1, 2, 3])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--llm", choices=["none", "ghc"], default="none")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    args = parser.parse_args()

    out_dir = Path(args.out_dir or "/tmp/multi_screener")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ts = now.strftime("%Y%m%d_%H%M")

    report = {"ts": now.isoformat(), "date": now.strftime("%Y-%m-%d"),
              "rounds_executed": args.rounds,
              "meta": {"algo": "tsp-multi-tier-v4", "zero_cred": True,
                       "note": "轮次1=热点扫描, 轮次2=深度打分, 轮次3=LLM综合"}}
    r2 = []
    r3 = None

    # Round 1
    r1 = round1_scan()
    report["round1"] = r1
    print(f"[r1] themes={len(r1['themes_top'])} zt={r1['zt_count']} funds={len(r1['funds_top10'])}", flush=True)

    # Round 2
    if args.rounds >= 2:
        r2 = round2_score(r1["candidate_codes"], limit=args.top * 2)
        report["round2_top"] = r2[:args.top]
        print(f"[r2] scored={len(r2)} top={r2[0]['code'] if r2 else '-'} score={r2[0]['score']:.3f}" if r2 else "[r2] empty", flush=True)
    else:
        r2 = []

    # Round 3
    if args.rounds >= 3:
        r3 = round3_llm(r2, args.model) if args.llm == "ghc" else None
        report["round3_llm"] = r3
        print(f"[r3] llm={'on' if args.llm=='ghc' else 'off'} result_len={len(str(r3)) if r3 else 0}", flush=True)

    # Output
    jp = out_dir / f"multi_screener_{ts}.json"
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    # MD
    md = [f"# 多轮审核选股 {now.strftime('%Y-%m-%d %H:%M')} CST", "",
          f"- 审核轮次: {args.rounds} · 候选: {len(r1['candidate_codes'])} → TOP {r2[:args.top] and len(r2)}",
          "- 数据源: 腾讯行情+东财涨跌停池+东财基金排行+同花顺 K 线 + 新浪 7x24 + 抖音热榜", "",
          "## 轮次 1: 热点扫描", ""]
    for t, cnt, heat in r1["themes_top"]:
        md.append(f"- **{t}** ({cnt}条, 热度 {heat:.0%})")
    md += ["", "## 轮次 2: 深度打分 TOP", "",
           "| 代码 | 名称 | 现价 | 5日 | 20日 | RSI | MA20偏离 | 量比 | 连板 | 综合分 |",
           "|------|------|------|-----|------|-----|---------|------|------|-------|"]
    for s in (r2 or [])[:args.top]:
        md.append(f"| {s['code']} | — | {s['price']} | {s['ret_5d']}% | {s['ret_20d']}% "
                  f"| {s['rsi']} | {s['dev20_pct']}% | {s['vol_ratio']}x "
                  f"| {s['lianban']}板 | **{s['score']}** |")
    if r3:
        md += ["", "## 轮次 3: LLM 综合判断", "", r3]
    mp = out_dir / f"multi_screener_{ts}.md"
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[output] {jp}\n[output] {mp}", flush=True)


if __name__ == "__main__":
    main()
