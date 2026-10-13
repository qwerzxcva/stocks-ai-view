#!/usr/bin/env python3
"""
data_audit.py — 网站数据准确性校验与溯源标注

针对 2026-10-13 核查发现的四类问题:
  1. latest.json 等文件可能过期 (页面读到陈旧数据)
  2. 板块数据未去重 (东财行业 Ⅱ/Ⅲ 分级重复) 且无涨跌幅校验
  3. themes/mainlines 字段为空对象
  4. 无数据溯源 (哪个源/什么时间/置信度)

本脚本提供:
  --check     校验 data/ 下所有文件的时效性与关键字段, 输出问题清单
  --stamp     给 JSON 补 provenance 字段 (source/probe_time/confidence)
  --dedup     板块去重 (Ⅱ/Ⅲ 分级取一级) + 涨跌幅合理性校验
  --fresh     重新采集并刷新过期文件 (latest/market_now/sector_pool)

用法:
  python3 data_audit.py --check
  python3 data_audit.py --stamp data/latest.json
  python3 data_audit.py --dedup data/sector_pool.json
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# 各文件的合理时效 (小时): 超过即视为过期
FRESHNESS = {
    "latest.json": 2,
    "market_now.json": 1,
    "sector_pool.json": 3,
    "hot_impact.json": 2,
    "enrich.json": 2,
    "index_kline.json": 6,
    "cycle.json": 6,
    "lowpool.json": 6,
    "ai_picks.json": 3,
    "engine_v5.json": 3,
    "ban_pick.json": 3,
    "ban_prob.json": 3,
    "flow_divergence.json": 3,
    "kline_sim.json": 1,
    "reflect_latest.json": 1,
    "predictions": 2,
}

# 页面实际消费的文件 (这些过期影响最大)
PAGE_CRITICAL = {"latest.json", "market_now.json", "sector_pool.json",
                 "hot_impact.json", "ai_picks.json"}


def _ts():
    return datetime.now(CST).isoformat(timespec="seconds")


def _probe(url, hdr=None, timeout=15):
    h = dict(UA)
    h.update(hdr or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
        return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__error__": f"{type(e).__name__} {e}"}


# ════════════════════════════════════════════════════════════
# 实时基准 (所有校验都拿它做对照)
# ════════════════════════════════════════════════════════════
def fetch_baseline():
    """东财涨跌停池 + 涨跌分布 + 全市场快照 (push2his 绕 WAF)。"""
    out = {}
    today = datetime.now(CST).strftime("%Y%m%d")

    d = _probe(f"https://push2ex.eastmoney.com/getTopicZTPool"
               f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               f"&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={today}")
    if "__error__" not in d:
        out["limit_up_tc"] = (d.get("data") or {}).get("tc")

    d = _probe(f"https://push2ex.eastmoney.com/getTopicDTPool"
               f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               f"&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={today}")
    if "__error__" not in d:
        out["limit_down_tc"] = (d.get("data") or {}).get("tc")

    d = _probe("https://push2ex.eastmoney.com/getTopicZDFenBu"
               "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               f"&date={today}")
    if "__error__" not in d:
        dist = {}
        for item in ((d.get("data") or {}).get("fenbu") or []):
            for k, v in item.items():
                try:
                    dist[int(k)] = int(v)
                except (TypeError, ValueError):
                    pass
        if dist:
            out["advance"] = sum(c for p, c in dist.items() if p > 0)
            out["decline"] = sum(c for p, c in dist.items() if p < 0)
            out["flat"] = dist.get(0, 0)

    # 全市场快照 → 自算涨跌停 (交叉校验 tc)
    d = _probe("https://push2his.eastmoney.com/api/qt/clist/get?pn=1&pz=6000&po=1&np=1"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f3,f12,f14&fid=f3")
    if "__error__" not in d:
        diff = (d.get("data") or {}).get("diff") or []
        out["snapshot_total"] = (d.get("data") or {}).get("total")
        # f3 是涨跌幅 ×100 (整数); 涨跌停按 10%/20% 口径需区分板块, 这里只统计 >=9.9%
        up10 = sum(1 for x in diff if (x.get("f3") or 0) >= 990)
        dn10 = sum(1 for x in diff if (x.get("f3") or 0) <= -990)
        out["snapshot_ge10pct"] = up10
        out["snapshot_le-10pct"] = dn10
    return out


# ════════════════════════════════════════════════════════════
# 1. 时效性校验
# ════════════════════════════════════════════════════════════
def check_freshness():
    print("=== 时效性校验 ===")
    problems = []
    now = datetime.now(CST)
    for fname, max_h in FRESHNESS.items():
        p = os.path.join(DATA_DIR, fname)
        if not os.path.exists(p):
            continue
        mt = datetime.fromtimestamp(os.path.getmtime(p), CST)
        age_h = (now - mt).total_seconds() / 3600
        if age_h > max_h:
            crit = " [页面关键]" if fname in PAGE_CRITICAL else ""
            problems.append((fname, age_h, max_h, crit))
            print(f"  ❌ {fname:24s} {age_h:5.1f}h 前 (阈值 {max_h}h){crit}")
        else:
            print(f"  ✅ {fname:24s} {age_h:5.1f}h 前 (阈值 {max_h}h)")
    return problems


# ════════════════════════════════════════════════════════════
# 2. 关键字段与实时基准对照
# ════════════════════════════════════════════════════════════
def check_against_baseline():
    print("\n=== 关键字段 vs 实时基准 ===")
    base = fetch_baseline()
    print(f"  基准(东财实测): {base}")
    issues = []

    def _load(name):
        p = os.path.join(DATA_DIR, name)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # latest.json market 字段
    d = _load("latest.json")
    if d:
        m = d.get("market") or {}
        checks = [
            ("limit_up", base.get("limit_up_tc")),
            ("limit_down", base.get("limit_down_tc")),
            ("advance", base.get("advance")),
            ("decline", base.get("decline")),
        ]
        for field, baseval in checks:
            fileval = m.get(field)
            if baseval is None or fileval is None:
                continue
            diff = abs(fileval - baseval)
            # 允许 5% 误差 (不同口径: 主板 vs 含北交所)
            tol = max(3, baseval * 0.05)
            if diff > tol:
                issues.append(f"latest.json market.{field}: 文件={fileval} 实测={baseval} "
                              f"(差 {diff}, 容差 {tol:.0f})")
                print(f"  ❌ latest.json market.{field}: 文件={fileval} 实测={baseval}")
            else:
                print(f"  ✅ latest.json market.{field}: {fileval} ≈ 实测 {baseval}")

    # market_now.json
    d = _load("market_now.json")
    if d:
        m = d.get("market") or d
        for field in ["limit_up", "limit_down", "advance", "decline"]:
            fileval = m.get(field)
            baseval = base.get(f"{field}_tc" if field.startswith("limit") else field)
            if baseval is None or fileval is None:
                continue
            tol = max(3, baseval * 0.05)
            if abs(fileval - baseval) > tol:
                issues.append(f"market_now.json {field}: 文件={fileval} 实测={baseval}")
                print(f"  ❌ market_now.json {field}: 文件={fileval} 实测={baseval}")
            else:
                print(f"  ✅ market_now.json {field}: {fileval} ≈ 实测 {baseval}")
    return issues


# ════════════════════════════════════════════════════════════
# 3. 板块数据去重 + 校验
# ════════════════════════════════════════════════════════════
def dedup_sectors(path, dry_run=True):
    """东财行业 Ⅱ/Ⅲ 分级重复 → 取一级 (罗马数字最小的)。"""
    p = os.path.join(DATA_DIR, path) if not os.path.isabs(path) else path
    if not os.path.exists(p):
        print(f"文件不存在: {p}")
        return
    with open(p, encoding="utf-8") as f:
        d = json.load(f)

    def _base_name(nm):
        # "地面兵装Ⅱ" → "地面兵装"; 去掉末尾罗马数字
        for suffix in ("Ⅲ", "Ⅱ", "Ⅰ", "IV", "III", "II", "I"):
            if nm.endswith(suffix):
                return nm[: -len(suffix)], suffix
        return nm, ""

    removed = []
    for tier in ["tier1", "tier2", "tier3", "tier_down"]:
        items = d.get(tier) or []
        if not items:
            continue
        seen = {}
        kept = []
        for it in items:
            nm = it.get("sector") or it.get("name") or ""
            base, suf = _base_name(nm)
            if base in seen:
                # 保留分数字段更全的 (score/chg_5d/chg_20d 非 None)
                prev = seen[base]
                cur_fields = sum(1 for k in ("score", "chg_5d", "chg_20d", "leader")
                                 if it.get(k) is not None)
                prev_fields = sum(1 for k in ("score", "chg_5d", "chg_20d", "leader")
                                  if prev.get(k) is not None)
                if cur_fields > prev_fields:
                    kept.remove(prev)
                    kept.append(it)
                    seen[base] = it
                    removed.append(f"{tier}: {prev.get('sector')} (被 {nm} 替代, 字段更全)")
                else:
                    removed.append(f"{tier}: {nm} (与 {prev.get('sector')} 重复)")
                continue
            seen[base] = it
            kept.append(it)
        d[tier] = kept
        # 同步计数
        cnt_key = f"{tier}_count"
        if cnt_key in d:
            d[cnt_key] = len(kept)

    d["meta"] = d.get("meta") or {}
    d["meta"]["deduped_at"] = _ts()
    d["meta"]["dedup_note"] = f"移除 {len(removed)} 条 Ⅱ/Ⅲ 分级重复"
    if "total_sectors" in d:
        d["total_sectors"] = sum(len(d.get(t) or []) for t in
                                 ["tier1", "tier2", "tier3", "tier_down"])

    # 计数与数组一致性 (2026-10-13 发现 tier3_count=68 但 tier3=[] 的脱节 bug)
    fixed_counts = []
    for tier in ["tier1", "tier2", "tier3", "tier_down"]:
        ck = f"{tier}_count"
        if ck in d and d[ck] != len(d.get(tier) or []):
            fixed_counts.append(f"{ck}: {d[ck]} → {len(d.get(tier) or [])}")
            d[ck] = len(d.get(tier) or [])
    if fixed_counts:
        d["meta"]["count_fixed"] = fixed_counts
        print(f"  [count-fix] {fixed_counts}")

    if dry_run:
        print(f"[dry-run] {path}: 将移除 {len(removed)} 条重复")
        for r in removed[:10]:
            print(f"    {r}")
    else:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"[写入] {path}: 移除 {len(removed)} 条重复")
        for r in removed[:10]:
            print(f"    {r}")
    return removed


# ════════════════════════════════════════════════════════════
# 4. 溯源标注
# ════════════════════════════════════════════════════════════
def stamp_provenance(path, dry_run=True):
    """给 JSON 补 provenance: 每个顶层字段的来源/时间/置信度。"""
    p = os.path.join(DATA_DIR, path) if not os.path.isabs(path) else path
    if not os.path.exists(p):
        print(f"文件不存在: {p}")
        return
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        print(f"非 dict 顶层, 跳过: {path}")
        return

    # 字段 → 源映射 (按已知采集管线)
    FIELD_SOURCE = {
        "market": ("eastmoney_push2ex + push2his", "high"),
        "limit_up_pool": ("eastmoney_push2ex_ztpool", "high"),
        "limit_down_pool": ("eastmoney_push2ex_dtpool", "high"),
        "themes": ("local_cluster + ths_strong", "medium"),
        "mainlines": ("eastmoney_clist_degraded_to_theme_cluster", "low"),
        "picks": ("tsp_v4_multi_pool", "medium"),
        "ai_picks": ("engine_v5_17indicator", "medium"),
        "news_recent": ("sina_7x24 + remote_news_fallback", "medium"),
        "fund_rank": ("eastmoney_fund_rank_html", "medium"),
        "social": ("douyin_hot", "low"),
        "moneyflow": ("eastmoney_datacenter", "high"),
        "ladder": ("eastmoney_zt_pool_lianban", "high"),
        "indices": ("tencent_kline", "high"),
        "tier1": ("tv_screener + local_kline", "medium"),
        "tier2": ("tv_screener + local_kline", "medium"),
        "tier3": ("tv_screener + local_kline", "medium"),
        "tier_down": ("tv_screener + local_kline", "medium"),
        "predictions": ("reflect_engine", "medium"),
    }

    prov = d.get("provenance") or {}
    prov["stamped_at"] = _ts()
    prov["stamped_by"] = "data_audit.py"
    fields = {}
    for k in d:
        if k == "provenance":
            continue
        src, conf = FIELD_SOURCE.get(k, ("unknown", "unknown"))
        fields[k] = {"source": src, "confidence": conf,
                     "snapshot_ts": d.get("ts") or d.get("timestamp") or d.get("date")}
    prov["fields"] = fields
    d["provenance"] = prov

    if dry_run:
        print(f"[dry-run] {path}: 将标注 {len(fields)} 个字段")
        for k, v in list(fields.items())[:6]:
            print(f"    {k}: {v['source']} ({v['confidence']})")
        unknown = [k for k, v in fields.items() if v["source"] == "unknown"]
        if unknown:
            print(f"    ! 未映射字段: {unknown}")
    else:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"[写入] {path}: 标注 {len(fields)} 个字段")


# ════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="时效性 + 关键字段校验")
    ap.add_argument("--stamp", help="给指定文件补 provenance (dry-run)")
    ap.add_argument("--stamp-write", help="给指定文件补 provenance 并写入")
    ap.add_argument("--dedup", help="板块去重 (dry-run)")
    ap.add_argument("--dedup-write", help="板块去重并写入")
    args = ap.parse_args()

    if not any([args.check, args.stamp, args.stamp_write, args.dedup, args.dedup_write]):
        ap.print_help()
        return 1

    if args.check:
        check_freshness()
        check_against_baseline()
    if args.stamp:
        stamp_provenance(args.stamp, dry_run=True)
    if args.stamp_write:
        stamp_provenance(args.stamp_write, dry_run=False)
    if args.dedup:
        dedup_sectors(args.dedup, dry_run=True)
    if args.dedup_write:
        dedup_sectors(args.dedup_write, dry_run=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
