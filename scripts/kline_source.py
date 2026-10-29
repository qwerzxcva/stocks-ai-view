#!/usr/bin/env python3
"""kline_source.py — 日线数据访问层（所有引擎共用）

抽出来的原因：结构层、威科夫层、交易管理层、题材层都要读同一批日线。
各写一份的话，复权口径（qfq CSV vs raw DuckDB）迟早分叉，最后两个引擎对同一只股票
用两套价格，还都觉得自己对。

口径纪律：
1. 优先 qfq CSV（stocks-data-kline/data/day/*.csv）——形态/结构判断必须用复权价，
   除权缺口会被误判成跳空形态。
2. DuckDB 只有 raw，缺 CSV 的标的用它兜底，并在返回里带 `adj="raw"` 标记，让调用方知道。
3. DuckDB 连接必须单例：每只重开一次 266MB 文件，770 只就是 28 分钟跑不完。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or os.path.join(os.path.dirname(HERE), "data")
DUCKDB_PATH = os.environ.get("STOCKS_DB") or "/root/workspace/stocks-data-live/data/stocks.duckdb"
KLINE_DAY_DIR = os.environ.get("STOCKS_KLINE_DAY") or os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "day")

_CONN = {"c": None}


def duck():
    if _CONN["c"] is None:
        import duckdb
        _CONN["c"] = duckdb.connect(DUCKDB_PATH, read_only=True)
    return _CONN["c"]


def has_duck():
    return os.path.exists(DUCKDB_PATH)


def list_codes(limit=None, min_bars=120):
    rows = duck().execute(
        "select code, count(*) n from kline_daily where adj='raw' "
        "group by code having n >= ? order by code", [min_bars]
    ).fetchall()
    codes = [r[0] for r in rows]
    return codes[:limit] if limit else codes


def read_day_csv(code, limit=400):
    """读 qfq 日线 CSV。返回 (bars, True)；不存在或太短返回 (None, False)。"""
    p = os.path.join(KLINE_DAY_DIR, f"{code}.csv")
    if not os.path.exists(p):
        return None, False
    rows = []
    with open(p) as f:
        for line in f:
            if line.startswith("#") or line.startswith("date,"):
                continue
            parts = line.strip().split(",")
            if len(parts) < 6:
                continue
            try:
                rows.append({"date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
                             "high": float(parts[3]), "low": float(parts[4]),
                             "volume": float(parts[5])})
            except ValueError:
                continue
    if len(rows) < 60:
        return None, False
    return rows[-limit:], True


def load_bars(code, limit=400):
    """单只日线，升序。带 `adj` 字段说明用的是哪种口径。"""
    bars, is_qfq = read_day_csv(code, limit)
    if bars:
        for b in bars:
            b["adj"] = "qfq"
        return bars
    rows = duck().execute(
        "select date, open, close, high, low, volume from kline_daily "
        "where code=? and adj='raw' order by date desc limit ?", [code, limit]
    ).fetchall()
    rows.reverse()
    if len(rows) < 60:
        return []
    return [{"date": str(r[0]), "open": float(r[1]), "close": float(r[2]),
             "high": float(r[3]), "low": float(r[4]), "volume": float(r[5] or 0),
             "adj": "raw"} for r in rows]


def load_all(codes, limit=400):
    """批量取数：先读 CSV，剩下的用**一次**窗口查询。--all 能跑完的关键。"""
    out, need = {}, []
    for code in codes:
        bars, _ = read_day_csv(code, limit)
        if bars:
            for b in bars:
                b["adj"] = "qfq"
            out[code] = bars
        else:
            need.append(code)
    if need:
        q = ("select code, date, open, close, high, low, volume from ("
             "  select code, date, open, close, high, low, volume, "
             "         row_number() over (partition by code order by date desc) rn"
             "  from kline_daily where adj='raw'"
             ") t where rn <= ? order by code, date")
        for r in duck().execute(q, [limit]).fetchall():
            out.setdefault(r[0], []).append(
                {"date": str(r[1]), "open": float(r[2]), "close": float(r[3]),
                 "high": float(r[4]), "low": float(r[5]), "volume": float(r[6] or 0),
                 "adj": "raw"})
    return {k: v for k, v in out.items() if len(v) >= 60}


def last_date(codes=None):
    """最新数据日期。用来看数据是否过期（过期时下游应拒绝出信号）。"""
    q = "select max(date) from kline_daily where adj='raw'"
    return str(duck().execute(q).fetchone()[0])
