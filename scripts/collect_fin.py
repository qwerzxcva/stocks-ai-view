#!/usr/bin/env python3
"""
collect_fin.py — 全市场 A 股财报数据采集（PEAD 输入）

数据源：东财 datacenter-web（零鉴权）。
接口：RPT_LICO_FN_CPD（利润表：BASIC_EPS, REPORTDATE, NOTICE_DATE 等）

分片策略：按股票代码前缀分批请求，每批 100 只，间隔 0.5s 避免限流。
输出：data/fin/<code>.json 增量覆盖，data/fin_index.json 汇总索引。

用法:
  python3 scripts/collect_fin.py            # 全量
  python3 scripts/collect_fin.py --limit 100  # 冒烟
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")
FIN_DIR = os.path.join(DATA, "fin")

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://data.eastmoney.com/"}


def get_universe() -> list[str]:
    """从 lp_syms.json 或 bulk 分片文件名拿全市场代码（带 sh/sz/bj 前缀）。"""
    p = os.path.join(DATA, "lp_syms.json")
    if os.path.exists(p):
        syms = json.load(open(p, encoding="utf-8"))
        if syms:
            return syms
    # 兜底：从 bulk 文件名提取
    bulk = os.path.join(DATA, "bulk")
    codes = set()
    for f in os.listdir(bulk) if os.path.isdir(bulk) else []:
        if f.startswith("day_qfq_") and f.endswith(".csv"):
            codes.add(f[8:-4])
    return sorted(codes)


def fetch_fin_batch(codes: list[str], retries: int = 3) -> dict[str, list[dict]]:
    """批量请求东财财报数据。返回 {code: [rows]}"""
    if not codes:
        return {}
    # SECURITY_CODE 过滤（去 sh/sz 前缀）
    bare = [c[2:] if c[:2] in ("sh", "sz", "bj") else c for c in codes]
    # 东财 filter 用 IN 语法: (SECURITY_CODE in ("600519","000001"))
    quoted = ",".join(f'"{c}"' for c in bare[:100])
    flt = f'(SECURITY_CODE in ({quoted}))'
    params = {
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": f"({flt})",
        "pageSize": "5000",
        "sortColumns": "REPORTDATE",
        "sortTypes": "-1",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8"))
            if not d.get("success") or not d.get("result"):
                raise ValueError(f"API fail: {d.get('message')}")
            rows = d["result"].get("data", [])
            out: dict[str, list[dict]] = {}
            for row in rows:
                code = row.get("SECURITY_CODE")
                if not code:
                    continue
                # 还原 sh/sz 前缀
                mkt = row.get("TRADE_MARKET_CODE", "")
                if mkt == "1" or code.startswith(("60", "68")):
                    full = "sh" + code
                elif mkt == "2" or code.startswith(("00", "30")):
                    full = "sz" + code
                elif code.startswith(("43", "83", "87", "92")) or mkt == "0":
                    full = "bj" + code
                else:
                    full = code
                out.setdefault(full, []).append({
                    "report_date": row.get("REPORTDATE"),
                    "announce_date": row.get("NOTICE_DATE"),
                    "eps": row.get("BASIC_EPS"),
                    "deduct_eps": row.get("DEDUCT_BASIC_EPS"),
                    "revenue": row.get("TOTAL_OPERATE_INCOME"),
                    "net_profit": row.get("PARENT_NETPROFIT"),
                })
            return out
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"batch 失败: {e}", file=sys.stderr)
    return {}


def save_stock(fin_dir: str, code: str, rows: list[dict]):
    """增量写入 data/fin/<code>.json"""
    path = os.path.join(fin_dir, f"{code}.json")
    existing = []
    if os.path.exists(path):
        try:
            existing = json.load(open(path, encoding="utf-8")).get("reports", [])
        except Exception:
            existing = []
    # 合并去重（按 report_date）
    seen = {r.get("report_date") for r in existing}
    merged = existing[:]
    for r in rows:
        if r.get("report_date") not in seen:
            merged.append(r)
            seen.add(r.get("report_date"))
    merged.sort(key=lambda r: r.get("report_date") or "")
    out = {
        "code": code,
        "source": "eastmoney datacenter-web RPT_LICO_FN_CPD",
        "n": len(merged),
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "reports": merged,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟）")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    os.makedirs(FIN_DIR, exist_ok=True)
    universe = get_universe()
    if args.limit:
        universe = universe[:args.limit]
    print(f"全市场 {len(universe)} 只，批大小 {args.batch}")

    total_written = 0
    total_events = 0
    for i in range(0, len(universe), args.batch):
        batch = universe[i:i + args.batch]
        res = fetch_fin_batch(batch)
        for code, rows in res.items():
            if rows:
                save_stock(FIN_DIR, code, rows)
                total_written += 1
                total_events += len(rows)
        print(f"  batch {i // args.batch + 1}: {len(batch)} 只 → {len(res)} 只有财报 (累计 {total_written})")
        if i + args.batch < len(universe):
            time.sleep(args.sleep)

    # 写索引
    index = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
             "n_files": len(os.listdir(FIN_DIR)),
             "engine": "collect_fin",
             "source": "eastmoney datacenter-web RPT_LICO_FN_CPD"}
    json.dump(index, open(os.path.join(FIN_DIR, "_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n完成: {total_written} 只股票, {total_events} 条财报 → {FIN_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
