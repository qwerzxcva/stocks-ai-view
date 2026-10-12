#!/usr/bin/env python3
"""
cycle_deep.py — 周期深度分析 (借鉴 jiegengxiangjianxue1/stocks-algo/cycle_algo.py)

与原版的差异:
  - 去掉 DuckDB 依赖, 统计结果直接落 JSON (网站可直接读)
  - 指数全史用腾讯 kline/kline 接口分页拉取 (上证1990起/深成1993/沪深3002005/创业板2010/科创2019)
  - 输出五维周期位置 + 历年逐月轮动表 + 连段续跌概率, 全部带 meta 标注
  - 路径环境变量化, 可在 GitHub Actions 跑

产出:
  data/cycle_deep.json  {
    status:   {date, index, close, dims[5], composite, bias},
    yearly:   {year: {month: cum%}, year_total},
    season:   {weekday: {avg,win,n}, month: {avg,win,n}},
    streak:   {up3: {cont_prob, n}, ...},
    meta:     {source, ts, span, n_days, ...}
  }

用法:
  python3 cycle_deep.py                     # 默认输出到 ai-view/data/cycle_deep.json
  python3 cycle_deep.py --out /tmp/x.json
  python3 cycle_deep.py --rebuild           # 强制重算统计 (忽略缓存)
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

INDEXES = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688"]
NAMES = {"sh000001": "上证指数", "sz399001": "深成指", "sh000300": "沪深300",
         "sz399006": "创业板指", "sh000688": "科创50"}

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "cycle_deep.json")
CACHE = os.environ.get("STOCKS_CYCLE_CACHE") or "/tmp/cycle_stats_cache.json"


# ─── 拉取: 腾讯指数全史 (分页) ─────────────────────────────────────
def idx_seg(code, end="", n=800):
    """腾讯指数日K 一段 (param=code,day,,end,n)。返回 [[date,o,c,h,l,v],...]"""
    u = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,{end},{n}"
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                              timeout=20).read())
        node = (d.get("data") or {}).get(code) or {}
        arr = node.get("day") or node.get("qfqday") or []
        return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in arr if len(r) >= 6]
    except Exception as e:
        print(f"[idx_seg {code}] {e}", file=sys.stderr)
        return []


def idx_all(code, floor="1991-01-01"):
    """分页拉全史 (腾讯每页最多 800 根, 用 end 往前翻)。"""
    allr, end, guard = {}, "", 0
    while guard < 60:
        guard += 1
        seg = idx_seg(code, end)
        if not seg:
            break
        for r in seg:
            allr[r[0]] = r
        dates = sorted(allr.keys())
        newmin = dates[0]
        if end and newmin >= end:
            break
        end = newmin
        if end <= floor:
            break
        time.sleep(0.2)
    return sorted(allr.values(), key=lambda r: r[0])


# ─── 统计 ─────────────────────────────────────────────────────────
def pct_series(rows):
    out = []
    for i in range(1, len(rows)):
        try:
            out.append((rows[i][0], (float(rows[i][2]) / float(rows[i - 1][2]) - 1) * 100))
        except Exception:
            pass
    return out


def season_stats(rets):
    """星期 + 月度季节性"""
    wd, mo = defaultdict(list), defaultdict(list)
    for d, p in rets:
        wd[datetime.strptime(d, "%Y-%m-%d").weekday()].append(p)
        mo[int(d[5:7])].append(p)

    def agg(arr):
        return {"avg": round(sum(arr) / len(arr), 3),
                "win": round(100 * sum(1 for x in arr if x > 0) / len(arr), 1),
                "n": len(arr)}

    return {"weekday": {k: agg(v) for k, v in sorted(wd.items())},
            "month": {k: agg(v) for k, v in sorted(mo.items())}}


def yearly_matrix(rets):
    """历年逐月收益% + 年合计"""
    ym = defaultdict(lambda: defaultdict(float))
    yr = defaultdict(float)
    for d, p in rets:
        y, m = d[:4], int(d[5:7])
        ym[y][m] += p
        yr[y] += p
    return {"months": {y: {str(m): round(v, 1) for m, v in sorted(mm.items())}
                       for y, mm in sorted(ym.items())},
            "year_total": {y: round(v, 1) for y, v in sorted(yr.items())}}


def streak_stats(rets):
    """连段长度 → 下一日继续概率 (实测)"""
    if not rets:
        return {}
    seq = []
    up = rets[0][1] > 0
    n = 1
    for _, p in rets[1:]:
        if (p > 0) == up:
            n += 1
        else:
            seq.append((up, n))
            up = p > 0
            n = 1
    seq.append((up, n))
    cont = defaultdict(lambda: [0, 0])
    for i, (u, n) in enumerate(seq):
        if i + 1 < len(seq):
            c = cont[(u, n)]
            c[1] += 1
            if seq[i + 1][0] == u:
                c[0] += 1
    return {f"{'up' if u else 'dn'}{n}": {"cont_prob": round(100 * c[0] / c[1], 1), "n": c[1]}
            for (u, n), c in sorted(cont.items()) if c[1] >= 8}


# ─── 统计构建 / 缓存 ───────────────────────────────────────────────
def build_stats():
    all_stats = {"rebuilt_at": datetime.now(CST).isoformat(timespec="seconds"),
                 "indexes": {}}
    for code in INDEXES:
        try:
            rows = idx_all(code)
            if len(rows) < 200:
                print(f"[stats] {code} 数据不足 ({len(rows)} 日), 跳过", file=sys.stderr)
                continue
            rets = pct_series(rows)
            all_stats["indexes"][code] = {
                "n_days": len(rows),
                "span": [rows[0][0], rows[-1][0]],
                "season": season_stats(rets),
                "yearly": yearly_matrix(rets),
                "streak": streak_stats(rets),
            }
            print(f"[stats] {NAMES[code]}: {len(rows)}日 {rows[0][0]}~{rows[-1][0]}")
        except Exception as e:
            print(f"[stats] {code} ERR {e}", file=sys.stderr)
    return all_stats


def load_stats(force=False):
    if not force and os.path.exists(CACHE):
        try:
            d = json.load(open(CACHE, encoding="utf-8"))
            if d.get("indexes"):
                print(f"[stats] 使用缓存 {CACHE} "
                      f"(rebuilt {d.get('rebuilt_at')})")
                return d
        except Exception:
            pass
    d = build_stats()
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(d, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[stats] 缓存 -> {CACHE}")
    except Exception as e:
        print(f"[stats] 缓存写入失败: {e}", file=sys.stderr)
    return d


# ─── 五维周期位置 ─────────────────────────────────────────────────
def status(code="sh000001"):
    st = load_stats()["indexes"].get(code)
    if not st:
        return None
    rows = idx_seg(code, "", 30)
    if len(rows) < 5:
        return None
    rets = pct_series(rows)
    today = rows[-1]
    wd = datetime.strptime(today[0], "%Y-%m-%d").weekday()
    dims = []

    w = st["season"]["weekday"].get(str(wd), {})
    dims.append({"dim": "星期", "now": ["周一", "周二", "周三", "周四", "周五"][wd],
                 "exp": f"历史均{w.get('avg', 0):+.3f}%/胜{w.get('win', 0)}%(n={w.get('n', 0)})",
                 "bias": 1 if w.get("avg", 0) > 0.05 else (-1 if w.get("avg", 0) < -0.05 else 0)})

    m = st["season"]["month"].get(str(int(today[0][5:7])), {})
    dims.append({"dim": "月份", "now": f"{int(today[0][5:7])}月",
                 "exp": f"历史均{m.get('avg', 0):+.3f}%/胜{m.get('win', 0)}%(n={m.get('n', 0)})",
                 "bias": 1 if m.get("avg", 0) > 0.05 else (-1 if m.get("avg", 0) < -0.05 else 0)})

    kind = "涨" if rets[-1][1] > 0 else "跌"
    n = 0
    for _, p in reversed(rets):
        if (p > 0) == (kind == "涨"):
            n += 1
        else:
            break
    key = f"{'up' if kind == '涨' else 'dn'}{n + 1}"
    cont = st["streak"].get(key, {})
    dims.append({"dim": "连段", "now": f"连{kind}{n}天",
                 "exp": f"历史续{kind}概率{cont.get('cont_prob', '?')}%(n={cont.get('n', '?')})",
                 "bias": 1 if (kind == "跌" and n >= 2) else (-1 if (kind == "涨" and n >= 3) else 0)})

    mp = sum(p for _, p in rets[-20:])
    dims.append({"dim": "20日动量", "now": f"{mp:+.1f}%",
                 "exp": "近20日累计涨跌",
                 "bias": 1 if mp > 3 else (-1 if mp < -3 else 0)})

    # 第五维: 年内位置 (当前月在今年已过月份中的排名)
    ym = st["yearly"]["months"]
    cur_y = today[0][:4]
    cur_m = int(today[0][5:7])
    months_done = [int(k) for k in ym.get(cur_y, {}) if int(k) <= cur_m]
    if months_done:
        vals = [ym[cur_y][str(m)] for m in months_done]
        rank = sorted(vals, reverse=True).index(ym[cur_y].get(str(cur_m), 0)) + 1
        dims.append({"dim": "年内位置", "now": f"{cur_y}年{cur_m}月 第{rank}/{len(vals)}月",
                     "exp": f"今年已过月份收益排名",
                     "bias": 1 if rank <= len(vals) / 3 else (-1 if rank > len(vals) * 2 / 3 else 0)})
    else:
        dims.append({"dim": "年内位置", "now": "—", "exp": "数据不足", "bias": 0})

    score = sum(d["bias"] for d in dims)
    return {"date": today[0], "index": NAMES[code], "close": today[2],
            "dims": dims, "composite": score,
            "bias": "偏多" if score >= 2 else ("偏空" if score <= -2 else "中性")}


# ─── main ─────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--rebuild", action="store_true", help="强制重算统计")
    ap.add_argument("--code", default="sh000001")
    args = ap.parse_args()

    now = datetime.now(CST)
    print(f"=== cycle_deep {now:%Y-%m-%d %H:%M CST} ===", flush=True)

    st_all = load_stats(force=args.rebuild)
    if not st_all.get("indexes"):
        print("ERROR: 无统计数据", file=sys.stderr)
        return 1

    st = st_all["indexes"][args.code]
    s = status(args.code)
    if not s:
        print("ERROR: status 计算失败", file=sys.stderr)
        return 1

    out = {
        "status": s,
        "yearly": st["yearly"],
        "season": st["season"],
        "streak": st["streak"],
        "meta": {
            "source": "腾讯证券 kline/kline 指数全史 (借鉴 stocks-algo/cycle_algo.py)",
            "ts": now.isoformat(),
            "rebuilt_at": st_all.get("rebuilt_at"),
            "index": args.code,
            "n_days": st["n_days"],
            "span": st["span"],
            "dims": "星期/月份/连段/20日动量/年内位置 五维, 每维 bias∈{-1,0,1}",
            "composite_rules": "composite=Σbias; ≥2偏多 / ≤-2偏空 / 其余中性",
            "caveat": "季节性统计为全史等权平均, 未剔除极端行情; "
                      "连段续跌概率要求样本≥8次; 仅供研究, 非投资建议",
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n五维周期位置 ({s['index']} {s['date']} close={s['close']}):")
    for d in s["dims"]:
        b = {1: "多", -1: "空", 0: "中"}[d["bias"]]
        print(f"  [{b}] {d['dim']:8s} {d['now']:16s} {d['exp']}")
    print(f"  综合 {s['composite']:+d} → {s['bias']}")

    yt = st["yearly"]["year_total"]
    if yt:
        years = sorted(yt.keys())
        print(f"\n历年年度收益 ({years[0]}~{years[-1]}):")
        for y in years[-8:]:
            bar = "+" * int(abs(yt[y]) / 2) if yt[y] > 0 else "-" * int(abs(yt[y]) / 2)
            print(f"  {y}: {yt[y]:+7.1f}% {bar}")

    print(f"\nDONE -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
