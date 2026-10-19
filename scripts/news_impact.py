#!/usr/bin/env python3
"""
news_impact.py — 热点→股票影响映射引擎

解决问题: 采到 362 条热点、110 条财经资讯, 但都是「死的标题列表」,
不告诉用户「这条热点影响哪些股票、利好还是利空」。

映射方法 (三层, 可解释):
  L1 题材词 → 概念板块: 从 ths_concept_list (375个概念) 匹配热点标题,
     命中则取该概念的成分股。
  L2 个股词 → 直接代码: 标题含股票名/简称/代码 → 直接关联。
  L3 产业链词 → 行业: 「钠离子电池」「光伏」「半导体」等产业链词 →
     映射到申万/新浪行业 → 行业成分股。

情感判定 (规则式, 不训练黑箱):
  利好词: 突破/获批/中标/预增/扭亏/回购/增持/降息/降准/扩大开放/减免/补贴
  利空词: 处罚/下滑/预亏/减持/退市/调查/加息/加税/限制/暂停/暴跌/违约
  中性: 其余 (或同时含利好+利空 → 中性, 标注「多空交织」)

输出 data/news_impact.json:
  {ts, n_news, n_mapped, impacts:[{title, source, time, direction,
   confidence, matched_by, stocks:[{code,name,industry}]}], summary}

用法:
  python3 news_impact.py --in data/hot_news.json --out data/news_impact.json
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# ─── 情感词典 (规则式) ────────────────────────────────────────
BULL_WORDS = [
    "突破", "获批", "批准", "中标", "预增", "扭亏", "为盈", "回购", "增持",
    "降息", "降准", "扩大开放", "减免", "补贴", "扶持", "利好", "创新高",
    "增长", "上涨", "提升", "扩大", "签署", "合作", "投产", "量产", "突破",
    "新高", "首次", "成功", "强劲", "超预期",
]
BEAR_WORDS = [
    "处罚", "罚款", "下滑", "预亏", "亏损", "减持", "退市", "调查", "立案",
    "加息", "加税", "限制", "暂停", "暴跌", "违约", "风险", "警示", "问询",
    "下滑", "下降", "减少", "失败", "终止", "中止", "叫停", "低于预期",
    "亏损", "下滑", "恶化", "造假", "违规",
]

# ─── 产业链词 → 行业 (L3) ────────────────────────────────────
CHAIN_MAP = {
    "半导体": ["半导体", "芯片", "集成电路", "晶圆", "封测", "光刻"],
    "新能源": ["新能源", "光伏", "风电", "储能", "锂电池", "钠电池", "氢能"],
    "人工智能": ["人工智能", "AI", "大模型", "算力", "算法", "智能", "机器人"],
    "医药": ["医药", "生物", "制药", "医疗", "疫苗", "中药", "创新药"],
    "消费": ["消费", "零售", "白酒", "食品", "饮料", "餐饮", "旅游"],
    "金融": ["银行", "保险", "证券", "券商", "基金", "金融", "支付"],
    "地产": ["房地产", "地产", "楼市", "住房", "土拍"],
    "军工": ["军工", "国防", "航空", "航天", "导弹", "船舶"],
    "汽车": ["汽车", "整车", "新能源车", "智驾", "自动驾驶", "零部件"],
    "有色": ["有色", "金属", "铜", "铝", "锌", "锂", "稀土", "黄金"],
    "化工": ["化工", "化学", "材料", "精细化工"],
    "通信": ["通信", "5G", "6G", "光模块", "运营商", "卫星"],
    "计算机": ["计算机", "软件", "互联网", "云", "数据", "安全"],
    "农业": ["农业", "种植", "养殖", "猪", "粮食", "种业"],
}


def judge_direction(title):
    """规则式情感判定。返回 (direction, confidence)。"""
    b = sum(1 for w in BULL_WORDS if w in title)
    s = sum(1 for w in BEAR_WORDS if w in title)
    if b > 0 and s > 0:
        return "mixed", 0.4      # 多空交织
    if b > s:
        return "bull", min(0.5 + b * 0.1, 0.9)
    if s > b:
        return "bear", min(0.5 + s * 0.1, 0.9)
    return "neutral", 0.2


def load_concepts():
    """加载概念板块映射 (ths_concept_list 的产物)。"""
    p = os.path.join(DATA_DIR, "concept_map.json")
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
        # 格式兼容: {concept: {name, stocks:[...]}} 或 {concept: [codes]}
        out = {}
        for k, v in (d.get("concepts") or d).items():
            if isinstance(v, dict):
                stocks = v.get("stocks") or []
            elif isinstance(v, list):
                stocks = v
            else:
                continue
            if stocks:
                out[k] = stocks[:20]   # 每概念最多 20 只, 防爆
        return out
    except Exception:
        return {}


def load_industry_stocks():
    """行业 → 成分股 (sina_industry 的产物)。"""
    p = os.path.join(DATA_DIR, "industry_stocks.json")
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
        return {k: v[:20] for k, v in (d.get("industries") or d).items()
                if isinstance(v, list) and v}
    except Exception:
        return {}


def load_stock_names():
    """代码 → 名称 (用于 L2 匹配)。"""
    p = os.path.join(DATA_DIR, "stock_names.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def match_news(title, concepts, industries, stock_names, item=None):
    """四层匹配。返回 (matched_by, codes, detail)。

    L0 优先: 采集器已给出的 codes (华尔街见闻 symbols), 编辑人工标注,
    比文本匹配可靠。
    """
    if item:
        codes = [str(c).replace("sh", "").replace("sz", "").replace("bj", "")[-6:]
                 for c in (item.get("codes") or [])]
        codes = [c for c in codes if c.isdigit()]
        if codes:
            return "annotated", codes[:10], "来源标注个股"
        # themes (编辑标注题材) → 概念/产业链匹配。
        # 实测 codes (symbols) 恒空, 但 themes 有值 (31/140),
        # 故 themes 作为 L0 的实际抓手。
        for th in (item.get("themes") or [])[:5]:
            th = str(th)
            if len(th) < 3:
                continue
            for concept, cstocks in concepts.items():
                if len(concept) >= 3 and (concept in th or th in concept):
                    cc = [str(c).replace("sh", "").replace("sz", "") for c in cstocks]
                    if cc:
                        return "theme", cc[:10], f"题材「{th}」→概念「{concept}」"
            for industry, kws in CHAIN_MAP.items():
                if any(k in th for k in kws):
                    cc = [c for c, n in stock_names.items()
                          if n and len(n) >= 3 and any(k in n for k in kws)]
                    if cc:
                        return "theme", cc[:10], f"题材「{th}」→产业链「{industry}」"

    # L2: 直接个股名/代码
    hit_codes = []
    for code, name in stock_names.items():
        if name and len(name) >= 3 and name in title:
            hit_codes.append(code)
        if code in title:
            hit_codes.append(code)
    if hit_codes:
        return "stock", hit_codes[:10], "直接命中个股"

    # L1: 概念板块
    for concept, stocks in concepts.items():
        if len(concept) < 3:
            continue
        if concept in title:
            codes = [str(c).replace("sh", "").replace("sz", "") for c in stocks]
            return "concept", codes, f"概念「{concept}」"

    # L3: 产业链词 → 股票名模糊匹配
    # sina_industry 无成分股, industry_map_tv 是英文行业名,
    # 故用「关键词命中标题 → 股票名含同关键词」两段式。
    for industry, kws in CHAIN_MAP.items():
        if any(k in title for k in kws):
            codes = [c for c, n in stock_names.items()
                     if n and len(n) >= 3 and any(k in n for k in kws)]
            if codes:
                return "industry", codes[:10], "industry:" + industry
    return None, [], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(DATA_DIR, "hot_news.json"))
    ap.add_argument("--news", default=os.path.join(DATA_DIR, "news_extra.json"),
                    help="财经资讯流 (第二个输入源)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "news_impact.json"))
    args = ap.parse_args()

    concepts = load_concepts()
    industries = load_industry_stocks()
    stock_names = load_stock_names()
    print(f"[impact] 映射库: 概念 {len(concepts)} | 行业 {len(industries)} | 个股名 {len(stock_names)}")

    items = []
    for src in (args.inp, args.news):
        if not os.path.exists(src):
            continue
        try:
            d = json.load(open(src, encoding="utf-8"))
            items.extend(d.get("items") or [])
        except Exception:
            pass
    if not items:
        print(f"[impact] 无输入 (检查 {args.inp} / {args.news})")
        return 1

    impacts = []
    stat = {"stock": 0, "concept": 0, "industry": 0, "annotated": 0, "theme": 0, "unmapped": 0}
    dir_stat = {"bull": 0, "bear": 0, "neutral": 0, "mixed": 0}

    for it in items:
        title = (it.get("title") or "").strip()
        if not title or len(title) < 8:
            continue
        direction, conf = judge_direction(title)
        dir_stat[direction] += 1
        matched_by, codes, detail = match_news(title, concepts, industries, stock_names, it)
        if not codes:
            stat["unmapped"] += 1
            continue
        stat[matched_by] += 1
        stocks = [{"code": c, "name": stock_names.get(c, "")} for c in codes]
        impacts.append({
            "title": title,
            "source": it.get("source_name") or it.get("source"),
            "time": it.get("updated") or it.get("time") or "",
            "url": it.get("url") or "",
            "channel": it.get("channel") or "",
            "themes": (it.get("themes") or [])[:5],
            "direction": direction,
            "confidence": round(conf, 2),
            "matched_by": matched_by,
            "match_detail": detail,
            "n_stocks": len(stocks),
            "stocks": stocks[:10],
        })

    # 按 (情感强度 × 影响股票数) 排序, 重要的在前
    def rank(x):
        w = {"bull": 1.0, "bear": 1.0, "mixed": 0.7, "neutral": 0.3}[x["direction"]]
        return w * x["confidence"] * (1 + x["n_stocks"] * 0.1)
    impacts.sort(key=rank, reverse=True)

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "news_impact v1 (三层映射 + 规则式情感)",
        "n_input": len(items),
        "n_mapped": len(impacts),
        "n_unmapped": stat["unmapped"],
        "match_stat": stat,
        "direction_stat": dir_stat,
        "impacts": impacts[:100],
        "meta": {
            "layers": "L1概念板块 / L2个股名代码 / L3产业链→行业",
            "sentiment": "规则式词典 (bull_words/bear_words), 同时命中判 mixed",
            "caveat": "rule mapping may miss polysemy and irony; sentiment is not price direction; Research reference only",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"[impact] 输入 {len(items)} -> 映射 {len(impacts)} (未映射 {stat['unmapped']})")
    print(f"[impact] 匹配方式: {stat}")
    print(f"[impact] 情感分布: {dir_stat}")
    print("[impact] TOP5:")
    for x in impacts[:5]:
        print(f"  [{x['direction']}] {x['title'][:44]}")
        print(f"       via {x['match_detail']} -> {x['n_stocks']} stocks")
    print(f"[impact] -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
