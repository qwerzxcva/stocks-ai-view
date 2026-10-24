# lgb_infer.py — LGBM 明日预测在线推理 (2026-10-24)
# 每日: 对全市场最新截面打分 → TOP 概率票 + 全市场分布 → 喂 orchestrator/push pipeline
import duckdb, pickle, json, numpy as np, datetime

DB_Q = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
MODEL = '/home/agentuser/stockdb/t1/data/lgb_mainboard_v3.pkl'

def latest_features(date=None):
    """factors_wide 最新截面 (115/115 特征全匹配)"""
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
    '''双模型融合: lgb_nextday(明日涨) + lgb_strongup(强涨3%+专项, TOP1%精度37%)'''
    import pandas as pd
    df = latest_features(date)
    if len(df) == 0: return {'note': '无当日数据'}
    out = {'date': str(df['date'].iloc[0]), 'n': len(df)}
    for key, path in [('nextday', MODEL), ('strongup', '/home/agentuser/stockdb/t1/data/lgb_strongup.pkl')]:
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
    # 双模型共振: 两个TOP1%名单交集
    try:
        s1 = {x['sym'] for x in out['nextday']['top1pct']}
        s2 = {x['sym'] for x in out['strongup']['top1pct']}
        out['consensus'] = sorted(s1 & s2)
    except Exception: pass
    return out

if __name__ == '__main__':
    print(json.dumps(infer(), ensure_ascii=False, indent=1)[:1200])
