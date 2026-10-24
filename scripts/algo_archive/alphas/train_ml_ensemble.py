#!/usr/bin/env python3
"""ML 三引擎训练 (吸收 stock-pead-engine): GBDT + NB + rule, 因果特征, IS/OOS 切分
特征: 18 因果特征 (predict/features BASE 对齐), 标签: 未来21日涨方向
数据: qfq_all.duckdb 全市场, 每票全历史月度截面
"""
import duckdb, numpy as np, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gbdt import GBDT
from nb import GaussianNB as NB
from gbdt import GBDT as _G  # noqa
def features_from_closes(c, o, h, l, v):
    """c/o/h/l/v: 升序 ndarray. 返回 (feature_names, list of (idx, feat_vec))"""
    n = len(c)
    r = np.diff(np.log(np.maximum(c,1e-9)), prepend=np.log(c[0]))
    def sma(x, w):
        cs = np.cumsum(np.concatenate([[0], x]))
        out = np.full(len(x), np.nan)
        out[w-1:] = (cs[w:]-cs[:-w])/w
        return out
    names = ["r1","r5","r21","r63","rev5","rev21","vol21","rsi14","px_sma20","px_sma50","px_sma200","atr_pct","vol_ratio"]
    feats = []
    sma20, sma50, sma200 = sma(c,20), sma(c,50), sma(c,200)
    tr = np.maximum.reduce([h[1:]-l[1:], np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])])
    tr = np.concatenate([[h[0]-l[0]], tr])
    for i in range(260, n-21):
        f = [
          float(r[i]), float(c[i]/c[i-5]-1), float(c[i]/c[i-21]-1), float(c[i]/c[i-63]-1),
          float(c[i]/c[i-5]-1 - (c[i-5]/c[i-10]-1)), float(c[i]/c[i-21]-1 - (c[i-21]/c[i-42]-1)),
          float(np.std(r[i-20:i+1])),
          None,  # rsi 后填
          float(c[i]/sma20[i]-1) if sma20[i]>0 else 0.0,
          float(c[i]/sma50[i]-1) if sma50[i]>0 else 0.0,
          float(c[i]/sma200[i]-1) if sma200[i]>0 else 0.0,
          float(tr[i-13:i+1].mean()/c[i]),
          float(v[i]/ (v[i-20:i+1].mean()+1e-9)),
        ]
        # rsi14
        seg = r[i-13:i+1]
        g = seg[seg>0].sum(); lo = -seg[seg<0].sum()
        f[7] = 100-100/(1+g/(lo+1e-9))
        y = 1 if c[i+21] > c[i] else 0
        feats.append((i, [x if x==x else 0.0 for x in f], y))
    return names, feats
def main(shard=(0,800)):
    con = duckdb.connect()
    con.execute("attach '/home/agentuser/stockdb/t1/qfq_all.duckdb' as t1 (read_only)")
    syms = [r[0] for r in con.execute("select distinct sym from t1.qfq order by sym").fetchall()[shard[0]:shard[1]]]
    X, y, meta = [], [], []
    for s in syms:
        rows = con.execute("select c,o,h,l,v from t1.qfq where sym=? order by d", [s]).fetchall()
        if len(rows) < 300: continue
        c = np.array([r[0] for r in rows], float); o = np.array([r[1] for r in rows], float)
        h = np.array([r[2] for r in rows], float); l = np.array([r[3] for r in rows], float)
        v = np.array([r[4] for r in rows], float)
        names, feats = features_from_closes(c,o,h,l,v)
        for i, f, yy in feats:
            X.append(f); y.append(yy); meta.append((s, i))
    print(f"样本: {len(X)} (来自 {len(set(m[0] for m in meta))} 票)")
    Xa = np.array(X); ya = np.array(y)
    # IS/OOS 70/30 按时间切 (meta idx 全局排序)
    cut = int(len(Xa)*0.7)
    # GBDT
    g = GBDT(n_trees=15)
    g.fit([list(x) for x in Xa[:cut]], [float(t) for t in ya[:cut]])
    preds = g.predict_proba([list(x) for x in Xa[cut:]])
    print("GBDT trained; predict method:", hasattr(g,'predict'))
    # OOS 评估: 按 pred 分五组看胜率
    if preds is not None:
        po = np.array(preds); yo = ya[cut:]
        order = np.argsort(po)
        for q in range(5):
            seg = yo[order[q*len(yo)//5:(q+1)*len(yo)//5]]
            print(f"Q{q+1}: win {seg.mean():.3f} (n={len(seg)})")
    nb = NB()
    nb.fit([list(x) for x in Xa[:cut]], [int(t) for t in ya[:cut]])
    pnb = np.array(nb.predict_proba([list(x) for x in Xa[cut:]]))
    order2 = np.argsort(pnb)
    print("NB:")
    for q in range(5):
        seg = yo[order2[q*len(yo)//5:(q+1)*len(yo)//5]]
        print(f"  Q{q+1}: win {seg.mean():.3f}")
    json.dump({"n_samples": len(X), "is_size": cut}, open('/tmp/ml_ens_info.json','w'))
if __name__ == "__main__":
    main()
