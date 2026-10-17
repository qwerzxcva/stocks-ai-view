#!/usr/bin/env python3
"""
collect_market_extras.py — 独立的市场数据采集模块 (零依赖, 单文件)

用途:
  给「另一台服务器 / GitHub Actions / 本地」提供一套不依赖 ai_screener.py 的
  数据采集入口, 只产出 latest.json 里新增的这些字段:

    market.advance/decline/flat/up_pct/turnover   涨跌家数 + 两市成交额
    market.limit_up/limit_down/broken (修正)      以东财 data.tc 为准
    commentary                                      盘面解读 (规则引擎)
    ths_strong                                      同花顺强势股 + 题材归因
    lhb                                             龙虎榜
    blocktrade                                      大宗交易
    margin                                          融资融券序列
    index_kline                                     上证指数历史日K
    westock.sector_ranking                          腾讯行业板块排行

用法:
  # 1) 直接生成一个只含新增字段的 JSON (便于调试)
  python3 collect_market_extras.py --out /tmp/extras.json

  # 2) 合并进已有的 latest.json (就地更新, 保留其余字段)
  python3 collect_market_extras.py --merge /path/to/latest.json

  # 3) 指定日期 (默认今天, CST)
  python3 collect_market_extras.py --merge latest.json --date 2026-10-12

设计原则:
  - 全部零鉴权 (腾讯/东财/同花顺公开接口)
  - 单文件, 可从 ai_screener.py 直接 import: from collect_market_extras import collect_all
  - 每个数据源独立 try/except, 单个失败不影响其他
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://quote.eastmoney.com/"}


# ─── 基础工具 ────────────────────────────────────────────────────────
def _http(url, timeout=12, retries=3, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                          timeout=timeout).read()
        except Exception:
            # 东财对连续请求会 RemoteDisconnected; 退避短了重试也白试
            # (实测 0.3s 间隔连丢 2 次, 3s 后第 3 次就通)。
            if i < retries - 1:
                time.sleep(2.0 * (i + 1))
    return b""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── 1. 涨跌停池 (核心修复: 读 data.tc 而非只读 pool) ─────────────────
def fetch_pool(kind, date_str, retries=3):
    """东财涨跌停池。

    关键: data.pool 可能为空数组, 而 data.tc 仍是权威计数。
    实测 2026-10-12 DT: pool=[] 但 tc=19 → 若只读 pool 会误显示跌停 0。
    """
    url = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
           f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}")
    for _ in range(retries):
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                          timeout=12).read()
            d = json.loads(body.decode("utf-8"))
            data = d.get("data") or {}
            tc = data.get("tc")
            pool = data.get(f"{kind}Pool") or data.get("pool") or []
            out = []
            for p in pool:
                pval = float(p.get("p", 0) or 0)
                # 东财价格单位是「厘」: 116050 → 116.05 元
                # 阈值判定: 正常股价 <1000 元, 故 pval>1000 才是厘
                price = pval / 1000.0 if pval > 1000 else pval
                out.append({
                    "code": str(p.get("c", p.get("code", ""))),
                    "name": str(p.get("n", p.get("name", ""))),
                    "price": round(price, 2),
                    "chg_pct": round(float(p.get("zdp", 0) or 0), 2),
                    "lianban": int(p.get("lbc", p.get("count", 1)) or 1),
                    "zt_time": str(p.get("fbt", "")),
                    "industry": str(p.get("hybk", "") or ""),
                })
            return {"count": int(tc) if tc is not None else len(pool), "pool": out}
        except Exception as e:
            print(f"[pool {kind}] {e}", file=sys.stderr)
    return {"count": 0, "pool": []}


# ─── 1b. 竞价抢筹榜 ────────────────────────────────────────────────
# 09:25 集合竞价结束即封涨停的票 = 竞价阶段资金直接抢筹,
# 不用额外接口: 涨停池的 fbt 字段就是首封时间 (HHMMSS)。
def build_jingjia(zt_pool):
    out = []
    for p in zt_pool or []:
        t = str(p.get("zt_time") or "").strip()
        if not t.isdigit():
            continue
        # 东财 fbt 省略前导零: 92500 = 09:25:00, 也可能给满 6 位
        t = t.zfill(6)
        hh, mm = int(t[:2]), int(t[2:4])
        # 竞价窗口 09:15-09:25; 09:25:00 整是竞价撮合出结果的时刻
        if not (hh == 9 and 15 <= mm <= 25):
            continue
        rec = dict(p)
        rec["auction_time"] = t
        # 竞价抢筹强度: 连板越高 + 封得越早 = 越强
        rec["strength"] = (p.get("lianban") or 1) * 10 + (25 - mm)
        out.append(rec)
    out.sort(key=lambda x: -x["strength"])
    return out


# ─── 2. 涨跌分布 ────────────────────────────────────────────────────
def fetch_zdfenbu(date_str):
    """东财涨跌分布: fenbu 是 [{'-11':19},{'11':46},...] 形式。"""
    url = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&date={date_str}")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=12).read()
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
        # 涨停家数 = 所有 ≥+10% 的档位合计 (主板10cm + 双创20cm + 北交所30cm)
        # 跌停同理 ≤-10%。只用 dist[11] 会漏掉 20cm/30cm 涨停板 (2026-10-13 实测漏 22 家)
        zt_like = sum(c for pct, c in dist.items() if pct >= 10)
        dt_like = sum(c for pct, c in dist.items() if pct <= -10)
        return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
                "up_pct": round(up / max(1, up + down + flat) * 100, 1),
                "zt_like": zt_like, "dt_like": dt_like, "dist": dist}
    except Exception as e:
        print(f"[zdfenbu] {e}", file=sys.stderr)
        return {}


# ─── 3. 两市成交额 ──────────────────────────────────────────────────
def fetch_turnover():
    """腾讯指数成交额累加 (p[37], 万元→亿元)。"""
    total_yi, parts = 0.0, {}
    for code, label in (("sh000001", "沪市"), ("sz399001", "深市")):
        try:
            body = _http(f"https://qt.gtimg.cn/q={code}", timeout=10, retries=2)
            for line in body.decode("gbk", "replace").strip().split("\n"):
                if "=" not in line:
                    continue
                p = line.split("=", 1)[1].strip('";\n').split("~")
                if len(p) > 37:
                    yi = float(p[37] or 0) / 1e4
                    parts[label] = round(yi, 1)
                    total_yi += yi
                break
        except Exception as e:
            print(f"[turnover {code}] {e}", file=sys.stderr)
    return {"parts": parts, "total_yi": round(total_yi, 1)} if parts else {}


# ─── 4. 融资融券 ────────────────────────────────────────────────────
def fetch_margin(days=30):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPTA_RZRQ_LSHJ&columns=ALL"
           f"&pageSize={days}&pageNumber=1"
           "&sortColumns=dim_date&sortTypes=-1&source=WEB&client=WEB")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        return [{
            "date": r.get("DIM_DATE", ""),
            "rzye": r.get("RZYE"), "rqye": r.get("RQYE"), "rzrqye": r.get("RZRQYE"),
            "rzmre": r.get("RZMRE"), "rzche": r.get("RZCHE"),
        } for r in ((d.get("result") or {}).get("data") or [])]
    except Exception as e:
        print(f"[margin] {e}", file=sys.stderr)
        return []


# ─── 5. 指数历史日K ─────────────────────────────────────────────────
def fetch_index_kline(code="sh000001", days=120):
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
        d = json.loads(_http(url, timeout=12).decode("utf-8"))
        arr = ((d.get("data") or {}).get(code) or {})
        arr = arr.get("qfqday") or arr.get("day") or []
        return [{"date": r[0], "close": float(r[2]), "vol": float(r[5]) if len(r) > 5 else 0}
                for r in arr]
    except Exception as e:
        print(f"[kline {code}] {e}", file=sys.stderr)
        return []


# ─── 6. 同花顺强势股 + 题材归因 ─────────────────────────────────────
def fetch_ths_strong_stocks(date_dash):
    url = ("http://zx.10jqka.com.cn/event/api/getharden"
           f"/date/{date_dash}/orderby/date/orderway/desc/charset/GBK/")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=15).read()
        d = json.loads(body.decode("gbk", "replace"))
        return [{"code": str(r.get("code", "")).strip(),
                 "name": str(r.get("name", "")).strip(),
                 "reason": str(r.get("reason", "")).strip()}
                for r in (d.get("data") or []) if r.get("code")]
    except Exception as e:
        print(f"[ths] {e}", file=sys.stderr)
        return []


# ─── 7. 东财 datacenter (龙虎榜 / 大宗交易) ─────────────────────────
def fetch_datacenter(report, sort_col, date_dash=None, page_size=20):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           f"?reportName={report}&columns=ALL&pageSize={page_size}&pageNumber=1"
           f"&sortColumns={sort_col}&sortTypes=-1&source=WEB&client=WEB")
    if date_dash:
        url += f"&filter=(TRADE_DATE%3D%27{date_dash}%27)"
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=15).read()
        d = json.loads(body.decode("utf-8"))
        return (d.get("result") or {}).get("data") or []
    except Exception as e:
        print(f"[dc:{report}] {e}", file=sys.stderr)
        return []


def fetch_lhb(date_dash):
    return [{
        "code": str(r.get("SECURITY_CODE", "") or ""),
        "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
        "close": r.get("CLOSE_PRICE"), "chg": r.get("CHANGE_RATE"),
        "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
        "expl": str(r.get("EXPLAIN", "") or "")[:60],
    } for r in fetch_datacenter("RPT_DAILYBILLBOARD_DETAILS", "TRADE_DATE",
                                date_dash, 30)]


def fetch_blocktrade(date_dash):
    return [{
        "code": str(r.get("SECURITY_CODE", "") or ""),
        "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
        "price": r.get("DEAL_PRICE"), "premium": r.get("PREMIUM_RATIO"),
        "vol": r.get("DEAL_VOLUME"), "amt": r.get("DEAL_AMT"),
    } for r in fetch_datacenter("RPT_DATA_BLOCKTRADE", "TRADE_DATE", date_dash, 30)]


# ─── 实时指数行情 ────────────────────────────────────────────
def fetch_indices():
    """腾讯实时指数 (GBK 解码, 字段已实测核对)"""
    IDX = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sh000300", "沪深300"),
           ("sz399006", "创业板指"), ("sh000688", "科创50"), ("bj899050", "北证50")]
    out = []
    url = "https://qt.gtimg.cn/q=" + ",".join(c for c, _ in IDX)
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        text = body.decode("gbk", "replace")
        for m in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', text):
            sym, payload = m.group(1), m.group(2).split("~")
            if len(payload) < 40:
                continue
            nm = next((n for c, n in IDX if c == sym), payload[1])
            try:
                out.append({
                    "code": sym, "name": nm, "price": float(payload[3] or 0),
                    "chg_pct": float(payload[32] or 0),
                    "turnover": float(payload[38] or 0) if len(payload) > 38 else 0,
                    "pe": float(payload[39] or 0) if len(payload) > 39 else 0,
                    "mktcap": (float(payload[44] or 0) if len(payload) > 44 else 0) * 1e8,
                    "pb": float(payload[46] or 0) if len(payload) > 46 else 0,
                })
            except (ValueError, IndexError):
                continue
    except Exception as e:
        print(f"[indices] {e}", file=sys.stderr)
    return out


# ─── 8. 腾讯行业板块排行 ────────────────────────────────────────────
def fetch_tx_sector_ranking(limit=20):
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
           f"?l={limit}&p=1&t=01/averatio")
    try:
        d = json.loads(_http(url, timeout=15).decode("utf-8"))
        out = []
        for r in (d.get("data") or []):
            name = str(r.get("bd_name", "") or "").strip()
            if not name:
                continue
            out.append({
                "name": name, "code": str(r.get("bd_code", "") or ""),
                "changePct": _f(r.get("bd_zdf")), "chg5d": _f(r.get("bd_zdf5")),
                "chg20d": _f(r.get("bd_zdf20")),
                "leader": str(r.get("nzg_name", "") or ""),
                "leaderPct": _f(r.get("nzg_zdf")),
                "mainNetInflow": None, "mainNetInflow5d": None,
            })
        return out
    except Exception as e:
        print(f"[tx-rank] {e}", file=sys.stderr)
        return []


# ─── 8b. 板块资金流 (东财行业主力净额) ─────────────────────────
# 腾讯 mktHs/rank 只给涨跌幅, 不给主力净额; 板块资金单独走东财 clist
# (m:90 = 行业板块, fid=f_flow 按主力净额倒序)。
def fetch_sector_moneyflow(limit=20):
    urls = [
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2his.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
    ]
    query = ("?fid=f62&po=1&pz=%d&pn=1&np=1&fltt=2&invt=2"
             "&fs=m:90+t:2&fields=f12,f14,f3,f62,f184,f128,f136" % limit)
    d = {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    for base in urls:
        try:
            req = urllib.request.Request(base + query, headers=headers)
            body = urllib.request.urlopen(req, timeout=15).read()
            d = json.loads(body.decode("utf-8"))
            if ((d.get("data") or {}).get("diff")):
                break
        except Exception:
            continue
    try:
        out = []
        for r in ((d.get("data") or {}).get("diff")) or []:
            name = str(r.get("f14", "") or "").strip()
            if not name:
                continue
            net = _f(r.get("f62"))          # 主力净额 (元)
            out.append({
                "name": name, "code": str(r.get("f12", "") or ""),
                "changePct": _f(r.get("f3")),
                "main_net": net,
                "main_net_yi": round(net / 1e8, 3) if net is not None else None,
                "net_pct": _f(r.get("f184")),   # 主力净额占成交额比 %
                "chg5d": _f(r.get("f128")), "chg20d": _f(r.get("f136")),
            })
        return out
    except Exception as e:
        print(f"[sector-mf] {e}", file=sys.stderr)
        return []


# ─── 9. 盘面解读 (规则引擎) ──────────────────────────────────────────
def build_commentary(zt_n, dt_n, zb_n, indices, zdfenbu, turnover, phase,
                     idx_kline, margin, ths_strong):
    c = {"summary": [], "breadth": {}, "volume": {}, "emotion": {},
         "margin_trend": {}, "hotspots": [], "risks": []}

    up, down, flat = zdfenbu.get("up", 0), zdfenbu.get("down", 0), zdfenbu.get("flat", 0)
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

    sh = next((i for i in indices if i.get("code") == "sh000001"), None)
    sz = next((i for i in indices if i.get("code") == "sz399001"), None)
    if sh:
        c["summary"].append(f"上证 {sh.get('price')} ({sh.get('chg_pct'):+.2f}%)")
    if sz:
        c["summary"].append(f"深成 {sz.get('price')} ({sz.get('chg_pct'):+.2f}%)")

    vols = [(k["date"], k["vol"]) for k in idx_kline if k.get("vol")]
    if len(vols) >= 6:
        today_vol = vols[-1][1]
        avg5 = sum(v for _, v in vols[-6:-1]) / 5
        ratio = today_vol / avg5 if avg5 else 0
        c["volume"] = {"today_vol_wan": round(today_vol / 1e4, 1),
                       "avg5_vol_wan": round(avg5 / 1e4, 1), "ratio": round(ratio, 2)}
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

    if len(margin) >= 2 and margin[0].get("rzye") and margin[1].get("rzye"):
        rz_chg = round((margin[0]["rzye"] - margin[1]["rzye"]) / margin[1]["rzye"] * 100, 3)
        c["margin_trend"] = {"date": str(margin[0].get("date", ""))[:10],
                             "rzye_yi": round(margin[0]["rzye"] / 1e8, 1),
                             "rzrqye_yi": round((margin[0].get("rzrqye") or 0) / 1e8, 1),
                             "rzye_chg_pct": rz_chg}
        if rz_chg > 0.2:
            c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 +{rz_chg}%, 杠杆资金加仓")
        elif rz_chg < -0.2:
            c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 {rz_chg}%, 杠杆资金撤退")
        else:
            c["summary"].append(f"两融融资余额 {c['margin_trend']['rzye_yi']} 亿, 环比 {rz_chg}%, 基本持稳")

    # ---- 6. 热点题材 (AIHOT 聚簇+热度算法, 替代原简单词频) ----
    # 旧逻辑: theme_count[kw] += 1 → 同一家源重复抓取会多算, 无时间衰减,
    #         一家媒体发十篇和十家媒体各发一篇无法区分。
    # 新逻辑: heat_algo.cluster_items + event_heat (独立来源只算一次,
    #         24h减半, 48h窗口, 同源多篇仅小幅加成)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from heat_algo import cluster_items, theme_heat
        items = [{"title": s.get("reason", ""), "source": "ths",
                  "ts": datetime.now(CST).isoformat(),
                  "codes": [s.get("code")], "themes": []}
                 for s in ths_strong if s.get("reason")]
        # 同花顺 reason 字段本身就是题材归因 (如 "钠电材料+硅碳负极+AI算力服务器"),
        # 拆成 themes 供 theme_heat 汇总
        for it, s in zip(items, [x for x in ths_strong if x.get("reason")]):
            it["themes"] = [kw.strip() for kw in str(s.get("reason", "")).replace("+", " ").split()
                            if len(kw.strip()) >= 2]
        events = cluster_items(items)
        th = theme_heat(events)
        c["hotspots"] = [
            {"theme": t, "count": d["events"], "heat": d["heat"],
             "n_sources": len(d["sources"]), "codes": sorted(d["codes"])[:5]}
            for t, d in sorted(th.items(), key=lambda x: -x[1]["heat"])[:8]
            if d["events"] >= 1
        ]
        c["hotspots_algo"] = "AIHOT聚簇+热度 (独立来源只算一次, 24h减半)"
        # 上游源单一说明: 强势股池目前只有同花顺一个源, 所以 n_sources 多为 1;
        # 独立来源去重要等多源 (新浪/东财/雪球) 接入后才显现差异。
        c["hotspots_caveat"] = ("题材热度基于同花顺强势股池(单源), n_sources 多为1; "
                                 "多源接入后『独立来源只算一次』的去重效果才会充分体现")
    except Exception as e:
        print(f"[heat_algo] {e}, 降级为词频", file=sys.stderr)
        theme_count = {}
        for s in ths_strong:
            for kw in str(s.get("reason", "")).replace("+", " ").split():
                kw = kw.strip()
                if len(kw) >= 2:
                    theme_count[kw] = theme_count.get(kw, 0) + 1
        c["hotspots"] = [{"theme": k, "count": v, "heat": float(v)}
                         for k, v in sorted(theme_count.items(), key=lambda x: -x[1])[:8] if v >= 2]
    if c["hotspots"]:
        c["summary"].append("资金聚焦题材: " + "、".join(
            f"{x['theme']}({x['count']}事件)" for x in c["hotspots"][:3]))

    if dt_n >= zt_n:
        c["risks"].append(f"跌停({dt_n})≥涨停({zt_n}), 短期风险释放中, 注意仓位")
    if seal < 50 and zt_n > 0:
        c["risks"].append(f"封板率仅 {seal}%, 打板资金接力意愿弱")
    if up_ratio < 30:
        c["risks"].append(f"上涨占比 {up_ratio}%, 普跌环境下逆势股易补跌")
    if phase.get("stage") in ("退潮", "冰点"):
        c["risks"].append(f"情绪周期处于「{phase.get('stage')}」, 降低交易频率")
    return c


# ─── 统一入口 ───────────────────────────────────────────────────────
def collect_all(date_str=None, indices=None, phase=None):
    """采集全部新增数据源, 返回 dict (可直接合并进 latest.json)。

    indices / phase 若为 None 则内部只做最小采集 (不含情绪周期完整字段)。
    """
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y%m%d")
    date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    print(f"[collect] date={date_str}", flush=True)
    zt = fetch_pool("ZT", date_str)
    dt = fetch_pool("DT", date_str)
    zb = fetch_pool("ZB", date_str)
    yzt = fetch_pool("ZT", (datetime.now(CST) - timedelta(days=1)).strftime("%Y%m%d"))
    zdfenbu = fetch_zdfenbu(date_str)
    turnover = fetch_turnover()
    margin = fetch_margin(30)
    idx_kline = fetch_index_kline("sh000001", 120)
    ths_strong = fetch_ths_strong_stocks(date_dash)
    # 龙虎榜/大宗交易: 用带回退的版本 (当日无数据时回退到最近交易日,
    # 并在 meta 标注实际数据日期), 避免凌晨/节假日整块为空
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from enrich_quotes import (fetch_with_fallback, _parse_lhb, _parse_blocktrade,
                                   enrich_strong)
        lhb, lhb_date, lhb_fb = fetch_with_fallback("RPT_DAILYBILLBOARD_DETAILS",
                                                    _parse_lhb, 5)
        blocktrade, bt_date, bt_fb = fetch_with_fallback("RPT_DATA_BLOCKTRADE",
                                                         _parse_blocktrade, 5)
        # 强势股补现价/涨跌/综合分 (同花顺接口本身不返回价格)
        ths_strong = enrich_strong(ths_strong)
        print(f"[enrich] 强势股 {len(ths_strong)} 只(含价格) | "
              f"龙虎榜 {len(lhb)} 条({lhb_date}{'回退' if lhb_fb else ''}) | "
              f"大宗 {len(blocktrade)} 笔({bt_date}{'回退' if bt_fb else ''})", flush=True)
        enrich_meta = {"lhb_date": lhb_date, "lhb_fallback": lhb_fb,
                       "blocktrade_date": bt_date, "blocktrade_fallback": bt_fb}
    except Exception as e:
        print(f"[enrich] 增强失败, 用原始数据: {e}", file=sys.stderr)
        lhb, blocktrade, enrich_meta = fetch_lhb(date_dash), fetch_blocktrade(date_dash), {}

    tx_rank = fetch_tx_sector_ranking(20)
    sector_mf = fetch_sector_moneyflow(20)

    if indices is None:
        indices = fetch_indices()
    if phase is None:
        phase = {"stage": "—", "score": None}

    # 涨跌停家数优先用涨跌分布口径 (全天准确, 含20cm/30cm涨停板)
    limit_up = zdfenbu.get("zt_like") or zt["count"]
    limit_down = zdfenbu.get("dt_like") or dt["count"]
    return {
        "market": {
            "indices": indices,
            "limit_up": limit_up, "limit_down": limit_down, "broken": zb["count"],
            "zt_rate": round(limit_up / max(1, limit_up + zb["count"]) * 100, 1),
            "yesterday_zt": yzt["count"],
            "advance": zdfenbu.get("up", 0), "decline": zdfenbu.get("down", 0),
            "flat": zdfenbu.get("flat", 0), "up_pct": zdfenbu.get("up_pct", 0),
            "turnover": turnover,
            "pool_zt_count": zt["count"], "pool_dt_count": dt["count"],
        },
        "commentary": build_commentary(limit_up, limit_down, zb["count"],
                                       indices, zdfenbu, turnover, phase,
                                       idx_kline, margin, ths_strong),
        "ths_strong": ths_strong[:40],
        "lhb": lhb[:20],
        "blocktrade": blocktrade[:20],
        "jingjia": build_jingjia(zt["pool"])[:20],
        "enrich_meta": enrich_meta,
        "margin": margin,
        "index_kline": idx_kline,
        "westock": {"sector_ranking": {"date": date_str, "data": tx_rank}},
        "sector_moneyflow": {"date": date_str, "data": sector_mf},
        "_pools_detail": {"zt": zt, "dt": dt, "zb": zb, "yzt": yzt},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出 JSON 路径")
    ap.add_argument("--merge", help="就地合并进已有 latest.json")
    ap.add_argument("--date", help="YYYYMMDD, 默认今天 CST")
    args = ap.parse_args()

    extras = collect_all(args.date)
    extras.pop("_pools_detail", None)   # 内部调试字段, 不进产物

    if args.merge:
        if not os.path.exists(args.merge):
            print(f"ERROR: {args.merge} not found", file=sys.stderr)
            return 1
        with open(args.merge, encoding="utf-8") as f:
            d = json.load(f)
        # 合并: market 整块替换 (含 indices), 其余模块增量更新
        mk = extras.pop("market")
        d["market"] = mk
        d.update(extras)
        with open(args.merge, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        print(f"[merge] updated {args.merge}")
        return 0

    out = args.out or "/tmp/market_extras.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(extras, f, ensure_ascii=False, indent=2)
    print(f"[out] {out}")
    m = extras["market"]
    print(f"  涨停 {m['limit_up']} | 跌停 {m['limit_down']} | 炸板 {m['broken']}")
    print(f"  上涨 {m['advance']} | 下跌 {m['decline']} | 占比 {m['up_pct']}%")
    print(f"  成交额 {m['turnover'].get('total_yi')} 亿")
    print(f"  两融 {len(extras['margin'])} 日 | K线 {len(extras['index_kline'])} 根 | "
          f"同花顺 {len(extras['ths_strong'])} | 龙虎榜 {len(extras['lhb'])} | "
          f"大宗 {len(extras['blocktrade'])} | 腾讯行业 {len(extras['westock']['sector_ranking']['data'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
