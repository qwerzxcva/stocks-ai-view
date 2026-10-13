#!/usr/bin/env python3
"""
merge_registry_to_catalog.py — 把 sources_registry.py 的新源同步进 catalog JSON

以 registry 为权威源: 每条 registry 源按 id 匹配 catalog 条目。
  - catalog 没有的 id → 追加到映射的 domain
  - catalog 已有的 id → 保留 catalog 原条目 (探测证据更细), 不覆盖
registry 状态映射 → catalog 状态:
  ok/degraded → available/degraded, blocked → blocked, unauth → unavailable, untested → unverified

用法: python3 merge_registry_to_catalog.py
"""
import json
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
CATALOG = "/root/workspace/stocks-data-live/data/research/data_source_catalog_20261013.json"
REGISTRY_DIR = "/root/workspace/stocks-ai-view/scripts"

sys.path.insert(0, REGISTRY_DIR)
from sources_registry import SOURCES  # noqa: E402

# registry id → catalog domain (只在需要追加时用)
DOMAIN_MAP = {
    "realtime_quote": ["tencent_quote", "sina_hq", "em_push2_data", "em_push2_clist",
                       "douyin_hot", "tv_screener", "toutiao_hot", "eastmoney_zdfenbu",
                       "eastmoney_zt_pool", "local_snapshot"],
    "kline": ["tencent_kline", "em_push2his_kline", "sina_kline_daily", "em_a_hist",
              "tencent_index_history", "sina_etf_hist", "eastmoney_kline", "baostock",
              "local_kline_bulk", "sina_kline_ma"],
    "capital_flow_lhb": ["em_datacenter_lhb", "em_datacenter_blocktrade", "em_datacenter_margin",
                         "em_zt_pool", "sina_lhb", "eastmoney_datacenter", "eastmoney_clist",
                         "eastmoney_stock_get", "em_push2_clist", "em_push2delay_clist"],
    "financial": ["ths_financial_abstract", "ths_financial_abstract_v2", "ths_industry_list",
                  "ths_concept_list", "macro_china_cpi_gdp", "em_lcpd_report",
                  "ths_strong_stocks", "ths_industry"],
    "news_sentiment": ["sina_news", "em_np_listapi_7x24", "remote_news", "douyin_hot_news",
                       "eastmoney_news", "cls_cn"],
    "fund_etf": ["em_fund_pingzhong", "em_fund_etf_hist_min", "em_fund_rank",
                 "eastmoney_fund_pingzhong", "eastmoney_fund_rank",
                 "jisilu_lof_index", "jisilu_lof_stock", "jisilu_etf_list",
                 "jisilu_cb_list_new", "jisilu_cb_adjust", "jisilu_cb_delisted"],
    "events": ["cninfo_announce", "em_ann_np", "szse_api", "sse_api", "sse_market",
               "jisilu_new_stock_apply", "jisilu_new_stock_hkipo", "jisilu_new_stock_sub",
               "jisilu_new_stock_super"],
    "social": ["douyin_hot_search", "xiaohongshu_hot", "xueqiu_hot", "xueqiu_anon_token",
               "tencent_news_author"],
    "exchange_official": ["sse_market", "szse_api_v2"],
    "paid_key_api": ["iwencai_openapi", "finnhub", "twelvedata", "fmp", "marketstack",
                     "tushare", "alpha_vantage"],
    "library_wrappers": ["akshare", "cn_a_stock_data", "sec_edgar"],
    "blocked_unavailable": ["yahoo_finance", "stooq", "kaipanla", "gitcode_api", "mootdx",
                            "coingecko"],
    "local_cache": ["industry_map_tv"],
}

STATUS_MAP = {
    "ok": "available",
    "degraded": "degraded",
    "blocked": "blocked",
    "unauth": "unavailable",
    "untested": "unverified",
}

FREQ_MAP = {
    "realtime": "realtime", "minute": "minute", "daily": "daily",
    "weekly": "weekly", "monthly": "monthly", "quarterly": "daily",
}


def reg_to_catalog_entry(s):
    """registry 条目 → catalog 条目格式。"""
    return {
        "id": s["id"],
        "name": s["name"],
        "domain": "",
        "url": s["url"],
        "auth": s.get("auth", "none"),
        "fields": s.get("fields", ""),
        "integration": "library_call (akshare)" if s["url"].startswith("via akshare")
                      else "library_call" if s["url"].startswith(("via ", "python 库"))
                      else "direct_json",
        "status": STATUS_MAP.get(s.get("status"), "unverified"),
        "evidence": {
            "http_code": None,
            "sample": "",
            "probe_time": datetime.now(CST).isoformat(timespec="seconds"),
            "container": "alpine-aarch64",
        },
        "rate_limit_suggestion": "10 req/min (registry 默认)",
        "waf_blocked": s.get("status") == "blocked",
        "notes": s.get("notes", ""),
        "degrade_to": s.get("degrade_to", []),
        "headers": {},
        "_source": "sources_registry",
    }


def main():
    with open(CATALOG, encoding="utf-8") as f:
        catalog = json.load(f)

    domains = catalog.setdefault("domains", {})
    existing = {item["id"] for items in domains.values() for item in items}

    # 建 id → domain 反查
    id2domain = {}
    for dom, ids in DOMAIN_MAP.items():
        for i in ids:
            id2domain.setdefault(i, dom)

    added = []
    for s in SOURCES:
        if s["id"] in existing:
            continue
        dom = id2domain.get(s["id"])
        if dom is None:
            # 未映射: 按状态兜底放 exchange_official? 不, 放到独立域
            dom = "registry_extra"
            domains.setdefault(dom, [])
        entry = reg_to_catalog_entry(s)
        entry["domain"] = dom
        domains.setdefault(dom, []).append(entry)
        existing.add(s["id"])
        added.append((s["id"], dom))

    catalog["timestamp"] = datetime.now(CST).isoformat(timespec="seconds")
    catalog.setdefault("note", "")
    catalog["note"] = (catalog["note"].rstrip() +
                      f"\n合并: sources_registry 追加 {len(added)} 条 (2026-10-13)").lstrip()

    with open(CATALOG, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in domains.values())
    print(f"catalog 总条目: {total}")
    print(f"registry 追加: {len(added)} 条")
    for i, d in added:
        print(f"  + [{d}] {i}")
    # 验证 id 唯一
    ids = [i.get("id") for d in domains.values() for i in d]
    assert len(ids) == len(set(ids)), f"重复 id: {[x for x in ids if ids.count(x) > 1]}"
    print("验证: JSON 合法, id 无重复 ✓")
    with open(CATALOG, encoding="utf-8") as f:
        json.load(f)
    print(f"→ {CATALOG}")


if __name__ == "__main__":
    main()
