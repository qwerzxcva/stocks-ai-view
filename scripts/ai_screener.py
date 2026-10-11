#!/usr/bin/env python3
"""
AI 实时选股分析引擎 (TSP 信号合成框架 v3, 多源容错, 零凭据)
v3 借鉴 shy3130/tick-stock-panel 的 情绪周期/regime/主线轮动/RPS 思路,
全降级为日线 + 公开接口口径:
  - 市场温度计 0-100 + 6 阶段情绪周期 (冰点/退潮/修复/启动/发酵/高潮, 单日快照)
  - 晋级率: 昨日涨停池 × 今日连板数交叉
  - 主线排行: 概念板块 rank 加权 (资金 0.45/涨幅 0.25/成交额 0.15/新闻热度 0.15);
    概念接口不可达时降级为涨停池主题聚类
  - RPS: 涨停股 5/20 日相对强度 (腾讯前复权日K)
  - 综合打分: 连板 0.22 + 封板时间 0.13 + 热点关联 0.17 + RPS5 0.20 + RPS20 0.12 + 梯队稀缺 0.16
数据源 (全公开, 无 token):
  - 指数/个股行情与K线: 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn
  - 涨跌停池: 东财 push2ex getTopicZtPool/DTPool/ZBPool (支持 date 回看)
  - 概念板块: 东财 push2 clist (m:90+t:2, WAF 不稳定, 自动降级)
  - 新闻: 新浪 7x24 (主) + 东财 7x24 / 财联社 (备) + 远端 data-news 仓回退
输出: data/latest.json (自包含, 网站直接渲染)
用法: python3 ai_screener.py --out /path/latest.json [--limit 120] [--days 3]
"""
import argparse, datetime, json, re, sys, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Referer": "https://finance.sina.com.cn/"}
IDX_CODES = ["sh000001", "sz399001", "sh000300", "sh000688", "sz399006", "bj899050"]

# ─── 关键词→主题 (金融/政策/产业全覆盖, 可扩展) ─────────────────────────────
THEMES = {
    "货币政策": ["降准", "降息", "MLF", "逆回购", "LPR", "公开市场", "央行", "流动性"],
    "财政政策": ["财政", "专项债", "赤字", "国债", "税收", "补贴", "减税"],
    "监管政策": ["证监会", "金融监管总局", "备案", "核准", "问询函", "退市"],
    "贸易": ["关税", "贸易战", "制裁", "出口管制", "反制", "稀土", "战略矿产"],
    "半导体": ["半导体", "芯片", "国产替代", "卡脖子", "光刻机", "先进制程", "EDA", "存储", "HBM", "闪存"],
    "人工智能": ["AI", "大模型", "算力", "数据中心", "GPU", "英伟达", "NVDA", "deepseek", "OpenAI", "智能体"],
    "机器人": ["人形机器人", "机器人", "工业母机", "减速器", "丝杠"],
    "低空经济": ["低空", "eVTOL", "飞行汽车", "无人机", "通航"],
    "新能源": ["固态电池", "锂电池", "光伏", "风电", "储能", "氢能", "充电桩"],
    "医药": ["创新药", "医药", "中药", "疫苗", "CRO", "恒瑞", "药明", "GLP-1"],
    "消费": ["白酒", "消费", "家电", "零售", "国补", "以旧换新"],
    "军工": ["军工", "国防", "航母", "歼", "导弹", "航天", "卫星"],
    "贵金属": ["黄金", "白银", "贵金属", "避险", "央行购金"],
    "券商": ["券商", "牛市", "注册制", "投行", "并购重组"],
    "房地产": ["地产", "保障房", "限购", "房地产", "保交楼"],
    "汽车": ["汽车", "新能源车", "比亚迪", "特斯拉", "Robotaxi"],
    "国资改革": ["中字头", "国企改革", "央企", "重组"],
    "算力": ["算力", "光模块", "CPO", "液冷", "服务器"],
}
POS_KW = ["利好", "大涨", "涨停", "超预期", "突破", "获批", "中标", "签约", "增产", "增持",
          "回购", "创新高", "新高", "签约", "中标", "投产", "订单", "上涨", "增长"]
NEG_KW = ["利空", "大跌", "跌停", "不及预期", "退市", "处罚", "立案", "减持", "违规", "下滑",
          "亏损", "召回", "反制", "袭击", "冲突", "下跌", "缩水", "违约"]

def theme_of(text):
    t = str(text).lower()
    hit = set()
    for theme, kws in THEMES.items():
        for kw in kws:
            if kw.lower() in t:
                hit.add(theme)
                break
    return hit

def sentiment_of(text):
    if any(k in text for k in POS_KW):
        return "pos"
    if any(k in text for k in NEG_KW):
        return "neg"
    return "neu"


def prefixed(code):
    """东财裸码 600241 → 腾讯前缀码 sh600241"""
    c = str(code).strip()
    if c.startswith(("sh", "sz", "bj")):
        return c
    if c.startswith("6"):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    return "bj" + c


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
                    price, prev = float(f[3] or 0), float(f[4] or 0)
                    chg = (price / prev - 1) * 100 if prev else 0
                    out.append({"code": code, "name": f[1], "price": round(price, 2),
                                "prev": round(prev, 2), "chg_pct": round(chg, 2)})
                except (ValueError, IndexError):
                    pass
            if out:
                return out
        except Exception as e:
            print(f"[indices] retry: {e}", file=sys.stderr)
    return []


# ─── 涨跌停池 (东财, 支持回看历史 date) ────────────────────────────────────
def fetch_pool(kind, date_str, retries=3):
    """kind: ZT/DT/ZB. price 单位: 千分之一元 (p 字段)。"""
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
           f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            pool = ((d.get("data") or {}).get(f"{kind}Pool")) or ((d.get("data") or {}).get("pool")) or []
            out = []
            for p in pool:
                pval = float(p.get("p", 0) or 0)
                out.append({
                    "code": str(p.get("c", p.get("code", ""))),
                    "name": str(p.get("n", p.get("name", ""))),
                    "price": round(pval / 1000, 2) if pval > 100 else pval,
                    "chg_pct": round(float(p.get("zdp", 0) or 0), 2),
                    "lianban": int(p.get("lbc", p.get("count", 1)) or 1),
                    "zt_time": str(p.get("fbt", "")),
                })
            return out
        except Exception as e:
            print(f"[pool {kind}] retry: {e}", file=sys.stderr)
    return []


# ─── 概念板块资金流 (东财 clist, WAF 不稳定 → 自动降级) ──────────────────
def fetch_concepts(limit=150, retries=2):
    url = (f"https://push2.eastmoney.com/api/qt/clist/get"
           f"?pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2+f:!50"
           f"&fields=f12,f14,f3,f6,f62")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8).read()
            d = json.loads(body.decode("utf-8"))
            diff = ((d.get("data") or {}).get("diff")) or []
            out = []
            for c in diff:
                out.append({
                    "code": str(c.get("f12", "")), "name": str(c.get("f14", "")),
                    "chg_pct": float(c.get("f3") or 0),
                    "amount": float(c.get("f6") or 0),
                    "main_net_in": float(c.get("f62") or 0),
                })
            return out
        except Exception as e:
            print(f"[concepts] retry: {e}", file=sys.stderr)
    return []


def _http_json(url, timeout=12):
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
    return json.loads(body.decode("utf-8"))


# ─── 新闻 (新浪7x24 主 + 东财/财联社备 + 远端 data-news 回退) ────────────
def fetch_news(limit=120):
    news, seen = [], set()

    def add(ts, title, src):
        title = re.sub(r"\s+", " ", str(title)).strip()
        if not title or title in seen:
            return
        seen.add(title)
        news.append({"ts": str(ts)[:19], "title": title[:120], "theme": sorted(theme_of(title)),
                     "sentiment": sentiment_of(title), "src": src, "digest": ""})

    # 新浪 7x24 (主源)
    try:
        d = _http_json(f"https://app.cj.sina.com.cn/api/news/pc?page=1&num={limit}")
        feed = d.get("result", {}).get("data", {}).get("feed", {}).get("list") or \
               d.get("data") or []
        for it in feed[:limit]:
            add(it.get("create_time") or it.get("ctime", ""),
                it.get("rich_text") or it.get("title", ""), "sina")
    except Exception as e:
        print(f"[news-sina] {e}", file=sys.stderr)
    # 东财 7x24 (财经/政策/产经, 静默降级)
    for ntype, label in [(8, "em-财经"), (11, "em-政策"), (12, "em-产经")]:
        try:
            d = _http_json(f"https://np-listapi.eastmoney.com/comm/wap/getListInfo?type={ntype}&pageSize=40&pageNo=1&client=web&source=XHH")
            data = d.get("data") if isinstance(d, dict) else None
            items = (data.get("list") if isinstance(data, dict) else None) or []
            for it in items[:40]:
                add(it.get("showtime") or it.get("ctime", ""), it.get("title", ""), label)
        except Exception as e:
            print(f"[news-{label}] {e}", file=sys.stderr)
    # 财联社电报 (静默降级)
    try:
        d = _http_json("https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6&sign=9dc7288db1f2472d0d42e1ea54f7411f")
        for it in (d.get("data", {}).get("roll_data") or [])[:80]:
            t = re.sub(r"\s+", " ", (it.get("title") or it.get("content") or "")).strip()
            add(it.get("ctime", ""), t, "cls")
    except Exception as e:
        print(f"[news-cls] {e}", file=sys.stderr)
    return news


def fetch_remote_news(raw_base="https://raw.githubusercontent.com/qwerzxcva/stocks-data-news/main/news", days=3):
    out, seen = [], set()
    for i in range(days):
        d = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
             - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            body = urllib.request.urlopen(urllib.request.Request(f"{raw_base}/{d}.md", headers=UA),
                                          timeout=10).read().decode("utf-8", "replace")
            for line in body.splitlines():
                m = re.match(r'\s*-\s*\[(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}(?::\d{2})?)\]\s+(.+)', line)
                if not m:
                    continue
                t = m.group(2).strip()
                if len(t) < 6 or t in seen:
                    continue
                seen.add(t)
                out.append({"ts": m.group(1)[:19], "title": t[:120], "theme": sorted(theme_of(t)),
                            "sentiment": sentiment_of(t), "src": "data-news", "digest": ""})
        except Exception as e:
            print(f"[remote-news {d}] {e}", file=sys.stderr)
    return out


# ─── 个股 RPS (腾讯前复权日K, 批量取 top N) ──────────────────────────────
def fetch_rps(codes, bars=20, limit=40):
    """codes: 裸码/前缀码; 返回 {裸码: {close, chg5, chg20}}"""
    out = {}
    for raw in codes[:limit]:
        code = prefixed(raw)
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{bars},qfq"
            d = _http_json(url, timeout=10)
            node = (d.get("data") or {}).get(code) or {}
            k = node.get("qfqday") or node.get("day") or []
            closes = [float(r[2]) for r in k if len(r) > 2 and float(r[2]) > 0]
            if len(closes) >= 5:
                c0 = closes[-1]
                out[str(raw)] = {
                    "close": round(c0, 2),
                    "chg5": round((c0 / closes[-5] - 1) * 100, 2),
                    "chg20": round((c0 / (closes[-20] if len(closes) >= 20 else closes[0]) - 1) * 100, 2),
                }
            time.sleep(0.05)
        except Exception as e:
            print(f"[rps {code}] {e}", file=sys.stderr)
    return out


# ─── 归一化工具 ────────────────────────────────────────────────────────────
def rank_norm(vals):
    """vals: dict[key, float] → {key: 0..1} 按值升序分位"""
    if not vals:
        return {}
    order = sorted(vals.items(), key=lambda x: x[1])
    n = max(1, len(order) - 1)
    return {k: i / n for i, (k, v) in enumerate(order)}

def clamp01(x):
    return max(0.0, min(1.0, x))


# ─── 市场温度计 + 情绪阶段 (TSP 降级口径: 单日快照, 无 2 日确认) ──────────
def market_phase(zt, dt, zb, yzt, indices):
    max_boards = max((z["lianban"] for z in zt), default=0)
    ge2 = sum(1 for z in zt if z["lianban"] >= 2)
    first_board = sum(1 for z in zt if z["lianban"] == 1)
    seal_rate = len(zt) / max(1, len(zt) + len(zb))
    promo_rate = ge2 / max(1, len(yzt)) if yzt else (ge2 / max(1, len(zt)) if zt else 0.0)
    idx_pct = next((i["chg_pct"] for i in indices if i.get("code") == "sh000001"), 0.0)

    def _score(v, low, high):
        return clamp01((v - low) / (high - low)) * 100

    spec = (_score(len(zt), 35, 97) * 0.30 + _score(seal_rate * 100, 57, 75) * 0.40
            + _score(max_boards, 4, 9) * 0.30)
    profit = _score(len(zt) - len(dt), -20, 60)
    resilience = _score(idx_pct, -2.5, 2.5)
    score = round(0.45 * spec + 0.30 * profit + 0.25 * resilience, 1)

    if ge2 >= 50 or first_board >= 220:
        label, desc = "高潮", "连板/首板密度极高, 情绪顶点, 警惕分歧转一致后的退潮"
    elif max_boards >= 7 and ge2 >= 15 and promo_rate >= 0.23:
        label, desc = "主升", "梯队完整且晋级率高, 主线持续发酵"
    elif ge2 >= 8 and promo_rate >= 0.20:
        label, desc = "发酵", "赚钱效应扩张中, 梯队向上延伸"
    elif max_boards <= 4 and ge2 <= 6 and first_board <= 24:
        label, desc = "冰点", "高度与首板双低, 情绪底部区域, 留意次日反核"
    elif promo_rate <= 0.15 and seal_rate <= 0.60:
        label, desc = "退潮", "晋级率低 + 封板率弱, 存量博弈, 防守优先"
    else:
        label, desc = "修复", "常规波动, 无显著方向"

    return {
        "score": score, "stage": label, "desc": desc,
        "seal_rate": round(seal_rate * 100, 1),
        "max_boards": max_boards, "ge2": ge2, "first_board": first_board,
        "promo_rate": round(promo_rate, 3),
        "components": {"speculation": round(spec, 1), "profit": round(profit, 1),
                       "resilience": round(resilience, 1)},
    }


# ─── 主线排行 (概念板块 × 新闻热度 加权; 空池降级为涨停主题聚类) ─────────
def mainline_rank(concepts, hot_themes, zt):
    if not concepts:
        clusters = {}
        for z in zt:
            for t in theme_of(z["name"]):
                clusters.setdefault(t, []).append(z)
        total = max(1, len(zt))
        biggest = max((len(p) for p in clusters.values()), default=1)
        return [
            {"name": t, "score": round(100 * len(pool) / biggest, 1),
             "note": "降级: 概念接口不可达, 按涨停主题聚类占比", "themes": [t], "up_count": len(pool),
             "leading_name": pool[0]["name"] if pool else ""}
            for t, pool in sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:10]
        ]
    theme_hit = {}
    for c in concepts:
        hits = [t for t in hot_themes if t in c["name"]]
        theme_hit[c["code"]] = (hits, min(1.0, len(hits) / max(1, len(hot_themes))))
    nn_main = rank_norm({c["code"]: c["main_net_in"] for c in concepts})
    nn_chg = rank_norm({c["code"]: c["chg_pct"] for c in concepts})
    nn_amt = rank_norm({c["code"]: c.get("amount", 0) for c in concepts})
    rows = []
    for c in concepts:
        hits, heat = theme_hit[c["code"]]
        s = 100 * (0.45 * nn_main.get(c["code"], 0) + 0.25 * nn_chg.get(c["code"], 0)
                   + 0.15 * nn_amt.get(c["code"], 0) + 0.15 * heat)
        rows.append({**c, "score": round(s, 1), "themes": hits, "news_heat": round(heat, 2)})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows


# ─── 主流程 ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y%m%d")
    ydate = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")

    indices = fetch_indices()
    zt = fetch_pool("ZT", date_str)
    dt = fetch_pool("DT", date_str)
    zb = fetch_pool("ZB", date_str)
    yzt = fetch_pool("ZT", ydate)
    concepts = fetch_concepts(150)
    news = fetch_news(args.limit)
    if len(news) < 30:
        news += fetch_remote_news(days=args.days)

    # 主题热度 (新闻命中 + 正负向)
    theme_news = {}
    for n in news:
        for t in n["theme"]:
            e = theme_news.setdefault(t, {"count": 0, "pos": 0, "neg": 0, "sample": []})
            e["count"] += 1
            if n["sentiment"] == "pos":
                e["pos"] += 1
            elif n["sentiment"] == "neg":
                e["neg"] += 1
            if len(e["sample"]) < 3:
                e["sample"].append(n["title"][:40])
    hot_themes = sorted(theme_news, key=lambda t: theme_news[t]["count"], reverse=True)[:10]

    phase = market_phase(zt, dt, zb, yzt, indices)
    mainlines = mainline_rank(concepts, hot_themes, zt)

    # 连板梯队
    tiers = {}
    for z in zt:
        tiers.setdefault(z["lianban"], []).append(z)
    ladder = {
        "height": phase["max_boards"],
        "tiers": [
            {"boards": b, "count": len(v),
             "codes": [x["code"] for x in sorted(v, key=lambda x: x["zt_time"], reverse=True)[:10]]}
            for b, v in sorted(tiers.items(), reverse=True)
        ],
    }

    # 打分池: 涨停股
    scored_pool = zt[:60]
    pool_codes = [z["code"] for z in scored_pool]
    rps = fetch_rps(pool_codes, limit=40)
    r5 = rank_norm({k: v["chg5"] for k, v in rps.items()})
    r20 = rank_norm({k: v["chg20"] for k, v in rps.items()})

    picks = []
    for z in scored_pool:
        code = z["code"]
        rps5, rps20 = r5.get(code, 0.5), r20.get(code, 0.5)
        # 封板时间: fbt "092500" → 分钟数 (09:30→1.0, 14:50→0)
        fbt = re.sub(r"\D", "", str(z.get("zt_time", "")))
        minutes = int(fbt.zfill(6)) // 100  # 6位 HHMMSS → HH*60+MM
        seal_bonus = clamp01((1450 - max(930, min(minutes, 1450))) / 520)
        is_theme = bool(theme_of(z["name"]) & set(hot_themes))
        score = 100 * (
            0.22 * min(z["lianban"] / 5, 1) +
            0.13 * seal_bonus +
            0.17 * (1.0 if is_theme else 0.0) +
            0.20 * rps5 +
            0.12 * rps20 +
            0.16 * (1.0 if z["lianban"] >= 3 else 0.0)
        )
        picks.append({
            "code": code, "name": z["name"], "price": rps.get(code, {}).get("close", z["price"]),
            "chg_pct": z["chg_pct"], "lianban": z["lianban"], "zt_time": z.get("zt_time", ""),
            "theme_hit": is_theme, "rps5": round(rps5 * 100), "rps20": round(rps20 * 100),
            "score": round(score, 1),
        })
    picks.sort(key=lambda x: x["score"], reverse=True)

    latest = {
        "ts": now.isoformat(),
        "date": date_str,
        "market": {
            "indices": indices,
            "limit_up": len(zt), "limit_down": len(dt), "broken": len(zb),
            "zt_rate": round(len(zt) / max(1, len(zt) + len(zb)) * 100, 1),
            "yesterday_zt": len(yzt),
        },
        "phase": phase,
        "themes": [
            {"name": t, "news_count": theme_news[t]["count"],
             "pos": theme_news[t]["pos"], "neg": theme_news[t]["neg"],
             "sample": theme_news[t]["sample"]}
            for t in hot_themes
        ],
        "mainlines": mainlines[:15],
        "ladder": ladder,
        "limit_up_pool": zt[:50],
        "limit_down_pool": dt[:30],
        "picks": picks[:30],
        "news_recent": news[:60],
        "meta": {"source": "tencent+eastmoney+sina", "algo": "tsp-signal-v3",
                 "zero_cred": True, "ref": "shy3130/tick-stock-panel"},
    }

    out = json.dumps(latest, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"written {args.out} ({len(out)} bytes)", flush=True)
    else:
        print(out)
    print(f"=== summary ===", file=sys.stderr)
    print(f"indices={len(indices)} zt={len(zt)} dt={len(dt)} zb={len(zb)} yzt={len(yzt)} "
          f"concepts={len(concepts)} news={len(news)} picks={len(picks)} rps={len(rps)} "
          f"phase={phase['stage']}({phase['score']})", file=sys.stderr)


if __name__ == "__main__":
    main()
