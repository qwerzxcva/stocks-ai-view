# 公告利好利空扫描 (新浪公告页, 2026-10-22)
import urllib.request, re
from news_mystic import news_sentiment  # 复用关键词引擎

def scan_announcements(sym_code="600900", max_n=5):
    """拉个股最近公告标题 → 利好/利空评分"""
    H = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletin.php?stockid={sym_code}"
    try:
        d = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=10).read().decode('gbk', 'ignore')
        # 公告标题在 <a target='_blank' ...>标题</a>
        titles = re.findall(r'>([^<>]{10,60}(?:公告|年报|季报|预案|增持|回购)[^<>]{0,10})<', d)
        out = []
        for t in titles[:max_n]:
            verdict, strength = news_sentiment(t)
            out.append({"title": t.strip(), "verdict": verdict, "strength": strength})
        return out
    except Exception as e:
        return [{"error": str(e)[:50]}]
