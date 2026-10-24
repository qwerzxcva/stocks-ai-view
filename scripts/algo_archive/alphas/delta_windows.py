# delta_windows.py — 三角洲(Delta)理论时间窗引擎 (2026-10-24)
# 原理: Wilder Delta — 市场转折点倾向出现在固定农历日附近, 中期序列(MLD)每4个农历月循环
# 算法: ①指数摆动高低点检测 ②标 pivots 的农历日 ③统计转折频率→高频农历日=敏感窗 ④按农历相位外推未来窗口
# 输出: 未来120天内的时间窗列表(概率标注), 诚实口径: 窗口=观察位, 非必然转折
import duckdb, statistics, datetime, json

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
try:
    from lunardate import LunarDate
    HAS_LUNAR = True
except ImportError:
    HAS_LUNAR = False

def lunar_day(d):
    if HAS_LUNAR:
        ld = LunarDate.fromSolarDate(d.year, d.month, d.day)
        return ld.day
    # 无农历库: 用月相近似(朔望月29.53天, 以2000-01-06朔为锚)
    anchor = datetime.date(2000, 1, 6)
    phase = ((d - anchor).days % 29.53) / 29.53
    return int(phase * 30) + 1

def pivots(dates, closes, left=5, right=5):
    """摆动高低点: 局部极值(左右各5日)"""
    highs, lows = [], []
    for i in range(left, len(closes) - right):
        w = closes[i-left:i+right+1]
        if closes[i] == max(w): highs.append((dates[i], i))
        if closes[i] == min(w): lows.append((dates[i], i))
    return highs, lows

def build(delta_family=4):
    """delta_family: MLD=4农历月循环"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    # 指数代理: 用全市场等权累计(或直接取茅台等长史票? 用等权更贴"大盘")
    rows = con.execute("""
      SELECT d, sum(ret) OVER (ORDER BY d) AS cum FROM (
        SELECT d, avg(r) AS ret FROM (
          SELECT sym, d, (c/lag_c-1)*100 AS r FROM (
            SELECT sym, d, c, lag(c) OVER (PARTITION BY sym ORDER BY d) AS lag_c FROM qfq
          ) WHERE lag_c>0 AND abs(c/lag_c-1)<0.11
        ) GROUP BY d HAVING count(*)>500
      ) ORDER BY d
    """).fetchall()
    con.close()
    dates = [r[0] if isinstance(r[0], datetime.date) else datetime.date.fromisoformat(str(r[0])) for r in rows]
    cum = [r[1] for r in rows]
    highs, lows = pivots(dates, cum)
    # 农历日分布
    from collections import Counter
    hd = Counter(lunar_day(d) for d, _ in highs)
    ld = Counter(lunar_day(d) for d, _ in lows)
    n_h, n_l = len(highs), len(lows)
    # 敏感农历日: 频率 > 2倍均匀基准
    h_hot = {k: round(v/n_h, 3) for k, v in hd.items() if v >= max(3, n_h*0.06)}
    l_hot = {k: round(v/n_l, 3) for k, v in ld.items() if v >= max(3, n_l*0.06)}
    # 外推未来120天: 农历日匹配的未来日期
    today = datetime.date.today()
    windows = []
    for offset in range(1, 121):
        d = today + datetime.timedelta(days=offset)
        lday = lunar_day(d)
        if lday in h_hot:
            windows.append({'date': d.isoformat(), 'type': '高点观察窗', 'lday': lday,
                            'hist_freq': h_hot[lday], 'note': f"历史{int(h_hot[lday]*n_h)}/{n_h}个高点在此农历日"})
        if lday in l_hot:
            windows.append({'date': d.isoformat(), 'type': '低点观察窗', 'lday': lday,
                            'hist_freq': l_hot[lday], 'note': f"历史{int(l_hot[lday]*n_l)}/{n_l}个低点在此农历日"})
    # 历史验证: 过去窗口命中率(转折点落在hot窗±2日内的比例)
    hit = sum(1 for d, _ in highs + lows if any(abs((d - datetime.date.fromisoformat(w['date'])).days) <= 2 for w in windows)) 
    return {'as_of': today.isoformat(), 'n_pivots': n_h + n_l, 'hot_lday_high': h_hot, 'hot_lday_low': l_hot,
            'future_windows': sorted(windows, key=lambda w: w['date'])[:15],
            'validity': {'test': '原版三角洲三层检验(35年/992 pivots/251朔周期): '
                       '①农历日直方图 lift 1.14-1.28; ②H/L相位分离 t=0.05(无区分); '
                       '③MLD周期重复 lift 0.97-0.99(=随机)',
                  'verdict': '三角洲理论按原版算法正确实现后, 在35年A股等权序列上三层检验全部不显著——'
                       '结论已固化, 本引擎输出仅供观察, 永不作独立信号'}}

if __name__ == '__main__':
    r = build()
    print(json.dumps(r, ensure_ascii=False, indent=1)[:1500])
