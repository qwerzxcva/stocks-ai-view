#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/theme_to_stocks.py ═══
# 上游 commit: fcd811f3fc55 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
题材 → 库内个股映射 (结合我们的数据: RPS强势+近期涨停基因+主板过滤)
用法: python3 theme_to_stocks.py   (读最新 news_map json)
"""
import duckdb, json, glob, os, re, time, urllib.request

THEME_STEM = {
    "券商": ["证券|券商"],
    "军工": ["航空|航天|军工|兵装|电子科技"],
    "半导体": ["半导|微电子|电子|科技"],
    "算力": ["科技|信息|数据|云"],
    "低空经济": ["航空|通用航空|无人机"],
    "新能源": ["能源|电力|新能|光伏"],
    "地产": ["地产|城建|建设|置业"],
    "消费": ["食品|百货|商业|消费"],
    "机器人": ["机电|智能|自动|精密"],
    "油气": ["石油|石化|能源|油"],
    "黄金": ["黄金|贵金属|有色"],
    "传媒": ["传媒|文化|出版|影视"],
    "电池": ["电池|能源|新能"],
    "信创": ["软件|信息|科技|系统"],
    "医药": ["医药|生物|制药|药"],
    "有色": ["有色|金属|铜|铝|锌"],
}

def main():
    import akshare as ak
    files = sorted(f for f in glob.glob("/home/agentuser/stockdb/data/news_map/*.json") if "_stocks" not in f)
    if not files:
        print("no news_map"); return
    rep = json.load(open(files[-1]))
    themes = list(rep.get("theme_hits", {}).items())[:6]
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)

    # 库内全市场名称 + RPS60 + 20日涨停基因
    base = con.execute("""
        WITH d AS (SELECT symbol, date, close, volume FROM daily),
        last AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn FROM d)
        SELECT a.symbol, a.close, a.close/NULLIF(b.close,0)-1 AS r60
        FROM last a JOIN last b ON a.symbol=b.symbol AND b.rn=60
        WHERE a.rn=1
    """).fetchdf()
    zt = con.execute("""
        SELECT symbol, COUNT(*) zt20 FROM (
          SELECT symbol, date, close/close-0+close/LAG(close) OVER (PARTITION BY symbol ORDER BY date)-1 AS r
          FROM daily WHERE date >= (SELECT MAX(date) FROM daily WHERE 1=1)
        ) WHERE r >= 0.095 GROUP BY symbol
    """).fetchdf()

    print("=== 热点题材 → 主板映射(60日动量Top) ===")
    out = {}
    for theme, cnt in themes:
        pats = THEME_STEM.get(theme)
        if not pats:
            continue
        pat = "|".join(pats)
        # 名称匹配: 需要股票名称 — 暂用 akshare 现货清单缓存
        # 名称映射: 腾讯qt批量(东财spot被墙) — 50只/批
        name_map = {}
        codes = [r["symbol"] for r in base.to_dict("records")]
        for i in range(0, len(codes), 50):
            batch = codes[i:i + 50]
            try:
                u = "https://qt.gtimg.cn/q=" + ",".join(batch)
                body = urllib.request.urlopen(urllib.request.Request(
                    u, headers={"Referer": "https://gu.qq.com"}), timeout=10).read().decode("gbk", "ignore")
                for line in body.strip().split(";"):
                    if "~" in line:
                        p = line.split("~")
                        if len(p) > 2:
                            name_map[p[2]] = p[1]
            except Exception:
                pass
            time.sleep(0.2)
        hits = []
        for _, row in base.iterrows():
            sym = row["symbol"]
            code = sym[2:]
            nm = name_map.get(code, "")
            if nm and re.search(pat, nm):
                hits.append((sym, nm, round(row["r60"] or 0, 3)))
        hits.sort(key=lambda x: -x[2])
        top = hits[:8]
        out[theme] = top
        print(f"\n[{theme}] 快讯{cnt}条 候选:")
        for sym, nm, r in top:
            print(f"  {sym} {nm:8s} 60日动量 {r:+.1%}")
    con.close()
    json.dump(out, open(files[-1].replace(".json", "_stocks.json"), "w"), ensure_ascii=False, indent=1)
    print("\nsaved:", files[-1].replace(".json", "_stocks.json"))

if __name__ == "__main__":
    main()
