# ak_adapter.py — akshare 数据源适配层 (2026-10-24, 数据源大扩容: 已验证可用接口)
# 本机403域(push2.eastmoney)的接口不可用; 其余已逐个验证。统一缓存+重试。
import time, json, hashlib, os
import akshare as ak

CACHE = '/tmp/ak_cache'
os.makedirs(CACHE, exist_ok=True)
TTL = {'default': 1800, 'realtime': 120, 'daily': 21600}

def _cached(name, ttl_key='default'):
    def deco(fn):
        def wrapper(*args, **kw):
            key = hashlib.md5(f'{name}|{args}|{sorted(kw.items())}'.encode()).hexdigest()[:16]
            p = f'{CACHE}/{key}.json'
            now = time.time()
            if os.path.exists(p) and now - os.path.getmtime(p) < TTL.get(ttl_key, 1800):
                try: return json.load(open(p))
                except Exception: pass
            for a in range(3):
                try:
                    df = fn(*args, **kw)
                    recs = df.to_dict('records') if hasattr(df, 'to_dict') else df
                    json.dump(recs, open(p, 'w'), ensure_ascii=False, default=str)
                    return recs
                except Exception:
                    time.sleep(3)
            return []
        return wrapper
    return deco

@_cached('hsgt_north')
def north_flow():          # 北向资金
    return ak.stock_hsgt_fund_flow_summary_em()

@_cached('concept_flow')
def concept_flow():        # 同花顺概念资金流 387板块
    return ak.stock_fund_flow_concept()

@_cached('lxsz_rank')
def lianzhang_rank():      # 连涨排行
    return ak.stock_rank_lxsz_ths()

@_cached('qgqp')
def comment_all():         # 千股千评(机构参与度/综合评分 5198票)
    return ak.stock_comment_em()

@_cached('gdhs')
def gdhs():                # 股东户数(筹码集中度)
    return ak.stock_zh_a_gdhs()

@_cached('cctv_news')
def cctv_news():           # 新闻联播(政策信号)
    return ak.news_cctv()

@_cached('lhb_detail')
def lhb_detail():          # 龙虎榜详情(带解读)
    return ak.stock_lhb_detail_em()

@_cached('sentiment_legu')
def sentiment():           # 市场情绪(涨跌家数/活跃度)
    return ak.stock_market_activity_legu()

@_cached('fin_abstract', 'daily')
def fin_abstract(symbol):  # 财务摘要
    return ak.stock_financial_abstract(symbol=symbol)

@_cached('top10_float', 'daily')
def top10_holders(symbol): # 十大流通股东
    return ak.stock_gdfx_free_top_10_em(symbol=symbol)

def sentiment_snapshot():
    s = sentiment()
    return {r['item']: r['value'] for r in s} if s else {}
