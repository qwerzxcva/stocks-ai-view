#!/usr/bin/env python3
"""theme_mining.py — 题材（概念）深度挖掘报告：炒作路径 + 分支拆解 + 正宗度 + 补涨候选。

方法来源：公众号「马小帅不过7秒」概念挖掘方法论（用户指定借鉴 2026-10-24）：
  1. 题材拆成分支（产业链细分），不是把板块当一个标签
  2. 每支分「在炒的」vs「未启动潜在补涨」，并给补涨星级
  3. 区分正宗（逻辑硬）vs 沾边（小盘低价纯情绪）
  4. 讲清炒作路径时间线（先炒什么、后扩散到什么）

数据源（全部真实，不编造）：
  sector_pool.json     —— 板块池 tier1/2/3 分层（热度排序）+ 龙头 + 5/20 日涨幅
  limitup_attribution  —— 涨停梯队（连板高度/封板时间/行业）
  industry_map_em      —— 个股→行业（分支归类）
  bulk 日 K            —— 个股 20 日涨幅与 60 日位置（补涨判定）
  concept_heat         —— 概念板块 30 日时序（有则算起爆日）

产出 data/theme_report.json（云端 CI 每日盘后产出；软件端读同一文件）
CLI: --theme <板块名> | --auto（取 tier1 最热） | --all（全部 tier1 板块）
"""
import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from paths import AI_VIEW_DATA as DATA_DIR, BULK as KLINE_BULK  # noqa: E402

OUT_DEFAULT = os.path.join(DATA_DIR, "theme_report.json")
CST = timezone(timedelta(hours=8))


def load(name, default=None):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def holo_closes(code6: str, days: int = 65):
    """stocks-holo per-stock kline.json：{symbol, bars, from, to, data:[...]}。
    远端 t1 的 60/30/68 分片是 14B 占位（超 GitHub 100MB 限制推不上），故 K 线以 holo 为主源。"""
    pref = "sh" if code6[0] in ("6", "5", "9") else "sz"
    d = fetch_json(f"https://api.github.com/repos/jiegengxiangjianxue1/stocks-holo/"
                   f"contents/{pref}{code6}/kline.json")
    if not d or "content" not in d:
        return []
    try:
        j = json.loads(base64.b64decode(d["content"]).decode("utf-8", "replace"))
    except Exception:
        return []
    rows = j.get("data") or []
    out = []
    for r in rows:
        if isinstance(r, dict):
            dt, cl = r.get("date") or r.get("d"), r.get("close") or r.get("c")
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            dt, cl = r[0], r[1]
        else:
            continue
        try:
            out.append((str(dt), float(cl)))
        except (TypeError, ValueError):
            continue
    out.sort()
    return [c for _, c in out[-days:]]


def bulk_closes(code6: str, shard: str, days: int = 65):
    """bulk 分片读日 K。分片内 code 列带 sh/sz 前缀（如 sh603528），故两种形态都匹配。"""
    path = os.path.join(KLINE_BULK, f"day_qfq_{shard}.csv")
    if not os.path.exists(path):
        return []
    pref = "sh" if code6[0] in ("6", "5", "9") else "sz"
    targets = (f"{pref}{code6}|", f"{code6}|")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith(targets):
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 5:
                    continue
                try:
                    out.append((p[1], float(p[3])))
                except ValueError:
                    continue
    except Exception:
        return []
    out.sort()
    return [c for _, c in out[-days:]]


def shard_of(code6: str) -> str:
    p = code6[:2]
    return p if p in ("00", "30", "60", "68", "20") else "00"


def closes_of(code6: str, days: int = 65):
    """K 线收盘序列：holo 主源（远端可用、个股级），bulk 兜底（本地全）"""
    return holo_closes(code6, days) or bulk_closes(code6, shard_of(code6), days)


def pct(a, b):
    return round((a - b) / b * 100, 2) if b else None


import urllib.request


def fetch_json(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "theme-mining/1.0"})
        return json.load(urllib.request.urlopen(req, timeout=25))
    except Exception:
        return None


DB_MARKET = "https://raw.githubusercontent.com/jiegengxiangjianxue1/db-market/main"


def board_members(bk: str):
    """取板块成分股（db-market 每日更新，board-constituents 管线产出）"""
    d = fetch_json(f"{DB_MARKET}/sector/industry_constituents/{bk}.json")
    if not d:
        d = fetch_json(f"{DB_MARKET}/sector/concept_constituents/{bk}.json")
    if not d:
        return []
    return [str(m.get("code"))[-6:] for m in (d.get("members") or []) if m.get("code")]


def sectors_index():
    """板块名 → bk 代码"""
    out = {}
    for kind in ("industry", "concept"):
        d = fetch_json(f"{DB_MARKET}/sector/{kind}.json") or {}
        for b in (d.get("boards") or []):
            if b.get("bk") and b.get("name"):
                out[b["name"]] = b["bk"]
    return out


def mine_theme(theme: str, pool: dict, la: dict, imap: dict, heat: dict, days: int,
               bk_map: dict) -> dict:
    """挖一个题材：tier 信息 + 涨停梯队 + 同分支个股分层"""
    tiers = {}
    for t in ("tier1", "tier2", "tier3"):
        for s in (pool.get(t) or []):
            tiers[s.get("sector")] = {"tier": t, **s}

    meta = tiers.get(theme) or {}

    # 1) 炒作路径：概念 K 线时序（有则用）→ 起爆日/累计；无则用板块 1/5/20 日涨幅描述
    path_notes = []
    series = []
    for b in (heat.get("boards") or heat.get("items") or []):
        if (b.get("name") or b.get("board")) == theme:
            series = b.get("series") or []
            break
    if series:
        chgs = []
        for i in range(1, len(series)):
            p0, p1 = series[i - 1], series[i]
            c0 = p0.get("close") if isinstance(p0, dict) else None
            c1 = p1.get("close") if isinstance(p1, dict) else None
            if c0 and c1:
                chgs.append((p1.get("date"), pct(c1, c0)))
        if chgs:
            best = max(chgs, key=lambda x: x[1] or 0)
            if (best[1] or 0) >= 3:
                path_notes.append(f"起爆日 {best[0]}（板块单日 {best[1]:+.2f}%）")
            path_notes.append(f"近 {len(chgs)} 日累计 {sum(c for _, c in chgs if c):+.2f}%")
    if not path_notes and meta:
        path_notes.append(
            f"板块近1日 {meta.get('avg_chg_1d')}% / 5日 {meta.get('chg_5d')}% / 20日 {meta.get('chg_20d')}%"
            f"（tier={meta.get('tier')}）")

    # 2) 涨停梯队（在炒的）：本题材所属行业内的涨停股，按连板高度排序
    zt = [it for it in (la.get("items") or [])
          if (it.get("industry") or "") == theme or str(it.get("sector") or "") == theme]
    ladder = {}
    for it in (la.get("items") or []):
        lb = it.get("lianban") or 0
        if lb >= 1:
            ladder[it.get("industry") or "未分类"] = ladder.get(it.get("industry") or "未分类", 0) + 1

    hot = []
    for it in zt:
        code = str(it.get("code") or "")
        c6 = code[-6:] if len(code) >= 6 else code
        closes = closes_of(c6)
        last = closes[-1] if closes else it.get("price")
        chg20 = pct(last, closes[-21]) if len(closes) > 21 else None
        lo, hi = (min(closes), max(closes)) if closes else (None, None)
        pos = round((last - lo) / (hi - lo) * 100, 1) if closes and hi > lo else None
        hot.append({
            "code": c6, "name": it.get("name"),
            "lianban": it.get("lianban"), "lianban_label": it.get("lianban_label"),
            "seal_bucket": it.get("seal_bucket"), "seal_label": it.get("seal_label"),
            "chg20": chg20, "pos": pos, "price": it.get("price"),
            "authentic": "正宗" if (it.get("lianban") or 0) >= 2 else "待验证",
        })
    hot.sort(key=lambda x: (-(x["lianban"] or 0), -(x["chg20"] or 0)))

    # 3) 分支拆解：本题材板块成分股 → 未大涨者为补涨候选
    #    主源：db-market 板块成分股（board-constituents 管线每日产出，实测非空）
    #    兜底：industry_map_em.map（个股→行业，1050 只）
    ind_stocks = None
    bk = bk_map.get(theme)
    if bk:
        ind_stocks = board_members(bk)
    if not ind_stocks:
        stocks_map = imap.get("map")
        if isinstance(stocks_map, dict):
            ind_stocks = [c for c, v in stocks_map.items() if v == theme]

    watch = []
    MAIN = ("600", "601", "603", "605", "000", "001", "002")
    # 主板红线：在炒列表只留可交易标的（排除 300/301/688/北交所）
    hot = [h for h in hot if h["code"][:3] in MAIN]
    seen = {h["code"] for h in hot}
    for c6 in (ind_stocks or [])[:120]:
        if c6 in seen:
            continue
        if c6[:3] not in MAIN:   # 主板红线：补涨候选同样过滤
            continue
        closes = closes_of(c6)
        if len(closes) < 21:
            continue
        last = closes[-1]
        chg20 = pct(last, closes[-21])
        lo, hi = min(closes), max(closes)
        pos = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else None
        # 补涨条件：未大涨(<=15%) + 位置不高(<=70%)；
        # 排除崩盘票：20日跌超 25% 属趋势破坏，不是「未启动」
        if (chg20 or 0) > 15 or (pos or 100) > 70 or (chg20 or 0) < -25:
            continue
        star = 1
        if -5 <= (chg20 or 0) <= 5:
            star += 1
        if (pos or 100) <= 40:
            star += 1
        if (chg20 or 0) > 0:
            star += 1
        watch.append({"code": c6, "name": None, "chg20": chg20, "pos": pos,
                      "close": last, "star": min(3, star)})
    watch.sort(key=lambda x: (-x["star"], x["pos"] or 100))

    return {
        "theme": theme,
        "tier": meta.get("tier"),
        "score": meta.get("score"),
        "leader": meta.get("leader"),
        "chg_1d": meta.get("avg_chg_1d"), "chg_5d": meta.get("chg_5d"), "chg_20d": meta.get("chg_20d"),
        "path": path_notes,
        "n_zt": len(zt),
        "hot": hot[:15],
        "watchlist": watch[:15],
        "degraded": ([] if (zt or watch) else ["该题材无涨停梯队且无同分支个股数据"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="题材深度挖掘报告")
    ap.add_argument("--theme", help="题材/板块名")
    ap.add_argument("--auto", action="store_true", help="取 tier1 最热题材")
    ap.add_argument("--all", action="store_true", help="挖全部 tier1 板块")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    pool = load("sector_pool.json") or {}
    la = load("limitup_attribution.json") or {}
    imap = load("industry_map_em.json") or {}
    heat = load("concept_heat.json") or {}

    themes = []
    if args.theme:
        themes = [args.theme]
    elif args.all:
        themes = [s.get("sector") for s in (pool.get("tier1") or [])]
    elif args.auto:
        t1 = pool.get("tier1") or []
        themes = [t1[0]["sector"]] if t1 else []
    if not themes:
        print("需要 --theme/--auto/--all，且 sector_pool 需有 tier1", file=sys.stderr)
        sys.exit(2)

    bk_map = sectors_index()
    reports = [mine_theme(t, pool, la, imap, heat, args.days, bk_map) for t in themes]
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "theme_mining v1 (炒作路径/分支拆解/正宗度/补涨候选)",
        "method": "公众号概念挖掘方法论：分支拆解 + 在炒/补涨分层 + 补涨星级",
        "n_themes": len(reports),
        "themes": reports,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"-> {args.out} ({os.path.getsize(args.out)} B)")
    for r in reports:
        print(f"  【{r['theme']}】tier={r['tier']} 龙头={r['leader']} "
              f"涨停{len(r['hot'])}只 补涨候选{len(r['watchlist'])}只")
        for n in r["path"]:
            print(f"     路径: {n}")


if __name__ == "__main__":
    main()
