#!/usr/bin/env python3
"""
build_news_maps.py — 为 news_impact 引擎生成映射库

从现有采集产出构建三个映射:
  concept_map.json    概念 → 成分股 (来自 ths_concept_list / sub_concepts)
  industry_stocks.json 行业 → 成分股 (来自 sina_industry + industry_map_tv)
  stock_names.json    代码 → 名称 (来自 all_sources / valuation / industry_map_tv)

用法: python3 build_news_maps.py
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def build_stock_names():
    """代码 → 名称，从多个来源合并。"""
    names = {}
    # 1. shard_*.json 的 quote 组 (sina_hq 3024 只全带 name, 最全)
    for sp in glob.glob(os.path.join(DATA_DIR, "shard_*.json")):
        try:
            d = json.load(open(sp, encoding="utf-8"))
        except Exception:
            continue
        for r in ((d.get("data") or {}).get("quote") or []):
            c = str(r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
            if len(c) == 6 and c.isdigit() and r.get("name"):
                names[c] = r["name"]
    # 2. industry_map_tv (5309 只全覆盖)
    im = load("industry_map_tv.json")
    if im:
        items = im.get("map") or im.get("stocks") or im
        if isinstance(items, dict):
            for code, v in items.items():
                c = str(code).replace("sh", "").replace("sz", "").replace("bj", "")
                if len(c) != 6 or not c.isdigit():
                    continue
                if isinstance(v, dict):
                    nm = v.get("name") or v.get("n")
                elif isinstance(v, str):
                    nm = None
                else:
                    nm = None
                if nm:
                    names[c] = nm
    # 2. all_sources.quote
    asrc = load("all_sources.json")
    if asrc:
        for r in ((asrc.get("data") or {}).get("quote") or []):
            c = str(r.get("code") or "").replace("sh", "").replace("sz", "")
            if len(c) == 6 and c.isdigit() and r.get("name"):
                names[c] = r["name"]
    # 3. valuation
    val = load("valuation.json")
    if val:
        for k, v in (val.get("stocks") or {}).items():
            c = str(k).replace("sh", "").replace("sz", "")
            if len(c) == 6 and c.isdigit() and isinstance(v, dict) and v.get("name"):
                names.setdefault(c, v["name"])
    # 4. 预测快照 (含 name)
    snaps = sorted(glob.glob(os.path.join(DATA_DIR, "predictions", "engine_v5_*.json")))
    if snaps:
        try:
            d = json.load(open(snaps[-1], encoding="utf-8"))
            for r in (d.get("top") or []):
                c = str(r.get("code") or "").replace("sh", "").replace("sz", "")
                if len(c) == 6 and c.isdigit() and r.get("code"):
                    # 快照的 code 带前缀, name 字段可能没有; 用 ai_pick_v4 补
                    pass
        except Exception:
            pass
    return names


def build_industry_stocks():
    """行业 → 成分股。从 shard_*.json 的 quote 组 (5309 只带 industry) 反查。"""
    items = {}
    for sp in glob.glob(os.path.join(DATA_DIR, "shard_*.json")):
        try:
            d = json.load(open(sp, encoding="utf-8"))
        except Exception:
            continue
        for r in ((d.get("data") or {}).get("quote") or []):
            code = str(r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
            ind = r.get("industry")
            if len(code) == 6 and code.isdigit() and ind:
                items[code] = ind
    if not items:
        return {}
    by_ind = {}
    for c, ind in items.items():
        by_ind.setdefault(ind, []).append(c)
    # 加上产业链归并 (CHAIN_MAP 的行业键)
    chain = {
        "半导体": ["半导体", "芯片", "集成电路"],
        "新能源": ["光伏", "电池", "新能源"],
        "人工智能": ["软件", "互联网", "计算机", "通信设备"],
        "医药": ["医药", "医疗", "生物"],
        "金融": ["银行", "保险", "证券"],
    }
    out = dict(by_ind)
    for chain_name, kws in chain.items():
        codes = []
        for ind, cs in by_ind.items():
            if any(k in ind for k in kws):
                codes.extend(cs)
        if codes:
            out[chain_name] = list(dict.fromkeys(codes))[:30]
    return out


def build_concept_map():
    """概念 → 成分股。来源: ths_concept_list (只有概念名) + sub_concepts 结构。"""
    # ths_concept_list 产出在 shard_a.json 的 sector 组
    sa = load("shard_a.json")
    concepts = {}
    if sa:
        for r in ((sa.get("data") or {}).get("sector") or []):
            nm = r.get("name") or r.get("concept") or r.get("title")
            if not nm or len(nm) < 3:
                continue
            codes = r.get("codes") or r.get("stocks") or r.get("components")
            if isinstance(codes, list) and codes:
                concepts[nm] = [str(c).replace("sh", "").replace("sz", "") for c in codes][:20]
    # 没有成分股时, 至少保留概念名 (news_impact 会走 L3 兜底)
    if not concepts:
        print("[maps] 警告: ths_concept_list 无成分股, 概念映射将退化为仅名称")
    return concepts


def main():
    names = build_stock_names()
    inds = build_industry_stocks()
    cons = build_concept_map()
    ts = datetime.now(CST).isoformat(timespec="seconds")

    for fname, obj in (("stock_names.json", names),
                       ("industry_stocks.json", {"ts": ts, "industries": inds}),
                       ("concept_map.json", {"ts": ts, "concepts": cons})):
        p = os.path.join(DATA_DIR, fname)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        n = len(obj) if isinstance(obj, dict) else 0
        print(f"[maps] {fname}: {n} 条")

    print(f"[maps] 个股名 {len(names)} | 行业 {len(inds)} | 概念 {len(cons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
