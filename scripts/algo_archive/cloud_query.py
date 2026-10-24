#!/usr/bin/env python3
"""
云端查询层 v2 — 读取通道三级回退: jsdelivr CDN → raw → API+token
  (CDN/raw 匿名无限额; API 带 token 5000/h, 仅兜底)
数据地图:
  K线:   t1/data/bulk/day_qfq_XX.csv   全市场日线分片("# code"分节, 行= code|date|o|c|h|l|v)
  盘中:  t2/…  快照 + minute/{date}.csv 全市场5分钟线(15:55自动归档)
  新闻:  t3/news/YYYY-MM-DD.md

用法:
  python3 cloud_query.py last sh601975        # 最新一根
  python3 cloud_query.py day sh601975 20      # 尾部N根
  python3 cloud_query.py minute 2026-10-12 sz001300   # 某日5分钟线(可过滤单票)
  python3 cloud_query.py profile 2026-10-12 sz001300  # 分钟级盘面画像
  python3 cloud_query.py tail-heavy 2026-10-12        # 尾盘放量异动榜
  python3 cloud_query.py stat                          # 通道使用统计
"""
from __future__ import annotations
import json, os, sys, urllib.request, time

TOKEN = os.environ.get("GH_TOKEN", "")
CDN1 = "https://cdn.jsdelivr.net/gh/qwerzxcva/t1@main"
RAW1 = "https://raw.githubusercontent.com/qwerzxcva/t1/main"
CDN2 = "https://cdn.jsdelivr.net/gh/qwerzxcva/t2@main"
RAW2 = "https://raw.githubusercontent.com/qwerzxcva/t2/main"
SHARDS = 10
CACHE = "/tmp/stocks_shards"
os.makedirs(CACHE, exist_ok=True)
_STAT = {"cdn": 0, "raw": 0, "api": 0, "fail": 0}


def _get(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": "stocks-algo"}), timeout=timeout).read()


def _get_api(repo, path, timeout=20):
    h = {"Authorization": f"token {TOKEN}", "User-Agent": "stocks-algo",
         "Accept": "application/vnd.github.raw"}
    return urllib.request.urlopen(urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{path}", headers=h), timeout=timeout).read()


def fetch_file(repo_num: int, path: str, timeout=20) -> str:
    """jsdelivr → raw → API 三级回退"""
    if repo_num == 1:
        cands = [(f"{CDN1}/{path}", "cdn"), (f"{RAW1}/{path}", "raw")]
    elif repo_num == 2:
        cands = [(f"{CDN2}/{path}", "cdn"), (f"{RAW2}/{path}", "raw")]
    else:
        cands = []
    for u, chan in cands:
        try:
            txt = _get(u, timeout).decode()
            _STAT[chan] += 1
            return txt
        except Exception:
            continue
    try:
        txt = _get_api(f"qwerzxcva/t{repo_num}", path, timeout).decode()
        _STAT["api"] += 1
        return txt
    except Exception:
        _STAT["fail"] += 1
        return ""


def _shard(i: int) -> str:
    fp = f"{CACHE}/day_qfq_{i:02d}.csv"
    if os.path.exists(fp) and os.path.getsize(fp) > 100000:
        return open(fp).read()
    txt = fetch_file(1, f"data/bulk/day_qfq_{i:02d}.csv")
    if txt:
        open(fp, "w").write(txt)
    return txt


def _code_sections(txt: str):
    out = {}
    cur = None
    for l in txt.splitlines():
        if l.startswith("# "):
            cur = l[2:].strip()
            if "allmarket" not in cur and cur not in out:
                out[cur] = []
        elif cur and "|" in l:
            out[cur].append(l)
    return out


def fetch_bars(code: str):
    for i in range(SHARDS):
        txt = _shard(i)
        if not txt:
            continue
        if f"\n# {code}\n" not in txt and not txt.startswith(f"# {code}\n"):
            if f"# {code}" not in txt:
                continue
        sec = _code_sections(txt).get(code)
        if sec:
            rows = []
            for l in sec:
                f = l.split("|")
                if len(f) >= 7:
                    rows.append({"code": f[0], "date": f[1], "open": float(f[2]), "close": float(f[3]),
                                 "high": float(f[4]), "low": float(f[5]), "volume": float(f[6])})
            rows.sort(key=lambda r: r["date"])
            return rows
    return []


# ---------- 分钟级云上读取 ----------
def minute_day(date: str, code: str = None):
    """t2仓 minute/{date}.csv → 全市场5分钟线; 可按code过滤"""
    fp = f"{CACHE}/minute_{date}.csv"
    if os.path.exists(fp) and os.path.getsize(fp) > 1000:
        txt = open(fp).read()
    else:
        txt = fetch_file(2, f"minute/{date}.csv", timeout=30)
        if not txt:
            return []
        open(fp, "w").write(txt)
    out = []
    for i, l in enumerate(txt.splitlines()):
        if i == 0 or not l.strip():
            continue
        f = l.split(",")
        if len(f) >= 5 and (code is None or f[0] == code):
            try:
                out.append({"code": f[0], "hhmm": f[2], "price": float(f[3]), "volume": float(f[4])})
            except (ValueError, IndexError):
                pass
    return out


def minute_intraday_profile(code: str, date: str):
    """分钟级盘面画像: 开盘30分动量/午后动量/日内振幅/最大放量时点"""
    rows = sorted(minute_day(date, code), key=lambda r: r["hhmm"])
    if len(rows) < 10:
        return {}
    ps = [r["price"] for r in rows]
    vs = [r["volume"] for r in rows]
    am = (ps[5] / ps[0] - 1) * 100 if ps[0] else 0
    pm = (ps[-1] / ps[len(ps)//2] - 1) * 100
    hi, lo = max(ps), min(ps)
    peak_i = vs.index(max(vs))
    return {"date": date, "code": code, "bars": len(rows),
            "am30_mom": round(am, 2), "pm_mom": round(pm, 2),
            "range_pct": round((hi / lo - 1) * 100, 2) if lo else 0,
            "vol_peak_hhmm": rows[peak_i]["hhmm"],
            "first_price": ps[0], "last_price": ps[-1]}


def minute_day_summary(date: str):
    """全市场分钟级统计: 尾盘半小时量占比>40%的异动票"""
    rows = minute_day(date)
    if not rows:
        return {}
    by = {}
    for r in rows:
        by.setdefault(r["code"], []).append(r)
    out = []
    for code, rs in by.items():
        rs.sort(key=lambda r: r["hhmm"])
        vols = [r["volume"] for r in rs]
        if sum(vols) <= 0 or len(vols) < 8:
            continue
        tail_v = sum(vols[-6:]) / sum(vols)
        out.append({"code": code, "bars": len(rs),
                    "tail_vol_ratio": round(tail_v, 2),
                    "last_price": rs[-1]["price"]})
    out.sort(key=lambda x: -x["tail_vol_ratio"])
    return {"date": date, "n": len(out), "tail_heavy": out[:20]}


def last(code: str):
    rows = fetch_bars(code)
    return rows[-1] if rows else None


def day(code: str, tail: int = 20):
    return fetch_bars(code)[-tail:]


def universe():
    txt = _shard(0)
    return sorted(_code_sections(txt).keys())


def index_month(code="sh000001", tail=12):
    return fetch_bars(code)[-tail:]


def news(day_str: str):
    try:
        return json.loads(fetch_file(3, f"news_map/{day_str}.json"))
    except Exception:
        return {}


def stat():
    return dict(_STAT)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    try:
        if cmd == "day":
            for r in day(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20):
                print(f"{r['date']},{r['open']},{r['close']},{r['high']},{r['low']},{r['volume']:.0f}")
        elif cmd == "last":
            r = last(sys.argv[2])
            print(f"{r['date']} O{r['open']} C{r['close']} H{r['high']} L{r['low']} V{r['volume']:.0f}" if r else "云端未找到")
        elif cmd == "minute":
            date = sys.argv[2]
            code = sys.argv[3] if len(sys.argv) > 3 else None
            rows = minute_day(date, code)
            print(f"{len(rows)} rows")
            for r in rows[:10]:
                print(r)
        elif cmd == "profile":
            print(json.dumps(minute_intraday_profile(sys.argv[3], sys.argv[2]), ensure_ascii=False, indent=1))
        elif cmd == "tail-heavy":
            print(json.dumps(minute_day_summary(sys.argv[2]), ensure_ascii=False, indent=1)[:2000])
        elif cmd == "stat":
            print(stat())
        else:
            print(__doc__)
    except Exception as e:
        print("ERR", e)
