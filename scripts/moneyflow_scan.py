#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/moneyflow_scan.py ═══
# 上游 commit: ac8e9ea942ec · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""资金流向采集 v1 (2026-10-12) — 新浪MoneyFlow接口(实测通, T-1至T日):
   个股主力净流入/超大单/大单, 给AI推荐池的票+市场快照TOP加资金流维度
   注意: 新浪该接口当日盘中有当日数据(部分延迟), 收盘后为最终值
"""
import json, os, datetime, urllib.request, gzip, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
BASE = "/home/agentuser/stockdb"

def _get(url):
    h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)", "Accept": "*/*", "Accept-Encoding": "gzip"}
    req = urllib.request.Request(url, headers=h)
    b = urllib.request.urlopen(req, timeout=15, context=ctx).read()
    if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")

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

def main():
    # 关注票: AI推荐池 + 三池 (从数据文件取)
    codes = set()
    try:
        ap = json.load(open(f"{BASE}/data/ai_picks.json"))
        codes |= {p["symbol"] for p in ap.get("picks", [])[:12]}
    except Exception: pass
    try:
        pools = json.load(open(f"{BASE}/data/pools.json"))
        for k in ("cycle", "lowpos"):
            sec = pools.get(k, {})
            for p in (sec.get("promotion_candidates") or sec.get("picks") or [])[:8]:
                codes.add(p["symbol"])
        for p in (pools.get("sector", {}) or {}).get("top", [])[:8]:
            codes.add(p["symbol"])
    except Exception: pass
    codes = sorted(codes)[:30]
    result = {"ts": datetime.datetime.now().isoformat(), "flows": {}}
    for sym in codes:
        rows = moneyflow_one(sym)
        if rows:
            today = rows[0] if rows else {}
            # 近3日主力净流入合计
            net3 = round(sum(r["net_main_wan"] for r in rows), 1)
            result["flows"][sym] = {**today, "net3_wan": net3}
    os.makedirs(f"{BASE}/data/moneyflow", exist_ok=True)
    json.dump(result, open(f"{BASE}/data/moneyflow/latest.json", "w"), ensure_ascii=False, indent=1)
    print(f"moneyflow: {len(result['flows'])}/{len(codes)} 票")

if __name__ == "__main__":
    main()
