# news_pulse.py — 盘中消息向扫描 (2026-10-23)
# 每30min抓 7x24 快讯(东财/新浪), 关键词映射题材 → 与 sector_watch 异动交叉 = 消息驱动的板块异动
import urllib.request, json, re, time

KEYMAP = [('机器人','机器人'), ('算力','算力/AI'), ('AI','算力/AI'), ('芯片','半导体'), ('存储','半导体'),
          ('固态电池','电池'), ('锂','电池'), ('低空','低空经济'), ('军工','军工'), ('地产','地产'),
          ('降准','货币宽松'), ('降息','货币宽松'), ('券商','券商'), ('并购','并购重组'), ('央企','中特估')]

def sinajs_7x24(n=20):
    try:
        d = urllib.request.urlopen(urllib.request.Request(
            f'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={n}&zhibo_id=152&tag_id=0',
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}), timeout=10).read()
        j = json.loads(d)
        items = j.get('result', {}).get('data', {}).get('feed', {}).get('list', [])
        out = []
        for it in items:
            txt = re.sub(r'<[^>]+>', '', it.get('rich_text', ''))
            out.append({'t': it.get('create_time', ''), 'text': txt[:100]})
        return out
    except Exception as e:
        return [{'t': '', 'text': f'fail: {str(e)[:40]}'}]

def themes(news):
    hits = {}
    for it in news:
        for kw, theme in KEYMAP:
            if kw in it['text']:
                hits.setdefault(theme, []).append(it['text'][:60])
    return hits

if __name__ == '__main__':
    news = sinajs_7x24()
    th = themes(news)
    print(f"快讯 {len(news)} 条, 涉及题材:")
    for t, items in th.items():
        print(f"  【{t}】x{len(items)}: {items[0]}")
    json.dump({'news': news[:20], 'themes': {k: v[:3] for k, v in th.items()}},
              open('/tmp/news_pulse.json', 'w'), ensure_ascii=False)
