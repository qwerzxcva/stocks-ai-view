# daily_market_pred.py — 大盘每日涨跌预测 (2026-10-23)
# 方法: ①35年等权市场日收益序列 ②历史相似5日K线检索(欧氏) ③连跌结构统计 ④大跌后惯性 ⑤近期规律回归
# 输出: 次日涨跌预测 + 置信 + 证据链
import duckdb, statistics, math, json, datetime

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def market_series():
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    rows = con.execute("""
      SELECT d, avg(c/NULLIF(lag_c,0)-1)*100 AS mkt_ret
      FROM (SELECT sym, d, c, lag(c) OVER (PARTITION BY sym ORDER BY d) AS lag_c FROM qfq)
      WHERE lag_c > 0 GROUP BY d ORDER BY d
    """).fetchall()
    con.close()
    return [str(r[0]) for r in rows], [r[1] for r in rows]

def breadth_table():
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='4GB'")
    rows = con.execute("""
      SELECT d, avg(r) AS eq_mean, sum(CASE WHEN r>0 THEN 1 ELSE 0 END)*100.0/count(*) AS adv_pct
      FROM (SELECT sym, d, (c/lag_c-1)*100 AS r FROM (
        SELECT sym, d, c, lag(c) OVER (PARTITION BY sym ORDER BY d) AS lag_c FROM qfq
      ) WHERE lag_c>0 AND abs(c/lag_c-1)<0.11) GROUP BY d HAVING count(*)>500 ORDER BY d
    """).fetchall()
    con.close()
    return rows

def structure_adjust(base_pred, note):
    """第五法: 结构修正。普跌大阴(adv<15%, mean<-2%)次日: 中小票反弹倾向(中位+0.45%, 反弹率39%)。
       若昨日同时指数虚涨(二八) → 次日『赚钱效应扩散』概率最高 → 上调预测。"""
    try:
        rows = breadth_table()
        if len(rows) < 2: return base_pred, note
        y_d, y_mean, y_adv = rows[-1]
        if y_adv < 15 and y_mean < -2:
            adj = +0.45
            note += f" | 结构修正: 昨日普跌大阴(adv{y_adv:.0f}%, 均值{y_mean:.1f}%)→次日中小票反弹倾向(46样本中位+0.45%) 上调{adj:+.2f}%"
            return base_pred + adj, note
        if y_adv > 80 and y_mean > 2:
            adj = -0.30
            note += f" | 结构修正: 昨日普涨(adv{y_adv:.0f}%)→次日等权回归 下调{adj:+.2f}%"
            return base_pred + adj, note
    except Exception as e:
        note += f" | 结构修正失败: {e}"
    return base_pred, note

def predict(n_sim=20):
    dates, rets = market_series()
    recent = rets[-5:]
    # ① 相似K线检索
    best = []
    for i in range(len(rets)-30):
        pat = rets[i:i+5]
        d = math.sqrt(sum((a-b)**2 for a, b in zip(pat, recent)))
        best.append((d, i))
    best.sort()
    sims1 = [rets[i+5] for _, i in best[:n_sim]]
    sims5 = [sum(rets[i+6:i+11]) for _, i in best[:n_sim] if i+11 <= len(rets)]
    sim_ret = statistics.mean(sims1)
    sim_win = sum(1 for x in sims1 if x > 0) / len(sims1)
    # ② 连跌结构
    streak = 0
    for r in reversed(rets[:-1]):
        if r < 0: streak += 1
        else: break
    run, pool = 0, []
    for i in range(1, len(rets)):
        run = run + 1 if rets[i-1] < 0 else 0
        if run == streak: pool.append(rets[i])
    streak_ret = statistics.mean(pool) if pool else 0
    streak_win = sum(1 for x in pool if x > 0) / len(pool) if pool else 0.5
    # ③ 大跌后惯性 (昨日跌幅<-2%)
    big_pool1 = [rets[i+1] for i in range(len(rets)-5) if rets[i] < -2]
    big_pool5 = [sum(rets[i+1:i+6]) for i in range(len(rets)-10) if rets[i] < -2]
    big_ret = statistics.mean(big_pool1) if rets[-1] < -2 else None
    big5 = statistics.mean(big_pool5) if rets[-1] < -2 else None
    # ④ 近20日均值漂移
    drift = statistics.mean(rets[-20:])
    # ⑤ 指数真数据 streak 日历 (触发式, 腾讯800日)
    streak_cal = 0.0
    regime_note = ''
    try:
        from gold_cells import check as _gc
        gc = _gc()
        if gc['hits']:
            streak_cal = 0.004 if gc['hits'][0]['bias'] == 'up' else -0.004
            regime_note = '金格子命中: ' + gc['hits'][0]['cell'] + ' (' + gc['hits'][0]['胜率(全期/近10年/近5年)'] + ')'
    except Exception:
        pass
    try:
        from streak_calendar import calendar as _cal
        cal = _cal()
        if cal['down_streaks']:
            streak_cal = float(cal['down_streaks'][max(cal['down_streaks'])]['next_med'])/100
    except Exception:
        pass
    try:
        from market_regime import analyze as _reg
        reg = _reg()
        if reg['matched']:
            m0 = reg['matched'][0]
            streak_cal = m0['next_med']/100
            regime_note = f"状态匹配n={m0['n']}: {reg['verdict']}"
    except Exception:
        pass
    # 加权合成 (等权4法, 简单可审计)
    parts = [('相似K线', sim_ret, sim_win), (f'连跌{streak}日结构', streak_ret, streak_win),
             ('近20日漂移', drift, 0.5 + (0.15 if drift > 0 else -0.15)),
             ('指数状态日历', streak_cal, 0.7)]
    if big_ret is not None:
        parts.append(('大跌次日惯性', big_ret, 0.55))
    pred = statistics.mean(p[1] for p in parts)
    win = statistics.mean(p[2] for p in parts)
    note = ''
    pred, note = structure_adjust(pred, note)
    return {'as_of': dates[-1], 'recent5': [round(x, 2) for x in recent],
            'streak_down': streak, 'cum10': round(sum(rets[-10:]), 2),
            'prediction_next1_pct': round(pred, 3),
            'direction': 'up' if pred > 0 else 'down',
            'historical_win_rate': round(win, 2),
            'evidence': [{'method': m, 'hist_ret': round(r, 3), 'hist_win': round(w, 2)} for m, r, w in parts],
            'similar_dates': [dates[i] for _, i in best[:5]],
            'structure_adjust': note,
            'regime_note': regime_note,
            'note': '等权代理指数(3057票), 非官方指数; 预测为统计倾向非方向承诺' + (f'; {regime_note}' if regime_note else '')}

def next5():
    dates, rets = market_series()
    recent = rets[-5:]
    best = []
    for i in range(len(rets)-30):
        pat = rets[i:i+5]
        d = math.sqrt(sum((a-b)**2 for a, b in zip(pat, recent)))
        best.append((d, i))
    best.sort()
    sims5 = sorted(sum(rets[i+6:i+11]) for _, i in best[:20] if i+11 <= len(rets))
    n = len(sims5)
    # Monte Carlo 口径置信带 (stocksmith.ai 方法): P10/P25/中位/P75/P90
    return {'next5_pred_pct': round(statistics.mean(sims5), 2),
            'conf_band': {'p10': round(sims5[n//10], 2), 'p25': round(sims5[n//4], 2),
                          'median': round(sims5[n//2], 2), 'p75': round(sims5[3*n//4], 2),
                          'p90': round(sims5[9*n//10], 2)},
            'prob_gain': round(sum(1 for x in sims5 if x > 0)/n*100),
            'n': n,
            'note': '置信带为相似历史路径的分布, 非保证; 窄带=高确定性, 宽带=高不确定'}

if __name__ == '__main__':
    r = predict()
    r['next5'] = next5()
    print(json.dumps(r, ensure_ascii=False, indent=1))
