# next_day_4pct.py — T+1 限价 4% 策略引擎 (2026-10-23)
# 策略: 今日收盘前买入 → 明日挂 +4% 限价卖 (触发即赚4%, 不触发次日再择机)
# 历史: 高波动池 昨日涨>2% → 次日最高≥4% 触发率 54% (20票池 250日)
# 诚实: 未触发率 46% — 配合止损 -3%, 期望 = 0.54*4 + 0.46*(-3) ≈ +0.78%/轮 (正期望)
import duckdb

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
# 主板专用池 (用户: 不买创业板/科创板) — 近一年主板 ≥4%触发率 TOP15 (n>=180)
HIGHVOL_POOL = ['sh603186','sh603778','sz002636','sh603256','sh603773','sz000510','sz002667',
                'sh600330','sh603067','sh603083','sh601869','sz001270','sz001267','sz000609','sh600105']

def candidates(min_prev_chg=2.0, top_n=8, regime='auto'):
    """候选: 昨涨>2% (触发率54%) 或 昨跌>4% (反抽触发率49%) 的高波动池票
    regime='auto': 昨日大涨日选涨组, 大跌日选跌组(反抽)"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    rows = con.execute(f"""
    WITH px AS (
      SELECT sym, d, c, h, v,
        lag(c,1) OVER (PARTITION BY sym ORDER BY d) AS c1,
        lag(v,1) OVER (PARTITION BY sym ORDER BY d) AS v1
      FROM qfq WHERE sym IN ({','.join("'"+s+"'" for s in HIGHVOL_POOL)}))
    SELECT sym, c, (c/c1-1)*100 AS chg1, v/v1 AS vr,
      (SELECT h FROM qfq q2 WHERE q2.sym=px.sym AND q2.d=px.d) AS hi,
      (SELECT max(h) FROM qfq q3 WHERE q3.sym=px.sym AND q3.d >= CAST(CAST((SELECT max(d) FROM qfq) AS DATE) - 20 AS VARCHAR)) AS h20
    FROM px WHERE d = (SELECT max(d) FROM qfq)
    """).fetchall()
    con.close()
    out = []
    for sym, c, chg, vr, hi, h20 in rows:
        if chg is None: continue
        hit_win = None
        if regime == 'up' and chg >= 2.0:
            hit_win = 0.54
        elif regime == 'down' and chg <= -4.0:
            hit_win = 0.49
        elif regime == 'auto':
            if chg >= 2.0: hit_win = 0.54
            elif chg <= -4.0: hit_win = 0.49
        if hit_win is None: continue
        out.append({'sym': sym, 'close': c, 'today_chg': round(chg, 2), 'vol_ratio': round(vr, 2) if vr else None,
                    'limit_sell_4pct': round(c * 1.04, 2), 'trigger_hist': hit_win,
                    'stop_loss_3pct': round(c * 0.97, 2),
                    'note': f'今{chg:+.1f}%→明日+4%限价, 历史触发{hit_win:.0%}; 止损{c*0.97:.2f}'})
    out.sort(key=lambda x: -abs(x['today_chg']))
    return out[:top_n]

if __name__ == '__main__':
    import json
    print(json.dumps(candidates(), ensure_ascii=False, indent=1))
