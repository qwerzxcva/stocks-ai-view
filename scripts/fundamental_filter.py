#!/usr/bin/env python3
"""fundamental_filter.py — 基本面过滤层（批次四）

先说结论，别绕：**本项目现在做不了真正的基本面筛选。**

实测数据：
  financial_summary 只有 108 只股票、2 个指标（每股净资产、销售毛利率）
  announcements 0 行、theme_lifecycle 0 行、events 102 行
  → 用这种覆盖度做"财报掘金""价值投资"式筛选，等于用 14% 的样本给全市场排序，
    排出来的结果比随机还危险，因为看起来有依据。

所以本模块的设计原则是：
  1. 有数据就老老实实算（PB、毛利率），并标 coverage
  2. 覆盖不足就返回 None + 原因，让下游**不能**假装做过基本面过滤
  3. 把「若能补齐数据该算什么」写成 RULES，供数据补齐后直接接入

补齐数据后可直接用的判据（来自 batch4 卡片）：
  ROE 连续 3 年 > 12% 且不下滑；经营现金流/净利润 > 0.8；
  应收账款增速 < 营收增速；商誉/净资产 < 20%；毛利率稳定（3 年标准差 < 3pct）；
  PB 分位数 < 40%（近 5 年）；净利润同比 > 0 且加速度为正。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MIN_COVERAGE = 0.60   # 覆盖率低于 60% 直接判定"不可用"

# 数据补齐后按此表接入（现在留空实现，故意不假装能用）
RULES = {
    "roe_trend": "ROE 连续 3 年 >12% 且不下滑",
    "cash_quality": "经营现金流/净利润 > 0.8",
    "receivable": "应收账款增速 < 营收增速",
    "goodwill": "商誉/净资产 < 20%",
    "margin_stable": "毛利率 3 年标准差 < 3pct",
    "pb_percentile": "PB 5 年分位 < 40%",
    "profit_accel": "净利润同比 >0 且加速度为正",
}


def coverage():
    """统计基本面数据覆盖度。返回可用性与原因，供下游决定是否允许出结论。"""
    try:
        from kline_source import duck, list_codes
        c = duck()
        n_rows = c.execute("select count(*) from financial_summary").fetchone()[0]
        n_codes = c.execute("select count(distinct code) from financial_summary").fetchone()[0]
        metrics = [r[0] for r in c.execute(
            "select distinct metric_name from financial_summary").fetchall()]
        total = len(list_codes())
    except Exception as e:
        return {"available": False, "reason": f"查询失败: {str(e)[:80]}"}
    ratio = n_codes / total if total else 0
    ok = ratio >= MIN_COVERAGE and len(metrics) >= 4
    return {"available": ok, "rows": n_rows, "codes": n_codes, "total_codes": total,
            "coverage_pct": round(ratio * 100, 1), "metrics": metrics,
            "reason": ("覆盖 {:.1f}%（{} / {}），指标 {} 个 → 不足以做全市场基本面排序"
                       .format(ratio * 100, n_codes, total, len(metrics))) if not ok else "可用"}


def view(code):
    """单只股票可算的基本面字段。缺就 None，不填默认值。"""
    bare = code.replace("sh", "").replace("sz", "").replace("bj", "")
    try:
        from kline_source import duck
        rows = duck().execute(
            "select metric_name, value from financial_summary where code=?", [bare]
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    d = {r[0]: r[1] for r in rows}
    out = {"code": code, "coverage": "partial", "fields": {}}
    bps = d.get("每股净资产")
    if bps:
        try:
            out["fields"]["bps"] = round(float(bps), 3)
        except ValueError:
            pass
    gm = d.get("销售毛利率")
    if gm:
        try:
            out["fields"]["gross_margin_pct"] = round(float(gm), 2)
        except ValueError:
            pass
    if not out["fields"]:
        return None
    out["missing"] = [k for k in RULES if k not in out["fields"]]
    return out


def pb(code, price):
    """PB = 价格 / 每股净资产。缺数据返回 None。"""
    v = view(code)
    if not v or "bps" not in v["fields"] or not v["fields"]["bps"]:
        return None
    return round(price / v["fields"]["bps"], 2)


def gate(code, price=None):
    """把基本面当作闸门用时的返回。

    pass / warn / None 三态：
      None  = 没有足够数据（下游必须当作"未检查"，不能当作"通过"）
      warn  = 有部分数据但不足以支持结论
      pass  = 数据齐且判据通过（当前数据量下几乎不会出现）
    """
    cov = coverage()
    if not cov["available"]:
        return {"status": None, "reason": cov["reason"],
                "note": "基本面未检查；不得在报告中写成'基本面良好'"}
    v = view(code)
    if not v:
        return {"status": None, "reason": "该股无基本面记录"}
    return {"status": "warn", "data": v,
            "reason": "仅每股净资产/毛利率两项，无法做质量与成长判断"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("--price", type=float)
    a = ap.parse_args()
    if a.code:
        print(json.dumps(gate(a.code, a.price), ensure_ascii=False, indent=1))
        if a.price:
            print("PB =", pb(a.code, a.price))
        return
    print(json.dumps(coverage(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
