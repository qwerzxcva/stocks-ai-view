#!/usr/bin/env python3
"""
collect_global_kline.py — 全球指数 K 线历史构建 (美股/日股/韩股/欧股)

用户要求: A 股已补齐全历史, 美股/韩股/日股「从现在开始构建」。

数据源: 腾讯 sqt.gtimg.cn / web.ifzq.gtimg.cn
  实测 (2026-10-14): 腾讯支持 A股(sh/sz)/港股(hk)/美股(us) 的 K 线,
  日韩欧股无响应 → 用东财 secid 或标注不可用。

K 线端点: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<code>,day,,,<count>,qfq
  美股代码格式: usDJI.us / usAAPL.us (腾讯风格)
  实测 640 根硬上限 (count>640 返回空), 故一次最多拉 640 根 (~2.5年日K)

策略「从现在开始构建」:
  每日增量: 每次拉最近 640 根, 与本地已有日期去重后追加。
  日积月累自然形成全历史 (A 股当初也是这么补的)。

用法:
  python3 collect_global_kline.py --out data/global_kline.json
  python3 collect_global_kline.py --markets us,hk --count 300
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

# (腾讯代码, 名称, 市场, 时区)
GLOBAL_INDICES = [
    # 美股 (UTC-4) — 腾讯需要交易所后缀: .OQ=纳斯达克 / .NY=纽交所
    ("usDJI.OQ", "道琼斯", "us", -4),
    ("usIXIC.OQ", "纳斯达克", "us", -4),
    ("usSPX.OQ", "标普500", "us", -4),
    ("usNDX.OQ", "纳斯达克100", "us", -4),
    ("usAAPL.OQ", "苹果", "us", -4),
    ("usNVDA.OQ", "英伟达", "us", -4),
    ("usTSLA.OQ", "特斯拉", "us", -4),
    ("usMSFT.OQ", "微软", "us", -4),
    # 港股 (UTC+8)
    ("hkHSI", "恒生指数", "hk", 8),
    ("hkHSCEI", "国企指数", "hk", 8),
    ("hkHSTECH", "恒生科技", "hk", 8),
    ("hk00700", "腾讯控股", "hk", 8),
    ("hk09988", "阿里巴巴-W", "hk", 8),
    # 日股 (UTC+9) — 腾讯可能不支持, 实测确认
    ("jpNI225", "日经225", "jp", 9),
    # 韩股 (UTC+9) — 腾讯可能不支持, 实测确认
    ("krKOSPI", "韩国综合", "kr", 9),
]

KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             "?param={code},day,,,{count},qfq")


def fetch_kline(code, count=640):
    """腾讯 K 线。返回 [{date, open, close, high, low, vol}, ...]"""
    url = KLINE_URL.format(code=code, count=count)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return [], "%s: %s" % (type(e).__name__, str(e)[:60])
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    if not arr:
        return [], "empty"
    rows = []
    for r in arr:
        if len(r) < 6:
            continue
        rows.append({
            "date": r[0], "open": float(r[1]), "close": float(r[2]),
            "high": float(r[3]), "low": float(r[4]), "vol": float(r[5]),
        })
    return rows, None


# 东财 secid fallback (腾讯无历史的美股指数/日韩欧)
EM_SECID = {
    "usDJI.OQ": "100.DJIA", "usIXIC.OQ": "100.IXIC",
    "usSPX.OQ": "100.SPX", "usNDX.OQ": "100.NDX",
    "jpNI225": "100.N225", "krKOSPI": "100.KOSPI",
    "hkHSI": "100.HSI",
}


def fetch_kline_em(secid, count=640):
    """东财 K 线 fallback。容器内常被 WAF, GitHub Actions 通常通。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, count))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return [], "%s: %s" % (type(e).__name__, str(e)[:60])
    kl = ((d.get("data") or {}).get("klines")) or []
    if not kl:
        return [], "empty"
    rows = []
    for s in kl:
        p = s.split(",")
        if len(p) < 6:
            continue
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]), "vol": float(p[5]),
        })
    return rows, None


def fetch_any(code, count):
    """腾讯优先, 东财 secid 兜底。"""
    rows, err = fetch_kline(code, count)
    if rows and len(rows) > 5:
        return rows, None
    secid = EM_SECID.get(code)
    if secid:
        rows2, err2 = fetch_kline_em(secid, count)
        if rows2:
            return rows2, None
        return [], err2 or err
    return rows, err


def load_existing(path, code):
    """读本地已有 K 线 (增量去重)。"""
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path, encoding="utf-8"))
        return {r["date"]: r for r in (d.get("klines") or {}).get(code) or []}
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "global_kline.json"))
    ap.add_argument("--markets", default="us,hk,jp,kr",
                    help="逗号分隔: us/hk/jp/kr")
    ap.add_argument("--count", type=int, default=640,
                    help="每标的拉多少根 (腾讯上限 640)")
    ap.add_argument("--codes", default="", help="逗号分隔腾讯代码 (调试用)")
    args = ap.parse_args()

    markets = set(m.strip() for m in args.markets.split(",") if m.strip())
    targets = []
    if args.codes.strip():
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        targets = [(c, c, "manual", 0) for c in codes]
    else:
        targets = [t for t in GLOBAL_INDICES if t[2] in markets]

    # 读已有 (增量)
    existing = {}
    if os.path.exists(args.out):
        try:
            existing = (json.load(open(args.out, encoding="utf-8")).get("klines")) or {}
        except Exception:
            existing = {}

    klines = {}
    ok, fail = [], []
    for i, (code, name, mkt, tz) in enumerate(targets):
        rows, err = fetch_any(code, args.count)
        if not rows:
            fail.append({"code": code, "name": name, "market": mkt, "reason": err})
            print(f"  [FAIL] {mkt} {name} ({code}): {err}")
            continue
        # 增量合并
        have = {r["date"]: r for r in existing.get(code) or []}
        for r in rows:
            have[r["date"]] = r
        merged = sorted(have.values(), key=lambda r: r["date"])
        klines[code] = merged
        ok.append({"code": code, "name": name, "market": mkt,
                   "n_total": len(merged), "n_new": len(merged) - len(existing.get(code) or []),
                   "first": merged[0]["date"], "last": merged[-1]["date"]})
        print(f"  [OK] {mkt} {name} ({code}): {len(merged)} 根 "
              f"(+{len(merged) - len(existing.get(code) or [])}) "
              f"{merged[0]['date']}..{merged[-1]['date']}")
        if i < len(targets) - 1:
            time.sleep(0.5)

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "collect_global_kline v1 (腾讯 fqkline, 增量构建)",
        "source": "web.ifzq.gtimg.cn",
        "n_targets": len(targets),
        "n_ok": len(ok),
        "n_fail": len(fail),
        "ok": ok,
        "fail": fail,
        "klines": klines,
        "meta": {
            "limit": "腾讯 fqkline 单次上限 640 根 (~2.5 年日 K)",
            "strategy": "每日增量拉取, 去重追加, 日积月累形成全历史",
            "caveat": "日股/韩股/欧股腾讯可能不支持 (实测确认); Research reference only",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print()
    print(f"[gkline] {len(ok)}/{len(targets)} 成功")
    if fail:
        print("[gkline] 失败的市场: " + ", ".join(sorted({f['market'] for f in fail})))
    print(f"[gkline] -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
