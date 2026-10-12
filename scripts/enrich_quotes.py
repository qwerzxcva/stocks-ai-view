#!/usr/bin/env python3
"""
enrich_quotes.py — 给强势股/龙虎榜/大宗交易补全字段 (被 collect_market_extras 调用)

解决的问题 (2026-10-13 用户反馈):
  1. 强势股 tab 只有 code/name/reason, 没有现价/涨跌/综合分
     → 同花顺接口只返回这 3 个字段, 价格要自己从腾讯行情补
  2. 龙虎榜 tab 空
  3. 大宗交易 tab 空
     → 东财 datacenter 当日无数据时 (盘前/节假日) 直接返回空,
        没有回退到最近交易日

数据源:
  - 腾讯 qt.gtimg.cn (实时行情: 现价/涨跌/换手/市值/PE/PB) — 零鉴权, 容器内可达
  - 东财 datacenter-web (龙虎榜/大宗交易) — 零鉴权, 需日期回退

腾讯字段 (实测核对):
  p[3]=现价 p[4]=昨收 p[32]=涨跌% p[38]=换手% p[39]=PE p[44]=总市值(亿)
  p[45]=流通市值(亿) p[46]=PB p[30]=时间
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}

DC_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ─── 腾讯实时行情 ─────────────────────────────────────────────────
def _prefixed(code):
    """601127 → sh601127; 000001 → sz000001; 430047 → bj430047"""
    c = str(code).strip().zfill(6)
    if c.startswith(("60", "68", "90", "11", "13")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sz" + c


def fetch_quotes(codes, batch=60):
    """批量拉腾讯实时行情 → {code: {...}}"""
    out = {}
    pref = [_prefixed(c) for c in codes]
    for i in range(0, len(pref), batch):
        chunk = pref[i:i + batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                          timeout=15).read()
            # 腾讯返回 GBK, 需手动解码
            raw = body.decode("latin-1")
            try:
                text = raw.encode("latin-1").decode("gbk", "replace")
            except Exception:
                text = raw
            import re
            for m in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', text):
                sym, payload = m.group(1), m.group(2).split("~")
                if len(payload) < 40:
                    continue
                bare = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
                try:
                    out[bare] = {
                        "code": bare,
                        "name": payload[1],
                        "price": float(payload[3] or 0),
                        "prev_close": float(payload[4] or 0),
                        "open": float(payload[5] or 0),
                        "high": float(payload[33] or 0),
                        "low": float(payload[34] or 0),
                        "chg_pct": float(payload[32] or 0),
                        "turnover": float(payload[38] or 0),
                        "vol_ratio": 1.0,          # 量比无独立字段
                        "mktcap": (float(payload[44] or 0)) * 1e8,   # 亿元→元
                        "floatcap": (float(payload[45] or 0)) * 1e8,
                        "pe": float(payload[39] or 0),
                        "pb": float(payload[46] or 0),
                        "time": payload[30] or "",
                    }
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            print(f"[quotes] batch {i//batch+1} failed: {e}", file=sys.stderr)
        time.sleep(0.15)
    return out


# ─── 综合分 (强势股专用) ──────────────────────────────────────────
def strong_score(q, reason=""):
    """强势股综合分 0-100。

    维度 (权重):
      涨跌幅    30  当日涨幅 (涨停附近最高分, 但排除已涨停10%的不再额外加分)
      换手率    20  适度换手最佳 (3%~15%)
      市值      15  中小盘 (50~300亿) 更受短线资金青睐
      PE        10  有正收益且不过高
      题材热度  25  同花顺 reason 里的题材关键词数量
    """
    s = 0.0
    chg = q.get("chg_pct") or 0
    turn = q.get("turnover") or 0
    cap = q.get("mktcap") or 0
    pe = q.get("pe")

    # 涨跌幅: 0~9% 线性, ≥9% 满分
    s += 30 * min(abs(chg) / 9.0, 1.0)
    # 换手: 3~15% 最佳
    if turn <= 0:
        pass
    elif turn < 3:
        s += 20 * (turn / 3)
    elif turn <= 15:
        s += 20
    else:
        s += 20 * max(0.4, 1 - (turn - 15) / 30)
    # 市值: 50~300亿 最佳
    cap_yi = cap / 1e8
    if cap_yi <= 0:
        pass
    elif cap_yi < 50:
        s += 15 * (cap_yi / 50)
    elif cap_yi <= 300:
        s += 15
    else:
        s += 15 * max(0.3, 1 - (cap_yi - 300) / 2000)
    # PE: 有正收益 +10, 负/过高 0
    if pe and 0 < pe <= 60:
        s += 10
    elif pe and pe <= 0:
        s += 2
    # 题材: reason 里关键词数 (每个+5, 上限25)
    kws = [k.strip() for k in str(reason).replace("+", " ").split() if len(k.strip()) >= 2]
    s += min(25, 5 * len(kws))

    return round(min(100.0, s), 1)


def enrich_strong(ths_strong):
    """给同花顺强势股补 现价/涨跌/换手/市值/PE/PB + 综合分 + 排名。"""
    if not ths_strong:
        return ths_strong
    codes = [s.get("code") for s in ths_strong if s.get("code")]
    quotes = fetch_quotes(codes)
    out = []
    for s in ths_strong:
        code = str(s.get("code", "")).strip()
        q = quotes.get(code)
        rec = {"code": code, "name": s.get("name", ""), "reason": s.get("reason", "")}
        if q:
            rec.update({
                "price": q["price"], "chg_pct": q["chg_pct"],
                "turnover": q["turnover"], "vol_ratio": q["vol_ratio"],
                "mktcap": q["mktcap"], "pe": q["pe"], "pb": q["pb"],
                "high": q["high"], "low": q["low"], "open": q["open"],
                "quote_time": q["time"],
            })
            rec["score"] = strong_score(q, s.get("reason", ""))
        else:
            rec.update({"price": None, "chg_pct": None, "score": None})
        out.append(rec)
    # 按综合分降序, 加 rank
    out.sort(key=lambda x: -(x.get("score") or 0))
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


# ─── 东财 datacenter (带日期回退) ─────────────────────────────────
def _dc_query(report, sort_col, date_dash, page_size=30):
    url = (f"{DC_BASE}?reportName={report}&columns=ALL&pageSize={page_size}"
           f"&pageNumber=1&sortColumns={sort_col}&sortTypes=-1"
           f"&source=WEB&client=WEB&filter=(TRADE_DATE%3D%27{date_dash}%27)")
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                      timeout=20).read()
        d = json.loads(body.decode("utf-8"))
        return (d.get("result") or {}).get("data") or []
    except Exception as e:
        print(f"[dc:{report} {date_dash}] {e}", file=sys.stderr)
        return []


def fetch_with_fallback(report, parser, max_back=7):
    """当日无数据时逐日回退 (最多 max_back 天)。

    返回 (rows, actual_date, used_fallback)
    龙虎榜/大宗交易都是收盘后才出, 盘前/凌晨/节假日必须回退,
    否则整块为空 (用户反馈"龙虎榜啥也没有")。
    """
    today = datetime.now(CST)
    for back in range(max_back):
        d = today - timedelta(days=back)
        # 跳过周末
        if d.weekday() >= 5 and back == 0:
            continue
        ds = d.strftime("%Y-%m-%d")
        raw = _dc_query(report, "TRADE_DATE", ds)
        if raw:
            rows = [parser(r) for r in raw]
            rows = [r for r in rows if r.get("code")]
            # 去重: 东财龙虎榜同一股票可能多行 (不同上榜原因),
            # 按 (code, expl) 保留首条, 避免前端表格出现重复行
            seen = set()
            uniq = []
            for r in rows:
                key = (r.get("code"), r.get("expl", "")[:40])
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(r)
            if uniq:
                return uniq, ds, (back > 0)
    return [], today.strftime("%Y-%m-%d"), False


def _parse_lhb(r):
    amt = r.get("BILLBOARD_DEAL_AMT")
    return {
        "code": str(r.get("SECURITY_CODE", "") or ""),
        "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
        "close": r.get("CLOSE_PRICE"),
        "chg": r.get("CHANGE_RATE"),
        "deal_amt": amt,
        "deal_amt_yi": round(amt / 1e8, 2) if isinstance(amt, (int, float)) else None,
        "expl": str(r.get("EXPLAIN", "") or "")[:80],
        "trade_date": str(r.get("TRADE_DATE", ""))[:10],
    }


def _parse_blocktrade(r):
    amt = r.get("DEAL_AMT")
    prem = r.get("PREMIUM_RATIO")
    return {
        "code": str(r.get("SECURITY_CODE", "") or ""),
        "name": str(r.get("SECURITY_NAME_ABBR", "") or ""),
        "price": r.get("DEAL_PRICE"),
        "premium": prem,
        "premium_pct": round(prem * 100, 2) if isinstance(prem, (int, float)) else None,
        "vol": r.get("DEAL_VOLUME"),
        "amt": amt,
        "amt_yi": round(amt / 1e8, 3) if isinstance(amt, (int, float)) else None,
        "trade_date": str(r.get("TRADE_DATE", ""))[:10],
    }


# ─── CLI (独立调试用) ────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--strong", action="store_true", help="测强势股增强")
    ap.add_argument("--lhb", action="store_true", help="测龙虎榜")
    ap.add_argument("--bt", action="store_true", help="测大宗交易")
    args = ap.parse_args()

    if args.strong:
        sys.path.insert(0, "/root/workspace/stocks-ai-view/scripts")
        from collect_market_extras import fetch_ths_strong_stocks
        ths = fetch_ths_strong_stocks(datetime.now(CST).strftime("%Y-%m-%d"))
        en = enrich_strong(ths)
        print(f"强势股 {len(en)} 只 (含价格与综合分):")
        for r in en[:10]:
            print(f"  #{r['rank']:2d} {r['code']} {r['name']:8s} "
                  f"价={r.get('price')} 涨={r.get('chg_pct')}% "
                  f"换手={r.get('turnover')}% 市值={((r.get('mktcap') or 0)/1e8):.0f}亿 "
                  f"PE={r.get('pe')} 综合分={r.get('score')}")

    if args.lhb:
        rows, ds, fb = fetch_with_fallback("RPT_DAILYBILLBOARD_DETAILS", _parse_lhb)
        print(f"\n龙虎榜 {len(rows)} 条 (数据日期 {ds}{' 回退' if fb else ''}):")
        for r in rows[:8]:
            print(f"  {r['code']} {r['name']:8s} 收={r['close']} 涨={r['chg']}% "
                  f"成交={r['deal_amt_yi']}亿 {r['expl'][:30]}")

    if args.bt:
        rows, ds, fb = fetch_with_fallback("RPT_DATA_BLOCKTRADE", _parse_blocktrade)
        print(f"\n大宗交易 {len(rows)} 笔 (数据日期 {ds}{' 回退' if fb else ''}):")
        for r in rows[:8]:
            print(f"  {r['code']} {r['name']:8s} 价={r['price']} "
                  f"溢价={r['premium_pct']}% 成交={r['amt_yi']}亿")


if __name__ == "__main__":
    main()
