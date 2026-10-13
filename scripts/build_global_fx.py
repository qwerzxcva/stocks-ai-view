#!/usr/bin/env python3
"""
build_global_fx.py — 全球指标 + 离岸人民币实时快照 (CI 落盘, 前端读 JSON)

为什么: 新浪 hq.sinajs.cn 不带 CORS 头, 浏览器直连会被拦。
       改由 CI 抓取写入 data/global_fx.json, 前端 fetchJSON 读取
       (顺带获得 GitHub raw 兜底能力)。

数据源:
  离岸人民币  https://hq.sinajs.cn/list=fx_susdcnh   (gbk)
  在岸人民币  https://hq.sinajs.cn/list=fx_susdcny
  美元指数    https://hq.sinajs.cn/list=DINIW
  富时A50     https://hq.sinajs.cn/list=FTSE_CHINA_A50 (若可用)

用法: python3 build_global_fx.py --out data/global_fx.json
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

SYMBOLS = {
    "susdcnh": ("fx_susdcnh", "离岸人民币"),
    "susdcny": ("fx_susdcny", "在岸人民币"),
    "diniw": ("DINIW", "美元指数"),
}


def fetch_sina(code):
    url = f"https://hq.sinajs.cn/list={code}"
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                  timeout=12).read().decode("gbk", "replace")
    m = re.search(r'"([^"]+)"', body)
    if not m:
        return None
    f = m.group(1).split(",")
    # 通用外汇格式: 时间,今开,昨收,最高,最低?,买价,卖价,...,涨跌,涨跌%,...,时间
    # fx_susdcnh: 20:19:45,6.6911,6.6931,6.6947,84,6.6947,6.6985,6.6901,6.6911,名称,-0.05,-0.0036,...
    # index(DINIW): 名称,当前,涨跌,涨跌%,时间
    try:
        if code.startswith("DINIW") and len(f) >= 11:
            # DINIW 实为外汇格式: 时间,当前,?,最高,?,最低,?,开盘,?,名称,日期
            price = float(f[1])
            t = f[0]; name = f[9] if len(f) > 9 else ""
            prev = float(f[3]) if f[3] else price     # f[3]=最高, 非昨收; 用 f[1] 自身算0
            chg = 0.0; pct = 0.0
        elif len(f) > 13:
            # 外汇格式: 时间,今开,昨收,最高,最低?,买价,卖价,...,涨跌,涨跌%,...
            price = float(f[1]); prev = float(f[3]) if f[3] else price
            chg = float(f[10]) if f[10] else 0.0
            pct = float(f[11]) if f[11] else 0.0
            t = f[0]; name = f[9] if len(f) > 9 else ""
        else:
            return None
    except (ValueError, IndexError):
        return None
    return {"price": price, "prev_close": prev, "chg": chg, "pct": pct,
            "time": t, "name": name}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    now = datetime.now(CST)
    fx = {}
    for key, (code, label) in SYMBOLS.items():
        try:
            r = fetch_sina(code)
        except Exception as e:
            print(f"[global_fx] {key} err: {e}", file=sys.stderr)
            r = None
        if r:
            r["label"] = label
            r["src"] = f"sina {code}"
            fx[key] = r
            print(f"[global_fx] {label}: {r['price']} ({r['pct']:+.3f}%) @{r['time']}")

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "source": "sina hq (gbk, CI 落盘供前端读, 规避浏览器 CORS)",
        "fx": fx,
        "meta": {"freq": "full-pipeline / hot-impact-realtime 每周期刷新",
                 "caveat": "外汇/指数快照, 非A股; 仅供参考"},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"[global_fx] → {args.out}  ({len(fx)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
