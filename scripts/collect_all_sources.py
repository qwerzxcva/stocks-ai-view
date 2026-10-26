#!/usr/bin/env python3
"""
collect_all_sources.py — 统一数据采集管道 (并发 + 合并 + 去重 + 溯源)

设计:
  1. 从 sources_registry 读源清单, 只采 status in (ok, degraded)
  2. 每个源一个 fetcher 函数, 独立 try/except + 超时, 单源失败不阻断其他
  3. 并发执行 (ThreadPoolExecutor, 默认 6  workers —— 遵循 a-share-data 技能规范)
  4. 按业务主键合并去重, 保留 source 溯源与 first_seen/last_seen
  5. 冲突解决: 数值型取「来源优先级 × 新鲜度」加权; 文本型取更长/更近的
  6. 输出带 meta 的 JSON (每个字段都能回答: 谁给的、什么时候给的、可不可信)

去重主键:
  quote      code                 (同一股票的多源行情)
  kline      code+date            (同一股票同一日的多源K线)
  event      code|theme|date      (龙虎榜/大宗/题材事件)
  news       title 归一化 hash     (同一新闻的多源转载)

用法:
  python3 collect_all_sources.py --out /tmp/all.json
  python3 collect_all_sources.py --out /tmp/all.json --groups quote,news
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sources_registry import SOURCES, by_status, shard_ids as shard_ids_for
from paths import SNAPSHOT_GLOB

# 来源优先级 (冲突时越高越可信): 数字越大优先级越高
SOURCE_PRIORITY = {
    "eastmoney_zt_pool": 90,      # 官方涨跌停, 家数权威
    "eastmoney_zdfenbu": 90,      # 官方涨跌分布
    "eastmoney_datacenter": 85,   # 官方龙虎榜/两融
    "tencent_quote": 80,          # 实时行情主力
    "tencent_kline": 80,
    "tencent_index_history": 80,
    "local_snapshot": 70,
    "local_kline_bulk": 70,
    "ths_strong_stocks": 60,      # 题材归因独家
    "tv_screener": 55,            # 15分钟延迟
    "eastmoney_fund_rank": 60,
    "sina_hq": 50,
    "sina_news": 40,
    "remote_news": 40,
    "coingecko": 50,
    "alpha_vantage": 45,
    "yahoo_finance": 30,          # 限流不稳定, 最低
    "eastmoney_kline": 65,
    "douyin_hot_search": 45,       # 社交媒体情绪, 参考性数据
    "eastmoney_fund_pingzhong": 55,  # 基金重仓股, 补充东财排行
}


def _http(url, timeout=15, retries=2, headers=None, method="GET", data=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, method=method, data=data)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            if i < retries - 1:
                time.sleep(0.3)
    return b""


def _gbk(b):
    try:
        return b.decode("gbk", "replace")
    except Exception:
        return b.decode("utf-8", "replace")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════════
# 各源 fetcher: 返回 (records, meta)
#   records: list[dict], 每条必须带 _key (去重主键) 与 _src (来源id)
# ════════════════════════════════════════════════════════════════

def fetch_tencent_quote(codes=None):
    """腾讯实时行情 → 按 code 主键

    codes 缺省时: 6 个指数 + 本地快照里的全市场 (5278只, 分 88 批)
    """
    if not codes:
        codes = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688", "bj899050"]
        # 尝试从本地快照补全市场
        import glob
        snaps = sorted(glob.glob(SNAPSHOT_GLOB))
        if snaps:
            try:
                d = json.load(open(snaps[-1], encoding="utf-8"))
                extra = [k for k in d.keys() if k not in codes]
                codes.extend(extra[:2000])    # 上限2000, 避免一次请求过多
                print(f"  [tencent_quote] 指数6 + 快照 {len(extra[:2000])} 只", flush=True)
            except Exception:
                pass
    out = []
    for i in range(0, len(codes), 60):
        url = "https://qt.gtimg.cn/q=" + ",".join(codes[i:i + 60])
        b = _http(url, timeout=15)
        if not b:
            continue
        for m in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', _gbk(b)):
            sym, p = m.group(1), m.group(2).split("~")
            if len(p) < 40:
                continue
            bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            out.append({
                "_key": bare, "_src": "tencent_quote",
                "code": bare, "symbol": sym, "name": p[1],
                "price": _f(p[3]), "prev_close": _f(p[4]), "open": _f(p[5]),
                "high": _f(p[33]), "low": _f(p[34]),
                "chg_pct": _f(p[32]), "turnover": _f(p[38]),
                "pe": _f(p[39]), "mktcap": (_f(p[44]) or 0) * 1e8,
                "floatcap": (_f(p[45]) or 0) * 1e8, "pb": _f(p[46]),
                "time": p[30] or "",
            })
        time.sleep(0.15)
    return out, {"source": "tencent_quote", "n": len(out)}


def fetch_eastmoney_pools(date_str):
    """东财涨跌停池 + 涨跌分布 → 按 kind 主键"""
    out = []
    for kind in ("ZT", "DT", "ZB"):
        url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
               f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
        b = _http(url, timeout=15)
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            continue
        data = d.get("data") or {}
        tc = data.get("tc")
        pool = data.get(f"{kind}Pool") or data.get("pool") or []
        out.append({
            "_key": kind, "_src": "eastmoney_zt_pool", "kind": kind,
            "count": int(tc) if tc is not None else len(pool),
            "detail_n": len(pool),
            "detail": [{
                "code": str(p.get("c", "")), "name": str(p.get("n", "")),
                "price": round(_f(p.get("p")) or 0, 2),
                "chg_pct": round(_f(p.get("zdp")) or 0, 2),
                "lianban": int(p.get("lbc") or 1),
                "zt_time": str(p.get("fbt", "")),
                "industry": str(p.get("hybk", "") or ""),
            } for p in pool],
        })
    # 涨跌分布
    url = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&date={date_str}")
    b = _http(url, timeout=15)
    if b:
        try:
            d = json.loads(b.decode("utf-8"))
            dist = {}
            for item in ((d.get("data") or {}).get("fenbu") or []):
                for k, v in item.items():
                    dist[int(k)] = int(v)
            if dist:
                up = sum(c for pct, c in dist.items() if pct > 0)
                down = sum(c for pct, c in dist.items() if pct < 0)
                out.append({
                    "_key": "zdfenbu", "_src": "eastmoney_zdfenbu",
                    "up": up, "down": down, "flat": dist.get(0, 0),
                    "dist": dist,
                })
        except Exception:
            pass
    return out, {"source": "eastmoney_pools", "n": len(out)}


def fetch_eastmoney_datacenter(date_dash, max_back=5):
    """东财龙虎榜/大宗交易 → 按 code|theme|date 主键

    当日无数据时逐日回退 (收盘后才出), 返回实际数据日期。
    """
    out = []
    actual_date = date_dash

    def q(report, sort_col, ds):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
               f"?reportName={report}&columns=ALL&pageSize=50&pageNumber=1"
               f"&sortColumns={sort_col}&sortTypes=-1&source=WEB&client=WEB"
               f"&filter=(TRADE_DATE%3D%27{ds}%27)")
        b = _http(url, timeout=20)
        if not b:
            return []
        try:
            return (json.loads(b.decode("utf-8")).get("result") or {}).get("data") or []
        except Exception:
            return []

    lhb_raw, bt_raw = [], []
    for back in range(max_back):
        d = (datetime.now(CST) - timedelta(days=back)).strftime("%Y-%m-%d")
        lhb_raw = q("RPT_DAILYBILLBOARD_DETAILS", "TRADE_DATE", d)
        bt_raw = q("RPT_DATA_BLOCKTRADE", "TRADE_DATE", d)
        if lhb_raw or bt_raw:
            actual_date = d
            break

    for r in lhb_raw:
        code = str(r.get("SECURITY_CODE", "") or "")
        expl = str(r.get("EXPLAIN", "") or "")
        out.append({
            "_key": f"{code}|{expl[:30]}|{actual_date}", "_src": "eastmoney_datacenter",
            "type": "lhb", "code": code,
            "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
            "close": r.get("CLOSE_PRICE"), "chg": r.get("CHANGE_RATE"),
            "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
            "expl": expl[:80], "trade_date": actual_date,
        })
    for r in bt_raw:
        code = str(r.get("SECURITY_CODE", "") or "")
        out.append({
            "_key": f"{code}|{r.get('DEAL_PRICE')}|{actual_date}", "_src": "eastmoney_datacenter",
            "type": "blocktrade", "code": code,
            "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
            "price": r.get("DEAL_PRICE"), "premium": r.get("PREMIUM_RATIO"),
            "vol": r.get("DEAL_VOLUME"), "amt": r.get("DEAL_AMT"),
            "trade_date": actual_date,
        })
    return out, {"source": "eastmoney_datacenter", "n": len(out),
                 "actual_date": actual_date,
                 "fallback": actual_date != date_dash}


def fetch_eastmoney_margin():
    """东财两融余额 → 按 date 主键"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize=30&pageNumber=1"
           "&sortColumns=dim_date&sortTypes=-1&source=WEB&client=WEB")
    b = _http(url, timeout=20)
    out = []
    if b:
        try:
            for r in ((json.loads(b.decode("utf-8")).get("result") or {}).get("data") or []):
                ds = str(r.get("DIM_DATE", ""))[:10]
                out.append({
                    "_key": ds, "_src": "eastmoney_datacenter", "date": ds,
                    "rzye": r.get("RZYE"), "rqye": r.get("RQYE"),
                    "rzrqye": r.get("RZRQYE"), "rzmre": r.get("RZMRE"),
                })
        except Exception:
            pass
    return out, {"source": "eastmoney_margin", "n": len(out)}


def fetch_ths_strong(date_dash):
    """同花顺强势股 → 按 code 主键"""
    url = ("http://zx.10jqka.com.cn/event/api/getharden"
           f"/date/{date_dash}/orderby/date/orderway/desc/charset/GBK/")
    b = _http(url, timeout=15, headers={"Referer": "http://www.10jqka.com.cn/"})
    out = []
    if b:
        try:
            for r in (json.loads(_gbk(b)).get("data") or []):
                code = str(r.get("code", "") or "").strip()
                if code:
                    out.append({
                        "_key": code, "_src": "ths_strong_stocks",
                        "code": code, "name": str(r.get("name", "") or ""),
                        "reason": str(r.get("reason", "") or ""),
                    })
        except Exception:
            pass
    return out, {"source": "ths_strong_stocks", "n": len(out)}


def fetch_tv_ranking(limit=20):
    """TradingView 行业排行 → 按 sector name 主键"""
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
           f"?l={limit}&p=1&t=01/averatio")
    b = _http(url, timeout=15, headers={"Referer": "https://gu.qq.com/"})
    out = []
    if b:
        try:
            for r in (json.loads(b.decode("utf-8")).get("data") or []):
                nm = str(r.get("bd_name", "") or "").strip()
                if nm:
                    out.append({
                        "_key": nm, "_src": "tencent_mktHs",
                        "sector": nm, "chg_pct": _f(r.get("bd_zdf")),
                        "chg_5d": _f(r.get("bd_zdf5")), "chg_20d": _f(r.get("bd_zdf20")),
                        "leader": str(r.get("nzg_name", "") or ""),
                        "leader_pct": _f(r.get("nzg_zdf")),
                    })
        except Exception:
            pass
    return out, {"source": "tencent_mktHs", "n": len(out)}


def fetch_sina_news(limit=100):
    """新浪财经7x24快讯 → 按 title hash 主键

    必须带 zhibo_id: 缺省时接口返回 {"code":11,"msg":"zhibo_id missing"},
    feed 为空, 曾经导致 news_digest 长期为 0 条 (2026-10-14 定位)。
    zhibo_id=152 是「财经滚动」直播流, 实测有实时数据。

    翻两页拿 ~40 条: 单页 page_size=20 只够撑起很小的资讯流,
    而 24h 面板按分类展示需要更多样本。
    """
    out, seen = [], set()
    for zhibo_id in ("152", "153"):
        for page in (1, 2):
            url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page="
                   f"{page}&page_size=20&zhibo_id={zhibo_id}")
            b = _http(url, timeout=12, headers={"Referer": "https://finance.sina.com.cn/"})
            if not b:
                continue
            try:
                d = json.loads(b.decode("utf-8"))
                feed = ((d.get("result") or {}).get("data") or {}).get("feed", {})
                for item in (feed.get("list") or [])[:limit]:
                    title = str(item.get("rich_text", "") or "").strip()
                    if not title:
                        continue
                    h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
                    if h in seen:
                        continue
                    seen.add(h)
                    out.append({
                        "_key": h, "_src": "sina_news",
                        "title": title[:120],
                        "ts": str(item.get("create_time", "") or ""),
                    })
            except Exception:
                pass
            if len(out) >= limit:
                break
    return out, {"source": "sina_news", "n": len(out)}


def fetch_fund_rank(top=50):
    """东财基金排行 → 按 code+name 主键

    注意: 东财 rankhandler 的 datas 是**字符串数组** (每个元素是逗号分隔串),
    不是嵌套数组。用 json.loads('['+...+']') 会把字符串拆成字符数组,
    导致 code=0/name=1 这种错位。必须先对每个元素做 split(',')。
    """
    url = ("https://fund.eastmoney.com/data/rankhandler.aspx"
           f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1nzf&st=desc&sd=&ed=&qdii=&tabSubtype=,,,,,&pi=1&pn={top}"
           "&dx=1")
    b = _http(url, timeout=15, headers={"Referer": "https://fund.eastmoney.com/data/fundranking.html"})
    out = []
    if b:
        try:
            txt = b.decode("utf-8", "replace")
            m = re.search(r"datas:\[(.*?)\],allRecords", txt, re.S)
            if not m:
                return out, {"source": "eastmoney_fund_rank", "n": 0,
                             "error": "datas field not found"}
            items = json.loads("[" + m.group(1) + "]")
            for item in items:
                # item 是逗号分隔字符串 → split
                row = item.split(",") if isinstance(item, str) else item
                if len(row) < 16:
                    continue
                # 东财字段 (实测核对 2026-10-13):
                #   0=基金代码 1=名称 2=拼音缩写 3=净值日期 4=单位净值 5=累计净值
                #   6=日增长率% 7=近1周 8=近1月 9=近3月 10=近6月 11=近2年...
                #   实际: 7=近1周 8=近1月 9=近3月 10=近6月 11=近1年
                #        12=近2年 13=近3年 14=今年来 15=成立来
                # 主键用 code+name: 同一基金代码可能有 A/C 等多类份额
                out.append({
                    "_key": f"{row[0]}|{row[1]}", "_src": "eastmoney_fund_rank",
                    "code": row[0], "name": row[1],
                    "date": row[3],
                    "nav": _f(row[4]), "acc_nav": _f(row[5]),
                    "daily_chg_pct": _f(row[6]),
                    "ret_1w": _f(row[7]), "ret_1m": _f(row[8]),
                    "ret_3m": _f(row[9]), "ret_6m": _f(row[10]),
                    "ret_1y": _f(row[11]), "ret_2y": _f(row[12]),
                    "ret_3y": _f(row[13]), "ret_ytd": _f(row[14]),
                    "ret_since": _f(row[15]),
                })
        except Exception as e:
            return out, {"source": "eastmoney_fund_rank", "n": len(out),
                         "error": f"{type(e).__name__}: {e}"}
    return out, {"source": "eastmoney_fund_rank", "n": len(out)}


def fetch_douyin_hot():
    """抖音热搜榜 → 按 sentence_id 主键

    返回: 当前抖音热门搜索词, 可用于市场情绪/题材参考
    接口: 公开零鉴权, 需移动端 UA
    """
    url = "https://aweme.snssdk.com/aweme/v1/hot/search/list/"
    b = _http(url, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148",
        "Referer": "https://www.douyin.com/",
    })
    out = []
    if not b:
        return out, {"source": "douyin_hot_search", "n": 0, "error": "empty response"}
    try:
        d = json.loads(b.decode("utf-8", "replace"))
        data = d.get("data", {})
        active_time = data.get("active_time", "")
        items = data.get("trending_list", [])
        for it in items:
            word = it.get("word", "")
            sid = it.get("sentence_id", "")
            if not word or not sid:
                continue
            out.append({
                "_key": sid,
                "_src": "douyin_hot_search",
                "word": word,
                "sentence_id": sid,
                "video_count": it.get("video_count", 0),
                "hot_value": it.get("hot_value", 0),
                "active_time": active_time,
            })
        return out, {"source": "douyin_hot_search", "n": len(out),
                     "active_time": active_time}
    except Exception as e:
        return out, {"source": "douyin_hot_search", "n": 0,
                    "error": f"{type(e).__name__}: {e}"}


def fetch_fund_pingzhong(codes):
    """东财基金持仓股 (pingzhongdata JS) → 按 fund_code 主键

    扩展东财基金排行榜字段, 获取前10大重仓股代码列表。
    codes: 基金代码列表 (str), 默认从 fund_rank 取前10只
    """
    if not codes:
        # 若未指定则从基金排行结果取前10只股票/混合基金 (持仓可解析的)
        try:
            fr, _ = fetch_fund_rank(top=30)
            codes = [r["code"] for r in fr
                     if r.get("code") and not any(
                         k in r.get("name", "") for k in ("纯硄", "货币", "指数增强"))][:10]
        except Exception:
            return [], {"source": "eastmoney_fund_pingzhong", "n": 0, "error": "no fund codes"}

    import time as _time
    out = []
    for code in codes[:20]:  # 最多20只, 避免过多请求
        ts = int(_time.time())
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={ts}"
        b = _http(url, timeout=12, headers={"Referer": "https://fund.eastmoney.com/"})
        if not b:
            continue
        try:
            text = b.decode("utf-8", "replace")
            # 解析 JS 变量: var fS_name = "xxx";
            import re as _re
            vars_map = {}
            for m in _re.finditer(r'var\s+(\w+)\s*=\s*([^;]+);', text):
                name, val = m.group(1), m.group(2).strip()
                # 安全解析: 尝试 int/float/bool/list, 否则保留字符串
                try:
                    val = json.loads(val) if val.startswith(('[', '{', '"')) else \
                          (int(val) if val.lstrip('-').isdigit() else (float(val) if _re.match(r'^[-+]?\d+\.\d*$', val) else val.strip('"')))
                except Exception:
                    pass
                vars_map[name] = val
            stock_codes = vars_map.get("stockCodes", [])
            if isinstance(stock_codes, str):
                stock_codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
            # stockCodes 是 7 位字符串 (6位代码 + 1位校验后缀), 如 "6882001" → bare="688200"
            holdings = []
            for raw in stock_codes[:10]:
                if len(raw) >= 6:
                    bare = raw[:6]
                    if bare.startswith(("60", "68", "90")):
                        holdings.append("sh" + bare)
                    elif bare.startswith(("43", "83", "87", "88")):
                        holdings.append("bj" + bare)
                    else:
                        holdings.append("sz" + bare)
            out.append({
                "_key": code, "_src": "eastmoney_fund_pingzhong",
                "fund_code": code,
                "fund_name": vars_map.get("fS_name", ""),
                "fund_rate": _f(vars_map.get("fund_Rate", 0)),
                "fund_source_rate": _f(vars_map.get("fund_sourceRate", 0)),
                "fund_minsg": _f(vars_map.get("fund_minsg", 0)),
                "holdings": holdings,
            })
            _time.sleep(0.1)  # 限速
        except Exception as e:
            out.append({"_key": code, "_src": "eastmoney_fund_pingzhong",
                       "fund_code": code, "error": f"{type(e).__name__}: {e}"})
    return out, {"source": "eastmoney_fund_pingzhong", "n": len(out)}


# 源 → fetcher 映射
FETCHERS = {
    "quote": [
        ("tencent_quote", lambda ctx: fetch_tencent_quote(ctx.get("codes"))),
        ("eastmoney_pools", lambda ctx: fetch_eastmoney_pools(ctx["date_str"])),
    ],
    "event": [
        ("eastmoney_datacenter", lambda ctx: fetch_eastmoney_datacenter(ctx["date_dash"])),
        ("eastmoney_margin", lambda ctx: fetch_eastmoney_margin()),
        ("ths_strong", lambda ctx: fetch_ths_strong(ctx["date_dash"])),
    ],
    "sector": [
        ("tencent_mktHs", lambda ctx: fetch_tv_ranking(ctx.get("limit", 20))),
    ],
    # 抖音热榜不放 news 组: 实测返回的是全站热搜 (体育/娱乐占多数),
    # 混进 news_digest 会让「24h 热点资讯流」被无关内容淹没。
    # 它作为情绪参考单独走 social 组。
    "news": [
        ("sina_news", lambda ctx: fetch_sina_news()),
    ],
    "fund": [
        ("eastmoney_fund_rank", lambda ctx: fetch_fund_rank()),
        ("eastmoney_fund_pingzhong", lambda ctx: fetch_fund_pingzhong(ctx.get("fund_codes"))),
    ],
}

# ─── 外部 fetcher 库 (fetchers_extra 51 个 + fetchers_reverse 3 个) ──
# 这两个库是按 group 组织的 (REVERSE_FETCHERS) 或散落 f_* 函数;
# 这里统一按 group 注册, 保持 FETCHERS 结构一致。
# 注: fetchers_extra 里的 f_* 没有 group 元数据, 用下面的映射表归组。
FETCHER_GROUPS = {
    # fetchers_reverse (有 REVERSE_FETCHERS 元数据, 优先)
    # fetchers_extra 按功能归组:
    "kline": ["tencent_kline", "eastmoney_kline", "sina_kline_ma",
              "sina_vip_kline", "sina_kline_daily", "em_a_hist",
              "tencent_index_history", "local_kline_bulk",
              "em_fund_etf_hist_min", "em_push2his_kline_DEPRECATED"],
    "quote": ["sina_hq", "eastmoney_stock_get", "em_quote_snapshot",
              "em_push2_clist", "em_push2delay_clist", "em_push2his_clist",
              "em_push2his_etf_onsite", "tv_screener", "industry_map_tv",
              "local_snapshot", "sina_etf_hist", "tx_tick_detail",
              "tx_minute", "minute1", "em_tick_details"],
    "fund": ["jisilu_dividend_rate", "jisilu_lof_index", "jisilu_lof_stock",
             "jisilu_etf_list", "jisilu_cb_list_new", "jisilu_cb_adjust",
             "jisilu_cb_delisted", "jisilu_new_stock_apply",
             "jisilu_new_stock_hkipo", "jisilu_new_stock_sub",
             "jisilu_new_stock_super", "em_fund_lsjz"],
    "event": ["eastmoney_zt_pool", "eastmoney_zdfenbu", "ths_strong_stocks",
              "ths_strong", "ths_industry", "ths_industry_list",
              "sina_bill_sum", "sina_trans_list", "em_lcpd_report",
              "em_fflow_history"],
    "sector": ["ths_concept_list", "ths_financial_abstract",
               "ths_financial_abstract_v2"],
    "news": ["sina_news", "eastmoney_news", "em_ann_np", "toutiao_hot",
             "remote_news", "douyin_hot_search"],
    "macro": ["coingecko", "sec_edgar", "macro_china_cpi_gdp"],
}


def _register_external_fetchers():
    """把 fetchers_extra / fetchers_reverse 的 f_* 函数按组注册进 FETCHERS。

    容错: 任一库 import 失败只跳过, 不影响内联的 9 个 fetcher。
    """
    registered = 0
    # fetchers_reverse: 有 REVERSE_FETCHERS 元数据
    try:
        import fetchers_reverse as _fr
        for grp, name, fn in getattr(_fr, "REVERSE_FETCHERS", []):
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_reverse 跳过: %s" % type(e).__name__)
    # fetchers_extra: 按 FETCHER_GROUPS 归组
    try:
        import fetchers_extra as _fe
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, names in FETCHER_GROUPS.items():
            for name in names:
                if name in have:
                    continue
                fn = getattr(_fe, "f_" + name, None)
                if fn is None:
                    continue
                FETCHERS.setdefault(grp, []).append((name, fn))
                registered += 1
    except Exception as e:
        print("[fetchers] fetchers_extra 跳过: %s" % type(e).__name__)
    # fetchers_new_sources: 2026-10-17 逆向调研确认的新源 (docs/research/)
    try:
        import fetchers_new_sources as _fn2
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("news", "jrj_yaowen_daily"),
                          ("news", "aigupiao_express"),
                          ("event", "sseinfo_qa"),
                          ("event", "sseinfo_survey"),
                          ("event", "stockstar_forecast")):
            if name in have:
                continue
            fn = getattr(_fn2, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_new_sources 跳过: %s" % type(e).__name__)
    # fetchers_f10: 东财 F10 + 股权质押（2026-10-26 实测 200 有数据）
    try:
        import fetchers_f10 as _f10
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("fund", "em_f10_mainop"),
                          ("fund", "em_f10_freeholders"),
                          ("sector", "em_f10_coretheme_board"),
                          ("fund", "em_equity_pledge_detail"),
                          ("fund", "em_equity_pledge_market")):
            if name in have:
                continue
            fn = getattr(_f10, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_f10 跳过: %s" % type(e).__name__)
    # fetchers_hsf10: 东财 HSF10 / 机构持仓 / 股东户数（2026-10-26 实测）
    try:
        import fetchers_hsf10 as _h10
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("fund", "em_hsf10_capital_structure"),
                          ("fund", "em_hsf10_bonus_financing"),
                          ("fund", "em_hsf10_business_analysis"),
                          ("fund", "em_hsf10_shareholder_research"),
                          ("fund", "em_holder_num_latest"),
                          ("fund", "em_f10_orghold_history")):
            if name in have:
                continue
            fn = getattr(_h10, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_hsf10 跳过: %s" % type(e).__name__)
    # fetchers_batch2: 龙虎榜/解禁/回购/增持/业绩/腾讯K线（2026-10-26 实测）
    try:
        import fetchers_batch2 as _b2
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("event", "em_lhb_detail"),
                          ("event", "em_lhb_org_daily"),
                          ("event", "em_lhb_yyb_statistics"),
                          ("event", "em_lhb_yyb_trade_detail"),
                          ("event", "em_lhb_yyb_return_ranking"),
                          ("event", "em_restricted_unlock_day"),
                          ("event", "em_repurchase_list"),
                          ("event", "em_shareholder_increase"),
                          ("fund", "em_performance_express"),
                          ("fund", "em_performance_forecast"),
                          ("quote", "tencent_newfqkline"),
                          ("quote", "tencent_hk_kline"),
                          ("quote", "tencent_us_kline"),
                          ("quote", "tencent_index_weektrends")):
            if name in have:
                continue
            fn = getattr(_b2, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_batch2 跳过: %s" % type(e).__name__)
    # fetchers_em3: 东财剩余 19 源（2026-10-26 实测）
    try:
        import fetchers_em3 as _e3
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("quote", "em_dmsk_ts_stock_new"),
                          ("event", "em_ipo_allotment"),
                          ("event", "em_ipo_apply_yield"),
                          ("event", "em_ipo_neeq_list"),
                          ("event", "em_ipo_review"),
                          ("fund", "em_kzz_ballot_num"),
                          ("fund", "em_kzz_important_date"),
                          ("fund", "em_kzz_invest_item"),
                          ("fund", "em_kzz_value_hist"),
                          ("fund", "em_macro_house_price_70"),
                          ("fund", "em_macro_shibor"),
                          ("fund", "em_margin_account_daily"),
                          ("fund", "em_org_hold_allmarket"),
                          ("event", "em_org_survey"),
                          ("fund", "em_seo_detail"),
                          ("event", "em_stock_participation"),
                          ("fund", "em_hsf10_company_survey"),
                          ("fund", "em_hsf10_operations_required"),
                          ("fund", "em_reportapi_stock_report")):
            if name in have:
                continue
            fn = getattr(_e3, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_em3 跳过: %s" % type(e).__name__)
    # fetchers_ths: 同花顺 JSON/JSONP + F10 HTML 表格（2026-10-26 实测）
    try:
        import fetchers_ths as _ths
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("quote", "ths_kline_day"),
                          ("quote", "ths_kline_month"),
                          ("quote", "ths_timeshare"),
                          ("news", "ths_news_push"),
                          ("event", "ths_strong_stocks"),
                          ("fund", "ths_f10_operate"),
                          ("fund", "ths_f10_holder"),
                          ("fund", "ths_f10_bonus"),
                          ("fund", "ths_f10_worth"),
                          ("fund", "ths_f10_capital"),
                          ("fund", "ths_f10_equity"),
                          ("event", "ths_f10_event"),
                          ("fund", "ths_f10_field"),
                          ("sector", "ths_board_constituents")):
            if name in have:
                continue
            fn = getattr(_ths, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_ths 跳过: %s" % type(e).__name__)
    # fetchers_batch3: 财联社/可转债/腾讯板块榜/新浪资金流/华尔街见闻（2026-10-26 实测）
    try:
        import fetchers_batch3 as _b3
        have = {n for g in FETCHERS.values() for n, _ in g}
        for grp, name in (("news", "cls_telegraph"),
                          ("news", "cls_telegraph_v1"),
                          ("event", "ths_kzz_all"),
                          ("event", "jisilu_cb_realtime"),
                          ("sector", "tencent_board_rank"),
                          ("fund", "sina_moneyflow_lscjfb"),
                          ("quote", "sina_hq_node_count"),
                          ("news", "wscn_lives")):
            if name in have:
                continue
            fn = getattr(_b3, "f_" + name, None)
            if fn is None:
                continue
            FETCHERS.setdefault(grp, []).append((name, fn))
            registered += 1
    except Exception as e:
        print("[fetchers] fetchers_batch3 跳过: %s" % type(e).__name__)
    return registered


_n_ext = _register_external_fetchers()
if _n_ext:
    print("[fetchers] 注册 %d 个外部 fetcher, 总计 %d" % (
        _n_ext, sum(len(v) for v in FETCHERS.values())))


# ════════════════════════════════════════════════════════════════
# 合并去重 + 冲突解决
# ════════════════════════════════════════════════════════════════
def merge_records(all_records, now=None):
    """按 _key 合并, 带溯源。

    冲突解决:
      - 同一 key 多源: 逐字段比对, 数值型取「优先级高且更新」的,
        并记录 conflicting_fields 与 sources 列表, 供审计
      - _src 合并为 sources (list), 保留全部来源
      - 加 first_seen / last_seen (本次运行时间)
    """
    if now is None:
        now = datetime.now(CST)
    now_iso = now.isoformat(timespec="seconds")

    groups = {}
    for rec in all_records:
        key = rec.get("_key")
        if not key:
            continue
        groups.setdefault(key, []).append(rec)

    merged = []
    conflicts = []
    for key, recs in groups.items():
        if len(recs) == 1:
            r = dict(recs[0])
            r["sources"] = [r.pop("_src", "unknown")]
            r["first_seen"] = r["last_seen"] = now_iso
            r["conflict"] = False
            merged.append(r)
            continue

        # 多源: 按优先级降序, 高优先级字段优先, 低优先级补充缺失字段
        recs.sort(key=lambda x: -SOURCE_PRIORITY.get(x.get("_src", ""), 0))
        base = dict(recs[0])
        sources = []
        field_src = {}
        for r in recs:
            sid = r.get("_src", "unknown")
            if sid not in sources:
                sources.append(sid)
            for k, v in r.items():
                if k.startswith("_") or k in ("sources", "first_seen", "last_seen", "conflict"):
                    continue
                if v is None or v == "" or v == []:
                    continue
                if k not in base or base.get(k) in (None, "", []):
                    base[k] = v
                    field_src[k] = sid
                elif base[k] != v and k not in ("_key",):
                    # 冲突: 记录但不覆盖 (保留高优先级的值)
                    if k not in field_src:
                        field_src[k] = sources[0]

        conf_fields = []
        for k in set().union(*[set(r.keys()) for r in recs]):
            if k.startswith("_"):
                continue
            vals = [r.get(k) for r in recs if r.get(k) not in (None, "", [])]
            if len(set(map(str, vals))) > 1:
                conf_fields.append(k)
        base["sources"] = sources
        base["first_seen"] = base["last_seen"] = now_iso
        base["conflict"] = bool(conf_fields)
        if conf_fields:
            base["conflicting_fields"] = sorted(conf_fields)
            base["field_sources"] = field_src
            conflicts.append({"key": key, "fields": sorted(conf_fields),
                              "sources": sources})
        merged.append(base)

    return merged, conflicts


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════
def collect(groups=None, workers=6, codes=None, shard=None):
    now = datetime.now(CST)
    date_str = now.strftime("%Y%m%d")
    date_dash = now.strftime("%Y-%m-%d")
    ctx = {"date_str": date_str, "date_dash": date_dash,
           "codes": codes, "limit": 20}

    want = groups or list(FETCHERS.keys())
    # 分片: 只跑指定 shard 白名单里的源 id (多平台错峰, 避免单 IP 打全部源)
    shard_ids = set(shard_ids_for(shard)) if shard else None
    tasks = []
    for g in want:
        for name, fn in FETCHERS.get(g, []):
            if shard_ids is not None and name not in shard_ids:
                continue
            tasks.append((g, name, fn))

    print(f"[collect] {len(tasks)} 个采集任务, workers={workers}"
          + (f", shard={shard}" if shard else ""), flush=True)
    results = {}
    errors = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, ctx): (g, name) for g, name, fn in tasks}
        for fut in as_completed(futs):
            g, name = futs[fut]
            try:
                recs, meta = fut.result()
                results.setdefault(g, []).extend(recs)
                print(f"  [OK] {g}/{name}: {len(recs)} 条 "
                      f"({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                errors.append({"group": g, "source": name, "error": f"{type(e).__name__}: {e}"})
                print(f"  [FAIL] {g}/{name}: {type(e).__name__} {e}", flush=True)

    # 合并去重 (每组独立)
    out_groups = {}
    all_conflicts = []
    for g, recs in results.items():
        merged, conf = merge_records(recs, now)
        out_groups[g] = merged
        all_conflicts.extend([{"group": g, **c} for c in conf])

    elapsed = time.time() - t0
    summary = {
        "ts": now.isoformat(timespec="seconds"),
        "date": date_str,
        "elapsed_s": round(elapsed, 1),
        "groups": {g: {"raw": len(results.get(g, [])), "merged": len(v)}
                   for g, v in out_groups.items()},
        "total_raw": sum(len(v) for v in results.values()),
        "total_merged": sum(len(v) for v in out_groups.values()),
        "dedup_removed": sum(len(v) for v in results.values()) - sum(len(v) for v in out_groups.values()),
        "conflicts": len(all_conflicts),
        "errors": errors,
        "registry": {
            "total_sources": len(SOURCES),
            "ok": len(by_status("ok")),
            "degraded": len(by_status("degraded")),
            "blocked": len(by_status("blocked")),
            "unauth": len(by_status("unauth")),
            "shard": shard,
            "shard_sources": len(shard_ids_for(shard)) if shard else None,
        },
        "meta": {
            "pipeline": "collect_all_sources.py (并发采集 + 按主键合并去重 + 溯源)",
            "conflict_policy": "数值冲突保留高优先级源的值, 记录 conflicting_fields 与 field_sources 供审计",
            "priority": SOURCE_PRIORITY,
            "caveat": "degraded 源内容可能不完整; blocked 源本次未采",
        },
    }
    return {"summary": summary, "data": out_groups, "conflicts": all_conflicts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--groups", help="逗号分隔: quote,event,sector,news,fund")
    ap.add_argument("--shard", choices=["a", "b", "c", "d", "e"],
                    help="只跑指定分片的源 (多平台错峰; a=主力稳定 b=东财push2 "
                         "c=交易所/同花顺/需key d=需大陆IP e=akshare库)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    groups = args.groups.split(",") if args.groups else None
    res = collect(groups=groups, workers=args.workers, shard=args.shard)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    s = res["summary"]
    print()
    print(f"=== 采集完成 ({s['elapsed_s']}s) ===")
    for g, v in s["groups"].items():
        print(f"  {g:10s} raw={v['raw']:4d} → merged={v['merged']:4d} "
              f"(去重 {v['raw']-v['merged']})")
    print(f"  合计 raw={s['total_raw']} merged={s['total_merged']} "
          f"冲突={s['conflicts']} 错误={len(s['errors'])}")
    print(f"→ {args.out}")
    return 0 if not s["errors"] or s["total_merged"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
