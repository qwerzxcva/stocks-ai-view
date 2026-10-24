#!/usr/bin/env python3
"""collect_etf_shares.py — 用「成交量/换手率」日度估算宽基 ETF 份额，跟踪大资金加减仓。

方法来源：公众号「多多拾贝」盯份额变化抄作业法（2026-10-24 用户指定借鉴）。
份额(亿份) = 成交量(手)×100 / 换手率(%) / 1e8。
东财 push2his 日 K 一次拿全历史（f56 成交量、f61 换手率），收盘后跑一轮追加当日。

输出 data/etf_shares.json：
  basket: 各 ETF {name, secid, latest{date,shares_yi,d1/d5/d20_chg}, series[...]}
  smart_money: 合计份额相对自身历史的仓位百分位（≈文章口径的「仓位%」）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUT = os.path.join(DATA_DIR, "etf_shares.json")

# 国家队/宽基核心篮子（沪市 1.5xx / 深市 0.159xxx）
BASKET = {
    "510300": ("1.510300", "沪深300ETF"),
    "510500": ("1.510500", "中证500ETF"),
    "510050": ("1.510050", "上证50ETF"),
    "159915": ("0.159915", "创业板ETF"),
    "588000": ("1.588000", "科创50ETF"),
    "512100": ("1.512100", "中证1000ETF"),
    "159919": ("0.159919", "沪深300ETF深"),
}

CST = timezone(timedelta(hours=8))


def fetch_klines(secid: str) -> list:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f56,f61"
           "&klt=101&fqt=1&beg=19900101&end=20500101")
    last_err = None
    for attempt in range(4):
        try:
            # 容器内 python urllib 被 WAF 掐连接（curl 正常），用 curl 子进程取数
            import subprocess, time
            r = subprocess.run(
                ["curl", "-s", "--max-time", "30", "-H", "Referer: https://quote.eastmoney.com/",
                 "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", url],
                capture_output=True, timeout=40)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError(f"curl rc={r.returncode}")
            root = json.loads(r.stdout.decode("utf-8", "replace"))
            rows = (root.get("data") or {}).get("klines") or []
            break
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)  # 容器 IP 被限流时退避重试
    else:
        raise last_err
    out = []
    for line in rows:
        p = line.split(",")
        if len(p) < 3:
            continue
        date, vol, turnover = p[0], float(p[1] or 0), float(p[2] or 0)
        if turnover <= 0:
            continue
        shares_yi = vol * 100 / turnover / 1e8  # 份额(亿份)
        out.append({"date": date, "vol": vol, "turnover": turnover, "shares_yi": round(shares_yi, 2)})
    return out


def pct(a: float, b: float) -> float | None:
    if b in (0, None) or a is None:
        return None
    return round((a - b) / b * 100, 2)


def main() -> None:
    basket = {}
    for code, (secid, name) in BASKET.items():
        try:
            series = fetch_klines(secid)
        except Exception as e:
            print(f"[warn] {code} 抓取失败: {e}", file=sys.stderr)
            continue
        if not series:
            continue
        import time
        time.sleep(1.5)  # 请求间隔，防容器 IP 限流
        latest = series[-1]
        d = {"name": name, "secid": secid, "n": len(series),
             "latest": {"date": latest["date"], "shares_yi": latest["shares_yi"],
                        "d1_chg_pct": pct(latest["shares_yi"], series[-2]["shares_yi"] if len(series) > 1 else None),
                        "d5_chg_pct": pct(latest["shares_yi"], series[-6]["shares_yi"] if len(series) > 5 else None),
                        "d20_chg_pct": pct(latest["shares_yi"], series[-21]["shares_yi"] if len(series) > 20 else None)},
             "series": series}
        basket[code] = d
        print(f"{code} {name}: {len(series)} 根, 最新 {latest['date']} 份额 {latest['shares_yi']} 亿份, "
              f"日变化 {d['latest']['d1_chg_pct']}%")

    # 聪明钱仓位：合计份额指数（各 ETF 份额相对 250 日区间的位置，等权平均）
    smart = None
    if len(basket) >= 3:
        positions = []
        for code, d in basket.items():
            s = [x["shares_yi"] for x in d["series"][-250:]]
            if len(s) < 60:
                continue
            lo, hi = min(s), max(s)
            positions.append((s[-1] - lo) / (hi - lo) if hi > lo else None)
        positions = [p for p in positions if p is not None]
        if positions:
            smart = {"position_pct": round(sum(positions) / len(positions) * 100, 1),
                     "etfs_used": len(positions),
                     "window": 250,
                     "note": "篮子合计份额处于自身250日区间的百分位，近似文章「仓位%」口径"}

    out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
           "source": "collect_etf_shares.py (成交量/换手率估算份额, 方法: 多多拾贝)",
           "smart_money": smart, "basket": basket}
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n-> {OUT} ({os.path.getsize(OUT)} B)")
    if smart:
        print(f"聪明钱仓位百分位: {smart['position_pct']}% ({smart['etfs_used']} 只 ETF)")


if __name__ == "__main__":
    main()
