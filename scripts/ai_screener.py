#!/usr/bin/env python3
"""
AI 实时选股分析引擎 (TSP 信号合成框架 v2, 多源容错, 零凭据)
数据源 (全公开, 无 token):
  - 指数行情: 腾讯 qt.gtimg.cn
  - 涨跌停池: 东财 push2ex getTopicZtPool/DTPool/ZBPool
  - 概念板块: 东财 push2 clist (m:90+t:2)
  - 新闻: 新浪 7x24 + 东财 7x24
  - 个股行情: 腾讯 qt.gtimg.cn 批量
输出: data/latest.json (自包含, 网站直接渲染)
用法: python3 ai_screener.py --out /path/latest.json
Env: STOCKS_OUT (输出文件, 默认 stdout)
"""
import argparse, base64, datetime, json, os, re, sys, urllib.request, urllib.parse

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}
IDX_CODES = ["sh000001", "sz399001", "sh000300", "sh000688", "sz399006", "bj899050"]

# ─── 关键词→主题 (金融/政策/产业全覆盖, 可扩展) ─────────────────────────────
THEMES = {
    "货币政策": ["降准", "降息", "MLF", "逆回购", "LPR", "公开市场", "央行", "流动性"],
    "财政政策": ["财政", "专项债", "赤字", "国债", "税收", "补贴", "减税"],
    "监管政策": ["证监会", "央行", "金融监管总局", "备案", "核准", "问询函", "退市"],
    "贸易": ["关税", "贸易战", "制裁", "出口管制", "反制", "稀土", "战略矿产"],
    "半导体": ["半导体", "芯片", "国产替代", "卡脖子", "光刻机", "先进制程", "EDA"],
    "人工智能": ["AI", "大模型", "算力", "数据中心", "GPU", "英伟达", "NVDA", "deepseek", "OpenAI"],
    "机器人": ["人形机器人", "机器人", "工业母机", "减速器", "丝杠"],
    "低空经济": ["低空", "eVTOL", "飞行汽车", "无人机", "通航"],
    "新能源": ["固态电池", "锂电池", "光伏", "风电", "储能", "氢能", "充电桩"],
    "医药": ["创新药", "医药", "中药", "疫苗", "CRO", "恒瑞", "药明"],
    "消费": ["白酒", "消费", "家电", "零售", "国补", "以旧换新"],
    "军工": ["军工", "国防", "航母", "歼", "导弹", "航天"],
    "贵金属": ["黄金", "白银", "贵金属", "避险", "央行购金"],
    "券商": ["券商", "牛市", "注册制", "投行", "并购重组"],
    "房地产": ["地产", "保障房", "限购", "房地产", "保交楼"],
    "汽车": ["汽车", "新能源车", "比亚迪", "特斯拉", "Robotaxi"],
}

def theme_of(text):
    hit = set()
    for theme, kws in THEMES.items():
        for kw in kws:
            if kw.lower() in text.lower():
                hit.add(theme)
                break
    return hit


# ─── 指数行情 (腾讯, 多重试) ────────────────────────────────────────────────
def fetch_indices(retries=3):
    url = f"https://qt.gtimg.cn/q={','.join(IDX_CODES)}"
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read().decode("gbk", "replace")
            out = []
            for m in re.finditer(r'v_(\w+)="([^"]+)"', body):
                code, payload = m.group(1), m.group(2)
                f = payload.split("~")
                if len(f) < 35:
                    continue
                try:
                    price, prev, openp = float(f[3] or 0), float(f[4] or 0), float(f[5] or 0)
                    chg = (price/prev - 1)*100 if prev else 0
                    out.append({"code": code, "name": f[1], "price": round(price, 2),
                                "prev": round(prev, 2), "open": round(openp, 2),
                                "chg_pct": round(chg, 2)})
                except (ValueError, IndexError):
                    pass
            if out:
                return out
        except Exception as e:
            print(f"[indices] retry: {e}", file=sys.stderr)
    return []


# ─── 涨跌停池 (东财) ────────────────────────────────────────────────────────
def fetch_pool(kind, date_str, retries=3):
    """kind: ZT(涨停)/DT(跌停)/ZB(炸板)"""
    pool_map = {"ZT": "ZT", "DT": "DT", "ZB": "ZB"}
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
           f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            data = d.get("data") or {}
            pool = data.get(f"{kind}Pool") or data.get("pool") or []
            out = []
            for p in pool:
                out.append({
                    "code": str(p.get("c", p.get("code", ""))),
                    "name": str(p.get("n", p.get("name", ""))),
                    "price": float(p.get("p", 0) or 0),
                    "chg_pct": float(p.get("zdp", 0) or 0),
                    "lianban": int(p.get("lbc", p.get("count", 1)) or 1),
                    "zt_time": str(p.get("fbt", "")),
                })
            return out
        except Exception as e:
            print(f"[pool {kind}] retry: {e}", file=sys.stderr)
    return []


# ─── 概念板块资金流 (东财 clist) ──────────────────────────────────────────
def fetch_concepts(retries=3):
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=120&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2+f:!50"
           "&fields=f12,f14,f2,f3,f62,f100")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            diff = (d.get("data") or {}).get("diff") or []
            out = []
            for c in diff:
                out.append({
                    "code": str(c.get("f12", "")), "name": str(c.get("f14", "")),
                    "price": float(c.get("f2") or 0), "chg_pct": float(c.get("f3") or 0),
                    "main_net_in": float(c.get("f62") or 0),
                    "concept": str(c.get("f100", "")),
                })
            return out
        except Exception as e:
            print(f"[concepts] retry: {e}", file=sys.stderr)
    return []


# ─── 新闻 (新浪7x24 + 东财, 含政策/财经) ──────────────────────────────────
def fetch_news(limit=120):
    news = []
    seen = set()
    # 新浪 7x24
    try:
        url = f"https://app.cj.sina.com.cn/api/news/pc?page=1&num={limit}"
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
        d = json.loads(body.decode("utf-8"))
        for it in (d.get("data") or [])[:limit]:
            t = str(it.get("title", "")).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            digest = str(it.get("digest", it.get("content", "")))[:160]
            news.append({"ts": str(it.get("ctime", ""))[:19], "title": t,
                         "theme": sorted(theme_of(t + " " + digest)), "src": "sina", "digest": digest})
    except Exception as e:
        print(f"[news-sina] {e}", file=sys.stderr)
    # 东财 7x24 (政策/财经)
    try:
        url = "https://np-listapi.eastmoney.com/comm/wap/getListInfo?type=8&pageSize=40&pageNo=1&client=web&source=XHH"
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
        d = json.loads(body.decode("utf-8"))
        items = (d.get("data") or d).get("list") if isinstance(d, dict) else []
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            items = d["data"].get("list", [])
        for it in (items or [])[:40]:
            t = str(it.get("title", "")).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            news.append({"ts": str(it.get("showtime", it.get("ctime", "")))[:19], "title": t,
                         "theme": sorted(theme_of(t)), "src": "eastmoney", "digest": ""})
    except Exception as e:
        print(f"[news-em] {e}", file=sys.stderr)
    return news


# ─── 个股行情批量 (腾讯) ────────────────────────────────────────────────────
def fetch_stock_quotes(codes, retries=3):
    if not codes:
        return []
    uniq = list(dict.fromkeys(codes))[:80]  # 腾讯一次最多 ~80
    out = []
    for i in range(0, len(uniq), 60):
        batch = uniq[i:i+60]
        for _ in range(retries):
            try:
                url = f"https://qt.gtimg.cn/q={','.join(batch)}"
                body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read().decode("gbk", "replace")
                for m in re.finditer(r'v_(\w+)="([^"]+)"', body):
                    code, payload = m.group(1), m.group(2)
                    f = payload.split("~")
                    if len(f) < 35:
                        continue
                    try:
                        price, prev = float(f[3] or 0), float(f[4] or 0)
                        chg = (price/prev - 1)*100 if prev else 0
                        out.append({"code": code, "name": f[1], "price": round(price, 2),
                                    "chg_pct": round(chg, 2)})
                    except (ValueError, IndexError):
                        pass
                break
            except Exception as e:
                print(f"[quotes] retry: {e}", file=sys.stderr)
    return out


# ─── 主流程: 热点主题 → 受影响板块 → 受影响个股 → 综合打分 ──────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y%m%d")

    # 1. 指数
    indices = fetch_indices()
    # 2. 涨跌停池
    zt = fetch_pool("ZT", date_str)
    dt = fetch_pool("DT", date_str)
    zb = fetch_pool("ZB", date_str)
    # 3. 概念板块
    concepts = fetch_concepts()
    # 4. 新闻
    news = fetch_news(args.limit)

    # 主题命中统计
    theme_news = {}
    for n in news:
        for t in n["theme"]:
            theme_news.setdefault(t, []).append(n["title"][:40])
    theme_hot = sorted(theme_news.items(), key=lambda x: len(x[1]), reverse=True)

    # 概念板块 × 主题匹配 → 受影响板块
    hot_themes = [t for t, _ in theme_hot[:8]]
    theme_concept = []
    for c in concepts:
        name = c["name"] + " " + c.get("concept", "")
        hit = [t for t in hot_themes if t in name]
        if hit:
            theme_concept.append({**c, "themes": hit})
    theme_concept.sort(key=lambda x: abs(x["main_net_in"]), reverse=True)
    top_concepts = theme_concept[:15]

    # 受影响个股: 涨停池 + 板块关联 (从概念板块拿关联代码)
    affected_codes = [z["code"] for z in zt[:60]]
    quotes = fetch_stock_quotes(affected_codes)
    quote_map = {q["code"]: q for q in quotes}

    # 打分 (TSP 信号合成: rank 归一化 + 加权)
    def rank_norm(vals):
        if not vals:
            return {}
        order = sorted(vals.items(), key=lambda x: x[1])
        n = len(order)
        return {k: i / max(1, n - 1) for i, (k, v) in enumerate(order)}

    scored = []
    for z in zt:
        code = z["code"]
        q = quote_map.get(code, {})
        chg = z.get("chg_pct") or q.get("chg_pct", 0)
        lianban = z.get("lianban", 1)
        # 信号: 涨停家数(热度) / 连板(动能) / 当日涨幅(强度) / 概念关联(逻辑)
        is_theme = any(t in (z.get("name","")+z.get("concept","")) for t in hot_themes)
        scored.append({
            "code": code, "name": z.get("name", ""), "price": q.get("price", z.get("price", 0)),
            "chg_pct": round(chg, 2), "lianban": lianban, "zt_time": z.get("zt_time", ""),
            "theme_hit": is_theme, "zt_score": 0.4*min(lianban/5, 1) + 0.3*min(abs(chg)/10, 1) + 0.3,
            "is_theme": is_theme,
        })
    scored.sort(key=lambda x: (x["is_theme"], x["zt_score"]), reverse=True)
    picks = scored[:30]

    latest = {
        "ts": now.isoformat(),
        "date": date_str,
        "market": {
            "indices": indices,
            "limit_up": len(zt), "limit_down": len(dt), "broken": len(zb),
            "zt_rate": round(len(zt)/max(1, len(zt)+len(zb))*100, 1),
        },
        "themes": [{"name": t, "news_count": len(items), "sample": items[:3]} for t, items in theme_hot[:12]],
        "concepts_affected": top_concepts,
        "limit_up_pool": zt[:50],
        "limit_down_pool": dt[:30],
        "picks": picks,
        "news_recent": news[:60],
        "meta": {"source": "tencent+eastmoney+sina", "algo": "tsp-signal-v2", "zero_cred": True},
    }

    out = json.dumps(latest, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"written {args.out} ({len(out)} bytes)", flush=True)
    else:
        print(out)

    print(f"=== summary ===", file=sys.stderr)
    print(f"indices={len(indices)} zt={len(zt)} dt={len(dt)} zb={len(zb)} "
          f"concepts={len(concepts)} news={len(news)} picks={len(picks)}", file=sys.stderr)


if __name__ == "__main__":
    main()
