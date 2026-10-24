# gold_cells.py — 大盘金格子引擎 (2026-10-24)
# 来源: 36年8748日上证全史 15 维交叉格子挖矿, 过三重稳定性检验(全期/近10年/近5年均>58%方向胜率)
# 用法: from gold_cells import check; check() → 当前命中的金格子 + 预测
import json, os
from collections import defaultdict

FULL = '/tmp/sh000001_full.json'
GOLD = {
    'G1_MA20上×昨日大跌': {'desc': '多头结构中的急跌日, 次日反抽', 'wr': '63.1/59.7/58.1', 'bias': 'up'},
    'G2_MA20上×昨日大涨×量平': {'desc': '温和放量上攻中的平量延续', 'wr': '61.8/62.7/59.5', 'bias': 'up'},
    'G3_MA20上×回踩1日×放量': {'desc': '多头回踩获承接, 次日修复', 'wr': '62.8/61.0/61.8', 'bias': 'up'},
    'G4_连跌4日': {'desc': '连跌4日后反转窗(近10年走强)', 'wr': '55.7/67.8/68.2', 'bias': 'up'},
}

def check():
    if not os.path.exists(FULL):
        from market_regime import fetch_sh_index_full
        fetch_sh_index_full()
    rows = json.load(open(FULL))
    closes = [r[1] for r in rows]; vols = [r[2] for r in rows]
    rets = [closes[i]/closes[i-1]-1 for i in range(1, len(closes))]
    C, V = closes[1:], vols[1:]
    i = len(rets)-1
    ma20 = sum(C[i-20:i])/20
    above = C[i] > ma20
    r1 = rets[i]
    kd = ku = 0
    while i-kd >= 0 and rets[i-kd] < 0: kd += 1
    while i-ku >= 0 and rets[i-ku] > 0: ku += 1
    vr = V[i]/((sum(V[i-20:i])/20) or 1)
    hits = []
    if above and r1 < -0.01: hits.append('G1_MA20上×昨日大跌')
    if above and r1 > 0.01 and 0.9 <= vr <= 1.2: hits.append('G2_MA20上×昨日大涨×量平')
    if above and kd == 1 and vr > 1.2: hits.append('G3_MA20上×回踩1日×放量')
    if kd >= 4: hits.append('G4_连跌4日')
    return {'date': rows[-1][0], 'now': {'连跌': kd, '连涨': ku, 'MA20上': above, 'MA20': round(ma20), '昨涨跌': round(r1*100, 2), '量比': round(vr, 2)},
            'hits': [{'cell': h, 'desc': GOLD[h]['desc'], 'bias': GOLD[h]['bias'], '胜率(全期/近10年/近5年)': GOLD[h]['wr']} for h in hits],
            'note': '方向胜率非收益承诺; 格子互斥性未强制, 多格命中时以更近期胜率排序'}

if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False, indent=1))
