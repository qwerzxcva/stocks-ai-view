#!/usr/bin/env python3
"""theme_engine.py — 题材生命周期层（批次四：《题材逐利》等）

为什么单独一层：A 股的短期涨跌很大程度由题材轮动驱动，而技术结构层只觉得
「这只票在涨」，说不出「它属于哪个正在发酵的题材、这个题材到了哪一步」。
这一步判断错了，结构再漂亮也是接最后一棒。

数据来源：data/concept_heat.json（96 个板块 × 30 个交易日的板块指数日涨跌，
由 build_concept_heat.py 从 db-market 的 sector/concept_kline 生成）。
**注意它给的是板块涨跌，不是真"热度"**，所以这里所有判据都基于收益率与斜率，
不假装有资金流或搜索热度数据。

阶段判据（可判定，全部用收益率，不用形容词）：
  萌芽  5 日累计 > 0 且 20 日累计 < 8%（还没人注意）
  发酵  5 日 > 3% 且 20 日 > 8% 且 5 日斜率 > 0（加速中）
  高潮  5 日 > 8% 或 20 日 > 25%（拥挤，风险最高）
  退潮  5 日 < −3%（已经在跌）或 20 日 > 10% 且 5 日 < 0（冲高回落）

纪律：阶段是**描述**不是买卖建议。高潮期最容易追高亏钱，退潮期最容易割在地板，
下游用法见 docs/books/batch4_fundamental_theme.md。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kline_source import DATA_DIR  # noqa: E402

CST = timezone(timedelta(hours=8))

# 全部用「百分比」为单位（与 _cum() 返回一致）。
# 这里曾经把 SPROUT_20D 写成 0.08 又拿它跟百分数比，结果把所有板块判成 climax：
# 单位不统一是这类阈值代码最常见的错，改值时连单位一起看。
SPROUT_20D = 8.0        # 20 日累计 <8% 且 5 日为正 → 萌芽
FERMENT_5D = 3.0
FERMENT_20D = 8.0
CLIMAX_5D = 8.0         # 5 日累计 >8%
CLIMAX_20D = 25.0       # 或 20 日 >25%
EBB_5D = -3.0


def _cum(cells, n):
    """最近 n 个交易日的累计涨幅（%）"""
    if not cells:
        return None
    seg = cells[-n:]
    acc = 1.0
    for c in seg:
        acc *= (1 + c.get("p", 0) / 100.0)
    return (acc - 1) * 100


def board_stage(cells):
    """单个板块的生命周期阶段。数据不足返回 unknown，不猜。"""
    if not cells or len(cells) < 10:
        return {"stage": "unknown", "reason": "样本不足 10 个交易日"}
    d5 = _cum(cells, 5)
    d20 = _cum(cells, 20) if len(cells) >= 20 else None
    d1 = cells[-1].get("p", 0)
    prev5 = _cum(cells[:-5], 5) if len(cells) >= 10 else 0
    accel = (d5 or 0) - (prev5 or 0)  # 近 5 日相对前 5 日的加速

    if d20 is None:
        return {"stage": "unknown", "reason": "不足 20 日无法判中期", "d5": round(d5, 2)}

    if d5 >= CLIMAX_5D or d20 >= CLIMAX_20D:
        stage, note = "climax", "拥挤度最高，追高风险最大"
    elif d5 <= EBB_5D or (d20 > 10 and d5 < 0):
        stage, note = "ebb", "退潮/冲高回落"
    elif d5 >= FERMENT_5D and d20 >= FERMENT_20D and accel > 0:
        stage, note = "ferment", "加速中，主升段"
    elif d5 > 0 and d20 < SPROUT_20D:
        stage, note = "sprout", "还没被大众注意"
    else:
        stage, note = "neutral", "无明显阶段特征"
    return {"stage": stage, "note": note, "d1": round(d1, 2), "d5": round(d5, 2),
            "d20": round(d20, 2), "accel": round(accel, 2)}


def load_heat(path=None):
    p = path or os.path.join(DATA_DIR, "concept_heat.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def theme_view(heat=None):
    heat = heat or load_heat()
    if not heat:
        return {"available": False, "reason": "concept_heat.json 缺失"}
    out = []
    for b in heat.get("boards") or []:
        st = board_stage(b.get("cells") or [])
        out.append({"bk": b.get("bk"), "name": b.get("name"),
                    "n_stocks": b.get("n_stocks"), "mktcap": b.get("total_mktcap"),
                    "latest_close": b.get("latest_close"), **st})
    stages = {}
    for x in out:
        stages[x["stage"]] = stages.get(x["stage"], 0) + 1
    out.sort(key=lambda x: (x.get("d5") if x.get("d5") is not None else -99), reverse=True)
    return {"available": True, "generated_at": heat.get("generated_at"),
            "as_of": (heat.get("dates") or [None])[-1],
            "boards": out, "stage_counts": stages,
            "data_caveat": "板块指数涨跌≠真实热度；无资金流/搜索热度数据，阶段判定基于收益率斜率"}


def stock_theme_map(path=None):
    """concept_map.json：概念 → 成分股，用于把板块阶段套到个股上。"""
    p = path or os.path.join(DATA_DIR, "concept_map.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p))
    except Exception:
        return None
    return d


def themes_of(code, cmap=None, view=None):
    """某只股票所属题材及各自阶段。返回空列表 = 没有映射，不是"没有题材"。

    concept_map.json 的结构是 {concepts: {题材名: [6位代码...]}}，键是申万/概念名称，
    与 concept_heat 的板块名可能不完全一致，所以用「精确名 → 包含关系」两级匹配，
    匹配不到就老实返回空，不猜。
    """
    cmap = cmap or stock_theme_map()
    view = view or theme_view()
    if not cmap or not view.get("available"):
        return []
    concepts = cmap.get("concepts") if isinstance(cmap, dict) else None
    if not isinstance(concepts, dict):
        return []
    bare = code.replace("sh", "").replace("sz", "").replace("bj", "")
    hot = {b["name"]: b for b in view["boards"] if b.get("name")}
    out = []
    for name, codes in concepts.items():
        if not isinstance(codes, list):
            continue
        if not any(str(c).lstrip("0") == bare.lstrip("0") or str(c) == bare for c in codes):
            continue
        b = hot.get(name) or next((v for k, v in hot.items() if k and (k in name or name in k)), None)
        out.append({"theme": name, "stage": (b or {}).get("stage", "unmatched"),
                    "d5": (b or {}).get("d5"), "d20": (b or {}).get("d20"),
                    "board": (b or {}).get("name")})
    return out


def mapping_health(cmap=None, view=None):
    """题材→个股映射的健康度。

    实测：concept_map.json（申万行业口径，180 个题材）与 concept_heat.json
    （东财概念口径，96 个板块）是**两套不同分类体系**，同名率只有 3.3%。
    所以“板块处于发酵期”能用于市场环境判断，但**不能**据此说某只个股属于发酵题材。
    这个函数存在的意义：让下游知道匹配率，别把 unmatched 当成中性。
    """
    cmap = cmap or stock_theme_map()
    view = view or theme_view()
    if not cmap or not view.get("available"):
        return {"ok": False, "reason": "缺少 concept_map 或 concept_heat"}
    concepts = list((cmap.get("concepts") or {}))
    names = [b["name"] for b in view["boards"] if b.get("name")]
    exact = sum(1 for n in concepts if n in names)
    loose = sum(1 for n in concepts if n in names or any(n in k or k in n for k in names))
    rate = loose / len(concepts) if concepts else 0
    return {"ok": rate >= 0.5, "concepts": len(concepts), "boards": len(names),
            "exact_match": exact, "loose_match": loose, "match_rate": round(rate, 3),
            "reason": "两套分类口径不同源，映射率 {:.1%} → 个股题材归属不可用".format(rate)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--code")
    a = ap.parse_args()
    v = theme_view()
    if not v.get("available"):
        print(v["reason"])
        return
    if a.code:
        ts = themes_of(a.code, view=v)
        if not ts:
            print("无题材映射（concept_map 未覆盖该股）")
            return
        for t in ts:
            print(f"{t['theme']} 阶段={t['stage']} 5日={t['d5']}% 20日={t['d20']}% 板块={t['board']}")
        return
    print(f"as_of {v['as_of']} 阶段分布 {v['stage_counts']}")
    for b in v["boards"][:a.top]:
        print(f"  {b['name'][:14]:16} {b['stage']:8} 1日={b.get('d1')}% 5日={b.get('d5')}% 20日={b.get('d20')}%")
    if a.all:
        p = os.path.join(DATA_DIR, "theme_signals.json")
        json.dump({"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                   "engine": "theme_engine/v1", **v}, open(p, "w"), ensure_ascii=False, indent=1)
        print(f"→ {p}")


if __name__ == "__main__":
    main()
