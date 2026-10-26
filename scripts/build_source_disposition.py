#!/usr/bin/env python3
"""build_source_disposition.py — 生成 data/api/source_disposition.json（可审计处置台账）。

目的：把「未接 31 个 + blocked 29 个」以及「板块成分不可达清单」从口头说明
变成可核验产物，避免每次都要重新讨论、也不让它们悬着当“待决事项”。

分类口径（按 id / registry notes 规则判定，全部可复核）：
  wired            已接执行器（来自 collect_all_sources.FETCHERS 与 sources.json）
  cloud_only       仅云端可跑（Python 库：akshare/baostock/tickflow）
  needs_key        需第三方注册/付费 key
  needs_parser     可达但需专门解析（HTML / 嵌套 JSON）
  params_pending   可达但参数未调通
  egress_limited   容器出口受限（云端可跑，本地 500/超时）
  archived_blocked 上游封禁/废弃/报表不存在，归档不绕过

用法: python3 scripts/build_source_disposition.py
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
CST = timezone(timedelta(hours=8))

CLOUD_ONLY = {"akshare", "baostock", "tickflow_free"}
NEEDS_KEY = {"alpha_vantage", "finnhub", "fmp", "marketstack", "twelvedata",
             "tushare", "tushare_pro", "klineshare_api", "iwencai_openapi"}
NEEDS_PARSER = {"ths_f10_news", "ths_finance_benefit", "ths_finance_cash",
                "ths_finance_debt", "ths_ipo_xgsr_html", "ths_lhb_yyb",
                "ths_board_kline", "sina_esg_hz", "sina_esg_msci", "sina_esg_rft",
                "sina_esg_stocks", "sina_esg_zd", "sina_option_stock_name",
                "cls_article_detail", "vango_articles", "zaobao_news"}
PARAMS_PENDING = {"yahoo_finance", "sina_moneyflow_zjlrqs", "sina_spot_goods",
                  "sina_us_index_static", "sina_hk_klc_js", "sina_futures_realtime",
                  "xueqiu_screener", "xueqiu_anon_token"}
EGRESS_LIMITED = {"szse_report_a_stock", "szse_report_etf_list", "szse_report_fund_list",
                  "cninfo_announce", "szse_api", "mootdx", "kaipanla"}
ARCHIVED_BLOCKED = {"cls_cn", "cls_nodeapi_dead", "dead_third_batch", "hexun_dead_all",
                    "stooq", "ths_rank_cxfl", "ths_funds_flow_rank", "eastmoney_stock_get",
                    "em_push2_clist", "em_push2his_kline_DEPRECATED", "gitcode_api",
                    "xiaohongshu_hot", "xueqiu_hot", "xueqiu_screener_dead_paths",
                    "em_datacenter_dead_reportnames", "em_lhb_dead_reports",
                    "em_rev_d_dead_reports"}


def classify(sid, has_fetcher, status):
    if has_fetcher:
        return "wired", "本地执行器已接入"
    if sid in CLOUD_ONLY:
        return "cloud_only", "Python 库，只能在云端 Actions 运行（属数据库侧采集）"
    if sid in NEEDS_KEY:
        return "needs_key", "需第三方注册/付费 key"
    if sid in EGRESS_LIMITED:
        return "egress_limited", "容器出口受限，云端可跑"
    if sid in NEEDS_PARSER:
        return "needs_parser", "接口可达，需 HTML/嵌套 JSON 专门解析"
    if sid in PARAMS_PENDING:
        return "params_pending", "接口可达，参数/编码未调通"
    if status in ("blocked", "unauth"):
        return "archived_blocked", "上游封禁/废弃/报表不存在，归档不绕过"
    return "pending_parse", "可达但尚未接入"


def main():
    import sources_registry as sr
    try:
        import collect_all_sources as cas
        fetchers = {n for g in cas.FETCHERS.values() for n, _ in g}
    except Exception:
        fetchers = set()

    ledger = {}
    lpath = os.path.join(ROOT, "data", "api", "sources.json")
    if os.path.exists(lpath):
        with open(lpath, encoding="utf-8") as f:
            ledger = json.load(f)

    items = []
    for src in sr.SOURCES:
        sid = src.get("id")
        status = src.get("status") or "missing"
        has = sid in fetchers
        disp, reason = classify(sid, has, status)
        items.append({
            "id": sid, "name": src.get("name"), "status": status,
            "has_fetcher": has, "disposition": disp, "reason": reason,
            "owner": "本地" if disp in ("wired", "needs_parser", "params_pending")
            else ("云端" if disp in ("cloud_only", "egress_limited") else "归档"),
        })

    boards = {"unreachable": [], "missing": []}
    spath = os.path.join(ROOT, "data", "api", "sectors.json")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            sec = json.load(f)
        members = sec.get("board_members") or {}
        for bk, v in members.items():
            if v.get("status") == "stale":
                boards["unreachable"].append({
                    "bk": bk, "name": v.get("name"), "kind": v.get("kind"),
                    "age_days": v.get("age_days"), "reason": "东财 push2 封禁 + 云端库无该板块成分",
                })
            elif v.get("status") == "missing":
                boards["missing"].append({"bk": bk, "name": v.get("name"),
                                          "kind": v.get("kind"),
                                          "reason": "无任何可用来源"})

    counts = {}
    for it in items:
        counts[it["disposition"]] = counts.get(it["disposition"], 0) + 1

    payload = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "counts": counts,
        "fetchers_total": len(fetchers),
        "ledger": {k: ledger.get(k) for k in
                   ("total", "usable", "with_fetcher", "live",
                    "usable_without_fetcher", "blocked_or_unauth")},
        "boards": {
            "unreachable_count": len(boards["unreachable"]),
            "missing_count": len(boards["missing"]),
            "unreachable": boards["unreachable"],
            "missing": boards["missing"],
        },
        "items": items,
    }
    out = os.path.join(ROOT, "data", "api", "source_disposition.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("[disposition] %s -> %s" % (counts, out))
    print("[disposition] boards unreachable=%d missing=%d"
          % (payload["boards"]["unreachable_count"], payload["boards"]["missing_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
