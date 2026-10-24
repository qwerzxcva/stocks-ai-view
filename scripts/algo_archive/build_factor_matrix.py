#!/usr/bin/env python3
"""全市场因子矩阵: 每票29因子全历史 → parquet分片(算法仓核心资产)"""
import duckdb, pandas as pd, numpy as np, os, sys
sys.path.insert(0, "/home/agentuser/stocks-algo")
import factor_lib

OUT = "/tmp/factor_matrix"
os.makedirs(OUT, exist_ok=True)
con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily ORDER BY symbol").fetchall()]
print("symbols:", len(syms), flush=True)

shard, shard_no, total = [], 0, 0
def flush(shard, shard_no):
    df = pd.concat(shard, ignore_index=True)
    df.to_parquet(f"{OUT}/factors_{shard_no:03d}.parquet", compression="zstd", index=False)
    print(f"  shard {shard_no}: {len(df)} rows, {os.path.getsize(f'{OUT}/factors_{shard_no:03d}.parquet')//1024}KB", flush=True)

for i, sym in enumerate(syms):
    rows = con.execute("SELECT date, open, close, high, low, volume FROM daily WHERE symbol=? ORDER BY date", [sym]).fetchdf()
    if len(rows) < 30:
        continue
    rows.columns = ["date", "open", "close", "high", "low", "volume"]
    full = factor_lib.compute_all(rows)
    full.insert(0, "symbol", sym)
    shard.append(full)
    total += len(full)
    if sum(len(s) for s in shard) > 400000:
        flush(shard, shard_no); shard_no += 1; shard = []
    if (i+1) % 500 == 0:
        print(f"{i+1}/{len(syms)} total={total}", flush=True)
if shard:
    flush(shard, shard_no)
print(f"FACTOR_MATRIX_DONE shards={shard_no+1} total_rows={total}")
