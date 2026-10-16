#!/usr/bin/env python3
"""overseas_impact.py — 从海外关联结果生成A股受益/受害候选。"""
import json,os
from datetime import datetime,timezone,timedelta
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); DATA=os.path.join(ROOT,'data'); CST=timezone(timedelta(hours=8))
def load(n):
 try:return json.load(open(os.path.join(DATA,n),encoding='utf8'))
 except:return {}
def main():
 now=datetime.now(CST); link=load('overseas_linkage.json'); latest=load('latest.json'); names=load('stock_names.json'); im=load('industry_map_em.json').get('map',{})
 pairs=link.get('pairs',[]); hot=(latest.get('ths_strong') or [])+(latest.get('limit_up_pool') or [])
 rows=[]
 for p in pairs:
  corr=p.get('corr') or 0; beta=p.get('beta') or 0; oc=p.get('overnight_corr') or 0
  if abs(corr)<0.2 and abs(oc)<0.15:continue
  effect='受益' if corr>0 and oc>=0 else '受害' if corr<0 or oc<0 else '混合'
  rows.append({'target':p.get('target'),'target_name':p.get('target_name'),'driver':p.get('driver'),'driver_name':p.get('driver_name'),'effect':effect,'corr':corr,'beta':beta,'r2':p.get('r2'),'lag':p.get('best_lag'),'overnight_corr':oc,'sample_n':p.get('n')})
 rows.sort(key=lambda x:-(abs(x.get('overnight_corr') or 0)+abs(x.get('corr') or 0)))
 payload={'ts':now.isoformat(timespec='seconds'),'engine':'overseas_impact.py','source_ts':link.get('ts'),'n_pairs':len(rows),'benefit': [r for r in rows if r['effect']=='受益'][:30],'harm': [r for r in rows if r['effect']=='受害'][:30],'mixed':[r for r in rows if r['effect']=='混合'][:20],'meta':{'purpose':'将海外关联corr/beta/lag/overnight转成受益/受害候选，非因果结论','caveat':'海外关联不等于因果；样本量、滞后和方向均需结合A股实时板块/个股证据'}}
 json.dump(payload,open(os.path.join(DATA,'overseas_impact.json'),'w'),ensure_ascii=False,indent=1);print('[overseas_impact]',len(rows))
if __name__=='__main__':main()
