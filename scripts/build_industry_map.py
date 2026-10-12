#!/usr/bin/env python3
"""build_industry_map.py — 用东财 push2/stock/get 的 f127 字段构建 个股→行业 映射。

为什么需要:
  腾讯快照没有行业字段; universe.csv 第三列是市场(sh/sz)不是行业,
  fix_snapshot_industry.py 曾误把 'sh' 当行业填入, 导致 multi_screener_v5
  的行业匹配/板块加分全部失效 → 候选 0 (详见会话记录)。

数据源: https://push2.eastmoney.com/api/qt/stock/get  fields=f127 (行业)
输出: stocks-data-live/data/meta/industry_map.json
        {"600519": "酿酒行业", ...}

策略: 全市场 5300 只逐个查询太慢 (5300 请求)。改为:
  1) 先用东财行业板块接口拿 行业→成分股 (bulk, 少量请求)
  2) 反推 个股→行业
  若 (1) 被 WAF 拦, 降级为对候选池 (涨停池+关注池) 逐个查询。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://quote.eastmoney.com/"}
OUT = "/root/workspace/stocks-data-live/data/meta/industry_map.json"
UNIVERSE = "/root/workspace/stocks-data-kline/data/bulk/universe.csv"


def _http(url, timeout=12, retries=3):
    for i in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception:
            if i < retries - 1:
                time.sleep(0.4)
    return b""


def load_universe():
    """返回 [(secid, code)] — secid 形如 1.600519 / 0.000001"""
    if not os.path.exists(UNIVERSE):
        return []
    out = []
    with open(UNIVERSE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if not parts[0]:
                continue
            raw = parts[0].strip()
            code = raw[2:] if raw[:2] in ("sh", "sz", "bj") else raw
            mkt = "1" if raw.startswith("sh") else "0"
            out.append((f"{mkt}.{code}", code))
    return out


def fetch_industry_bulk():
    """方案1: 东财行业板块列表 → 行业→成分股, 反推个股→行业。
    接口: push2 clist fs=m:90+t:2 (申万二级行业), fields=f12(行业码) f14(行业名) f100(行业)
    注: 该接口在容器内常被 reset, 故仅作为首选, 失败则降级。
    """
    ind_map = {}
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m:90+t:2&fields=f12,f14")
    body = _http(url, timeout=10, retries=2)
    if not body:
        return {}
    try:
        d = json.loads(body.decode("utf-8"))
        diff = (d.get("data") or {}).get("diff") or []
        if not isinstance(diff, dict):
            return {}
        print(f"[bulk] industry list: {len(diff)} sectors")
        # 逐个行业取成分股
        for code, item in diff.items():
            ind_name = str(item.get("f14", "")).strip()
            if not ind_name:
                continue
            secid = f"90.{code}"
            u = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=500&po=1&np=1"
                 f"&fltt=2&invt=2&fid=f3&fs=b:{secid}&fields=f12")
            b = _http(u, timeout=10, retries=2)
            if not b:
                continue
            try:
                dd = json.loads(b.decode("utf-8"))
                mem = (dd.get("data") or {}).get("diff") or []
                if isinstance(mem, dict):
                    for c in mem.keys():
                        ind_map[str(c)] = ind_name
            except Exception:
                continue
            time.sleep(0.05)
    except Exception as e:
        print(f"[bulk] parse err: {e}", file=sys.stderr)
    return ind_map


def fetch_industry_per_stock(universe, limit=800):
    """方案2 (降级): 对候选逐个查 f127。800 只 ≈ 8 分钟。"""
    ind_map = {}
    n = 0
    for secid, code in universe:
        if n >= limit:
            break
        u = ("https://push2.eastmoney.com/api/qt/stock/get"
             f"?fltt=2&invt=2&fields=f57,f127&secid={secid}")
        b = _http(u, timeout=10, retries=1)
        if not b:
            continue
        try:
            d = json.loads(b.decode("utf-8"))
            data = d.get("data") or {}
            ind = str(data.get("f127", "") or "").strip()
            if ind:
                ind_map[code] = ind
                n += 1
        except Exception:
            continue
        if n % 100 == 0:
            print(f"  [per-stock] {n}/{limit}", flush=True)
    return ind_map


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    universe = load_universe()
    print(f"universe: {len(universe)} codes")

    ind_map = fetch_industry_bulk()
    if len(ind_map) < 500:
        print(f"[fallback] bulk only got {len(ind_map)}, switching to per-stock (limit 800)")
        ind_map = fetch_industry_per_stock(universe, limit=800)

    if not ind_map:
        print("ERROR: no industry data", file=sys.stderr)
        return 1

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(ind_map, f, ensure_ascii=False)
    print(f"\nDONE: {len(ind_map)} codes -> {OUT}")
    # 抽样展示
    for c in ["600519", "000001", "601127", "600825"]:
        if c in ind_map:
            print(f"  {c} -> {ind_map[c]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
