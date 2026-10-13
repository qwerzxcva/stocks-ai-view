#!/usr/bin/env python3
"""
collect_v2_extras.py — v2 新域采集 + 入库 (announcements/news_items/fund_nav/fund_rebalance/financial_summary)

与 collect_all_sources.py 的关系:
  那个管道产出 latest.json 等前端消费文件; 本脚本只负责把 v2 schema 的 5 张表灌满,
  不复用它的 merge 逻辑 (新域没有多源合并需求, 单源直写)。

只用已验证可用的源 (2026-10-13 探测):
  - em_lcpd_report       东财 datacenter 业绩快报 (RPT_LICO_FN_CPD)   → financial_summary
  - ths_financial_abstract_v2  同花顺财务摘要 v2 (akshare)          → financial_summary
  - em_fund_pingzhong    东财基金重仓 (pingzhongdata JS)            → fund_rebalance
  - em_fund_etf_hist_min 东财 ETF 历史净值 (akshare)                → fund_nav
  - sina_news / em_np_listapi_7x24  新闻                           → news_items
  - em_ann_np            东财公告 (需 XHR 头)                       → announcements

用法:
  python3 collect_v2_extras.py --init                 # 只建表
  python3 collect_v2_extras.py --group financial      # 只采某组
  python3 collect_v2_extras.py --group fund --limit 5 # ETF/基金只采前 N 只
  python3 collect_v2_extras.py                        # 全采 (默认限量)
  python3 collect_v2_extras.py --stats                # 看各表行数
"""
import argparse
import ast
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "stocks-data-live", "stocks.duckdb")


def _http(url, timeout=20, retries=2, headers=None, method="GET", data=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, method=method, data=data)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            if i == retries - 1:
                print(f"    [http-fail] {type(e).__name__} {url[:80]}", file=sys.stderr)
            time.sleep(0.4)
    return b""


def _db_path():
    return os.environ.get("STOCKS_DB") or DEFAULT_DB


def _connect(writable=True):
    import duckdb
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if writable:
        return duckdb.connect(path)
    return duckdb.connect(path, read_only=True)


def _ts():
    return datetime.now(CST).isoformat(timespec="seconds")


# ════════════════════════════════════════════════════════════
# 采集: 东财业绩快报 (RPT_LICO_FN_CPD) → financial_summary
# ════════════════════════════════════════════════════════════
# 接口返回宽表 (每股收益/净利润/ROE/同比... 各一列), 转长表 metric_name/value 入库
EM_LCPD_METRICS = {
    "EPSJB": "基本每股收益", "EPSKCJB": "扣非每股收益",
    "TOTALOPERATEREVE": "营业总收入", "PARENTNETPROFIT": "净利润",
    "KCFJCXSYJLR": "扣非净利润", "ROEJQ": "加权ROE",
    "TOTALOPERATEREVETZ": "营收同比", "PARENTNETPROFITTZ": "净利润同比",
    "BPS": "每股净资产", "MGJZC": "每股经营现金流",
    "XSMLL": "销售毛利率", "TOTAL_ASSETS": "总资产",
}


def fetch_em_lcpd(report_date, max_pages=3):
    """东财业绩快报全市场 (按报告期)。返回长表 records。"""
    out = []
    for page in range(1, max_pages + 1):
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            f"?reportName=RPT_LICO_FN_CPD&columns=ALL&pageSize=50&pageNumber={page}"
            "&sortColumns=UPDATE_DATE,SECURITY_CODE&sortTypes=-1,-1"
            f"&source=WEB&client=WEB&filter=(REPORTDATE%3D%27{report_date}%27)"
        )
        b = _http(url)
        if not b:
            break
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            break
        rows = ((d.get("result") or {}).get("data")) or []
        if not rows:
            break
        for r in rows:
            code = str(r.get("SECURITY_CODE", ""))
            # 接口的 REPORTDATE 可能是 "2026-09-30 00:00:00", 统一裁到日期
            rd_raw = str(r.get("REPORTDATE", "") or "")
            rd = rd_raw.split(" ")[0][:10]
            base = {
                "code": code,
                "report_date": rd,
                "report_name": rd,
                "source": "em_lcpd_report",
            }
            for api_f, cn in EM_LCPD_METRICS.items():
                v = r.get(api_f)
                if v is None or v == "" or v is False:
                    continue
                out.append({**base, "metric_name": cn,
                            "value": str(v),
                            "yoy": str(r.get(api_f + "TZ", "") or ""),
                            "mom": ""})
        print(f"  [em_lcpd] page {page}: {len(rows)} 行")
        if len(rows) < 50:
            break
        time.sleep(0.5)
    return out


# ════════════════════════════════════════════════════════════
# 采集: 同花顺财务摘要 v2 (akshare) → financial_summary
# ════════════════════════════════════════════════════════════
def fetch_ths_financial(codes, sleep_s=0.3):
    """akshare stock_financial_abstract_new_ths, 长表已是 metric_name/value 格式。"""
    try:
        import akshare as ak
    except ImportError:
        print("    [skip] akshare 未安装", file=sys.stderr)
        return []
    out = []
    for i, code in enumerate(codes):
        try:
            df = ak.stock_financial_abstract_new_ths(symbol=code)
        except Exception as e:
            print(f"    [ths-fin] {code} {type(e).__name__}", file=sys.stderr)
            time.sleep(sleep_s)
            continue
        for _, row in df.iterrows():
            out.append({
                "code": code,
                "report_date": str(row.get("report_date", "")),
                "report_name": str(row.get("report_name", "")),
                "metric_name": str(row.get("metric_name", "")),
                "value": str(row.get("value", "")),
                "yoy": str(row.get("yoy", "") or ""),
                "mom": str(row.get("mom", "") or ""),
                "source": "ths_financial_abstract_v2",
            })
        print(f"  [ths-fin] {code}: {len(df)} 行")
        time.sleep(sleep_s)
    return out


# ════════════════════════════════════════════════════════════
# 采集: 东财基金重仓 (pingzhongdata JS) → fund_rebalance
# ════════════════════════════════════════════════════════════
def fetch_fund_rebalance(fund_codes, sleep_s=0.3):
    out = []
    for fc in fund_codes:
        url = f"https://fund.eastmoney.com/pingzhongdata/{fc}.js?v={int(time.time())}"
        b = _http(url)
        if not b:
            continue
        txt = b.decode("utf-8", "ignore")
        # stockCodes 是前10大重仓: stockCodes = ["600519","..."]
        m = re.search(r"stockCodes\s*=\s*(\[[^\]]*\])", txt)
        name_m = re.search(r"fS_name\s*=\s*\"([^\"]*)\"", txt)
        fund_name = name_m.group(1) if name_m else ""
        stocks = []
        if m:
            try:
                stocks = ast.literal_eval(m.group(1))
            except Exception:
                stocks = []
        # stockCodes 是 6位代码+1位市场后缀 (如 6884981 = 688498.SH);
        # 前导零在 JS 数字字面量里已丢失, 需剥离市场位后补零
        rd = datetime.now(CST).strftime("%Y-%m-%d")
        for rank, sc in enumerate(stocks[:10], 1):
            sc_s = str(sc).strip()
            if sc_s.isdigit() and len(sc_s) == 7:
                sc_s = sc_s[:-1].zfill(6)
            elif sc_s.isdigit() and len(sc_s) < 6:
                sc_s = sc_s.zfill(6)
            out.append({
                "fund_code": fc, "report_date": rd,
                "stock_code": sc_s, "stock_name": "",
                "ratio": 0.0, "rank": rank,
                "fund_name": fund_name,
                "source": "em_fund_pingzhong",
            })
        print(f"  [fund-rebal] {fc} {fund_name}: {len(stocks[:10])} 只重仓")
        time.sleep(sleep_s)
    return out


# ════════════════════════════════════════════════════════════
# 采集: 东财 ETF 历史净值 (akshare) → fund_nav
# ════════════════════════════════════════════════════════════
def fetch_fund_nav(fund_codes, start="20000101", end="20500101", sleep_s=0.3):
    try:
        import akshare as ak
    except ImportError:
        print("    [skip] akshare 未安装", file=sys.stderr)
        return []
    out = []
    for fc in fund_codes:
        try:
            df = ak.fund_etf_fund_info_em(fund=fc, start_date=start, end_date=end)
        except Exception as e:
            print(f"    [fund-nav] {fc} {type(e).__name__}", file=sys.stderr)
            time.sleep(sleep_s)
            continue
        for _, row in df.iterrows():
            out.append({
                "fund_code": fc, "fund_name": "",
                "date": str(row.get("净值日期", "")),
                "nav": _num(row.get("单位净值")),
                "cum_nav": _num(row.get("累计净值")),
                "change_pct": _num(row.get("日增长率")),
                "scale_yi": None,
                "source": "em_fund_etf_hist_min",
            })
        print(f"  [fund-nav] {fc}: {len(df)} 行")
        time.sleep(sleep_s)
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════
# 采集: 新闻 (新浪 7x24 + 东财 np-listapi) → news_items
# ════════════════════════════════════════════════════════════
def fetch_news(limit=30):
    out = []
    # 新浪 7x24 (容器内常返回 zhibo_id missing / data=[])
    b = _http("https://zhibo.sina.com.cn/api/zhibo/feed?page=1",
              headers={"Referer": "https://finance.sina.com.cn/"})
    if b:
        try:
            d = json.loads(b.decode("utf-8"))
            for item in ((d.get("result") or {}).get("data") or {}).get("feed", {}).get(
                    "list", [])[:limit]:
                out.append({
                    "title": str(item.get("title", "") or "").strip(),
                    "content": str(item.get("rich_text", "") or "")[:500],
                    "source": "sina_news",
                    "pub_time": str(item.get("create_time", "")),
                    "url": str(item.get("docurl", "")),
                })
        except Exception as e:
            print(f"    [news-sina] parse {type(e).__name__}", file=sys.stderr)
    # 东财 np-listapi 7x24 (req_trace 随机 UUID)
    import uuid
    trace = uuid.uuid4().hex
    b = _http(
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        f"?client=web&biz=web7_24&fastColumn=100&column=100&req_trace={trace}"
        "&pageSize=30&sortEnd=&sortType=0")
    if b:
        try:
            d = json.loads(b.decode("utf-8"))
            for item in (d.get("data") or {}).get("list", [])[:limit]:
                out.append({
                    "title": str(item.get("title", "") or "").strip(),
                    "content": str(item.get("digest", "") or "")[:500],
                    "source": "eastmoney_news",
                    "pub_time": str(item.get("showTime", "")),
                    "url": str(item.get("url", "")),
                })
        except Exception as e:
            print(f"    [news-em] parse {type(e).__name__}", file=sys.stderr)
    out = [r for r in out if r["title"]]
    n_sina = sum(1 for r in out if r["source"] == "sina_news")
    n_em = len(out) - n_sina
    print(f"  [news] 新浪 {n_sina} + 东财 {n_em}")
    # 两个直连源都空 → 远端 news 仓兜底 (与 collect_all_sources 的降级链一致)
    if not out:
        print("  [news] 直连源均空, 走远端 news 仓兜底")
        out = fetch_remote_news(limit)
        if out:
            print(f"  [news] 远端兜底 {len(out)} 条")
    return out


def fetch_remote_news(limit=30):
    """远端 news 仓兜底: raw.githubusercontent.com/qwerzxcva/stocks-data-news/main/news/{date}.md"""
    out = []
    for back in range(0, 5):
        day = (datetime.now(CST) - timedelta(days=back)).strftime("%Y-%m-%d")
        url = (f"https://raw.githubusercontent.com/qwerzxcva/stocks-data-news/"
               f"main/news/{day}.md")
        b = _http(url, timeout=15, retries=1)
        if not b:
            continue
        txt = b.decode("utf-8", "ignore")
        # 每条新闻形如 "- [HH:MM] 标题" 或 "## 标题"
        for line in txt.splitlines():
            line = line.strip()
            m = re.match(r"^[-*]\s*\[?(\d{1,2}:\d{2})\]?\s*(.+)$", line)
            if m:
                out.append({
                    "title": m.group(2).strip()[:200],
                    "content": "",
                    "source": "remote_news",
                    "pub_time": f"{day}T{m.group(1)}:00+08:00",
                    "url": url,
                })
        if out:
            break
    return out[:limit]


# ════════════════════════════════════════════════════════════
# 采集: 东财公告 (np-ann, 需 XHR 头) → announcements
# ════════════════════════════════════════════════════════════
def fetch_announcements(codes, per_code=5, sleep_s=0.4):
    out = []
    for code in codes:
        url = ("https://np-ann.eastmoney.com/api/security/ann"
               f"?sr=-1&page_size={per_code}&page_idx=1&ann_type=A"
               f"&client_source=web&f_node=0&s_code={code}")
        b = _http(url, headers={"X-Requested-With": "XMLHttpRequest",
                                "Referer": "https://data.eastmoney.com/"})
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            # 返回 HTML SPA 壳 → 该源仍 degraded
            print(f"    [ann] {code} 非 JSON (SPA 壳?)", file=sys.stderr)
            continue
        items = (d.get("data") or {}).get("list") or d.get("list") or []
        for it in items:
            out.append({
                "code": code,
                "name": str(it.get("sec_name", "") or it.get("short_title", "")),
                "title": str(it.get("title", "")),
                "ann_date": str(it.get("notice_date", "") or "")[:10],
                "ann_type": str(it.get("ann_type", "") or ""),
                "url": str(it.get("art_code", "") and
                           f"https://data.eastmoney.com/notices/detail/{code}/{it.get('art_code')}.html"
                           or ""),
                "source": "em_ann_np",
            })
        print(f"  [ann] {code}: {len(items)} 条")
        time.sleep(sleep_s)
    return out


# ════════════════════════════════════════════════════════════
# 组定义
# ════════════════════════════════════════════════════════════
GROUPS = {
    "financial": {
        "desc": "财务摘要 (东财业绩快报 + 同花顺摘要)",
        "loader": "load_financial_summary",
        "collect": lambda ctx: (
            fetch_em_lcpd(ctx["report_date"], ctx.get("max_pages", 3)) +
            fetch_ths_financial(ctx.get("fin_codes", []))
        ),
    },
    "fund": {
        "desc": "基金重仓 + ETF 净值",
        "loader": "load_fund_rebalance+load_fund_nav",
        "collect": lambda ctx: None,  # 特殊处理 (两个 loader)
    },
    "news": {
        "desc": "新闻快讯 (新浪 + 东财)",
        "loader": "load_news_items",
        "collect": lambda ctx: fetch_news(ctx.get("news_limit", 30)),
    },
    "announcements": {
        "desc": "公告 (东财 np-ann)",
        "loader": "load_announcements",
        "collect": lambda ctx: fetch_announcements(
            ctx.get("ann_codes", []), ctx.get("ann_per_code", 5)),
    },
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="只建表")
    ap.add_argument("--group", help="只采某组: financial/fund/news/announcements")
    ap.add_argument("--limit", type=int, default=5, help="基金/公告/财务代码数上限")
    ap.add_argument("--max-pages", type=int, default=3, help="业绩快报页数")
    ap.add_argument("--report-date", help="报告期 YYYY-MM-DD (默认最近一个季度末)")
    ap.add_argument("--news-limit", type=int, default=30)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.init:
        import db_layer
        db_layer.init_db()
        print(f"[init] {_db_path()}")
        return 0

    if args.stats:
        con = _connect(writable=False)
        for t in ["quotes", "kline_daily", "events", "margin", "pools", "sources_log",
                  "announcements", "news_items", "fund_nav", "fund_rebalance",
                  "financial_summary"]:
            try:
                n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                print(f"  {t:20s} {n}")
            except Exception:
                print(f"  {t:20s} -")
        con.close()
        return 0

    import db_layer

    # 默认代码集: 用库里已有的 quotes 最新一批 code (避免硬编码)
    con = _connect()
    try:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM quotes ORDER BY code LIMIT 200").fetchall()]
    except Exception:
        codes = []
    con.close()
    fin_codes = codes[:args.limit]
    fund_codes = ["510500", "511280", "510300", "159915", "588000"][:args.limit]
    ann_codes = codes[:args.limit]

    report_date = args.report_date
    if not report_date:
        # 最近一个季度末
        now = datetime.now(CST)
        q = {(3, 31), (6, 30), (9, 30), (12, 31)}
        y, m, d = now.year, now.month, now.day
        for (qm, qd) in sorted(q):
            if (m, d) > (qm, qd):
                report_date = f"{y}-{qm:02d}-{qd:02d}"
        if not report_date:
            report_date = f"{y - 1}-12-31"
    print(f"[ctx] report_date={report_date} fin_codes={len(fin_codes)} "
          f"fund_codes={len(fund_codes)} ann_codes={len(ann_codes)}")

    ctx = {
        "report_date": report_date, "fin_codes": fin_codes,
        "fund_codes": fund_codes, "ann_codes": ann_codes,
        "news_limit": args.news_limit, "max_pages": args.max_pages,
        "ann_per_code": 5,
    }

    groups = [args.group] if args.group else list(GROUPS)
    con = _connect()
    con.execute(db_layer.SCHEMA)
    for idx in db_layer.INDEXES:
        try:
            con.execute(idx)
        except Exception:
            pass

    for g in groups:
        spec = GROUPS[g]
        print(f"[{g}] {spec['desc']}")
        try:
            if g == "fund":
                recs = fetch_fund_rebalance(ctx["fund_codes"])
                n = db_layer.load_fund_rebalance(con, recs) if recs else 0
                print(f"  → fund_rebalance +{n}")
                recs = fetch_fund_nav(ctx["fund_codes"])
                n = db_layer.load_fund_nav(con, recs) if recs else 0
                print(f"  → fund_nav +{n}")
            else:
                recs = spec["collect"](ctx) or []
                loader = getattr(db_layer, spec["loader"])
                n = loader(con, recs) if recs else 0
                print(f"  → {spec['loader']} +{n}")
        except Exception as e:
            print(f"  [group-fail] {g}: {type(e).__name__}: {e}", file=sys.stderr)
    con.close()
    print(f"[done] {_db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
