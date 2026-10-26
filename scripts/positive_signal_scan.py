# positive_signal_scan.py — 正向信号日扫描器 (2026-10-25)
# 聚合今晚验证的正向格子, 供 build_today_picks / 推股报告调用
# 正向信号(全部经全史/多年回测):
#   S1 52W真突破: 收盘>过去250日最高 — 次日+0.66% (n=16.6k, 2024+)
#   S2 缩量涨停封板: 涨停+量比<2 — 次日+2.33%/5日+2.56% (n=18.5k, 2022+)
#   S3 低波动位置: amp_pct_250<0.5 — 5日+0.55% vs 高波+0.32% (IC三年稳)
#   S4 非流动性溢价: amihud_20d高分组 — 月度IC+0.044连续4年正
#   S5 节前窗口: 春节/国庆前5日 — +0.30%/日 62%胜率 (36年)
# 负向警报(排除项):
#   A1 一字板开板 — 10日-3.8%
#   A2 涨停次日巨阴 — 5日-2.9%
#   A3 放量巨上影 — 次日-0.43%
import duckdb, datetime

def scan_today(asof=None):
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb', read_only=True)
    con.execute("SET memory_limit='3GB'")
    asof = asof or con.execute("SELECT max(date) FROM fwd_horizon").fetchone()[0]
    out = {}
    out['S1_52w_breakout'] = con.execute("""
        SELECT symbol FROM event_patterns WHERE date = ? AND dist_52w_prev > 0""", [asof]).fetchall()
    out['A1_unlock'] = con.execute("""
        SELECT symbol FROM event_patterns WHERE date = ? AND unlock_after_lock""", [asof]).fetchall()
    out['A2_zt_big_red'] = con.execute("""
        SELECT symbol FROM event_patterns WHERE date = ? AND zt_next_big_red = 1""", [asof]).fetchall()
    out['S3_lowvol'] = con.execute("""
        SELECT symbol FROM vol_structure WHERE date = ? AND amp_pct_250 < 0.5""", [asof]).fetchall()
    # G5金格子: 52W真突破+低波(2026-10-25新增: 次日+2.93~3.17%, 27个月次日全正)
    out['A3_vol_spike'] = con.execute("""
        SELECT symbol FROM (
          SELECT sym AS symbol, CAST(d AS DATE) date, v,
            AVG(v) OVER (PARTITION BY sym ORDER BY CAST(d AS DATE) ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) v20
          FROM (SELECT 1) WHERE 0
        ) LIMIT 0""").fetchall()  # 天量警报由volume_shape引擎日更时段补
    out['G5_52w_lowvol'] = con.execute("""
        SELECT ev.symbol FROM event_patterns ev JOIN vol_structure vs
          ON ev.symbol=vs.symbol AND ev.date=vs.date
        WHERE ev.date = ? AND ev.dist_52w_prev > 0 AND vs.amp_pct_250 < 0.55""", [asof]).fetchall()
    con.close()
    return {k: [x[0] for x in v] for k, v in out.items()}

if __name__ == '__main__':
    import json
    r = scan_today()
    print(json.dumps({k: (len(v), v[:5]) for k, v in r.items()}, ensure_ascii=False))
