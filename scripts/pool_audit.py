#!/usr/bin/env python3
"""pool_audit.py — 统一审计多池真实字段/时间/来源，给前端和预测包消费。"""
import json,os
from datetime import datetime,timezone,timedelta
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); DATA=os.path.join(ROOT,'data'); CST=timezone(timedelta(hours=8))
POOLS={
 'risk':('decision.json','avoid'), 'cycle':('cycle_pool_v2.json','regimes'),
 'recommend':('ai_pick_v4.json','picks'), 'hot':('hot_news.json','items'),
 'low':('lowpool.json','strong'), 'limitup':('ban_pick.json','picks'),
 'ticks':('auction_engine.json','stocks'), 'blocktrade':('enrich.json','blocktrade'),
 'lhb':('enrich.json','lhb'), 'auction':('enrich.json','jingjia'),
 'sector':('sector_pool.json','tier2'), 'money_divergence':('flow_divergence.json','buy_side'),
}
def load(f):
 try:return json.load(open(os.path.join(DATA,f),encoding='utf8'))
 except:return {}
def ts(d):
 if not isinstance(d,dict):return None
 for k in ('ts','timestamp','generated','date','data_date'):
  if isinstance(d.get(k),str):return d[k]
 return None
def main():
 now=datetime.now(CST); out={}; stale=[]
 for name,(fn,key) in POOLS.items():
  d=load(fn); v=d.get(key) if isinstance(d,dict) else None
  t=ts(d); age=None
  try:
   x=datetime.fromisoformat(str(t)[:25]); x=x if x.tzinfo else x.replace(tzinfo=CST); age=round((now-x).total_seconds()/3600,2)
  except:pass
  count=len(v) if isinstance(v,(list,dict)) else 0
  status='fresh' if age is not None and age<=8 else ('stale' if age is not None else 'no_timestamp')
  if status!='fresh':stale.append(name)
  out[name]={'file':fn,'field':key,'count':count,'ts':t,'age_hours':age,'status':status,'source':(d.get('source') or (d.get('meta') or {}).get('source')) if isinstance(d,dict) else None}
 payload={'ts':now.isoformat(timespec='seconds'),'engine':'pool_audit.py','status':'fresh' if not stale else 'partial','stale_pools':stale,'pools':out,'meta':{'purpose':'多池真实字段/时间/来源审计，防止前端读错字段后显示空池或旧池'}}
 json.dump(payload,open(os.path.join(DATA,'pool_audit.json'),'w'),ensure_ascii=False,indent=1);print('[pool_audit]',payload['status'],'stale=',stale)
if __name__=='__main__':main()
