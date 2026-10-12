#!/usr/bin/env python3
"""
enrich_quotes.py — 给强势股/龙虎榜/大宗交易补全现价/涨跌/综合分 + 交易日回退

解决的三个问题 (用户反馈):
  1. 强势股只有 code/name/reason, 没有现价/涨跌/综合分
     → 同花顺接口本身不返回价格, 用腾讯批量行情补 (一次60个代码)
     → 综合分用多指标加权 (动量/位置/换手/市值/题材强度)
  2. 龙虎榜啥也没有
     → 当日无数据时回退到最近有数据的交易日, 并标注实际数据日期
  3. 大宗交易同样
     → 同上, 且补全溢价率与成交额

数据源:
  - 同花顺 zx.10jqka.com.cn 强势股题材归因 (零鉴权)
  - 腾讯 qt.gtimg.cn 批量行情 (零鉴权, 字段已实测核对)
  - 东财 datacenter 龙虎榜/大宗交易 (零鉴权, 容器内可达)

腾讯字段映射 (2026-10-13 用茅台/平安银行/比亚迪/中国平安交叉验证):
  p[3]=现价  p[32]=涨跌%  p[38]=换手率%  p[39]=PE(TTM)
  p[44]=总市值(亿)  p[45]=流通市值(亿)  p[46]=PB  p[30]=时间
  注意: 腾讯不提供「量比」, 一律默认 1.0
"""
import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}
UA_EM = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
         "Referer": "https://data.eastmoney.com/"}


# ─── 腾讯批量行情 ────────────────────────────────────────────────
def fetch_quotes(codes):
    """批量拉腾讯行情 → {code: {...}} (每批60个)"""
    out = {}
    codes = list(dict.fromkeys(codes))
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                          timeout=15).read()
            txt = body.decode("gbk", "replace")
        except Exception as e:
            print(f"[quotes] batch {i//60+1} 失败: {e}", file=sys.stderr)
            continue
        import re
        for m in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', txt):
            code, p = m.group(1), m.group(2).split("~")
            if len(p) < 40:
                continue
            try:
                out[code] = {
                    "code": code,
                    "name": p[1],
                    "price": float(p[3] or 0),
                    "chg_pct": float(p[32] or 0),
                    "turnover": float(p[38] or 0),   # 换手率%
                    "pe": float(p[39] or 0),         # PE(TTM)
                    "mktcap": float(p[44] or 0) * 1e8,   # 总市值(亿→元)
                    "floatcap": float(p[45] or 0) * 1e8,
                    "pb": float(p[46] or 0),
                    "vol_ratio": 1.0,                # 腾讯不提供量比
                    "time": p[30] or "",
                }
            except (ValueError, IndexError):
                continue
    return out


def load_cn_names():
    """本地腾讯快照: 代码(无前缀) → 中文名"""
    out = {}
    snaps = sorted(glob.glob("/root/workspace/stocks-data-live/data/market/snapshot_*.json"))
    if not snaps:
        return out
    try:
        d = json.load(open(snaps[-1], encoding="utf-8"))
        for k, v in d.items():
            bare = k[2:] if k[:2] in ("sh", "sz", "bj") else k
            out[bare] = v.get("name", "")
    except Exception:
        pass
    return out


def to_prefixed(code):
    c = str(code).strip().zfill(6)
    if c.startswith(("60", "68", "90", "11", "50", "51", "58")):
        return "sh" + c
    if c.startswith(("00", "30", "15", "16", "18", "12", "39")):
        return "sz" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sh" + c


# ─── 强势股增强 ──────────────────────────────────────────────────
def composite_score(q, reason=""):
    """强势股综合分 (0-100)。

    维度 (权重合计100):
      动量 30   当日涨跌幅 (0~5% → 0~30, 涨停按满分)
      换手 20   换手率 (2%~15% 线性, 过高降分)
      市值 15   市值越大越稳 (50亿~500亿线性)
      估值 15   PE 越低越高分 (亏损/负PE 得0)
      题材 20   题材归因关键词个数 (1~5个线性)
    """
    if not q:
        return None
    chg = q.get("chg_pct") or 0
    turn = q.get("turnover") or 0
    cap = q.get("mktcap") or 0
    pe = q.get("pe") or 0

    # 动量: 0~5% 线性, >5% (含涨停) 满分
    momentum = min(chg / 5.0, 1.0) * 30 if chg > 0 else 0.0
    # 换手: 2%~15% 线性最佳, >20% 过热降分
    if turn < 2:
        turnover_s = turn / 2 * 10
    elif turn <= 15:
        turnover_s = 10 + (turn - 2) / 13 * 10
    else:
        turnover_s = max(0, 20 - (turn - 15) * 1.0)
    # 市值: 50亿~500亿 线性
    cap_yi = cap / 1e8
    if cap_yi < 50:
        cap_s = cap_yi / 50 * 8
    elif cap_yi <= 500:
        cap_s = 8 + (cap_yi - 50) / 450 * 7
    else:
        cap_s = 15
    # 估值: PE 0~20 越高分, 负PE(亏损) 0分
    if pe <= 0:
        pe_s = 0.0
    elif pe <= 20:
        pe_s = 15
    elif pe <= 50:
        pe_s = 15 - (pe - 20) / 30 * 8
    else:
        pe_s = 15 - 7 - min((pe - 50) / 50 * 4, 8)
    # 题材: 关键词个数
    kws = [k for k in str(reason or "").replace("+", " ").split() if len(k.strip()) >= 2]
    theme_s = min(len(kws) / 5.0, 1.0) * 20

    total = momentum + turnover_s + cap_s + pe_s + theme_s
    return round(max(0.0, min(100.0, total)), 1)


def enrich_strong(ths_strong):
    """给同花顺强势股补现价/涨跌/综合分/市值/PE。"""
    if not ths_strong:
        return []
    cn = load_cn_names()
    prefixed = [to_prefixed(s.get("code")) for s in ths_strong if s.get("code")]
    quotes = fetch_quotes(prefixed)

    out = []
    for s in ths_strong:
        code = str(s.get("code", "")).strip()
        pf = to_prefixed(code)
        q = quotes.get(pf)
        rec = {
            "code": code,
            "name": s.get("name") or cn.get(code) or (q or {}).get("name") or code,
            "reason": s.get("reason", ""),
        }
        if q:
            rec.update({
                "price": q["price"],
                "chg_pct": q["chg_pct"],
                "turnover": q["turnover"],
                "mktcap": q["mktcap"],
                "pe": q["pe"],
                "pb": q["pb"],
                "score": composite_score(q, s.get("reason", "")),
                "in_zt": (q["chg_pct"] or 0) >= 9.5,
                "quote_time": q["time"],
            })
        else:
            rec.update({"price": None, "chg_pct": None, "score": None})
        out.append(rec)
    out.sort(key=lambda x: -(x.get("score") or 0))
    return out


# ─── 龙虎榜 / 大宗交易 (带回退) ─────────────────────────────────
def _dc_query(report, date_dash, page_size=30):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           f"?reportName={report}&columns=ALL&pageSize={page_size}&pageNumber=1"
           f"&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB"
           f"&filter=(TRADE_DATE%3D%27{date_dash}%27)")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA_EM),
                                      timeout=20).read()
        d = json.loads(body.decode("utf-8"))
        return (d.get("result") or {}).get("data") or []
    except Exception as e:
        print(f"[dc:{report} {date_dash}] {e}", file=sys.stderr)
        return []


def fetch_with_fallback(report, parser, days_back=5, page_size=30):
    """当日无数据则回退到最近有数据的交易日。

    返回 (rows, actual_date, is_fallback)
    """
    today = datetime.now(CST)
    for k in range(days_back):
        d = today - timedelta(days=k)
        ds = d.strftime("%Y-%m-%d")
        rows = _dc_query(report, ds, page_size)
        if rows:
            return parser(rows), ds, (k > 0)
    return [], None, False


def _parse_lhb(rows):
    """龙虎榜明细 → 按股票去重合并 (东财按营业部分条, 同一股票多条)。"""
    by_code = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE", "") or "")
        if not code:
            continue
        amt = r.get("BILLBOARD_DEAL_AMT") or 0
        if code in by_code:
            # 合并: 成交额累加, 保留第一条的说明
            by_code[code]["deal_amt"] = (by_code[code]["deal_amt"] or 0) + amt
            by_code[code]["seats"] = by_code[code].get("seats", 1) + 1
            continue
        by_code[code] = {
            "code": code,
            "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
            "close": r.get("CLOSE_PRICE"),
            "chg": r.get("CHANGE_RATE"),
            "deal_amt": amt,
            "expl": str(r.get("EXPLAIN", "") or "")[:60],
            "turnover_rate": r.get("TURNOVERRATE"),
            "d1": r.get("D1_CLOSE_ADJCHRATE"),
            "d2": r.get("D2_CLOSE_ADJCHRATE"),
            "d5": r.get("D5_CLOSE_ADJCHRATE"),
            "seats": 1,
        }
    out = list(by_code.values())
    out.sort(key=lambda x: -(x.get("deal_amt") or 0))
    return out


def _parse_blocktrade(rows):
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


# ─── main ─────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ths", help="同花顺强势股 JSON (ths_strong)")
    ap.add_argument("--out", required=True, help="输出 JSON")
    ap.add_argument("--days-back", type=int, default=5)
    args = ap.parse_args()

    now = datetime.now(CST)
    result = {"ts": now.isoformat(), "ths_strong": [], "lhb": [], "blocktrade": [],
              "meta": {}}

    # 1. 强势股增强
    ths = []
    if args.ths and os.path.exists(args.ths):
        ths = json.load(open(args.ths, encoding="utf-8"))
    if ths:
        print(f"强势股增强: {len(ths)} 只")
        result["ths_strong"] = enrich_strong(ths)
        n_price = sum(1 for x in result["ths_strong"] if x.get("price"))
        print(f"  补到价格 {n_price}/{len(result['ths_strong'])} 只")

    # 2. 龙虎榜 (带回退)
    print("龙虎榜 (带回退)...")
    lhb, lhb_date, lhb_fb = fetch_with_fallback("RPT_DAILYBILLBOARD_DETAILS",
                                                _parse_lhb, args.days_back)
    result["lhb"] = lhb
    result["meta"]["lhb_date"] = lhb_date
    result["meta"]["lhb_fallback"] = lhb_fb
    print(f"  {len(lhb)} 条, 数据日期 {lhb_date}{' (回退)' if lhb_fb else ''}")

    # 3. 大宗交易 (带回退)
    print("大宗交易 (带回退)...")
    bt, bt_date, bt_fb = fetch_with_fallback("RPT_DATA_BLOCKTRADE",
                                             _parse_blocktrade, args.days_back)
    result["blocktrade"] = bt
    result["meta"]["blocktrade_date"] = bt_date
    result["meta"]["blocktrade_fallback"] = bt_fb
    print(f"  {len(bt)} 笔, 数据日期 {bt_date}{' (回退)' if bt_fb else ''}")

    result["meta"].update({
        "source": "同花顺题材 + 腾讯行情 + 东财datacenter",
        "ts": now.isoformat(),
        "quote_fields": "腾讯: p3现价 p32涨跌% p38换手 p39PE p44总市值(亿) p46PB",
        "composite_score": "动量30+换手20+市值15+估值15+题材20, 满分100",
        "fallback": "当日无龙虎榜/大宗交易数据时, 回退到最近有数据的交易日, "
                    "并在 meta 标注 lhb_date/blocktrade_date/lhb_fallback",
    })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nDONE -> {args.out}")
    if result["ths_strong"][:3]:
        print("强势股 TOP3:")
        for x in result["ths_strong"][:3]:
            print(f"  {x['code']} {x['name']:8s} 价={x.get('price')} "
                  f"涨={x.get('chg_pct')}% 综合分={x.get('score')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
