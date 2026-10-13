#!/usr/bin/env python3
"""
query_api.py — 统一数据查询接口 (PoC)

每个数据域一个子命令，输出 JSON（默认）或 CSV。
只读操作，不写数据库。

用法 (所有命令均假设已运行过 db_layer.py --init 或 --load):
  # 数据库路径: 环境变量 STOCKS_DB 或默认 stocks-data-live/stocks.duckdb

  # 实时行情域
  python3 query_api.py quotes --top 20 --by chg_pct
  python3 query_api.py quotes --code 600519
  python3 query_api.py quotes --low-value          # 低估值筛选 pe<15 pb<1.5
  python3 query_api.py quotes --csv

  # K线域
  python3 query_api.py kline --code 600519 --days 20
  python3 query_api.py kline --index sh000001      # 上证全史
  python3 query_api.py kline --csv

  # 事件域 (龙虎榜 / 大宗交易)
  python3 query_api.py events --type lhb --limit 10
  python3 query_api.py events --type blocktrade --limit 10
  python3 query_api.py events --top-by deal_amt --limit 20
  python3 query_api.py events --csv

  # 两融域
  python3 query_api.py margin --limit 30

  # 公告域
  python3 query_api.py announcements --code 600519 --limit 10
  python3 query_api.py announcements --date 2026-10-13

  # 新闻域
  python3 query_api.py news --source sina --limit 20
  python3 query_api.py news --keyword 降准

  # 基金域
  python3 query_api.py fund-nav --code 510500 --days 30
  python3 query_api.py fund-rebalance --code 510500 --quarter 2026Q1

  # 财务域
  python3 query_api.py financial --code 600519 --quarter 2026Q1

  # 全库统计
  python3 query_api.py stats

所有输出 JSON 均含 "query" (查询描述) 和 "n_rows" 字段，timestamp 自动填充。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


def _db():
    try:
        import duckdb
    except ImportError:
        print("需要 duckdb: pip install duckdb", file=sys.stderr)
        sys.exit(1)
    path = os.environ.get("STOCKS_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "stocks-data-live", "stocks.duckdb"
    )
    # 尝试多个已知位置
    if not os.path.exists(path):
        for alt in [
            "/root/workspace/stocks-data-live/stocks.duckdb",
            os.path.expanduser("~/workspace/stocks-data-live/stocks.duckdb"),
        ]:
            if os.path.exists(alt):
                path = alt
                break
    if not os.path.exists(path):
        print(f"找不到 DuckDB 文件，已尝试: {path}", file=sys.stderr)
        print("请先运行: python3 db_layer.py --init", file=sys.stderr)
        sys.exit(1)
    return duckdb.connect(path, read_only=True)


def _ts():
    return datetime.now(CST).isoformat(timespec="seconds")


def _output(data, csv_mode=False, csv_path=None):
    if csv_mode and csv_path:
        rows = data.get("rows", [])
        if not rows:
            print("无数据")
            return
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(list(r.values()))
        print(f"[csv] → {csv_path} ({len(rows)} rows)")
    else:
        out = dict(data)
        out["timestamp"] = _ts()
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def _rows(con, sql, params=None):
    res = con.execute(sql, params) if params else con.execute(sql)
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in res.fetchall()]


# ─── 行情域 ───────────────────────────────────────────────────────
def cmd_quotes(args):
    con = _db()
    base = "SELECT code, name, price, chg_pct, turnover, pe, pb, mktcap FROM quotes WHERE trade_date=(SELECT max(trade_date) FROM quotes)"
    sql = base
    conditions = []

    if args.code:
        conditions.append(f"code='{args.code}'")
    if args.top:
        if args.by == "pe":
            sql += f" AND pe>0 ORDER BY pe LIMIT {args.top}"
        elif args.by == "mktcap":
            sql += f" ORDER BY mktcap DESC LIMIT {args.top}"
        else:
            sql += f" ORDER BY chg_pct DESC LIMIT {args.top}"
    elif args.low_value:
        sql += " AND pe>0 AND pe<15 AND pb<1.5 ORDER BY pe LIMIT 50"
    else:
        sql += " ORDER BY chg_pct DESC LIMIT 100"

    if args.limit and not args.top and not args.low_value:
        sql += f" LIMIT {args.limit}"

    rows = _rows(con, sql)
    out = {"query": f"quotes: top={args.top} by={args.by} code={args.code}",
           "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── K线域 ───────────────────────────────────────────────────────
def cmd_kline(args):
    con = _db()
    code = args.code or "600519"
    days = args.days or 20
    sql = f"""
        SELECT date, open, close, high, low, volume
        FROM kline_daily
        WHERE code='{code}'
        ORDER BY date DESC
        LIMIT {days}
    """
    rows = _rows(con, sql)
    out = {"query": f"kline: code={code} days={days}", "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 事件域 (龙虎榜/大宗交易) ────────────────────────────────────
def cmd_events(args):
    con = _db()
    sql = "SELECT type, code, name, close, chg, deal_amt, premium, vol, expl, trade_date FROM events"
    conditions = []
    if args.type:
        conditions.append(f"type='{args.type}'")
    if args.date:
        conditions.append(f"trade_date='{args.date}'")
    if args.code:
        conditions.append(f"code='{args.code}'")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if args.top_by == "deal_amt":
        sql += " ORDER BY deal_amt DESC"
    else:
        sql += " ORDER BY trade_date DESC, deal_amt DESC"
    limit = args.limit or 20
    sql += f" LIMIT {limit}"
    rows = _rows(con, sql)
    out = {"query": f"events: type={args.type} date={args.date} code={args.code}",
           "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 两融域 ──────────────────────────────────────────────────────
def cmd_margin(args):
    con = _db()
    limit = args.limit or 30
    sql = f"SELECT date, rzye, rqye, rzrqye, rzmre FROM margin ORDER BY date DESC LIMIT {limit}"
    rows = _rows(con, sql)
    out = {"query": f"margin: limit={limit}", "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 公告域 ──────────────────────────────────────────────────────
def cmd_announcements(args):
    con = _db()
    sql = "SELECT code, name, title, ann_date, ann_type, url, source FROM announcements"
    conditions = []
    if args.code:
        conditions.append(f"code='{args.code}'")
    if args.date:
        conditions.append(f"ann_date='{args.date}'")
    if args.type:
        conditions.append(f"ann_type='{args.type}'")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY ann_date DESC, code LIMIT " + str(args.limit or 20)
    rows = _rows(con, sql)
    out = {"query": f"announcements: code={args.code} date={args.date}",
           "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 新闻域 ──────────────────────────────────────────────────────
def cmd_news(args):
    con = _db()
    sql = "SELECT title, source, pub_time, url, ts FROM news_items"
    conditions = []
    if args.source:
        conditions.append(f"source='{args.source}'")
    if args.keyword:
        conditions.append(f"title LIKE '%{args.keyword}%'")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY pub_time DESC LIMIT " + str(args.limit or 20)
    rows = _rows(con, sql)
    out = {"query": f"news: source={args.source} keyword={args.keyword}",
           "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 基金域 ─────────────────────────────────────────────────────
def cmd_fund_nav(args):
    con = _db()
    code = args.code or "510500"
    days = args.days or 30
    sql = f"""
        SELECT fund_code, fund_name, date, nav, cum_nav, change_pct, scale_yi
        FROM fund_nav WHERE fund_code='{code}'
        ORDER BY date DESC LIMIT {days}
    """
    rows = _rows(con, sql)
    out = {"query": f"fund_nav: code={code} days={days}", "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


def cmd_fund_rebalance(args):
    con = _db()
    code = args.code or "510500"
    sql = f"""
        SELECT fund_code, report_date, stock_code, stock_name, ratio
        FROM fund_rebalance WHERE fund_code='{code}'
        ORDER BY report_date DESC, ratio DESC LIMIT {args.limit or 10}
    """
    rows = _rows(con, sql)
    out = {"query": f"fund_rebalance: code={code}", "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 财务域 ─────────────────────────────────────────────────────
def cmd_financial(args):
    con = _db()
    code = args.code or "600519"
    sql = f"""
        SELECT code, report_date, report_name, metric_name, value, yoy, mom
        FROM financial_summary WHERE code='{code}'
        ORDER BY report_date DESC, metric_name LIMIT {args.limit or 20}
    """
    rows = _rows(con, sql)
    out = {"query": f"financial: code={code}", "n_rows": len(rows), "rows": rows}
    _output(out, args.csv, args.out)
    con.close()


# ─── 全库统计 ───────────────────────────────────────────────────
def cmd_stats(_args):
    con = _db()
    tables = ["quotes", "kline_daily", "events", "margin", "pools",
              "sources_log", "announcements", "news_items", "fund_nav",
              "fund_rebalance", "financial_summary"]
    stats = {}
    for t in tables:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            stats[t] = n
        except Exception:
            stats[t] = -1
    out = {"query": "stats", "tables": stats, "timestamp": _ts()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    con.close()


# ─── CLI 入口 ────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="统一数据查询接口 (DuckDB read-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd")

    # quotes
    q = sub.add_parser("quotes", help="实时行情")
    q.add_argument("--code", help="股票代码，如 600519")
    q.add_argument("--top", type=int, help="取前 N 条")
    q.add_argument("--by", default="chg_pct", help="排序字段: chg_pct/pe/mktcap")
    q.add_argument("--low-value", action="store_true", help="低估值筛选 pe<15 pb<1.5")
    q.add_argument("--limit", type=int, help="行数限制")
    q.add_argument("--csv", action="store_true", help="CSV 输出")
    q.add_argument("--out", help="CSV 输出路径")
    q.set_defaults(func=cmd_quotes)

    # kline
    k = sub.add_parser("kline", help="K线")
    k.add_argument("--code", help="股票代码")
    k.add_argument("--days", type=int, help="天数")
    k.add_argument("--csv", action="store_true")
    k.add_argument("--out", help="CSV 输出路径")
    k.set_defaults(func=cmd_kline)

    # events
    e = sub.add_parser("events", help="事件 (龙虎榜/大宗交易)")
    e.add_argument("--type", help="lhb/blocktrade")
    e.add_argument("--date", help="YYYY-MM-DD")
    e.add_argument("--code", help="股票代码")
    e.add_argument("--top-by", help="deal_amt")
    e.add_argument("--limit", type=int, default=20)
    e.add_argument("--csv", action="store_true")
    e.add_argument("--out", help="CSV 输出路径")
    e.set_defaults(func=cmd_events)

    # margin
    m = sub.add_parser("margin", help="两融余额")
    m.add_argument("--limit", type=int)
    m.add_argument("--csv", action="store_true")
    m.add_argument("--out", help="CSV 输出路径")
    m.set_defaults(func=cmd_margin)

    # announcements
    a = sub.add_parser("announcements", help="公告")
    a.add_argument("--code", help="股票代码")
    a.add_argument("--date", help="YYYY-MM-DD")
    a.add_argument("--type", help="公告类型")
    a.add_argument("--limit", type=int)
    a.add_argument("--csv", action="store_true")
    a.add_argument("--out", help="CSV 输出路径")
    a.set_defaults(func=cmd_announcements)

    # news
    n = sub.add_parser("news", help="新闻")
    n.add_argument("--source", help="来源 (sina/em/douyin/remote_news)")
    n.add_argument("--keyword", help="关键词 (LIKE 模糊匹配)")
    n.add_argument("--limit", type=int)
    n.add_argument("--csv", action="store_true")
    n.add_argument("--out", help="CSV 输出路径")
    n.set_defaults(func=cmd_news)

    # fund-nav
    fn = sub.add_parser("fund-nav", help="基金净值")
    fn.add_argument("--code", help="基金代码")
    fn.add_argument("--days", type=int)
    fn.add_argument("--csv", action="store_true")
    fn.add_argument("--out", help="CSV 输出路径")
    fn.set_defaults(func=cmd_fund_nav)

    # fund-rebalance
    fr = sub.add_parser("fund-rebalance", help="基金重仓股")
    fr.add_argument("--code", help="基金代码")
    fr.add_argument("--limit", type=int)
    fr.add_argument("--csv", action="store_true")
    fr.add_argument("--out", help="CSV 输出路径")
    fr.set_defaults(func=cmd_fund_rebalance)

    # financial
    fi = sub.add_parser("financial", help="财务摘要")
    fi.add_argument("--code", help="股票代码")
    fi.add_argument("--limit", type=int)
    fi.add_argument("--csv", action="store_true")
    fi.add_argument("--out", help="CSV 输出路径")
    fi.set_defaults(func=cmd_financial)

    # stats
    st = sub.add_parser("stats", help="全库统计")
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
