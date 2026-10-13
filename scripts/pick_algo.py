#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · algorithms/pick_algo.py ═══
# 上游 commit: 3a25369b013c · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 适配: 2026-10-13 路径规范 (数据源 t1 云端分片 → 本地 BULK bulk CSV; --out 落盘 data/pick_algo.json)
"""
选股算法 v1 — 打板统计学驱动（qwerzxcva/stocks-algo）
依据: stocks/docs/limitup-stats.md 实测统计 (168,368 首板样本, 2022-07~2026-10)

核心统计事实（代码内常量全部来自实测）:
  - 晋级率: 首板10.6% / 2板24.2% / 3板45.2% / 4板49.6% / 5板+63.9%
  - 次日收益: 首板+0.42% / 2板+1.18% / 3板+2.19%(峰) / 4板+1.95%
  - 量能因子: 爆量(>3x)首板+0.93% vs 缩量首板+0.30%
  - 年份环境: 2023打板亏(-0.91%), 2025大年(+2.01%), 打板前先判环境

输入: 数据来自 t1 (GitHub 云端 bycode CSV) 或本地 parquet
输出: 候选打分列表 (score 0-100)
"""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

TOKEN = os.environ.get("GH_TOKEN", "")
RAW = "https://raw.githubusercontent.com/qwerzxcva/t1/main"

# ── 实测统计常量 ──────────────────────────────────────────
PROMOTE = {1: 10.6, 2: 24.2, 3: 45.2, 4: 49.6, 5: 63.9}   # N板晋级率%
NEXTRET = {1: 0.42, 2: 1.18, 3: 2.19, 4: 1.95, 5: 1.92}   # N板次日均收益%

# ── 数据获取层（云端优先） ────────────────────────────────
def fetch_day_csv(code: str) -> list[dict]:
    """从 t1 云端拉单只日线 (bulk 分片, "# code" 分节)。"""
    fp = f"/tmp/stocks_shards/{code}.json"
    if os.path.exists(fp):
        return json.load(open(fp))
    SHARDS_N = 10
    for i in range(SHARDS_N):
        try:
            txt = _get(f"https://api.github.com/repos/qwerzxcva/t1/contents/data/bulk/day_qfq_{i:02d}.csv")
        except Exception:
            continue
        if f"# {code}\n" not in txt and not txt.startswith(f"# {code}\n"):
            continue
        rows = []
        cur = None
        for line in txt.splitlines():
            if line.startswith("# "):
                cur = line[2:].strip()
            elif cur == code and "|" in line:
                f = line.split("|")
                if len(f) >= 7:
                    rows.append({"date": f[1], "open": float(f[2]), "close": float(f[3]),
                                 "high": float(f[4]), "low": float(f[5]), "volume": float(f[6])})
        if rows:
            json.dump(rows, open(fp, "w"))
            return rows
    return []

def _gh_headers():
    h = {"User-Agent": "stocks-algo",
         "Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.raw"}
    return h

def _get(url, timeout=90):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_gh_headers()), timeout=timeout).read().decode()

def fetch_universe() -> list[str]:
    """全市场代码清单 (api.github.com raw 模式, 本机 raw 域名不稳)"""
    txt = _get("https://api.github.com/repos/qwerzxcva/t1/contents/meta/universe.csv")
    out = []
    for line in txt.splitlines():
        if line.startswith("#") or "|" not in line:
            continue
        code = line.split("|")[0].strip()
        if code.startswith(("sh60", "sz00")):   # 主板硬约束
            out.append(code)
    return out

# ── 指标计算 ──────────────────────────────────────────────
def is_zt(row) -> bool:
    """收盘封死涨停: close>=high*0.999 (实测口径)"""
    return row["close"] >= row["high"] * 0.999
def streak_height(rows: list[dict]) -> int:
    """当前连板高度 (以最新一根为 today)"""
    h = 0
    for r in reversed(rows):
        if is_zt(r):
            h += 1
        else:
            break
    return h

def vol_ratio(rows: list[dict]) -> float:
    """今日量/昨日量"""
    if len(rows) < 2 or rows[-2]["volume"] == 0:
        return 0.0
    return rows[-1]["volume"] / rows[-2]["volume"]

def pos_20d(rows: list[dict]) -> float:
    """20日涨幅（位置因子）"""
    if len(rows) < 21:
        return 0.0
    base = rows[-21]["close"]
    return rows[-1]["close"] / base - 1 if base else 0.0

def is_new_high_60(rows: list[dict]) -> bool:
    w = rows[-60:] if len(rows) >= 60 else rows
    return rows[-1]["close"] >= max(r["high"] for r in w[:-1]) if len(w) > 1 else False

# ── 打分模型 ──────────────────────────────────────────────
@dataclass
class Candidate:
    code: str
    score: float = 0.0
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

def score_limitup(code: str, rows: list[dict], env_ok: bool, env_up_ratio: float | None = None) -> Candidate:
    """对一只已涨停的票打分 (0-100)。核心权重来自实测统计。"""
    c = Candidate(code)
    today = rows[-1]

    # 硬门槛: 今天必须收盘封死
    if not is_zt(today):
        c.checks["zt"] = False
        return c
    c.checks["zt"] = True

    h = streak_height(rows)
    c.checks["height"] = h

    # ① 高度分 (0-35): 2-4板甜区, 3板最优(+2.19%)
    if h == 1:
        c.score += 12; c.reasons.append("首板(基线12)")
    elif 2 <= h <= 4:
        c.score += 22 + (3 - abs(h - 3)) * 5   # 3板=35, 2/4板=30
        c.reasons.append(f"{h}板甜区(+{22 + (3 - abs(h-3))*5})")
    elif h >= 5:
        c.score += 15; c.reasons.append(f"{h}板高位(晋级率高但收益衰减,仅15)")

    # ② 量能分 (0-30): 最强因子
    vr = vol_ratio(rows)
    c.checks["vol_ratio"] = round(vr, 2)
    if vr > 3:
        c.score += 30; c.reasons.append("爆量>3x(+30,实测+0.93%)")
    elif vr > 1.5:
        c.score += 18; c.reasons.append("温和放量(+18)")
    else:
        c.score += 3;  c.reasons.append("缩量板(+3,负期望子集)")

    # ③ 位置分 (0-15): 区分度小,高位趋势板略优
    p20 = pos_20d(rows)
    c.checks["pos_20d"] = round(p20 * 100, 1)
    if p20 > 0.15:
        c.score += 12; c.reasons.append("高位趋势板(+12)")
    elif p20 < 0:
        c.score += 10; c.reasons.append("低位板(+10)")
    else:
        c.score += 6;  c.reasons.append("中位板(+6)")

    # ④ 创新高加分 (0-10): 60日新高=无套牢盘
    if is_new_high_60(rows):
        c.score += 10; c.reasons.append("60日新高(+10)")

    # ⑤ 环境闸门 (0-10 + 乘数): 非打板年/冰点期整体降权; 普跌日全组乘数降权
    c.score += 10 if env_ok else 0
    c.reasons.append("环境闸门" + ("通过(+10)" if env_ok else "关闭(+0)"))
    # ⑤b 大盘宽度乘数 (回测验证: 普跌日次日均值-0.005 vs 普涨+0.119,见 backtest.py)
    if env_up_ratio is not None:
        m = env_mult(env_up_ratio)
        if m < 1.0:
            c.score *= m
            c.reasons.append(f"大盘宽度{env_up_ratio:.0%}普跌降权(×{m})")
        elif m > 1.0:
            c.score = min(c.score * m, 100)
            c.reasons.append(f"大盘宽度{env_up_ratio:.0%}普涨惯性(×{m})")

    return c

def env_mult(up_ratio: float) -> float:
    """大盘宽度乘数 (2026-10-11 回测消融验证): 普跌日整体降权防补跌。"""
    if up_ratio < 0.25:  return 0.3   # 恐慌日
    if up_ratio < 0.40:  return 0.7   # 弱市
    if up_ratio > 0.80:  return 1.1   # 普涨日
    return 1.0

def env_gate(ztpool_today: int, ztpool_20avg: float) -> bool:
    """环境闸门: 当日涨停家数 vs 20日均值。
    冰点(<100)关闭; 亢奋(>600)降权; 健康区(150-500)通过。"""
    if ztpool_today < 100:
        return False
    if ztpool_20avg and ztpool_20avg < 120:
        return False
    return True

# ── 主流程: 扫全市场找候选 ────────────────────────────────
def scan(universe: list[str] | None = None, ztpool_today: int = 300, ztpool_20avg: float = 300.0,
         min_score: float = 55, top_n: int = 20, progress=None,
         env_up_ratio: float | None = None) -> list[dict]:
    """扫描全市场(涨停池优先后全分片扫描), 返回打分>=min_score 的候选(降序)。

    v2 提速: 分片级并行 —— 一次下载一个分片(2.4MB,~520只)即可评全部片内票,
    网络往返从 O(股票数) 降到 O(分片数)=~11 次, 整体快 40x。
    """
    from concurrent.futures import ThreadPoolExecutor
    codes = universe or fetch_universe()
    env_ok = env_gate(ztpool_today, ztpool_20avg)
    # 按 sh/sz 前缀分片
    shards = {}
    for code in codes:
        shards.setdefault(("sh" if code.startswith("sh") else "sz"), []).append(code)
    results = []
    seen = set()   # 全局去重: 同一票可能出现在多个分片(nightly 增量重写)

    def scan_prefix(prefix: str, plist: list[str]) -> list[dict]:
        out = []
        code_set = set(plist) - seen
        # 分片为混合市场编号 day_qfq_00..49 (每片~2.4MB, "# code" 分节)
        for shard in range(0, 50):
            name = f"day_qfq_{shard:02d}.csv"
            cf = f"/tmp/stocks_shards/{name}"
            try:
                import time as _t
                if os.path.exists(cf) and _t.time() - os.path.getmtime(cf) < 21600:
                    txt = open(cf).read()
                else:
                    txt = _get(f"https://api.github.com/repos/qwerzxcva/t1/contents/data/bulk/{name}")
                    os.makedirs("/tmp/stocks_shards", exist_ok=True)
                    open(cf, "w").write(txt)
            except Exception:
                continue
            cur_code, cur_rows = None, []
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if cur_code in code_set and len(cur_rows) >= 25:
                        c = _score_one(cur_code, cur_rows, env_ok, env_up_ratio, _seen=seen)
                        if c: out.append(c)
                    cur_code, cur_rows = line[1:].strip(), []
                    continue
                p = line.split("|")
                if len(p) < 7:
                    continue
                cur_rows.append({"date": (p[1][:4]+"-"+p[1][4:6]+"-"+p[1][6:8]) if len(p[1])==8 else p[1], "open": float(p[2]), "close": float(p[3]),
                                 "high": float(p[4]), "low": float(p[5]), "volume": float(p[6])})
            if cur_code in code_set and len(cur_rows) >= 25:
                c = _score_one(cur_code, cur_rows, env_ok, env_up_ratio, _seen=seen)
                if c: out.append(c)
            if progress:
                progress(name, len(out))
        return out

    with ThreadPoolExecutor(max_workers=2) as ex:
        for part in ex.map(lambda kv: scan_prefix(*kv), shards.items()):
            results.extend(part)

    results.sort(key=lambda x: -x["score"])
    return results[:top_n]

def _score_one(code, rows, env_ok, env_up_ratio, _seen=None):
    if _seen is not None:
        if code in _seen:
            return None
        _seen.add(code)
    try:
        # 新鲜度门槛: 最后K线须在近10个自然日内(过滤退市/长停票的陈年涨停)
        if (date.today() - date.fromisoformat(rows[-1]["date"])).days > 10:
            return None
        if not is_zt(rows[-1]):
            return None
        c = score_limitup(code, rows, env_ok, env_up_ratio)
        if c.score >= 55:
            return {"code": code, "score": round(c.score, 1),
                    "checks": c.checks, "reasons": c.reasons}
    except Exception:
        pass
    return None

# ── 本地文件模式 (Actions/离线用): 零网络 ─────────────────
def load_bulk_rows(bulk_dir: str = None) -> dict:
    """读本地 bulk day_qfq_*.csv → {code: [row,...]} 升序 (与上游 t1 分片同结构)。"""
    import glob
    bulk_dir = bulk_dir or BULK
    out = {}
    for p in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|") if "|" in line else line.split(",")
                if len(parts) < 6:
                    continue
                code = parts[0]
                if not code.startswith(("sh60", "sz00")):   # 主板硬约束
                    continue
                try:
                    d = parts[1]
                    d = (d[:4] + "-" + d[4:6] + "-" + d[6:8]) if len(d) == 8 and "-" not in d else d
                    out.setdefault(code, []).append({
                        "date": d, "open": float(parts[2]), "close": float(parts[3]),
                        "high": float(parts[4]), "low": float(parts[5]), "volume": float(parts[6])})
                except ValueError:
                    continue
    for c in out:
        out[c].sort(key=lambda r: r["date"])
    return out


def scan_local(shard_dir: str = "/tmp/stocks_shards", min_score: float = 55,
               top_n: int = 20, env_up_ratio: float | None = None,
               ztpool_today: int = 300, ztpool_20avg: float = 300.0) -> list[dict]:
    """从本地分片目录(如 shallow clone 的 data/bulk)扫描, 零网络请求。"""
    import glob
    env_ok = env_gate(ztpool_today, ztpool_20avg)
    results, seen = [], set()
    for fp in sorted(glob.glob(f"{shard_dir}/day_qfq_*.csv")):
        cur_code, cur_rows = None, []
        for line in open(fp, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if cur_code and cur_code not in seen and len(cur_rows) >= 25:
                    seen.add(cur_code)
                    c = _score_one(cur_code, cur_rows, env_ok, env_up_ratio)
                    if c:
                        results.append(c)
                cur_code, cur_rows = (line[1:].strip() if line[1:].strip().startswith(("sh60", "sz00")) else None), []
                continue
            p = line.split("|")
            if len(p) < 7:
                continue
            try:
                cur_rows.append({"date": (p[1][:4]+"-"+p[1][4:6]+"-"+p[1][6:8]) if len(p[1])==8 else p[1], "open": float(p[2]), "close": float(p[3]),
                                 "high": float(p[4]), "low": float(p[5]), "volume": float(p[6])})
            except ValueError:
                continue
        if cur_code and cur_code not in seen and len(cur_rows) >= 25:
            seen.add(cur_code)
            c = _score_one(cur_code, cur_rows, env_ok, env_up_ratio)
            if c:
                results.append(c)
    results.sort(key=lambda x: -x["score"])
    return results[:top_n]


def scan_bulk(bulk_dir: str = None, min_score: float = 55, top_n: int = 20,
              env_up_ratio: float | None = None, ztpool_today: int = 300,
              ztpool_20avg: float = 300.0) -> list[dict]:
    """本项目数据源: paths.BULK 的 day_qfq_*.csv (替代上游 t1 云端分片), 零网络。"""
    env_ok = env_gate(ztpool_today, ztpool_20avg)
    rows_by_code = load_bulk_rows(bulk_dir)
    results = []
    for code, rows in rows_by_code.items():
        if len(rows) < 25:
            continue
        c = _score_one(code, rows, env_ok, env_up_ratio)
        if c:
            results.append(c)
    results.sort(key=lambda x: -x["score"])
    return results[:top_n]


def main():
    ap = argparse.ArgumentParser(description="打板选股算法 (统计驱动)")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--up-ratio", type=float, default=None, help="大盘上涨占比 0-1")
    ap.add_argument("--zt-today", type=int, default=300, help="当日涨停家数 (环境闸门)")
    ap.add_argument("--zt-20avg", type=float, default=300.0, help="涨停家数20日均值 (环境闸门)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "pick_algo.json"))
    args = ap.parse_args()

    res = scan_bulk(args.bulk, min_score=args.min_score, top_n=args.top,
                    env_up_ratio=args.up_ratio, ztpool_today=args.zt_today,
                    ztpool_20avg=args.zt_20avg)
    payload = {"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
               "source": f"bulk day_qfq ({os.path.basename(args.bulk or BULK)})",
               "env": {"zt_today": args.zt_today, "zt_20avg": args.zt_20avg,
                       "up_ratio": args.up_ratio,
                       "env_ok": env_gate(args.zt_today, args.zt_20avg)},
               "min_score": args.min_score,
               "n": len(res),
               "picks": res,
               "stats_ref": "PROMOTE/NEXTRET 常量来自 qwerzxcva/stocks-algo 实测统计 (168,368 首板样本)",
               "note": "算法逻辑与上游一致, 仅数据源换成本地 bulk; 不构成投资建议"}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[pick_algo] {len(res)} 只候选 (min_score={args.min_score})")
    for p in res[:10]:
        print(f"  {p['code']} score={p['score']} height={p['checks'].get('height')} "
              f"vr={p['checks'].get('vol_ratio')} | {' / '.join(p['reasons'][:2])}")
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
