#!/usr/bin/env python3
"""
heat_algo.py — 事件聚类 + 热度计算 (借鉴 AIHOT 的聚簇与热度算法)

AIHOT 核心算法 (README):
  - 热度按「事件」算, 不按「文章」算
  - 48 小时内, 每个独立来源只算一次
  - 24 小时减半 (时间衰减)
  - 一家媒体发十篇也只算一次 (来源去重)
  - X/社交讨论也算进来
  - 与 6 小时前比, 涨得快的标「上升」, 新出现的标「新」

我们网站的现状问题:
  - 主题热度是简单词频计数: theme_count[kw] += 1
  - 同一家源重复抓取会多算
  - 没有时间衰减 (三天前的旧闻和三分钟前的一样重)
  - 没有跨源合并 (同一件事被 5 家报道, 只算 5 个词频而不是 1 个热点)

本模块:
  1. 事件聚类: 用关键词重合度 + 标的代码做轻量聚类 (不依赖向量服务)
  2. 热度计算: 独立来源数 × 时间衰减 × 提及次数
  3. 趋势标注: 与上一轮快照比, 标「新」/「上升」/「下降」

用法:
  python3 heat_algo.py --items items.json --prev prev.json --out heat.json

items.json 格式:
  [{"title": "...", "source": "sina", "ts": "2026-10-13T09:30:00+08:00",
    "codes": ["600519"], "themes": ["消费"], "sentiment": "pos"}, ...]
"""
import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ─── 文本归一化与关键词抽取 ────────────────────────────────────────
STOP = set("""的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
他们 她们 我们 你们 以及 或者 但是 因为 所以 如果 虽然 然后 还是 已经 可能 应该 需要 通过 对于 关于
a an the and or but if then else for to of in on at by with from as is are was were be been being this that these those""".split())


def norm_text(s):
    s = str(s or "").lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    return [w for w in s.split() if len(w) >= 2 and w not in STOP]


def jaccard(a, b):
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


# ─── 事件聚类 (轻量: 关键词重合 + 共同标的) ────────────────────────
def cluster_items(items, sim_thresh=0.32, code_weight=0.35, lookback_hours=336):
    """把 items 聚成事件。

    每条 item 归一化后得到 (词集, 标的集, 源, ts, 情绪, 原始)。
    相似度 = jaccard(词集) + code_weight * 共同标的比例
    超过 sim_thresh 且时间在 lookback 内 → 归为同一事件。
    采用单链聚合 (逐条比对已有事件代表)。
    """
    now = datetime.now(CST)
    prepared = []
    for it in items:
        try:
            ts = datetime.fromisoformat(str(it.get("ts")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=CST)
        except Exception:
            ts = now
        prepared.append({
            "words": set(norm_text(it.get("title", ""))),
            "codes": set(str(c) for c in (it.get("codes") or [])),
            "source": str(it.get("source") or "unknown"),
            "ts": ts,
            "sentiment": it.get("sentiment") or "neu",
            "raw": it,
        })

    prepared.sort(key=lambda x: x["ts"])
    events = []          # [{words, codes, sources:{}, items:[], first_ts, last_ts}]
    cutoff = now - timedelta(hours=lookback_hours)

    for p in prepared:
        if p["ts"] < cutoff:
            continue
        best, best_sim = None, 0.0
        for ev in events:
            sim = jaccard(p["words"], ev["words"])
            if p["codes"] and ev["codes"]:
                inter = len(p["codes"] & ev["codes"])
                sim += code_weight * inter / max(1, min(len(p["codes"]), len(ev["codes"])))
            if sim > best_sim:
                best, best_sim = ev, sim
        if best is not None and best_sim >= sim_thresh:
            best["words"] |= p["words"]
            best["codes"] |= p["codes"]
            best["items"].append(p)
            best["sources"].setdefault(p["source"], []).append(p["ts"])
            best["last_ts"] = max(best["last_ts"], p["ts"])
        else:
            events.append({
                "words": set(p["words"]),
                "codes": set(p["codes"]),
                "items": [p],
                "sources": {p["source"]: [p["ts"]]},
                "first_ts": p["ts"],
                "last_ts": p["ts"],
            })
    return events


# ─── 热度计算 (AIHOT 口径) ────────────────────────────────────────
def event_heat(ev, now=None, half_life_h=24.0, window_h=48.0):
    """热度 = Σ(每个独立来源只算一次) × 时间衰减, 超出 window 不计。

    - 独立来源: ev["sources"] 的 key 数 (一家媒体发十篇只算一次)
    - 时间衰减: 24 小时减半 → weight = 0.5 ** (age_h / 24)
    - 窗口: 48 小时外的提及不计入热度 (但事件仍保留)
    - 提及强度: 同一来源多篇给小幅加成 (0.15/篇, 上限 +1.0)
    """
    if now is None:
        now = datetime.now(CST)

    source_score = 0.0
    n_sources = len(ev["sources"])
    for src, times in ev["sources"].items():
        # 该来源最近一次提及的时间决定衰减
        latest = max(times)
        age_h = (now - latest).total_seconds() / 3600.0
        if age_h > window_h:
            continue
        w = 0.5 ** (age_h / half_life_h)
        # 同源多篇小幅加成
        extra = min(1.0, 0.15 * (len(times) - 1))
        source_score += w + extra * w

    if source_score <= 0:
        return 0.0, {"n_sources": n_sources, "in_window": 0, "stale": True}

    return round(source_score, 3), {
        "n_sources": n_sources,
        "in_window": sum(1 for ts_list in ev["sources"].values()
                         for t in ts_list if (now - t).total_seconds() / 3600.0 <= window_h),
        "total_mentions": sum(len(v) for v in ev["sources"].values()),
        "stale": False,
    }


# ─── 主题热度 (从事件汇总到主题) ──────────────────────────────────
def theme_heat(events, theme_field="themes"):
    """把事件的热度汇总到主题: 一个事件可属多个主题。"""
    now = datetime.now(CST)
    th = defaultdict(lambda: {"heat": 0.0, "events": 0, "codes": set(), "sources": set()})
    for ev in events:
        h, meta = event_heat(ev, now)
        if h <= 0:
            continue
        themes = set()
        for it in ev["items"]:
            for t in (it["raw"].get(theme_field) or []):
                themes.add(t)
            # 标的代码也作为主题线索
        for t in themes:
            e = th[t]
            e["heat"] = round(e["heat"] + h, 3)
            e["events"] += 1
            e["codes"] |= ev["codes"]
            e["sources"] |= set(ev["sources"].keys())
    return dict(th)


# ─── 趋势标注 ─────────────────────────────────────────────────────
def annotate_trend(cur_theme, prev_theme, rise_thresh=0.25):
    """与上一轮快照对比: 新出现=新, 涨>=thresh=上升, 跌>=thresh=下降。"""
    out = {}
    for t, v in cur_theme.items():
        p = (prev_theme or {}).get(t, {}).get("heat", 0.0)
        if p <= 0:
            tag = "new"
        elif v["heat"] >= p * (1 + rise_thresh):
            tag = "rising"
        elif v["heat"] <= p * (1 - rise_thresh):
            tag = "falling"
        else:
            tag = "flat"
        out[t] = dict(v, prev_heat=round(p, 3), trend=tag,
                      delta=round(v["heat"] - p, 3))
    return out


# ─── main ─────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="输入 items JSON")
    ap.add_argument("--prev", help="上一轮主题热度 JSON (用于趋势标注)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    now = datetime.now(CST)
    items = json.load(open(args.items, encoding="utf-8"))
    prev = json.load(open(args.prev, encoding="utf-8")) if args.prev and os.path.exists(args.prev) else {}

    events = cluster_items(items)
    th = theme_heat(events)
    ann = annotate_trend(th, prev)

    ranked = sorted(ann.items(), key=lambda x: -x[1]["heat"])[:args.top]

    out = {
        "ts": now.isoformat(),
        "n_items": len(items),
        "n_events": len(events),
        "themes": [{"theme": t, **{k: (sorted(v) if isinstance(v, set) else v)
                                   for k, v in d.items()}} for t, d in ranked],
        "events": [{
            "codes": sorted(ev["codes"]),
            "sources": sorted(ev["sources"].keys()),
            "n_sources": len(ev["sources"]),
            "mentions": sum(len(v) for v in ev["sources"].values()),
            "first_ts": ev["first_ts"].isoformat(),
            "last_ts": ev["last_ts"].isoformat(),
            "heat": event_heat(ev, now)[0],
            "sample_titles": [it["raw"].get("title", "")[:60] for it in ev["items"][:3]],
        } for ev in sorted(events, key=lambda e: -event_heat(e, now)[0])[:15]],
        "meta": {
            "algo": "AIHOT 聚簇+热度算法 (简化实现)",
            "source": "借鉴 gitcode.com/gh_mirrors/ai/AIHOT README 聚簇与热点章节",
            "rules": [
                "热度按事件算, 不按文章算",
                "48小时窗口内, 每个独立来源只算一次",
                "24小时减半: weight = 0.5 ** (age_h/24)",
                "同一来源多篇: +0.15/篇, 上限 +1.0",
                "聚类: jaccard(词集) + 0.35*共同标的比例 >= 0.32",
                "聚类回看 14 天 (336h)",
            ],
            "ts": now.isoformat(),
            "trend_rules": "new=上轮无此主题; rising=涨>=25%; falling=跌>=25%; 其余 flat",
            "caveat": "聚类用轻量文本重合, 未用向量模型; 同一事件的不同说法可能拆分; "
                      "标的代码由上游提供, 缺失时退化为纯文本聚类",
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"items={len(items)} → events={len(events)} → themes={len(th)}")
    print(f"TOP{len(ranked)} 主题热度:")
    for t, d in ranked:
        codes = ",".join(sorted(d["codes"])[:3]) or "-"
        print(f"  {d['heat']:7.3f}  {t:12s} 事件{d['events']:3d} 来源{len(d['sources']):2d} "
              f"趋势:{d['trend']:8s} 标的:{codes}")
    print(f"\nDONE -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
