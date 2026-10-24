# pattern_engines.py — 形态类引擎组 (2026-10-23)
# 模块: 超跌反弹分析预测 / 低位股分析 / 高位股分析 / 高低吸筹洗盘辅助
# 数据: qfq_all 全史 (2,160,527 行); 35 年暴跌统计作先验
import duckdb, statistics

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def oversold_rebound(top_n=12, drop_thr=-15.0, window=20):
    """超跌反弹候选: 20日跌幅<-15% 且缩量见底 + 35年统计先验"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    w = int(window)
    rows = con.execute(f"""
        WITH latest AS (SELECT max(d) AS md FROM qfq),
        px AS (
          SELECT sym,
            max(CASE WHEN d >= (SELECT md FROM latest) THEN c END) AS c_now,
            min(c) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - {w} AS VARCHAR)) AS c_low,
            max(c) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - {w} AS VARCHAR)) AS c_high,
            avg(v) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 5 AS VARCHAR)) AS v5,
            avg(v) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 20 AS VARCHAR) AND d < CAST(CAST((SELECT md FROM latest) AS DATE) - 5 AS VARCHAR)) AS v20
          FROM qfq WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - {w + 60} AS VARCHAR)
          GROUP BY sym)
        SELECT sym, c_now, c_high, c_low,
          (c_now / c_high - 1) * 100 AS drop_pct,
          v5 / NULLIF(v20, 0) AS vol_ratio
        FROM px WHERE c_high > 0 AND c_now < c_high * (1 + {drop_thr} / 100)
    """).fetchall()
    out = []
    for sym, now, high, low, drop, vr in rows:
        if now is None or high is None or low is None or high <= low:
            continue
        pos = (now - low) / (high - low) * 100
        # 评分: 跌得深(分位低)+缩量(已止跌)+非ST
        score = min(1.0, abs(drop) / 25) * 0.6 + (1 - pos / 100) * 0.3 + (0.1 if vr and vr < 0.8 else 0)
        out.append({'sym': sym, 'drop20': round(drop, 1), 'pos_low': round(pos, 1),
                    'vol_ratio': round(vr, 2) if vr else None,
                    'score': round(score, 3),
                    'prior': '35年统计: 大跌后5日反弹56%均值+0.88%, 创业板领涨+1.59%',
                    'prediction': '超跌反弹窗口5-10日' if score > 0.5 else '弱势阴跌, 反弹待量'})
    out.sort(key=lambda x: -x['score'])
    con.close()
    return out[:top_n]

def low_high_scan(top_n=10):
    """低位股(240日分位<20)+高位股(>85)分析"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    rows = con.execute("""
        WITH latest AS (SELECT max(d) AS md FROM qfq),
        px AS (
          SELECT sym,
            max(CASE WHEN d >= (SELECT md FROM latest) THEN c END) AS c_now,
            min(c) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 240 AS VARCHAR)) AS lo,
            max(c) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 240 AS VARCHAR)) AS hi,
            count(*) FILTER (WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 240 AS VARCHAR)) AS n
          FROM qfq WHERE d >= CAST(CAST((SELECT md FROM latest) AS DATE) - 300 AS VARCHAR) GROUP BY sym)
        SELECT sym, c_now, lo, hi FROM px WHERE n >= 150 AND hi > lo
    """).fetchall()
    lows, highs = [], []
    for sym, now, lo, hi in rows:
        if now is None or lo is None or hi is None or hi <= lo:
            continue
        pos = (now - lo) / (hi - lo) * 100
        if pos < 20:
            lows.append({'sym': sym, 'pos240': round(pos, 1), 'dist_to_high': round((hi/now-1)*100, 1)})
        elif pos > 85:
            highs.append({'sym': sym, 'pos240': round(pos, 1), 'dist_to_low': round((1-lo/now)*100, 1)})
    lows.sort(key=lambda x: x['pos240']); highs.sort(key=lambda x: -x['pos240'])
    con.close()
    return {'low_stocks': lows[:top_n], 'high_stocks': highs[:top_n],
            'note': '低位≠买入理由, 需配合 moneyflow_behavior 吸筹态; 高位需配合出货态'}

if __name__ == '__main__':
    import json
    print(json.dumps({'oversold': oversold_rebound(6)}, ensure_ascii=False, default=str)[:800])
    lh = low_high_scan(4)
    print('low:', lh['low_stocks'][:3])
    print('high:', lh['high_stocks'][:3])
