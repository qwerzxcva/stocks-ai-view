#!/usr/bin/env python3
"""全市场逐票 walk-forward 训练器 v1
每票: 全历史, 每22交易日一个截面, 280日因子窗, 前500日IC等权训练(简化: 单截面rankIC无权重),
输出审计行: {sym, t0, picks_top10, fwd22_ret_median, mkt_fwd22}
逐票完成后写 audit/<sym>.json, 每50票由外层上传+清理。
"""
import duckdb, json, os, sys, time
import numpy as np
T1 = "/home/agentuser/stockdb/t1"
OUT = f"{T1}/audit"
os.makedirs(OUT, exist_ok=True)

FACTORS = ["ret5","ret20","ret60","vol20","amihud20","dist_ma60","dist_ma250","turn_z","volprc_corr","range20"]

def factors(df):
    # df: 按日期升序的 ndarray: d, c, o, h, l, v
    c, o, h, l, v = df[:,1], df[:,2], df[:,3], df[:,4], df[:,5]
    n = len(c)
    out = {}
    with np.errstate(all='ignore'):
        r = np.diff(np.log(np.maximum(c, 1e-9)), prepend=np.log(c[0]))
        out["ret5"] = np.full(n, np.nan); out["ret5"][5:] = c[5:]/c[:-5]-1
        out["ret20"] = np.full(n, np.nan); out["ret20"][20:] = c[20:]/c[:-20]-1
        vv = np.full(n, np.nan)
        for i in range(19, n): vv[i] = np.std(r[i-19:i+1])
        out["vol20"] = vv
        av = np.full(n, np.nan)
        dvr = np.abs(r) / np.maximum(v, 1e-9)
        for i in range(19, n): av[i] = np.mean(dvr[i-19:i+1])
        out["amihud20"] = av
        ma60 = np.full(n, np.nan); ma250 = np.full(n, np.nan)
        cs = np.cumsum(c); cs = np.concatenate([[0], cs])
        for i in range(59, n): ma60[i] = (cs[i+1]-cs[i-59])/60
        for i in range(249, n): ma250[i] = (cs[i+1]-cs[i-249])/250
        out["dist_ma60"] = c/np.maximum(ma60,1e-9)-1
        out["dist_ma250"] = c/np.maximum(ma250,1e-9)-1
        vma20 = np.full(n, np.nan)
        vs = np.cumsum(v); vs = np.concatenate([[0], vs])
        for i in range(19, n): vma20[i] = (vs[i+1]-vs[i-19])/20
        vsd = np.full(n, np.nan)
        for i in range(19, n):
            w = v[max(0,i-19):i+1]; vsd[i] = np.std(w) if len(w)>1 else np.nan
        out["turn_z"] = (v - vma20) / np.maximum(vsd, 1e-9)
    # v2 新因子: ret60 / 量价相关性 / 振幅
    out["ret60"] = np.full(n, np.nan); out["ret60"][60:] = c[60:]/c[:-60]-1
    rc = np.full(n, np.nan)
    for i in range(24, n):
        w_r = r[i-23:i+1]; w_v = v[i-23:i+1]
        if w_r.std() > 0 and w_v.std() > 0:
            rc[i] = np.corrcoef(w_r, w_v)[0,1]
    out["volprc_corr"] = rc
    rng = np.full(n, np.nan)
    for i in range(19, n):
        hh = h[i-19:i+1].max(); ll = l[i-19:i+1].min()
        rng[i] = (hh-ll)/np.maximum(ll,1e-9)
    out["range20"] = rng
    return out

def run_sym(con, sym, step=22):
    rows = con.execute("""
        select d, c, o, h, l, v from all_px where sym=?
        order by d""", [sym]).fetchall()
    if len(rows) < 800: return None
    import datetime
    df = np.array([[ (r[0] if isinstance(r[0], str) else r[0].isoformat()), r[1], r[2], r[3], r[4], r[5]] for r in rows], dtype=object)
    dates = [r[0] for r in rows]
    c = np.array([r[1] for r in rows], dtype=float)
    F = factors(np.array([[i, r[1], r[2], r[3], r[4], r[5]] for i, r in enumerate(rows)], dtype=float))
    idxs = list(range(780, len(rows)-22, step))
    if not idxs: return None
    audits = []
    for i in idxs:
        t0 = dates[i]
        # 训练: 过去500日内每60日截面算 rankIC, 权重=正IC占比
        w = {}
        train_idx = [j for j in range(max(60, i-500), i-22, 60)]
        for f in FACTORS:
            ics = []
            for j in train_idx:
                xs = F[f][j-60:j]
                ys = (c[j:j+60]/c[j])-1 if j+60 <= len(c) else None
                if ys is None or len(ys) < 20: continue
                x = xs[~np.isnan(xs)]; y = ys[~np.isnan(xs)]
                if len(x) < 20: continue
                rx = np.argsort(np.argsort(x)); ry = np.argsort(np.argsort(y))
                if rx.std() == 0 or ry.std() == 0: continue
                ics.append(np.corrcoef(rx, ry)[0,1])
            if ics:
                a = np.array(ics)
                w[f] = max(0.0, float((a>0).mean()))
        if sum(w.values()) < 0.5: continue
        # 打分截面: t0 当天用该票自身因子? 单票无法截面 → 用时序 z-score: 当前因子值在自身历史的位置
        xs = {f: F[f][max(0,i-280):i] for f in FACTORS}
        score = 0.0
        for f, wt in w.items():
            cur = F[f][i]
            hist = xs[f]; hist = hist[~np.isnan(hist)]
            if len(hist) < 50 or np.isnan(cur): continue
            mu, sd = hist.mean(), hist.std()
            if sd > 0: score += wt * (cur - mu) / sd
        fwd22 = (c[i+22]/c[i])-1 if i+22 < len(c) else None
        audits.append({"sym": sym, "t0": t0, "score": round(float(score),4),
                       "fwd22": round(float(fwd22),5) if fwd22 is not None else None,
                       "w": {k: round(v,3) for k,v in w.items()}})
    return audits

def main():
    con = duckdb.connect()
    from px_view import attach_px
    attach_px(con)
    con.execute("create table px_mat as select * from all_px")
    con.execute("drop view all_px")
    con.execute("create view all_px as select * from px_mat")
    syms = [r[0] for r in con.execute("select distinct sym from all_px order by sym").fetchall()]
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else len(syms)
    todo = syms[start:end]
    print(f"[all] total {len(syms)} syms, shard [{start}:{end}] = {len(todo)}", flush=True)
    t0 = time.time()
    done = 0
    for k, s in enumerate(todo):
        outp = f"{OUT}/{s}.json"
        if os.path.exists(outp):
            done += 1; continue
        try:
            a = run_sym(con, s)
            if a: json.dump(a, open(outp,"w"), ensure_ascii=False, default=str)
        except Exception as e:
            print(f"[err] {s}: {str(e)[:80]}", flush=True)
        done += 1
        if done % 50 == 0:
            print(f"[prog] {done}/{len(todo)} elapsed {time.time()-t0:.0f}s", flush=True)
    print(f"[shard done] [{start}:{end}] {done} syms {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
