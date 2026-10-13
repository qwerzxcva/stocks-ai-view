#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/cycle_algo.py ═══
# 上游 commit: 4f3099796513 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
周期算法 v2 — 数据驱动重构 (2026-10-12)
数据: 腾讯kline/kline接口指数全史(上证1990起/深成1993/沪深300 2005/创业板2010/科创2019)
     落 stockdb/wh/index_daily 分区(按年), stats缓存 data/cycle/stats.json
对比v1: 硬编码常量(单次手工统计) → 自动重算+多指数+逐年轮动表

用法:
  python3 cycle_algo.py rebuild       # 拉指数全史→重算全部统计→落wh+缓存
  python3 cycle_algo.py status        # 当前周期位置(五维打分+阶段机联动)
  python3 cycle_algo.py yearly        # 历年逐月收益轮动表(热力图数据)
  python3 cycle_algo.py season        # 月度/星期季节性(全史自动重算)
"""
from __future__ import annotations
import json, os, sys, time, urllib.request
from datetime import datetime
from collections import defaultdict

HOME = os.path.expanduser("~")
sys.path.insert(0, f"{HOME}/stockdb/scripts")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
STATS = f"{HOME}/stockdb/data/cycle/stats.json"
INDEXES = ["sh000001", "sz399001", "sh000300", "sz399006", "sh000688"]
NAMES = {"sh000001": "上证指数", "sz399001": "深成指", "sh000300": "沪深300",
         "sz399006": "创业板指", "sh000688": "科创50"}

# ── 拉取 ──────────────────────────────────────────────
def idx_seg(code, end, n=800):
    u = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,{end},800"
    d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=15))
    return d["data"][code]["day"]

def idx_all(code):
    allr, end = {}, ""
    for _ in range(25):
        seg = idx_seg(code, end)
        if not seg: break
        for r in seg: allr[r[0]] = r
        newmin = min(r[0] for r in seg)
        if end and newmin >= end: break
        end = newmin
        if end <= "1991-01-01": break
        time.sleep(0.2)
    return sorted(allr.values(), key=lambda r: r[0])

# ── 统计重算 ────────────────────────────────────────────
def pct_series(rows):
    out = []
    for i in range(1, len(rows)):
        try: out.append((rows[i][0], (float(rows[i][2])/float(rows[i-1][2])-1)*100))
        except Exception: pass
    return out

def season_stats(rets):
    """星期+月度季节性: {avg, win, n}"""
    wd = defaultdict(list); mo = defaultdict(list)
    for d, p in rets:
        wd[datetime.strptime(d, "%Y-%m-%d").weekday()].append(p)
        mo[int(d[5:7])].append(p)
    agg = lambda arr: {"avg": round(sum(arr)/len(arr), 3), "win": round(100*sum(1 for x in arr if x > 0)/len(arr), 1), "n": len(arr)}
    return {"weekday": {k: agg(v) for k, v in sorted(wd.items())},
            "month": {k: agg(v) for k, v in sorted(mo.items())}}

def yearly_matrix(rets):
    """历年逐月收益%: {year: {month: cum%}} + 年合计"""
    ym = defaultdict(lambda: defaultdict(float))
    yr = defaultdict(float)
    for d, p in rets:
        y, m = d[:4], int(d[5:7])
        ym[y][m] += p; yr[y] += p
    return {"months": {y: {str(m): round(v, 1) for m, v in sorted(mm.items())} for y, mm in sorted(ym.items())},
            "year_total": {y: round(v, 1) for y, v in sorted(yr.items())}}

def streak_stats(rets):
    """连段长度→下一日继续概率(实测)"""
    seq = []  # (kind, len)
    up = rets[0][1] > 0; n = 1
    for _, p in rets[1:]:
        if (p > 0) == up: n += 1
        else: seq.append((up, n)); up = p > 0; n = 1
    seq.append((up, n))
    cont = defaultdict(lambda: [0, 0])
    for i, (up, n) in enumerate(seq):
        if i+1 < len(seq):
            nxt = seq[i+1][0] == up
            c = cont[(up, n)]; c[1] += 1
            if nxt: c[0] += 1
    return {f"{'up' if u else 'dn'}{n}": {"cont_prob": round(100*c[0]/c[1], 1), "n": c[1]}
            for (u, n), c in sorted(cont.items()) if c[1] >= 8}

def width_stats(rets):
    """涨跌家数近似: 用当日相对全市场宽度(此处指数无宽度, 用单日涨幅分层)"""
    return None  # 宽度用pools/limit数据, 不在指数内

def rebuild():
    os.makedirs(os.path.dirname(STATS), exist_ok=True)
    all_stats = {"rebuilt_at": datetime.now().astimezone().isoformat(timespec="seconds"), "indexes": {}}
    for code in INDEXES:
        try:
            rows = idx_all(code)
            rets = pct_series(rows)
            s = {"n_days": len(rows), "span": [rows[0][0], rows[-1][0]],
                 "season": season_stats(rets), "yearly": yearly_matrix(rets),
                 "streak": streak_stats(rets)}
            all_stats["indexes"][code] = s
            print(f"{code} {NAMES[code]}: {len(rows)}日 {rows[0][0]}~{rows[-1][0]}")
        except Exception as e:
            print(code, "ERR", e)
    json.dump(all_stats, open(STATS, "w"), ensure_ascii=False)
    print("saved", STATS)
    return all_stats

def load_stats():
    if not os.path.exists(STATS): rebuild()
    return json.load(open(STATS))

# ── 状态判定 ────────────────────────────────────────────
def status(code="sh000001"):
    st = load_stats()["indexes"][code]
    rows = idx_seg(code, "", 30)
    rets = pct_series(rows)
    today = rows[-1]
    wd = datetime.strptime(today[0], "%Y-%m-%d").weekday()
    dims = []
    w = st["season"]["weekday"].get(str(wd), {})
    dims.append(["星期", ["周一","周二","周三","周四","周五"][wd],
                 f"{w.get('avg',0):+.3f}%/胜{w.get('win',0)}%", 1 if w.get("avg", 0) > 0.05 else (-1 if w.get("avg", 0) < -0.05 else 0)])
    m = st["season"]["month"].get(str(int(today[0][5:7])), {})
    dims.append(["月份", f"{int(today[0][5:7])}月",
                 f"{m.get('avg',0):+.3f}%/胜{m.get('win',0)}%", 1 if m.get("avg", 0) > 0.05 else (-1 if m.get("avg", 0) < -0.05 else 0)])
    kind, n = ("涨" if rets[-1][1] > 0 else "跌"), 0
    for _, p in reversed(rets):
        if (p > 0) == (kind == "涨"): n += 1
        else: break
    key = f"{'up' if kind=='涨' else 'dn'}{n+1}"
    cont = st["streak"].get(key, {})
    dims.append(["连段", f"连{kind}{n}天", f"→续{kind}{cont.get('cont_prob','?')}%(n={cont.get('n','?')})", 1 if (kind=="跌" and n>=2) else (-1 if (kind=="涨" and n>=3) else 0)])
    mp = sum(p for _, p in rets[-20:])
    dims.append(["20日动量", f"{mp:+.1f}%", "近20日累计", 1 if mp > 3 else (-1 if mp < -3 else 0)])
    score = sum(d[3] for d in dims)
    return {"date": today[0], "index": NAMES[code], "close": today[2],
            "dims": [{"dim": d[0], "now": d[1], "exp": d[2], "bias": d[3]} for d in dims],
            "composite": score, "bias": "偏多" if score >= 2 else ("偏空" if score <= -2 else "中性")}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "rebuild": rebuild()
    elif cmd == "status": print(json.dumps(status(), ensure_ascii=False, indent=1))
    elif cmd == "yearly":
        st = load_stats()["indexes"]["sh000001"]["yearly"]
        print(json.dumps(st, ensure_ascii=False, indent=1))
    elif cmd == "season":
        st = load_stats()["indexes"]["sh000001"]["season"]
        print(json.dumps(st, ensure_ascii=False, indent=1))
    else: print(__doc__)
