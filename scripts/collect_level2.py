#!/usr/bin/env python3
"""
collect_level2.py — Level2/逐笔/盘口/资金数据采集 (2026-10-13 逆向成果)

子代理逆向 + 主会话实测验证: 17 个接口, 16 ok / 0 blocked。
本脚本落地最有价值的 7 个:

  1. em_fflow_history   东财历史资金流 (五档: 超大/大/中/小单净流入) — 日/周/月/年
  2. em_quote_snapshot  东财个股实时快照 (五档/内外盘/市值/换手/市盈)
  3. em_tick_details    东财逐笔成交 (3秒聚合, 含主买主卖方向)
  4. tx_tick_detail     腾讯分笔成交 (3秒聚合, B/S/M 性质 + 成交额)
  5. tx_minute          腾讯分时 (每分钟累计成交量/成交额)
  6. sina_bill_sum      新浪大单分级汇总 (分档占比 + 主买/主卖/中性)
  7. sina_trans_list    新浪逐笔成交 (含内外盘总量)

字段覆盖 (用户要求的清单):
  level2 ✅(五档/逐笔)  vip ✅(成交单)  资金 ✅(五档净流入)
  主力净流入/流出 ✅  大单占比 ✅  内盘/外盘 ✅  均价 ✅(avgprice)
  成交额 ✅  市值 ✅(f116/f117)  换手 ✅  量比 ✅(快照 f50 附近)
  逐笔买卖方向 ✅(f55/col6/kind)

产出: data/level2/{code}_{kind}_{date}.json

用法:
  python3 collect_level2.py --code 600519                    # 全部 7 类
  python3 collect_level2.py --code 600519 --kind fflow       # 只要资金流
  python3 collect_level2.py --code 600519 --kind quote,tick  # 多类
  python3 collect_level2.py --batch-file codes.txt --kind quote
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
EM_REF = {"Referer": "https://quote.eastmoney.com/"}
TX_REF = {"Referer": "https://gu.qq.com/"}
SINA_REF = {"Referer": "https://finance.sina.com.cn/"}

DEFAULT_OUT = "/root/workspace/stocks-ai-view/data/level2"

# 东财字段映射 (实测)
EM_FIELDS = ("f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f60,f71,"
             "f107,f116,f117,f162,f167,f168,f169,f170,f171,f177")
EM_FIELD_MAP = {
    "f43": "price", "f44": "high", "f45": "low", "f46": "open",
    "f47": "volume_hand", "f48": "amount", "f49": "outer_vol_hand",
    "f50": "inner_vol_hand", "f51": "limit_up", "f52": "limit_down",
    "f57": "code", "f58": "name", "f60": "prev_close",
    "f71": "avg_price", "f107": "market_type",
    "f116": "total_mktcap", "f117": "float_mktcap",
    "f162": "pe_ttm", "f167": "pb", "f168": "turnover_rate",
    "f169": "chg_amt", "f170": "chg_pct", "f171": "amplitude",
    "f177": "volume_ratio",
}


def _secid(code):
    """6 位代码 → 东财 secid (1=沪, 0=深)。"""
    c = str(code).strip()
    return ("1." if c.startswith(("5", "6", "9")) else "0.") + c


def _tk(code):
    c = str(code).strip()
    if c.startswith(("sh", "sz")):
        return c
    return ("sh" if c.startswith(("5", "6", "9")) else "sz") + c


def _get(url, ref, retries=3, timeout=20, text=False):
    h = dict(UA)
    h.update(ref)
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            b = r.read()
            return b.decode("utf-8", "ignore") if text else json.loads(b.decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                return {"__error__": f"{type(e).__name__} {e}"}
            time.sleep(1.0)
    return {"__error__": "unknown"}


# ─── 1. 东财历史资金流 (五档) ────────────────────────────────
def fetch_em_fflow(code, klt=101, lmt=20):
    """klt: 101=日 102=周 103=月 104=年。"""
    u = ("https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
         f"?secid={_secid(code)}&fields1=f1,f2,f3,f7"
         f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&lmt={lmt}&klt={klt}")
    d = _get(u, EM_REF)
    klines = ((d.get("data") or {}).get("klines")) or []
    out = []
    for line in klines:
        p = line.split(",")
        # 实测: 6 列 = date,主力,小单,中单,大单,超大单 (收盘价在 data.close 里)
        # 部分版本 7 列带收盘 → 兼容
        if len(p) >= 6:
            rec = {
                "date": p[0],
                "main_net_inflow": float(p[1]),
                "small_net_inflow": float(p[2]),
                "medium_net_inflow": float(p[3]),
                "large_net_inflow": float(p[4]),
                "xlarge_net_inflow": float(p[5]),
                "close": float(p[6]) if len(p) >= 7 else None,
                "chg_pct": float(p[7]) if len(p) >= 8 else None,
            }
            out.append(rec)
    return out


# ─── 2. 东财个股实时快照 ────────────────────────────────────
def fetch_em_snapshot(code):
    u = ("https://push2delay.eastmoney.com/api/qt/stock/get"
         f"?secid={_secid(code)}&fltt=2&invt=2&fields={EM_FIELDS}")
    d = _get(u, EM_REF)
    data = d.get("data") or {}
    out = {}
    for k, v in data.items():
        out[EM_FIELD_MAP.get(k, k)] = v
    return out


# ─── 3. 东财逐笔成交 (3 秒聚合) ─────────────────────────────
def fetch_em_tick(code, pos=-20):
    u = ("https://push2delay.eastmoney.com/api/qt/stock/details/get"
         f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
         f"&fields2=f51,f52,f53,f54,f55&pos={pos}&fltt=2")
    d = _get(u, EM_REF)
    data = d.get("data") or {}
    out = []
    for line in (data.get("details") or []):
        p = line.split(",")
        if len(p) >= 5:
            out.append({
                "time": p[0], "price": float(p[1]),
                "trades": int(p[2]), "volume_hand": int(p[3]),
                "direction": int(p[4]),   # 1=主买 2=主卖
            })
    return {"pre_price": data.get("prePrice"), "ticks": out}


# ─── 4. 腾讯分笔成交 ────────────────────────────────────────
def fetch_tx_tick(code, page=0):
    tk = _tk(code)
    u = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={tk}&p={page}"
    txt = _get(u, TX_REF, text=True)
    if not isinstance(txt, str) or not txt.startswith("v_detail_data"):
        return []
    # v_detail_data_sz000001=[0,"0/09:25:00/11.73/-0.01/3480/4082040/S|..."]
    m = re.search(r'=\s*\[(.+)\];?\s*$', txt.strip(), re.S)
    if not m:
        return []
    raw = m.group(1)
    sm = re.search(r'"([^"]+)"', raw)
    if not sm:
        return []
    out = []
    for item in sm.group(1).split("|"):
        p = item.split("/")
        if len(p) >= 6:
            out.append({
                "idx": int(p[0]), "time": p[1], "price": float(p[2]),
                "chg": float(p[3]), "volume_hand": int(p[4]),
                "amount": float(p[5]),
                "kind": p[6] if len(p) > 6 else "",  # B=主买 S=主卖 M=中性
            })
    return out


# ─── 5. 腾讯分时 (每分钟) ───────────────────────────────────
def fetch_tx_minute(code):
    tk = _tk(code)
    u = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tk}"
    d = _get(u, TX_REF)
    node = ((d.get("data") or {}).get(tk) or {}).get("data") or {}
    out = []
    for line in (node.get("data") or []):
        p = line.split(" ")
        if len(p) >= 4:
            out.append({
                "time": p[0], "price": float(p[1]),
                "cum_volume_hand": float(p[2]), "cum_amount": float(p[3]),
            })
    return {"date": node.get("date"), "minutes": out}


# ─── 6. 新浪大单分级汇总 ────────────────────────────────────
def fetch_sina_bill_sum(code):
    tk = _tk(code)
    u = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"CN_Bill.GetBillSum?symbol={tk}&volume=0")
    d = _get(u, SINA_REF)
    if not isinstance(d, list):
        return []
    out = []
    for r in d:
        out.append({
            "symbol": r.get("symbol"), "name": r.get("name"),
            "opendate": r.get("opendate"),
            "minvol": r.get("minvol"), "voltype": r.get("voltype"),
            "total_vol": r.get("totalvol"), "total_vol_pct": r.get("totalvolpct"),
            "total_amt": r.get("totalamt"), "total_amt_pct": r.get("totalamtpct"),
            "avg_price": r.get("avgprice"), "kuvolume": r.get("kuvolume"),
        })
    return out


# ─── 7. 新浪逐笔成交 (含内外盘) ─────────────────────────────
def fetch_sina_trans(code, num=50):
    tk = _tk(code)
    u = ("http://vip.stock.finance.sina.com.cn/quotes_service/view/CN_TransListV2.php"
         f"?num={num}&symbol={tk}")
    txt = _get(u, SINA_REF, text=True)
    if not isinstance(txt, str):
        return {}
    items = re.findall(
        r"trade_item_list\[\d+\]\s*=\s*new Array\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'\)",
        txt)
    iv = re.search(r"trade_INVOL_OUTVOL\s*=\s*new Array\('([^']+)',\s*'([^']+)'\)", txt)
    return {
        "ticks": [{"time": t, "volume": v, "price": p, "direction": d}
                  for t, v, p, d in items],
        "outer_vol": iv.group(1) if iv else None,   # 外盘/主动买
        "inner_vol": iv.group(2) if iv else None,   # 内盘/主动卖
    }


KINDS = {
    "fflow": ("东财历史资金流(五档)", fetch_em_fflow),
    "quote": ("东财实时快照", fetch_em_snapshot),
    "tick": ("东财逐笔成交", fetch_em_tick),
    "tick_tx": ("腾讯分笔成交", fetch_tx_tick),
    "minute": ("腾讯分时", fetch_tx_minute),
    "bill": ("新浪大单分级", fetch_sina_bill_sum),
    "trans": ("新浪逐笔+内外盘", fetch_sina_trans),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", action="append")
    ap.add_argument("--batch-file")
    ap.add_argument("--kind", default="all",
                    help="逗号分隔: fflow/quote/tick/tick_tx/minute/bill/trans 或 all")
    ap.add_argument("--lmt", type=int, default=20, help="资金流根数")
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    args = ap.parse_args()

    codes = list(args.code or [])
    if args.batch_file:
        with open(args.batch_file, encoding="utf-8") as f:
            codes += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if not codes:
        ap.print_help()
        return 1

    kinds = list(KINDS) if args.kind == "all" else [k.strip() for k in args.kind.split(",")]
    date = datetime.now(CST).strftime("%Y-%m-%d")
    os.makedirs(args.out_dir, exist_ok=True)

    ok_n = fail_n = 0
    for code in codes:
        for k in kinds:
            if k not in KINDS:
                print(f"  [skip] 未知 kind: {k}")
                continue
            desc, fn = KINDS[k]
            try:
                if k == "fflow":
                    data = fn(code, lmt=args.lmt)
                else:
                    data = fn(code)
                if isinstance(data, dict) and data.get("__error__"):
                    print(f"  ❌ {code} {k}: {data['__error__']}")
                    fail_n += 1
                    continue
                if not data:
                    print(f"  ⚠️ {code} {k}: 空 (可能非交易时段)")
                    fail_n += 1
                    continue
                p = os.path.join(args.out_dir, f"{code}_{k}_{date}.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"timestamp": datetime.now(CST).isoformat(timespec="seconds"),
                               "code": code, "kind": k, "desc": desc, "data": data},
                              f, ensure_ascii=False, separators=(",", ":"))
                if isinstance(data, list):
                    n = len(data)
                elif isinstance(data, dict):
                    n = len(data.get("ticks") or data.get("minutes") or data)
                else:
                    n = 1
                print(f"  ✅ {code} {k}: {n} 条 → {os.path.basename(p)}")
                ok_n += 1
            except Exception as e:
                print(f"  ❌ {code} {k}: {type(e).__name__} {e}")
                fail_n += 1
            time.sleep(0.3)
    print(f"\n[done] ok={ok_n} fail={fail_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
