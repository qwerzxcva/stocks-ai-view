#!/usr/bin/env python3
"""
collect_moneyflow.py — 实时主力资金流入/流出采集 (2026-10-14 实测)

为什么需要它:
  stock-detail.js 的 fetchFundFlow 走 push2his 的 daykline/get (日级),
  只有「收盘后的历史」, 没有当日实时/分钟级主力净流入; 首页也没有资金流数据。
  本脚本补齐这两块: 个股当日实时 + 分钟序列 + 全市场主力净流入/流出排行。

数据源 (按优先级降级, 任一级拿到即停):

  1. em_fflow_minute   东财分钟级资金流 (当日, 累计值)
     push2/push2delay/push2his 三个域轮询 + 重试。
     字段口径 (实测 2026-10-14, 230 行全量校验):
       col0=时间 col1=f51 主力净流入 col2=f52 小单 col3=f53 中单
       col4=f54 大单    col5=f55 超大单
       恒等式 f54+f55 == f51 (0/230 行违例), 即「主力」= 超大单+大单。
     坑: 值是累计量, 每分钟净流入要 diff; 且请求一次要 1-15 次重试才通
        (见下「连通性实测」)。
  2. em_fflow_daily    东财日级资金流 (近 N 日, 同一接口 klt=101)
  3. em_fflow_rank     东财全市场主力净流入排行 (clist/get, fid=f62)
        po=1 净流入从大到小, po=0 净流出从大到小 (po=-1 返回空, 勿用)
  4. em_index_flow     东财大盘/宽基指数主力净流入 (ulist.np/get)
  5. em_sector_flow    东财板块资金流 (clist/get, fs=m:90+t:2)
  6. dc_stock_flow     东财 datacenter 个股资金流 (上一个交易日, 五档 in/out)
        datacenter-web.eastmoney.com 连通性 10/10, 是 push2 全挂时的兜底
  7. dc_board_flow     东财 datacenter 板块资金流 (上一交易日)
  8. sina_quote        新浪实时行情 (价格/成交额, 无资金字段) — 最后兜底

连通性实测 (本容器, 2026-10-14 14:00-15:00 CST):
  push2* 三个域成功率只有 10-35%, 且失败形态是 TLS 层直接 RST
  (RemoteDisconnected), 不是 HTTP 4xx —— 即「WAF 静默丢弃」而非限流。
  根因是 IPv6 优先但不可用 + A 记录多 IP 轮询, 部分 IP 对 HTTP 请求直接
  重置连接 (curl -4 --resolve 逐 IP 实测: 43.144.251.121 与 101.226.30.206
  偶发可用, 14.103.191.91 25/25 全挂)。因此本脚本对所有 push2 调用统一
  走「多域轮询 + 重试到上限」, 不假设单次能通。
  datacenter-web / data.eastmoney.com 10/10 稳定, 但只有上一交易日数据。

用法:
  python3 collect_moneyflow.py --out data/moneyflow.json
  python3 collect_moneyflow.py --code 600519 --top 20
  python3 collect_moneyflow.py --code 600519,000001 --top 50 --retries 25
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
EM_REF = {"Referer": "https://quote.eastmoney.com/"}
SINA_REF = {"Referer": "https://finance.sina.com.cn/"}

# push2 三个域: 本容器 IP 层 RST, 成功率 10-35%, 必须多域轮询
EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com",
            "push2his.eastmoney.com")
# datacenter 域: 连通性 10/10, 但数据滞后一个交易日
DC_HOST = "datacenter-web.eastmoney.com"

# 分钟/日级资金流字段: f51 主力 f52 小单 f53 中单 f54 大单 f55 超大单
FFLOW_FIELDS = "f51,f52,f53,f54,f55"
# 全市场排行字段 (clist/get): 代码/名称/价/涨跌幅/主力净流入/主力净占比
# 必须带 fltt=2 才是元/百分比; 不带时 f2/f3/f184 全部 x100
# (实测: f2=586→5.86 元, f3=994→9.94%, f184=3998→39.98%, 已用 qt.gtimg.cn 交叉验证)
RANK_FIELDS = "f12,f14,f2,f3,f62,f184,f124"
# 板块排行字段
SECTOR_FIELDS = "f12,f14,f62,f184"
# 指数资金流字段 (ulist.np/get): f62 主力净流入 f66 超大单净流入 f184 占比
INDEX_FIELDS = "f62,f66,f184"
# 沪深 A 股全市场 (含科创/创业/北交所)
ALL_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
# 行业板块
SECTOR_FS = "m:90+t:2"

# datacenter 报表名 (实测可用; 任务书给的 RPT_DATA_WEB_JZ_JZLB 已下线)
DC_STOCK_REPORT = "RPT_DMSK_TS_STOCKNEW"
DC_BOARD_REPORT = "RPT_INDUSTRY_FUNDFLOW"


def _secid(code):
    """6 位代码 → 东财 secid (1=沪, 0=深)。"""
    c = str(code).strip()
    return ("1." if c.startswith(("5", "6", "9")) else "0.") + c


def _tk(code):
    """6 位代码 → 新浪前缀代码。"""
    c = str(code).strip()
    if c.startswith(("sh", "sz")):
        return c
    return ("sh" if c.startswith(("5", "6", "9")) else "sz") + c


def _get(url, ref, retries=3, timeout=5.0, text=False):
    """GET + 重试。失败形态多为 TLS RST (RemoteDisconnected), 必须重试。

    返回 (payload, err)。payload 为 None 表示彻底失败。
    """
    h = dict(UA)
    h.update(ref or {})
    last = "unknown"
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            b = r.read()
            return (b.decode("utf-8", "ignore") if text
                    else json.loads(b.decode("utf-8", "ignore"))), None
        except Exception as e:  # 含 RemoteDisconnected / URLError / JSONDecodeError
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
            if i < retries - 1:
                time.sleep(0.4 * (i + 1))
    return None, last


# ─── 熔断器 ────────────────────────────────────────────────────
# 东财对高频请求的惩罚是 TLS RST, 继续重试只会延长封锁。连续失败若干次后
# 本周期直接放弃 push2*, 只走 datacenter 兜底 — 这既省时间也避免加剧封锁。
_CB = {"fails": 0, "tripped": False, "threshold": 6}


def cb_trip():
    """标记一次 push2 失败; 达到阈值则置熔断。"""
    _CB["fails"] += 1
    if _CB["fails"] >= _CB["threshold"]:
        _CB["tripped"] = True


def cb_ok():
    """一次成功即重置计数 (说明封锁已解除)。"""
    _CB["fails"] = 0
    _CB["tripped"] = False


def cb_open():
    return _CB["tripped"]


def _get_multi(paths, ref, retries=3, timeout=5.0):
    """同一接口在多个 push2 域上轮询, 任一成功即停。

    单域成功率 10-35%, 单域重试到上限仍可能全挂; 换域重试能显著提高
    拿到数据的概率 (实测: 单域 15 次重试全挂, 换域后 1-3 次即通)。
    三个域并发请求, 避免串行把墙钟时间放大 3 倍 (实测串行 80s+,
    并发约 1/3)。任一域先拿到数据即返回。

    熔断: 连续失败达阈值后直接返回, 不再打 push2 — 继续打只会加剧封锁,
    且本周期已无望拿到实时数据, 交给 datacenter 兜底更快。
    """
    if cb_open():
        return None, "circuit_open (push2 连续失败, 本周期跳过)"
    last = "unknown"
    if len(paths) > 1:
        try:
            with ThreadPoolExecutor(max_workers=len(paths)) as ex:
                futs = [ex.submit(_get, p, ref, retries=retries, timeout=timeout)
                        for p in paths]
                for f in futs:
                    d, err = f.result()
                    if d is not None:
                        cb_ok()
                        return d, None
                    last = err
            cb_trip()
            return None, last
        except Exception:
            pass
    for p in paths:
        d, err = _get(p, ref, retries=retries, timeout=timeout)
        if d is not None:
            cb_ok()
            return d, None
        last = err
    cb_trip()
    return None, last


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── 1. 东财分钟级/日级资金流 ──────────────────────────────
def fetch_fflow(code, klt=1, lmt=0, retries=2):
    """资金流 K 线。klt=1 分钟(当日累计) / 101 日 / 102 周 / 103 月。

    返回 (klines, err)。klines 为 [ {time, main_net, small_net, medium_net,
    large_net, xlarge_net}, ... ]; 值为累计量 (分钟级) 或当日量 (日级)。
    """
    qs = ("secid=%s&lmt=%s&klt=%s&fields1=f1,f2,f3,f7&fields2=%s"
          % (_secid(code), lmt, klt, FFLOW_FIELDS))
    paths = ["https://%s/api/qt/stock/fflow/kline/get?%s" % (h, qs)
             for h in EM_HOSTS]
    d, err = _get_multi(paths, EM_REF, retries=retries)
    if d is None:
        return [], err
    kl = ((d.get("data") or {}).get("klines")) or []
    out = []
    for line in kl:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        out.append({
            "time": p[0],
            "main_net": _num(p[1]),      # f51 主力 = f54 + f55
            "small_net": _num(p[2]),     # f52 小单
            "medium_net": _num(p[3]),    # f53 中单
            "large_net": _num(p[4]),     # f54 大单
            "xlarge_net": _num(p[5]),    # f55 超大单
        })
    return out, None


def minute_deltas(series):
    """分钟累计量 → 每分钟净流入 (diff)。首点无前值, 直接给累计值。"""
    out = []
    prev = None
    for r in series:
        v = r.get("main_net")
        if v is None:
            continue
        out.append({"time": r.get("time"),
                    "main_net": round(v - prev, 2) if prev is not None else round(v, 2)})
        prev = v
    return out


# ─── 2. 东财全市场主力净流入/流出排行 ────────────────────────
def fetch_rank(direction="in", top=20, retries=2):
    """全市场主力净流入排行。direction=in 净流入降序, out 净流出降序。

    po=1 → f62 从大到小 (净流入); po=0 → f62 从小到大 (净流出)。
    po=-1 实测返回空 diff, 不能用。
    带 fltt=2 后 f3/f184 为百分比原值; 不带则全部 x100 (见 RANK_FIELDS 注释)。
    """
    po = "1" if direction == "in" else "0"
    qs = ("pn=1&pz=%d&po=%s&np=1&fltt=2&fid=f62&fs=%s&fields=%s"
          % (top, po, ALL_A_FS, RANK_FIELDS))
    paths = ["https://%s/api/qt/clist/get?%s" % (h, qs) for h in EM_HOSTS]
    d, err = _get_multi(paths, EM_REF, retries=retries)
    if d is None:
        return [], err
    out = []
    for r in ((d.get("data") or {}).get("diff")) or []:
        stamp = _num(r.get("f124"))
        quote_time = (datetime.fromtimestamp(stamp, CST).isoformat(timespec="seconds")
                      if stamp and stamp > 1_000_000_000 else None)
        out.append({
            "code": str(r.get("f12") or ""),
            "name": r.get("f14") or "",
            "chg_pct": _num(r.get("f3")),
            "main_net": _num(r.get("f62")),
            "main_net_ratio": _num(r.get("f184")),
            "date": quote_time[:10] if quote_time else None,
            "snapshot_ts": quote_time,
            "_src": "em_fflow_rank",
        })
    return out, None


# ─── 3. 东财大盘/指数主力净流入 ─────────────────────────────
def fetch_index_flow(secids=("1.000001", "0.399001"), retries=2):
    """指数/大盘主力净流入。secids 顺序即返回顺序。

    注意: ulist.np 的 f43/f48 口径不对 (实测 f43=1013870.58, f48=159.7 是
    换手率), 故这里只取资金字段 f62/f66/f184。
    """
    qs = "fltt=2&secids=%s&fields=%s" % (",".join(secids), INDEX_FIELDS)
    paths = ["https://%s/api/qt/ulist.np/get?%s" % (h, qs) for h in EM_HOSTS]
    d, err = _get_multi(paths, EM_REF, retries=retries)
    if d is None:
        return [], err
    names = {"1.000001": "上证指数", "0.399001": "深证成指",
             "1.000300": "沪深300", "0.399006": "创业板指",
             "1.000688": "科创50", "0.399005": "中小100"}
    out = []
    for sid, r in zip(secids, ((d.get("data") or {}).get("diff")) or []):
        out.append({"secid": sid, "name": names.get(sid, sid),
                    "main_net": _num(r.get("f62")),
                    "xlarge_net": _num(r.get("f66")),
                    "main_net_ratio": _num(r.get("f184"))})
    return out, None


# ─── 4. 东财板块资金流 ─────────────────────────────────────
def fetch_sector_flow(top=20, retries=2):
    """行业板块主力净流入排行 (fs=m:90+t:2)。"""
    qs = ("fid=f62&po=1&pz=%d&pn=1&np=1&fltt=2&fs=%s&fields=%s"
          % (top, SECTOR_FS, SECTOR_FIELDS))
    paths = ["https://%s/api/qt/clist/get?%s" % (h, qs) for h in EM_HOSTS]
    d, err = _get_multi(paths, EM_REF, retries=retries)
    if d is None:
        return [], err
    out = []
    for r in ((d.get("data") or {}).get("diff")) or []:
        out.append({"code": str(r.get("f12") or ""),
                    "name": r.get("f14") or "",
                    "main_net": _num(r.get("f62")),
                    "main_net_ratio": _num(r.get("f184"))})
    return out, None


# ─── 5. datacenter 个股/板块资金流 (push2 全挂时兜底) ─────────
def fetch_dc_stock_flow(code, limit=5, retries=3):
    """datacenter 个股资金流, 按 TRADE_DATE 降序 (上一个交易日, 五档 in/out)。"""
    u = ("https://%s/api/data/v1/get?reportName=%s&columns=ALL"
         "&filter=(SECURITY_CODE%%3D%%22%s%%22)&pageSize=%d"
         "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB"
         % (DC_HOST, DC_STOCK_REPORT, str(code).strip(), limit))
    d, err = _get(u, EM_REF, retries=retries)
    if d is None:
        return [], err
    out = []
    for r in ((d.get("result") or {}).get("data")) or []:
        def net(a, b):
            va, vb = _num(r.get(a)), _num(r.get(b))
            return (va - vb) if (va is not None and vb is not None) else None
        out.append({
            "date": (r.get("TRADE_DATE") or "")[:10],
            "main_net": _num(r.get("PRIME_INFLOW")),
            "xlarge_net": net("SUPERDEAL_INFLOW", "SUPERDEAL_OUTFLOW"),
            "large_net": net("BIGDEAL_INFLOW", "BIGDEAL_OUTFLOW"),
            "close": _num(r.get("CLOSE_PRICE")),
            "chg_pct": _num(r.get("CHANGE_RATE")),
        })
    return out, None


def _dc_last_date(report, retries=3):
    """datacenter 报表最近交易日 (最新一页第一条)。"""
    u = ("https://%s/api/data/v1/get?reportName=%s&columns=ALL&pageSize=1"
         "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB"
         % (DC_HOST, report))
    d, err = _get(u, EM_REF, retries=retries)
    if d is None:
        return None, err
    rows = ((d.get("result") or {}).get("data")) or []
    if not rows:
        return None, "empty"
    return (rows[0].get("TRADE_DATE") or "")[:10], None


def fetch_dc_rank(direction="in", top=20, trade_date=None, retries=3):
    """datacenter 全市场主力净流入排行 (上一交易日)。

    排序字段是 PRIME_INFLOW 而非 NET_INFLOW; 不带 TRADE_DATE 过滤时按
    PRIME_INFLOW 排序会跨日期混算, 所以显式传最近交易日。
    """
    if not trade_date:
        trade_date, err = _dc_last_date(DC_STOCK_REPORT, retries=retries)
        if not trade_date:
            return [], err or "no trade date"
    sort = "-1" if direction == "in" else "1"
    u = ("https://%s/api/data/v1/get?reportName=%s&columns=ALL"
         "&filter=(TRADE_DATE%%3D%%27%s%%27)&pageSize=%d"
         "&sortColumns=PRIME_INFLOW&sortTypes=%s&source=WEB&client=WEB"
         % (DC_HOST, DC_STOCK_REPORT, trade_date, top, sort))
    d, err = _get(u, EM_REF, retries=retries)
    if d is None:
        return [], err
    out = []
    for r in ((d.get("result") or {}).get("data")) or []:
        out.append({"code": str(r.get("SECURITY_CODE") or ""),
                    "name": r.get("SECURITY_NAME_ABBR") or "",
                    "main_net": _num(r.get("PRIME_INFLOW")),
                    "close": _num(r.get("CLOSE_PRICE")),
                    "chg_pct": _num(r.get("CHANGE_RATE")),
                    "date": trade_date})
    return out, None


def fetch_dc_board_flow(top=20, retries=3):
    """datacenter 板块资金流排行 (上一交易日, 五档净额 + 占比)。"""
    td, err = _dc_last_date(DC_BOARD_REPORT, retries=retries)
    if not td:
        return [], err or "no board date"
    u = ("https://%s/api/data/v1/get?reportName=%s&columns=ALL"
         "&filter=(TRADE_DATE%%3D%%27%s%%27)&pageSize=%d"
         "&sortColumns=NET_INFLOW&sortTypes=-1&source=WEB&client=WEB"
         % (DC_HOST, DC_BOARD_REPORT, td, top))
    d, err = _get(u, EM_REF, retries=retries)
    if d is None:
        return [], err
    out = []
    for r in ((d.get("result") or {}).get("data")) or []:
        out.append({"code": str(r.get("BOARD_CODE") or ""),
                    "name": r.get("BOARD_NAME") or "",
                    "main_net": _num(r.get("NET_INFLOW")),
                    "main_net_ratio": _num(r.get("NET_INFLOW_RATIO")),
                    "date": td})
    return out, None


# ─── 6. 新浪兜底 (无资金字段, 只给价格/成交额) ────────────────
def fetch_sina_quote(codes, retries=2):
    """新浪实时行情。实测无主力资金字段, 仅作价格/成交额兜底。"""
    if not codes:
        return {}, "no codes"
    u = "https://hq.sinajs.cn/list=%s" % ",".join(_tk(c) for c in codes)
    raw, err = _get(u, SINA_REF, retries=retries, text=True)
    if raw is None:
        return {}, err
    # GBK 编码, 非 UTF-8; 字段序: 0名称 3现价 9成交额 30日期 31时间
    try:
        body = raw.encode("utf-8").decode("gbk", "ignore")
    except Exception:
        body = raw
    out = {}
    for m in re.finditer(r'var hq_str_(\w+)="([^"]*)"', body):
        sym, payload = m.group(1), m.group(2)
        f = payload.split(",")
        if len(f) < 32 or not sym[2:]:
            continue
        out[sym[2:]] = {
            "code": sym[2:], "name": f[0], "price": _num(f[3]),
            "amount": _num(f[9]), "date": f[30], "time": f[31],
            "main_net": None, "main_net_ratio": None, "_src": "sina",
        }
    return out, None


# ─── 组装 ─────────────────────────────────────────────────
def _stock_slot(c, minute, daily, retries):
    """单只个股的降级链: push2 分钟 → push2 日级 → datacenter → 新浪。"""
    item = {"code": c, "name": None, "main_net_today": None,
            "main_net_5d": None, "minute_series": [],
            "minute_delta": [], "daily_series": [],
            "source": None, "reason": None}
    reasons = []
    if minute:
        item["source"] = "em_fflow_minute"
        item["main_net_today"] = minute[-1].get("main_net")
        item["minute_series"] = minute
        item["minute_delta"] = minute_deltas(minute)
    if daily:
        if item["source"] is None:
            item["source"] = "em_fflow_daily"
        item["daily_series"] = daily
        item["main_net_5d"] = [d.get("main_net") for d in daily]
        if item["main_net_today"] is None:
            item["main_net_today"] = daily[-1].get("main_net")
    if item["main_net_today"] is None:
        dc, err = fetch_dc_stock_flow(c, retries=3)
        if dc:
            item["source"] = "dc_stock_flow"
            item["daily_series"] = dc
            item["main_net_today"] = dc[0].get("main_net")
            item["main_net_5d"] = [d.get("main_net") for d in dc]
            item["reason"] = "东财 push2 不可用, 降级为上一交易日数据"
        else:
            reasons.append("dc_stock_flow(%s): %s" % (c, err))
    if item["main_net_today"] is None:
        sq, err = fetch_sina_quote([c])
        if c in sq:
            item.update(sq[c])
            item["reason"] = "东财/新浪资金流均不可用, 仅有实时价格"
        else:
            reasons.append("sina_quote(%s): %s" % (c, err))
    return item, reasons


def collect(code=None, top=12, retries=2, include_sector=True):
    """采集一次, 返回结构化 dict。任何一级失败都不抛异常, 记 reason。

    所有数据源并发执行: push2 单次成功率仅 10-35%, 串行会把墙钟时间
    放大到 100s+ (实测), 并发后总耗时约等于最慢单源。
    """
    ts = datetime.now(CST).isoformat(timespec="seconds")
    reasons = []
    sources = []
    codes = [x.strip() for x in str(code or "").split(",") if x.strip()]

    jobs = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for c in codes:
            jobs[("minute", c)] = ex.submit(fetch_fflow, c, 1, 0, retries)
            jobs[("daily", c)] = ex.submit(fetch_fflow, c, 101, 5, retries)
        if top:
            jobs[("rank_in",)] = ex.submit(fetch_rank, "in", top, retries)
            jobs[("rank_out",)] = ex.submit(fetch_rank, "out", top, retries)
        jobs[("index",)] = ex.submit(fetch_index_flow, retries=retries)
        if include_sector:
            jobs[("sector",)] = ex.submit(fetch_sector_flow, top, retries)
        res = {k: v.result() for k, v in jobs.items()}

    stocks = {}
    for c in codes:
        minute, err = res.get(("minute", c), ([], None))
        if not minute:
            reasons.append("em_fflow_minute(%s): %s" % (c, err))
        daily, err = res.get(("daily", c), ([], None))
        if not daily:
            reasons.append("em_fflow_daily(%s): %s" % (c, err))
        item, rs = _stock_slot(c, minute, daily, retries)
        reasons.extend(rs)
        if item["source"]:
            sources.append(item["source"])
        stocks[c] = item

    top_in = top_out = []
    if top:
        top_in, err = res.get(("rank_in",), ([], None))
        if top_in:
            sources.append("em_fflow_rank")
        else:
            reasons.append("em_fflow_rank(in): %s" % err)
            top_in, err = fetch_dc_rank("in", top=top, retries=3)
            if top_in:
                sources.append("dc_stock_rank")
            else:
                reasons.append("dc_stock_rank(in): %s" % err)
        top_out, err = res.get(("rank_out",), ([], None))
        if top_out:
            sources.append("em_fflow_rank")
        else:
            reasons.append("em_fflow_rank(out): %s" % err)
            top_out, err = fetch_dc_rank("out", top=top, retries=3)
            if top_out:
                sources.append("dc_stock_rank")
            else:
                reasons.append("dc_stock_rank(out): %s" % err)

    index_flow, err = res.get(("index",), ([], None))
    if index_flow:
        sources.append("em_index_flow")
    else:
        reasons.append("em_index_flow: %s" % err)

    sector_flow = []
    if include_sector:
        sector_flow, err = res.get(("sector",), ([], None))
        if sector_flow:
            sources.append("em_sector_flow")
        else:
            reasons.append("em_sector_flow: %s" % err)
            sector_flow, err = fetch_dc_board_flow(top=top, retries=3)
            if sector_flow:
                sources.append("dc_board_flow")
            else:
                reasons.append("dc_board_flow: %s" % err)

    # 数据时效: 全部源都是 dc_ 前缀说明本轮完全降级 (push2 全挂)
    fully_degraded = bool(sources) and all(s.startswith("dc_") for s in sources)
    data_dates = set()
    for s in stocks.values():
        for r in (s.get("daily_series") or []):
            if r.get("date"):
                data_dates.add(r["date"])
    for r in top_in + top_out:
        if r.get("date"):
            data_dates.add(r["date"])
    today = datetime.now(CST).strftime("%Y-%m-%d")
    stale_days = None
    if data_dates:
        newest = max(data_dates)
        try:
            stale_days = (datetime.strptime(today, "%Y-%m-%d")
                          - datetime.strptime(newest, "%Y-%m-%d")).days
        except ValueError:
            stale_days = None
    return {
        "ts": ts,
        "source": ",".join(dict.fromkeys(sources)) or None,
        "realtime": bool(not fully_degraded and (
            top_in or any(s.get("main_net_today") is not None for s in stocks.values()))),
        "fully_degraded": fully_degraded,
        "data_date": max(data_dates) if data_dates else None,
        "stale_days": stale_days,
        "circuit_open": cb_open(),
        "stocks": stocks,
        "top_inflow": top_in,
        "top_outflow": top_out,
        "index_flow": index_flow,
        "sector_flow": sector_flow,
        "reason": "; ".join(reasons) if reasons else None,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", default="600519",
                    help="个股代码, 逗号分隔多个 (默认 600519)")
    ap.add_argument("--top", type=int, default=20, help="主力净流入/流出排行条数")
    ap.add_argument("--out", default=os.path.join(
        os.environ.get("STOCKS_AI_VIEW") or
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "moneyflow.json"), help="输出 JSON 路径")
    ap.add_argument("--retries", type=int, default=8,
                    help="每个 push2 域的重试次数 (本容器单域成功率仅 10-35%%)")
    ap.add_argument("--no-sector", action="store_true", help="跳过板块资金流")
    args = ap.parse_args()

    out = collect(code=args.code, top=args.top, retries=args.retries,
                  include_sector=not args.no_sector)

    d = os.path.dirname(os.path.abspath(args.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    n_in = len(out.get("top_inflow") or [])
    n_out = len(out.get("top_outflow") or [])
    n_sec = len(out.get("sector_flow") or [])
    print("[moneyflow] ts=%s source=%s realtime=%s"
          % (out["ts"], out["source"], out["realtime"]))
    for c, s in (out.get("stocks") or {}).items():
        print("  %s %s main_today=%s minute_rows=%d daily_rows=%d src=%s"
              % (c, s.get("name"), s.get("main_net_today"),
                 len(s.get("minute_series") or []),
                 len(s.get("daily_series") or []), s.get("source")))
    print("  top_inflow=%d top_outflow=%d sector=%d index=%d"
          % (n_in, n_out, n_sec, len(out.get("index_flow") or [])))
    if out.get("reason"):
        print("  reason: %s" % out["reason"])
    print("  -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
