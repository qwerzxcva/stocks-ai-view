#!/usr/bin/env python3
"""limit_structure.py — 涨停结构层（书 #15《股市趋势交易大师：龙头作手》
/#29《铁血短线》《铁血战记》/#31《擒庄秘籍》的可落地部分）

## 为什么单独一层

那几本书的核心是「打板 / 连板高度 / 龙头识别」，判据高度依赖**封单量、封板时间、
龙虎榜席位、筹码分布**——这些数据本项目都没有（`pools` 表为 0 行、无 tick、无 L2）。
但这不代表什么都做不了：

**能算准的**（raw 日线足够）：
- 涨停判定（按板块涨跌幅上限）
- 连板高度（连续涨停天数）→ 龙头高度
- 封板强度近似（收盘价 = 当日最高价 → 收在板上；否则视为盘中打开后回落）
- 断板事件（昨日涨停、今日未涨停）
- 涨停量能（相对 20 日均量的倍数）

**算不了的**（必须如实标注，不许假装）：
- 封单金额 / 封单占流通比（要 L2 逐笔）
- 首次封板时间 / 打开次数（要分钟数据）
- 龙虎榜席位（要榜单数据，`events` 表只有 102 行且非此用途）
- 筹码分布 / 主力净买（要逐笔或股东数据）

因此本层输出的标签一律带 `limit:` 前缀且 docstring 声明近似，
`known_limits()` 返回不可用项清单供上游展示，避免下游误以为拿到的是完整打板判据。

## 用 raw 而不是 qfq

`kline_source.load_bars()` 优先读 qfq CSV。**涨跌幅判定必须用 raw**：
除权日的 qfq 序列被人为抹平，会算出假的涨停/跌停。本模块自取 raw 序列。

## 用法
    python3 scripts/limit_structure.py --code sh600017 -v
    python3 scripts/limit_structure.py --validate --sample 200
    python3 scripts/limit_structure.py --calibrate --sample 200
    python3 scripts/limit_structure.py --all
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kline_source import duck, list_codes  # noqa: E402
import signal_validator as SV  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or os.path.join(os.path.dirname(HERE), "data")
CST = timezone(timedelta(hours=8))

RECENT_WIN = 10        # 只输出最近 10 根内的涨停事件
VOL_MA_N = 20
MIN_BARS = 80

# 理论上偏多的标签（打板/连板属于多头动能叙事，方向仍由实测决定）
BULLISH_TAGS = (
    "limit:first_board",
    "limit:board2",
    "limit:board3",
    "limit:board4p",
    "limit:strong_seal",
)


def limit_pct(code):
    """按板块返回涨停幅度阈值（留 0.2% 容差，避免浮点与四舍五入误判）。"""
    c = code.lower()
    if c.startswith(("sh688", "sz300", "sz301")):
        return 19.8      # 科创板 / 创业板 20%
    if c.startswith(("bj", "sh8", "sz8", "sh4", "sz4")):
        return 29.8      # 北交所 30%
    return 9.8           # 主板 10%（ST 5% 无法从代码判断，见 known_limits）


def load_raw(code, limit=400):
    """raw 日线（不复权），升序。涨停判定只能用它。"""
    rows = duck().execute(
        "select date, open, close, high, low, volume from kline_daily "
        "where code=? and adj='raw' order by date desc limit ?",
        [code, limit],
    ).fetchall()
    rows.reverse()
    return [
        {"date": str(r[0]), "open": float(r[1]), "close": float(r[2]),
         "high": float(r[3]), "low": float(r[4]), "volume": float(r[5] or 0)}
        for r in rows
    ]


def vol_ma(bars, n=VOL_MA_N):
    return sum(b["volume"] for b in bars[-n:]) / n if len(bars) >= n else None


def limit_events(bars, code):
    """扫描全序列，返回每个涨停日的连板高度与封板强度近似。"""
    thr = limit_pct(code)
    events = []
    streak = 0
    # 前缀和算量比：前向校准会对同一序列调分析几十次，O(n²) 的均量会把整轮跑成十几分钟
    cum = [0.0]
    for b in bars:
        cum.append(cum[-1] + b["volume"])

    def vma(i, n=VOL_MA_N):
        if i + 1 < n:
            return None
        return (cum[i + 1] - cum[i + 1 - n]) / n

    for i in range(1, len(bars)):
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        if not prev:
            continue
        chg = (cur / prev - 1.0) * 100.0
        is_limit = chg >= thr
        if is_limit:
            streak += 1
            v = vma(i)
            events.append({
                "idx": i,
                "date": bars[i]["date"],
                "boards": streak,
                "chg": round(chg, 2),
                # 收在最高价 → 收盘时仍在板上；否则盘中打开过（日线只能看收盘，属近似）
                "strong_seal": abs(bars[i]["close"] - bars[i]["high"]) < 1e-6,
                "vol_ratio": round(bars[i]["volume"] / v, 2) if v else None,
                "upper_shadow_pct": round((bars[i]["high"] - bars[i]["close"]) / prev * 100.0, 2),
            })
        else:
            if streak > 0:
                events.append({
                    "idx": i, "date": bars[i]["date"], "boards": 0,
                    "chg": round(chg, 2), "broken": True, "prev_streak": streak,
                })
            streak = 0
    return events


def known_limits():
    """不可用项：上游要如实展示，别让用户以为封单/龙虎榜被算过了。"""
    return {
        "seal_amount": "需 L2 逐笔封单量，本项目无 tick",
        "first_seal_time": "需分钟数据，intraday_events 表为空",
        "open_count": "需分钟数据，只能由收盘是否等于最高价近似",
        "lhb_seats": "需龙虎榜榜单，events 表仅 102 行且非此用途",
        "chip_distribution": "需股东/逐笔数据",
        "st_stock": "ST 涨跌幅 5% 无法从代码判断，可能把 ST 涨停误判为普通涨幅",
    }


def analyze(bars, code=""):
    if not bars or len(bars) < MIN_BARS:
        return None
    ev = limit_events(bars, code)
    n = len(bars)
    recent = [e for e in ev if e["idx"] >= n - RECENT_WIN]
    tags = []
    detail = {"events": recent[-6:], "limit_pct": limit_pct(code)}

    # 今日状态
    last = ev[-1] if ev else None
    today_limit = bool(last and last["idx"] == n - 1 and not last.get("broken"))
    if today_limit:
        b = last["boards"]
        if b == 1:
            tags.append("limit:first_board")
        elif b == 2:
            tags.append("limit:board2")
        elif b == 3:
            tags.append("limit:board3")
        else:
            tags.append("limit:board4p")
        if last.get("strong_seal"):
            tags.append("limit:strong_seal")
        vr = last.get("vol_ratio")
        if vr is not None and vr >= 2.0:
            tags.append("limit:vol_surge")

    # 断板：最近 3 根内出现「昨日涨停今日未涨停」
    for e in recent:
        if e.get("broken") and e["prev_streak"] >= 1:
            tags.append("limit:broken")
            if e["prev_streak"] >= 3:
                tags.append("limit:high_board_broken")
            break

    # 近 5 日有涨停（不论是否今天）
    if any(e["idx"] >= n - 5 and not e.get("broken") for e in ev):
        tags.append("limit:active_5d")

    last_bar = bars[-1]
    return {
        "code": code,
        "date": last_bar["date"],
        "close": round(last_bar["close"], 3),
        "tags": list(dict.fromkeys(tags)),
        "detail": detail,
        "known_limits": known_limits(),
        "note": "封单/首次封板时间/龙虎榜不可用；strong_seal 为「收盘=最高价」近似",
    }


def run_all(codes=None, out_path=None):
    codes = codes or list_codes()
    items, skipped = [], 0
    for code in codes:
        try:
            r = analyze(load_raw(code), code)
        except Exception as e:
            skipped += 1
            print(f"skip {code}: {str(e)[:60]}", file=sys.stderr)
            continue
        if r:
            items.append(r)
        else:
            skipped += 1
    doc = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "limit_structure/v1",
        "book": "#15 龙头作手 / #29 铁血短线·铁血战记 / #31 擒庄秘籍（可落地部分）",
        "count": len(items),
        "known_limits": known_limits(),
        "items": items,
    }
    out_path = out_path or os.path.join(DATA_DIR, "limit_signals.json")
    tmp = out_path + ".tmp"
    json.dump(doc, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return doc, skipped


def _tag_fn(view):
    """校准入口：code 随每根 bar 带入（涨停阈值按板块不同，不能统一用主板 10%）。

    bars 里的 code 由 main() 在校准前注入；缺省按主板阈值兜底。
    """
    code = view[-1].get("code") or "sh600000"
    r = analyze(view, code)
    return r["tags"] if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--require-oos", dest="require_oos", action="store_true",
                    help="严格模式：主统计只吃 in-sample，OOS 仅用于判定（涨停层实测会清空）")
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args()

    if a.code:
        r = analyze(load_raw(a.code), a.code)
        if not r:
            print("数据不足，无法分析")
            return
        if a.verbose:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print(f"{r['code']} {r['date']} close={r['close']} tags={r['tags']}")
        return

    if a.validate or a.calibrate:
        codes = list_codes()[: a.sample]
        samples = []
        for c in codes:
            b = load_raw(c, limit=400)
            if len(b) >= 120:
                for x in b:
                    x["code"] = c
                samples.append((c, b))
        rows = SV.validate(_tag_fn, samples, horizon=5, min_history=MIN_BARS, cut_step=2)
        if a.validate:
            SV.print_rows(rows, 30)
        if a.calibrate:
            # 涨停层是样本外最脆弱的一层（实测 6 条显著标签 OOS 全部归零或反号，
            # strong_seal 系列直接翻负），故写表时附上样本外体检字段，让人一眼
            # 看到「这条在表外还成立吗」，而不是只在报告里留一句话。
            #   --require-oos : 严格模式，主统计只吃 in-sample（避免训练/验证重叠）
            #   默认         : 主统计仍为全样本，仅把 OOS 字段并上去作体检，不据此过滤
            if a.require_oos:
                rows = SV.validate(_tag_fn, samples, horizon=5, min_history=MIN_BARS,
                                   cut_step=2, oos_split=0.7)
            else:
                seg = {r["signal"]: r for r in SV.validate(
                    _tag_fn, samples, horizon=5, min_history=MIN_BARS,
                    cut_step=2, oos_split=0.7)}
                for r in rows:
                    s = seg.get(r["signal"])
                    if s and s.get("oos_t") is not None:
                        r["oos_n"], r["oos_t"], r["oos_avg_fwd"] = (
                            s["oos_n"], s["oos_t"], s["oos_avg_fwd"])
            table = SV.calibrate(rows, BULLISH_TAGS, min_n=100, min_t=1.5,
                                 require_oos=a.require_oos)
            p = os.path.join(DATA_DIR, "limit_calibration.json")
            meta = {"sample": a.sample, "engine": "limit_structure/v1", "adj": "raw"}
            if a.require_oos:
                meta["require_oos"] = True
                meta["note"] = "主统计仅 in-sample（split 0.7），oos_* 用于判定"
            else:
                meta["oos_diag"] = "oos_* 为样本外体检（split 0.7）；主统计仍为全样本，未据此过滤"
                robust = [k for k, v in table.items() if v.get("oos_t") is not None
                          and abs(v["oos_t"]) >= 1.5 and v.get("oos_avg_fwd") is not None
                          and (v["avg_fwd"] > 0) == (v["oos_avg_fwd"] > 0)]
                meta["oos_robust"] = f"{len(robust)}/{len(table)}"
                if not robust:
                    meta["oos_warning"] = ("涨停层无一条标签通过样本外检验"
                                           "（OOS |t|<1.5 或方向翻转），建议下游降权或停用")
            SV.write_calibration(p, table, meta=meta, horizon=5)
            print(f"校准表 → {p}（{len(table)} 个标签）")
            for k, v in sorted(table.items(), key=lambda kv: -abs(kv[1]["t"]))[:10]:
                print(f"  {k:26} n={v['n']:>5} fwd={v['avg_fwd']:>6.2f}% t={v['t']:>5.2f} {v['edge']}")
        return

    if a.all:
        doc, skipped = run_all()
        print(f"已写出 {doc['count']} 只（跳过 {skipped}）→ data/limit_signals.json")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
