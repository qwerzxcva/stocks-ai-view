#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/news_map.py ═══
# 上游 commit: d76b18a22a14 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
新闻热点 → 个股映射 v2（多源降级）
源: 东财全球快讯(200条) + 财联社 + 新浪快讯；概念: 东财(挂)→同花顺列表
输出: ~/stockdb/data/news_map/{date}.json
"""
import akshare as ak
import json, os, re, datetime

OUT = "/home/agentuser/stockdb/data/news_map"
os.makedirs(OUT, exist_ok=True)

# 关键词→题材→我们库中映射(轻量词典, 硬逻辑层)
THEME_KW = {
    "人工智能|AI|大模型|算力|GPU|光模块": "算力",
    "机器人|人形机器人|减速器": "机器人",
    "半导体|芯片|光刻|晶圆|存储": "半导体",
    "黄金|避险|金价": "黄金",
    "油价|原油|OPEC|石油": "油气",
    "券商|并购|资本市场|IPO": "券商",
    "地产|房地产|房贷|楼市": "地产",
    "白酒|消费|食品": "消费",
    "新能源|光伏|锂电|储能|风电": "新能源",
    "军工|国防|军贸": "军工",
    "稀土|有色|铜|铝": "有色",
    "医药|创新药|疫苗|医疗": "医药",
    "传媒|游戏|影视|短剧": "传媒",
    "数据要素|东数西算|IDC": "数据要素",
    "低空经济|无人机|eVTOL": "低空经济",
    "固态电池|电池": "电池",
    "信创|国产替代|自主可控": "信创",
}

def match_daily_code(code: str, con) -> bool:
    sym = ("sh" if code.startswith("6") else "sz") + code
    try:
        return con.execute("SELECT 1 FROM daily WHERE symbol=? LIMIT 1", [sym]).fetchone() is not None
    except Exception:
        return False

def fetch_news_all():
    items = []
    for fn, cols in [("stock_info_global_em", ("标题", "摘要", "发布时间")),
                     ("stock_info_global_cls", ("标题", "内容", "发布时间")),
                     ("stock_info_global_sina", (None, "内容", "时间"))]:
        try:
            df = getattr(ak, fn)()
            for _, r in df.head(60).iterrows():
                title = str(r[cols[0]])[:80] if cols[0] else ""
                body = str(r[cols[1]])[:200]
                ts = str(r[cols[2]])[:20]
                text = title + body
                items.append({"src": fn, "time": ts, "title": title or body[:60], "text": text})
        except Exception as e:
            items.append({"src": fn, "error": str(e)[:60]})
    return items

def extract_themes(items):
    """快讯→题材命中计数"""
    hit = {}
    for it in items:
        if "error" in it:
            continue
        text = it["text"]
        for pat, theme in THEME_KW.items():
            if re.search(pat, text):
                hit[theme] = hit.get(theme, 0) + 1
                it.setdefault("themes", []).append(theme)
    return hit

def main():
    today = datetime.date.today().isoformat()
    report = {"date": today, "ts": datetime.datetime.now().isoformat()}

    news = fetch_news_all()
    themes = extract_themes(news)
    report["theme_hits"] = dict(sorted(themes.items(), key=lambda x: -x[1]))
    report["news"] = [n for n in news if "error" not in n][:80]
    report["source_errors"] = [n for n in news if "error" in n]

    print("=== 24h 快讯题材热度 ===")
    for t, c in report["theme_hits"].items():
        print(f"  {t}: {c} 条")
    print(f"新闻源: 东财/财联社/新浪 ok={len(report['news'])} err={len(report['source_errors'])}")

    out = f"{OUT}/{today}.json"
    json.dump(report, open(out, "w"), ensure_ascii=False, indent=1)
    print("saved:", out)

if __name__ == "__main__":
    main()
