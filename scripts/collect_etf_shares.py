#!/usr/bin/env python3
"""collect_etf_shares.py — 宽基 ETF 份额跟踪（聪明钱 / 国家队加减仓）

数据口径（2026-10-27 重定，勿回退）：
- 份额**真值**取自交易所官方日度披露，单位统一换算成亿份：
    上交所 «ETF规模»  query.sse.com.cn commonQuery
        sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L，参数 STAT_DATE=YYYY-MM-DD
        → 每个交易日一张全表，字段 TOT_VOL = 总份额（万份）
    深交所 «基金规模»  www.szse.cn ShowReport  CATALOGID=scsj_fund_jjgm
        txtDm=代码 & jjlb=ETF & txtStart/txtEnd 区间（必须带 jjlb=ETF，否则 0 条）
        → current_size = 份额（万份）
  交叉核对：510300 交易所 2026-09-01 = 230.0 亿份、2026-10-23 = 352.4 亿份，
  与基金季报「期末总份额」2026-09-30 = 241.20 亿份同一口径、同一量级。
- 交易所侧数据可回溯约一年（实测 2025-09 仍有数据），因此可以一次性回填历史，
  不必像旧实现那样"从今天开始逐日攒 20 天才有信号"。
- 旧的「份额 = 成交量/换手率」估算保留在 live_series 作对照，**仅量级参考**：
  它其实相当准（2026-10-23 估算 352.31 亿份 vs 交易所 352.43 亿份），但依赖换手率
  口径、且无法回填历史，所以主序列（series）与 smart_money 只用交易所真值。
- 交易所 T 日晚间更新 T 日规模，T+1 早间为准；因此每日盘后跑一次即可。

用法：
  python3 scripts/collect_etf_shares.py                  # 日更：抓当日 + 深市近 10 日补漏
  python3 scripts/collect_etf_shares.py --backfill-days 250   # 回填最近 250 个工作日
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUT = os.path.join(DATA_DIR, "etf_shares.json")

CST = timezone(timedelta(hours=8))

# code -> (东财 secid, 名称, 交易所)
BASKET = {
    "510300": ("1.510300", "沪深300ETF", "sse"),
    "510500": ("1.510500", "中证500ETF", "sse"),
    "510050": ("1.510050", "上证50ETF", "sse"),
    "512100": ("1.512100", "中证1000ETF", "sse"),
    "588000": ("1.588000", "科创50ETF", "sse"),
    "159915": ("0.159915", "创业板ETF", "szse"),
    "159919": ("0.159919", "沪深300ETF深", "szse"),
}

SSE_URL = ("http://query.sse.com.cn/commonQuery.do?isPagination=true"
           "&pageHelp.pageSize=1000&pageHelp.pageNo=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE={day}")
SSE_REFERER = "http://www.sse.com.cn/"
SZSE_URL = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            "&CATALOGID=scsj_fund_jjgm&TABKEY=tab1&jjlb=ETF"
            "&txtDm={code}&txtStart={start}&txtEnd={end}&PAGENO={page}")
SZSE_REFERER = "https://www.szse.cn/market/fund/volume/etf/index.html"

# 出仓位信号至少需要的历史天数（不足就只展示、不调仓位）
MIN_HISTORY_DAYS = 20
# 百分位窗口
WINDOW_DAYS = 250


def curl_text(url, referer, retries=3):
    """所有取数走 curl 子进程：容器内 python urllib 会被 WAF 掐连接。"""
    last = None
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "30",
                 "-H", "Referer: " + referer,
                 "-H", "Accept: */*",
                 "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", url],
                capture_output=True, timeout=45)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError("curl rc=%s" % r.returncode)
            return r.stdout.decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(3 + attempt * 4)
    raise last


def fetch_sse_day(day):
    """上交所某日全表 → (实际数据日期, {code: 亿份})。

    STAT_DATE 传非交易日时接口会回落到最近交易日，所以用返回行里的 STAT_DATE
    当真实日期，避免把周末/节假日写成一条假记录。
    """
    txt = curl_text(SSE_URL.format(day=day), SSE_REFERER)
    data = json.loads(txt)
    rows = (data.get("pageHelp") or {}).get("data") or []
    actual = None
    out = {}
    for r in rows:
        code = str(r.get("SEC_CODE") or "").strip()
        if not code:
            continue
        actual = actual or str(r.get("STAT_DATE") or "").strip()
        try:
            v = float(r.get("TOT_VOL") or 0)
        except ValueError:
            continue
        if v > 0:
            out[code] = round(v / 1e4, 2)   # 万份 → 亿份
    return actual, out


def fetch_szse_series(code, start, end):
    """深交所单只 ETF 区间份额 → [(date, 亿份)]。

    实测（2026-10-27）：一次查询跨度过大时接口直接返回非 JSON（约一年必失败、
    半年可用），所以按 80 天分段请求再合并；段内仍需分页（每页 20 条）。
    """
    out = []
    try:
        cur = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return out
    while cur <= last:
        seg_end = min(cur + timedelta(days=80), last)
        out.extend(_fetch_szse_segment(code, cur.isoformat(), seg_end.isoformat()))
        cur = seg_end + timedelta(days=1)
        time.sleep(0.8)
    return out


def _fetch_szse_segment(code, start, end):
    """单段（<=80 天）查询，带分页。必须带 jjlb=ETF，否则 0 条。"""
    series = []
    page = 1
    while page <= 20:                        # 防御性上限
        txt = curl_text(SZSE_URL.format(code=code, start=start, end=end, page=page),
                        SZSE_REFERER)
        blocks = json.loads(txt)
        if not isinstance(blocks, list) or not blocks:
            break
        blk = blocks[0]
        rows = blk.get("data") or []
        for r in rows:
            d = str(r.get("size_date") or "").strip()
            if str(r.get("fund_code") or "").strip() != code:
                continue
            try:
                v = float(str(r.get("current_size") or "0").replace(",", ""))
            except ValueError:
                continue
            if d and v > 0:
                series.append((d, round(v / 1e4, 2)))
        pagecount = int((blk.get("metadata") or {}).get("pagecount") or 1)
        if page >= pagecount:
            break
        page += 1
        time.sleep(0.6)
    return series


def merge_points(store, code, points, source):
    for d, v in points:
        store.setdefault(code, {})[d] = {"date": d, "shares_yi": v, "source": source}


def pct(a, b):
    return round((a - b) / b * 100, 2) if b else None


def weekdays_back(days):
    """往前数 days 个工作日（含今天），返回升序 YYYY-MM-DD。"""
    out = []
    cursor = datetime.now(CST).date()
    while len(out) < days:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def build_smart_money(basket):
    """篮子份额在自身窗口区间里的百分位（近似公众号「仓位%」口径）。"""
    positions = []
    window_used = 0
    for code, d in basket.items():
        s = [x["shares_yi"] for x in d["series"][-WINDOW_DAYS:]]
        if len(s) < MIN_HISTORY_DAYS:
            continue
        lo, hi = min(s), max(s)
        if hi <= lo:
            continue
        positions.append((s[-1] - lo) / (hi - lo))
        window_used = max(window_used, len(s))
    if not positions:
        return None
    return {
        "position_pct": round(sum(positions) / len(positions) * 100, 1),
        "etfs_used": len(positions),
        "window_days": window_used,
        "method": "交易所日度总份额在自身窗口区间的百分位（0=区间最低，100=区间最高）",
        "source": "sse/szse official daily share disclosure",
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    backfill_days = 0
    if "--backfill-days" in argv:
        i = argv.index("--backfill-days")
        backfill_days = int(argv[i + 1]) if i + 1 < len(argv) else 120

    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8")).get("basket") or {}
        except Exception:
            old = {}

    # 已有真值序列先装进来（按日期去重，新值覆盖）。
    # 只接受交易所真值来源：旧的 series 是「成交量/换手率」估算底稿，量级虽准但口径不同，
    # 一旦混进来就会让 20 日变动算出 11257% 这种东西（2026-10-27 实测踩到）。
    TRUSTED_SOURCES = ("sse_etf_vol", "szse_fund_size")
    store = {}
    for code, d in old.items():
        for point in d.get("series") or []:
            if point.get("source") not in TRUSTED_SOURCES:
                continue
            dt = point.get("date")
            v = point.get("shares_yi")
            if dt and isinstance(v, (int, float)):
                store.setdefault(code, {})[dt] = {
                    "date": dt, "shares_yi": v,
                    "source": point.get("source"),
                }

    today = datetime.now(CST).date()
    if backfill_days > 0:
        days = weekdays_back(backfill_days)
        print("[etf] backfill %d weekdays: %s .. %s" % (len(days), days[0], days[-1]))
        for day in days:
            try:
                actual, shares = fetch_sse_day(day)
                if actual and shares:
                    picks = {c: v for c, v in shares.items() if BASKET.get(c, ("", "", ""))[2] == "sse"}
                    for code, v in picks.items():
                        merge_points(store, code, [(actual, v)], "sse_etf_vol")
                    print("[etf] sse %s -> %d codes" % (actual, len(picks)))
                else:
                    print("[etf] sse %s -> no data" % day)
            except Exception as e:
                print("[etf] sse %s failed: %s" % (day, e), file=sys.stderr)
            time.sleep(0.5)
        sz_start = days[0]
    else:
        # 日更：当天 + 深市近 10 个自然日补漏
        try:
            actual, shares = fetch_sse_day(today.isoformat())
            picks = {c: v for c, v in (shares or {}).items() if BASKET.get(c, ("", "", ""))[2] == "sse"}
            for code, v in picks.items():
                merge_points(store, code, [(actual, v)], "sse_etf_vol")
            print("[etf] sse %s -> %d codes" % (actual, len(picks)))
        except Exception as e:
            print("[etf] sse today failed: %s" % e, file=sys.stderr)
        sz_start = (today - timedelta(days=12)).isoformat()

    sz_end = today.isoformat()
    for code, (_secid, _name, market) in BASKET.items():
        if market != "szse":
            continue
        try:
            series = fetch_szse_series(code, sz_start, sz_end)
            merge_points(store, code, series, "szse_fund_size")
            print("[etf] szse %s -> %d days" % (code, len(series)))
        except Exception as e:
            print("[etf] szse %s failed: %s" % (code, e), file=sys.stderr)

    basket = {}
    for code, (secid, name, market) in BASKET.items():
        points = sorted(store.get(code, {}).values(), key=lambda x: x["date"])
        if not points:
            # 没有真值也别把篮子写空：保留旧数据，方便下次补齐
            prev = old.get(code) or {}
            if prev:
                basket[code] = prev
            continue
        vals = [p["shares_yi"] for p in points]
        latest = points[-1]
        d = {
            "name": name,
            "secid": secid,
            "market": market,
            "n": len(points),
            "series": points,
            "latest": {
                "date": latest["date"],
                "shares_yi": latest["shares_yi"],
                "d1_chg_pct": pct(vals[-1], vals[-2]) if len(vals) > 1 else None,
                "d5_chg_pct": pct(vals[-1], vals[-6]) if len(vals) > 5 else None,
                "d20_chg_pct": pct(vals[-1], vals[-21]) if len(vals) > 20 else None,
            },
            "live_series": (old.get(code) or {}).get("live_series") or [],
            "live_series_note": "成交量/换手率估算口径，仅量级对照，不参与信号",
        }
        basket[code] = d
        print("%s %s: %d 天真值 | 最新 %s %s 亿份 | 日 %s%% 5日 %s%% 20日 %s%%"
              % (code, name, len(points), latest["date"], latest["shares_yi"],
                 d["latest"]["d1_chg_pct"], d["latest"]["d5_chg_pct"], d["latest"]["d20_chg_pct"]))

    smart = build_smart_money(basket)

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "collect_etf_shares.py — 份额= 上交所 ETF规模(TOT_VOL)+深交所 基金规模(current_size)，单位亿份",
        "unit": "亿份",
        "min_history_days": MIN_HISTORY_DAYS,
        "window_days": WINDOW_DAYS,
        "smart_money": smart,
        "basket": basket,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print("\n-> %s (%d B)" % (OUT, os.path.getsize(OUT)))
    if smart:
        print("聪明钱仓位百分位: %s%%（篮子 %d 只，窗口 %d 天）"
              % (smart["position_pct"], smart["etfs_used"], smart["window_days"]))
    else:
        print("聪明钱仓位: 历史真值天数不足 %d 天，先跑 --backfill-days" % MIN_HISTORY_DAYS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
