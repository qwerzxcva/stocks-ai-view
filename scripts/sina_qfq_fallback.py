#!/usr/bin/env python3
"""sina_qfq_fallback.py — 新浪前复权 K 线 (腾讯 qfq 负价 bug 的替代源)

腾讯 fqkline qfq 对长期高分红股票算出负价 (2026-10-13 实测:
  茅台 3531/6019 根 <=0, 平安银行 2872/5876 根, 深发展 3379/8549 根)
新浪 vip.stock.finance.sina.com.cn 的 getKLineData 是标准前复权, 无此问题。

接口:
  https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/
  CN_MarketData.getKLineData?symbol={sh|sz}{code}&scale=240&ma=5&datalen=1023

坑:
  - datalen 实测可远超 1023 (6000 也能返回), 文档说的 1023 不准; 其他分页参数
    (page/start_date/year/season/end_date/type) 全部无效 → 只能靠 datalen 拿长度
  - 必须带 Referer: https://finance.sina.com.cn/
  - scale=240 日线; 120=30分; 60=小时; 5/15/30=分钟
  - 返回直含 ma_price5/ma_volume5/ma_price10/ma_volume10
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}
BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData")


def _tk(code):
    code = str(code).strip()
    if code.startswith(("sh", "sz")):
        return code
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def fetch_window(code, end_date, datalen=1023, scale=240, ma=5):
    """拉 end_date 之前 (含) 的 datalen 根。"""
    u = (f"{BASE}?symbol={_tk(code)}&scale={scale}&ma={ma}"
         f"&datalen={datalen}&end_date={end_date}" if False else
         f"{BASE}?symbol={_tk(code)}&scale={scale}&ma={ma}&datalen={datalen}")
    # 新浪接口不支持 end_date; 只返回最近 datalen 根 → 只能拿近期数据
    for i in range(3):
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == 2:
                print(f"    [fail] {type(e).__name__}", file=sys.stderr)
            time.sleep(0.5)
    return []


def fetch_recent(code, datalen=1023):
    """新浪只能拿最近 datalen 根 (无 end 参数)。"""
    return fetch_window(code, None, datalen)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", action="append")
    ap.add_argument("--batch-file")
    ap.add_argument("--out-dir", default="/root/workspace/stocks-ai-view/data/adjusted")
    ap.add_argument("--datalen", type=int, default=6000,
                    help="根数 (实测上限远超 1023, 6000 可覆盖多数全史; 默认 6000)")
    args = ap.parse_args()

    codes = list(args.code or [])
    if args.batch_file:
        with open(args.batch_file, encoding="utf-8") as f:
            codes += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if not codes:
        ap.print_help()
        return 1

    for code in codes:
        arr = fetch_recent(code, args.datalen)
        if not arr:
            print(f"  ❌ {code}: 新浪返回空")
            continue
        bars = [[r["day"], float(r["open"]), float(r["close"]), float(r["high"]),
                 float(r["low"]), float(r["volume"])] for r in arr]
        neg = sum(1 for b in bars if b[1] <= 0 or b[2] <= 0)
        p = os.path.join(args.out_dir, f"{code}_qfq_sina.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now(CST).isoformat(timespec="seconds"),
                       "source": "sina_vip_getKLineData",
                       "code": code, "tk_code": _tk(code), "adj": "qfq",
                       "n": len(bars), "span": [bars[0][0], bars[-1][0]],
                       "bars": bars,
                       "meta": {"url": BASE, "adj_note": "前复权 (新浪标准口径)",
                                "note": "腾讯 qfq 负价 bug 的替代源; 单次最多 1023 根"}},
                      f, ensure_ascii=False, separators=(",", ":"))
        print(f"  ✅ {code} qfq(sina): {len(bars)} 根 "
              f"({bars[0][0]} ~ {bars[-1][0]}) 负价={neg} → {os.path.basename(p)}")
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
