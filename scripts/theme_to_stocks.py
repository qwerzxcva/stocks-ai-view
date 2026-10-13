#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/theme_to_stocks.py ═══
# 上游 commit: fcd811f3fc55 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (news_map → data/hot_impact.json; duckdb → DUCKDB_PATH; --out 落盘)
"""
题材 → 库内个股映射 (结合我们的数据: RPS强势+近期涨停基因+主板过滤)
用法: python3 theme_to_stocks.py   (读 data/hot_impact.json 的热点题材)
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, DUCKDB_PATH
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    DUCKDB_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "stocks-data-live", "data", "stocks.duckdb")

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


def load_themes(data_dir):
    """热点题材来源: hot_impact.json (本项目热点推演产物) 优先, 退化 news_map json。"""
    themes = []
    hp = os.path.join(data_dir, "hot_impact.json")
    if os.path.exists(hp):
        try:
            d = json.load(open(hp, encoding="utf-8"))
            for r in (d.get("reports") or []):
                th = r.get("theme")
                if th:
                    themes.append((th, r.get("news_count") or r.get("heat") or 0))
        except Exception:
            pass
    if not themes:
        files = sorted(f for f in glob.glob(os.path.join(data_dir, "news_map", "*.json"))
                       if "_stocks" not in f)
        if files:
            try:
                rep = json.load(open(files[-1], encoding="utf-8"))
                themes = list((rep.get("theme_hits") or {}).items())
            except Exception:
                themes = []
    return themes[:6]


def main():
    ap = argparse.ArgumentParser(description="题材 → 库内个股映射")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "theme_to_stocks.json"))
    args = ap.parse_args()

    themes = load_themes(DATA_DIR)
    if not themes:
        print("no themes: 需要 data/hot_impact.json 或 data/news_map/*.json", file=sys.stderr)
        return 1

    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    # 库内全市场名称 + 60日动量 (表名/列名按本项目 db_layer: kline_daily.code)
    base = con.execute("""
        WITH d AS (SELECT code, date, close FROM kline_daily),
        last AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn FROM d)
        SELECT a.code AS symbol, a.close, a.close/NULLIF(b.close,0)-1 AS r60
        FROM last a JOIN last b ON a.code=b.code AND b.rn=60
        WHERE a.rn=1
    """).fetchdf()
    con.close()
    if base.empty:
        print("kline_daily 无数据: 需先跑 db_layer --load 导入日K", file=sys.stderr)
        return 1

    print("=== 热点题材 → 主板映射(60日动量Top) ===")
    out = {}
    for theme, cnt in themes:
        pats = THEME_STEM.get(theme)
        if not pats:
            continue
        pat = "|".join(pats)
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

    payload = {"ts": datetime.now().isoformat(timespec="seconds"),
               "source": "data/hot_impact.json 热点题材 × kline_daily 60日动量 × 腾讯qt名称",
               "themes": {k: [{"symbol": s, "name": n, "r60": r} for s, n, r in v]
                          for k, v in out.items()}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("\nsaved:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
