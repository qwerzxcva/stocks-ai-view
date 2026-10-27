# lgb_infer.py — LGBM 明日预测在线推理 (2026-10-27 v2)
# 每日: 对全市场最新截面打分 → TOP 概率票 + 全市场分布 → 喂 orchestrator/push pipeline
# v2: 三模型 (nextday / strongup / p4hit=非涨停池触4%专项 OOS AUC 0.7065)
import duckdb, pickle, json, numpy as np, datetime

DB_Q = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
MODEL = '/home/agentuser/stockdb/t1/data/lgb_nextday.pkl'

def latest_features(date=None):
    """factors_wide 最新截面"""
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb', read_only=True)
    con.execute("SET memory_limit='3GB'")
    con.execute("SET temp_directory='/tmp/ducktmp'")
    d = pickle.load(open(MODEL, 'rb'))['features']
    collist = ','.join(f'"{f}"' for f in d)
    df = con.execute(f"""
      SELECT symbol AS sym, date, {collist} FROM factors_wide
      WHERE date = (SELECT max(date) FROM factors_wide)
    """).fetchdf()
    con.close()
    return df

def infer(date=None):
    """三模型: nextday(明日涨) + strongup(强涨专项) + p4hit(非涨停触4%专项, 执行口径=尾盘买/次日冲高4%止盈-2%止损)"""
    import pandas as pd
    df = latest_features(date)
    if len(df) == 0: return {'note': '无当日数据'}
    out = {'date': str(df['date'].iloc[0]), 'n': len(df)}
    for key, path in [('nextday', MODEL),
                      ('strongup', '/home/agentuser/stockdb/t1/data/lgb_strongup.pkl'),
                      ('p4hit', '/home/agentuser/stockdb/t1/data/lgb_p4hit.pkl')]:
        try:
            d = pickle.load(open(path, 'rb'))
            m, feats = d['model'], d['features']
            Xdf = df[feats].copy().fillna(df[feats].median())
            prob = m.predict_proba(Xdf.values.astype('float32'))[:, 1]
            df[f'p_{key}'] = prob
            k = max(10, len(df)//100)
            top = df.nlargest(k, f'p_{key}')[['sym', f'p_{key}']]
            out[key] = {'p_mean': round(float(prob.mean()), 4),
                        'top1pct': [{'sym': r.sym, 'p': round(float(getattr(r, f'p_{key}')), 3)} for r in top.itertuples()]}
        except Exception as e:
            out[key] = {'err': str(e)[:60]}
    # p4hit 非涨停专项: 排除昨日涨幅>5%后的 TOP (用户口径: 不要涨停票)
    try:
        con = duckdb.connect(DB_Q, read_only=True)
        con.execute("SET memory_limit='3GB'")
        rets = con.execute("""
          WITH px AS (SELECT sym, CAST(d AS DATE) AS dd, c FROM qfq WHERE (sym LIKE 'sh60%' OR sym LIKE 'sz00%'))
          SELECT sym, c/lag(c) OVER (PARTITION BY sym ORDER BY dd) - 1 AS ret
          FROM px WHERE dd = (SELECT max(dd) FROM px)
        """).fetchdf()
        con.close()
        calm = rets[rets.ret <= 0.05].sym.tolist()
        sub = df[df.sym.isin(calm)].nlargest(20, 'p_p4hit')
        out['p4hit_calm_top'] = [{'sym': r.sym, 'p': round(float(r.p_p4hit), 3)} for r in sub.itertuples()]
        s1 = {x['sym'] for x in out['nextday']['top1pct']}
        s2 = {x['sym'] for x in out['strongup']['top1pct']}
        out['consensus'] = sorted(s1 & s2)
    except Exception as e:
        out['p4hit_calm_top'] = {'err': str(e)[:60]}
    return out

if __name__ == '__main__':
    print(json.dumps(infer(), ensure_ascii=False, indent=1)[:1500])
