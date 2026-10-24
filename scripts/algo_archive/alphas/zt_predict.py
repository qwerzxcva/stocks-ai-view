# zt_predict.py — 主板涨停/4%前兆引擎 (2026-10-23 反思产物)
# 核心结论 (35年/近一年主板 59 万样本验证):
#   涨停票前兆 = 昨日「涨>3% + 量比2-4」→ 今日≥4%触发32%, 涨停率8.4% (全市场基准 2.0%, 4.2倍)
#   陷阱: 光放量不涨(量比2.5+未涨) 涨停率仅1.9%; 量比>4 已爆量衰减
#   纯量价抓不到的: 题材情绪票 (平量2-4涨停) → 需 dual_theme 题材梯队叠加
import duckdb, json

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
STATS = {'A_trigger4': 0.32, 'A_zt': 0.084, 'base_trigger4': 0.15, 'base_zt': 0.020}

def scan(min_prev_chg=3.0, vr_range=(2.0, 4.0), top_n=10, trade_date=None):
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    con.execute("SET temp_directory='/tmp/ducktmp'")
    rows = con.execute(f"""
    WITH px AS (
      SELECT sym, d, c, v,
        lag(c,1) OVER (PARTITION BY sym ORDER BY d) AS c1,
        lag(v,1) OVER (PARTITION BY sym ORDER BY d) AS v1,
        avg(v) OVER (PARTITION BY sym ORDER BY d ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING) AS v5
      FROM qfq WHERE (sym LIKE 'sh60%' OR sym LIKE 'sz00%'))
    SELECT sym, c, v1/NULLIF(v5,0) AS vr,
      (c1/lag(c1,1) OVER (PARTITION BY sym ORDER BY d) - 1)*100 AS prev_chg
    FROM px WHERE d = COALESCE('{trade_date}', (SELECT max(d) FROM qfq)) AND c1 > 0 AND v5 > 0
    """).fetchall()
    con.close()
    out = []
    for sym, c, vr, chg in rows:
        if chg is None or chg < min_prev_chg or vr is None or not (vr_range[0] <= vr <= vr_range[1]):
            continue
        out.append({'sym': sym, 'close': c, 'prev_chg': round(chg, 2), 'vol_ratio': round(vr, 2),
                    'limit4': round(c * 1.04, 2),
                    'prob_4pct': STATS['A_trigger4'], 'prob_zt': STATS['A_zt']})
    out.sort(key=lambda x: -(x['prev_chg'] * min(x['vol_ratio'], 4)))
    return out[:top_n]

if __name__ == '__main__':
    r = scan()
    print(json.dumps({'stats': STATS, 'candidates': r}, ensure_ascii=False, indent=1)[:1500])
