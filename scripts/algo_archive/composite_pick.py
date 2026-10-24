"""composite_pick.py — 7因子等权合成选股 v3 (月末截面口径, 2026-10-17 诚实锚定)

诚实锚点 (月末截面 T+22 含成本): +8.4%/yr 净超额 (缓冲带版 +7.3%), 59 个月验证。
口径铁律: ①持仓池 = 最近已收盘月末的截面 (非最新日) ②nsmallest(低值做多) ③与 eom_audit.py 同口径。
因子 (均经 factor_audit PASS, 低值做多):
  wq_ts_rank_high_10 / mic_close_loc_10 / vp_vwap_mom_20 / a158_range_pos_20
  a158_amt_vwap_dev_20 / ma20_bias / gi_close_strength_10
"""
import duckdb, os, json, datetime

DB = os.environ.get("FM_DB", "/home/agentuser/stockdb/factor_matrix.duckdb")
F = ["wq_ts_rank_high_10", "mic_close_loc_10", "vp_vwap_mom_20", "a158_range_pos_20",
     "a158_amt_vwap_dev_20", "ma20_bias", "gi_close_strength_10"]

def _last_eom_date(c):
    """最近一个已完整的月末截面日 (当月未完则用上月末)"""
    today = datetime.date.today()
    first_of_month = today.replace(day=1)
    last_completed_eom = first_of_month - datetime.timedelta(days=1)
    r = c.execute("""
        SELECT max(CASE WHEN length(dstr)=8 THEN strptime(dstr,'%Y%m%d')
                        ELSE strptime(dstr,'%Y-%m-%d') END::DATE)
        FROM (SELECT CAST(date AS VARCHAR) AS dstr FROM factors_wide)
        WHERE (CASE WHEN length(dstr)=8 THEN strptime(dstr,'%Y%m%d')
                    ELSE strptime(dstr,'%Y-%m-%d') END::DATE) <= ?
    """, [last_completed_eom]).fetchone()
    return r[0]

def pick(top_n=295):
    c = duckdb.connect(DB, read_only=True)
    eom = _last_eom_date(c)
    cols = ", ".join(f'"{f}"' for f in F)
    df = c.execute(f"""
        SELECT symbol, {cols} FROM (
          SELECT *, CASE WHEN length(CAST(date AS VARCHAR))=8
                         THEN strptime(CAST(date AS VARCHAR),'%Y%m%d')::DATE
                         ELSE strptime(CAST(date AS VARCHAR),'%Y-%m-%d')::DATE END AS dnorm
          FROM factors_wide
        ) WHERE dnorm = ?""", [eom]).fetchdf()
    c.close()
    df = df.dropna()
    score = sum((df[f].rank(pct=True) - 0.5) for f in F) / len(F)
    df["score"] = score
    top = df.nsmallest(top_n, "score")
    return {"ts": str(eom), "algo": "composite_pick_v3_eom",
            "asof_section": str(eom),
            "picks": [{"symbol": r.symbol, "score": round(float(r.score), 4)} for r in top.itertuples()],
            "note": "7-factor composite v3 EOM-section (honest +8.4%/yr net), 持仓池按月末截面"}

if __name__ == "__main__":
    print(json.dumps(pick(), ensure_ascii=False)[:400])
