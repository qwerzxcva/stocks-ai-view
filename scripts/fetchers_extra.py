#!/usr/bin/env python3
"""
fetchers_extra.py — collect_all_sources.py 的补充 fetcher 库

定位: 把 sources_registry 里「已探测但未接入采集管道」的源补齐成 fetcher。
      每个函数签名与 collect_all_sources 既有 fetcher 一致:

          def f_xxx(ctx) -> (records, meta)

      records 是带 _key / _src 的业务记录列表, meta 说明本次采集状况。
      单源失败不抛异常, 一律返回 ([], {"reason": ...})。

与 collect_all_sources.py 的关系:
  collect_all_sources.py 顶部 from fetchers_extra import * 后, 在 FETCHERS
  里按 group 注册; 本文件不直接执行 (除 __main__ 的自测入口)。

代码来源:
  - 集思录 11 个源:  复用 collect_jisilu.py 的匿名 session + 端点表
  - 东财涨停/涨跌分布: 复用 collect_market_extras.py 的 fetch_pool/fetch_zdfenbu
                     (含 data.tc 权威计数与厘价折算两个坑)
  - K 线多源:        复用 expand_kline_coverage.py / collect_adjusted.py 的双源逻辑
  - 本地/远端源:     复用 collect_v2_extras.py 的 fetch_remote_news + paths.py

多源降级 (与 sources_registry 的 degrade_to 对齐):
  tencent_kline   → eastmoney_kline → sina_kline_ma(接口直返 MA)
  tencent_index_history → sina_kline_ma (指数, ma=5/10)
  sina_hq         → tencent_quote  (同进程内不复用, 由 _src 区分)
  em_push2*       → 三域轮询 + 熔断 (复用 collect_moneyflow 的策略)
"""
import glob
import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}

# 集合 11 个源 (2026-10-13 逆向确认, 老 .php 已全 404, 新版无后缀路径)
JISILU_BASE = "https://www.jisilu.cn"
JISILU_ENDPOINTS = {
    "jisilu_lof_index": ("/data/lof/index_lof_list/", "/data/lof/", "fund_id", "fund"),
    "jisilu_lof_stock": ("/data/lof/stock_lof_list/", "/data/lof/", "fund_id", "fund"),
    "jisilu_etf_list": ("/data/etf/etf_list/", "/data/etf/", "fund_id", "fund"),
    "jisilu_cb_list_new": ("/data/cbnew/cb_list_new/", "/data/cbnew/", "bond_id", "cb"),
    "jisilu_cb_adjust": ("/data/cbnew/adjust_list/", "/data/cbnew/", "bond_id", "cb"),
    "jisilu_cb_delisted": ("/data/cbnew/delisted/", "/data/cbnew/", "bond_id", "cb"),
    "jisilu_new_stock_apply": ("/data/new_stock/apply/", "/data/new_stock/", "stock_cd", "stock"),
    "jisilu_new_stock_hkipo": ("/data/new_stock/hkipo/", "/data/new_stock/", "stock_cd", "stock"),
    "jisilu_new_stock_sub": ("/data/new_stock/sub/", "/data/new_stock/", "stock_cd", "cb"),
    "jisilu_new_stock_super": ("/data/new_stock/super/", "/data/new_stock/", "stock_cd", "stock"),
}
JISILU_DIVIDEND = ("/data/stock/dividend_rate_list/?rp=%d&page=1", "/data/stock/", "stock_id", "stock")

# push2 三域: 容器 IP 层 TLS RST, 必须多域轮询 + 熔断 (collect_moneyflow 的策略)
EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com", "push2his.eastmoney.com")


def _jhttp(url, timeout=8, headers=None, method="GET", data=None, retries=2):
    h = dict(UA)
    if headers:
        h.update(headers)
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, method=method, data=data)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            time.sleep(0.2)
    return b""


def _jnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jstr(v, limit=200):
    s = str(v if v is not None else "").strip()
    return s[:limit]


def _keycode(code):
    """去重主键用的代码口径: 输入带 sh/sz/bj 前缀时保留前缀, 否则裸码。

    裸码会撞键: sh000001 (上证指数) 与 sz000001 (平安银行) 归一并后同为
    000001, 同一批请求里两条记录互相覆盖 (2026-10-14 实测)。
    """
    c = str(code or "").strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    return _bare(c)


def _bare(code):
    c = str(code or "").strip().lower()
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def _tk(code):
    """6 位代码 → 腾讯前缀代码 (sh/sz/bj)。

    已带前缀的原样返回 —— 不能先 _bare 再按首位猜: sh000001 (上证指数)
    与 sz000001 (平安银行) 裸码同为 000001, 猜市场会把指数误判成深市
    (2026-10-14 实测踩坑)。
    """
    raw = str(code or "").strip().lower()
    if raw[:2] in ("sh", "sz", "bj"):
        return raw
    c = _bare(raw)
    if not c:
        return ""
    if c.startswith(("5", "6", "9")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sz" + c


def _secid(code):
    """6 位代码 → 东财 secid (1=沪 0=深)。"""
    raw = str(code or "").strip().lower()
    if raw[:2] in ("sh", "bj"):
        return "1." + raw[2:]
    if raw[:2] == "sz":
        return "0." + raw[2:]
    c = _bare(raw)
    return ("1." if c.startswith(("5", "6", "9")) else "0.") + c


# ════════════════════════════════════════════════════════════════
# 集思录 (11 源): 匿名 session + XHR 头
# ════════════════════════════════════════════════════════════════
_JL_SESSION = {"opener": None}


def _jisilu_opener():
    """首次调用时建匿名 session (先访问首页拿 kbzw__Session cookie)。"""
    import http.cookiejar
    if _JL_SESSION["opener"] is None:
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            op.open(urllib.request.Request(JISILU_BASE + "/", headers=dict(UA)),
                   timeout=10).read()
        except Exception:
            pass
        _JL_SESSION["opener"] = op
    return _JL_SESSION["opener"]


def _jisilu_get(path, referer, timeout=8):
    url = f"{JISILU_BASE}{path}"
    h = dict(UA)
    h["Referer"] = JISILU_BASE + referer
    h["X-Requested-With"] = "XMLHttpRequest"
    try:
        return json.loads(_jisilu_opener().open(
            urllib.request.Request(url, headers=h), timeout=timeout).read().decode("utf-8"))
    except Exception:
        return {}


def _jisilu_rows(sid, rp=20):
    """拉一个集思录端点的 rows (每页 20 行, page 参数无效 → id 去重截断)。"""
    path, referer, key_field, _kind = JISILU_ENDPOINTS[sid]
    rows, seen = [], set()
    for page in (1, 2, 3):
        d = _jisilu_get(f"{path}?page={page}&rp={rp}&___t={int(time.time() * 1000)}",
                        referer)
        batch = d.get("rows") or []
        new = 0
        for r in batch:
            cell = r.get("cell", r)
            rid = r.get("id") or cell.get(key_field) or json.dumps(cell, sort_keys=True,
                                                                   default=str)
            if rid in seen:
                continue
            seen.add(rid)
            cell["_row_id"] = r.get("id", "")
            rows.append(cell)
            new += 1
        if not batch or new == 0:
            break
        time.sleep(0.25)
    return rows


def f_jisilu_dividend_rate(ctx):
    """集思录股息率榜 (200只/页, 单接口信息密度最高: 估值温度+5年均值)。

    cell 字段 (2026-10-14 实测): dividend_rate / dividend_rate2 / pb_temperature /
    pe_temperature / roe_average / revenue_average / profit_average / debt_ratio /
    pledge_ratio / industry / province。
    """
    rp = int(ctx.get("jisilu_rows", 100) or 100)
    path, referer, key_field, _ = JISILU_DIVIDEND
    d = _jisilu_get(path % rp, referer)
    rows = []
    for r in (d.get("rows") or []):
        c = r.get("cell", r)
        code = _bare(c.get(key_field))
        if not code:
            continue
        rows.append({
            "_key": _keycode(code), "_src": "jisilu_dividend_rate",
            "code": code, "name": _jstr(c.get("stock_nm"), 60),
            "dividend_rate": _jnum(c.get("dividend_rate")),
            "dividend_rate2": _jnum(c.get("dividend_rate2")),
            "price": _jnum(c.get("price")),
            "pe": _jnum(c.get("pe")), "pb": _jnum(c.get("pb")),
            "pe_temperature": _jnum(c.get("pe_temperature")),
            "pb_temperature": _jnum(c.get("pb_temperature")),
            "roe_average": _jnum(c.get("roe_average")),
            "revenue_average": _jnum(c.get("revenue_average")),
            "profit_average": _jnum(c.get("profit_average")),
            "debt_ratio": _jnum(c.get("debt_ratio")),
            "industry": _jstr(c.get("industry"), 40),
            "last_dt": _jstr(c.get("last_dt"), 10),
        })
    if not rows:
        return [], {"reason": "jisilu dividend_rate_list 无数据 (session/反爬?)"}
    return rows, {"source": "jisilu_dividend_rate", "n": len(rows)}


def _mk_jisilu_fetcher(sid):
    """按端点类型生成 fetcher (fund/cb/stock 三套字段映射)。"""
    path, referer, key_field, kind = JISILU_ENDPOINTS[sid]

    def fn(ctx):
        rows_in = _jisilu_rows(sid)
        out = []
        for c in rows_in:
            code = _bare(c.get(key_field))
            if not code:
                continue
            rec = {"_key": _keycode(code), "_src": sid, "code": code,
                   "name": _jstr(c.get("fund_nm") or c.get("bond_nm")
                                 or c.get("stock_nm"), 60)}
            if kind == "fund":
                rec.update({
                    "price": _jnum(c.get("price")),
                    "pre_close": _jnum(c.get("pre_close")),
                    "increase_rt": _jnum(c.get("increase_rt")),
                    "volume": _jnum(c.get("volume")),
                    "amount": _jnum(c.get("amount")),
                    "fund_nav": _jnum(c.get("fund_nav")),
                    "nav_incr_rt": _jnum(c.get("nav_incr_rt")),
                    "asset_ratio": _jnum(c.get("asset_ratio")),
                    "index_id": _jstr(c.get("index_id"), 20),
                    "fee": _jnum(c.get("fee")),
                    "m_fee": _jnum(c.get("m_fee")),
                    "issuer_nm": _jstr(c.get("issuer_nm"), 40),
                    "creation_unit": _jnum(c.get("creation_unit")),
                    "price_dt": _jstr(c.get("price_dt"), 10),
                })
            elif kind == "cb":
                rec.update({
                    "price": _jnum(c.get("price")),
                    "increase_rt": _jnum(c.get("increase_rt")),
                    "stock_id": _bare(c.get("stock_id")),
                    "stock_nm": _jstr(c.get("stock_nm"), 60),
                    "sprice": _jnum(c.get("sprice")),
                    "pb": _jnum(c.get("pb")),
                    "convert_price": _jnum(c.get("convert_price")),
                    "convert_value": _jnum(c.get("convert_value")),
                    "premium_rt": _jnum(c.get("premium_rt")),
                    "dblow": _jnum(c.get("dblow")),
                    "rating_cd": _jstr(c.get("rating_cd"), 8),
                    "curr_iss_amt": _jnum(c.get("curr_iss_amt")),
                    "adjust_price_ratio": _jnum(c.get("adjust_price_ratio")),
                    "apply_dt": _jstr(c.get("apply_dt"), 24),
                    "issue_dt": _jstr(c.get("issue_dt"), 10),
                    "redeem_dt": _jstr(c.get("redeem_dt"), 10),
                    "delist_dt": _jstr(c.get("delist_dt"), 10),
                    "maturity_dt": _jstr(c.get("maturity_dt"), 10),
                    "jsl_theory_price": _jnum(c.get("jsl_theory_price")),
                    "issue_amt": _jnum(c.get("issue_amt")),
                    "market": _jstr(c.get("market"), 12),
                    "list_dt": _jstr(c.get("list_dt"), 24),
                    "jsl_advise": _jstr(c.get("jsl_advise"), 40),
                })
            else:
                rec.update({
                    "apply_dt": _jstr(c.get("apply_dt"), 24),
                    "need_market_value": _jnum(c.get("need_market_value")),
                    "issue_price": _jnum(c.get("issue_price")),
                    "above_rt": _jnum(c.get("above_rt")),
                    "pub_pe": _jnum(c.get("pub_pe")),
                    "avg_pe": _jnum(c.get("avg_pe")),
                    "market": _jstr(c.get("market"), 12),
                    "list_dt": _jstr(c.get("list_dt"), 24),
                    "total_shares": _jnum(c.get("total_shares")),
                    "raise_money": _jnum(c.get("raise_money")),
                    "jsl_issue_price": _jnum(c.get("jsl_issue_price")),
                    "valid_apply_account": _jnum(c.get("valid_apply_account")),
                    "total_draw_count": _jnum(c.get("total_draw_count")),
                    "price": _jnum(c.get("price")),
                    "increase_rt": _jstr(c.get("increase_rt"), 12),
                    "issue_amt": _jnum(c.get("issue_amt")),
                })
            out.append(rec)
        if not out:
            return [], {"reason": f"{sid} 无数据 (session/反爬/端点变更)"}
        return out, {"source": sid, "n": len(out), "url": JISILU_BASE + path}

    fn.__name__ = "f_" + sid
    return fn


# ════════════════════════════════════════════════════════════════
# 东财涨停池 / 涨跌分布 (打板池核心)
# ════════════════════════════════════════════════════════════════
def _em_pool(kind, date_str):
    """东财涨跌停池。坑1: data.pool 可能为空但 data.tc 是权威计数;
    坑2: 价格单位是「厘」 (>1000 才折算)。"""
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
           f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
    b = _jhttp(url, timeout=8, headers={"Referer": "https://quote.eastmoney.com/"})
    if not b:
        return None
    try:
        data = (json.loads(b.decode("utf-8")).get("data") or {})
    except Exception:
        return None
    tc = data.get("tc")
    pool = data.get(f"{kind}Pool") or data.get("pool") or []
    detail = []
    for p in pool:
        pv = _jnum(p.get("p")) or 0
        detail.append({
            "code": _bare(p.get("c")), "name": _jstr(p.get("n"), 60),
            "price": round(pv / 1000.0 if pv > 1000 else pv, 2),
            "chg_pct": round(_jnum(p.get("zdp")) or 0, 2),
            "lianban": int(_jnum(p.get("lbc")) or 1),
            "zt_time": _jstr(p.get("fbt"), 16),
            "industry": _jstr(p.get("hybk"), 40),
        })
    return {"count": int(tc) if tc is not None else len(pool),
            "detail": detail, "qdate": str(data.get("qdate") or "")}


def f_eastmoney_zt_pool(ctx):
    """涨停/跌停/炸板池 → 按 kind 主键 (ZT/DT/ZB 三条记录)。"""
    date_str = ctx.get("date_str") or datetime.now(CST).strftime("%Y%m%d")
    out, got = [], 0
    for kind in ("ZT", "DT", "ZB"):
        r = _em_pool(kind, date_str)
        if not r:
            continue
        got += 1
        out.append({
            "_key": kind, "_src": "eastmoney_zt_pool", "kind": kind,
            "count": r["count"], "detail_n": len(r["detail"]), "detail": r["detail"],
            "trade_date": date_str,
        })
    if not out:
        return [], {"reason": "push2ex 涨停池三端均无响应 (容器出口受限?)"}
    return out, {"source": "eastmoney_zt_pool", "n": len(out), "kinds_ok": got}


def f_eastmoney_zdfenbu(ctx):
    """涨跌分布 → 各涨幅档位家数。"""
    date_str = ctx.get("date_str") or datetime.now(CST).strftime("%Y%m%d")
    url = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&date={date_str}")
    b = _jhttp(url, timeout=8, headers={"Referer": "https://quote.eastmoney.com/"})
    if not b:
        return [], {"reason": "push2ex 涨跌分布无响应"}
    try:
        fenbu = ((json.loads(b.decode("utf-8")).get("data") or {}).get("fenbu")) or []
    except Exception:
        return [], {"reason": "涨跌分布 JSON 解析失败"}
    dist = {}
    for item in fenbu:
        for k, v in item.items():
            try:
                dist[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
    if not dist:
        return [], {"reason": "涨跌分布 fenbu 为空 (非交易时段?)"}
    up = sum(c for pct, c in dist.items() if pct > 0)
    down = sum(c for pct, c in dist.items() if pct < 0)
    return [{
        "_key": "zdfenbu", "_src": "eastmoney_zdfenbu",
        "up": up, "down": down, "flat": dist.get(0, 0),
        "total": up + down + dist.get(0, 0),
        # 涨停家数 = ≥+10% 档位合计 (主板10cm + 双创20cm + 北交所30cm)
        "zt_like": sum(c for pct, c in dist.items() if pct >= 10),
        "dt_like": sum(c for pct, c in dist.items() if pct <= -10),
        "dist": dist, "trade_date": date_str,
    }], {"source": "eastmoney_zdfenbu", "n": 1}


# ════════════════════════════════════════════════════════════════
# K 线多源 (补齐「上市至今」)
# ════════════════════════════════════════════════════════════════
def _kline_records(sid, code, bars, adj=""):
    """bars: [[date, o, c, h, l, v], ...] → records (主键 code|date)。"""
    out = []
    for r in bars:
        if len(r) < 6:
            continue
        out.append({
            "_key": f"{_keycode(code)}|{r[0]}", "_src": sid,
            "code": _keycode(code), "date": r[0],
            "open": _jnum(r[1]), "close": _jnum(r[2]), "high": _jnum(r[3]),
            "low": _jnum(r[4]), "volume": _jnum(r[5]), "adj": adj,
        })
    return out


def _tk_fqkline(code, n, adj):
    """腾讯 fqkline (qfq/hfq)。实测: 连续 ~1750 请求后返回 501, 需错峰。"""
    u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
         f"?param={_tk(code)},day,,,{n},{adj}")
    b = _jhttp(u, timeout=8, headers={"Referer": "https://gu.qq.com/"})
    if not b:
        return []
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return []
    node = (d.get("data") or {}).get(_tk(code)) or {}
    arr = node.get(f"{adj}day") or node.get("day") or []
    return [r for r in arr if len(r) >= 6 and re.match(r"^\d{4}-\d{2}-\d{2}$", str(r[0]))]


def _tk_index_history(code, n):
    """腾讯指数日K全史 (无复权, 分页往前翻)。"""
    tk = _tk(code)
    allr, end, guard = {}, "", 0
    while guard < 12:
        guard += 1
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
             f"?param={tk},day,,{end},{n}")
        b = _jhttp(u, timeout=8, headers={"Referer": "https://gu.qq.com/"})
        if not b:
            break
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            break
        arr = ((d.get("data") or {}).get(tk) or {}).get("day") or []
        if not arr:
            break
        new = 0
        for r in arr:
            if len(r) >= 6 and r[0] not in allr:
                allr[r[0]] = r
                new += 1
        oldest = arr[0][0]
        if new == 0 or oldest <= "1991-01-01" or len(arr) < n:
            break
        end = oldest
        time.sleep(0.15)
    return [allr[k] for k in sorted(allr)]


def _em_kline(code, n):
    """东财 push2his 日K (fqt=1 前复权)。push2his 域不在 push2 WAF 范围。"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
         f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
         "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
         f"&end=20500101&lmt={n}")
    b = _jhttp(u, timeout=8, headers={"Referer": "https://quote.eastmoney.com/"})
    if not b:
        return []
    try:
        kl = ((json.loads(b.decode("utf-8")).get("data") or {}).get("klines")) or []
    except Exception:
        return []
    return [s.split(",") for s in kl
            if len(s.split(",")) >= 6 and re.match(r"^\d{4}-\d{2}-\d{2}$", s.split(",")[0])]


def _sina_kline(code, scale=240, datalen=1023, ma=5, vip=False):
    """新浪 K 线 (接口直返 MA5/MA10, 省本地计算; 支持指数)。"""
    host = ("vip.stock.finance.sina.com.cn" if vip else "money.finance.sina.com.cn")
    u = (f"https://{host}/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
         f"?symbol={_tk(code)}&scale={scale}&ma={ma}&datalen={datalen}")
    b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.sina.com.cn/"})
    if not b:
        return []
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return []


def _default_codes(ctx, limit=8, skip_index=False):
    """未显式给 codes 时: 本地快照里取市值最大的前 N 只。

    skip_index=True 时剔除指数代码 (sh000001/ sz399001/ sh000300/ sz399006 ...):
    大单分档/分笔明细这类接口只支持个股, 传指数会返回空
    (2026-10-14 实测 sina_bill_sum / tx_tick_detail 踩坑)。
    """
    codes = ctx.get("codes")
    if codes:
        picked = list(codes)[: int(ctx.get("kline_max_codes", limit) or limit)]
    else:
        picked = []
        # 优先本地快照 (SNAPSHOT_GLOB), 没有再从 all_sources.json 的 quote 组取
        # (容器内没有 snapshot, 只有 CI 采集产出 all_sources.json)
        try:
            from paths import SNAPSHOT_GLOB, AI_VIEW_DATA
            snaps = sorted(glob.glob(SNAPSHOT_GLOB))
            if snaps:
                d = json.load(open(snaps[-1], encoding="utf-8"))
                picked = [k for k, _ in sorted(d.items(),
                                               key=lambda kv: -(kv[1].get("mktcap") or 0))]
        except Exception:
            pass
        if not picked:
            # all_sources.quote 常只有指数, industry_map_tv 未必落盘;
            # 最稳的是从历史预测快照取 (那里每天有 top 股票代码)
            try:
                pdir = os.path.join(AI_VIEW_DATA, "predictions")
                if os.path.isdir(pdir):
                    import glob as _g
                    snaps = sorted(_g.glob(os.path.join(pdir, "engine_v5_*.json")))
                    if snaps:
                        d = json.load(open(snaps[-1], encoding="utf-8"))
                        picked = [str(r.get("code")) for r in (d.get("top") or [])
                                  if r.get("code")]
            except Exception:
                pass
    if skip_index:
        # 指数: 沪 000/950/899 开头, 深 399 开头
        # 必须在截断之前过滤, 否则 snapshot 里指数 mktcap 最大,
        # 先截断会把这几个全留下, 再过滤就空了 (2026-10-14 实测踩坑)
        picked = [c for c in picked
                  if _bare(c) and not _bare(c).startswith(("000", "399", "950", "899"))]
    picked = picked[: int(ctx.get("kline_max_codes", limit) or limit)]
    return picked


def f_tencent_kline(ctx):
    """腾讯前复权日K → 主源失败自动降级 eastmoney_kline (degrade_to)。"""
    codes = _default_codes(ctx)
    out, ok, errs = [], 0, []
    for code in codes:
        bars = _tk_fqkline(code, int(ctx.get("kline_bars", 320) or 320), "qfq")
        src = "tencent_kline"
        if not bars:
            bars = _em_kline(code, int(ctx.get("kline_bars", 320) or 320))
            src = "eastmoney_kline"
            if not bars:
                errs.append(f"{code}: 腾讯+东财均空")
                continue
        ok += 1
        out.extend(_kline_records(src, code, bars, adj="qfq"))
        time.sleep(0.12)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "tencent_kline", "n": len(out), "codes_ok": ok,
                 "codes_total": len(codes)}


def f_eastmoney_kline(ctx):
    """东财 push2his 前复权日K → 失败降级 tencent (互为备用)。"""
    codes = _default_codes(ctx)
    out, ok, errs = [], 0, []
    for code in codes:
        bars = _em_kline(code, int(ctx.get("kline_bars", 320) or 320))
        src = "eastmoney_kline"
        if not bars:
            bars = _tk_fqkline(code, int(ctx.get("kline_bars", 320) or 320), "qfq")
            src = "tencent_kline"
            if not bars:
                errs.append(f"{code}: 东财+腾讯均空")
                continue
        ok += 1
        out.extend(_kline_records(src, code, bars, adj="qfq"))
        time.sleep(0.12)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "eastmoney_kline", "n": len(out), "codes_ok": ok}


def f_tencent_index_history(ctx):
    """指数日K全史 (上证1990起, 每页800根往前翻)。"""
    codes = ctx.get("index_codes") or ["sh000001", "sz399001", "sh000300", "sz399006"]
    out, ok, errs = [], 0, []
    for code in codes[: int(ctx.get("kline_max_codes", 6) or 6)]:
        bars = _tk_index_history(code, 800) or []
        if not bars:
            rows = _sina_kline(code, scale=240, datalen=1023, ma=5, vip=True) or []
            bars = [[r.get("day"), r.get("open"), r.get("close"), r.get("high"),
                     r.get("low"), r.get("volume")] for r in rows if r.get("day")]
            if not bars:
                errs.append(f"{code}: 腾讯指数史+新浪均空")
                continue
        ok += 1
        out.extend(_kline_records("tencent_index_history", code, bars))
        time.sleep(0.15)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无指数可采"}
    return out, {"source": "tencent_index_history", "n": len(out), "codes_ok": ok}


def f_sina_kline_ma(ctx):
    """新浪日K + 接口直返 MA5/MA10 → 按 code|date 主键。"""
    codes = _default_codes(ctx, limit=4)
    out, ok, errs = [], 0, []
    for code in codes:
        rows = _sina_kline(code, scale=240, datalen=1023, ma=5)
        if not rows:
            errs.append(f"{code}: 新浪 K 线空")
            continue
        ok += 1
        for r in rows:
            d = _jstr(r.get("day"), 10)
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                continue
            out.append({
                "_key": f"{_keycode(code)}|{d}", "_src": "sina_kline_ma",
                "code": _keycode(code), "date": d,
                "open": _jnum(r.get("open")), "close": _jnum(r.get("close")),
                "high": _jnum(r.get("high")), "low": _jnum(r.get("low")),
                "volume": _jnum(r.get("volume")),
                "ma_price5": _jnum(r.get("ma_price5")),
                "ma_volume5": _jnum(r.get("ma_volume5")),
                "ma_price10": _jnum(r.get("ma_price10")),
                "ma_volume10": _jnum(r.get("ma_volume10")),
            })
        time.sleep(0.12)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "sina_kline_ma", "n": len(out), "codes_ok": ok}


def f_sina_vip_kline(ctx):
    """新浪 vip 域 K 线 (与 money 域互为备用, 1023 根上限)。"""
    codes = _default_codes(ctx, limit=4)
    out, ok, errs = [], 0, []
    for code in codes:
        rows = _sina_kline(code, scale=240, datalen=1023, ma=5, vip=True)
        if not rows:
            rows = _sina_kline(code, scale=240, datalen=1023, ma=5)
            if not rows:
                errs.append(f"{code}: vip+money 域均空")
                continue
        ok += 1
        for r in rows:
            d = _jstr(r.get("day"), 10)
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                continue
            out.append({
                "_key": f"{_keycode(code)}|{d}", "_src": "sina_vip_kline",
                "code": _keycode(code), "date": d,
                "open": _jnum(r.get("open")), "close": _jnum(r.get("close")),
                "high": _jnum(r.get("high")), "low": _jnum(r.get("low")),
                "volume": _jnum(r.get("volume")),
                "ma_price5": _jnum(r.get("ma_price5")),
                "ma_volume5": _jnum(r.get("ma_volume5")),
            })
        time.sleep(0.12)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "sina_vip_kline", "n": len(out), "codes_ok": ok}


def f_sina_kline_daily(ctx):
    """akshare.stock_zh_a_daily 封装 (新浪日K全史) — akshare 未装则空。"""
    codes = _default_codes(ctx, limit=2)
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for code in codes[:2]:
        try:
            df = ak.stock_zh_a_daily(symbol=_tk(code), adjust="qfq")
        except Exception as e:
            continue
        for _, row in df.tail(60).iterrows():
            d = str(row.get("date"))[:10]
            out.append({
                "_key": f"{_keycode(code)}|{d}", "_src": "sina_kline_daily",
                "code": _keycode(code), "date": d,
                "open": _jnum(row.get("open")), "close": _jnum(row.get("close")),
                "high": _jnum(row.get("high")), "low": _jnum(row.get("low")),
                "volume": _jnum(row.get("volume")),
                "amount": _jnum(row.get("amount")),
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "akshare 新浪日K 无数据 (容器出口受限?)"}
    return out, {"source": "sina_kline_daily", "n": len(out)}


def f_sina_etf_hist(ctx):
    """akshare.fund_etf_hist_sina (ETF 全史)。"""
    codes = [c for c in _default_codes(ctx, limit=2)
             if _bare(c).startswith(("51", "56", "58", "159", "511", "513", "515"))]
    if not codes:
        codes = ["sh510300"]
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for code in codes[:2]:
        try:
            df = ak.fund_etf_hist_sina(symbol=_tk(code))
        except Exception:
            continue
        for _, row in df.tail(60).iterrows():
            d = str(row.get("date"))[:10]
            out.append({
                "_key": f"{_keycode(code)}|{d}", "_src": "sina_etf_hist",
                "code": _keycode(code), "date": d,
                "open": _jnum(row.get("open")), "close": _jnum(row.get("close")),
                "high": _jnum(row.get("high")), "low": _jnum(row.get("low")),
                "volume": _jnum(row.get("volume")),
                "amount": _jnum(row.get("amount")),
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "akshare ETF 历史无数据"}
    return out, {"source": "sina_etf_hist", "n": len(out)}


def f_em_a_hist(ctx):
    """akshare.stock_zh_a_hist (东财日K封装, 底层 push2his 容器内被 RST)。"""
    codes = _default_codes(ctx, limit=2)
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for code in codes[:2]:
        try:
            df = ak.stock_zh_a_hist(symbol=_bare(code), period="daily", adjust="qfq")
        except Exception:
            continue
        for _, row in df.tail(60).iterrows():
            d = str(row.get("日期"))[:10]
            out.append({
                "_key": f"{_keycode(code)}|{d}", "_src": "em_a_hist",
                "code": _keycode(code), "date": d,
                "open": _jnum(row.get("开盘")), "close": _jnum(row.get("收盘")),
                "high": _jnum(row.get("最高")), "low": _jnum(row.get("最低")),
                "volume": _jnum(row.get("成交量")),
                "amount": _jnum(row.get("成交额")),
                "chg_pct": _jnum(row.get("涨跌幅")),
                "turnover": _jnum(row.get("换手率")),
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "akshare 东财日K 无数据 (push2his 容器 RST)"}
    return out, {"source": "em_a_hist", "n": len(out)}


# ════════════════════════════════════════════════════════════════
# 东财资金流 / 快照 / Tick (push2 三域轮询 + 熔断)
# ════════════════════════════════════════════════════════════════
_EM_CB = {"fails": 0, "tripped": False, "threshold": 6}


def _em_multi(path_tmpl, **kw):
    """同一接口在 push2 三域轮询; 连续失败达阈值后本周期熔断。

    容器 IP 层 TLS RST, 单域成功率 10-35%, 继续重试只会加剧封锁
    (策略照搬 collect_moneyflow.py)。
    """
    if _EM_CB["tripped"]:
        return None, "circuit_open (push2 连续失败, 本周期跳过)"
    last = "unknown"
    for host in EM_HOSTS:
        for attempt in range(3):
            u = f"https://{host}" + path_tmpl.format(**kw)
            b = _jhttp(u, timeout=6, retries=1,
                       headers={"Referer": "https://quote.eastmoney.com/"})
            if b:
                break
            last = f"{host} 无响应"
            time.sleep(0.3 * (attempt + 1))
        if b:
            try:
                d = json.loads(b.decode("utf-8"))
            except Exception as e:
                last = f"json: {type(e).__name__}"
                b = b""
                continue
            if d.get("data"):
                _EM_CB["fails"] = 0
                _EM_CB["tripped"] = False
                return d, None
            last = "empty data"
            b = b""
        else:
            last = "no response"
    _EM_CB["fails"] += 1
    if _EM_CB["fails"] >= _EM_CB["threshold"]:
        _EM_CB["tripped"] = True
    return None, last


def f_em_fflow_history(ctx):
    """东财历史资金流 (日级) → 按 code|date 主键。"""
    codes = _default_codes(ctx, limit=6)
    out, ok, errs = [], 0, []
    for code in codes:
        d, err = _em_multi("/api/qt/stock/fflow/kline/get?secid={secid}"
                           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55"
                           "&lmt=20&klt=101", secid=_secid(code))
        if not d:
            errs.append(f"{code}: {err}")
            continue
        ok += 1
        for s in ((d.get("data") or {}).get("klines")) or []:
            p = s.split(",")
            if len(p) < 6:
                continue
            out.append({
                "_key": f"{_keycode(code)}|{p[0]}", "_src": "em_fflow_history",
                "code": _keycode(code), "date": p[0],
                "main_net": _jnum(p[1]), "small_net": _jnum(p[2]),
                "medium_net": _jnum(p[3]), "large_net": _jnum(p[4]),
                "xlarge_net": _jnum(p[5]),
            })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "push2 三域均不可用"}
    return out, {"source": "em_fflow_history", "n": len(out), "codes_ok": ok}


def f_em_quote_snapshot(ctx):
    """东财个股快照 (价/量/额/市值/PE/PB/换手/量比/内外盘/均价/振幅)。"""
    codes = _default_codes(ctx, limit=10)
    out, ok, errs = [], 0, []
    for code in codes:
        d, err = _em_multi("/api/qt/stock/get?secid={secid}&fltt=2&invt=2"
                           "&fields=f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f60,"
                           "f71,f116,f117,f162,f167,f168,f169,f170,f171,f177",
                           secid=_secid(code))
        if not d:
            errs.append(f"{code}: {err}")
            continue
        r = d.get("data") or {}
        ok += 1
        out.append({
            "_key": _keycode(code), "_src": "em_quote_snapshot",
            "code": _keycode(code), "name": _jstr(r.get("f58"), 60),
            "price": _jnum(r.get("f43")), "high": _jnum(r.get("f44")),
            "low": _jnum(r.get("f45")), "open": _jnum(r.get("f46")),
            "vol": _jnum(r.get("f47")), "amt": _jnum(r.get("f48")),
            "outer": _jnum(r.get("f49")), "inner": _jnum(r.get("f50")),
            "limit_up": _jnum(r.get("f51")), "limit_down": _jnum(r.get("f52")),
            "prev_close": _jnum(r.get("f60")), "avg_price": _jnum(r.get("f71")),
            "mktcap": _jnum(r.get("f116")), "floatcap": _jnum(r.get("f117")),
            "pe_ttm": _jnum(r.get("f162")), "pb": _jnum(r.get("f167")),
            "turnover": _jnum(r.get("f168")), "chg": _jnum(r.get("f169")),
            "chg_pct": _jnum(r.get("f170")), "amplitude": _jnum(r.get("f171")),
            "vol_ratio": (_jnum(r.get("f177")) or 0) / 100.0,
        })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "push2 三域均不可用"}
    return out, {"source": "em_quote_snapshot", "n": len(out), "codes_ok": ok}


def f_em_tick_details(ctx):
    """东财分笔成交 (3秒聚合快照, 非逐笔委托) → 按 code|time|price 主键。"""
    codes = _default_codes(ctx, limit=2)
    n = int(ctx.get("tick_n", 50) or 50)
    out, ok, errs = [], 0, []
    for code in codes:
        d, err = _em_multi("/api/qt/stock/details/get?secid={secid}"
                           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55"
                           "&pos=-{n}&fltt=2", secid=_secid(code), n=n)
        if not d:
            errs.append(f"{code}: {err}")
            continue
        ok += 1
        for it in ((d.get("data") or {}).get("details")) or []:
            p = str(it).split(",")
            if len(p) < 5:
                continue
            out.append({
                "_key": f"{_keycode(code)}|{p[0]}|{p[1]}", "_src": "em_tick_details",
                "code": _keycode(code), "time": _jstr(p[0], 12),
                "price": _jnum(p[1]), "deals": _jnum(p[2]),
                "vol": _jnum(p[3]), "direction": _jstr(p[4], 4),
            })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "push2 三域均不可用"}
    return out, {"source": "em_tick_details", "n": len(out), "codes_ok": ok}


def f_tx_tick_detail(ctx):
    """腾讯分笔 (比东财多成交额字段) → 按 code|time 主键。

    只支持个股: 默认代码要剔除指数, 否则接口返回空。
    """
    codes = _default_codes(ctx, limit=2, skip_index=True)
    out, ok, errs = [], 0, []
    for code in codes:
        tk = _tk(code)
        u = (f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data"
             f"&c={tk}&p=0")
        b = _jhttp(u, timeout=8, headers={"Referer": "https://gu.qq.com/"})
        if not b:
            errs.append(f"{code}: 无响应")
            continue
        # 格式: v_detail_data_sh600519=[0,"0/09:25:02/1270.10/-0.10/83/10541830/S|1/..."]
        # 数据段在第二组引号里 (第一组是页码 0), 用非贪婪匹配取所有引号段
        segs = re.findall(r'"([^"]*)"', b.decode("utf-8", "replace"))
        body = segs[1] if len(segs) > 1 else (segs[0] if segs else "")
        if not body:
            errs.append(f"{code}: 无数据段")
            continue
        ok += 1
        # 每笔: 序号/HH:MM:SS/价/价格变动/成交量(手)/成交额(元)/性质(B/S/M)
        for seg in body.split("|"):
            p = seg.split("/")
            if len(p) < 6:
                continue
            out.append({
                "_key": f"{_keycode(code)}|{p[1]}", "_src": "tx_tick_detail",
                "code": _keycode(code), "time": _jstr(p[1], 12),
                "price": _jnum(p[2]), "chg": _jnum(p[3]),
                "vol": _jnum(p[4]), "amt": _jnum(p[5]),
                "direction": _jstr(p[6] if len(p) > 6 else "", 4),
            })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "tx_tick_detail", "n": len(out), "codes_ok": ok}


def f_tx_minute(ctx):
    """腾讯分钟线 (累计值, 需 diff 得每分钟量) → 按 code|hhmm 主键。"""
    codes = _default_codes(ctx, limit=2)
    out, ok, errs = [], 0, []
    for code in codes:
        tk = _tk(code)
        u = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tk}"
        b = _jhttp(u, timeout=8, headers={"Referer": "https://gu.qq.com/"})
        if not b:
            errs.append(f"{code}: 无响应")
            continue
        try:
            node = ((json.loads(b.decode("utf-8")).get("data") or {})
                    .get(tk) or {}).get("data") or {}
        except Exception:
            errs.append(f"{code}: JSON 解析失败")
            continue
        arr = node.get("data") or []
        if not arr:
            errs.append(f"{code}: 分钟数据空")
            continue
        ok += 1
        prev_v = prev_a = 0.0
        for line in arr:
            p = str(line).split()
            if len(p) < 4:
                continue
            v, a = _jnum(p[2]) or 0, _jnum(p[3]) or 0
            out.append({
                "_key": f"{_keycode(code)}|{p[0]}", "_src": "tx_minute",
                "code": _keycode(code), "hhmm": _jstr(p[0], 6),
                "price": _jnum(p[1]),
                "cum_vol": v, "cum_amt": a,
                "vol": round(max(v - prev_v, 0.0), 2),
                "amt": round(max(a - prev_a, 0.0), 2),
                "trade_date": _jstr(node.get("date"), 10),
            })
            prev_v, prev_a = v, a
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "tx_minute", "n": len(out), "codes_ok": ok}


def f_sina_bill_sum(ctx):
    """新浪大单分档汇总 (茅台28档) → 按 code 主键 (一票一档位数组)。

    只支持个股: 默认代码要剔除指数, 否则接口返回空。
    """
    codes = _default_codes(ctx, limit=2, skip_index=True)
    out, ok, errs = [], 0, []
    for code in codes:
        tk = _tk(code)
        u = (f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"CN_Bill.GetBillSum?symbol={tk}&volume=0")
        b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.sina.com.cn/"})
        if not b:
            errs.append(f"{code}: 无响应")
            continue
        try:
            arr = json.loads(b.decode("utf-8"))
        except Exception:
            errs.append(f"{code}: JSON 解析失败")
            continue
        if not arr:
            errs.append(f"{code}: 空")
            continue
        ok += 1
        tiers = [{"minvol": _jstr(x.get("minvol"), 12),
                  "voltype": _jstr(x.get("voltype"), 4),
                  "totalvol": _jnum(x.get("totalvol")),
                  "totalvolpct": _jnum(x.get("totalvolpct")),
                  "totalamt": _jnum(x.get("totalamt")),
                  "avgprice": _jnum(x.get("avgprice")),
                  "kuvolume": _jnum(x.get("kuvolume"))} for x in arr]
        out.append({
            "_key": _keycode(code), "_src": "sina_bill_sum",
            "code": _keycode(code), "name": _jstr(arr[0].get("name"), 60),
            "opendate": _jstr(arr[0].get("opendate"), 10),
            "tiers": tiers, "tier_n": len(tiers),
        })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "sina_bill_sum", "n": len(out), "codes_ok": ok}


def f_sina_trans_list(ctx):
    """新浪成交明细 (JS Array 需正则解析) → 按 code|time 主键。"""
    codes = _default_codes(ctx, limit=2)
    n = int(ctx.get("tick_n", 20) or 20)
    out, ok, errs = [], 0, []
    for code in codes:
        tk = _tk(code)
        u = (f"http://vip.stock.finance.sina.com.cn/quotes_service/view/"
             f"CN_TransListV2.php?num={n}&symbol={tk}")
        b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.sina.com.cn/"})
        if not b:
            errs.append(f"{code}: 无响应")
            continue
        txt = b.decode("utf-8", "replace")
        ok += 1
        # 响应是 JS: trade_item_list[0] = new Array('15:00:00', '72200', '1286.000', 'UP')
        # 必须匹配整个 Array(...) 再按 ', ' 拆 —— 只抓第一组引号会丢字段
        for m in re.finditer(r"new Array\(([^)]*)\)", txt):
            p = [x.strip().strip("'") for x in m.group(1).split(",")]
            if len(p) < 4:
                continue
            out.append({
                "_key": f"{_keycode(code)}|{p[0]}", "_src": "sina_trans_list",
                "code": _keycode(code), "time": _jstr(p[0], 12),
                "vol": _jnum(p[1]), "price": _jnum(p[2]),
                "direction": _jstr(p[3], 8),
            })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "无代码可采"}
    return out, {"source": "sina_trans_list", "n": len(out), "codes_ok": ok}


# ════════════════════════════════════════════════════════════════
# 东财 datacenter 报表 (业绩快报 / 基金净值 / 历史资金流)
# ════════════════════════════════════════════════════════════════
def _dc_get(report, page_size=50, page=1, sort="UPDATE_DATE,SECURITY_CODE", filt=None):
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         f"?reportName={report}&columns=ALL&pageSize={page_size}&pageNumber={page}"
         f"&sortColumns={sort}&sortTypes=-1,-1&source=WEB&client=WEB")
    if filt:
        u += f"&filter={filt}"
    b = _jhttp(u, timeout=8, headers={"Referer": "https://data.eastmoney.com/"})
    if not b:
        return []
    try:
        return ((json.loads(b.decode("utf-8")).get("result") or {}).get("data")) or []
    except Exception:
        return []


def f_em_lcpd_report(ctx):
    """东财业绩快报 (全市场 5700+ 家, 每股收益/ROE/营收同比...)。"""
    pages = int(ctx.get("lcpd_pages", 2) or 2)
    report_date = ctx.get("report_date") or ""
    filt = f"(REPORTDATE%3D%27{report_date}%27)" if report_date else None
    rows = []
    for p in range(1, pages + 1):
        batch = _dc_get("RPT_LICO_FN_CPD", 50, p, filt=filt)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 50:
            break
        time.sleep(0.4)
    if not rows:
        return [], {"reason": "RPT_LICO_FN_CPD 无数据 (报告期未选/接口变更)"}
    out = []
    for r in rows[:400]:
        code = _bare(r.get("SECURITY_CODE"))
        if not code:
            continue
        out.append({
            "_key": code, "_src": "em_lcpd_report",
            "code": code, "name": _jstr(r.get("SECURITY_NAME_ABBR"), 60),
            "report_date": _jstr(r.get("REPORTDATE"), 10),
            "eps": _jnum(r.get("EPSJB")),
            "eps_deduct": _jnum(r.get("EPSKCJB")),
            "revenue": _jnum(r.get("TOTALOPERATEREVE")),
            "net_profit": _jnum(r.get("PARENTNETPROFIT")),
            "net_profit_deduct": _jnum(r.get("KCFJCXSYJLR")),
            "roe_weighted": _jnum(r.get("ROEJQ")),
            "revenue_yoy": _jnum(r.get("TOTALOPERATEREVETZ")),
            "profit_yoy": _jnum(r.get("PARENTNETPROFITTZ")),
            "bps": _jnum(r.get("BPS")),
            "gross_margin": _jnum(r.get("XSMLL")),
            "industry": _jstr(r.get("INDUSTRY_NAME") or r.get("EM_INDUSTRY_NAME"), 40),
        })
    return out, {"source": "em_lcpd_report", "n": len(out)}


def f_em_fund_lsjz(ctx):
    """东财基金历史净值 (静默鉴权: 不带 Referer 返回 ErrCode=-999)。"""
    codes = ctx.get("fund_codes") or ["510300", "510500", "159915", "511010"]
    out = []
    for fc in codes[: int(ctx.get("fund_max", 4) or 4)]:
        u = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={fc}"
             f"&pageIndex=1&pageSize=30")
        b = _jhttp(u, timeout=8, headers={
            "Referer": f"https://fundf10.eastmoney.com/jjjz_{fc}.html"})
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            continue
        if d.get("ErrCode") not in (0, None):
            continue
        for r in (d.get("Data") or {}).get("LSJZList") or []:
            d10 = _jstr(r.get("FSRQ"), 10)
            out.append({
                "_key": f"{fc}|{d10}", "_src": "em_fund_lsjz",
                "code": fc, "date": d10,
                "nav": _jnum(r.get("DWJZ")), "acc_nav": _jnum(r.get("LJJZ")),
                "daily_chg_pct": _jnum(r.get("JZZZL")),
                "sub_status": _jstr(r.get("SGZT"), 8),
                "redeem_status": _jstr(r.get("SHZT"), 8),
            })
        time.sleep(0.15)
    if not out:
        return [], {"reason": "基金净值接口无数据 (Referer 静默鉴权失败?)"}
    return out, {"source": "em_fund_lsjz", "n": len(out)}


def f_em_fund_etf_hist_min(ctx):
    """东财 ETF 历史净值 (akshare.fund_etf_fund_info_em, 全史)。"""
    codes = [c for c in (ctx.get("fund_codes") or ["511280", "510300"])
             if _bare(c).startswith(("51", "56", "58", "159"))][:2]
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for fc in codes:
        try:
            df = ak.fund_etf_fund_info_em(fund=_bare(fc), start_date="20000101")
        except Exception:
            continue
        for _, row in df.tail(30).iterrows():
            d = str(row.get("净值日期"))[:10]
            out.append({
                "_key": f"{_keycode(fc)}|{d}", "_src": "em_fund_etf_hist_min",
                "code": _bare(fc), "date": d,
                "nav": _jnum(row.get("单位净值")),
                "acc_nav": _jnum(row.get("累计净值")),
                "daily_chg_pct": _jnum(row.get("日增长率")),
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "ETF 历史净值无数据"}
    return out, {"source": "em_fund_etf_hist_min", "n": len(out)}


def f_ths_financial_abstract_v2(ctx):
    """同花顺财务摘要 v2 (长表, 直接可用) → 按 code|report_date|metric 主键。"""
    codes = _default_codes(ctx, limit=1)
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for code in codes[:1]:
        try:
            df = ak.stock_financial_abstract_new_ths(symbol=_bare(code))
        except Exception:
            return [], {"reason": "同花顺财务摘要接口失败"}
        for _, row in df.head(60).iterrows():
            metric = _jstr(row.get("metric_name"), 60)
            out.append({
                "_key": f"{_keycode(code)}|{_jstr(row.get('report_date'), 10)}|{metric}",
                "_src": "ths_financial_abstract_v2",
                "code": _bare(code),
                "report_date": _jstr(row.get("report_date"), 10),
                "report_name": _jstr(row.get("report_name"), 24),
                "metric_name": metric,
                "value": _jstr(row.get("value"), 24),
                "yoy": _jstr(row.get("yoy"), 24),
                "mom": _jstr(row.get("mom"), 24),
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "同花顺财务摘要无数据"}
    return out, {"source": "ths_financial_abstract_v2", "n": len(out)}


def f_ths_financial_abstract(ctx):
    """同花顺财务摘要 v1 (宽表, 字段值含「亿」需 parse)。"""
    codes = _default_codes(ctx, limit=1)
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for code in codes[:1]:
        try:
            df = ak.stock_financial_abstract_ths(symbol=_bare(code))
        except Exception:
            return [], {"reason": "同花顺财务摘要 v1 接口失败"}
        for _, row in df.head(40).iterrows():
            rd = _jstr(row.get("报告期"), 24)
            if not rd:
                continue
            rec = {"_key": f"{_keycode(code)}|{rd}", "_src": "ths_financial_abstract",
                   "code": _keycode(code), "report_date": rd}
            for col in df.columns:
                if col == "报告期":
                    continue
                v = row.get(col)
                if v in (None, False, ""):
                    continue
                if isinstance(v, str):
                    m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
                    rec[_jstr(col, 40)] = _jnum(m.group(0)) if m else _jstr(v, 40)
                else:
                    rec[_jstr(col, 40)] = _jnum(v)
            out.append(rec)
        time.sleep(0.2)
    if not out:
        return [], {"reason": "同花顺财务摘要 v1 无数据"}
    return out, {"source": "ths_financial_abstract", "n": len(out)}


# ════════════════════════════════════════════════════════════════
# 同花顺强势股 / 行业 / 概念 (板块标签作事件驱动)
# ════════════════════════════════════════════════════════════════
def f_ths_strong_stocks(ctx):
    """同花顺强势股 + 题材归因 (reason 字段本身就是题材标签)。"""
    date_dash = ctx.get("date_dash") or datetime.now(CST).strftime("%Y-%m-%d")
    u = (f"http://zx.10jqka.com.cn/event/api/getharden/date/{date_dash}"
         f"/orderby/date/orderway/desc/charset/GBK/")
    b = _jhttp(u, timeout=10, headers={"Referer": "http://www.10jqka.com.cn/"})
    if not b:
        return [], {"reason": "同花顺强势股无响应"}
    try:
        data = json.loads(b.decode("gbk", "replace")).get("data") or []
    except Exception:
        return [], {"reason": "同花顺强势股解析失败 (GBK)"}
    out = []
    for r in data:
        code = _bare(r.get("code"))
        if not code:
            continue
        reason = _jstr(r.get("reason"), 200)
        out.append({
            "_key": code, "_src": "ths_strong_stocks",
            "code": code, "name": _jstr(r.get("name"), 60), "reason": reason,
            "themes": [kw.strip() for kw in reason.replace("+", " ").split()
                       if len(kw.strip()) >= 2],
            "trade_date": date_dash,
        })
    if not out:
        return [], {"reason": "同花顺强势股为空 (非交易日?)"}
    return out, {"source": "ths_strong_stocks", "n": len(out)}


def _ak_board_list(fn_name):
    try:
        import akshare as ak
    except ImportError:
        return None
    try:
        return getattr(ak, fn_name)()
    except Exception:
        return None


def f_ths_industry_list(ctx):
    """同花顺行业板块名录 (90 个, akshare 底层 d.10jqka 域零 WAF)。"""
    df = _ak_board_list("stock_board_industry_name_ths")
    if df is None or df.empty:
        return [], {"reason": "akshare 行业板块名录不可用"}
    out = [{"_key": _jstr(r.get("name")), "_src": "ths_industry_list",
            "name": _jstr(r.get("name")), "code": _jstr(r.get("code"), 12),
            "board_type": "industry"}
           for _, r in df.iterrows() if r.get("name")]
    return out, {"source": "ths_industry_list", "n": len(out)}


def f_ths_concept_list(ctx):
    """同花顺概念板块名录 (375 个, 概念代码可作事件驱动标签)。"""
    df = _ak_board_list("stock_board_concept_name_ths")
    if df is None or df.empty:
        return [], {"reason": "akshare 概念板块名录不可用"}
    out = [{"_key": _jstr(r.get("name")), "_src": "ths_concept_list",
            "name": _jstr(r.get("name")), "code": _jstr(r.get("code"), 12),
            "board_type": "concept"}
           for _, r in df.iterrows() if r.get("name")]
    return out, {"source": "ths_concept_list", "n": len(out)}


def f_ths_industry(ctx):
    """同花顺行业行情 (老 .js 端点返回 SPA 壳, 需登录态 → 优雅失败)。"""
    u = ("http://q.10jqka.com.cn/interface/stock/thshy/zdf/desc/1/quote/1.js")
    b = _jhttp(u, timeout=8, headers={"Referer": "http://q.10jqka.com.cn/"})
    if not b or b"<html" in b[:200].lower():
        return [], {"reason": "thshy .js 返回 SPA 壳/无响应, 需登录态"}
    return [], {"reason": "thshy 返回非 JSON (上游已标注需登录态)"}


# ════════════════════════════════════════════════════════════════
# 新浪实时行情 (腾讯降级源) / 分钟线
# ════════════════════════════════════════════════════════════════
def f_sina_hq(ctx):
    """新浪实时行情 (必带 Referer, 否则 403) → 按 code 主键。

    响应是 GBK 的 `var hq_str_sh000001="名称,开,昨,现,高,低,..."`, 字段顺序
    与腾讯不同; 必须先 GBK 解码再 split(',') (用 _jstr 会按 utf-8 replace
    把中文名打碎)。
    """
    codes = ctx.get("codes") or ["sh000001", "sz399001", "sh600519", "sz000001"]
    out = []
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        u = "https://hq.sinajs.cn/list=" + ",".join(_tk(c) for c in chunk)
        b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.sina.com.cn/"})
        if not b:
            continue
        txt = b.decode("gbk", "replace")
        for line in txt.splitlines():
            if "=" not in line:
                continue
            sym, _, payload = line.partition("=")
            p = payload.strip().strip('";\n ').split(",")
            if len(p) < 32:
                continue
            # 变量名形如 var hq_str_sh000001 (先剥 var/hq_str_, 别再动内容)
            sym_raw = re.sub(r"^(?:var\s+)?hq_str_", "", sym.strip()).strip().lower()
            if not re.match(r"^(sh|sz|bj)?\d{6}$", sym_raw):
                continue
            bare = _keycode(sym_raw)
            out.append({
                "_key": bare, "_src": "sina_hq", "code": bare,
                "name": _jstr(p[0], 60),
                "open": _jnum(p[1]), "prev_close": _jnum(p[2]),
                "price": _jnum(p[3]), "high": _jnum(p[4]), "low": _jnum(p[5]),
                "vol": _jnum(p[8]), "amt": _jnum(p[9]),
                "date": _jstr(p[30], 10), "time": _jstr(p[31], 10),
                "chg_pct": (round(((_jnum(p[3]) or 0) - (_jnum(p[2]) or 1))
                                 / (_jnum(p[2]) or 1) * 100, 2)
                            if _jnum(p[2]) else None),
            })
        time.sleep(0.15)
    if not out:
        return [], {"reason": "新浪行情无数据 (Referer 403?)"}
    return out, {"source": "sina_hq", "n": len(out)}


def f_minute1(ctx):
    """新浪 1 分钟 K (盘中增量, datalen=30) → 按 symbol|date|hhmm 主键。"""
    codes = _default_codes(ctx, limit=3)
    datalen = int(ctx.get("minute_datalen", 30) or 30)
    out, ok = [], 0
    for code in codes:
        bare = _bare(code)
        u = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
             f"CN_MarketDataService.getKLineData?symbol={bare}&scale=1&ma=no"
             f"&datalen={datalen}")
        b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.sina.com.cn/"})
        if not b:
            continue
        txt = b.decode("utf-8", "replace")
        s, e = txt.find("=("), txt.rfind(")")
        if s < 0 or e < 0:
            continue
        try:
            arr = json.loads(txt[s + 2:e])
        except Exception:
            continue
        if not arr:
            continue
        ok += 1
        for bar in arr:
            day = _jstr(bar.get("day"), 20)
            if len(day) < 16:
                continue
            out.append({
                "_key": f"{_keycode(code)}|{day[:10]}|{day[11:16]}", "_src": "minute1",
                "symbol": _keycode(code), "date": day[:10], "hhmm": day[11:16],
                "open": _jnum(bar.get("open")), "close": _jnum(bar.get("close")),
                "high": _jnum(bar.get("high")), "low": _jnum(bar.get("low")),
                "volume": _jnum(bar.get("volume")),
                "amount": _jnum(bar.get("amount")),
            })
        time.sleep(0.1)
    if not out:
        return [], {"reason": "分钟线无数据 (收盘后常为空)"}
    return out, {"source": "minute1", "n": len(out), "codes_ok": ok}


# ════════════════════════════════════════════════════════════════
# 本地 / 远端 数据源 (零网络)
# ════════════════════════════════════════════════════════════════
def f_local_kline_bulk(ctx):
    """本地全市场日K bulk (day_qfq_*.csv, code|date|o|c|h|l|v)。"""
    try:
        from paths import BULK
    except ImportError:
        return [], {"reason": "paths 不可用"}
    files = sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv")))
    if not files:
        return [], {"reason": f"bulk 目录无文件: {BULK}"}
    want = {_bare(c) for c in (ctx.get("codes") or [])}
    max_codes = int(ctx.get("bulk_max_codes", 200) or 200)
    per_code_max = int(ctx.get("bulk_bars_per_code", 120) or 120)
    seen_codes, out = {}, []
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or "|" not in line:
                        continue
                    # 两种行格式 (registry 已标注):
                    #   code|date|o|c|h|l|v        与   code|date,o,c,h,l,v
                    head, _, tail = line.strip().partition("|")
                    parts = [head] + tail.replace("|", ",").split(",")
                    if len(parts) < 7:
                        continue
                    code, d = parts[0].strip(), parts[1].strip()
                    if want and code not in want and _bare(code) not in want:
                        continue
                    n = seen_codes.get(code, 0)
                    if n >= per_code_max:
                        continue
                    if not want and len(seen_codes) >= max_codes and code not in seen_codes:
                        continue
                    seen_codes[code] = n + 1
                    out.append({
                        "_key": f"{_bare(code)}|{d}", "_src": "local_kline_bulk",
                        "code": _bare(code), "date": d,
                        "open": _jnum(parts[2]), "close": _jnum(parts[3]),
                        "high": _jnum(parts[4]), "low": _jnum(parts[5]),
                        "volume": _jnum(parts[6]), "adj": "qfq",
                    })
        except Exception:
            continue
        if not want and len(seen_codes) >= max_codes:
            break
    if not out:
        return [], {"reason": f"bulk 无可用行 (files={len(files)}, want={len(want)})"}
    return out, {"source": "local_kline_bulk", "n": len(out),
                 "codes": len(seen_codes), "files": len(files)}


def f_local_snapshot(ctx):
    """本地实时快照 (snapshot_YYYY-MM-DD.json, 5278只)。"""
    try:
        from paths import SNAPSHOT_GLOB
    except ImportError:
        return [], {"reason": "paths 不可用"}
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if not snaps:
        return [], {"reason": "本地快照不存在"}
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
    except Exception as e:
        return [], {"reason": f"快照读取失败 {type(e).__name__}"}
    limit = int(ctx.get("snapshot_max", 3000) or 3000)
    out = []
    for k, v in list(d.items())[:limit]:
        code = _bare(k)
        if not code:
            continue
        out.append({
            "_key": code, "_src": "local_snapshot", "code": code,
            "name": _jstr(v.get("name"), 60),
            "price": _jnum(v.get("price")),
            "prev_close": _jnum(v.get("prev_close")),
            "open": _jnum(v.get("open")), "high": _jnum(v.get("high")),
            "low": _jnum(v.get("low")),
            "chg_pct": _jnum(v.get("chg_pct")),
            "turnover": _jnum(v.get("turnover")),
            "pe": _jnum(v.get("pe")), "pb": _jnum(v.get("pb")),
            "mktcap": _jnum(v.get("mktcap")),
            "floatcap": _jnum(v.get("floatcap")),
            "vol_ratio": _jnum(v.get("vol_ratio")),
            "time": _jstr(v.get("time"), 16),
            "snapshot_file": os.path.basename(snaps[-1]),
        })
    if not out:
        return [], {"reason": "快照为空"}
    return out, {"source": "local_snapshot", "n": len(out),
                 "file": os.path.basename(snaps[-1]), "total": len(d)}


def f_remote_news(ctx):
    """远端 news 仓兜底 (raw.githubusercontent.com/.../news/{date}.md)。"""
    out, seen = [], set()
    for back in range(0, 5):
        day = (datetime.now(CST) - timedelta(days=back)).strftime("%Y-%m-%d")
        u = (f"https://raw.githubusercontent.com/qwerzxcva/stocks-data-news/"
             f"main/news/{day}.md")
        b = _jhttp(u, timeout=8, retries=1)
        if not b:
            continue
        for line in b.decode("utf-8", "ignore").splitlines():
            m = re.match(r"^[-*]\s*\[?(\d{1,2}:\d{2})\]?\s*(.+)$", line.strip())
            if not m:
                continue
            title = _jstr(m.group(2), 200)
            h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
            if h in seen:
                continue
            seen.add(h)
            out.append({
                "_key": h, "_src": "remote_news", "title": title,
                "pub_time": f"{day}T{m.group(1)}:00+08:00", "url": u,
            })
        if out:
            break
    if not out:
        return [], {"reason": "远端 news 仓近5日无内容 (404/未生成)"}
    return out, {"source": "remote_news", "n": len(out)}


def f_industry_map_tv(ctx):
    """TradingView 行业映射 (5309只 → sector/industry/type)。"""
    try:
        from paths import INDUSTRY_MAP
    except ImportError:
        return [], {"reason": "paths 不可用"}
    if not os.path.exists(INDUSTRY_MAP):
        return [], {"reason": f"行业映射不存在: {INDUSTRY_MAP}"}
    try:
        d = json.load(open(INDUSTRY_MAP, encoding="utf-8"))
    except Exception as e:
        return [], {"reason": f"行业映射读取失败 {type(e).__name__}"}
    limit = int(ctx.get("industry_map_max", 6000) or 6000)
    out = []
    for k, v in list(d.items())[:limit]:
        code = _bare(k)
        if not code or not isinstance(v, dict):
            continue
        out.append({
            "_key": code, "_src": "industry_map_tv", "code": code,
            "name_en": _jstr(v.get("name_en"), 120),
            "sector": _jstr(v.get("sector"), 60),
            "industry": _jstr(v.get("industry"), 60),
            "type": _jstr(v.get("type"), 16),
            "market": _jstr(v.get("market"), 12),
        })
    if not out:
        return [], {"reason": "行业映射为空"}
    return out, {"source": "industry_map_tv", "n": len(out), "total": len(d)}


# ════════════════════════════════════════════════════════════════
# 交易所官方 / 官方文件 (容器内常不通, 优雅失败即可)
# ════════════════════════════════════════════════════════════════
def f_sse_market(ctx):
    """上交所 commonQuery (sqlId 需有效会话; 实测 data=null)。"""
    u = ("http://query.sse.com.cn/commonQuery.do"
         "?sqlId=COMMON_SSE_HQ_QUERYTJ_ZSJY_L&pageHelp.pageSize=10&_="
         + str(int(time.time() * 1000)))
    b = _jhttp(u, timeout=8, headers={"Referer": "http://www.sse.com.cn/"})
    if not b:
        return [], {"reason": "上交所无响应 (容器出口受限)"}
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "上交所返回非 JSON"}
    rows = d.get("pageHelp") or d.get("result") or []
    # pageHelp 可能是 dict(含 data 键) 或 list; 统一成 list
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("results") or []
    if not rows or not isinstance(rows, list):
        return [], {"reason": "上交所 data=null (sqlId/会话无效, 上游已标注未验证)"}
    out = []
    for r in rows[:20]:
        if not isinstance(r, dict):
            continue
        key = _jstr(r.get("SECURITY_CODE") or r.get("id") or "", 20)
        if not key:
            continue
        out.append({"_key": key, "_src": "sse_market", "raw": r})
    return out, {"source": "sse_market", "n": len(out)}


def f_szse_api(ctx):
    """深交所 ShowReport (CATALOGID 需大陆出口, 容器内 500)。"""
    u = ("https://www.szse.cn/api/report/ShowReport?SHOWTYPE=JSON"
         "&CATALOGID=1835&txtDMorJC=&tab1PAGENO=1&pageSize=5&random=0.123")
    b = _jhttp(u, timeout=8, headers={"Referer": "https://www.szse.cn/"})
    if not b:
        return [], {"reason": "深交所无响应 (HTTP 500, 需大陆出口 IP)"}
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "深交所返回非 JSON (SPA 壳/500)"}
    rows = (d[0] if isinstance(d, list) and d else d).get("data") or []
    if not rows:
        return [], {"reason": "深交所 data 空 (CATALOGID 参数待验证)"}
    out = [{"_key": _jstr(r.get("zqdm") or r.get("dm") or i, 20), "_src": "szse_api",
            "code": _jstr(r.get("zqdm") or r.get("dm"), 12),
            "name": _jstr(r.get("zqjc") or r.get("jc"), 60), "raw": r}
           for i, r in enumerate(rows[:20])]
    return out, {"source": "szse_api", "n": len(out)}


def f_cninfo_announce(ctx):
    """巨潮公告 (容器内 500; POST + column/secuCode/seDate)。"""
    u = ("https://www.cninfo.com.cn/new/hisAnnouncement/query"
         "?column=szse&pageSize=5&pageNum=1")
    data = ("stock=&tabName=fulltext&pageSize=5&pageNum=1"
            "&seDate=&searchkey=&sortName=&sortType=&isHLtitle=true").encode()
    b = _jhttp(u, timeout=8, method="POST", data=data, headers={
        "Referer": "https://www.cninfo.com.cn/",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest"})
    if not b:
        return [], {"reason": "巨潮无响应 (容器内 500, 需大陆出口 IP)"}
    try:
        items = ((json.loads(b.decode("utf-8")).get("announcements")) or [])
    except Exception:
        return [], {"reason": "巨潮返回非 JSON (500/SPA 壳)"}
    if not items:
        return [], {"reason": "巨潮公告列表空"}
    out = []
    for it in items[:20]:
        title = _jstr(it.get("announcementTitle"), 200)
        if not title:
            continue
        h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
        out.append({
            "_key": h, "_src": "cninfo_announce", "title": title,
            "code": _bare(it.get("secCode")), "date": _jstr(it.get("announcementTime"), 20),
            "url": "https://static.cninfo.com.cn/" + _jstr(it.get("adjunctUrl"), 200),
        })
    return out, {"source": "cninfo_announce", "n": len(out)}


def f_em_ann_np(ctx):
    """东财公告 (np-ann, 需 XHR 头, 否则返回 SPA 壳)。"""
    codes = _default_codes(ctx, limit=3)
    out, ok, errs = [], 0, []
    for code in codes:
        bare = _bare(code)
        u = ("https://np-ann.eastmoney.com/api/security/ann"
             f"?sr=-1&page_size=5&page_idx=1&ann_type=A&client_source=web"
             f"&f_node=0&s_code={bare}")
        b = _jhttp(u, timeout=8, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://data.eastmoney.com/"})
        if not b:
            errs.append(f"{bare}: 无响应")
            continue
        try:
            items = ((json.loads(b.decode("utf-8")).get("data") or {}).get("list")) or []
        except Exception:
            errs.append(f"{bare}: 非 JSON (SPA 壳)")
            continue
        ok += 1
        for it in items:
            title = _jstr(it.get("title"), 200)
            if not title:
                continue
            h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
            art = it.get("art_code")
            out.append({
                "_key": h, "_src": "em_ann_np", "title": title,
                "code": bare, "name": _jstr(it.get("sec_name"), 60),
                "ann_date": _jstr(it.get("notice_date"), 10),
                "url": f"https://data.eastmoney.com/notices/detail/{bare}/{art}.html"
                       if art else "",
            })
        time.sleep(0.2)
    if not out:
        return [], {"reason": "; ".join(errs[:5]) or "东财公告无数据"}
    return out, {"source": "em_ann_np", "n": len(out), "codes_ok": ok}


def f_eastmoney_news(ctx):
    """东财 7x24 快讯。

    2026-10-15 实测: 端点是 /comm/web/getFastNewsList (不是 getNewsByColumns),
    biz=web_724 (不是 web7_24), 且必须带 Referer: finance.eastmoney.com
    才算通过风控。此前三个条件全错, 所以一直「列表空」。
    req_trace 用 uuid4().hex 即可 (服务端实际用自己的 trace, 参数值不校验)。
    """
    import uuid
    u = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
         "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=30"
         "&req_trace=%s" % uuid.uuid4().hex)
    b = _jhttp(u, timeout=8, headers={"Referer": "https://finance.eastmoney.com/"})
    if not b:
        return [], {"reason": "东财快讯无响应 (容器出口受限, GitHub Actions 通常通)"}
    try:
        d = json.loads(b.decode("utf-8")).get("data") or {}
        # 字段名是 fastNewsList (不是 list); code="1" 表示成功
        items = d.get("fastNewsList") or d.get("list") or []
    except Exception:
        return [], {"reason": "东财快讯非 JSON"}
    if not items:
        return [], {"reason": "东财快讯列表空"}
    out = []
    for it in items:
        title = _jstr(it.get("title"), 200)
        if not title:
            continue
        h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
        out.append({
            "_key": h, "_src": "eastmoney_news", "title": title,
            "digest": _jstr(it.get("digest"), 500),
            "pub_time": _jstr(it.get("showTime"), 20),
            "url": _jstr(it.get("url"), 300),
        })
    return out, {"source": "eastmoney_news", "n": len(out)}


# ════════════════════════════════════════════════════════════════
# 热搜 / 情绪 (零鉴权)
# ════════════════════════════════════════════════════════════════
def f_douyin_hot_search(ctx):
    """抖音热搜 (公开零鉴权, 需移动端 UA)。"""
    u = "https://aweme.snssdk.com/aweme/v1/hot/search/list/"
    b = _jhttp(u, timeout=8, headers={
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                       "Mobile/15E148"),
        "Referer": "https://www.douyin.com/"})
    if not b:
        return [], {"reason": "抖音热搜无响应"}
    try:
        data = json.loads(b.decode("utf-8", "replace")).get("data") or {}
    except Exception:
        return [], {"reason": "抖音热搜解析失败"}
    active = _jstr(data.get("active_time"), 24)
    out = []
    for it in (data.get("trending_list") or [])[: int(ctx.get("hot_limit", 30) or 30)]:
        word = _jstr(it.get("word"), 120)
        sid = _jstr(it.get("sentence_id"), 32)
        if not word or not sid:
            continue
        out.append({
            "_key": sid, "_src": "douyin_hot_search", "word": word,
            "sentence_id": sid,
            "video_count": _jnum(it.get("video_count")),
            "hot_value": _jnum(it.get("hot_value")),
            "active_time": active,
        })
    if not out:
        return [], {"reason": "抖音热搜 trending_list 空"}
    return out, {"source": "douyin_hot_search", "n": len(out)}


def f_toutiao_hot(ctx):
    """头条热榜 (ut/max_behave_time 可伪造, _signature 有时校验)。"""
    u = ("https://www.toutiao.com/api/hotboard/pc/hot-news/feed/"
         "?ut=0&max_behave_time=0&_signature=0")
    b = _jhttp(u, timeout=8, headers={"Referer": "https://www.toutiao.com/"})
    if not b:
        return [], {"reason": "头条热榜无响应 (404/412, _signature 校验)"}
    try:
        d = json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return [], {"reason": "头条热榜返回非 JSON"}
    items = d.get("data") or d.get("items") or []
    out = []
    for it in items[: int(ctx.get("hot_limit", 30) or 30)]:
        title = _jstr(it.get("Title") or it.get("title"), 200)
        if not title:
            continue
        h = hashlib.md5(re.sub(r"\W+", "", title[:40]).encode()).hexdigest()[:12]
        out.append({
            "_key": h, "_src": "toutiao_hot", "title": title,
            "hot_value": _jnum(it.get("HotValue") or it.get("hot_value")),
            "url": _jstr(it.get("Url") or it.get("url"), 300),
        })
    if not out:
        return [], {"reason": "头条热榜列表空"}
    return out, {"source": "toutiao_hot", "n": len(out)}


# ════════════════════════════════════════════════════════════════
# 海外 / 宏观 (零鉴权)
# ════════════════════════════════════════════════════════════════
def f_coingecko(ctx):
    """CoinGecko 简单报价 (BTC/ETH 等, 完全免费无 key)。"""
    ids = ctx.get("crypto_ids") or "bitcoin,ethereum"
    u = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    b = _jhttp(u, timeout=10, retries=2)
    if not b:
        return [], {"reason": "coingecko 无响应 (容器出口常超时)"}
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "coingecko 解析失败"}
    out = [{"_key": k, "_src": "coingecko", "coin": k,
            "usd": _jnum((v or {}).get("usd")),
            "ts": datetime.now(CST).isoformat(timespec="seconds")}
           for k, v in d.items() if isinstance(v, dict)]
    if not out:
        return [], {"reason": "coingecko 空响应"}
    return out, {"source": "coingecko", "n": len(out)}


def f_sec_edgar(ctx):
    """SEC EDGAR submissions (中概股财报; UA 必填, 否则 403)。"""
    ciks = ctx.get("edgar_ciks") or ["0000320193", "0000789019"]
    out = []
    for cik in ciks[:2]:
        u = f"https://data.sec.gov/submissions/CIK{cik}.json"
        b = _jhttp(u, timeout=10, headers={"User-Agent": "stocks-ai-view research"})
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8"))
        except Exception:
            continue
        rec = d.get("filings", {}).get("recent", {}) or {}
        forms = rec.get("form") or []
        dates = rec.get("filingDate") or []
        items = [{"form": _jstr(f, 12), "date": _jstr(dt, 10)}
                 for f, dt in list(zip(forms, dates))[:20]]
        out.append({
            "_key": cik, "_src": "sec_edgar", "cik": cik,
            "name": _jstr(d.get("name"), 120),
            "tickers": _jstr(",".join(d.get("tickers") or []), 40),
            "exchange": _jstr(d.get("exchanges"), 40),
            "sic": _jstr(d.get("sicDescription"), 60),
            "filings": items, "filing_n": len(items),
        })
        time.sleep(0.3)
    if not out:
        return [], {"reason": "EDGAR 无响应 (UA 403?)"}
    return out, {"source": "sec_edgar", "n": len(out)}


def f_macro_china_cpi_gdp(ctx):
    """统计局 CPI/GDP (akshare.macro_china_cpi / macro_china_gdp)。"""
    try:
        import akshare as ak
    except ImportError:
        return [], {"reason": "akshare 未安装"}
    out = []
    for label, fn, key_col, val_cols in (
            ("cpi", ak.macro_china_cpi, "月份",
             ("全国-当月", "全国-同比增长", "全国-环比增长")),
            ("gdp", ak.macro_china_gdp, "季度",
             ("国内生产总值-绝对值", "国内生产总值-同比增长"))):
        try:
            df = fn()
        except Exception:
            continue
        for _, row in df.head(12).iterrows():
            period = _jstr(row.get(key_col), 24)
            if not period:
                continue
            rec = {"_key": f"{label}|{period}", "_src": "macro_china_cpi_gdp",
                   "indicator": label, "period": period}
            for c in val_cols:
                if c in df.columns:
                    rec[c] = _jnum(row.get(c))
            out.append(rec)
        time.sleep(0.2)
    if not out:
        return [], {"reason": "宏观 CPI/GDP 无数据"}
    return out, {"source": "macro_china_cpi_gdp", "n": len(out)}


def f_tv_screener(ctx):
    """TradingView 扫描 (POST /china/scan; 容器内常超时, 非实时 15min 延迟)。"""
    import socket
    payload = {"columns": ["name", "description", "close", "change", "volume"],
               "range": [0, int(ctx.get("tv_limit", 100) or 100)]}
    old_to = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(6)
        req = urllib.request.Request(
            "https://scanner.tradingview.com/china/scan", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]})
        b = urllib.request.urlopen(req, timeout=6).read()
    except Exception as e:
        socket.setdefaulttimeout(old_to)
        return [], {"reason": f"TradingView 扫描失败 {type(e).__name__}"}
    finally:
        socket.setdefaulttimeout(old_to)
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "TradingView 返回非 JSON"}
    rows = d.get("data") or []
    out = []
    for r in rows:
        s = r.get("s") or {}
        vals = r.get("d") or []
        # TradingView 的 s 可能是 dict 或字符串, 都兼容
        if isinstance(s, str):
            code = _bare(s)
        elif isinstance(s, dict):
            code = _bare(s.get("name", ""))
        else:
            continue
        # 剥掉 sse:/szse: 市场前缀, 只留 6 位代码
        code = code.split(":")[-1]
        if not code or len(vals) < 3:
            continue
        # columns = [name, description, close, change, volume]
        # vals[0]=代码名(str) vals[1]=中文名(str) vals[2]=收盘 vals[3]=涨跌% vals[4]=成交量
        out.append({
            "_key": code, "_src": "tv_screener", "code": code,
            "name": _jstr(vals[1], 120) or _jstr(vals[0], 120),
            "close": _jnum(vals[2]), "chg_pct": _jnum(vals[3]),
            "volume": _jnum(vals[4] if len(vals) > 4 else None),
        })
    if not out:
        return [], {"reason": "TradingView 扫描返回空"}
    return out, {"source": "tv_screener", "n": len(out)}


# ════════════════════════════════════════════════════════════════
# 需 key 的源: 无 key 返回空 + reason, 不崩
# ════════════════════════════════════════════════════════════════
def _need_key(env_name, sid):
    key = os.environ.get(env_name)
    if not key:
        return None, f"未配置 {env_name} (os.environ), 跳过"
    return key, None


def f_tushare(ctx):
    """tushare.pro (POST JSON; token 从 TUSHARE_TOKEN 读)。"""
    token, err = _need_key("TUSHARE_TOKEN", "tushare")
    if not token:
        return [], {"reason": err}
    body = json.dumps({"api_name": "daily", "token": token,
                       "params": {"ts_code": "600519.SH", "limit": 20},
                       "fields": "ts_code,trade_date,open,high,low,close,vol"}).encode()
    b = _jhttp("http://api.tushare.pro", timeout=8, method="POST", data=body,
               headers={"Content-Type": "application/json"})
    if not b:
        return [], {"reason": "tushare 无响应"}
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "tushare 返回非 JSON"}
    items = ((d.get("data") or {}).get("items")) or []
    fields = (d.get("data") or {}).get("fields") or []
    if not items:
        return [], {"reason": f"tushare code={d.get('code')} (token 无效/积分不足)"}
    out = []
    for row in items:
        rec = dict(zip(fields, row))
        code = _bare(str(rec.get("ts_code", "")).split(".")[0])
        d10 = _jstr(rec.get("trade_date"), 8)
        out.append({
            "_key": f"{code}|{d10}", "_src": "tushare", "code": code, "date": d10,
            "open": _jnum(rec.get("open")), "close": _jnum(rec.get("close")),
            "high": _jnum(rec.get("high")), "low": _jnum(rec.get("low")),
            "volume": _jnum(rec.get("vol")),
        })
    return out, {"source": "tushare", "n": len(out)}


def _mk_quote_key_fetcher(sid, env_name, url_tmpl, keys):
    """通用「需 key 的报价源」模板: 无 key → 空 + reason。"""
    def fn(ctx):
        key, err = _need_key(env_name, sid)
        if not key:
            return [], {"reason": err}
        sym = ctx.get("us_symbol") or "AAPL"
        u = url_tmpl.format(key=key, sym=sym)
        b = _jhttp(u, timeout=8)
        if not b:
            return [], {"reason": f"{sid} 无响应"}
        try:
            d = json.loads(b.decode("utf-8", "replace"))
        except Exception:
            return [], {"reason": f"{sid} 返回非 JSON"}
        # 各家的「有效数据」判据不同, 统一用 keys 探测
        node = d
        for k in keys:
            node = (node or {}).get(k) if isinstance(node, dict) else None
        if node in (None, "", [], {}):
            return [], {"reason": f"{sid} 无有效数据 (key 无效/限额用尽?)"}
        return [{"_key": sym, "_src": sid, "symbol": sym, "raw": d}], \
               {"source": sid, "n": 1}

    fn.__name__ = "f_" + sid
    return fn


def f_iwencai_openapi(ctx):
    """问问开放 API (POST, 中文语义查询独家; IWENCAI_API_KEY)。"""
    key, err = _need_key("IWENCAI_API_KEY", "iwencai_openapi")
    if not key:
        return [], {"reason": err}
    body = json.dumps({"query": ctx.get("iwencai_query") or "今日涨停",
                       "source": "Ths_iwencai_Xuangu",
                       "page": 1, "limit": 10, "is_cache": 1}).encode()
    b = _jhttp("https://openapi.iwencai.com/v1/query2data", timeout=8,
               method="POST", data=body, headers={
                   "Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"})
    if not b:
        return [], {"reason": "iwencai 无响应 (401/限频)"}
    try:
        d = json.loads(b.decode("utf-8"))
    except Exception:
        return [], {"reason": "iwencai 返回非 JSON"}
    datas = d.get("datas") or []
    if not datas:
        return [], {"reason": "iwencai datas 空 (query 无结果/key 无效)"}
    out = []
    for r in datas[: int(ctx.get("iwencai_limit", 20) or 20)]:
        code = _bare(r.get("code") or r.get("股票代码") or r.get("证券代码"))
        if not code:
            continue
        out.append({"_key": code, "_src": "iwencai_openapi", "code": code,
                    "name": _jstr(r.get("股票简称") or r.get("name"), 60),
                    "raw": r})
    if not out:
        return [], {"reason": "iwencai 结果无代码字段"}
    return out, {"source": "iwencai_openapi", "n": len(out)}


def f_yahoo_finance(ctx):
    """Yahoo chart (容器内 429/403 限流, 最低优先级源)。"""
    sym = ctx.get("us_symbol") or "AAPL"
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
    b = _jhttp(u, timeout=8, retries=1)
    if not b:
        return [], {"reason": "Yahoo 无响应 (429 限流/403)"}
    try:
        res = ((json.loads(b.decode("utf-8")).get("chart") or {}).get("result")) or []
    except Exception:
        return [], {"reason": "Yahoo 返回非 JSON"}
    if not res:
        return [], {"reason": "Yahoo result 空"}
    meta = res[0].get("meta") or {}
    ts = res[0].get("timestamp") or []
    closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        d = datetime.fromtimestamp(t, CST).strftime("%Y-%m-%d")
        out.append({"_key": f"{sym}|{d}", "_src": "yahoo_finance",
                    "symbol": sym, "date": d, "close": _jnum(c),
                    "currency": _jstr(meta.get("currency"), 8)})
    if not out:
        return [], {"reason": "Yahoo 收盘序列空"}
    return out, {"source": "yahoo_finance", "n": len(out)}


def f_stooq(ctx):
    """stooq CSV (上游实测 404, 路径已变更 → 直接说明不可用)。"""
    return [], {"reason": "stooq /q/l/ 实测 404 (路径已变更), 未实现"}


# ════════════════════════════════════════════════════════════════
# 明确不实现的源 (SPA/反爬/TCP/需装库) — 返回原因即可
# ════════════════════════════════════════════════════════════════
_NOT_IMPL = {
    "mootdx": "mootdx 是 TCP 二进制协议 (119.147.164.60:7709), 容器内连接超时",
    "xueqiu_hot": "雪球热榜需 xq_a_token cookie, 容器内无法稳定获取",
    "xiaohongshu_hot": "小红书 API 全路径 404 (React SPA), 无法零凭据接入",
    "cls_cn": "财联社 nodeapi/telegraphList 实测 404",
    "kaipanla": "开盘啦 app./xg. 子域 DNS 不可解析, www 被 SPA 劫持",
    "gitcode_api": "GitCode web-api 返回 418 反爬, 需浏览器+登录",
    "akshare": "akshare 是封装层不是独立源, 由 em_a_hist/sina_kline_daily 等 fetcher 内部调用",
    "baostock": "baostock 需先 login() 且注册表单列, 未接入采集管道 (本地可用库)",
    "cn_a_stock_data": "需 pip install cn-a-stock-data, 容器内未安装",
    "xueqiu_anon_token": "仅为取 cookie 的辅助端点, 本身不产出业务记录",
    "em_push2_clist": "push2 主域容器内 TLS RST, 由 em_push2delay_clist 承担",
    "em_push2his_kline_DEPRECATED": "晚间复测 rc=102 data=null, 上游已标记 DEPRECATED",
    "eastmoney_stock_get": "push2 域 WAF, 行业映射已由 industry_map_tv 覆盖",
}


def _mk_not_impl(sid, reason):
    def fn(ctx):
        return [], {"reason": reason}

    fn.__name__ = "f_" + sid
    return fn


for _sid, _reason in _NOT_IMPL.items():
    globals()["f_" + _sid] = _mk_not_impl(_sid, _reason)

# 集思录 10 个端点 fetcher (fund/cb/stock 三套字段映射)
for _sid in JISILU_ENDPOINTS:
    globals()["f_" + _sid] = _mk_jisilu_fetcher(_sid)
del _sid, _reason


# push2 系 fetcher: 复用熔断轮询 (b 分片专用)
def f_em_push2delay_clist(ctx):
    """东财板块列表 (push2delay 域, 容器内同样常被 RST → 三域轮询)。"""
    fs = ctx.get("sector_fs") or "m:90+t:2"
    d, err = _em_multi("/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fs={fs}"
                       "&fields=f12,f14,f2,f3,f62,f184&fltt=2", fs=fs)
    if not d:
        return [], {"reason": err or "push2 三域均不可用"}
    out = []
    for r in ((d.get("data") or {}).get("diff")) or []:
        name = _jstr(r.get("f14"), 60)
        if not name:
            continue
        out.append({
            "_key": name, "_src": "em_push2delay_clist", "name": name,
            "code": _jstr(r.get("f12"), 12),
            "price": _jnum(r.get("f2")), "chg_pct": _jnum(r.get("f3")),
            "main_net": _jnum(r.get("f62")),
            "main_net_ratio": _jnum(r.get("f184")),
        })
    if not out:
        return [], {"reason": "push2delay clist diff 空"}
    return out, {"source": "em_push2delay_clist", "n": len(out)}


def f_em_push2his_clist(ctx):
    """东财全 A 股列表 (push2his 域; 价格单位是分)。"""
    d, err = _em_multi("/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fs={fs}"
                       "&fields=f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f20,f21&fid=f8",
                       fs=ctx.get("stock_fs")
                       or "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048")
    if not d:
        return [], {"reason": err or "push2 三域均不可用"}
    out = []
    for r in ((d.get("data") or {}).get("diff")) or []:
        code = _bare(r.get("f12"))
        if not code:
            continue
        out.append({
            "_key": code, "_src": "em_push2his_clist", "code": code,
            "name": _jstr(r.get("f14"), 60),
            "price": (_jnum(r.get("f2")) or 0) / 100.0,
            "chg_pct": _jnum(r.get("f3")),
            "vol": _jnum(r.get("f5")), "amt": _jnum(r.get("f6")),
            "turnover": _jnum(r.get("f8")), "pe": _jnum(r.get("f9")),
            "vol_ratio": _jnum(r.get("f10")),
            "mktcap": _jnum(r.get("f20")), "floatcap": _jnum(r.get("f21")),
        })
    if not out:
        return [], {"reason": "push2his clist diff 空"}
    return out, {"source": "em_push2his_clist", "n": len(out),
                 "total": (d.get("data") or {}).get("total")}


def f_em_push2his_etf_onsite(ctx):
    """东财场内基金列表 (fs=b:MK0021, 1357只; 价格单位分)。"""
    d, err = _em_multi("/api/qt/clist/get?pn=1&pz=200&po=1&np=1&fs=b:MK0021"
                       "&fields=f12,f13,f14,f2,f3,f6")
    if not d:
        return [], {"reason": err or "push2 三域均不可用"}
    out = []
    for r in ((d.get("data") or {}).get("diff")) or []:
        code = _bare(r.get("f12"))
        if not code:
            continue
        out.append({
            "_key": code, "_src": "em_push2his_etf_onsite", "code": code,
            "name": _jstr(r.get("f14"), 60),
            "market": _jstr(r.get("f13"), 4),
            "price": (_jnum(r.get("f2")) or 0) / 100.0,
            "chg_pct": _jnum(r.get("f3")), "amt": _jnum(r.get("f6")),
        })
    if not out:
        return [], {"reason": "场内基金 diff 空"}
    return out, {"source": "em_push2his_etf_onsite", "n": len(out)}


# 需 key 报价源批量生成 (alpha_vantage / finnhub / twelvedata / fmp / marketstack)
f_alpha_vantage = _mk_quote_key_fetcher(
    "alpha_vantage", "ALPHA_VANTAGE_KEY",
    "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={sym}"
    "&apikey={key}", ["Global Quote"])
f_finnhub = _mk_quote_key_fetcher(
    "finnhub", "FINNHUB_KEY",
    "https://finnhub.io/api/v1/quote?symbol={sym}&token={key}", ["c"])
f_twelvedata = _mk_quote_key_fetcher(
    "twelvedata", "TWELVEDATA_KEY",
    "https://api.twelvedata.com/quote?symbol={sym}&apikey={key}", ["close"])
f_fmp = _mk_quote_key_fetcher(
    "fmp", "FMP_KEY",
    "https://financialmodelingprep.com/api/v3/quote/{sym}?apikey={key}", [])
f_marketstack = _mk_quote_key_fetcher(
    "marketstack", "MARKETSTACK_KEY",
    "http://api.marketstack.com/v1/eod?symbols={sym}&access_key={key}", ["data"])
