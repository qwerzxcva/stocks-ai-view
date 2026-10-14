#!/usr/bin/env python3
"""
collect_global_markets.py — 全球各大股市实时状态 + 情绪周期

覆盖: A股(上证/深证/科创/北证) / 港股(恒指/国企/恒生科技) /
      美股(道指/纳指/标普) / 日股(日经225) / 韩股(韩国综合) /
      欧股(富时100/德国DAX/法国CAC)

每个指数输出:
  code / name / price / chg_pct / 状态(盘中/休市/盘前) /
  当地时间 / 交易时段(09:30-15:00 等) / kline_url (点击看历年K线)

数据源:
  1. 腾讯 sqt.gtimg.cn (实测可用, 一条请求取多个指数)
  2. 东财 push2 ulist.np (f62 主力资金, 补充)

用法:
  python3 collect_global_markets.py --out data/global_markets.json
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}

# (腾讯代码, 东财secid, 名称, 市场, 时区偏移小时, 交易时段)
# 腾讯 sqt.gtimg.cn 只覆盖 A股/港股/美股 (实测日韩欧代码无响应);
# 日股/韩股/欧股走东财 secid (100.XXX)。
MARKETS = [
    # ── A 股 (UTC+8) ──
    ("s_sh000001", "1.000001", "上证指数", "A股", 8, "09:30-15:00"),
    ("s_sz399001", "0.399001", "深证成指", "A股", 8, "09:30-15:00"),
    ("s_sh000688", "1.000688", "科创50", "A股", 8, "09:30-15:00"),
    ("s_bj899050", "0.899050", "北证50", "A股", 8, "09:30-15:00"),
    # ── 港股 (UTC+8) ──
    ("s_hkHSI", "100.HSI", "恒生指数", "港股", 8, "09:30-16:00"),
    ("s_hkHSCEI", "100.HSCEI", "国企指数", "港股", 8, "09:30-16:00"),
    ("s_hkHSTECH", "100.HSTECH", "恒生科技", "港股", 8, "09:30-16:00"),
    # ── 日股 (UTC+9) ──
    ("", "100.N225", "日经225", "日股", 9, "09:00-15:00"),
    # ── 韩股 (UTC+9) ──
    ("", "100.KOSPI", "韩国综合", "韩股", 9, "09:00-15:30"),
    # ── 欧股 ──
    ("", "100.FTSE", "英国富时100", "欧股", 1, "08:00-16:30"),
    ("", "100.DAX", "德国DAX", "欧股", 2, "09:00-17:30"),
    ("", "100.CAC40", "法国CAC40", "欧股", 2, "09:00-17:30"),
    # ── 美股 (UTC-4) ──
    ("s_usDJI", "100.DJIA", "道琼斯", "美股", -4, "21:30-04:00"),
    ("s_usIXIC", "100.IXIC", "纳斯达克", "美股", -4, "21:30-04:00"),
    ("s_usSPX", "100.SPX", "标普500", "美股", -4, "21:30-04:00"),
]

# 情绪周期判断: 按涨跌幅与涨跌家数给 5 档
SENTIMENT_BANDS = [
    (3.0, "euphoria", "亢奋"),
    (1.0, "optimistic", "乐观"),
    (-1.0, "neutral", "中性"),
    (-3.0, "fearful", "悲观"),
    (-99, "panic", "恐慌"),
]


def sentiment_of(chg_pct):
    if chg_pct is None:
        return "unknown", "未知"
    for th, key, label in SENTIMENT_BANDS:
        if chg_pct >= th:
            return key, label
    return "panic", "恐慌"


def market_status(tz_offset, session):
    """判断该市场现在是 盘中 / 盘前 / 盘后 / 休市(周末)。"""
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=tz_offset)
    weekday = local.weekday()
    if weekday >= 5:
        return "closed", "周末休市"
    # session 形如 "09:30-15:00" (当地时间) — 跨零点的用 "21:30-04:00"
    try:
        a, b = session.split("-")
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
        start = ah * 60 + am
        end = bh * 60 + bm
        cur = local.hour * 60 + local.minute
        if start <= end:
            if start <= cur < end:
                return "open", "盘中"
            return ("pre", "盘前") if cur < start else ("post", "盘后")
        else:
            # 跨零点 (美股)
            if cur >= start or cur < end:
                return "open", "盘中"
            return ("post", "已收盘") if cur < start else ("pre", "盘前")
    except Exception:
        return "unknown", "时段未知"


def fetch_tencent():
    """腾讯 sqt.gtimg.cn 一条请求取全部指数 (实测可用)。"""
    codes = ",".join(m[0] for m in MARKETS)
    url = "https://sqt.gtimg.cn/qt?app=tengxuncaijing&q=" + codes
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read().decode("gbk", errors="replace")
    except Exception as e:
        print("[global] 腾讯失败: %s %s" % (type(e).__name__, e))
        return {}

    out = {}
    for line in body.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        # 形如 v_s_sh000001="1~上证指数~000001~3767.23~..."
        var = line.split("=", 1)[0].strip()
        # 剥掉 v_s_ 前缀, 剩下 s_sh000001 / s_hkHSI 等
        if var.startswith("v_s_"):
            var = var[4:]
        elif var.startswith("v_"):
            var = var[2:]
        payload = line.split('"', 2)[1] if line.count('"') >= 2 else ""
        f = payload.split("~")
        if len(f) < 6:
            continue
        out[var] = {
            "name": f[1],
            "code": f[2],
            "price": _num(f[3]),
            "chg": _num(f[4]),
            "chg_pct": _num(f[5]),
        }
    return out


def fetch_eastmoney_indices(secids):
    """东财 ulist.np 补日/韩/欧股 (腾讯不覆盖)。返回 {secid: {...}}"""
    if not secids:
        return {}
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=%s"
           "&fields=f12,f14,f2,f3,f62,f184" % ",".join(secids))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print("[global] 东财失败: %s %s" % (type(e).__name__, str(e)[:50]))
        return {}
    out = {}
    for it in ((d.get("data") or {}).get("diff")) or []:
        secid = "%s.%s" % (it.get("f13"), it.get("f12")) if it.get("f13") else it.get("f12")
        # ulist.np 用 secids 查询时返回的 f12 是代码, 需要用 secid 反查
        out[str(it.get("f12"))] = {
            "name": it.get("f14"),
            "price": _num(it.get("f2")),
            "chg_pct": _num(it.get("f3")),
        }
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "global_markets.json"))
    args = ap.parse_args()

    quotes = fetch_tencent()
    # 东财补腾讯不覆盖的 (日/韩/欧): 按 secid 查询, 结果按 secid 索引
    em_secids = [m[1] for m in MARKETS if not m[0]]
    em_quotes = fetch_eastmoney_indices(em_secids) if em_secids else {}
    # 东财返回的 key 是 f12 (如 N225), 建一个 secid->data 的映射
    em_by_secid = {}
    for secid in em_secids:
        code = secid.split(".", 1)[1]
        if code in em_quotes:
            em_by_secid[secid] = em_quotes[code]
    now = datetime.now(CST)

    markets = {}
    indices = []
    for tk, secid, name, mkt, tz, session in MARKETS:
        # 腾讯优先, 东财兜底
        key = tk.replace("s_", "") if tk else ""
        q = quotes.get(key) if key else None
        if not q:
            q = em_by_secid.get(secid) or {}
        status, status_label = market_status(tz, session)
        local_time = (datetime.now(timezone.utc) + timedelta(hours=tz)).strftime("%H:%M")
        sent, sent_label = sentiment_of(q.get("chg_pct"))
        bare = q.get("code") or key or secid.split(".", 1)[-1]
        rec = {
            "code": bare,
            "name": q.get("name") or name,
            "market": mkt,
            "price": q.get("price"),
            "chg_pct": q.get("chg_pct"),
            "status": status,
            "status_label": status_label,
            "local_time": local_time,
            "tz_offset": tz,
            "session": session,
            "sentiment": sent,
            "sentiment_label": sent_label,
            # 点击看历年K线: 详情页用这个代码拉历史
            "kline_code": bare,
        }
        indices.append(rec)
        markets.setdefault(mkt, []).append(rec)

    # 全球情绪汇总: 取各市场当日涨跌幅均值
    chgs = [i["chg_pct"] for i in indices if i["chg_pct"] is not None]
    avg = sum(chgs) / len(chgs) if chgs else None
    g_sent, g_sent_label = sentiment_of(avg)

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "collect_global_markets v1 (腾讯 sqt.gtimg.cn)",
        "source": "tencent sqt.gtimg.cn",
        "n_indices": len(indices),
        "n_ok": sum(1 for i in indices if i["price"] is not None),
        "indices": indices,
        "markets": markets,
        "global_sentiment": {
            "avg_chg_pct": round(avg, 2) if avg is not None else None,
            "sentiment": g_sent,
            "sentiment_label": g_sent_label,
            "interpretation": {
                "euphoria": "全球风险偏好极高, 注意追高风险",
                "optimistic": "全球偏多, 可顺势但留余地",
                "neutral": "全球分化, 以结构行情对待",
                "fearful": "全球偏空, 降低仓位等待",
                "panic": "全球恐慌, 以防守为主",
                "unknown": "数据不足, 无法判断",
            }.get(g_sent, ""),
        },
        "meta": {
            "sessions": "各市场交易时段按当地时间; 跨零点时段(美股)已特殊处理",
            "caveat": "休市时价格是上一交易日收盘; Research reference only",
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("[global] %d/%d 个指数有数据" % (out["n_ok"], out["n_indices"]))
    for mkt, lst in markets.items():
        parts = []
        for i in lst:
            mark = {"open": "●", "pre": "○", "post": "◌", "closed": "×"}.get(i["status"], "?")
            pv = i["chg_pct"]
            parts.append("%s%s%s" % (mark, i["name"],
                                     ("%+.2f%%" % pv) if pv is not None else ""))
        print("  %-4s %s" % (mkt, "  ".join(parts)))
    print("[global] 全球情绪: %s (%s) 均值%s" % (
        g_sent_label, g_sent,
        ("%+.2f%%" % avg) if avg is not None else "—"))
    print("[global] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
