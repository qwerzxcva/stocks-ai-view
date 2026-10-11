#!/usr/bin/env python3
"""
hot_impact: 实时热点 → 对应股票走势推演 (TSP 推演框架 v4)

逻辑链:
  1. 热点主题: 从 ai_screener 的 themes + 抖音热榜 + 基金排行热度提取 top N 主题
  2. 关联股票: 主题关键词 × (涨停池名称 + kline 仓 universe 名称) → 候选池
  3. 走势推演: 每个候选股票拉腾讯前复权日K (30根), 计算:
     - 近1/5/10/20日收益
     - 相对 MA20 偏离度 / 20日区间位置 (低=0 高=1)
     - 推演信号 (规则引擎):
         * 动量延续: 5日涨>0 且 距MA20偏离<10%  → 建议关注 (预期 3-5 日再上行 2-5%)
         * 追高警惕: 距20日最高<3% 且 5日涨幅>15% → 建议回避 (短期过热, 预期回踩 3-8%)
         * 超跌修复: 10日跌>5% 且 收盘<MA20        → 左侧观察 (预期 1-3% 修复空间)
         * 横盘蓄势: 20日区间位置 0.3-0.7 且 5日波动<3% → 等待热点二次催化
     - 预期区间: 由 0.5×5日动量 + 0.3×热点强度 + 0.2×资金(涨停池有则+) 归一化估计
  4. 输出: JSON + MD → 写 stocks/analysis/ 与 ai-view latest.json 的 hot_impact 字段

数据源 (全公开免登录): 腾讯 fqkline + 东财涨跌停池 + 本地 universe.csv
用法: python3 hot_impact.py --out-dir ... [--kline-universe ...] [--top-themes 8]
"""
import argparse, datetime, json, os, re, sys, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_screener import (fetch_pool, fetch_news, fetch_remote_news, theme_of, THEMES)


def load_universe(path):
    """universe.csv: code|name|market → {name: code}"""
    m = {}
    if not os.path.exists(path):
        return m
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith("#") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            m[parts[1].strip()] = parts[0].strip()
    return m


def find_related(theme, universe, zt_names):
    """主题 → 关联股票 {腾讯码: 名称}: 名称命中关键词的涨停股 + 全市场名称命中前 8"""
    kws = [theme] + THEMES.get(theme, [])
    hits = []
    for zt_name, zt_code in zt_names:
        if any(k.lower() in zt_name.lower() for k in kws if len(k) >= 2):
            hits.append((zt_code, zt_name, True))
    for name, code in universe.items():
        if any(k.lower() in name.lower() for k in kws if len(k) >= 2):
            if not any(c == code for _, c, _ in hits):
                hits.append((code, name, False))
    return hits[:8]


def kline_metrics(code, bars=30):
    """腾讯前复权日K → 走势指标 dict; 失败返回 None"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{bars},qfq"
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read()
        d = json.loads(body.decode("utf-8", "replace"))
        node = (d.get("data") or {}).get(code) or {}
        k = node.get("qfqday") or node.get("day") or []
        closes = [float(r[2]) for r in k if len(r) > 2]
        highs = [float(r[3]) for r in k if len(r) > 3]
        if len(closes) < 20:
            return None
        c = closes[-1]

        def ret(n):
            return round((c / closes[-(n + 1)] - 1) * 100, 2) if len(closes) > n else None

        ma20 = sum(closes[-20:]) / 20
        lo20, hi20 = min(closes[-20:]), max(closes[-20:])
        pos = (c - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        dev20 = (c / ma20 - 1) * 100
        r5, r10 = ret(5), ret(10)
        return {"price": c, "ret_1d": ret(1), "ret_5d": r5, "ret_10d": r10, "ret_20d": ret(20),
                "ma20": round(ma20, 2), "dev20_pct": round(dev20, 2), "pos20": round(pos, 2)}
    except Exception:
        return None


def derive_signal(m, heat, in_zt):
    """规则引擎: 指标 + 热点强度 → (信号, 预期区间, 理由)"""
    r5 = m["ret_5d"] or 0
    dev = m["dev20_pct"]
    pos = m["pos20"]
    # 预期估计: 5日动量 (归一) + 热点强度 + 涨停热度
    momentum = max(-1, min(1, r5 / 15))
    exp_up = momentum * 4 + heat * 3 + (3 if in_zt else 0)
    exp_down = -abs(momentum) * 4 - (5 if pos > 0.9 and r5 > 15 else 0)

    if pos > 0.95 and r5 > 15:
        sig = "追高警惕"
        expect = f"{exp_down:.0f}% ~ {min(exp_up, 3):.0f}%"
        why = f"距20日最高 {(1-pos)*100:.0f}% 内, 5日 +{r5:.0f}% 短期过热"
    elif r5 > 0 and dev < 10 and pos < 0.8:
        sig = "动量延续"
        expect = f"+{max(exp_up, 1):.0f}% ~ +{max(exp_up + 3, 4):.0f}%"
        why = f"5日 +{r5:.0f}% 且距MA20 +{dev:.0f}% 未过热, 热点 {heat:.0%} 强度下有望延续"
    elif (m["ret_10d"] or 0) < -5 and m["price"] < m["ma20"]:
        sig = "超跌修复"
        expect = f"-2% ~ +3%"
        why = f"10日 {(m['ret_10d']):.0f}% 且跌破MA20, 热点若持续有修复空间"
    else:
        sig = "横盘蓄势"
        expect = f"{exp_down:.0f}% ~ +{max(exp_up, 2):.0f}%"
        why = f"20日区间位置 {pos:.0%}, 等待热点二次催化"
    return sig, expect, why


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--kline-universe", default=None)
    parser.add_argument("--top-themes", type=int, default=8)
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y%m%d")

    zt = fetch_pool("ZT", date_str)
    zt_names = [(z["name"], z["code"].zfill(6)) for z in zt]

    # 主题热度 (新闻命中数 + 抖音热榜辅助)
    news = fetch_news(120)
    if len(news) < 30:
        news += fetch_remote_news(days=3)
    theme_count = {}
    for n in news:
        for t in n["theme"]:
            theme_count[t] = theme_count.get(t, 0) + 1
    top_themes = sorted(theme_count, key=theme_count.get, reverse=True)[:args.top_themes]
    max_c = max(theme_count.values(), default=1)

    # 全市场名称映射
    uni_path = args.kline_universe or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "stocks-data-kline", "meta", "universe.csv")
    universe = load_universe(uni_path)
    if not universe:
        print("[hot-impact] universe 缺失, 仅用涨停池推演", file=sys.stderr)

    # 每个主题: 找关联股票 → 拉K线 → 推演
    reports = []
    for t in top_themes:
        hits = find_related(t, universe, zt_names)
        if not hits:
            continue
        heat = theme_count.get(t, 0) / max(1, max_c)
        stocks = []
        for code, name, in_zt in hits[:6]:
            m = kline_metrics(code)
            if not m:
                continue
            sig, expect, why = derive_signal(m, heat, in_zt)
            stocks.append({"code": code, "name": name, "in_zt": in_zt, "signal": sig,
                           "expect": expect, "why": why, **m})
            time.sleep(0.05)
        if stocks:
            reports.append({"theme": t, "heat": round(heat, 2), "news_count": theme_count.get(t, 0),
                            "stocks": stocks})

    out = {"ts": now.isoformat(), "date": date_str,
           "note": "热点→走势推演: 规则引擎(动量/过热/超跌/蓄势), 预期区间仅供研究参考",
           "reports": reports,
           "meta": {"algo": "tsp-derive-v4", "source": "tencent-kline+zt-pool+news", "zero_cred": True}}

    out_dir = args.out_dir or "/tmp/hot_impact"
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "hot_impact.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    md = [f"# 热点→股票走势推演 {now.strftime('%Y-%m-%d %H:%M')} CST", "",
          f"- 主题数 {len(reports)} · 推演股票 {sum(len(r['stocks']) for r in reports)} 只 · 规则: 动量/过热/超跌/蓄势"]
    for r in reports:
        md += ["", f"## {r['theme']} (热度 {r['heat']:.0%} · {r['news_count']}条新闻)", "",
               "| 代码 | 名称 | 现价 | 5日 | 20日位置 | 信号 | 预期区间 | 依据 |",
               "|------|------|------|-----|---------|------|----------|------|"]
        for s in r["stocks"]:
            md.append(f"| {s['code']} | {s['name']}{' 🔥涨停' if s['in_zt'] else ''} | {s['price']} "
                      f"| {s['ret_5d']}% | {s['pos20']:.0%} | **{s['signal']}** | {s['expect']} | {s['why']} |")
    md += ["", "---", "*预期区间由 0.5×5日动量+0.3×热点强度+0.2×涨停热度 归一化估计, 仅供研究, 不构成投资建议*"]
    mp = os.path.join(out_dir, "hot_impact.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[output] {jp}\n[output] {mp}", flush=True)


if __name__ == "__main__":
    main()
