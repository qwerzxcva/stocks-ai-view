# market_regime.py — 条件化大盘状态日历 (2026-10-24)
# 方法: 36年上证全史(8748日, akshare stock_zh_index_daily), 五维状态切片:
#   连跌天数 × 5日跌幅深度 × 量比(今日/20日均量) × MA20位置 × 距250日高回撤
# 输出: 当前状态 + 历史同状态后续1/5/10日分布 + 反弹区/熊市区判定
# 数据: /tmp/sh000001_full.json 由 fetch_sh_index_full() 刷新(建议每周)
import json, statistics, os
from collections import defaultdict

FULL = '/tmp/sh000001_full.json'

def fetch_sh_index_full():
    import sys
    sys.path.insert(0, '/tmp/senv/lib/python3.12/site-packages')
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000001")
    rows = [(str(d), float(c), float(v)) for d, c, v in zip(df['date'], df['close'], df['volume'])]
    json.dump(rows, open(FULL, 'w'))
    return rows

def analyze():
    if not os.path.exists(FULL):
        fetch_sh_index_full()
    rows = json.load(open(FULL))
    closes = [r[1] for r in rows]; vols = [r[2] for r in rows]; dates = [r[0] for r in rows]
    rets = [closes[i]/closes[i-1]-1 for i in range(1, len(closes))]
    C, V = closes[1:], vols[1:]

    def state(i):
        kd = 0
        while i-1-kd >= 0 and rets[i-1-kd] < 0: kd += 1
        d5 = C[i]/C[i-5]-1 if i >= 5 else 0
        v20 = sum(V[max(0, i-20):i])/20 if i >= 20 else V[i]
        # 量纲异常防护: 20日均量与当日量级差>8倍 → 用5日均量
        if v20 < V[i]/8 or v20 > V[i]*8:
            v20 = sum(V[max(0, i-5):i])/5 if i >= 5 else V[i]
        vr = V[i]/(v20 or 1)
        ma20 = sum(C[i-20:i])/20 if i >= 20 else C[i]
        hi = max(C[max(0, i-250):i+1])
        return kd, d5, vr, C[i] < ma20, C[i]/hi-1

    pool = defaultdict(list)
    for i in range(250, len(rets)-10):
        kd, d5, vr, below20, dd = state(i)
        if kd < 2 and d5 > -0.03: continue
        depth = '<3%' if d5 > -0.03 else '3-7%' if d5 > -0.07 else '7-12%' if d5 > -0.12 else '>12%'
        ddb = '<8%' if dd > -0.08 else '8-20%' if dd > -0.20 else '20-35%' if dd > -0.35 else '>35%'
        pool[(min(kd, 5), depth, '缩' if vr < 0.9 else '常量',
              below20, ddb)].append(
            (rets[i], C[min(i+5, len(C)-1)]/C[i]-1, C[min(i+10, len(C)-1)]/C[i]-1))

    table = []
    for k, v in pool.items():
        if len(v) < 40: continue
        n = len(v)
        f1 = sorted(x[0] for x in v); f5 = sorted(x[1] for x in v); f10 = sorted(x[2] for x in v)
        table.append({'cond': {'连跌': k[0], '5日深度': k[1], '量': k[2], 'MA20下': k[3], '250日回撤': k[4]},
                      'n': n, 'next_med': round(f1[n//2]*100, 2), 'next_win': round(sum(1 for x in v if x[0] > 0)/n*100),
                      'd5_med': round(f5[n//2]*100, 2), 'd10_med': round(f10[n//2]*100, 2)})

    # 当前状态
    i = len(rets)-1
    kd, d5, vr, below20, dd = state(i)
    now = {'date': dates[-1], '连跌': kd, '5日跌幅': round(d5*100, 2), '量比': round(vr, 2),
           'MA20下方': below20, '250日回撤': round(dd*100, 1)}
    # 匹配历史同状态
    depth = '<3%' if d5 > -0.03 else '3-7%' if d5 > -0.07 else '7-12%' if d5 > -0.12 else '>12%'
    ddb = '<8%' if dd > -0.08 else '8-20%' if dd > -0.20 else '20-35%' if dd > -0.35 else '>35%'
    match = [t for t in table if t['cond']['连跌'] == min(kd, 5) and t['cond']['MA20下'] == below20
             and t['cond']['250日回撤'] == ddb and t['cond']['5日深度'] == depth]
    # 判定
    verdict = '观察'
    if below20 and d5 < -0.03 and kd <= 1: verdict = '一日急杀不接刀区(历史次日胜率18-29%)'
    if (not below20) and kd >= 2: verdict = '健康回调反弹区(历史65-83%胜率)'
    if below20 and dd < -0.20 and kd >= 2: verdict = '深熊延续风险区(10日中位为负)'
    if below20 and kd >= 3 and -0.20 < dd: verdict = '超跌反弹窗(连跌3+20日线下, 61%胜率)'
    return {'now': now, 'verdict': verdict, 'matched': match[:3], 'table_top': sorted(table, key=lambda x: -x['n'])[:8]}

if __name__ == '__main__':
    print(json.dumps(analyze(), ensure_ascii=False, indent=1))
