# factor_full_daily.py — 从 qfq_all 全量重算日频因子并 append 到 factors_wide (2026-10-24)
# 解决: append_factors_daily 只写11列, 其余104列在最新截面全NaN → lgb_infer 同值失效
import duckdb, json, math
import numpy as np
import pandas as pd

FA = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
FM = '/home/agentuser/stockdb/factor_matrix.duckdb'

def main():
    con = duckdb.connect(FA, read_only=True)
    con.execute("SET memory_limit='3GB'")
    con.execute("SET temp_directory='/tmp/ducktmp'")
    # 最近30交易日的全量K线 → pandas算因子
    df = con.execute("""
      SELECT sym, CAST(d AS DATE) AS date, c, o, h, l, v
      FROM qfq WHERE CAST(d AS DATE) >= (SELECT max(CAST(d AS DATE)) - INTERVAL 60 DAY FROM qfq) ORDER BY sym, date
    """).fetchdf()
    con.close()
    print('kline rows:', len(df), 'syms:', df['sym'].nunique())

    df['ret_1d'] = df.groupby('sym')['c'].pct_change()
    g = df.groupby('sym')
    out = pd.DataFrame({'sym': df['sym'], 'date': df['date']})
    out['ret_1d'] = df['ret_1d']
    for n in (5, 10, 20, 60):
        out[f'momentum_{n}d'] = g['c'].pct_change(n)
    out['vol_20d'] = g['ret_1d'].transform(lambda s: s.rolling(20).std())
    hi20 = g['c'].transform(lambda s: s.rolling(20).max())
    lo20 = g['c'].transform(lambda s: s.rolling(20).min())
    out['close_position'] = np.where((hi20 - lo20) > 0, (df['c'] - lo20) / (hi20 - lo20), 0.5)
    out['gap_return'] = df['o'] / g['c'].shift(1) - 1
    out['intraday_return'] = df['c'] / df['o'] - 1
    out['amplitude'] = (df['h'] - df['l']) / g['c'].shift(1)
    out['hl_spread'] = (df['h'] - df['l']) / df['c']
    for n in (10, 20, 60):
        out[f'a158_close_ma_ratio_{n}'] = df['c'] / g['c'].transform(lambda s: s.rolling(n).mean())
        out[f'a158_vol_ma_ratio_{n}'] = df['v'] / g['v'].transform(lambda s: s.rolling(n).mean())
        out[f'a158_range_pos_{n}'] = np.where((hi20 if n == 20 else g['c'].transform(lambda s: s.rolling(n).max())) -
                                              g['c'].transform(lambda s: s.rolling(n).min()) > 0,
                                              (df['c'] - g['c'].transform(lambda s: s.rolling(n).min())) /
                                              (g['c'].transform(lambda s: s.rolling(n).max()) - g['c'].transform(lambda s: s.rolling(n).min())), 0.5)
        out[f'a158_ret_std_{n}'] = g['ret_1d'].transform(lambda s: s.rolling(n).std())
    out['dist_high_60d'] = df['c'] / g['c'].transform(lambda s: s.rolling(60).max()) - 1
    # GK波动
    log_hl = np.log(df['h'] / df['l']) ** 2
    log_co = np.log(df['c'] / df['o']) ** 2
    gk = (0.5 * log_hl - (2 * math.log(2) - 1) * log_co)
    out['vs_gk_20'] = gk.groupby(df['sym']).transform(lambda s: s.rolling(20).mean())
    out['mic_volume_cv_20'] = g['v'].transform(lambda s: s.rolling(20).std() / s.rolling(20).mean())
    df['amp'] = (df['h'] - df['l']) / df.groupby('sym')['c'].shift(1)
    out['vs_range_vol_20'] = df.groupby('sym')['amp'].transform(lambda s: s.rolling(20).mean())
    # ATR
    tr_ = pd.concat([(df['h'] - df['l']), (df['h'] - g['c'].shift(1)).abs(), (df['l'] - g['c'].shift(1)).abs()], axis=1).max(axis=1)
    atr14 = tr_.groupby(df['sym']).transform(lambda s: s.rolling(14).mean())
    out['vs_atr_norm_14'] = tr_ / (atr14 * 14 / 14)
    df['ret_clip'] = df['ret_1d'].clip(upper=0)
    out['vs_downside_dev_20'] = df.groupby('sym')['ret_clip'].transform(lambda s: s.rolling(20).std())
    out['vs_vol_trend_20_60'] = g['v'].transform(lambda s: s.rolling(20).mean()) / g['v'].transform(lambda s: s.rolling(60).mean())
    out['a158_kshape'] = out['ret_1d'] / (out['amplitude'] + 1e-9)

    # 只 append 目标日(最后一天), 对齐 factors_wide 列
    last_date = out['date'].max()
    day = out[out['date'] == last_date]
    con2 = duckdb.connect(FM)
    con2.execute("SET memory_limit='3GB'")
    cols = [c[0] for c in con2.execute("DESCRIBE factors_wide").fetchall()]
    have = [c for c in day.columns if c in cols and c not in ('sym', 'date')]
    day = day.rename(columns={'sym': 'symbol'})
    day2 = day[['symbol', 'date'] + [c for c in have if c != 'sym']].copy()
    for c in cols:
        if c not in day2.columns:
            day2[c] = None
    day2 = day2[cols]
    # 删除已有该日行再插入(幂等)
    con2.execute(f"DELETE FROM factors_wide WHERE date = DATE '{last_date}'")
    con2.execute("INSERT INTO factors_wide SELECT * FROM day2")
    n = con2.execute(f"SELECT count(*) FROM factors_wide WHERE date = DATE '{last_date}'").fetchone()[0]
    con2.close()
    print(f'appended {last_date}: {n} rows, cols filled: {len(have)}')

if __name__ == '__main__':
    main()
