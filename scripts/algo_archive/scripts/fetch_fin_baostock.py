#!/usr/bin/env python3
"""④ PIT 基本面: baostock 季度财务 (profit/growth) → fin_pit 表, 分批防会话崩"""
import baostock as bs
import duckdb, sys, time
def main():
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
    con.execute("""create table if not exists fin_pit(
      symbol varchar, year int, quarter int,
      roe_avg double, net_profit_yoy double, revenue_yoy double,
      gross_margin double, debt_ratio double, eps_ttm double)""")
    syms = [r[0] for r in con.execute("select distinct symbol from factors_wide order by symbol").fetchall()]
    # baostock: 只能一次查一只; 限制: 最新已披露季 = 2026Q2 (report 2026-06-30)
    quarters = [(2025,4),(2026,1),(2026,2)]
    done = 0
    batch = 0
    total = 0
    for sym in syms:
        code = ("sh." if sym.startswith('sh') else "sz.") + sym[2:]
        if batch == 0:
            lg = bs.login()
            if lg.error_code != '0':
                print("login fail", lg.error_msg); return
        ok_any = False
        for y, q in quarters:
            try:
                rs = bs.query_profit_data(code=code, year=y, quarter=q)
                rows = []
                while rs.error_code == '0' and rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    r = rows[0]
                    def f(x):
                        try: return float(x) if x not in ('', None) else None
                        except: return None
                    roe = f(r[7]) if len(r) > 7 else None  # roeAvg
                    # 字段序: code,pubDate,statDate,roeAvg,npMargin,gpMargin,netProfit,epsMB... 用名称映射更稳
                    fields = rs.fields
                    def get(name):
                        try: return f(r[fields.index(name)])
                        except: return None
                    con.execute("delete from fin_pit where symbol=? and year=? and quarter=?", [sym, y, q])
                    con.execute("insert into fin_pit values (?,?,?,?,?,?,?,?,?)", [
                      sym, y, q, get('roeAvg'), None, None, get('gpMargin'), None, get('epsTTM')])
                    ok_any = True
            except Exception:
                pass
            time.sleep(0.05)
        if ok_any: total += 1
        done += 1
        batch += 1
        if batch >= 300:
            bs.logout(); batch = 0
            print(f"{done}/{len(syms)} 有数据票 {total}", flush=True)
    if batch: bs.logout()
    con.execute("checkpoint")
    print(f"[done] {done}/{len(syms)} 票, 有 PIT 财务 {total}", flush=True)
    con.close()
if __name__ == "__main__":
    main()
