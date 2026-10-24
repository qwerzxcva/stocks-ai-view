#!/usr/bin/env python3
"""collect_etf_shares.py — 日度估算宽基 ETF 份额，跟踪大资金（国家队）加减仓。

方法来源：公众号「多多拾贝」盯份额抄作业法（用户指定借鉴 2026-10-24）。
份额(亿份) = 成交量(手) / 换手率(%) / 1e4。

数据源要点（实测结论，勿回退）：
- push2his 日 K 的 f61 换手率按固定参考份额计算，vol/turnover 恒为常数 → 只能给
  正确量级的历史底稿，不能反映份额增减。
- push2 实时快照的 f8 换手率按当日真实份额计算 → 逐日收盘后采集，才是有效的
  份额跟踪序列。首次运行起积累；历史用 K 线底稿占位（smart_money 标注 live=false）。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUT = os.path.join(DATA_DIR, "etf_shares.json")

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


def curl_json(url: str) -> dict:
    """容器内 python urllib 被东财 WAF 掐连接（curl 正常），统一走 curl 子进程。"""
    last = None
    for attempt in range(4):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "30",
                 "-H", "Referer: https://quote.eastmoney.com/",
                 "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", url],
                capture_output=True, timeout=40)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError(f"curl rc={r.returncode}")
            return json.loads(r.stdout.decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise last


def kline_series(secid: str) -> list:
    root = curl_json("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                     f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f56,f61"
                     "&klt=101&fqt=1&beg=19900101&end=20500101")
    rows = (root.get("data") or {}).get("klines") or []
    out = []
    for line in rows:
        p = line.split(",")
        if len(p) < 3:
            continue
        date, vol, turnover = p[0], float(p[1] or 0), float(p[2] or 0)
        if vol <= 0 or turnover <= 0:
            continue
        # 份额(亿份) = 成交量(手)/(换手率%)/1e4；f61 为固定参考口径，仅作历史底稿
        out.append({"date": date, "vol": vol, "turnover": turnover,
                    "shares_yi": round(vol / turnover / 1e4, 2)})
    return out


def live_snapshot(code: str, secid: str) -> dict | None:
    """push2 实时快照：f47 成交量(手) + f8 换手率(真实份额口径)。"""
    root = curl_json("https://push2.eastmoney.com/api/qt/stock/get"
                     f"?secid={secid}&fields=f43,f47,f48,f8,f10,f57,f58")
    d = (root.get("data") or {})
    if code == "510300":
        print(f"  [probe] 510300 push2 raw: {d}")  # CI 日志里确认 f8 是否为换手率
    vol = d.get("f47")
    turnover = d.get("f8")
    if vol is None or turnover is None:
        return None
    vol, turnover = float(vol), float(turnover)
    if turnover <= 0:
        return None
    return {"date": datetime.now(CST).strftime("%Y-%m-%d"), "vol": vol, "turnover": turnover,
            "live_shares_yi": round(vol / turnover / 1e4, 2)}


def pct(a, b):
    return round((a - b) / b * 100, 2) if b else None


def main() -> None:
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8")).get("basket") or {}
        except Exception:
            old = {}

    basket = {}
    for code, (secid, name) in BASKET.items():
        try:
            series = kline_series(secid)
            time.sleep(1.5)
            live = live_snapshot(code, secid)
            time.sleep(1.0)
        except Exception as e:
            print(f"[warn] {code} 抓取失败: {e}", file=sys.stderr)
            if code not in old:
                continue
            series = old[code].get("series") or []
            live = (old[code].get("live_series") or [None])[-1]

        # 合并 live 序列（按日期去重，新值覆盖）
        live_series = {x["date"]: x for x in (old.get(code, {}).get("live_series") or [])}
        if live:
            live_series[live["date"]] = live
        live_list = [live_series[k] for k in sorted(live_series)]

        latest = series[-1] if series else None
        latest_live = live_list[-1] if live_list else None
        ref = (latest_live or latest or {}).get("live_shares_yi") or (latest or {}).get("shares_yi")
        d = {"name": name, "secid": secid, "n": len(series),
             "latest": {"date": (latest_live or latest or {}).get("date"),
                        "shares_yi": ref,
                        "d1_chg_pct": None, "d5_chg_pct": None, "d20_chg_pct": None},
             "series": series, "live_series": live_list}
        if latest_live and len(live_list) > 1:
            vals = [x["live_shares_yi"] for x in live_list]
            d["latest"]["d1_chg_pct"] = pct(vals[-1], vals[-2])
            d["latest"]["d5_chg_pct"] = pct(vals[-1], vals[-6]) if len(vals) > 5 else None
            d["latest"]["d20_chg_pct"] = pct(vals[-1], vals[-21]) if len(vals) > 20 else None
        elif latest and len(series) > 1:
            d["latest"]["d1_chg_pct"] = pct(latest["shares_yi"], series[-2]["shares_yi"])
        basket[code] = d
        print(f"{code} {name}: kline {len(series)} 根 | live {len(live_list)} 天 | "
              f"最新 {d['latest']['date']} 份额 {ref} 亿份 日 {d['latest']['d1_chg_pct']}%")

    smart = None
    if len(basket) >= 3:
        positions = []
        for code, d in basket.items():
            src = d["live_series"] if len(d["live_series"]) >= 20 else None
            if not src:
                continue  # live 序列不足 20 天不出仓位信号，防止用固定底稿误导
            s = [x["live_shares_yi"] for x in src[-250:]]
            lo, hi = min(s), max(s)
            positions.append((s[-1] - lo) / (hi - lo) if hi > lo else None)
        positions = [p for p in positions if p is not None]
        if positions:
            smart = {"position_pct": round(sum(positions) / len(positions) * 100, 1),
                     "etfs_used": len(positions), "window_days": len(src),
                     "note": "篮子合计份额处于自身 live 序列区间的百分位（近似文章「仓位%」口径）"}

    out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
           "source": "collect_etf_shares.py (份额=成交量/换手率; live 序列来自 push2 真实份额口径)",
           "smart_money": smart, "basket": basket}
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n-> {OUT} ({os.path.getsize(OUT)} B)")
    if smart:
        print(f"聪明钱仓位百分位: {smart['position_pct']}%")
    else:
        print("聪明钱仓位: live 序列积累中（首日起逐日填充，≥20 天后出信号）")


if __name__ == "__main__":
    main()
