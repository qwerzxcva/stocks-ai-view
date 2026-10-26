#!/usr/bin/env python3
"""collect_etf_shares.py — 宽基 ETF 份额跟踪（聪明钱 / 国家队加减仓）

口径（2026-10-27 定，勿回退）：

1) 份额真值 = 交易所官方日度披露，单位统一换成「亿份」
   - 上交所 «ETF规模» query.sse.com.cn commonQuery
     sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L, STAT_DATE=YYYY-MM-DD
     字段 TOT_VOL（万份）；一次请求返回当天全部沪市 ETF（约 920 只）。
   - 深交所 «基金规模» www.szse.cn ShowReport CATALOGID=scsj_fund_jjgm
     txtDm=代码 & jjlb=ETF（不带 jjlb 会返回 0 条）& txtStart/txtEnd
     字段 current_size（万份）；区间跨太大时返回非 JSON，按 80 天分段。
   交叉核对：510300 交易所 2026-09-01=230.0 亿份、2026-10-23=352.4 亿份，
   季报 2026-09-30 期末 241.20 亿份，同一口径。

2) 信号按**指数**聚合，不是看单只代表 ETF
   同一指数下常有几十只 ETF（沪市沪深300 系列 38 只）。只看一只会把
   「同类 ETF 之间的份额搬家」误读成净申购，也会严重低估规模：
   2026-10-20→10-26 沪深300 系列合计 +157.77 亿份，而 510300 单只 +60.46 亿份，
   只占 38%（实测）。所以每个指数取「该指数全部 ETF 份额合计」再算百分位。

3) 成员映射（data/etf_index_map.json，--refresh-map 重建）
   - 深市：深交所 ETF 列表 CATALOGID=1945 自带「拟合指数」。
     注意深交所用自己的指数码：沪深300=399300、中证500=399905，需归一。
   - 沪市：上交所接口不给跟踪指数，用东方财富基金档案页
     fundf10.eastmoney.com/jbgk_<code>.html 的「跟踪标的」精确匹配指数全名。
     一次建好缓存入库，日常不再请求（新增 ETF 需刷新）。

4) 旧的「份额=成交量/换手率」估算只作 live_series 对照，绝不与真值混算
   （混算会得出 11257% 的 20 日变动）。估算本身是准的：2026-10-23 估算 352.31
   亿份 vs 交易所 352.43 亿份，只是无法回填历史。

用法：
  python3 scripts/collect_etf_shares.py                      # 日更（抓当天）
  python3 scripts/collect_etf_shares.py --backfill-days 250   # 回填最近 N 个工作日
  python3 scripts/collect_etf_shares.py --refresh-map         # 重建 ETF→指数 映射
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUT = os.path.join(DATA_DIR, "etf_shares.json")
MAP_FILE = os.path.join(DATA_DIR, "etf_index_map.json")

CST = timezone(timedelta(hours=8))

# 指数代码 -> (名称, 沪市「跟踪标的」精确值集合, 深交所拟合指数代码集合)
# 深交所对同一指数会同时用中证码和深证码（沪深300 既有 000300 也有 399300），两边都要收。
INDEX_DEFS = {
    "000300": ("沪深300", {"沪深300指数"}, {"000300", "399300"}),
    "000905": ("中证500", {"中证500指数"}, {"000905", "399905"}),
    "000016": ("上证50", {"上证50指数"}, set()),
    "000852": ("中证1000", {"中证1000指数"}, {"000852"}),
    "000688": ("科创50", {"上证科创板50成份指数", "科创板50成份指数"}, set()),
    "399006": ("创业板指", {"创业板指数"}, {"399006"}),
}

# 单只代表 ETF（保留旧口径便于对照；信号不再依赖它）
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
           "&pageHelp.pageSize=2000&pageHelp.pageNo=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE={day}")
SSE_REFERER = "http://www.sse.com.cn/"
SZSE_URL = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            "&CATALOGID=scsj_fund_jjgm&TABKEY=tab1&jjlb=ETF"
            "&txtDm={code}&txtStart={start}&txtEnd={end}&PAGENO={page}")
SZSE_REFERER = "https://www.szse.cn/market/fund/volume/etf/index.html"
SZSE_LIST_URL = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
                 "&CATALOGID=1945&TABKEY=tab1&PAGENO={page}")
SZSE_LIST_REFERER = "https://www.szse.cn/www/market/product/list/etfList/"
EM_REFERER = "https://fundf10.eastmoney.com/"

MIN_HISTORY_DAYS = 20
WINDOW_DAYS = 250


def curl_text(url, referer, retries=3, timeout=45):
    """所有取数走 curl 子进程：容器内 python urllib 会被 WAF 掐连接。"""
    last = None
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "30",
                 "-H", "Referer: " + referer,
                 "-H", "Accept: */*",
                 "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", url],
                capture_output=True, timeout=timeout)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError("curl rc=%s" % r.returncode)
            return r.stdout.decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(3 + attempt * 4)
    raise last


def strip_tags(h):
    return re.sub(r"<[^>]+>", "", str(h)).strip()


# ---------------------------------------------------------------- 份额取数

def fetch_sse_day(day):
    """沪市某日全表 → (实际数据日期, {code: 亿份})。非交易日会回落到最近交易日。"""
    data = json.loads(curl_text(SSE_URL.format(day=day), SSE_REFERER))
    rows = (data.get("pageHelp") or {}).get("data") or []
    actual, out = None, {}
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
            out[code] = round(v / 1e4, 4)      # 万份 → 亿份
    return actual, out


def _fetch_szse_segment(code, start, end):
    series, page = [], 1
    while page <= 20:
        txt = curl_text(SZSE_URL.format(code=code, start=start, end=end, page=page),
                        SZSE_REFERER)
        blocks = json.loads(txt)
        if not isinstance(blocks, list) or not blocks:
            break
        blk = blocks[0]
        for r in blk.get("data") or []:
            d = str(r.get("size_date") or "").strip()
            if str(r.get("fund_code") or "").strip() != code:
                continue
            try:
                v = float(str(r.get("current_size") or "0").replace(",", ""))
            except ValueError:
                continue
            if d and v > 0:
                series.append((d, round(v / 1e4, 4)))
        pagecount = int((blk.get("metadata") or {}).get("pagecount") or 1)
        if page >= pagecount:
            break
        page += 1
        time.sleep(0.5)
    return series


def fetch_szse_series(code, start, end):
    """深市单只区间份额。长区间接口返非 JSON，按 80 天分段。"""
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
        time.sleep(0.6)
    return out


# ---------------------------------------------------------------- 成员映射

def _szse_all_etfs():
    """深交所 ETF 列表全量 → {code: {name, index}}（含拟合指数，权威）。"""
    out, page = {}, 1
    while page <= 120:
        try:
            blocks = json.loads(curl_text(SZSE_LIST_URL.format(page=page), SZSE_LIST_REFERER))
        except Exception as e:
            print("[etf] szse list page %d failed: %s" % (page, e), file=sys.stderr)
            break
        if not isinstance(blocks, list) or not blocks:
            break
        blk = blocks[0]
        rows = blk.get("data") or []
        if not rows:
            break
        for r in rows:
            code = strip_tags(r.get("sys_key"))
            raw = strip_tags(r.get("nhzs"))
            idx = raw.split()[0] if raw else ""
            if code.isdigit() and idx:
                out[code] = {"name": strip_tags(r.get("kzjcurl")), "index": idx}
        pagecount = int((blk.get("metadata") or {}).get("pagecount") or 1)
        if page >= pagecount:
            break
        page += 1
        time.sleep(0.3)
    return out


def _sse_tracking_target(code):
    """东财基金档案「跟踪标的」→ 指数全名（沪市唯一可批量拿到的权威来源）。"""
    url = "https://fundf10.eastmoney.com/jbgk_%s.html" % code
    for _ in range(3):
        try:
            t = curl_text(url, EM_REFERER)
        except Exception:
            return None
        if "跟踪标的" in t:
            i = t.find("跟踪标的")
            m = re.search(r"<td[^>]*>(.*?)</td>", t[i:i + 400], re.S)
            if m:
                return strip_tags(m.group(1))
        time.sleep(2)
    return None


def build_index_map():
    """重建 ETF→指数 成员映射并落盘。较慢（沪市要逐只查跟踪标的），只在 --refresh-map 跑。"""
    _, sse_all = fetch_sse_day(datetime.now(CST).date().isoformat())
    all_targets = set()
    for _code, (_n, targets, _sz) in INDEX_DEFS.items():
        all_targets |= targets

    members = {ic: {"name": v[0], "sse": [], "szse": []} for ic, v in INDEX_DEFS.items()}
    sse_codes = sorted(sse_all)

    print("[map] 沪市 %d 只逐个核对跟踪标的（约 %d 分钟）..."
          % (len(sse_codes), max(1, len(sse_codes) // 100)), flush=True)
    done = 0
    for code in sse_codes:
        target = _sse_tracking_target(code)
        done += 1
        if done % 50 == 0:
            print("[map]   %d/%d" % (done, len(sse_codes)), flush=True)
        if target and target in all_targets:
            for ic, (_n, targets, _sz) in INDEX_DEFS.items():
                if target in targets:
                    members[ic]["sse"].append(code)
                    break
        time.sleep(0.35)

    print("[map] 深市 ETF 列表...", flush=True)
    for code, info in _szse_all_etfs().items():
        for ic, v in INDEX_DEFS.items():
            if info["index"] in v[2]:
                members[ic]["szse"].append(code)
                break

    now = datetime.now(CST).isoformat(timespec="seconds")
    payload = {"built": now, "indexes": members,
               "note": "members.sse 依据东财跟踪标的精确匹配；members.szse 依据深交所拟合指数"}
    json.dump(payload, open(MAP_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for ic, m in members.items():
        print("[map] %s %s: 沪 %d 只 + 深 %d 只" % (ic, m["name"], len(m["sse"]), len(m["szse"])))
    return payload


def load_index_map():
    if os.path.exists(MAP_FILE):
        try:
            d = json.load(open(MAP_FILE, encoding="utf-8"))
            if d.get("indexes"):
                return d
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- 主流程

def pct(a, b):
    return round((a - b) / b * 100, 2) if b else None


def weekdays_back(days):
    out, cursor = [], datetime.now(CST).date()
    while len(out) < days:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def series_sum(store, dates, codes):
    """按日期把若干只 ETF 份额求和；当日缺任何一只就跳过（不缺数据才可信）。"""
    out = {}
    for d in dates:
        vals = [store.get(c, {}).get(d) for c in codes]
        if codes and all(v is not None for v in vals):
            out[d] = round(sum(vals), 2)
    return out


def build_smart_money(indexes):
    positions, window_used = [], 0
    for ic, d in indexes.items():
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
        "index_used": len(positions),
        "window_days": window_used,
        "method": "各宽基指数下全部 ETF 份额合计，在自身窗口区间的百分位（0=最低，100=最高）",
        "source": "sse/szse official daily share disclosure, aggregated per index",
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    backfill_days = 0
    if "--backfill-days" in argv:
        i = argv.index("--backfill-days")
        backfill_days = int(argv[i + 1]) if i + 1 < len(argv) else 120

    imap = build_index_map() if ("--refresh-map" in argv or load_index_map() is None) \
        else load_index_map()
    members = imap["indexes"]

    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = {}

    store = {}          # {code: {date: 亿份}} 单只累计点
    for code, d in (old.get("basket") or {}).items():
        for p in d.get("series") or []:
            if p.get("source") in ("sse_etf_vol", "szse_fund_size"):
                store.setdefault(code, {})[p["date"]] = p["shares_yi"]

    idx_hist = {}       # {index_code: {date: 亿份}} 指数合计
    for ic, d in (old.get("indexes") or {}).items():
        for p in d.get("series") or []:
            idx_hist.setdefault(ic, {})[p["date"]] = p["shares_yi"]

    sse_all_codes = sorted({c for m in members.values() for c in m["sse"]})
    szse_all_codes = sorted({c for m in members.values() for c in m["szse"]})
    today = datetime.now(CST).date()

    if backfill_days > 0:
        days = weekdays_back(backfill_days)
        missing = [d for d in days if d not in idx_hist.get("000300", {})]
        print("[etf] backfill %s..%s: %d weekdays, %d missing"
              % (days[0], days[-1], len(days), len(missing)))
        sse_days, sz_start = missing, days[0]
    else:
        sse_days = [today.isoformat()]
        sz_start = (today - timedelta(days=14)).isoformat()

    for day in sse_days:
        try:
            actual, shares = fetch_sse_day(day)
            if not actual or not shares:
                print("[etf] sse %s: no data" % day)
                continue
            for code in sse_all_codes:
                if code in shares:
                    store.setdefault(code, {})[actual] = shares[code]
            for ic, m in members.items():
                codes = [c for c in m["sse"] if c in shares]
                if codes:
                    idx_hist.setdefault(ic, {})[actual] = round(sum(shares[c] for c in codes), 2)
            print("[etf] sse %s ok (%d 只)" % (actual, len(shares)))
        except Exception as e:
            print("[etf] sse %s failed: %s" % (day, e), file=sys.stderr)
        time.sleep(0.5)

    for code in szse_all_codes:
        try:
            series = fetch_szse_series(code, sz_start, today.isoformat())
            for d, v in series:
                store.setdefault(code, {})[d] = v
            print("[etf] szse %s: %d 天" % (code, len(series)))
        except Exception as e:
            print("[etf] szse %s failed: %s" % (code, e), file=sys.stderr)

    # 深市并入指数合计
    for ic, m in members.items():
        dates = sorted({d for c in m["szse"] for d in store.get(c, {})})
        for d, v in series_sum(store, dates, m["szse"]).items():
            idx_hist.setdefault(ic, {})[d] = round(idx_hist.get(ic, {}).get(d, 0) + v, 2)

    indexes_out = {}
    for ic, m in members.items():
        pts = sorted(({"date": d, "shares_yi": v} for d, v in idx_hist.get(ic, {}).items()),
                     key=lambda x: x["date"])
        if not pts:
            continue
        vals = [p["shares_yi"] for p in pts]
        indexes_out[ic] = {
            "name": m["name"],
            "n_etfs": len(m["sse"]) + len(m["szse"]),
            "members": {"sse": m["sse"], "szse": m["szse"]},
            "series": pts,
            "latest": {
                "date": pts[-1]["date"],
                "shares_yi": vals[-1],
                "d1_chg_pct": pct(vals[-1], vals[-2]) if len(vals) > 1 else None,
                "d5_chg_pct": pct(vals[-1], vals[-6]) if len(vals) > 5 else None,
                "d20_chg_pct": pct(vals[-1], vals[-21]) if len(vals) > 20 else None,
            },
        }
        print("%s %s: %d 天 · %d 只 · 最新 %s %.2f 亿份 · 5日 %s%% · 20日 %s%%"
              % (ic, m["name"], len(pts), indexes_out[ic]["n_etfs"], pts[-1]["date"],
                 vals[-1], indexes_out[ic]["latest"]["d5_chg_pct"],
                 indexes_out[ic]["latest"]["d20_chg_pct"]))

    basket = {}
    for code, (secid, name, market) in BASKET.items():
        pts = sorted(({"date": d, "shares_yi": v,
                       "source": "sse_etf_vol" if market == "sse" else "szse_fund_size"}
                      for d, v in store.get(code, {}).items()), key=lambda x: x["date"])
        if not pts:
            if old.get("basket", {}).get(code):
                basket[code] = old["basket"][code]
            continue
        vals = [p["shares_yi"] for p in pts]
        basket[code] = {
            "name": name, "secid": secid, "market": market, "n": len(pts),
            "series": pts,
            "latest": {
                "date": pts[-1]["date"], "shares_yi": vals[-1],
                "d1_chg_pct": pct(vals[-1], vals[-2]) if len(vals) > 1 else None,
                "d5_chg_pct": pct(vals[-1], vals[-6]) if len(vals) > 5 else None,
                "d20_chg_pct": pct(vals[-1], vals[-21]) if len(vals) > 20 else None,
            },
        }

    smart = build_smart_money(indexes_out)
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "collect_etf_shares.py — 交易所日度总份额，按宽基指数聚合（亿份）",
        "unit": "亿份",
        "min_history_days": MIN_HISTORY_DAYS,
        "window_days": WINDOW_DAYS,
        "smart_money": smart,
        "indexes": indexes_out,
        "basket": basket,
        "map_built": imap.get("built"),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print("\n-> %s (%d B)" % (OUT, os.path.getsize(OUT)))
    if smart:
        print("聪明钱仓位百分位: %s%%（%d 个指数，窗口 %d 天）"
              % (smart["position_pct"], smart["index_used"], smart["window_days"]))
    else:
        print("聪明钱仓位: 历史不足 %d 天，先跑 --backfill-days" % MIN_HISTORY_DAYS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
