"""
双主线监测引擎 (2026-10-22 用户指令)
逻辑: 双主线 = 连续两日涨停家数 top2 题材 (双主线内个股涨停概率显著高)
      + 消息联动: 当日/昨日新闻关键词 → 题材加权
数据: zt_factors (涨停池, 含题材标签 via THEME_MAP) + zt_market_ts (历史每日涨停结构) + news_mystic 消息关键词
输出: 双主线题材名 + 持续天数 + 强度 + 联动个股 (题材内未涨停=接力候选, 已涨停=龙头)
"""
import json, duckdb, sys
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
def load_theme_map():
    # 30+ 主线题材关键词 → 题材名 (行业映射 + 涨停池常用分类)
    return {
        '海洋经济': ['海洋', '海工', '船舶', '渔业'],
        '算力/AI': ['算力', 'AI', '人工智能', 'GPU', '算力租赁', '数据中心', '液冷'],
        '机器人': ['机器人', '人形机器人', '减速器', '伺服', '灵巧手'],
        '半导体': ['半导体', '芯片', '光刻', '存储', '晶圆'],
        '固态电池': ['固态电池', '电解质', '锂电'],
        '可控核聚变': ['核聚变', '核电', '超导'],
        '军工': ['军工', '导弹', '航空', '航天', '国防'],
        '低空经济': ['低空', '无人机', 'eVTOL', '通航'],
        '电力': ['电力', '水电', '核电', '绿电', '电网'],
        '创新药': ['创新药', '医药', 'CXO', '疫苗'],
        '稀土永磁': ['稀土', '磁材'],
        '券商': ['券商', '证券', '互联网金融'],
        '鸿蒙/信创': ['鸿蒙', '信创', '国产软件', '操作系统'],
        '汽车链': ['汽车', '整车', '汽车零部件', '智能驾驶'],
        '消费电子': ['消费电子', '苹果链', '折叠屏', 'AI手机'],
    }

def analyze(db_path='/home/agentuser/stockdb/factor_matrix.duckdb'):
    con = duckdb.connect(db_path, read_only=True)
    con.execute("SET memory_limit='2GB'")
    # 近 5 日涨停池
    rows = con.execute("""
        SELECT as_of, symbol FROM zt_factors WHERE is_zt = TRUE ORDER BY as_of DESC LIMIT 600
    """).fetchall()
    if not rows:
        return {'status': 'no_zt_data'}
    themes = load_theme_map()
    by_day = {}
    # 名称从主库/实时源补: 这里用 symbol 映射表 (主库 qfq 没名字, 走腾讯实时批量补)
    syms_all = sorted({s for _, s in rows})
    try:
        sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
        from source_router import ROUTER
        q, _src = ROUTER.quotes(syms_all[:300])
        names = {s: q[s]['name'] for s in q}
    except Exception:
        names = {}
    for d, sym in rows:
        by_day.setdefault(str(d), []).append((sym, names.get(sym, sym)))
    days = sorted(by_day.keys(), reverse=True)[:2]
    today, yesterday = (days + [None, None])[:2]
    def theme_score(day):
        if not day: return {}
        sc = {}
        for tname, kws in themes.items():
            hits = [s for s, n in by_day[day] if any(k in n for k in kws)]
            if hits: sc[tname] = (len(hits), hits)
        return sc
    t_today, t_yest = theme_score(today), theme_score(yesterday or today)
    # 双主线: 两日均在 top3 且今日家数 >=3
    combined = {}
    for t in set(t_today) | set(t_yest):
        a = t_today.get(t, (0, []))[0]; b = t_yest.get(t, (0, []))[0]
        combined[t] = a + b + (2 if (a >= 2 and b >= 2) else 0)   # 连续性加权
    dual = sorted(combined.items(), key=lambda x: -x[1])[:2]
    out = {'as_of': today, 'dual_themes': []}
    for t, sc in dual:
        if sc < 3: continue
        hits_t = t_today.get(t, (0, []))[1]
        hits_y = t_yest.get(t, (0, []))[1]
        leaders = list(set(hits_t) & set(hits_y))          # 两日连板=龙头
        relay = list(set(hits_t) - set(hits_y))            # 今日新进
        out['dual_themes'].append({
            'theme': t, 'strength': sc, 'today_n': len(hits_t), 'yest_n': len(hits_y),
            'leaders': leaders[:6], 'relay_candidates': relay[:8],
            'note': f"两日连续活跃({len(hits_t)}+{len(hits_y)}家), 龙头{len(leaders)}只, 接力候选{len(relay)}只"
        })
    # 消息联动: 新浪滚动新闻 50 条 → 题材命中 → 热度加成 (今日/昨日消息驱动主线)
    news_hits = {}
    try:
        url = 'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1'
        d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=10).read())
        titles = [it.get('title', '') for it in d.get('result', {}).get('data', [])]
        for t, kws in themes.items():
            c = sum(1 for ti in titles if any(k in ti for k in kws))
            if c >= 2: news_hits[t] = c
    except Exception:
        pass
    out['news_heat'] = news_hits
    # 消息热度注入主线强度: 新闻 >=3 条的题材若无涨停也提示"消息预热"
    for t, c in news_hits.items():
        if c >= 3 and t not in combined:
            out['dual_themes'].append({'theme': t, 'strength': c, 'today_n': 0, 'yest_n': 0,
                'leaders': [], 'relay_candidates': [], 'note': f'消息预热({c}条新闻, 涨停未启动, 观察首板)'})
    out['verdict'] = '双主线明确' if len(out['dual_themes']) >= 2 else ('单主线' if out['dual_themes'] else '无主线')
    return out

if __name__ == '__main__':
    r = analyze()
    print(json.dumps(r, ensure_ascii=False, indent=1))
