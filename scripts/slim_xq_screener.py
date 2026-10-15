#!/usr/bin/env python3
# slim_xq_screener.py — 从雪球全量 screener 提炼选股核心字段 (减小入库体积)
#
# 背景: 雪球 screener 全量 5000 只 × 40 字段 = 4.5MB, 直接入库太大。
# 本脚本抽选股真正用的 18 个字段 (估值/成长/资金/情绪), 并做派生计算,
# 输出约 1MB 的选股池, 供 multi_screener / ai_pick 直接消费。
import argparse
import json
import sys

# 保留字段: 标识 + 行情 + 估值 + 成长 + 资金 + 情绪
KEEP = [
    "symbol", "name", "current", "percent", "chg",
    "volume", "amount", "turnover_rate", "amplitude", "volume_ratio",
    "pe_ttm", "pb_ttm", "ps", "pcf", "dividend_yield",
    "roe_ttm", "net_profit_cagr", "income_cagr",
    "market_capital", "float_market_capital",
    "limitup_days", "percent5m", "north_net_inflow", "main_net_inflows",
    "followers", "current_year_percent", "zdf_y", "eps",
]
# 丢弃: dual_counter_mapping_symbol/mapping_quote_current/has_follow/tick_size/
#       lot_size/type/issue_date_ts/north_net_inflow_time/first_percent/total_percent/
#       float_shares/total_shares/state (空值率高或可由其他字段推导)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    d = json.load(open(args.src, encoding="utf-8"))
    rows = d["markets"]["CN"]["rows"]
    out_rows = []
    for r in rows:
        o = {k: r.get(k) for k in KEEP}
        # 归一化 symbol: SZ000001 -> 000001 (与其他源对齐)
        sym = (o.get("symbol") or "")
        o["code"] = sym[2:] if len(sym) > 2 and sym[:2] in ("SZ", "SH", "BJ") else sym
        o["market"] = {"SZ": "深", "SH": "沪", "BJ": "北"}.get(sym[:2], sym[:2])
        # 派生: PEG = pe_ttm / net_profit_cagr (成长性修正估值)
        try:
            pe, g = o.get("pe_ttm"), o.get("net_profit_cagr")
            o["peg"] = round(pe / g, 3) if pe and g and g > 0 else None
        except Exception:
            o["peg"] = None
        out_rows.append(o)

    result = {
        "generated": d.get("generated"),
        "source": "xueqiu_screener_slim",
        "n": len(out_rows),
        "fields": KEEP + ["code", "market", "peg"],
        "rows": out_rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out_rows)} rows, {len(KEEP) + 3} fields -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
