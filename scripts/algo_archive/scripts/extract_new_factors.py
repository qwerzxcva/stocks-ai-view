#!/usr/bin/env python3
"""补 4 缺口中的 2.5 个: ②半变差波动率 ③情绪(涨停/连板/振幅温度) — 主库日线直接算, 零新数据"""
import duckdb, numpy as np, json, sys
def main(limit=None):
    con = duckdb.connect()
    con.execute("attach '/home/agentuser/stockdb/t1/qfq_all.duckdb' as t1 (read_only)")
    q = "select distinct sym from t1.qfq"
    if limit: q += f" limit {limit}"
    syms = [r[0] for r in con.execute(q).fetchall()]
    out = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
    out.execute("""create table if not exists vol_sent_factors(
        symbol varchar, as_of date,
        rv_up_20 double, rv_down_20 double, rv_ratio_20 double,
        ret_kurt_60 double,
        zt_count_60 int, zt_recent_10 int, amp_mean_20 double, amp_last5 double)""")
    done = 0
    for s in syms:
        try:
            rows = con.execute("select d, c, h, l from t1.qfq where sym=? order by d", [s]).fetchall()
            if len(rows) < 120: continue
            c = np.array([r[1] for r in rows], float)
            h = np.array([r[2] for r in rows], float)
            l = np.array([r[3] for r in rows], float)
            ret = np.diff(np.log(np.maximum(c, 1e-9)))
            r = ret[-80:]
            up = np.clip(r, 0, None)**2; dn = np.clip(-r, 0, None)**2
            rv_up, rv_dn = float(np.sqrt(up[-20:].mean()*252)), float(np.sqrt(dn[-20:].mean()*252))
            k60 = float(((r[-60:]-r[-60:].mean())**4).mean()/max((r[-60:].std()**4),1e-12))
            # 涨停近似: 日涨幅 > 9.7% (非创业板 10% 限制, 粗近似)
            zt60 = int((ret[-60:] > 0.095).sum()); zt10 = int((ret[-10:] > 0.095).sum())
            amp20 = float(((h[-20:]-l[-20:])/np.maximum(l[-20:],1e-9)).mean())
            amp5 = float(((h[-5:]-l[-5:])/np.maximum(l[-5:],1e-9)).mean())
            out.execute("delete from vol_sent_factors where symbol=?", [s])
            out.execute("insert into vol_sent_factors values (?,?,?,?,?,?,?,?,?,?)",
                [s, str(rows[-1][0]), rv_up, rv_dn, rv_up/max(rv_dn,1e-9), k60, zt60, zt10, amp20, amp5])
            done += 1
        except Exception:
            pass
        if done % 500 == 0 and done: print(f"{done}", flush=True)
    out.execute("checkpoint"); out.close()
    print(f"done {done}/{len(syms)}")
if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv)>1 else None)
