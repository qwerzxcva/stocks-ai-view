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
import urllib.request
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
    """行业 → 成分股。

    2026-10-16 修: 原实现从 shard_*.json 的 quote.industry 取, 但该字段
    在快照里大面积缺失 (实测只产出 2 个行业), 导致 news_impact 的行业映射层
    几乎失效 —— match_stat.industry 长期为 0, 87% 新闻 unmapped。

    现优先用 industry_map_em.json (东财中文行业, 实测 1050 只 / 100 个行业,
    由 build_industry_map_em.py 产出), shard quote 作补充。
    """
    items = {}
    # 主源: industry_map_em (覆盖最全)
    d = load("industry_map_em.json")
    if d:
        m = d.get("map") or {}
        if isinstance(m, dict):
            for k, v in m.items():
                c = str(k).replace("sh", "").replace("sz", "").replace("bj", "")
                if isinstance(v, str) and v:
                    items[c] = v
    # 补充: shard 快照的 quote.industry
    for sp in glob.glob(os.path.join(DATA_DIR, "shard_*.json")):
        try:
            sd = json.load(open(sp, encoding="utf-8"))
        except Exception:
            continue
        for r in ((sd.get("data") or {}).get("quote") or []):
            code = str(r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
            ind = r.get("industry")
            if len(code) == 6 and code.isdigit() and ind:
                items.setdefault(code, ind)
    if not items:
        return {}
    by_ind = {}
    for c, ind in items.items():
        by_ind.setdefault(ind, []).append(c)
    # 加上产业链归并 (关键词比原来的更宽, 覆盖东财细分行业名)
    chain = {
        "半导体": ["半导体", "芯片", "集成电路", "元件", "印制电路板"],
        "新能源": ["光伏", "电池", "新能源", "电源设备"],
        "人工智能": ["软件", "互联网", "计算机", "通信", "光学", "电子"],
        "医药": ["医药", "医疗", "生物", "中药", "化学制药"],
        "金融": ["银行", "保险", "证券", "多元金融"],
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
    """概念板块 → 成分股 (或至少板块名)。

    2026-10-16 修: 原实现读 shard_a.json 的 sector 组, 但 shard_a 实际只有
    quote/event/news/fund/kline/macro 六组, **没有 sector** —— 所以概念映射
    恒为空 (实测 concept_map 只有 2 条), news_impact 的 L1 概念层完全失效。

    现改用腾讯板块排行 (proxy.finance.qq.com mktHs/rank, t=01 行业 /
    t=02 概念), 零鉴权可用, 返回板块名 + 领涨股 (nzg_code)。
    同花顺成分股接口已 401 (2026-10-15 实测), 拿不到完整成分股时保留板块名,
    news_impact 会走 L3 名称兜底匹配。
    """
    concepts = {}
    for t, label in (("01", "行业"), ("02", "概念")):
        try:
            url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
                   f"?l=100&p=1&t={t}/averatio")
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
                "Referer": "https://gu.qq.com/"})
            d = json.loads(urllib.request.urlopen(req, timeout=20).read()
                           .decode("utf-8", "replace"))
            rows = d.get("data") or []
            n_with = 0
            for r in rows:
                nm = str(r.get("bd_name") or "").strip()
                if not nm or len(nm) < 3:
                    continue
                concepts.setdefault(nm, [])
                code = str(r.get("nzg_code") or "").replace("sh", "").replace("sz", "")
                if code and code not in concepts[nm]:
                    concepts[nm].append(code)
                    n_with += 1
            print(f"[maps] 腾讯{label}板块 {len(rows)} 个 (带领涨股 {n_with})",
                  file=sys.stderr)
        except Exception as e:
            print(f"[maps] 腾讯{label}板块排行失败: {type(e).__name__} {e}",
                  file=sys.stderr)
    if not any(v for v in concepts.values()):
        print("[maps] 警告: 板块只拿到名称没有成分股, 概念映射退化为仅名称 "
              "(news_impact 走 L3 兜底)", file=sys.stderr)
    return concepts


def main():
    names = build_stock_names()
    inds = build_industry_stocks()
    cons = build_concept_map()
    ts = datetime.now(CST).isoformat(timespec="seconds")

    for fname, obj, cnt in (("stock_names.json", names, len(names)),
                             ("industry_stocks.json", {"ts": ts, "industries": inds},
                              len(inds)),
                             ("concept_map.json", {"ts": ts, "concepts": cons},
                              len(cons))):
        p = os.path.join(DATA_DIR, fname)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        print(f"[maps] {fname}: {cnt} 条")

    print(f"[maps] 个股名 {len(names)} | 行业 {len(inds)} | 概念 {len(cons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
