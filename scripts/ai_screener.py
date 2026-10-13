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


# ─── 东财基金排行 (混合基金近1年收益, KOL 抱团方向代理指标, 纯 HTTP 免登录) ──
def fetch_fund_rank(days=90, top=50, retries=2):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    sd = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    ed = now.strftime("%Y-%m-%d")
    url = (f"https://fund.eastmoney.com/data/rankhandler.aspx"
           f"?op=ph&dt=kf&ft=hh&rs=&gs=0&sc=1nzf&st=desc&sd={sd}&ed={ed}"
           f"&qdii=&tabType=new&pi=1&pn={top}&dx=")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(
                urllib.request.Request(url, headers={**UA, "Referer": "https://fund.eastmoney.com/"}),
                timeout=12).read().decode("utf-8", "replace")
            m = re.search(r"datas:\s*(\[.*?\])", body, re.DOTALL)
            if not m:
                return []
            rows = json.loads(m.group(1))
            date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            out = []
            for raw in rows:
                p = str(raw).split(",")
                if len(p) < 10:
                    continue
                code = p[0].strip()
                if not code or code == "---":
                    continue
                anchor = -1
                for i in range(3, min(9, len(p))):
                    if date_re.match(p[i].strip()):
                        anchor = i
                        break
                if anchor < 0:
                    continue

                def f(off):
                    j = anchor + off
                    if j >= len(p):
                        return 0.0
                    try:
                        return float(p[j].strip() or 0)
                    except ValueError:
                        return 0.0

                out.append({"code": code, "name": p[1].strip(),
                            "nav": round(f(1), 4),
                            "daily_chg_pct": round(f(3), 2),
                            "ret_1m": round(f(5), 2), "ret_3m": round(f(6), 2),
                            "ret_6m": round(f(7), 2), "ret_1y": round(f(8), 2),
                            "ret_since": round(f(12), 2),
                            "setup_date": p[anchor + 13].strip() if anchor + 13 < len(p) else ""})
            return out
        except Exception as e:
            print(f"[fund-rank] retry: {e}", file=sys.stderr)
    return []


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
    """kind: ZT/DT/ZB. price 单位: 千分之一元 (p 字段)。

    重要: 东财该接口 data.pool 有时为空数组, 但 data.tc 仍是权威计数
    (实测 20261012 DT: pool=[] 而 tc=19)。因此同时返回 tc,
    调用方须用 tc 作为家数, pool 仅作明细。
    """
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
           f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            data = d.get("data") or {}
            tc = data.get("tc")
            pool = data.get(f"{kind}Pool") or data.get("pool") or []
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
                    "industry": str(p.get("hybk", "") or ""),
                })
            # tc 缺失时退回 len(pool); 两者都不可用才返回空
            if tc is None:
                tc = len(pool)
            return {"count": int(tc), "pool": out}
        except Exception as e:
            print(f"[pool {kind}] retry: {e}", file=sys.stderr)
    return {"count": 0, "pool": []}


# ─── 涨跌分布 (上涨/下跌/平家数, 以及各涨幅档位) ───────────────────────────
def fetch_zdfenbu(date_str):
    """东财涨跌分布: data.fenbu 是 [{'-11':19}, {'11':46}, ...] 形式。
    -11 表示跌幅超11%(≈跌停), 11 表示涨幅超11%(≈涨停)。
    """
    url = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&date={date_str}")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
        d = json.loads(body.decode("utf-8"))
        fenbu = ((d.get("data") or {}).get("fenbu")) or []
        dist = {}
        for item in fenbu:
            for k, v in item.items():
                dist[int(k)] = int(v)
        if not dist:
            return {}
        up = sum(c for pct, c in dist.items() if pct > 0)
        down = sum(c for pct, c in dist.items() if pct < 0)
        flat = dist.get(0, 0)
        # 涨停/跌停口径: 涨幅≥11% / 跌幅≤-11% (东财 fenbu 的 11/-11 档)
        zt = dist.get(11, 0)
        dt = dist.get(-11, 0)
        return {
            "up": up, "down": down, "flat": flat, "total": up + down + flat,
            "zt_like": zt, "dt_like": dt,
            "dist": dist,
            "up_pct": round(up / max(1, up + down + flat) * 100, 1),
        }
    except Exception as e:
        print(f"[zdfenbu] {e}", file=sys.stderr)
        return {}


# ─── 同花顺强势股 + 题材归因 (零鉴权, 东财被封时的有效替代源) ───────────
def fetch_ths_strong_stocks(date_str=None):
    """同花顺 zx.10jqka.com.cn 强势股池: 每只带 reason 题材归因。
    返回 [{code, name, reason}]。该源在容器内稳定可用 (东财 push2 被 WAF 时仍通)。
    """
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
    url = ("http://zx.10jqka.com.cn/event/api/getharden"
           f"/date/{date_str}/orderby/date/orderway/desc/charset/GBK/")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        d = json.loads(body.decode("gbk", "replace"))
        rows = d.get("data") or []
        out = []
        for r in rows:
            code = str(r.get("code", "") or "").strip()
            name = str(r.get("name", "") or "").strip()
            reason = str(r.get("reason", "") or "").strip()
            if code:
                out.append({"code": code, "name": name, "reason": reason})
        print(f"[ths] {len(out)} strong stocks", flush=True)
        return out
    except Exception as e:
        print(f"[ths] {e}", file=sys.stderr)
        return []


# ─── 东财 datacenter: 龙虎榜 / 大宗交易 ─────────────────────────────────
def fetch_datacenter(report, sort_col, date=None, page_size=20, code=None):
    """通用东财 datacenter 查询。date 格式 YYYY-MM-DD。"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           f"?reportName={report}&columns=ALL&pageSize={page_size}&pageNumber=1"
           f"&sortColumns={sort_col}&sortTypes=-1&source=WEB&client=WEB")
    if date:
        url += f"&filter=(TRADE_DATE%3D%27{date}%27)"
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        return (d.get("result") or {}).get("data") or []
    except Exception as e:
        print(f"[dc:{report}] {e}", file=sys.stderr)
        return []


def fetch_lhb(date_str=None):
    """龙虎榜明细 → 简化为 [{code, name, close, chg, expl, deal_amt}]"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
    rows = fetch_datacenter("RPT_DAILYBILLBOARD_DETAILS", "TRADE_DATE",
                            date=date_str, page_size=30)
    out = []
    for r in rows:
        out.append({
            "code": str(r.get("SECURITY_CODE", "") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
            "close": r.get("CLOSE_PRICE"),
            "chg": r.get("CHANGE_RATE"),
            "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
            "expl": str(r.get("EXPLAIN", "") or "")[:60],
        })
    return out


def fetch_blocktrade(date_str=None):
    """大宗交易 → [{code, name, price, premium, vol, amt, buyer, seller}]"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
    rows = fetch_datacenter("RPT_DATA_BLOCKTRADE", "TRADE_DATE",
                            date=date_str, page_size=30)
    out = []
    for r in rows:
        out.append({
            "code": str(r.get("SECURITY_CODE", "") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
            "price": r.get("DEAL_PRICE"),
            "premium": r.get("PREMIUM_RATIO"),
            "vol": r.get("DEAL_VOLUME"),
            "amt": r.get("DEAL_AMT"),
        })
    return out


# ─── 两市成交额 (沪深京合计) ─────────────────────────────────────────────
def fetch_turnover():
    """用腾讯行情累加沪深京三市成交额。
    指数本身的 amount 字段不可靠, 改为: 拉主要指数 + 用其成交额字段。
    腾讯指数接口 p[37]=成交额(万元)。
    """
    total_yi = 0.0
    parts = {}
    for code, label in (("sh000001", "沪市"), ("sz399001", "深市")):
        try:
            url = f"https://qt.gtimg.cn/q={code}"
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read()
            text = body.decode("gbk", "replace")
            for m in re.finditer(r'v_(\w+)="([^"]+)"', text):
                p = m.group(2).split("~")
                if len(p) > 37:
                    yi = float(p[37] or 0) / 1e4   # 万元 → 亿元
                    parts[label] = round(yi, 1)
                    total_yi += yi
                break
        except Exception as e:
            print(f"[turnover {code}] {e}", file=sys.stderr)
    if not parts:
        return {}
    return {"parts": parts, "total_yi": round(total_yi, 1)}


# ─── 融资融券 (东财 datacenter, 两融余额序列) ───────────────────────────
def fetch_margin(days=30):
    """RZYE=融资余额 RQYE=融券余额 RZRQYE=两融余额合计 (单位: 元)"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPTA_RZRQ_LSHJ&columns=ALL"
           f"&pageSize={days}&pageNumber=1"
           "&sortColumns=dim_date&sortTypes=-1&source=WEB&client=WEB")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        recs = (d.get("result") or {}).get("data") or []
        out = []
        for r in recs:
            out.append({
                "date": r.get("DIM_DATE", ""),
                "rzye": r.get("RZYE"),        # 融资余额
                "rqye": r.get("RQYE"),        # 融券余额
                "rzrqye": r.get("RZRQYE"),    # 两融余额合计
                "rzmre": r.get("RZMRE"),      # 融资买入额
                "rzche": r.get("RZCHE"),      # 融资偿还额
            })
        return out
    except Exception as e:
        print(f"[margin] {e}", file=sys.stderr)
        return []


# ─── 指数历史日K (画成交量线图; 腾讯源, 东财被封时仍可用) ─────────────────
def fetch_index_kline(code="sh000001", days=120):
    """返回 [{date, close, vol_yi}, ...] (成交额单位: 亿元)"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12).read()
        d = json.loads(body.decode("utf-8"))
        inner = (d.get("data") or {}).get(code) or {}
        arr = inner.get("qfqday") or inner.get("day") or []
        out = []
        for r in arr:
            # r = [date, open, close, high, low, vol(手)]
            out.append({
                "date": r[0],
                "close": float(r[2]),
                "vol": float(r[5]) if len(r) > 5 else 0,
            })
        return out
    except Exception as e:
        print(f"[kline {code}] {e}", file=sys.stderr)
        return []


# ─── 腾讯自选股: 行业板块排行 (零鉴权, 东财被封时的替代源) ───────────
def fetch_tx_sector_ranking(limit=20):
    """腾讯行业板块涨幅/区间涨幅排行。
    返回 [{name, code, changePct, chg5d, chg20d, leader, leaderPct}]
    注: 该接口不含主力净流入字段, mainNetInflow 置 null (前端有兜底)
    """
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
           f"?l={limit}&p=1&t=01/averatio")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        rows = d.get("data") or []
        out = []
        for r in rows:
            name = str(r.get("bd_name", "") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "code": str(r.get("bd_code", "") or ""),
                "changePct": _f(r.get("bd_zdf")),
                "chg5d": _f(r.get("bd_zdf5")),
                "chg20d": _f(r.get("bd_zdf20")),
                "leader": str(r.get("nzg_name", "") or ""),
                "leaderPct": _f(r.get("nzg_zdf")),
                "mainNetInflow": None,   # 该源无此字段
                "mainNetInflow5d": None,
            })
        print(f"[tx-rank] {len(out)} sectors", flush=True)
        return out
    except Exception as e:
        print(f"[tx-rank] {e}", file=sys.stderr)
        return []


def _f(v):
    """安全转 float, 失败返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── 盘面解读 (规则引擎, 零 LLM 依赖, 每次刷新自动更新) ──────────────────
def build_commentary(zt_n, dt_n, zb_n, indices, zdfenbu, turnover, phase,
                     idx_kline, margin, ths_strong):
    """基于当日客观数据生成结构化盘面解读。

    输出: {summary, breadth, volume, emotion, margin_trend, hotspots, risks[]}
    全部由数据推导, 不编造; 数据缺失时对应字段留空并说明。
    """
    c = {"summary": [], "breadth": {}, "volume": {}, "emotion": {},
         "margin_trend": {}, "hotspots": [], "risks": []}

    # ---- 1. 涨跌家数 (市场宽度) ----
    up = zdfenbu.get("up", 0)
    down = zdfenbu.get("down", 0)
    flat = zdfenbu.get("flat", 0)
    tot = max(1, up + down + flat)
    up_ratio = round(up / tot * 100, 1)
    c["breadth"] = {"up": up, "down": down, "flat": flat,
                     "up_ratio": up_ratio, "total": up + down + flat}
    if up_ratio >= 60:
        c["summary"].append(f"普涨格局, 上涨 {up} 家占比 {up_ratio}%, 赚钱效应强")
    elif up_ratio >= 40:
        c["summary"].append(f"涨跌互现, 上涨占比 {up_ratio}%, 结构市特征")
    elif up_ratio >= 25:
        c["summary"].append(f"跌多涨少, 上涨占比仅 {up_ratio}%, 亏钱效应明显")
    else:
        c["summary"].append(f"普跌格局, 上涨占比 {up_ratio}% ({up}/{up+down}), 系统性承压")

    # ---- 2. 指数表现 ----
    sh = next((i for i in indices if i.get("code") == "sh000001"), None)
    sz = next((i for i in indices if i.get("code") == "sz399001"), None)
    if sh:
        c["summary"].append(f"上证 {sh.get('price')} ({sh.get('chg_pct'):+.2f}%)")
    if sz:
        c["summary"].append(f"深成 {sz.get('price')} ({sz.get('chg_pct'):+.2f}%)")

    # ---- 3. 成交量与放缩量 ----
    vols = [(k["date"], k["vol"]) for k in idx_kline if k.get("vol")]
    if len(vols) >= 6:
        today_vol = vols[-1][1]
        prev_vols = [v for _, v in vols[-6:-1]]
        avg5 = sum(prev_vols) / len(prev_vols)
        ratio = today_vol / avg5 if avg5 else 0
        c["volume"] = {
            "today_vol_wan": round(today_vol / 1e4, 1),
            "avg5_vol_wan": round(avg5 / 1e4, 1),
            "ratio": round(ratio, 2),
        }
        if ratio >= 1.25:
            c["summary"].append(f"显著放量, 为5日均量 {ratio:.2f} 倍")
        elif ratio >= 1.08:
            c["summary"].append(f"温和放量 (5日均量 {ratio:.2f} 倍)")
        elif ratio >= 0.92:
            c["summary"].append(f"量能持平 (5日均量 {ratio:.2f} 倍)")
        elif ratio >= 0.75:
            c["summary"].append(f"缩量 (5日均量 {ratio:.2f} 倍)")
        else:
            c["summary"].append(f"严重缩量, 仅为5日均量 {ratio:.2f} 倍, 观望情绪浓")
    if turnover.get("total_yi"):
        c["volume"]["turnover_yi"] = turnover["total_yi"]
        c["volume"]["parts"] = turnover.get("parts", {})
        c["summary"].append(f"沪深两市成交额 {turnover['total_yi']} 亿元")

    # ---- 4. 涨跌停情绪 ----
    seal = round(zt_n / max(1, zt_n + zb_n) * 100, 1)
    c["emotion"] = {"zt": zt_n, "dt": dt_n, "zb": zb_n, "seal_rate": seal,
                     "stage": phase.get("stage"), "score": phase.get("score")}
    if zt_n >= 60 and seal >= 70:
        c["summary"].append(f"涨停 {zt_n} 家、封板率 {seal}%, 情绪处于高位")
    elif zt_n >= 30:
        c["summary"].append(f"涨停 {zt_n} 家、封板率 {seal}%, 情绪中性偏暖")
    else:
        c["summary"].append(f"涨停仅 {zt_n} 家、封板率 {seal}%, 情绪偏冷")
    if dt_n >= 20:
        c["summary"].append(f"跌停 {dt_n} 家, 亏钱效应扩散")

    # ---- 5. 融资融券趋势 ----
    if len(margin) >= 3:
        latest_m = margin[0]
        prev_m = margin[1]
        rz_chg = None
        if latest_m.get("rzye") and prev_m.get("rzye"):
            rz_chg = round((latest_m["rzye"] - prev_m["rzye"]) / prev_m["rzye"] * 100, 3)
        c["margin_trend"] = {
            "date": latest_m.get("date", "")[:10],
            "rzye_yi": round((latest_m.get("rzye") or 0) / 1e8, 1),
            "rzrqye_yi": round((latest_m.get("rzrqye") or 0) / 1e8, 1),
            "rzye_chg_pct": rz_chg,
        }
        if rz_chg is not None:
            if rz_chg > 0.2:
                c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 +{rz_chg}%, 杠杆资金加仓")
            elif rz_chg < -0.2:
                c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 {rz_chg}%, 杠杆资金撤退")
            else:
                c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 {rz_chg}%, 基本持稳")

    # ---- 6. 热点题材 (同花顺归因聚类) ----
    theme_count = {}
    for s in ths_strong:
        for kw in str(s.get("reason", "")).replace("+", " ").split():
            kw = kw.strip()
            if len(kw) >= 2:
                theme_count[kw] = theme_count.get(kw, 0) + 1
    hot = sorted(theme_count.items(), key=lambda x: -x[1])[:8]
    c["hotspots"] = [{"theme": k, "count": v} for k, v in hot if v >= 2]
    if c["hotspots"]:
        top3 = "、".join(f"{x['theme']}({x['count']})" for x in c["hotspots"][:3])
        c["summary"].append(f"资金聚焦题材: {top3}")

    # ---- 7. 风险提示 ----
    if dt_n >= zt_n:
        c["risks"].append(f"跌停({dt_n})≥涨停({zt_n}), 短期风险释放中, 注意仓位")
    if seal < 50 and zt_n > 0:
        c["risks"].append(f"封板率仅 {seal}%, 打板资金接力意愿弱")
    if up_ratio < 30:
        c["risks"].append(f"上涨占比 {up_ratio}%, 普跌环境下逆势股易补跌")
    if phase.get("stage") in ("退潮", "冰点"):
        c["risks"].append(f"情绪周期处于「{phase.get('stage')}」, 降低交易频率")

    return c



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

    # 新浪 7x24 (主源: feed.mix.sina 滚动财经, 结构稳定)
    def _sina_ts(v):
        """feed.mix 的 ct 是 Unix 秒 (int), app.cj 的 create_time 是字符串。"""
        try:
            iv = int(v)
            if iv > 10**9:
                return datetime.datetime.fromtimestamp(
                    iv, datetime.timezone(datetime.timedelta(hours=8))
                ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
        return str(v)[:19]
    try:
        d = _http_json(f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num={limit}&page=1")
        for it in ((d.get("result") or {}).get("data") or [])[:limit]:
            add(_sina_ts(it.get("ct") or it.get("ctime", "")), it.get("title", ""), "sina")
    except Exception as e:
        print(f"[news-sina] {e}", file=sys.stderr)
    # 新浪财经7x24 (备用源, 主源失败时补)
    if not news:
        try:
            d = _http_json(f"https://app.cj.sina.com.cn/api/news/pc?page=1&num={limit}")
            feed = d.get("result", {}).get("data", {}).get("feed", {}).get("list") or \
                   d.get("data") or []
            for it in feed[:limit]:
                add(it.get("create_time") or it.get("ctime", ""),
                    it.get("rich_text") or it.get("title", ""), "sina")
        except Exception as e:
            print(f"[news-sina-cj] {e}", file=sys.stderr)
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
    # 抖音热榜 (免登录公开端点)
    try:
        url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
               "?device_platform=webapp&aid=6383&channel=channel_pc_web"
               "&source=9&hot_board_content=hot_board_default&hot_board_duration=7200")
        body = urllib.request.urlopen(urllib.request.Request(url, headers={**UA, "Referer": "https://www.douyin.com/hot"}),
                                      timeout=15).read()
        d = json.loads(body.decode("utf-8", "replace"))
        for w in (d.get("data") or {}).get("word_list") or []:
            word = str(w.get("word", "")).strip()
            if not word or word in seen or len(word) < 4:
                continue
            seen.add(word)
            add("", f"[抖音热榜] {word}", "douyin-hot")
    except Exception as e:
        print(f"[news-douyin] {e}", file=sys.stderr)
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
def market_phase(zt, dt, zb, yzt, indices, zt_n=None, dt_n=None, zb_n=None):
    # 家数优先用东财 data.tc (pool 明细可能为空), 缺省退回 len(pool)
    zt_n = zt_n if zt_n is not None else len(zt)
    dt_n = dt_n if dt_n is not None else len(dt)
    zb_n = zb_n if zb_n is not None else len(zb)
    max_boards = max((z["lianban"] for z in zt), default=0)
    ge2 = sum(1 for z in zt if z["lianban"] >= 2)
    first_board = sum(1 for z in zt if z["lianban"] == 1)
    seal_rate = zt_n / max(1, zt_n + zb_n)
    promo_rate = ge2 / max(1, len(yzt)) if yzt else (ge2 / max(1, zt_n) if zt_n else 0.0)
    idx_pct = next((i["chg_pct"] for i in indices if i.get("code") == "sh000001"), 0.0)

    def _score(v, low, high):
        return clamp01((v - low) / (high - low)) * 100

    spec = (_score(zt_n, 35, 97) * 0.30 + _score(seal_rate * 100, 57, 75) * 0.40
            + _score(max_boards, 4, 9) * 0.30)
    profit = _score(zt_n - dt_n, -20, 60)
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
    _zt = fetch_pool("ZT", date_str)
    _dt = fetch_pool("DT", date_str)
    _zb = fetch_pool("ZB", date_str)
    _yzt = fetch_pool("ZT", ydate)
    # 家数以东财 data.tc 为准 (pool 明细可能为空), 详见 fetch_pool 注释
    zt, dt, zb, yzt = _zt["pool"], _dt["pool"], _zb["pool"], _yzt["pool"]
    zt_n, dt_n, zb_n, yzt_n = _zt["count"], _dt["count"], _zb["count"], _yzt["count"]

    # 新增: 涨跌分布 / 两市成交额 / 融资融券 / 指数历史K线
    zdfenbu = fetch_zdfenbu(date_str)
    turnover = fetch_turnover()
    margin = fetch_margin(30)
    idx_kline = fetch_index_kline("sh000001", 120)
    # 同花顺强势股(题材归因) + 东财龙虎榜/大宗交易
    # 注意: date_str 是 YYYYMMDD (东财涨跌停池口径),
    #       而 datacenter 接口的 filter 需要 YYYY-MM-DD
    date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    ths_strong = fetch_ths_strong_stocks(date_dash)
    lhb = fetch_lhb(date_dash)
    blocktrade = fetch_blocktrade(date_dash)
    # 腾讯自选股行业排行 (东财被封时的替代源, 前端 txBody tab 消费)
    tx_sector_ranking = fetch_tx_sector_ranking(20)

    concepts = fetch_concepts(150)
    news = fetch_news(args.limit)
    if len(news) < 30:
        news += fetch_remote_news(days=args.days)
    fund_rank = fetch_fund_rank(top=50)

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

    phase = market_phase(zt, dt, zb, yzt, indices, zt_n=zt_n, dt_n=dt_n, zb_n=zb_n)
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
            # 家数以东财 data.tc 为准 (pool 明细可能为空, 详见 fetch_pool 注释)
            "limit_up": zt_n, "limit_down": dt_n, "broken": zb_n,
            "zt_rate": round(zt_n / max(1, zt_n + zb_n) * 100, 1),
            "yesterday_zt": yzt_n,
            # 涨跌分布 (东财 fenbu 口径)
            "advance": zdfenbu.get("up", 0),
            "decline": zdfenbu.get("down", 0),
            "flat": zdfenbu.get("flat", 0),
            "up_pct": zdfenbu.get("up_pct", 0),
            # 两市成交额 (腾讯累加, 单位: 亿元)
            "turnover": turnover,
        },
        "phase": phase,
        "themes": [
            {"name": t, "news_count": theme_news[t]["count"],
             "pos": theme_news[t]["pos"], "neg": theme_news[t]["neg"],
             "sample": theme_news[t]["sample"]}
            for t in hot_themes
        ],
        "mainlines": mainlines[:15],
        "fund_rank": fund_rank[:50],
        "ladder": ladder,
        "limit_up_pool": zt[:50],
        "limit_down_pool": dt[:30],
        "picks": picks[:30],
        "news_recent": news[:60],
        # 融资融券 (最近30日, 单位: 元)
        "margin": margin,
        # 上证指数历史日K (画成交量线图, 单位: 手)
        "index_kline": idx_kline,
        # 同花顺强势股 + 题材归因
        "ths_strong": ths_strong[:40],
        # 腾讯自选股行业板块排行 (前端 txBody tab)
        "westock": {
            "sector_ranking": {"date": date_str, "data": tx_sector_ranking},
        },
        # 龙虎榜 / 大宗交易
        "lhb": lhb[:20],
        "blocktrade": blocktrade[:20],
        # 盘面解读 (规则引擎生成, 不依赖 LLM)
        "commentary": build_commentary(
            zt_n=zt_n, dt_n=dt_n, zb_n=zb_n, indices=indices, zdfenbu=zdfenbu,
            turnover=turnover, phase=phase, idx_kline=idx_kline,
            margin=margin, ths_strong=ths_strong,
        ),
        "meta": {"source": "tencent+eastmoney+sina+ths", "algo": "tsp-signal-v3",
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
    print(f"indices={len(indices)} zt={zt_n}(pool{len(zt)}) dt={dt_n}(pool{len(dt)}) "
          f"zb={zb_n}(pool{len(zb)}) yzt={yzt_n} "
          f"adv={zdfenbu.get('up',0)} dec={zdfenbu.get('down',0)} "
          f"turnover={turnover.get('total_yi',0)}yi margin={len(margin)} kline={len(idx_kline)} "
          f"ths={len(ths_strong)} lhb={len(lhb)} bt={len(blocktrade)} txrank={len(tx_sector_ranking)} "
          f"concepts={len(concepts)} news={len(news)} picks={len(picks)} rps={len(rps)} fundrank={len(fund_rank)} "
          f"phase={phase['stage']}({phase['score']})", file=sys.stderr)


if __name__ == "__main__":
    main()
