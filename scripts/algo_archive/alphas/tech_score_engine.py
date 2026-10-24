# tech_score_engine.py — 技术面三维评分 (2026-10-24, 吸收 stock-scanner 评分框架)
# MA排列±20 / RSI位置 / MACD±15 / 布林位置 / 量能±10 → 0-100 分
import duckdb, statistics

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def tech_score(sym):
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    rows = con.execute("SELECT d, c, v FROM qfq WHERE sym=? ORDER BY d DESC LIMIT 60", [sym]).fetchall()
    con.close()
    if len(rows) < 35: return None
    rows = rows[::-1]  # 时间升序
    closes = [r[1] for r in rows]; vols = [r[2] for r in rows]
    score = 50.0
    # MA 排列 (5/10/20)
    ma5, ma10, ma20 = statistics.mean(closes[-5:]), statistics.mean(closes[-10:]), statistics.mean(closes[-20:])
    if ma5 > ma10 > ma20: score += 20
    elif ma5 < ma10 < ma20: score -= 20
    # RSI 14
    diffs = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    up = statistics.mean([d for d in diffs[-14:] if d > 0]) if any(d > 0 for d in diffs[-14:]) else 0
    dn = -statistics.mean([d for d in diffs[-14:] if d < 0]) if any(d < 0 for d in diffs[-14:]) else 0
    rsi = 100 * up / (up + dn) if (up + dn) else 50
    if 30 <= rsi <= 70: score += 10
    elif rsi < 30: score += 5
    elif rsi > 70: score -= 5
    # MACD 信号 (EMA12-EMA26 vs 信号线9)
    def ema(vals, n):
        k = 2/(n+1); e = vals[0]
        for v in vals[1:]: e = v*k + e*(1-k)
        return e
    dif = ema(closes, 12) - ema(closes, 26)
    dea = ema([c for c in closes[-30:]], 9)  # 近似
    if dif > dea: score += 15
    else: score -= 15
    # 布林位置 (20日)
    mu, sd = statistics.mean(closes[-20:]), statistics.pstdev(closes[-20:])
    bpos = (closes[-1] - (mu - 2*sd)) / (4*sd) if sd else 0.5
    if 0.2 <= bpos <= 0.8: score += 5
    elif bpos < 0.2: score += 10
    elif bpos > 0.8: score -= 5
    # 量能
    v5, v20 = statistics.mean(vols[-5:]), statistics.mean(vols[-20:])
    chg5 = (closes[-1]/closes[-6]-1)*100 if len(closes) > 6 else 0
    if v5 > v20*1.3 and chg5 > 0: score += 10
    elif v5 > v20*1.3 and chg5 < 0: score -= 10
    return {'sym': sym, 'score': round(max(0, min(100, score)), 1), 'rsi': round(rsi, 1),
            'ma': '多头' if ma5 > ma10 > ma20 else ('空头' if ma5 < ma10 < ma20 else '缠绕'),
            'boll_pos': round(bpos, 2), 'vol_ratio': round(v5/v20, 2) if v20 else 0}

if __name__ == '__main__':
    import sys
    for s in (sys.argv[1:] or ['sh600900','sz002535','sh601328']):
        print(tech_score(s))
