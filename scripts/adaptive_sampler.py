#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/adaptive_sampler.py ═══
# 上游 commit: adfbfe915bd8 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""adaptive_sampler.py — 动态分层采样采集调度 (独特算法: 市场注意力模型)
原理: 全市场4000+票1分钟采集不现实; 用"注意力分数"动态分层:
  S = 0.4*|涨幅|z + 0.3*量比z + 0.2*涨停基因(近5日涨停=1) + 0.1*RPS
  Tier1(前80) 每分钟采 / Tier2(前300) 每5分 / Tier3 其余 每30分
输出: data/sampler_tiers.json (minute1_collect读取并按tier调度)
"""
import json, os, urllib.request, datetime, math, duckdb

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
STOCKDB = os.environ.get("STOCKS_STOCKDB") or os.path.join(
    os.path.expanduser("~"), "stockdb")
OUT = os.path.join(STOCKDB, "data", "sampler_tiers.json")
STOCK_DB = os.path.join(STOCKDB, "stock.duckdb")

def qt_batch(codes):
    """腾讯qt批量行情"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i+60]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            txt = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read().decode("gbk", "ignore")
        except Exception:
            continue
        for line in txt.split(";"):
            if "=" not in line: continue
            f = line.split("=")[1].strip('"').split("~")
            if len(f) < 50: continue
            sym = ("sh" if f[0] == "1" else "sz") + f[2]
            out[sym] = {"name": f[1], "pct": float(f[32] or 0), "price": float(f[3] or 0), "vratio": float(f[49] or 0) if len(f) > 49 else 0}
    return out

def main():
    if not os.path.exists(STOCK_DB):
        print(f"stock.duckdb not found at {STOCK_DB}; sampler needs the local DB")
        return
    con = duckdb.connect(STOCK_DB, read_only=True)
    # 涨停基因: 近5日涨停(主板10%)
    gene = {r[0] for r in con.execute("""
        SELECT symbol FROM (
          SELECT symbol, date, close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS ret
          FROM daily WHERE date >= (SELECT strftime(MAX(date)::DATE - 7, '%Y-%m-%d') FROM daily))
        WHERE ret >= 0.095 GROUP BY symbol""").fetchall()}
    # RPS top池(简化: 近10日涨幅前300)
    rps = con.execute("""
        SELECT symbol, ret FROM (
          SELECT symbol, MAX(CASE WHEN rn=1 THEN close END)/MAX(CASE WHEN rn=10 THEN close END)-1 AS ret
          FROM (SELECT symbol, close, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
                FROM daily WHERE date >= (SELECT strftime(MAX(date)::DATE - 30, '%Y-%m-%d') FROM daily))
          GROUP BY symbol HAVING ret IS NOT NULL)
        WHERE symbol NOT LIKE '%30%' AND symbol NOT LIKE '%68%'
        ORDER BY ret DESC LIMIT 500""").fetchall()
    con.close()
    syms = [r[0] for r in rps]
    rps_map = {r[0]: r[1] for r in rps}
    quotes = qt_batch(syms)
    # 注意力分数
    scored = []
    for s, q in quotes.items():
        z_rank = rps_map.get(s, 0)
        score = 0.4 * abs(q["pct"]) / 10 + 0.3 * min(q["vratio"], 10) / 10 + 0.2 * (1 if s in gene else 0) + 0.1 * max(z_rank, 0) * 10
        scored.append((round(score, 4), s, q["name"], q["pct"]))
    scored.sort(reverse=True)
    tiers = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "t1_1min": [{"symbol": s, "name": n, "score": sc, "pct": p} for sc, s, n, p in scored[:80]],
        "t2_5min": [{"symbol": s, "name": n, "score": sc} for sc, s, n, p in scored[80:300]],
        "t3_30min_count": len(scored) - 300,
        "stats": {"scanned": len(scored), "limitup_gene": len(gene)},
    }
    json.dump(tiers, open(OUT, "w"), ensure_ascii=False)
    top = tiers["t1_1min"][:5]
    print("T1前5:", [(t["symbol"], t["name"], t["score"]) for t in top])

if __name__ == "__main__":
    main()
