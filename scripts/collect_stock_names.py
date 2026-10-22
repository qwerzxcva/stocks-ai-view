#!/usr/bin/env python3
"""
collect_stock_names.py — 批量获取 A 股中文名

从东财数据源拉取全市场股票名。输出 data/all_stock_names.json:
  { "sh600000": "浦发银行", "sz000001": "平安银行", ... }

用法:
  python3 scripts/collect_stock_names.py --out data/all_stock_names.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))


def fetch_all_stock_names() -> dict[str, str]:
    """从东财数据接口拉取全市场 A 股中文名。"""
    # 东财全市场股票列表 API (免费，无鉴权)
    url = "https://datacenter.eastmoney.com/securities/api/data/get?type=RPTA_QW_QW&sty=SECURITY_NAME&st=SECURITY_CODE&ps=6000&p=1&filter=(MARKET+in+(\"SSE\",\"SZSE\"))"
    
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.eastmoney.com/"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return {}

    if not data.get("data"):
        print("API 返回空数据", file=sys.stderr)
        return {}

    out = {}
    for row in data["data"]:
        code = row.get("SECURITY_CODE")
        name = row.get("SECURITY_ABBR") or row.get("SECURITY_NAME")
        if code and name:
            # 拼接市场前缀
            market = row.get("MARKET", "")
            if market == "SSE":
                full_code = f"sh{code}"
            elif market == "SZSE":
                full_code = f"sz{code}"
            else:
                full_code = f"{code}"
            out[full_code] = name

    return out


def fallback_fetch() -> dict[str, str]:
    """备用方案：从腾讯接口拉取。"""
    out = {}
    # 腾讯 A 股列表 API
    url = "https://qt.gtimg.cn/r=fs123&fs=gs_gsc&_=123"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.qq.com/"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("gbk", errors="ignore")
        # 解析 q=sh600000~浦发银行~600000~格式
        import re
        matches = re.findall(r'q=([a-zA-Z]*\d+)~([^~]*)~\d+', text)
        for code, name in matches:
            if code.startswith(("sh", "sz")):
                out[code] = name
    except Exception as e:
        print(f"腾讯 fallback 失败: {e}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "all_stock_names.json"))
    args = ap.parse_args()

    print("正在拉取全市场股票名 (东财)...")
    names = fetch_all_stock_names()
    
    if len(names) < 2000:
        print(f"东财只拉到 {len(names)} 只，尝试腾讯备用...")
        fb = fallback_fetch()
        for k, v in fb.items():
            names.setdefault(k, v)
        print(f"合并后 {len(names)} 只")

    if len(names) < 100:
        print("警告：全市场数量过低，请检查网络或 API 限流", file=sys.stderr)

    out = {
        "ts": datetime.now(CST).isoformat(),
        "engine": "collect_stock_names",
        "n_symbols": len(names),
        "names": names,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"→ {args.out} ({len(names):,} 只)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
