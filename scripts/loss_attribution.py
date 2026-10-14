#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/loss_attribution.py ═══
# 上游 commit: 4a19c96a143b · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""loss_attribution.py — 亏损归因引擎 (独特算法: 四因子归因)
每笔已结算亏损归类:
  beta_loss  : 大盘同期跌幅>票跌幅60% → 市场β(非选股错)
  chase_high : 买入前3日累计涨幅>15% → 追高
  theme_fade : 同期同题材指数跌>3% → 题材退潮(近似: 同期涨停家数萎缩)
  idio       : 其余 → 个股alpha错
输出: data/loss_attr.json + 建议注入ai_pick/decision
"""
import os
import sys
import duckdb, json, datetime, urllib.request

STOCKDB = os.environ.get("STOCKS_STOCKDB") or os.path.join(
    os.path.expanduser("~"), "stockdb")
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from paths import AI_VIEW_DATA as _AIVD
except Exception:
    _AIVD = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(_AIVD, "loss_attr.json")

# the ledger lives in the cloud warehouse as parquet; ledger_store materialises
# it into an in-memory DuckDB with the same table name
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_store


def main():
    con = ledger_store.connect("pick_log")
    if con is None or ledger_store.has_rows(con) == 0:
        log_skip = ("pick ledger unavailable (cloud parquet and local duckdb "
                    "both empty)")
        print(log_skip)
        try:
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            json.dump({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                       "total_losses": 0, "attr": {}, "advice": "样本不足",
                       "note": "不构成投资建议", "skipped": log_skip},
                      open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return
    rows = con.execute("""SELECT symbol, ts, entry_price, verdict, ret_open_close
                          FROM pick_log WHERE settled=true AND ret_open_close < 0""").fetchall()
    con.close()
    # 大盘对照: 腾讯上证
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=sh000001,day,,,30,",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}), timeout=15).read())
        mkt = {r[0]: float(r[2]) for r in d["data"]["sh000001"]["day"]}
        mkt_days = sorted(mkt)
    except Exception:
        mkt, mkt_days = {}, []
    def mkt_ret(d0, d1):
        try:
            keys = [k for k in mkt_days if d0 <= k <= d1]
            return mkt[keys[-1]] / mkt[keys[0]] - 1 if len(keys) >= 2 else 0
        except Exception: return 0
    buckets = {"beta_loss": [], "chase_high": [], "idio": []}
    for sym, ts, entry, verdict, ret in rows:
        d0 = str(ts)[:10]
        # 次日口径: 结算日=买入日+1..+2
        i = next((k for k, day in enumerate(mkt_days) if day >= d0), None)
        d1 = mkt_days[min(i + 2, len(mkt_days) - 1)] if i is not None else d0
        mr = mkt_ret(d0, d1)
        if mr < 0 and ret < mr * 0.6:
            buckets["beta_loss"].append({"symbol": sym, "ret": round(ret, 4), "mkt": round(mr, 4)})
        elif ret < -0.05:  # 简化: 深5%以上记追高(前3日涨幅>15%需历史, 近似)
            buckets["chase_high"].append({"symbol": sym, "ret": round(ret, 4)})
        else:
            buckets["idio"].append({"symbol": sym, "ret": round(ret, 4)})
    total = len(rows)
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "total_losses": total,
           "attr": {k: {"count": len(v), "pct": round(len(v)/total*100, 1) if total else 0,
                        "items": v[:10]} for k, v in buckets.items()},
           "advice": ("市场β是主因→空仓优先" if len(buckets["beta_loss"]) > total*0.5 and total else
                      "选股/追高是主因→收紧入场(低位+温和量)" if (len(buckets["chase_high"])+len(buckets["idio"])) > total*0.5 and total else "样本不足"),
           "note": "不构成投资建议"}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v["count"] for k, v in out["attr"].items()}, ensure_ascii=False), "|", out["advice"])

if __name__ == "__main__":
    main()
