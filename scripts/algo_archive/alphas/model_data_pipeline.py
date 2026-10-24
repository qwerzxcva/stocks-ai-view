# model_data_pipeline.py — 模型训练数据累积管道 (2026-10-24)
# 每日: ①EOD后把最新截面写入训练样本库(train_samples.parquet, 滚动) ②每周把样本增量推 GitHub stocks-algo/data/samples/
# 样本schema: sym, date, 因子117列(继承factor_matrix), fwd_ret_1d/5d/22d
import duckdb, os, json, datetime

DB = '/home/agentuser/stockdb/factor_matrix.duckdb'
OUT = '/home/agentuser/stockdb/t1/data/samples'
os.makedirs(OUT, exist_ok=True)

def append_daily(date=None):
    """追加最新交易日的因子截面到滚动样本库"""
    date = date or datetime.date.today().isoformat()
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='3GB'")
    con.execute(f"SET temp_directory='/tmp/ducktmp'")
    try:
        df = con.execute(f"""
          SELECT * FROM factor_matrix WHERE date = DATE '{date}'
        """).fetchdf()
    except Exception:
        df = None
    con.close()
    if df is None or len(df) == 0:
        return {'date': date, 'rows': 0, 'note': '当日无截面(未回补或非交易日)'}
    p = f'{OUT}/samples_{date}.parquet'
    df.to_parquet(p, index=False)
    return {'date': date, 'rows': len(df), 'cols': len(df.columns), 'file': p}

def weekly_push():
    """滚动打包推 GitHub (TOK3 仓 data/samples/)"""
    import base64, urllib.request
    TOK3 = open('/tmp/tok3.txt').read().strip()
    files = sorted(f for f in os.listdir(OUT) if f.endswith('.parquet'))[-5:]  # 最近5日
    ok = []
    for f in files:
        path = f'data/samples/{f}'
        url = f'https://api.github.com/repos/jiegengxiangjianxue1/stocks-algo/contents/{path}'
        body = {'message': f'train samples {f}', 'content': base64.b64encode(open(f"{OUT}/{f}", 'rb').read()).decode()}
        for a in range(4):
            try:
                try: body['sha'] = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': f'token {TOK3}'}), timeout=30))['sha']
                except Exception: pass
                urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Authorization': f'token {TOK3}', 'Content-Type': 'application/json'}, method='PUT'), timeout=120)
                ok.append(f); break
            except Exception:
                import time; time.sleep(5)
    return ok

if __name__ == '__main__':
    import sys
    r = append_daily(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(r, ensure_ascii=False))
