#!/usr/bin/env python3
"""
build_mainlines.py — 从 news_impact + hot_impact 构建「今日主线」与「热点主题」

网站 index.html 的两个 tab 原先读 data/mainline.json 与 data/hot_themes.json,
但仓库里从未产出过这两个文件(也没有对应采集脚本), 所以那两个 tab 一直是空的。
这里从已有数据里提炼, 不新增外部依赖:

  - 热点主题 (hot_themes.json): 按产业链/题材聚合 news_impact 的映射结果,
    统计新闻条数、多空方向、关联个股。
  - 今日主线 (mainline.json): 按「新闻热度 × 方向一致性 × 关联股数」排序,
    输出主线名/来源市场/热度/代表股/逻辑; 明日主线预测给简版推演。

用法:
  python3 scripts/build_mainlines.py --news data/news_impact.json \
      --hot data/hot_impact.json --out-themes data/hot_themes.json \
      --out-mainline data/mainline.json
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# 这些「主题」是资讯流的栏目名, 不是题材, 聚合时要剔掉
COLUMN_THEMES = {
    "A股7×24快讯直播", "股市重要公告提醒", "公司财报大集合",
    "A股动态滚动播报", "这些股票业绩预增", "港股快讯",
    "又有哪些公司被大股东减持", "A股增持信息都在这里",
    "港美股", "国际要闻", "宏观经济",
}

# 产业链中文名 -> 展示名(与 hot_impact 的 theme 对齐)
INDUSTRY_LABEL = {
    "人工智能": "人工智能", "计算机": "计算机", "金融": "金融",
    "半导体": "半导体", "有色": "有色金属", "军工": "军工",
    "医药": "医药", "汽车": "汽车", "消费": "消费", "化工": "化工",
}

THEME_BENEFICIARIES = {
    "低空经济": [("688297", "中无人机"), ("000099", "中信海直"), ("002085", "万丰奥威"), ("600178", "东安动力")],
    "军工": [("002933", "新兴装备"), ("300581", "晨曦航空"), ("601606", "长城军工"), ("002683", "广东宏大")],
    "机器人": [("300024", "机器人"), ("603278", "大业股份"), ("002889", "东方嘉盛"), ("603197", "保隆科技")],
    "半导体": [("688981", "中芯国际"), ("002049", "紫光国微"), ("603986", "兆易创新"), ("688012", "中微公司")],
    "人工智能": [("002230", "科大讯飞"), ("603019", "中科曙光"), ("000977", "浪潮信息"), ("688111", "金山办公")],
    "汽车": [("000550", "江铃汽车"), ("600741", "华域汽车"), ("000887", "中鼎股份"), ("601127", "赛力斯")],
    "金融": [("000001", "平安银行"), ("601318", "中国平安"), ("600036", "招商银行"), ("601398", "工商银行")],
}


def load(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("[mainline] 读取失败 %s: %s" % (path, e))
        return {}


def theme_from_impact(imp):
    """取一条新闻的题材标签: 优先概念类 themes, 退回 match_detail 的产业链。"""
    themes = [t for t in (imp.get("themes") or []) if t not in COLUMN_THEMES]
    if themes:
        return themes[0]
    md = imp.get("match_detail") or ""
    if md.startswith("industry:"):
        return md.split(":", 1)[1]
    if md.startswith("题材「"):
        # 题材「航天概念股」→产业链「军工」
        try:
            return md.split("→产业链「", 1)[1].rstrip("」")
        except IndexError:
            return ""
    return ""


def build_themes(news, hot, extra=None, hot_news=None):
    impacts = list((news or {}).get("impacts") or [])
    for item in (extra or {}).get("items") or []:
        impacts.append({"title": item.get("title"), "themes": item.get("themes") or [],
                        "direction": "neutral", "stocks": [{"code": c} for c in item.get("codes") or []]})
    for item in (hot_news or {}).get("items") or []:
        if item.get("group"):
            impacts.append({"title": item.get("title"), "themes": [item.get("group")], "direction": "neutral", "stocks": []})
    buckets = defaultdict(lambda: {
        "news": 0, "bull": 0, "bear": 0, "neutral": 0,
        "stocks": {}, "titles": [],
    })
    for imp in impacts:
        t = theme_from_impact(imp)
        if not t:
            continue
        b = buckets[t]
        b["news"] += 1
        d = imp.get("direction")
        if d in ("bull", "bear", "neutral"):
            b[d] += 1
        for s in (imp.get("stocks") or [])[:10]:
            code = s.get("code")
            if code:
                b["stocks"].setdefault(code, s.get("name") or code)
        if len(b["titles"]) < 5:
            b["titles"].append(imp.get("title") or "")

    themes = []
    for name, b in buckets.items():
        n = b["news"]
        bull, bear = b["bull"], b["bear"]
        # 方向: 多空差额占总量的比例
        net = (bull - bear) / n if n else 0.0
        direction = "bull" if net > 0.2 else ("bear" if net < -0.2 else "neutral")
        themes.append({
            "name": name,
            "count": n,
            "news_count": n,
            "bull": bull,
            "bear": bear,
            "direction": direction,
            "confidence": round(min(1.0, n / 10.0), 2),
            "codes": list(b["stocks"].keys())[:8],
            "stocks": [{"code": c, "name": nm} for c, nm in
                       list(b["stocks"].items())[:8]],
            "titles": b["titles"],
        })
    themes.sort(key=lambda x: (-x["news_count"], -abs(x["bull"] - x["bear"])))

    # hot_impact 的推演结论作为补充(带预期区间与理由)
    hot_reports = []
    for r in (hot or {}).get("reports") or []:
        known = {str(s.get("code", "")).replace("sh", "").replace("sz", "").replace("bj", ""): s
                 for s in (r.get("stocks") or [])}
        for code, name in THEME_BENEFICIARIES.get(r.get("theme"), []):
            known.setdefault(code, {"code": code, "name": name, "signal": "主题映射", "why": "主题受益股映射表"})
        hot_reports.append({
            "theme": r.get("theme"),
            "heat": r.get("heat"),
            "news_count": r.get("news_count"),
            "stocks": [
                {
                    "code": s.get("code"),
                    "name": s.get("name"),
                    "signal": s.get("signal"),
                    "expect": s.get("expect"),
                    "why": s.get("why"),
                }
                for s in known.values()
            ],
        })

    return {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "build_mainlines v1 (news_impact 聚合)",
        "source": "data/news_impact.json + data/hot_impact.json",
        "n_themes": len(themes),
        "themes": themes,
        "hot_reports": hot_reports,
        "caveat": "主题由新闻映射结果聚合, 热度=新闻条数; 多空为规则式判定, 仅供研究参考",
    }


def build_mainline(themes_doc, news):
    """今日主线: 用主题的热度/方向/关联股排序。"""
    themes = themes_doc.get("themes") or []
    impacts = (news or {}).get("impacts") or []
    n_news = len(impacts)

    mainlines = []
    for t in themes:
        if t["news_count"] < 1:
            continue
        heat = round(min(1.0, t["news_count"] / 8.0), 2)
        # 来源市场: 映射结果全部来自 A股/产业链, 海外单列
        market = "A股"
        logic = "相关新闻 %d 条, 多 %d / 空 %d" % (
            t["news_count"], t["bull"], t["bear"])
        if t["titles"]:
            logic += "; 代表: " + t["titles"][0][:40]
        mainlines.append({
            "name": t["name"],
            "market": market,
            "heat": heat,
            "news_count": t["news_count"],
            "direction": t["direction"],
            "codes": t["codes"],
            "stocks": t["codes"],
            "reason": logic,
            "logic": logic,
        })

    # 明日主线预测: 取当前最热且方向偏多的主题
    pred = []
    for t in themes:
        if t["direction"] in ("bull", "neutral") and t["news_count"] >= 2:
            pred.append({
                "name": t["name"],
                "confidence": t["confidence"],
                "basis": "当前热度 %d 条新闻, 方向 %s" % (
                    t["news_count"], t["direction"]),
            })
        if len(pred) >= 5:
            break

    return {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "build_mainlines v1 (news_impact 聚合)",
        "n_input_news": n_news,
        "mainlines": mainlines[:20],
        "tomorrow": pred,
        "caveat": "主线由新闻映射聚合得出, 非行情涨幅排名; 来源市场字段暂固定为A股(产业链映射只覆盖A股)",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--news", default="data/news_impact.json")
    ap.add_argument("--hot", default="data/hot_impact.json")
    ap.add_argument("--extra", default="data/news_extra.json")
    ap.add_argument("--hot-news", default="data/hot_news.json")
    ap.add_argument("--out-themes", default="data/hot_themes.json")
    ap.add_argument("--out-mainline", default="data/mainline.json")
    args = ap.parse_args()

    news = load(args.news)
    hot = load(args.hot)
    if not news.get("impacts"):
        print("[mainline] %s 无 impacts, 终止" % args.news)
        return 1

    themes_doc = build_themes(news, hot, load(args.extra), load(args.hot_news))
    mainline_doc = build_mainline(themes_doc, news)

    for path, doc in ((args.out_themes, themes_doc),
                      (args.out_mainline, mainline_doc)):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print("[mainline] -> %s" % path)

    print("[mainline] 主题 %d 个: %s" % (
        themes_doc["n_themes"],
        ", ".join("%s(%d)" % (t["name"], t["news_count"])
                  for t in themes_doc["themes"][:10])))
    print("[mainline] 今日主线 %d 条, 明日预测 %d 条" % (
        len(mainline_doc["mainlines"]), len(mainline_doc["tomorrow"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
