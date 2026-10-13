#!/usr/bin/env python3
"""
build_news_digest.py — 生成网站「24h 热点资讯流」消费的 data/news_digest.json

为什么需要这个脚本:
  index.html 的 renderNews() 只读 data/news_digest.json, 但仓库里**没有任何
  脚本生成它** — collect_all_sources 只把新闻写进 all_sources.json,
  没人做这层转换。结果 news_digest.json 永远停在最后一次本地手跑的时间
  (线上实测停在 2026-10-13 08:30, 前端「24h 热点资讯流」20 小时不更新)。

数据流:
  all_sources.json 的 data.news  (sina_news + douyin_hot)
    → 分类 (关键词规则) → 去重 → 按时间倒序
    → data/news_digest.json

分类规则 (与旧版 news_digest 的 categories 对齐, 前端按 _cat 渲染标签):
  财报动态 / 行业题材 / 市场行情 / 国内时政 / 国际新闻 / 综合热点

用法:
  python3 build_news_digest.py                       # 读 data/all_sources.json
  python3 build_news_digest.py --in /tmp/all.json --out data/news_digest.json
  python3 build_news_digest.py --keep-hours 48       # 保留窗口 (默认 24h)
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

# ─── 分类规则: (类别, 关键词) 按顺序首个命中生效 ──────────────────
# 顺序即优先级。市场行情/国内时政/国际新闻 放前面: 它们的词更「硬」
# (央行/国务院/美联储), 而行业题材含「政策/规划/标准」这类也会出现在
# 时政句里的宽泛词, 放后面只捡漏。
CATEGORY_RULES = [
    ("市场行情", ["A股", "港股", "美股", "涨停", "跌停", "大盘", "指数", "上证",
                "深证", "创业板", "科创", "北证", "成交额", "北向", "融资",
                "融券", "龙虎榜", "央行", "逆回购", "MLF", "LPR", "降准",
                "降息", "汇率", "离岸人民币", "美元指数", "国债收益率",
                "大宗商品", "黄金", "原油", "期货"]),
    ("国内时政", ["国务院", "发改委", "工信部", "财政部", "证监会", "商务部",
                "外交部", "总书记", "总理", "会议", "部署", "印发", "意见",
                "法规", "法律", "草案", "审议", "通过", "监管部门", "部委",
                "对华", "反制", "关税"]),
    ("国际新闻", ["美国", "欧洲", "欧盟", "日本", "韩国", "印度", "俄罗斯", "乌克兰",
                "中东", "以色列", "伊朗", "沙特", "OPEC", "美联储", "加息",
                "关税", "贸易战", "制裁", "出口管制", "大选", "北约",
                "联合国", "世贸", "IMF", "世行"]),
    ("财报动态", ["业绩", "财报", "营收", "净利润", "同比", "环比", "季报", "年报",
                "毛利", "EPS", "分红", "回购", "增持", "减持", "预告", "快报",
                "产量", "销量", "订单", "中报", "三季", "市值", "估值",
                "力拓", "必和必拓", "淡水河谷"]),
    ("行业题材", ["板块", "行业", "概念", "产业链", "产能", "扩产", "投产", "招标",
                "中标", "补贴", "规划", "标准", "技术突破", "量产",
                "半导体", "芯片", "新能源", "光伏", "风电", "储能", "电池",
                "医药", "生物", "人工智能", "算力", "机器人", "汽车", "白酒",
                "银行", "保险", "证券", "地产", "钢铁", "有色", "化工"]),
]

DEFAULT_CAT = "综合热点"


def classify(title):
    """返回 (类别, 命中词)。无命中给 DEFAULT_CAT。"""
    for cat, kws in CATEGORY_RULES:
        for kw in kws:
            if kw in title:
                return cat, kw
    return DEFAULT_CAT, ""


def sentiment_of(title):
    """极简情绪标记, 前端用 dot 颜色区分。pos/neg/neu。"""
    pos = ["涨", "新高", "突破", "增长", "超预期", "利好", "大增", "回升",
           "扩大", "加速", "中标", "量产", "获批", "创新高"]
    neg = ["跌", "下滑", "下降", "亏损", "低于预期", "利空", "大减", "放缓",
           "缩减", "处罚", "违规", "退市", "违约", "爆雷", "调查", "停产"]
    t = title
    if any(k in t for k in neg):
        return "neg"
    if any(k in t for k in pos):
        return "pos"
    return "neu"


def parse_ts(s):
    """兼容 '2026-10-14 05:27:53' / ISO / 空值。失败返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 6], fmt)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1) + " " + m.group(2),
                                     "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def load_news_records(path):
    """从 all_sources.json 取 news 组记录, 并补齐 src/ts 字段。"""
    if not os.path.exists(path):
        return []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"[news_digest] 读取失败 {path}: {e}", file=sys.stderr)
        return []
    out = []
    for r in (d.get("data") or {}).get("news") or []:
        # 抖音热榜的字段是 word/active_time, 新浪快讯是 title/ts
        title = str(r.get("title") or r.get("word") or "").strip()
        if not title:
            continue
        ts = r.get("ts") or r.get("active_time") or ""
        src = "新浪7x24" if r.get("_src") == "sina_news" else "抖音热榜"
        out.append({"title": title[:160], "ts": str(ts), "src": src,
                    "key": r.get("_key") or title[:40]})
    return out


def build(in_path, keep_hours=24, min_n=0):
    now = datetime.now(CST)
    cutoff = now - timedelta(hours=keep_hours)
    records = load_news_records(in_path)

    seen, items = set(), []
    for r in records:
        k = r["key"]
        if k in seen:
            continue
        seen.add(k)
        t = parse_ts(r["ts"])
        if t is not None:
            # 无时区信息按 CST 解释
            if t.tzinfo is None:
                t = t.replace(tzinfo=CST)
            if t < cutoff:
                continue
            items.append({**r, "_t": t})
        else:
            # 解析不出时间的保留 (标 unknown), 不因时间缺失丢内容
            items.append({**r, "_t": None})

    items.sort(key=lambda x: x["_t"] or datetime.min.replace(tzinfo=CST),
               reverse=True)

    categories = {}
    for it in items:
        cat, _hit = classify(it["title"])
        categories.setdefault(cat, []).append({
            "title": it["title"],
            "digest": "",
            "ts": it["ts"],
            "src": it["src"],
            "cat": cat,
            "sentiment": sentiment_of(it["title"]),
        })

    # 若窗口内为空, 退回不限时间 (保证前端永远有内容, 但带 stale 标记)
    stale = False
    if not items and records:
        stale = True
        for r in records[:80]:
            cat, _ = classify(r["title"])
            categories.setdefault(cat, []).append({
                "title": r["title"], "digest": "", "ts": r["ts"],
                "src": r["src"], "cat": cat,
                "sentiment": sentiment_of(r["title"]),
            })

    total = sum(len(v) for v in categories.values())
    return {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "stale": stale,
        "keep_hours": keep_hours,
        "total": total,
        "cat_counts": {k: len(v) for k, v in categories.items()},
        "sentiment": {},
        "global_score": 0,
        "global_mood": "unknown",
        "theme_hits": [],
        "ths_themes": [],
        "categories": categories,
        "note": ("built by build_news_digest.py from all_sources.json"
                 + (" (window empty, showing older items)" if stale else "")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=None,
                    help="all_sources.json 路径 (默认 <data>/all_sources.json)")
    ap.add_argument("--out", default=None,
                    help="输出路径 (默认 <data>/news_digest.json)")
    ap.add_argument("--keep-hours", type=int, default=24)
    args = ap.parse_args()

    inp = args.inp or os.path.join(AI_VIEW_DATA, "all_sources.json")
    out = args.out or os.path.join(AI_VIEW_DATA, "news_digest.json")

    # all_sources.json 不存在时, 找最新的快照兜底
    if not os.path.exists(inp):
        cands = sorted(glob.glob(os.path.join(AI_VIEW_DATA, "all_sources*.json")))
        if cands:
            inp = cands[-1]
            print(f"[news_digest] all_sources.json 缺失, 用 {inp}", file=sys.stderr)

    d = build(inp, args.keep_hours)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"[news_digest] {out}: {d['total']} 条 "
          f"{'(stale, 窗口内为空)' if d['stale'] else ''} "
          f"分类={d['cat_counts']}")


if __name__ == "__main__":
    main()
