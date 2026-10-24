# 行业映射 v1: 东财 f10 INDUSTRYCSRC1 按需查询 + 本地缓存 (2026-10-22)
import urllib.request, json, gzip, os
CACHE = '/home/agentuser/stockdb/t1/industry_cache.json'
H = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://emweb.securities.eastmoney.com/', 'Accept-Encoding': 'gzip'}

def _load_cache():
    try: return json.load(open(CACHE))
    except Exception: return {}

def _save_cache(c):
    json.dump(c, open(CACHE, 'w'), ensure_ascii=False)

def industry_of(sym):
    """sym: sh600900 → 证监会行业 (缓存优先)"""
    cache = _load_cache()
    if sym in cache: return cache[sym]
    code = sym[2:]
    prefix = 'SH' if sym.startswith('sh') else 'SZ'
    try:
        req = urllib.request.Request(
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={prefix}{code}", headers=H)
        d = json.loads(gzip.decompress(urllib.request.urlopen(req, timeout=10).read()).decode('utf-8'))
        ind = d['jbzl'][0].get('INDUSTRYCSRC1') or '未知'
        cache[sym] = ind
        _save_cache(cache)
        return ind
    except Exception as e:
        return f"查询失败: {str(e)[:30]}"

def industries_of_batch(syms):
    return {s: industry_of(s) for s in syms}

def sector_rotation_industry(days=20, sample_syms=None):
    """行业轮动: 推荐池/候选票按行业聚合近 N 日动量"""
    import duckdb
    con = duckdb.connect('/home/agentuser/stockdb/t1/qfq_all.duckdb', read_only=True)
    out = {}
    for s in (sample_syms or []):
        ind = industry_of(s)
        try:
            rows = con.execute(f"""select c from qfq_raw where sym='{s}' and try_cast(d as date) >=
              try_cast((select max(d) from qfq_raw) as date) - interval {days} day order by d""").fetchall()
            if len(rows) >= 10:
                ret = (rows[-1][0]/rows[0][0]-1)*100
                out.setdefault(ind.split('-')[-1], []).append((s, round(ret,1)))
        except Exception: continue
    con.close()
    return {k: {"avg": round(sum(r for _, r in v)/len(v),1), "n": len(v), "syms": v[:3]} for k, v in out.items()}
