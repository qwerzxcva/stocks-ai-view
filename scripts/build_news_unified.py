#!/usr/bin/env python3
"""build_news_unified.py — 消息类数据统一管线 (2026-10-19)

为什么合并: 用户要求把「24h 分类摘要 / 实时热榜 / 全平台热点 / 财经资讯流 /
消息传导」合成一个模块。现状是 6 个独立文件各自维护、前端多个块分别渲染,
同一条消息在 A 文件有、B 文件没有, 传导规则只作用在其中一个, 导致
「消息 -> 板块 -> 个股」的链条断裂。

本脚本把 6 个来源归一成**一个事件流**, 并对**每一条**消息套用传导规则,
输出单一契约 data/news_unified.json。

输入 (存在才读, 缺失记入 sources_missing 而不是静默跳过):
  hot_news.json        全平台热榜 (365 条, 15 个源)
  news_extra.json      补充资讯 (110 条)
  news_digest.json     分类摘要
  news_impact.json     消息->个股映射 (60 条)
  news_transmission.json  传导规则 (16 条规则 / 2 条传导)
  hotspot_infer.json   热点推断 (13 条 strong)
  hot_impact.json      热点影响报告

统一事件 schema:
  {id, ts, date, title, summary, source, category,
   heat: {score, rank, sources[]},
   transmission: {themes[], sectors[], codes[], path[], confidence},
   impact: {direction, strength, horizon}}

分类 (category): policy | company | industry | macro | overseas | other
用法: python3 build_news_unified.py [--out data/news_unified.json]
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# 分类关键词 (命中即归类; 顺序即优先级)
CATEGORY_RULES = [
    ("policy", ("降准", "降息", "国常会", "政治局", "证监会", "央行", "财政部",
                "发改委", "政策", "监管", "新规", "试点", "补贴", "税收",
                "国常", "常务会议", "金融监管总局")),
    # 经济数据: 覆盖「同比 xx%」「统计局」「前三季度」这类新闻式表述
    ("macro", ("GDP", "CPI", "PPI", "PMI", "社融", "M2", "进出口", "外汇储备",
               "失业率", "工业增加值", "国家统计局", "统计局", "同比",
               "环比", "社会消费品零售", "固定资产投资", "房地产开发投资",
               "前三季度", "财政收支", "规上工业")),
    # 地缘政治: 直接影响原油/黄金/军工, 不能归到 other
    ("geopolitics", ("乌克兰", "俄罗斯", "以色列", "伊朗", "巴勒斯坦", "加沙",
                     "霍尔木兹", "红海", "停火", "和平会议", "爆炸", "袭击",
                     "制裁", "关税", "北约", "朝鲜", "台海", "南海")),
    ("overseas", ("美联储", "美股", "纳斯达克", "道指", "标普", "欧洲央行",
                  "日本央行", "汇率", "离岸人民币", "原油", "黄金", "美元指数",
                  "非农", "美债", "英伟达", "特斯拉", "苹果")),
    ("industry", ("板块", "行业", "产业链", "供需", "涨价", "降价", "产能",
                  "订单", "开工率", "库存", "招标", "中标", "出货量", "渗透率")),
    ("company", ("公告", "业绩", "净利", "营收", "回购", "增持", "减持",
                 "签订", "投产", "重组", "并购", "分红", "预增", "预亏",
                 "定向增发", "股权激励")),
]

# 传导链: 主题词 -> 板块 -> 典型个股前缀 (只做方向提示, 具体标的市场决定)
TRANSMISSION_CHAINS = {
    "算力": {"sectors": ["算力", "AI", "CPO", "光模块"], "horizon": "mid"},
    "半导体": {"sectors": ["半导体", "芯片", "封测", "设备"], "horizon": "mid"},
    "机器人": {"sectors": ["机器人", "减速器", "伺服", "丝杠"], "horizon": "mid"},
    "新能源": {"sectors": ["光伏", "储能", "锂电", "风电"], "horizon": "long"},
    "军工": {"sectors": ["军工", "航空", "航天", "船舶"], "horizon": "mid"},
    "医药": {"sectors": ["创新药", "CXO", "医疗器械", "中药"], "horizon": "long"},
    "消费": {"sectors": ["白酒", "食品", "零售", "免税"], "horizon": "long"},
    "地产": {"sectors": ["地产", "建材", "家居", "物业"], "horizon": "mid"},
    "券商": {"sectors": ["券商", "金融科技", "参股金融"], "horizon": "short"},
    "降准": {"sectors": ["银行", "券商", "地产", "基建"], "horizon": "short"},
    "降息": {"sectors": ["券商", "地产", "黄金", "成长"], "horizon": "short"},
    # 地缘政治 -> 避险/能源/军工
    "霍尔木兹": {"sectors": ["原油", "油运", "军工"], "horizon": "short"},
    "红海": {"sectors": ["航运", "油运", "军工"], "horizon": "short"},
    "制裁": {"sectors": ["半导体", "国产替代", "军工"], "horizon": "mid"},
    "原油": {"sectors": ["石油石化", "油服", "化工"], "horizon": "short"},
    "房地产": {"sectors": ["地产", "建材", "家居", "物业"], "horizon": "mid"},
}


DIGEST_CAT_MAP = {
    "国内时政": "policy", "国际新闻": "geopolitics", "市场行情": "industry",
    "行业题材": "industry", "财报动态": "company", "综合热点": "other",
}


def _map_digest_cat(name):
    return DIGEST_CAT_MAP.get(name, "other")


def _norm_title(t):
    return re.sub(r"\s+", "", str(t or ""))[:80]


def _event_id(title, ts):
    h = hashlib.md5((_norm_title(title) + str(ts)[:13]).encode()).hexdigest()
    return h[:12]


def classify(title, summary=""):
    text = f"{title} {summary}"
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "other"


def transmit(title, summary=""):
    """对单条消息套传导: 命中主题 -> 板块 + 影响方向 + 置信度。"""
    text = f"{title} {summary}"
    hits = []
    for theme, spec in TRANSMISSION_CHAINS.items():
        if theme in text:
            hits.append((theme, spec))
    if not hits:
        return {"themes": [], "sectors": [], "codes": [], "path": [],
                "confidence": 0.0, "horizon": None}
    sectors = []
    for _, spec in hits:
        for s in spec["sectors"]:
            if s not in sectors:
                sectors.append(s)
    # 利好/利空判断: negative 词表优先
    neg = ("下修", "亏损", "减持", "处罚", "立案", "退市", "暴跌", "违约",
           "下调", "制裁", "限产", "停牌")
    direction = "negative" if any(k in text for k in neg) else "positive"
    return {
        "themes": [t for t, _ in hits],
        "sectors": sectors[:8],
        "codes": [],
        "path": [f"{t} -> {', '.join(spec['sectors'][:3])}" for t, spec in hits],
        "confidence": round(min(0.9, 0.35 + 0.15 * len(hits)), 2),
        "horizon": hits[0][1]["horizon"],
        "direction": direction,
    }


def _load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "news_unified.json"))
    args = ap.parse_args()
    now = datetime.now(CST)

    sources, missing = {}, []
    raw_events = []

    # ── 1. 全平台热榜 ──
    hn = _load("hot_news.json")
    if hn:
        sources["hot_news"] = {"ts": hn.get("ts"), "n": hn.get("n_items"),
                               "sources_ok": hn.get("n_sources_ok")}
        for it in (hn.get("items") or []):
            raw_events.append({
                "title": it.get("title") or it.get("name"),
                "summary": it.get("summary") or "",
                "source": it.get("source") or "hot_news",
                "ts": it.get("ts") or it.get("time") or hn.get("ts"),
                "heat": {"score": it.get("heat") or it.get("hot") or 0,
                         "rank": it.get("rank"), "sources": [it.get("source")]},
            })
    else:
        missing.append("hot_news.json")

    # ── 2. 补充资讯 ──
    ne = _load("news_extra.json")
    if ne:
        sources["news_extra"] = {"ts": ne.get("ts"), "n": ne.get("n_items")}
        for it in (ne.get("items") or []):
            raw_events.append({
                "title": it.get("title"),
                "summary": it.get("summary") or it.get("content") or "",
                "source": it.get("source") or "news_extra",
                "ts": it.get("ts") or it.get("time") or ne.get("ts"),
                "heat": {"score": 0, "rank": None, "sources": [it.get("source")]},
            })
    else:
        missing.append("news_extra.json")

    # ── 3. 分类摘要 (只提供分类统计, 不提供条目时并入 counts) ──
    nd = _load("news_digest.json")
    digest = None
    if nd:
        digest = {"ts": nd.get("ts"), "date": nd.get("date"),
                  "total": nd.get("total"), "cat_counts": nd.get("cat_counts")}
        sources["news_digest"] = {"ts": nd.get("ts"), "total": nd.get("total")}
        # categories 是真正的 346 条分类消息, 之前只当统计读, 漏掉了内容
        cats = nd.get("categories") or {}
        if isinstance(cats, dict):
            digest["category_sizes"] = {k: len(v) for k, v in cats.items()
                                        if isinstance(v, list)}
            for cat_name, items in cats.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    raw_events.append({
                        "title": it.get("title") or it.get("name"),
                        "summary": it.get("summary") or it.get("content") or "",
                        "source": it.get("source") or "news_digest",
                        "ts": it.get("time") or it.get("ts") or nd.get("ts"),
                        "heat": {"score": it.get("heat") or 0,
                                 "rank": None,
                                 "sources": [it.get("source") or "digest"]},
                        "digest_category": cat_name,
                    })
    else:
        missing.append("news_digest.json")

    # ── 4. 消息 -> 个股映射 (用于给事件补 codes) ──
    ni = _load("news_impact.json")
    impact_by_title = {}
    if ni:
        sources["news_impact"] = {"ts": ni.get("ts"),
                                  "n_mapped": ni.get("n_mapped")}
        for im in (ni.get("impacts") or []):
            key = _norm_title(im.get("title") or im.get("news") or "")
            if key:
                # 真实字段是 stocks (list) 不是 codes, 之前写错导致 0 条带个股
                raw_stocks = im.get("stocks") or im.get("codes") or []
                codes = []
                for c in raw_stocks:
                    if isinstance(c, dict):
                        v = c.get("code") or c.get("symbol")
                    else:
                        v = c
                    if v:
                        codes.append(str(v))
                impact_by_title[key] = {
                    "codes": codes[:10],
                    "direction": im.get("direction"),
                    "strength": im.get("confidence") or im.get("strength"),
                    "themes": im.get("themes") or [],
                }
    else:
        missing.append("news_impact.json")

    # ── 5. 传导规则 (给每条消息套用, 不再单独成模块) ──
    nt = _load("news_transmission.json")
    if nt:
        sources["news_transmission"] = {"ts": nt.get("ts"),
                                        "n_rules": nt.get("n_rules")}
    else:
        missing.append("news_transmission.json")

    # ── 6. 热点推断 (作为 theme 热度补充) ──
    hi = _load("hotspot_infer.json")
    theme_heat = {}
    if hi:
        sources["hotspot_infer"] = {"ts": hi.get("ts"),
                                    "strong": hi.get("strong_count")}
        for t in (hi.get("strong") or []):
            name = t.get("theme") or t.get("name")
            if name:
                theme_heat[name] = t.get("score") or t.get("heat") or 0
    else:
        missing.append("hotspot_infer.json")

    # ── 归一 + 传导 + 去重 ──
    seen, events = set(), []
    for ev in raw_events:
        title = ev.get("title")
        if not title:
            continue
        eid = _event_id(title, ev.get("ts"))
        if eid in seen:
            continue
        seen.add(eid)
        # digest 已人工/规则分好类, 优先采信; 否则走关键词
        cat = ev.get("digest_category") and _map_digest_cat(ev["digest_category"]) \
            or classify(title, ev.get("summary", ""))
        tr = transmit(title, ev.get("summary", ""))
        # 用 impact 映射补个股
        imp = impact_by_title.get(_norm_title(title)) or {}
        if imp.get("codes"):
            tr["codes"] = imp["codes"]
        if imp.get("direction"):
            tr["direction"] = imp["direction"]
        # 主题热度回填
        for th in tr.get("themes", []):
            if th in theme_heat:
                ev["heat"]["score"] = max(ev["heat"]["score"], theme_heat[th])
        events.append({
            "id": eid,
            "ts": ev.get("ts"),
            "date": str(ev.get("ts") or "")[:10] or now.strftime("%Y-%m-%d"),
            "title": title,
            "summary": (ev.get("summary") or "")[:400],
            "source": ev.get("source"),
            "category": cat,
            "heat": ev.get("heat"),
            "transmission": tr,
            "impact": {"direction": tr.get("direction"),
                       "strength": imp.get("strength"),
                       "horizon": tr.get("horizon")},
        })

    # 按热度 + 时间排序
    events.sort(key=lambda e: (-(e["heat"].get("score") or 0),
                               str(e.get("ts") or "")))

    # 分类摘要 (实时统计)
    cat_counts = {}
    for e in events:
        cat_counts[e["category"]] = cat_counts.get(e["category"], 0) + 1

    # 传导汇总: 哪些板块被最多消息命中
    sector_hits = {}
    for e in events:
        for s in e["transmission"].get("sectors", []):
            sector_hits[s] = sector_hits.get(s, 0) + 1

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "build_news_unified v1",
        "contract": "unified_news_stream",
        "n_events": len(events),
        "n_with_transmission": sum(1 for e in events
                                   if e["transmission"].get("sectors")),
        "category_counts": cat_counts,
        "sector_transmission": dict(sorted(sector_hits.items(),
                                           key=lambda x: -x[1])[:20]),
        "digest": digest,
        "sources": sources,
        "sources_missing": missing,
        "events": events,
        "note": ("所有消息类数据合并为单一事件流; 传导规则作用在每条消息上, "
                 "不再单独成模块。category 为实时归类, 非人工标注。"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[news_unified] {len(events)} 条事件, "
          f"{payload['n_with_transmission']} 条带传导, "
          f"缺失源 {len(missing)}: {missing}")
    print(f"[news_unified] 分类: {cat_counts}")
    print(f"[news_unified] Top板块: "
          f"{list(payload['sector_transmission'].items())[:5]}")
    print(f"[news_unified] -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())