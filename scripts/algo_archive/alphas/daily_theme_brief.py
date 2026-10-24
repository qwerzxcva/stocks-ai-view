# daily_theme_brief.py — 每日涨停题材情报 (15:35 EOD)
# 源: 同花顺涨停池 reason_type (涨停原因聚类) → 主题材/新题材识别 → 写 /tmp/theme_brief_YYYYMMDD.json
# 次日: sector_burst.scan_live + morning_judge 消费此情报
import sys, json, re, time
from collections import Counter
sys.path.insert(0, '/home/agentuser/stockdb/t1')
from fetch_zt_ths import fetch_day, H
import urllib.request

def fetch_with_reason(yyyymmdd):
    url = ('https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?page=1&limit=100'
           '&field=199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004'
           f'&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={yyyymmdd}')
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=15).read())
    return (d.get('data') or {}).get('info') or []

STOP = ['首板','天','板','业绩增长','拟收购','增持','减持','复牌','三季报','半年报','年报']
NORM = [('机器人','机器人'), ('算力','算力/AI'), ('AI','算力/AI'), ('智能','算力/AI'),
        ('芯片','半导体'), ('半导体','半导体'), ('存储','半导体'), ('集成电路','半导体'),
        ('地产','地产'), ('房地产','地产'), ('电池','电池'), ('锂','电池'), ('固态','电池'),
        ('数据','数据要素'), ('信创','信创'), ('低空','低空经济'), ('军工','军工'), ('证券','券商'), ('金融','券商')]
def normalize(kw):
    for sub, big in NORM:
        if sub in kw: return big
    return kw
def brief(yyyymmdd):
    info = fetch_with_reason(yyyymmdd)
    themes = Counter(); stocks = {}
    for r in info:
        reason = str(r.get('reason_type') or '')
        seen = set()
        for kw in re.split(r'[+、,，]', reason):
            kw = normalize(kw.strip())
            if len(kw) >= 2 and not any(s in kw for s in STOP) and kw not in seen:
                seen.add(kw)
                themes[kw] += 1
                stocks.setdefault(kw, []).append({'code': r.get('code'), 'name': r.get('name'),
                                                  'lianban': str(r.get('high_days'))[:6]})
    main = [{'theme': t, 'count': n, 'stocks': stocks[t][:6]} for t, n in themes.most_common(10) if n >= 2]
    out = {'date': yyyymmdd, 'zt_total': len(info), 'themes': main}
    json.dump(out, open(f'/tmp/theme_brief_{yyyymmdd}.json', 'w'), ensure_ascii=False, indent=1)
    return out

if __name__ == '__main__':
    d = brief(sys.argv[1] if len(sys.argv) > 1 else '20261023')
    print(f"涨停 {d['zt_total']} 只 · 主线:")
    for t in d['themes'][:6]:
        print(f"  {t['theme']} x{t['count']}: {[s['name'] for s in t['stocks'][:4]]}")
