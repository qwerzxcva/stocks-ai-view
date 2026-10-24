# 缺口引擎包 2: 闭环跟踪 + 牛熊周期 + 热点/行业 — 2026-10-22
import duckdb, json, time
from datetime import date

TRACK_LOG = '/home/agentuser/stockdb/t1/pick_tracker.jsonl'
CON_PATH = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

# ── 1. 推荐-跟踪闭环 (涨跌跟踪/数据跟踪) ──
def track_pick(sym, name, reason, rec_price):
    """推荐时登记 → T+1/T+5 自动回查"""
    with open(TRACK_LOG, 'a') as f:
        f.write(json.dumps({"ts": time.time(), "date": str(date.today()), "sym": sym,
                            "name": name, "reason": reason, "rec_price": rec_price,
                            "status": "open"}, ensure_ascii=False) + "\n")

def track_check():
    """回查 open 推荐的 T+1/T+5 表现 → 更新引擎权重依据"""
    con = duckdb.connect(CON_PATH, read_only=True)
    results = []
    try:
        lines = open(TRACK_LOG).read().strip().split('\n')
    except FileNotFoundError:
        return []
    for ln in lines:
        try: rec = json.loads(ln)
        except: continue
        if rec.get('status') != 'open': continue
        sym, rp = rec['sym'], rec['rec_price']
        rows = con.execute(f"""select d, c from qfq_raw where sym='{sym}' and d > '2026-10-20'
          order by d""").fetchall()
        if rows:
            latest = rows[-1]
            ret = (latest[1]/rp-1)*100
            days = len(rows)
            rec['tracked'] = {"days": days, "ret_pct": round(ret,2),
                              "verdict": "win" if ret > 0.5 else ("lose" if ret < -1 else "flat")}
            results.append(rec)
    con.close()
    return results

# ── 2. 牛熊周期判定 (独立引擎, 长周期) ──
def bull_bear_phase():
    """牛熊判定: 上证 250 日均线 + 全市场中位票位置 + 连板高度趋势
    返回 (phase, strength, evidence)"""
    con = duckdb.connect(CON_PATH, read_only=True)
    con.execute("SET memory_limit='4GB'; SET temp_directory='/tmp/ducktmp'")
    r = con.execute("""
    with pr as (
      select sym, d, c, c/lag(c) over (partition by sym order by d)-1 as ret
      from qfq_raw where try_cast(d as date) > try_cast((select max(d) from qfq_raw) as date) - interval 400 day
    ), mkt as (
      select d, avg(ret) as ret, avg(c) as avg_c
      from pr group by d order by d
    )
    select d, ret, avg_c,
      avg(avg_c) over (order by d rows between 249 preceding and current row) as ma250
    from mkt order by d desc limit 5""").fetchall()
    con.close()
    if not r or r[0][3] is None:
        return ("unknown", 0, "数据不足")
    d, ret, avg_c, ma250 = r[0]
    above = avg_c > ma250
    strength = (avg_c/ma250 - 1) * 100
    phase = "bull_early" if above and strength < 10 else ("bull_late" if above else "bear")
    # 连板高度趋势辅助
    ev = f"价格{'在' if above else '低于'}250日均线 {strength:+.1f}%, 等权代理"
    return (phase, round(strength,2), ev)

# ── 3. 行业/板块轮动 (简版: 代码段聚合) ──
def sector_rotation(days=20):
    """近 N 日各板块动量排名 (代码段近似行业, 后续接 ths 行业映射)"""
    con = duckdb.connect(CON_PATH, read_only=True)
    con.execute("SET memory_limit='4GB'; SET temp_directory='/tmp/ducktmp'")
    r = con.execute(f"""
    with pr as (
      select sym, d, c from qfq_raw
      where try_cast(d as date) >= try_cast((select max(d) from qfq_raw) as date) - interval {days+30} day
    ), seg as (
      select sym, d, c,
        case when sym like 'sh688%' then '科创'
             when sym like 'sz30%' then '创业板'
             when sym like 'sh60%' then '沪主板'
             else '深主板' end as sector
      from pr
    ), first_last as (
      select sector, sym,
        max(case when d = (select max(d) from pr) then c end) as c_now,
        min(c) as c_first
      from seg group by sector, sym
    )
    select sector, count(*) as n, avg(c_now/c_first-1)*100 as seg_ret
    from first_last where c_first > 0 and c_now is not null
    group by sector order by seg_ret desc""").fetchall()
    con.close()
    return [(s, n, round(a,2)) for s, n, a in r]
