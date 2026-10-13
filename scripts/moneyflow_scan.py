#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/moneyflow_scan.py ═══
# 上游 commit: ac8e9ea942ec · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (关注票源 stockdb/data → data/ 本项目产物; --out 落盘 data/moneyflow/latest.json)
"""
资金流向采集 v1 (2026-10-12) — 新浪MoneyFlow接口(实测通, T-1至T日):
   个股主力净流入/超大单/大单, 给AI推荐池的票+市场快照TOP加资金流维度
   注意: 新浪该接口当日盘中有当日数据(部分延迟), 收盘后为最终值

关注票来源 (本项目):
  data/ai_picks.json (build_ai_picks.py 产出, picks[].code)
  data/multi_v4.json / data/lowpool.json / data/sector_pool.json (三池候选)
"""
import argparse
import datetime
import gzip
import json
import os
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE


def _get(url):
    h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)", "Accept": "*/*", "Accept-Encoding": "gzip"}
    req = urllib.request.Request(url, headers=h)
    last = None
    for _ in range(3):   # 该接口偶发连接重置, 重试
        try:
            b = urllib.request.urlopen(req, timeout=15, context=ctx).read()
            if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
            return b.decode("utf-8", "ignore")
        except Exception as e:
            last = e
    raise last


def moneyflow_one(sym):
    """sym=sh603810 → [{date, close, pct, net_main(万), r0(万), turnover}] 近3日"""
    try:
        t = _get(f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb?page=1&num=3&sort=opendate&asc=0&daima={sym}")
        d = json.loads(t)
        return [{"date": x["opendate"], "close": float(x["trade"]),
                 "pct": round(float(x["changeratio"])*100, 2),
                 "net_main_wan": round(float(x["netamount"])/1e4, 1),
                 "r0_wan": round(float(x.get("r0", 0))/1e4, 1),
                 "turnover_wan": round(float(x["turnover"]), 1)} for x in d]
    except Exception:
        return []


def _codes_from(path, key, limit):
    """从 data/*.json 取股票代码, 兼容 code/symbol 字段与 sh/sz 前缀缺失。"""
    if not os.path.exists(path):
        return set()
    out = set()
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return out
    for p in (d.get(key) or [])[:limit]:
        if not isinstance(p, dict):
            continue
        c = p.get("code") or p.get("symbol") or ""
        if not c:
            continue
        c = str(c)
        if not c.startswith(("sh", "sz")):
            c = ("sh" if c.startswith("6") else "sz") + c
        out.add(c)
    return out


def collect_codes(data_dir):
    """关注票: AI推荐池 + 多维引擎头部 + 三池 (本项目 data/ 产物)。"""
    codes = set()
    codes |= _codes_from(os.path.join(data_dir, "ai_picks.json"), "picks", 12)
    codes |= _codes_from(os.path.join(data_dir, "multidim.json"), "top", 10)
    for name, key in [("multi_v4.json", "picks"), ("lowpool.json", "picks"),
                      ("sector_pool.json", "top"), ("ban_pick.json", "items")]:
        codes |= _codes_from(os.path.join(data_dir, name), key, 8)
    return sorted(codes)[:30]


def main():
    ap = argparse.ArgumentParser(description="资金流向采集 (新浪 MoneyFlow)")
    ap.add_argument("--data-dir", default=DATA_DIR, help="本项目 data 目录")
    ap.add_argument("--codes", default=None, help="逗号分隔代码 (默认从 data/ 产物收集)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "moneyflow", "latest.json"))
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = collect_codes(args.data_dir)
    if not codes:
        print("[moneyflow] 无关注票: 需先有 data/ai_picks.json 或 pools 产物", file=sys.stderr)
        return 1
    result = {"ts": datetime.datetime.now().isoformat(), "source": "sina MoneyFlow", "flows": {}}
    for sym in codes:
        rows = moneyflow_one(sym)
        if rows:
            today = rows[0] if rows else {}
            # 近3日主力净流入合计
            net3 = round(sum(r["net_main_wan"] for r in rows), 1)
            result["flows"][sym] = {**today, "net3_wan": net3}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"moneyflow: {len(result['flows'])}/{len(codes)} 票 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
