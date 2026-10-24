#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据体检: bulk 分片的 (code,date) 重复率、多值冲突、复权跳变、脏值。"""
import glob
from collections import defaultdict

files = sorted(glob.glob('/root/workspace/stocks-data-kline/data/bulk/day_qfq_*.csv'))
total = 0
dup_same = 0
dup_conflict = 0
first = {}          # (code,date) -> close (first seen)
big_moves = 0       # |日收益| > 31% (复权断裂/脏数据)
flat_pairs = 0      # 连续两日 close 完全相等
pair_total = 0
code_set = set()
per_file_codes = defaultdict(set)
neg = 0
for p in files:
    prev = {}
    with open(p, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '|' not in line:
                continue
            code, rest = line.split('|', 1)
            parts = rest.split('|') if '|' in rest else rest.split(',')
            if len(parts) < 3:
                continue
            try:
                d = parts[0]; c = float(parts[2])
            except ValueError:
                continue
            total += 1
            code_set.add(code)
            per_file_codes[p.split('/')[-1]].add(code)
            k = (code, d)
            if k in first:
                if abs(first[k] - c) < 1e-9:
                    dup_same += 1
                else:
                    dup_conflict += 1
            else:
                first[k] = c
            pc = prev.get(code)
            if pc is not None and pc[0] < d and c > 0 and pc[1] > 0:
                r = (c / pc[1] - 1) * 100
                pair_total += 1
                if abs(r) > 31:
                    big_moves += 1
                if c == pc[1]:
                    flat_pairs += 1
            prev[code] = (d, c)
print('总行数:', total)
print('唯一股票数:', len(code_set))
print('唯一(code,date):', len(first))
print('重复行 同值:', dup_same, ' 冲突值:', dup_conflict)
print('相邻对:', pair_total, ' |>31%|:', big_moves,
      f'({big_moves/max(pair_total,1)*100:.1f}%)', ' 平盘:', flat_pairs)
for fn, cs in sorted(per_file_codes.items()):
    print(fn, len(cs))
