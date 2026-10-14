#!/usr/bin/env python3
"""ledger_store.py — read the pick ledger from the cloud, not from a local DB.

pick_tracker / loss_attribution / engine_fusion / strategy_card all read
~/stockdb/picks.duckdb, which does not exist on the Actions runner and is often
empty locally. Every one of them bailed out with "picks.duckdb not found", so
they were dead code on every automated run.

The ledger now lives as parquet in the public market warehouse:

    jiegengxiangjianxue1/db-market/ledger/pick_log.parquet
    jiegengxiangjianxue1/db-market/ledger/daily/<year>.parquet

This module fetches it into a local DuckDB so the existing SQL in those engines
keeps working unchanged. It caches the download for the process and falls back
to a local picks.duckdb when the cloud copy is unavailable.

    import ledger_store
    con = ledger_store.connect()      # duckdb connection with pick_log
    con = ledger_store.connect(kind="daily")   # daily kline table
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".cache", "ledger_store")
MARKET_REPO = "jiegengxiangjianxue1/db-market"
RAW = f"https://raw.githubusercontent.com/{MARKET_REPO}/main"

# cloud parquet -> table name the engines expect
TABLES = {
    "pick_log": "ledger/pick_log.parquet",
}
DAILY_GLOB = "ledger/daily/*.parquet"


def _fetch(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ledger-store"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def _ensure(url, local_name):
    """Download once per cache lifetime; return the local path or None."""
    dest = os.path.join(CACHE, local_name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        return _fetch(url, dest)
    except Exception:
        return None


def _con():
    import duckdb
    return duckdb.connect(":memory:")


def connect(kind="pick_log"):
    """A DuckDB connection exposing the requested table.

    kind: pick_log | daily
    Returns None when neither the cloud copy nor a local DB is available.
    """
    try:
        import duckdb
    except ImportError:
        return None

    con = _con()

    if kind == "pick_log":
        local = _ensure(f"{RAW}/ledger/pick_log.parquet", "pick_log.parquet")
        if local:
            con.execute(
                f"CREATE VIEW pick_log AS "
                f"SELECT * FROM read_parquet('{local}')")
            return con
        # fall back to a local duckdb that actually has rows
        for cand in (os.environ.get("STOCKS_STOCKDB"),
                     os.path.join(HOME, "stockdb")):
            if not cand:
                continue
            p = os.path.join(cand, "picks.duckdb")
            if os.path.exists(p):
                try:
                    c2 = duckdb.connect(p, read_only=True)
                    n = c2.execute("SELECT count(*) FROM pick_log").fetchone()[0]
                    c2.close()
                    if n:
                        con.execute(f"ATTACH '{p}' AS local (READ_ONLY)")
                        con.execute(
                            "CREATE VIEW pick_log AS SELECT * FROM local.pick_log")
                        return con
                except Exception:
                    pass
        return None

    if kind == "daily":
        # the cloud keeps one parquet per year
        names = ["2026.parquet"]
        for n in names:
            local = _ensure(f"{RAW}/ledger/daily/{n}", f"daily_{n}")
            if local:
                con.execute(
                    f"CREATE VIEW daily AS SELECT * FROM read_parquet('{local}')")
                return con
        # local parquet tree
        for base in (os.environ.get("STOCKS_STOCKDB"),
                     os.path.join(HOME, "stockdb")):
            if not base:
                continue
            files = sorted(glob.glob(os.path.join(base, "wh", "daily", "*.parquet")))
            if files:
                lst = "','".join(files)
                con.execute(
                    f"CREATE VIEW daily AS SELECT * FROM read_parquet(['{lst}'])")
                return con
        return None

    return None


def has_rows(con, table="pick_log"):
    if con is None:
        return 0
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0
