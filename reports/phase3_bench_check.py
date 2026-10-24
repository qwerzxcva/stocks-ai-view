#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照检查: reflect_engine 解析口径(4019只)下的每日上涨家数比例基准,
验证 29.45% 是否为 bulk 数据假象(无脑扫描显示 ~80%, 明显异常)。"""
import sys
from collections import defaultdict
sys.path.insert(0, '/root/workspace/stocks-ai-view/scripts')
from reflect_engine import load_klines

kl = load_klines('/root/workspace/stocks-data-kline/data/bulk')
print('codes(reflect口径):', len(kl))
bydate = defaultdict(lambda: [0, 0])
for code, bars in kl.items():
    for i in range(len(bars) - 1):
        d = bars[i + 1][0]
        if bars[i][2] <= 0:
            continue
        ret = (bars[i + 1][2] / bars[i][2] - 1) * 100
        if -21 <= ret <= 31:  # 剔除拆分/脏数据
            c = bydate[d]
            c[0] += 1 if ret > 0 else 0
            c[1] += 1
ds = [d for d in sorted(bydate) if d >= '2023-11-21' and bydate[d][1] > 1000]
r = [bydate[d][0] / bydate[d][1] * 100 for d in ds]
print('mean up-ratio (reflect universe, 过滤后):', round(sum(r) / len(r), 2),
      '% days', len(ds))
print('samples:', [(d, round(bydate[d][0] / bydate[d][1] * 100, 1), bydate[d][1])
                   for d in ds[::150]])
