# 缺口引擎包 1: 市场结构类 (β/反向/量价细分/主力预测) — 2026-10-22
import duckdb
import math

CON_PATH = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def beta_rank(top_n=30):
    """易受大盘影响的票 (β 高) 与反向票 (β 负) — 180 日回归
    返回 {'high_beta': [(sym,beta,corr)...], 'inverse_beta': [...]}"""
    con = duckdb.connect(CON_PATH, read_only=True)
    con.execute("SET memory_limit='4GB'; SET temp_directory='/tmp/ducktmp'")
    rows = con.execute("""
    with pr as (
      select sym, d, c/lag(c) over (partition by sym order by d)-1 as ret from qfq_raw
    ), mkt as (select d, avg(ret) as mkt_ret from pr group by d having count(*) > 500),
    j as (
      select pr.sym, pr.ret as s_ret, mkt.mkt_ret
      from pr join mkt on pr.d = mkt.d
      where try_cast(pr.d as date) >= try_cast((select max(d) from mkt) as date) - interval 180 day
    )
    select sym, count(*) as n,
      sum(s_ret*mkt_ret)/count(*) as sm,
      avg(s_ret) as ms, avg(mkt_ret) as mm,
      sum(s_ret*s_ret)/count(*) as ss, sum(mkt_ret*mkt_ret)/count(*) as mt
    from j where s_ret is not null
    group by sym having count(*) >= 80""").fetchall()
    con.close()
    out = []
    for sym, n, sm, ms, mm, ss, mt in rows:
        cov = sm - ms*mm
        var = mt - mm*mm
        beta = cov/var if var > 1e-12 else 0.0
        var_s = ss - ms*ms
        corr = cov/math.sqrt(var_s*var) if var_s > 1e-12 and var > 1e-12 else 0.0
        out.append((sym, beta, corr))
    out.sort(key=lambda x: -x[1])
    hi = [(s, round(b,2), round(c,2)) for s,b,c in out[:top_n] if b > 1.2 and c > 0.3]
    inv = [(s, round(b,2), round(c,2)) for s,b,c in out if b < -0.05 and c < -0.1][:top_n]
    return {"high_beta": hi, "inverse_beta": inv}

def flow_price_divergence_full(mf_con='/home/agentuser/stockdb/factor_matrix.duckdb'):
    """量价资金四象限: 微涨大流入(吸筹)/大涨大流入(主升)/微涨大流出(派发)/跌后大流出(出清)
    基于 mf_factors + 主库 20 日收益"""
    con_mf = duckdb.connect(mf_con, read_only=True)
    r = con_mf.execute("""
    select symbol, mf_net_sum_20 from mf_factors
    where mf_net_sum_20 is not null""").fetchall()
    con_mf.close()
    con = duckdb.connect(CON_PATH, read_only=True)
    out = {}
    for sym, net20 in r:
        try:
            rows = con.execute(f"""select d, c from qfq_raw where sym='{sym}'
              order by d desc limit 21""").fetchall()
            if len(rows) < 21: continue
            ret20 = rows[0][1]/rows[-1][1]-1
            if net20 > 3e8 and ret20 < 0.03:
                out.setdefault('微涨大流入_吸筹', []).append((sym, round(net20/1e8,1), round(ret20*100,1)))
            elif net20 > 5e8 and ret20 > 0.15:
                out.setdefault('大涨大流入_主升', []).append((sym, round(net20/1e8,1), round(ret20*100,1)))
            elif net20 < -3e8 and ret20 > 0.05:
                out.setdefault('微涨大流出_派发', []).append((sym, round(net20/1e8,1), round(ret20*100,1)))
            elif net20 < -3e8 and ret20 < -0.1:
                out.setdefault('大跌大流出_出清观察', []).append((sym, round(net20/1e8,1), round(ret20*100,1)))
        except Exception:
            continue
    con.close()
    return out
