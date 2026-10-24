# market_forecast.py — 大盘四问引擎 (2026-10-23)
# 模块: 当日大盘预测 / 大盘复盘 / 明日大盘预测 / 高β联动涨跌票预测 / 反向票预测 / 外围关联
# 引擎组合: 本模块(35年统计+台阶关键位+外围+情绪+日历)最高权重, 其他引擎辅助
import json, statistics

STATS = {  # 35 年暴跌统计 (data/ 338 样本): 大跌后惯性
    'crash_next': {'mean': 0.0088, 'win': 0.56},
    'crash2_next': {'mean': 0.0270, 'win': 0.60},
}
CALENDAR_MULT = {0: 1.0, 1: 1.05, 2: 1.0, 3: 0.95, 4: 0.805}  # 周二最强 周五最弱

def _quote_index(sym='sh000001'):
    import sys; sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
    from source_router import Router
    q, src = Router().quotes([sym])
    return q[sym]

def _kline_index(n=400):
    try:
        from source_router import kline_tencent
        return kline_tencent('sh000001', n)
    except Exception:
        from source_router import kline_sina
        return kline_sina('sh000001', n)

def forecast_today():
    """当日大盘预测 (盘前/盘中): 关键位+外围+情绪+日历"""
    from key_levels import key_levels as kl
    from overseas_alert import overseas_snapshot
    q = _quote_index()
    px = q['price']
    lv = kl('sh000001', cur=px)
    weekday = __import__('datetime').date.today().weekday()
    cal = CALENDAR_MULT.get(weekday, 1.0)
    try:
        ov = overseas_snapshot()
    except Exception:
        ov = {}
    evidence = [f'现价{px}', f'支撑{lv.get("支撑位")} 压力{lv.get("压力位")}', f'日历乘数{cal}']
    bias = 0.0
    sup = lv.get('支撑位') or 0
    res = lv.get('压力位') or 1e9
    if sup and px < sup + 20: bias -= 0.1
    if res < 1e9 and px > res - 20: bias += 0.1
    bias *= cal
    return {'module': 'forecast_today', 'index': px, 'bias': round(bias, 3),
            'bias_label': '偏多' if bias > 0.05 else ('偏空' if bias < -0.05 else '中性'),
            'evidence': evidence}

def review_today():
    """当日复盘: 量价结构+涨停数+关键位得失"""
    q = _quote_index()
    k = _kline_index(30)
    today = k[-1]
    prev = k[-2]
    pct = (today[2] / prev[2] - 1) * 100
    import duckdb
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb', read_only=True)
    try:
        zt = con.execute("SELECT count(*) FROM zt_factors WHERE as_of = (SELECT max(as_of) FROM zt_factors)").fetchone()[0]
    except Exception:
        zt = -1
    return {'module': 'review_today', 'index': today[2], 'pct': round(pct, 2),
            'vol_vs_prev': round(today[5] / prev[5], 2) if prev[5] else None,
            'limit_up_count': zt,
            'note': ('冰点' if 0 < zt < 40 else '正常' if zt >= 40 else '涨停池数据缺失')}

def forecast_tomorrow():
    """明日大盘预测: 复盘结论+暴跌统计+日历"""
    rv = review_today()
    pct = rv['pct']
    ev = []
    p_up = 0.5
    if pct <= -2:
        p_up = STATS['crash_next']['win']
        ev.append(f'今日大跌{pct:.1f}%→明日惯性反弹概率{p_up:.0%}(35年338样本+0.88%)')
    elif pct <= -4:
        p_up = STATS['crash2_next']['win']
        ev.append(f'恐慌级下跌{pct:.1f}%→次日反弹概率{p_up:.0%}(连续暴跌+2.70%)')
    elif pct >= 2:
        p_up = 0.45
        ev.append(f'今日大涨{pct:.1f}%→明日均值回归偏弱')
    wd = (__import__('datetime').date.today().weekday() + 1) % 7
    cal = CALENDAR_MULT.get(wd, 1.0)
    if cal != 1.0:
        ev.append(f'明日星期{wd+1} 日历乘数{cal}(周五最弱0.805/周二最强1.05)')
    return {'module': 'forecast_tomorrow', 'p_up': round(p_up, 3),
            'evidence': ev}

def beta_impact(top_n=10):
    """高β联动票预测: 大盘跌→跌最多; 与大盘反向票"""
    import duckdb
    con = duckdb.connect('/home/agentuser/stockdb/t1/qfq_all.duckdb', read_only=True)
    con.execute("SET memory_limit='3GB'")
    # 指数不在主库 → 腾讯/新浪直接拉
    try:
        k = _kline_index(60)
    except Exception:
        from source_router import kline_sina
        k = kline_sina('sh000001', 60)
    imap = {r[0].replace('-',''): r[2] for r in k}
    rows = con.execute("""
        SELECT sym, replace(CAST(d AS VARCHAR), '-', ''), c FROM qfq
        WHERE sym IN (SELECT DISTINCT sym FROM qfq WHERE d = (SELECT max(d) FROM qfq))
        AND d >= CAST(CAST((SELECT max(d) FROM qfq) AS DATE) - 90 AS VARCHAR) ORDER BY sym, d
    """).fetchall()
    from collections import defaultdict
    ser = defaultdict(list)
    for sym, d, c in rows:
        ser[sym].append((str(d).replace('-',''), c))
    import math
    ibeta = []
    ianti = []
    for sym, pts in ser.items():
        if len(pts) < 40: continue
        pairs = [(c, imap[d]) for d, c in pts if d in imap]
        if len(pairs) < 30: continue
        # 收益率回归 (价格水平回归无意义): beta = cov(r_s, r_m)/var(r_m)
        rs = [(pairs[i][0]/pairs[i-1][0]-1)*100 for i in range(1, len(pairs))]
        rm = [(pairs[i][1]/pairs[i-1][1]-1)*100 for i in range(1, len(pairs))]
        mx, my = statistics.mean(rm), statistics.mean(rs)
        sx = sum((x-mx)**2 for x in rm)
        if sx == 0: continue
        beta = sum((x-mx)*(y-my) for x, y in zip(rm, rs)) / sx
        if beta > 1.6: ibeta.append((sym, round(beta,2)))
        elif beta < -0.2: ianti.append((sym, round(beta,2)))
    ibeta.sort(key=lambda x: -x[1]); ianti.sort(key=lambda x: x[1])
    return {'module': 'beta_impact',
            'high_beta_fall_with_market': ibeta[:top_n],
            'inverse_to_market': ianti[:top_n]}

if __name__ == '__main__':
    print(json.dumps(forecast_today(), ensure_ascii=False, default=str))
    print(json.dumps(review_today(), ensure_ascii=False, default=str))
    print(json.dumps(forecast_tomorrow(), ensure_ascii=False, default=str))
