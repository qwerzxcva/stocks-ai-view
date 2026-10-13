#!/usr/bin/env python3
"""
build_search_index.py — 生成前端搜索索引 data/search_index.json

格式 (与前端 index.html 的 symSearch 严格兼容):
  顶层扁平对象, key = 带前缀代码, value = {n: 名称, p: 价格, chg: 涨跌幅%, t: 类型}
  t: s=股票, etf=ETF/基金

数据优先级:
  1. stocks-data-live/data/market/snapshot_*.json (全市场行情快照)
  2. tv_screener.py 结果 (补充 ETF + 行业映射)
  3. latest.json / market_now.json (兜底)

用法:
  python3 build_search_index.py [--out data/search_index.json]
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paths import SNAPSHOT_GLOB, AI_VIEW_DATA


def _is_etf_code(code: str) -> bool:
    """判断裸码是否为 ETF"""
    bare = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    return bare.startswith(("15", "16", "50", "51", "56", "58", "159", "16"))


def _code_to_sym(bare_code: str) -> str:
    c6 = str(bare_code).zfill(6)
    if c6.startswith(("60", "68", "90")):
        return "sh" + c6
    if c6.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c6
    return "sz" + c6


def build_from_snapshots():
    out = {}
    # 1) JSON 快照 (全市场股票)
    snaps = sorted(glob.glob(SNAPSHOT_GLOB))
    if snaps:
        latest = snaps[-1]
        print(f"[search_index] 使用快照: {os.path.basename(latest)}")
        d = json.load(open(latest, encoding="utf-8"))
        for sym, rec in d.items():
            name = (rec.get("name") or "").strip()
            if not name:
                continue
            out[sym] = {
                "n": name,
                "p": round(rec.get("price", 0) or 0, 3),
                "chg": round(rec.get("chg_pct", 0) or 0, 2),
                "t": "etf" if _is_etf_code(sym) else "s",
            }
        print(f"[search_index] JSON 快照: {len(out)} 只")

    # 2) all_*.txt (含 ETF, 以「sh/sz」结尾行)
    #    格式: code|name|price|chg%|...|mkt | sh/sz/bj
    all_files = sorted(glob.glob(
        os.path.join(os.path.dirname(SNAPSHOT_GLOB.rstrip("*.json")), "all_*.txt"))
    ) if SNAPSHOT_GLOB else []
    # fallback: also check live market dir directly
    import os as _os
    market_dir = _os.path.join(_os.path.dirname(SNAPSHOT_GLOB.rstrip("*.json")))
    if not all_files:
        all_files = sorted(glob.glob(_os.path.join(market_dir, "all_*.txt")))
    if all_files:
        with open(all_files[-1], encoding="utf-8") as f:
            lines = [l.strip() for l in f if l[0:1].isdigit()]
        added = 0
        for line in lines:
            parts = line.split("|")
            if len(parts) < 3:
                continue
            bare_code = parts[0]
            name = parts[1]
            if not name:
                continue
            try:
                price = float(parts[2])
            except (ValueError, IndexError):
                price = 0.0
            try:
                chg = float(parts[3])
            except (ValueError, IndexError):
                chg = 0.0
            # 最后一列是前缀 (sh/sz/bj)
            prefix = parts[-1].strip() if parts[-1].strip() in ("sh", "sz", "bj") else "sz"
            sym = prefix + bare_code.zfill(6)
            if sym not in out:
                out[sym] = {
                    "n": name, "p": price, "chg": chg,
                    "t": "etf" if _is_etf_code(sym) else "s",
                }
                added += 1
        etf_count = sum(1 for v in out.values() if v["t"] == "etf")
        print(f"[search_index] all_*.txt 补充: +{added} 只 (ETF 共 {etf_count})")
    return out


def build_from_tv():
    """从 tv_screener.py 的输出中补充 ETF 数据。"""
    # 查找 TV 输出文件
    candidates = glob.glob(os.path.join(AI_VIEW_DATA, "industry_map_tv.json")) + \
                 glob.glob(os.path.join(AI_VIEW_DATA, "*.json"))
    out = {}
    for fpath in candidates:
        if not os.path.isfile(fpath):
            continue
        try:
            d = json.load(open(fpath, encoding="utf-8"))
        except Exception:
            continue
        # industry_map_tv.json 格式: {code: {name, sector, industry, type, subtype, ...}}
        if "meta" in d and "sector_ranking" in d.get("meta", {}):
            continue  # skip meta files
        items = d if isinstance(d, list) else d.get("data", [])
        if isinstance(items, list):
            for item in items:
                name = item.get("name", "")  # TV 用 name 存代码
                bare = name
                if not bare:
                    continue
                sym = _code_to_sym(bare)
                desc = item.get("description", "")
                close = item.get("close", 0)
                change = item.get("change", 0)
                typ = item.get("type", "")
                subtype = item.get("subtype", "")
                is_etf = typ == "etf" or subtype == "etf" or _is_etf_code(sym)
                out[sym] = {
                    "n": desc or bare,
                    "p": round(close, 3) if close else 0,
                    "chg": round(change, 2) if change else 0,
                    "t": "etf" if is_etf else "s",
                }
    if out:
        print(f"[search_index] TV 补充索引: {len(out)} 只")
    return out


def build_from_latest():
    """兜底: 从 latest.json / market_now.json 提取可见代码"""
    base = AI_VIEW_DATA
    out = {}

    for fname in ("latest.json", "market_now.json"):
        p = os.path.join(base, fname)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue

        # 指数
        for idx in (d.get("market", {}).get("indices") or []):
            sym = idx.get("code", "")
            if sym and sym not in out:
                out[sym] = {"n": idx.get("name", sym), "p": idx.get("price", 0),
                            "chg": idx.get("chg_pct", 0), "t": "s"}

        # 涨跌停池 (代码是裸码)
        for pool_key in ("limit_up_pool", "limit_down_pool"):
            for zt in (d.get(pool_key) or []):
                code = zt.get("code", "")
                if not code:
                    continue
                sym = _code_to_sym(code)
                if sym not in out:
                    out[sym] = {"n": zt.get("name", code), "p": zt.get("price", 0),
                                "chg": zt.get("chg_pct", 0), "t": "s"}

        # 强势股
        for s in (d.get("ths_strong") or []):
            code = s.get("code", "")
            if not code:
                continue
            sym = _code_to_sym(code)
            if sym not in out:
                out[sym] = {"n": s.get("name", code),
                            "p": s.get("price", s.get("p", 0)),
                            "chg": s.get("chg_pct", s.get("pct", 0)),
                            "t": "etf" if _is_etf_code(sym) else "s"}

    if out:
        print(f"[search_index] latest/market_now 兜底: {len(out)} 只")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "search_index.json"))
    args = ap.parse_args()

    idx = build_from_snapshots()
    if not idx:
        idx = build_from_tv()
    if not idx:
        idx = build_from_latest()

    if not idx:
        print("[search_index] 无数据可用, 跳过")
        return 1

    # 合并 TV 补充的 ETF 数据（如有）
    tv_idx = build_from_tv()
    if tv_idx:
        for sym, v in tv_idx.items():
            if sym not in idx:
                idx[sym] = v

    total = len(idx)
    etf_count = sum(1 for v in idx.values() if v.get("t") == "etf")
    print(f"[search_index] 最终: {total} 只 (股票 {total-etf_count}, ETF {etf_count})")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[search_index] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
