#!/usr/bin/env python3
"""
db_layer.py — DuckDB 数据库层 (索引 + 分区 + 查询优化 + 并发压测)

为什么用 DuckDB:
  - 零依赖单文件, 适合容器/CI
  - 列式存储 + 向量化执行, 聚合查询比 SQLite 快一个量级
  - 支持分区 (PARTITION_BY) 与索引 (CREATE INDEX)
  - 可直接读 Parquet/CSV, 也能被 Python/pandas/BI 工具共用

Schema 设计 (按查询模式反推):
  quotes         日行情快照     隐式分区: trade_date (DuckDB zone maps, 非显式 PARTITION BY)   索引: (code, trade_date)
  kline_daily    日K (qfq)      隐式分区: year           索引: (code, date)
  events         龙虎榜/大宗     隐式分区: trade_date     索引: (code, trade_date), (type), (trade_date, deal_amt)
  margin         两融余额       索引: date (UNIQUE)
  pools          池子/板块评分   隐式分区: date           索引: (kind, date)
  sources_log    采集溯源日志    索引: (ts, source)
  -- v2 扩展 (2026-10-13): 公告/新闻/基金净值/财务摘要
  announcements  公告 (按 ann_date 分区)
  news_items     新闻快讯
  fund_nav       基金/ETF 净值
  financial_summary  财务摘要

用法:
  python3 db_layer.py --init                  # 建库建表
  python3 db_layer.py --load quotes.json      # 从采集结果导入
  python3 db_layer.py --bench                 # 查询压测
  python3 db_layer.py --query \"SELECT ...\"    # 任意查询
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

try:
    import duckdb
except ImportError:
    print("需要 duckdb: pip install duckdb", file=sys.stderr)
    raise SystemExit(1)

DB_PATH = os.environ.get("STOCKS_DB") or None   # None → paths.DUCKDB_PATH

SCHEMA = """
-- 日行情快照 (腾讯/东财合并后)
CREATE TABLE IF NOT EXISTS quotes (
    code         VARCHAR NOT NULL,
    name         VARCHAR,
    price        DOUBLE,
    prev_close   DOUBLE,
    open         DOUBLE,
    high         DOUBLE,
    low          DOUBLE,
    chg_pct      DOUBLE,
    turnover     DOUBLE,          -- 换手率 %
    pe           DOUBLE,
    pb           DOUBLE,
    mktcap       DOUBLE,          -- 元
    floatcap     DOUBLE,
    trade_date   VARCHAR NOT NULL, -- YYYY-MM-DD
    sources      VARCHAR[],        -- 溯源: 哪些源提供了这条
    ts           VARCHAR           -- 数据时间戳
);

-- 日K (前复权)
CREATE TABLE IF NOT EXISTS kline_daily (
    code    VARCHAR NOT NULL,
    date    VARCHAR NOT NULL,
    open    DOUBLE,
    close   DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    volume  DOUBLE,
    year    VARCHAR
);

-- 事件 (龙虎榜/大宗交易)
CREATE TABLE IF NOT EXISTS events (
    type        VARCHAR,     -- lhb / blocktrade
    code        VARCHAR,
    name        VARCHAR,
    close       DOUBLE,
    chg         DOUBLE,
    deal_amt    DOUBLE,
    premium     DOUBLE,
    vol         DOUBLE,
    expl        VARCHAR,
    trade_date  VARCHAR,
    sources     VARCHAR[]
);

-- 两融余额
CREATE TABLE IF NOT EXISTS margin (
    date     VARCHAR PRIMARY KEY,
    rzye     DOUBLE,
    rqye     DOUBLE,
    rzrqye   DOUBLE,
    rzmre    DOUBLE
);

-- 池子/板块评分
CREATE TABLE IF NOT EXISTS pools (
    kind    VARCHAR,   -- cycle/sector/lowpool/multi/hotspot
    date    VARCHAR,
    name    VARCHAR,   -- 板块名/股票名/指数名
    code    VARCHAR,
    score   DOUBLE,
    detail  JSON
);

-- 采集溯源日志
CREATE TABLE IF NOT EXISTS sources_log (
    ts       VARCHAR,
    source   VARCHAR,
    n_raw    INTEGER,
    n_merged INTEGER,
    elapsed  DOUBLE,
    status   VARCHAR
);

-- ── v2 扩展表 (2026-10-13) ─────────────────────────────────────────

-- 公告 (东财 np-ann / 巨潮 / 交易所)
CREATE TABLE IF NOT EXISTS announcements (
    code        VARCHAR,
    name        VARCHAR,
    title       VARCHAR,
    ann_date    VARCHAR,   -- YYYY-MM-DD
    ann_type    VARCHAR,   -- 业绩预告/定期报告/临时公告
    url         VARCHAR,
    source      VARCHAR,   -- 采集源标识
    ts          VARCHAR
);

-- 新闻快讯 (7x24 多源合并)
CREATE TABLE IF NOT EXISTS news_items (
    title       VARCHAR,
    content     TEXT,
    source      VARCHAR,   -- sina/em/douyin/remote_news
    pub_time    VARCHAR,   -- ISO 8601
    url         VARCHAR,
    ts          VARCHAR
);

-- 基金/ETF 净值
CREATE TABLE IF NOT EXISTS fund_nav (
    fund_code   VARCHAR,
    fund_name   VARCHAR,
    date        VARCHAR,
    nav         DOUBLE,
    cum_nav     DOUBLE,
    change_pct  DOUBLE,
    scale_yi    DOUBLE,   -- 规模(亿)
    ts          VARCHAR
);

-- 基金重仓股 (pingzhongdata 解析结果)
CREATE TABLE IF NOT EXISTS fund_rebalance (
    fund_code    VARCHAR,
    report_date  VARCHAR,   -- 季度末日期
    stock_code   VARCHAR,
    stock_name   VARCHAR,
    ratio        DOUBLE,   -- 持股比例%
    ts           VARCHAR
);

-- 财务摘要 (同花顺 stock_financial_abstract_new_ths)
CREATE TABLE IF NOT EXISTS financial_summary (
    code          VARCHAR,
    report_date   VARCHAR,   -- 报告期 YYYY-MM-DD
    report_name   VARCHAR,   -- 如 "2026一季报"
    metric_name   VARCHAR,   -- 如 "index_weighted_avg_roe"
    value         VARCHAR,
    yoy           VARCHAR,
    mom           VARCHAR,
    ts            VARCHAR
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_quotes_code_date ON quotes(code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_chg ON quotes(trade_date, chg_pct)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_pe_pb ON quotes(pe, pb)",
    "CREATE INDEX IF NOT EXISTS idx_kline_code_date ON kline_daily(code, date)",
    "CREATE INDEX IF NOT EXISTS idx_kline_year ON kline_daily(year)",
    "CREATE INDEX IF NOT EXISTS idx_events_code_date ON events(code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_date ON events(type, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_events_deal ON events(type, trade_date, deal_amt)",
    "CREATE INDEX IF NOT EXISTS idx_pools_kind_date ON pools(kind, date)",
    "CREATE INDEX IF NOT EXISTS idx_sources_log_ts ON sources_log(ts)",
    # ── v2 新增索引 (2026-10-13) ─────────────────────────────────────
    # 覆盖索引: 补齐缺失的 ORDER BY 扫描列
    "CREATE INDEX IF NOT EXISTS idx_quotes_chg_cover ON quotes(trade_date, chg_pct, code, name, price)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_pe_pb_cover ON quotes(trade_date, pe, pb, code, name)",
    "CREATE INDEX IF NOT EXISTS idx_events_deal_cover ON events(trade_date, deal_amt, code, name, type)",
    # 新表索引
    "CREATE INDEX IF NOT EXISTS idx_ann_date ON announcements(ann_date)",
    "CREATE INDEX IF NOT EXISTS idx_ann_code ON announcements(code, ann_date)",
    "CREATE INDEX IF NOT EXISTS idx_news_ts ON news_items(ts)",
    "CREATE INDEX IF NOT EXISTS idx_news_source ON news_items(source, ts)",
    "CREATE INDEX IF NOT EXISTS idx_fund_nav_code ON fund_nav(fund_code, date)",
    "CREATE INDEX IF NOT EXISTS idx_fund_rebal_code ON fund_rebalance(fund_code, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_fin_code_period ON financial_summary(code, report_date)",
]


def connect(path=None, read_only=False):
    path = path or DB_PATH
    if path is None:
        from paths import DUCKDB_PATH
        path = DUCKDB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return duckdb.connect(path, read_only=read_only)


def init_db(path=None):
    con = connect(path)
    con.execute(SCHEMA)
    for idx in INDEXES:
        con.execute(idx)
    n = con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
    print(f"[init] {path or DB_PATH}  表数={n}  索引={len(INDEXES)}")
    con.close()
    return True


def load_quotes(con, records, trade_date):
    """导入行情快照 (批量 executemany 比逐条快 10x+)。"""
    rows = []
    for r in records:
        if "code" not in r:      # 跳过涨跌停池等非股票记录
            continue
        rows.append((
            r.get("code"), r.get("name"), r.get("price"), r.get("prev_close"),
            r.get("open"), r.get("high"), r.get("low"), r.get("chg_pct"),
            r.get("turnover"), r.get("pe"), r.get("pb"),
            r.get("mktcap"), r.get("floatcap"),
            trade_date, r.get("sources") or [], r.get("time") or "",
        ))
    if not rows:
        return 0
    con.executemany(
        "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_events(con, records):
    rows = []
    for r in records:
        if r.get("type") not in ("lhb", "blocktrade"):
            continue
        rows.append((
            r.get("type"), r.get("code"), r.get("name"), r.get("close"),
            r.get("chg"), r.get("deal_amt"), r.get("premium"), r.get("vol"),
            r.get("expl"), r.get("trade_date"), r.get("sources") or [],
        ))
    if not rows:
        return 0
    con.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_margin(con, records):
    n = 0
    for r in records:
        if not r.get("date"):
            continue
        try:
            con.execute(
                "INSERT OR REPLACE INTO margin VALUES (?,?,?,?,?)",
                [r["date"], r.get("rzye"), r.get("rqye"),
                 r.get("rzrqye"), r.get("rzmre")])
            n += 1
        except Exception:
            pass
    return n


# ─── v2 新增加载函数 (2026-10-13) ───────────────────────────────────────────
def load_announcements(con, records):
    """records: list of dict with code/name/title/ann_date/ann_type/url/source."""
    ts = datetime.now(CST).isoformat(timespec="seconds")
    rows = [(r.get("code"), r.get("name"), r.get("title"),
             r.get("ann_date"), r.get("ann_type"), r.get("url"),
             r.get("source", ""), ts) for r in records]
    if rows:
        con.executemany(
            "INSERT INTO announcements VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_news_items(con, records):
    """records: list of dict with title/content/source/pub_time/url."""
    ts = datetime.now(CST).isoformat(timespec="seconds")
    rows = [(r.get("title"), r.get("content"), r.get("source", ""),
             r.get("pub_time"), r.get("url"), ts) for r in records]
    if rows:
        con.executemany(
            "INSERT INTO news_items VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def load_fund_nav(con, records):
    """records: list of dict with fund_code/fund_name/date/nav/cum_nav/change_pct/scale_yi."""
    ts = datetime.now(CST).isoformat(timespec="seconds")
    rows = [(r.get("fund_code"), r.get("fund_name"), r.get("date"),
             r.get("nav"), r.get("cum_nav"), r.get("change_pct"),
             r.get("scale_yi"), ts) for r in records]
    if rows:
        con.executemany(
            "INSERT INTO fund_nav VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_fund_rebalance(con, records):
    """records: list of dict with fund_code/report_date/stock_code/stock_name/ratio."""
    ts = datetime.now(CST).isoformat(timespec="seconds")
    rows = [(r.get("fund_code"), r.get("report_date"),
             r.get("stock_code"), r.get("stock_name"), r.get("ratio"), ts)
            for r in records]
    if rows:
        con.executemany(
            "INSERT INTO fund_rebalance VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def load_financial_summary(con, records):
    """records: list of dict with code/report_date/report_name/metric_name/value/yoy/mom."""
    ts = datetime.now(CST).isoformat(timespec="seconds")
    rows = [(r.get("code"), r.get("report_date"), r.get("report_name"),
             r.get("metric_name"), r.get("value"), r.get("yoy"), r.get("mom"), ts)
            for r in records]
    if rows:
        con.executemany(
            "INSERT INTO financial_summary VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_from_collect(json_path, path=None):
    """从 collect_all_sources.py 的输出导入。"""
    d = json.load(open(json_path, encoding="utf-8"))
    trade_date = d["summary"]["date"]
    td = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    con = connect(path)
    init_db(path)
    nq = load_quotes(con, d["data"].get("quote", []), td)
    ne = load_events(con, d["data"].get("event", []))
    nm = load_margin(con, d["data"].get("event", []))
    # 溯源日志
    con.execute("INSERT INTO sources_log VALUES (?,?,?,?,?,?)",
                [d["summary"]["ts"], "collect_all", d["summary"]["total_raw"],
                 d["summary"]["total_merged"], d["summary"]["elapsed_s"], "ok"])
    con.close()
    print(f"[load] quotes={nq} events={ne} margin={nm} (date={td})")
    return nq + ne + nm


# ─── 查询优化示例 ─────────────────────────────────────────────────
BENCH_QUERIES = [
    ("全市场快照计数", "SELECT count(*) FROM quotes WHERE trade_date=(SELECT max(trade_date) FROM quotes)"),
    ("单股最新行情", "SELECT * FROM quotes WHERE code='600519' ORDER BY trade_date DESC LIMIT 1"),
    ("涨幅前20", "SELECT code,name,chg_pct FROM quotes WHERE trade_date=(SELECT max(trade_date) FROM quotes) ORDER BY chg_pct DESC LIMIT 20"),
    ("低估值筛选", "SELECT code,name,pe,pb FROM quotes WHERE trade_date=(SELECT max(trade_date) FROM quotes) AND pe>0 AND pe<15 AND pb<1.5 ORDER BY pe LIMIT 20"),
    ("龙虎榜按成交额", "SELECT code,name,deal_amt FROM events WHERE type='lhb' AND trade_date=(SELECT max(trade_date) FROM events WHERE type='lhb') ORDER BY deal_amt DESC LIMIT 10"),
    ("两融趋势", "SELECT date,rzye,rzrqye FROM margin ORDER BY date DESC LIMIT 10"),
    ("分板块聚合", "SELECT type, count(*), sum(deal_amt) FROM events GROUP BY type"),
]


def bench(path=None, repeats=3):
    con = connect(path, read_only=True)
    print(f"{'query':22s} {'avg(ms)':>10s} {'rows':>6s}")
    print("-" * 46)
    for label, sql in BENCH_QUERIES:
        times = []
        rows = 0
        for _ in range(repeats):
            t0 = time.time()
            try:
                res = con.execute(sql).fetchall()
                rows = len(res)
            except Exception:
                rows = -1
            times.append((time.time() - t0) * 1000)
        avg = sum(times) / len(times)
        print(f"{label:22s} {avg:>10.2f} {rows:>6d}")
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--load", help="collect_all_sources.py 的输出 JSON")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--query", help="执行任意 SQL")
    ap.add_argument("--path", help="DB 路径 (默认 STOCKS_DB 或 stocks-data-live/stocks.duckdb)")
    args = ap.parse_args()

    p = args.path or DB_PATH
    if p is None:
        from paths import DUCKDB_PATH
        p = DUCKDB_PATH
    if args.init:
        init_db(p)
        return 0
    if args.load:
        if not os.path.exists(p):
            init_db(p)
        load_from_collect(args.load, p)
        return 0
    if args.bench:
        bench(p)
        return 0
    if args.query:
        con = connect(p, read_only=True)
        res = con.execute(args.query).fetchall()
        cols = [d[0] for d in con.description]
        print("\t".join(cols))
        for r in res[:100]:
            print("\t".join(str(x) for x in r))
        print(f"({len(res)} rows)")
        con.close()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
