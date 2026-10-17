#!/usr/bin/env python3
"""
build_industry_map_em.py — 构建「个股 → 东财中文行业」映射

背景: multidim_engine 的「板块面」维度一直是占位分 50。
根因是 load_sector_map() 用 industry_map_tv.json (TradingView 英文行业,
如 "Electrical Products") 去查 sector_pool.json 的中文板块名 (如「油服工程」),
两者永不匹配, dim_sector() 直接返回 None, 上游再填 50。

本脚本用东财行业成分股接口 (fs=b:BKxxxx) 反查, 产出中文行业映射,
让「板块面」能真正按所属板块的当日强度/资金打分。

用法:
  python3 build_industry_map_em.py --out data/industry_map_em.json
  python3 build_industry_map_em.py --limit 30   # 只取前 N 个板块 (调试)
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}


def _get(url, timeout=15, retries=8):
    """东财 push2 对连续请求会 RemoteDisconnected。

    实测: 退避 2s 时连续 4 次全失败, 拉到 6s 后第 6 次才通。
    所以这里用固定 6s 退避 + 8 次重试, 靠时长换成功率。
    """
    for i in range(retries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception:
            if i < retries - 1:
                time.sleep(6)


def _j(b):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return {}


def fetch_em_boards(fs_filter, max_pages=8):
    """东财板块列表。fs=m:90+t:2 是行业 (~86), fs=m:90+t:3 是概念 (400+)。

    必须分页: 东财把 pz 上限压到 100 (传 200 也只回 100 个),
    而完整行业表有 500+ 个。只取一页会让半导体/化工/黄金这些关键
    行业缺失, 下游 news_transmission 的传导表大面积匹配不上
    (2026-10-15 实测: 单页只拿到 80 个行业, 半导体/黄金/化工全是 0 只)。
    概念板块 (t:3) 覆盖燃料电池/飞行汽车等题材, 与行业互为补充。
    """
    out = []
    seen = set()
    for pn in range(1, max_pages + 1):
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?fid=f3&po=1&pz=100&pn=%d&np=1&fltt=2&invt=2"
               "&fs=%s&fields=f12,f14" % (pn, fs_filter))
        d = _j(_get(url))
        items = ((d.get("data") or {}).get("diff")) or []
        if not items:
            break
        for it in items:
            name = str(it.get("f14", "") or "").strip()
            code = str(it.get("f12", "") or "").strip()
            if name and code and name not in seen:
                seen.add(name)
                out.append({"code": code, "name": name})
        if len(items) < 100:
            break
        time.sleep(0.5)
    return out


def fetch_em_industries(max_pages=8):
    return fetch_em_boards("m:90+t:2", max_pages)


def fetch_em_concepts(max_pages=8):
    return fetch_em_boards("m:90+t:3", max_pages)


def fetch_industry_stocks(bk_code, page_size=100):
    """东财行业成分股 (fs=b:BKxxxx), 分页拉全。

    page_size 用 100 而不是 200: 东财把 pz 上限压到 100, 传 200
    实际也只回 100 条, 但代码按 200 判断「还有没有下一页」,
    会把不足一页的情况误判为已到底 (len(items) < page_size 永远成立)。
    """
    out = []
    pn = 1
    while pn <= 10:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?fid=f3&po=1&pz=%d&pn=%d&np=1&fltt=2&invt=2"
               "&fs=b:%s&fields=f12,f14" % (page_size, pn, bk_code))
        d = _j(_get(url))
        items = ((d.get("data") or {}).get("diff")) or []
        if not items:
            break
        for it in items:
            code = str(it.get("f12", "") or "").strip()
            name = str(it.get("f14", "") or "").strip()
            if code.isdigit() and len(code) == 6:
                out.append(code)
        if len(items) < page_size:
            break
        pn += 1
        time.sleep(0.4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        HERE, "..", "data", "industry_map_em.json"))
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 个板块 (0=全部)")
    args = ap.parse_args()

    inds = fetch_em_boards("m:90+t:2", 8)
    # 概念板块 (400+, 含燃料电池/飞行汽车等题材): 与行业互补, 扩大成分股覆盖
    concepts = fetch_em_boards("m:90+t:3", 8)
    if not inds and not concepts:
        print("[indmap] 东财行业/概念列表取数失败 (限流?), 终止")
        return 1
    if args.limit:
        inds = inds[:args.limit]
        concepts = concepts[:args.limit]
    print("[indmap] 行业 %d 个 + 概念 %d 个, 开始拉成分股..." % (
        len(inds), len(concepts)), flush=True)

    code2ind = {}
    ok = 0
    for i, b in enumerate(inds):
        codes = fetch_industry_stocks(b["code"])
        if codes:
            ok += 1
            for c in codes:
                # 一只票可能在多个板块, 保留第一个 (东财按涨幅排序, 靠前的是主板块)
                code2ind.setdefault(c, b["name"])
        if (i + 1) % 10 == 0:
            print("[indmap] 行业 %d/%d 完成, 已映射 %d 只" % (
                i + 1, len(inds), len(code2ind)), flush=True)
        time.sleep(0.3)

    # 概念板块同样反查 (不进 code2ind 主映射, 单独存概念→成分, 供周期池/题材用)
    concept_map = {}
    okc = 0
    for i, b in enumerate(concepts):
        codes = fetch_industry_stocks(b["code"])
        if codes:
            okc += 1
            concept_map[b["name"]] = codes
        if (i + 1) % 20 == 0:
            print("[indmap] 概念 %d/%d 完成" % (i + 1, len(concepts)), flush=True)
        time.sleep(0.3)

    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "engine": "build_industry_map_em v2 (东财行业成分股反查 + 概念板块)",
        "n_industries": len(inds),
        "n_industries_ok": ok,
        "n_concepts": len(concepts),
        "n_concepts_ok": okc,
        "n_stocks": len(code2ind),
        "map": code2ind,
        "concept_map": concept_map,
        "caveat": "个股→东财行业多对一; 保留首个命中板块 (东财按涨幅排序); "
                  "concept_map 是概念→[代码] 供周期池/题材引擎用",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("[indmap] -> %s (%d 只 / %d 板块)" % (
        args.out, len(code2ind), ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
