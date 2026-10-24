#!/usr/bin/env python3
"""从 stocks-holo moneyflow.json 提取资金流因子 (1001 沪市票)"""
import json, urllib.request, base64, os, sys, time
import numpy as np
TOK = open('/tmp/gctok.txt').read().strip() if os.path.exists('/tmp/gctok.txt') else None
def get_json(sym, name):
    if TOK:
        try:
            req = urllib.request.Request(f"https://gitcode.com/api/v5/repos/dufuwuxin/stocks-holo/contents/{sym}/{name}.json?access_token={TOK}")
            return json.loads(base64.b64decode(json.load(urllib.request.urlopen(req, timeout=30))["content"]))
        except Exception: return None
def factors_from_mf(d):
    rows = d.get('data') or []
    if len(rows) < 20: return None
    net = np.array([r.get('net_in_wan') or 0 for r in rows[:20]], dtype=float)
    r0 = np.array([r.get('r0_net_wan') or 0 for r in rows[:20]], dtype=float)
    tot = np.abs(net).sum()
    return {
      "mf_net_sum_20": float(net.sum()),
      "mf_net_mean_20": float(net.mean()),
      "mf_main_ratio_20": float(np.abs(r0).sum()/tot) if tot>0 else None,
      "mf_net_std_20": float(net.std()),
      "mf_last5_net": float(net[:5].sum()),
    }
if __name__ == "__main__":
    syms = [l.strip() for l in sys.stdin if l.strip()]
    out = {}
    for i, s in enumerate(syms):
        d = get_json(s, "moneyflow")
        f = factors_from_mf(d) if d else None
        if f: out[s] = f
        if (i+1) % 100 == 0: print(f"{i+1}/{len(syms)} got={len(out)}", flush=True); time.sleep(1)
    json.dump(out, open(sys.argv[1] if len(sys.argv)>1 else '/tmp/mf_factors.json','w'), ensure_ascii=False, default=str)
    print(f"done: {len(out)}/{len(syms)}")
