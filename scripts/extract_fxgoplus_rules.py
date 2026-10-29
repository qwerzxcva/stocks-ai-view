#!/usr/bin/env python3
"""from fxgoplus 语料抽出可执行判据，生成 playbook。

输入：~/.aharou/knowledge/sources/fxgoplus/raw.jsonl（浏览器抓的站点正文）
输出：
  1. rules.json（机器可读：主题 → 判据条目，每条带原文与出处 URL）
  2. docs/FXGOPLUS_PLAYBOOK.md（人读：按主题归并，只留有数字/条件/阈值的条目）

判断标准：站点文章里大量是「什么是 X」的科普，真正能变成代码判据的是
**带数字或明确条件**的句子（止损几个点、仓位几个百分点、时段几点到几点、
指标参数多少）。没有数字也没有条件的句子一律丢弃——它们进不了引擎。
"""
import json
import os
import re
import collections
import argparse

RAW = os.path.expanduser("~/.aharou/knowledge/sources/fxgoplus/raw.jsonl")

TOPICS = {
    "止损风控": r"止损|风险|回撤|爆仓|风控|亏损|风险回报|盈亏比|risk.reward",
    "仓位资金管理": r"仓位|资金管理|杠杆|加仓|减仓|头寸|position.size|lot",
    "蜡烛图形态": r"蜡烛|k线|锤子|吞没|十字星|pin.bar|doji|engulf|harami|形态",
    "支撑阻力": r"支撑|阻力|压力位|供给区|需求区|supply|demand|support|resistance",
    "趋势与均线": r"趋势|均线|ma\b|ema|金叉|死叉|trend|移动平均",
    "指标": r"RSI|MACD|KDJ|布林|BOLL|ATR|CCI|随机指标|背离|indicator",
    "成交量流动性": r"成交量|放量|缩量|量能|流动性|liquidity|订单流|volume|点差|spread",
    "缺口与突破": r"缺口|gap|突破|假突破|breakout|区间|盘整",
    "交易时段": r"时段|session|开盘|收盘|亚盘|欧盘|美盘|伦敦|纽约|东京|时区",
    "交易心理": r"心理|情绪|纪律|贪婪|恐惧|耐心|复盘|心态|emotion|discipline",
    "形态与形态学": r"头肩|双顶|双底|三角|旗形|楔形|矩形|杯子|pattern",
    "斐波那契": r"斐波那契|fibonacci|回撤位|0\.618|黄金分割",
}

# 判据必须带的「数字感」：阈值、单位、参数
NUM = re.compile(r"(\d+(\.\d+)?\s*(%|％|倍|点|个点|pip|pips|分钟|小时|天|日|周|月|年|笔|次|档|手|美元|倍)|"
                 r"\d+:\d+|1[-–]2[-–]3|70%|30%|50%|2%|1%|至少|不超过|最多|最少|高于|低于|大于|小于)")
ACTION = re.compile(r"入场|出场|开仓|平仓|止损|止盈|加仓|减仓|买入|卖出|做多|做空|等待|确认|突破|跌穿|站上|"
                    r"不交易|禁止|必须|应该|建议|规则|原则|条件")
BROKER = re.compile(r"经纪商|平台评测|开户|入金|出金|返佣|点差对比|监管牌照|broker|rebate", re.I)
BROKER_TITLE = re.compile(r"怎么样|测评|评测|开户|监管|牌照|平台评价|点差对比|赠金|返佣|入金|出金|经纪商", re.I)
# 正文出现多个券商指标才算营销稿：方法文也会提 MT4 或点差，单次命中不算
BROKER_SIGNALS = [r"开户", r"入金", r"出金", r"赠金", r"返佣", r"监管牌照", r"点差低至",
                  r"最低入金", r"杠杆比例", r"MT[45]", r"DMA", r"佣金", r"经纪商", r"交易手数"]
NEWS = re.compile(r"roundup|日报|周报|行情回顾|数据前瞻|财报|加息|央行|2026-\d|2025-\d", re.I)


def sentences(text):
    parts = re.split(r"(?<=[。！？!?])|\n+", text)
    return [p.strip(" 　\t·•-—*") for p in parts if p and p.strip()]


def classify(rec):
    t = (rec.get("title") or "") + " " + (rec.get("url") or "")
    if BROKER_TITLE.search(t) or BROKER.search(t):
        return "skip"
    body = (rec.get("text") or "")[:1500]
    if sum(1 for p in BROKER_SIGNALS if re.search(p, body, re.I)) >= 3:
        return "skip"
    if NEWS.search(t):
        return "news"
    return "method"


def topic_of(text):
    scores = collections.Counter()
    for name, pat in TOPICS.items():
        scores[name] = len(re.findall(pat, text, re.I))
    name, n = scores.most_common(1)[0]
    return (name, n) if n else ("未分类", 0)


def extract(rec):
    out = []
    for s in sentences(rec.get("text") or ""):
        if len(s) < 12 or len(s) > 320:
            continue
        if not NUM.search(s):
            continue
        if not ACTION.search(s):
            continue
        out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=RAW)
    ap.add_argument("--out-json", default=os.path.expanduser(
        "~/.aharou/knowledge/sources/fxgoplus/rules.json"))
    ap.add_argument("--out-md", default="docs/FXGOPLUS_PLAYBOOK.md")
    a = ap.parse_args()

    recs = [json.loads(l) for l in open(a.raw, encoding="utf-8")]
    stats = collections.Counter()
    by_topic = collections.defaultdict(list)

    for r in recs:
        kind = classify(r)
        stats[kind] += 1
        if kind != "method":
            continue
        rec_rules = extract(r)
        if not rec_rules:
            stats["无非数字判据"] += 1
            continue
        topic, score = topic_of(" ".join(rec_rules)[:2000] + " " + (r.get("title") or ""))
        by_topic[topic].append({
            "url": r["url"],
            "title": r.get("title") or "",
            "rules": rec_rules[:8],
            "heads": (r.get("heads") or [])[:10],
        })
        stats["有判据文章"] += 1

    payload = {"generated_from": a.raw, "stats": dict(stats),
               "topics": {k: v for k, v in by_topic.items()}}
    os.makedirs(os.path.dirname(os.path.abspath(a.out_json)), exist_ok=True)
    json.dump(payload, open(a.out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # Markdown：主题 → 条目 → 原文出处
    order = sorted(by_topic.items(), key=lambda kv: -len(kv[1]))
    lines = [
        "# fxgoplus 策略 playbook（自动抽取）",
        "",
        f"来源：`{a.raw}`（浏览器抓取，467 篇）。生成器：`scripts/extract_fxgoplus_rules.py`。",
        "",
        "抽取口径：正文里**同时带数字阈值和动作词**的句子才算判据；"
        "泛泛而谈的科普句丢弃。每条都带原文 URL，可回溯。",
        "",
        f"统计：{json.dumps(dict(stats), ensure_ascii=False)}",
        "",
        "## 主题分布",
        "",
        "| 主题 | 文章数 | 判据条数 |",
        "|---|---|---|",
    ]
    for name, items in order:
        n = sum(len(i["rules"]) for i in items)
        lines.append(f"| {name} | {len(items)} | {n} |")

    for name, items in order:
        lines += ["", f"## {name}", ""]
        for it in items:
            lines.append(f"### {it['title'] or it['url']}")
            lines.append(f"<{it['url']}>")
            for r in it["rules"]:
                lines.append(f"- {r}")
            lines.append("")
    os.makedirs(os.path.dirname(os.path.abspath(a.out_md)), exist_ok=True)
    open(a.out_md, "w", encoding="utf-8").write("\n".join(lines))
    print(json.dumps(dict(stats), ensure_ascii=False))
    print("主题:", [(k, len(v)) for k, v in order])
    print("输出:", a.out_json, a.out_md)


if __name__ == "__main__":
    main()
