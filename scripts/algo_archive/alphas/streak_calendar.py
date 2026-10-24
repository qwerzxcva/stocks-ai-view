# streak_calendar.py — 连跌/连涨反转日历 (2026-10-24, 三角洲的可检验替代)
# 口径: 腾讯指数真K线; 统计连跌k日后次日/5日分布; 窗口="状态触发"而非"日期预言"
# 与三角洲的区别: 触发式(状态可观测) vs 日期式(不可检验), 本引擎样本外可复验
import urllib.request, json, statistics
from collections import defaultdict

def idx_kline(sym='sh000001', n=800):
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq',
        headers={'User-Agent': 'Mozilla/5.0'}), timeout=10).read())
    k = d['data'][sym]['day']
    return [x[0] for x in k], [float(x[2]) for x in k]

def calendar(sym='sh000001'):
    dates, closes = idx_kline(sym)
    rets = [(closes[i]/closes[i-1]-1) for i in range(1, len(closes))]
    # 连跌
    stats_dn, stats_up = defaultdict(list), defaultdict(list)
    for i in range(4, len(rets)-6):
        kd = 0
        while i-1-kd >= 0 and rets[i-1-kd] < 0: kd += 1
        ku = 0
        while i-1-ku >= 0 and rets[i-1-ku] > 0: ku += 1
        f1 = rets[i]
        f5 = closes[min(i+5, len(closes)-1)]/closes[i]-1
        if kd >= 2: stats_dn[kd].append((f1, f5))
        if ku >= 3: stats_up[ku].append((f1, f5))
    out = {'down_streaks': {}, 'up_streaks': {}}
    for kk, v in sorted(stats_dn.items()):
        if len(v) >= 25:
            f1 = sorted(x[0] for x in v); f5 = sorted(x[1] for x in v); n = len(v)
            out['down_streaks'][kk] = {'n': n, 'next_med': round(f1[n//2]*100, 2),
                'next_win': round(sum(1 for x in v if x[0] > 0)/n*100), 'fwd5_med': round(f5[n//2]*100, 2)}
    for kk, v in sorted(stats_up.items()):
        if len(v) >= 25:
            f1 = sorted(x[0] for x in v); f5 = sorted(x[1] for x in v); n = len(v)
            out['up_streaks'][kk] = {'n': n, 'next_med': round(f1[n//2]*100, 2),
                'next_win': round(sum(1 for x in v if x[0] > 0)/n*100), 'fwd5_med': round(f5[n//2]*100, 2)}
    # 当前状态
    kd = ku = 0
    i = len(rets)-1
    while rets[i-1-kd] < 0: kd += 1
    while rets[i-1-ku] > 0: ku += 1
    out['now'] = {'down': kd, 'up': ku, 'last_date': dates[-1]}
    # 触发提示
    if kd >= 2:
        s = out['down_streaks'].get(kd+1, out['down_streaks'].get(kd))
        if s: out['trigger'] = f"已连跌{kd}日, 若明日续跌至{kd+1}日: 历史次日中位{s['next_med']:+.2f}% 胜率{s['next_win']}%"
    return out

if __name__ == '__main__':
    print(json.dumps(calendar(), ensure_ascii=False, indent=1))
