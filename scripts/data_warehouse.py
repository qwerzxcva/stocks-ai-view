#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/data_warehouse.py ═══
# 上游 commit: b261f1e84a70 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""data_warehouse.py — 本地数据仓库 v2: parquet分区 + 索引表
目录: ~/stockdb/wh/{daily,moneyflow_hist,minute1}/{YYYY}.parquet (zstd)
索引: ~/stockdb/wh/_index.json {dataset: {partition: {rows, size, min_sym, max_sym, updated}}}
查询: load(dataset, year=None, symbols=None) -> DataFrame
"""
import json, os, glob, duckdb, datetime, pathlib

HOME = os.path.expanduser("~")
WH = f"{HOME}/stockdb/wh"
IDX = f"{WH}/_index.json"

DATASETS = {
    "daily":          ["symbol", "date", "open", "close", "high", "low", "volume"],
    "moneyflow_hist": ["symbol", "date", "trade", "changeratio", "turnover", "netamount",
                       "r0", "r1", "r2", "r3", "r0_net", "r1_net", "r2_net", "r3_net"],
    "minute1":        ["symbol", "date", "hhmm", "open", "close", "high", "low", "volume", "amount"],
    "minute5":        ["symbol", "date", "hhmm", "open", "close", "high", "low", "volume"],
}

def _part_path(ds, year):
    return f"{WH}/{ds}/{year}.parquet"

def write_partition(ds, year, rows, merge_on=None):
    """rows: list of tuple(按DATASETS列序).
    merge_on=None: 覆盖写; 否则给key列下标元组, 如 (0,1) 按 symbol+date 去重合并已有分区."""
    import pyarrow.parquet as pq, pyarrow as pa
    cols = DATASETS[ds]
    pathlib.Path(f"{WH}/{ds}").mkdir(parents=True, exist_ok=True)
    if merge_on is not None and os.path.exists(_part_path(ds, year)):
        t = pq.read_table(_part_path(ds, year))
        names = t.column_names
        cols_order = [names.index(c) for c in cols]
        old_rows, old_keys = [], set()
        for row in zip(*[t.column(n) for n in names]):
            fixed = tuple(row[i].as_py() if hasattr(row[i], 'as_py') else row[i] for i in cols_order)
            old_rows.append(fixed)
            old_keys.add(tuple(fixed[i] for i in merge_on))
        new_rows = [r for r in rows if tuple(r[i] for i in merge_on) not in old_keys]
        rows = old_rows + new_rows
    rows.sort(key=lambda r: (r[0], r[1]) if len(r) > 1 else r)
    table = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})
    pq.write_table(table, _part_path(ds, year), compression="zstd")
    syms = sorted({r[0] for r in rows})
    _update_index(ds, year, len(rows), os.path.getsize(_part_path(ds, year)), (syms[0], syms[-1]))

def _update_index(ds, year, rows, size, symbols_minmax=None):
    idx = json.load(open(IDX)) if os.path.exists(IDX) else {}
    ent = {"rows": rows, "size_bytes": size,
           "updated": datetime.datetime.now().isoformat(timespec="seconds")}
    if symbols_minmax:
        ent["sym_min"], ent["sym_max"] = symbols_minmax
    idx.setdefault(ds, {})[year] = ent
    json.dump(idx, open(IDX, "w"), indent=1)

def load(ds, year=None, symbols=None):
    con = duckdb.connect()
    parts = sorted(glob.glob(f"{WH}/{ds}/*.parquet"))
    if year:
        parts = [p for p in parts if str(year) in p]
    if not parts: return None
    q = f"SELECT * FROM read_parquet({parts!r})"
    if symbols:
        q += f" WHERE symbol IN {tuple(symbols)}"
    return con.execute(q).fetchdf()

def stats():
    idx = json.load(open(IDX)) if os.path.exists(IDX) else {}
    for ds, parts in idx.items():
        rows = sum(p["rows"] for p in parts.values())
        size = sum(p["size_bytes"] for p in parts.values())
        print(f"{ds:16} {len(parts)}分区 {rows:>12,}行 {size/1e6:>8.1f}MB")

if __name__ == "__main__":
    stats()
