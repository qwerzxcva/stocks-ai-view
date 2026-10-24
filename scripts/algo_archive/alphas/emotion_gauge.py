# emotion_gauge.py — 市场情绪仪表 (2026-10-23)
# 涨停家数/炸板率/连板高度/跌停数 → 情绪温度计: 冰点(<30涨停)/回暖/亢奋(>100,连板>=7)
# 数据: 同花顺涨停池(fetch_day), 下跌家数用宽度表
import sys, json
sys.path.insert(0, '/home/agentuser/stockdb/t1')
from fetch_zt_ths import fetch_day

def gauge(yyyymmdd):
    rows = fetch_day(yyyymmdd)
    zt = len(rows)
    lianban_max = max((r.get('lianban') or 1) for r in rows) if rows else 0
    zb = sum(1 for r in rows if (r.get('zhaban') or 0) > 0)
    # 炸板率近似: 破板的不再池里(池里的是封住的), 用 open_num>0 占比近似"开过板比例"
    temp = min(100, zt * 1.2 + lianban_max * 4)
    mood = '冰点' if zt < 30 else ('回暖' if zt < 60 else ('活跃' if zt < 100 else '亢奋'))
    return {'date': yyyymmdd, 'zt': zt, 'lianban_max': lianban_max, 'opened_board_pct': round(zb/zt*100, 0) if zt else 0,
            'temp': round(temp), 'mood': mood,
            'note': '涨停数+连板高度双因子; 冰点→反转观察期, 亢奋→接力警惕'}

if __name__ == '__main__':
    import datetime
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y%m%d')
    print(json.dumps(gauge(d), ensure_ascii=False))
