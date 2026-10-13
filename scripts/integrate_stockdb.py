#!/usr/bin/env python3
"""
integrate_stockdb.py — 接入私有仓 stockdb 的全量 K线 (207万行 duckdb)

数据源 (私有 GitHub 仓, 需 GH_TOKEN):
  jiegengxiangjianxue1/stockdb → stock.duckdb (70MB)
    daily    表: 2078356 行, 3175 只股票, 2024-01-02 ~ 2026-10-09
    minute   表: 126607 行 (分钟线)
    analysis 表: 31 行 (分析落库)
  picks.duckdb → pick_log 表: 对账闭环 (ai_pick/tailpick/chase/dip/bcard)

解决的本项目问题:
  - 本地 bulk K线只 2764 只 (51% 覆盖), 且只到 2023-06 起
  - 本脚本导出成 bulk 格式 (day_qfq_XX.csv 同格式), 所有引擎自动可用

用法:
  GH_TOKEN=<pat> python3 integrate_stockdb.py --check      # 只查数据库状态
  GH_TOKEN=<pat> python3 integrate_stockdb.py --export     # 导出到 bulk (增量)
  GH_TOKEN=<pat> python3 integrate_stockdb.py --picks      # 导对账数据到 data/

环境变量:
  GH_TOKEN   GitHub PAT (需私有仓读权限)
  STOCKS_DB  本地 stock.duckdb 路径 (已下载过则免下载)
"""
import argparse
import glob
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA
except Exception:
    BULK = "/root/workspace/stocks-data-kline/data/bulk"
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

# GitHub PAT 从环境变量读 (不要硬编码, 会被 GitHub secret scanning 拦截)
# 用法: GH_TOKEN=<pat> python3 integrate_stockdb.py --check
GH_TOKEN = os.environ.get("GH_TOKEN", "")
STOCKDB_REPO = "https://dufuwuxin:{token}@github.com/jiegengxiangjianxue1/stockdb.git"
DB_NAME = "stock.duckdb"
PICKS_NAME = "picks.duckdb"
LOCAL_DB = os.environ.get("STOCKS_DB") or os.path.join("/tmp", "stockdb_dl", DB_NAME)


def ensure_db():
    """确保本地有 stock.duckdb (稀疏 clone 下载)。返回路径。"""
    if os.path.exists(LOCAL_DB) and os.path.getsize(LOCAL_DB) > 10_000_000:
        return LOCAL_DB
    work = "/tmp/stockdb_dl"
    if os.path.exists(work):
        shutil.rmtree(work)
    url = STOCKDB_REPO.format(token=GH_TOKEN)
    print(f"[stockdb] 稀疏克隆 {url.split('@')[1]} ...", flush=True)
    r = subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", url, work],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"clone 失败: {r.stderr[:200]}")
    subprocess.run(["git", "sparse-checkout", "set", DB_NAME, PICKS_NAME, "ARCHITECTURE.md"],
                   cwd=work, capture_output=True, text=True, timeout=300)
    subprocess.run(["git", "checkout"], cwd=work, capture_output=True, text=True, timeout=600)
    if not os.path.exists(LOCAL_DB):
        raise RuntimeError(f"checkout 后仍未找到 {LOCAL_DB}")
    return LOCAL_DB


def cmd_check():
    import duckdb
    db = ensure_db()
    con = duckdb.connect(db, read_only=True)
    print(f"[stockdb] {db} ({os.path.getsize(db)/1e6:.1f}MB)")
    for t in ["daily", "minute", "analysis"]:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            nsym = con.execute(f"SELECT count(DISTINCT symbol) FROM {t}").fetchone()[0]
            print(f"  {t}: {n} 行, {nsym} 个 symbol")
        except Exception as e:
            print(f"  {t}: {e}")
    rng = con.execute("SELECT min(date), max(date) FROM daily").fetchone()
    print(f"  daily 日期范围: {rng[0]} ~ {rng[1]}")
    # 与本地 bulk 对比
    have = set()
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                have.add(line.split("|", 1)[0])
    db_syms = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily").fetchall()}
    print(f"\n[对比] 本地 bulk {len(have)} 只 | stockdb {len(db_syms)} 只")
    print(f"  stockdb 独有 (本地缺): {len(db_syms - have)} 只")
    print(f"  本地独有 (stockdb 缺): {len(have - db_syms)} 只")
    # 抽一只有数据的看格式
    row = con.execute("SELECT * FROM daily WHERE symbol='sh600519' ORDER BY date DESC LIMIT 2").fetchall()
    print(f"  样例 sh600519: {row}")
    con.close()
    return 0


def cmd_export():
    import duckdb
    db = ensure_db()
    con = duckdb.connect(db, read_only=True)
    # 已有 bulk 的 code 集合 (增量跳过)
    have = set()
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                have.add(line.split("|", 1)[0])
    print(f"[stockdb] 本地 bulk 已有 {len(have)} 只, 导出 stockdb 独有部分", flush=True)

    rows = con.execute("""
        SELECT symbol, date, open, close, high, low, volume
        FROM daily ORDER BY symbol, date
    """).fetchall()
    con.close()
    print(f"[stockdb] 读取 {len(rows)} 行, 开始写 bulk ...", flush=True)

    out_path = os.path.join(BULK, "day_qfq_stockdb.csv")
    n_new = n_row = 0
    cur_sym = None
    fh = open(out_path, "w", encoding="utf-8")
    fh.write(f"# stockdb integrate {datetime.now(CST):%Y%m%d} (jiegengxiangjianxue1/stockdb)\n")
    for sym, d, o, c, h, l, v in rows:
        if sym != cur_sym:
            cur_sym = sym
            if sym in have:
                cur_sym = "__skip__"
                continue
            n_new += 1
        if cur_sym == "__skip__":
            continue
        fh.write(f"{sym}|{d}|{o}|{c}|{h}|{l}|{v}\n")
        n_row += 1
        if n_row % 200000 == 0:
            fh.flush()
            print(f"  ... {n_row} 行 (新增 {n_new} 只)", flush=True)
    fh.close()
    print(f"[stockdb] ✅ 导出 {n_new} 只新股 / {n_row} 行 → {out_path}")
    print("[stockdb] 所有引擎 (engine_v5/multidim/kline_sim/backfill) 会自动读取 day_qfq_*.csv")
    return 0


def cmd_picks():
    """导对账闭环数据到 data/ (供 self_reflect/winrate_matrix 用)。"""
    import duckdb
    db = ensure_db()
    pk = os.path.join(os.path.dirname(db), PICKS_NAME)
    if not os.path.exists(pk):
        print(f"[stockdb] picks.duckdb 不存在: {pk}")
        return 1
    con = duckdb.connect(pk, read_only=True)
    rows = con.execute("""
        SELECT pick_date, symbol, name, entry_price, kind, rule, next_date,
               ret_open_close, ret_open_high, ret_t2, verdict, settled
        FROM pick_log ORDER BY pick_date DESC LIMIT 200
    """).fetchall()
    con.close()
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "jiegengxiangjianxue1/stockdb picks.duckdb (pick_log)",
        "n": len(rows),
        "by_kind": {},
        "items": [{
            "pick_date": r[0], "symbol": r[1], "name": r[2], "entry": r[3],
            "kind": r[4], "rule": r[5], "next_date": r[6],
            "ret_open_close": r[7], "ret_open_high": r[8], "ret_t2": r[9],
            "verdict": r[10], "settled": r[11],
        } for r in rows],
    }
    for it in out["items"]:
        out["by_kind"][it["kind"]] = out["by_kind"].get(it["kind"], 0) + 1
    p = os.path.join(AI_VIEW_DATA, "pick_ledger.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[stockdb] ✅ 对账数据 {len(rows)} 条 ({out['by_kind']}) → {p}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查数据库状态与覆盖对比")
    ap.add_argument("--export", action="store_true", help="导出全量K线到 bulk (增量)")
    ap.add_argument("--picks", action="store_true", help="导对账闭环数据到 data/")
    args = ap.parse_args()
    try:
        if args.picks:
            return cmd_picks()
        if args.export:
            return cmd_export()
        return cmd_check()
    except Exception as e:
        print(f"[stockdb] 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
