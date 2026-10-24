# structure_lens.py — 结构市透镜 (2026-10-23 复盘产物)
# 根因: 10-22 上证+2.29% 但全市场等权 -2.64% (极致二八)。大盘预测吃等权序列 → 把"权重拉指数、散户票普跌"
#       误读为普跌 → 推防御 → 10-23 创业板+4.16% 大反攻日全踏空。
# 功能: 每日收盘输出「指数 vs 等权」背离度 + 结构性质标签, 喂给 daily_market_pred 和 orchestrator。
import duckdb, statistics, json

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def breadth(day):
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    rows = con.execute("""
      SELECT (c/lag_c-1)*100 AS r FROM (
        SELECT sym, d, c, lag(c) OVER (PARTITION BY sym ORDER BY d) AS lag_c FROM qfq
      ) WHERE d=? AND lag_c>0 AND abs(c/lag_c-1)<0.11
    """, [day]).fetchall()
    con.close()
    if not rows: return None
    vals = [r[0] for r in rows]
    return {'day': day, 'n': len(vals), 'eq_mean': round(statistics.mean(vals), 2),
            'eq_median': round(sorted(vals)[len(vals)//2], 2),
            'adv_pct': round(sum(1 for v in vals if v > 0)/len(vals)*100, 1)}

def divergence(idx_ret, day):
    b = breadth(day)
    if not b: return None
    gap = idx_ret - b['eq_mean']
    if gap > 1.5:   tag = '二八权重拉抬(指数虚涨, 中小票弱)'
    elif gap < -1.5: tag = '八二普涨(指数低估市场热度)'
    elif abs(gap) <= 0.5: tag = '同涨同跌(健康结构)'
    else: tag = '轻度分化'
    b.update({'index_ret': idx_ret, 'gap': round(gap, 2), 'structure': tag})
    return b

def regime_hint():
    """近3日结构 → 明日修正:
       连续「二八」≥2日 → 权重行情延续; 「二八」转「八二」→ 赚钱效应扩散(题材全面补涨); 普涨次日 → 等权回归中性"""
    import glob
    days = sorted(glob.glob('/tmp/breadth_*.json'))[-3:]
    tags = [json.load(open(f)).get('structure') for f in days]
    if tags.count('二八权重拉抬(指数虚涨, 中小票弱)') >= 2:
        hint = '权重行情延续, 上调指数预测'
    elif tags and tags[-1] == '二八权重拉抬(指数虚涨, 中小票弱)':
        hint = '警惕赚钱效应扩散: 题材/中小票补涨概率上升'
    else:
        hint = '无强结构信号'
    return {'recent': tags, 'hint': hint}

if __name__ == '__main__':
    import sys
    b = divergence(float(sys.argv[1]), sys.argv[2])
    json.dump(b, open(f"/tmp/breadth_{sys.argv[2]}.json", 'w'), ensure_ascii=False)
    print(json.dumps(b, ensure_ascii=False))
