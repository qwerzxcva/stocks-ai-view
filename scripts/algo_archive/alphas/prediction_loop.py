# prediction_loop.py — 预测→次日实际→准确率闭环 (夜班建, 长期积累)
import duckdb, json, datetime, sys
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
DB = '/home/agentuser/stockdb/knowledge.duckdb'

def make_prediction():
    from daily_market_pred import predict
    r = predict()
    con = duckdb.connect(DB)
    today = datetime.date.today()
    # 覆盖今日(重跑幂等)
    con.execute("DELETE FROM predictions WHERE p_date = ? AND horizon = 'next1'", [today])
    con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?)",
        (today, 'next1', r['prediction_next1_pct']/100, r['direction'],
         json.dumps(r['evidence'], ensure_ascii=False)[:400], None, None))
    con.close()
    print('预测已存:', today, r['prediction_next1_pct'], r['direction'])

def evaluate():
    """用昨日实际收盘评估未评估预测"""
    import urllib.request, json as J
    con = duckdb.connect(DB)
    rows = con.execute("SELECT p_date, predicted, direction FROM predictions WHERE actual IS NULL AND p_date < current_date").fetchall()
    if not rows:
        con.close(); print('无待评估'); return
    d = J.loads(urllib.request.urlopen(urllib.request.Request(
        'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,300,qfq',
        headers={'User-Agent': 'Mozilla/5.0'}), timeout=15).read())
    k = {x[0]: float(x[2]) for x in d['data']['sh000001']['day']}
    dates = sorted(k)
    hit = tot = 0
    for pd_, pred, direction in rows:
        ds = str(pd_)
        prior = [x for x in dates if x <= ds]
        nxt = [x for x in dates if x > ds]
        if not prior or not nxt: continue
        actual = k[nxt[0]]/k[prior[-1]] - 1
        con.execute("UPDATE predictions SET actual = ?, eval_date = ? WHERE p_date = ? AND horizon = 'next1'",
                    [actual, nxt[0], pd_])
        ok = (direction == 'up') == (actual > 0)
        hit += ok; tot += 1
    con.close()
    if tot: print(f'评估{tot}条, 方向命中{hit} ({hit/tot*100:.0f}%)')

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'both'
    if mode in ('predict', 'both'): make_prediction()
    if mode in ('eval', 'both'): evaluate()
