#!/usr/bin/env python3
"""
realtime_minute.py — 分钟级实时采集器 (常驻服务, 云服务器用)

为什么需要它 (2026-10-14):
  GitHub Actions cron 最快 5 分钟, 且每次 workflow 启动 + checkout + pip install
  要 ~40-60 秒 → 实际延迟 5-7 分钟。要降到分钟级必须常驻进程。

架构:
  本脚本在云服务器 (GitCode Notebook / 独立服务器) 常驻运行:
    1. 交易时段 (09:25-11:30, 13:00-15:00) 每 60 秒采集一轮
    2. 采集: 东财 push2delay 实时快照 (全市场) + 腾讯分时 + 五档资金流
    3. 落盘: data/market/snapshot_{ts}.json (滚动保留最近 N 个)
    4. 推送: 每 N 轮 commit+push 到 GitHub/GitCode (默认每 5 轮 = 5 分钟,
            避免 push 太频繁触发 GitHub 风控; 页面读 latest.json 即可)

数据源 (全部实测可用, 见 sources_registry):
  - push2delay.eastmoney.com 全市场快照 (绕开 push2 WAF)
  - push2delay.eastmoney.com 个股实时 (24 字段)
  - web.ifzq.gtimg.cn 分时 (每分钟)

用法:
  # 前台跑 (调试)
  python3 realtime_minute.py --once            # 只采一轮
  python3 realtime_minute.py --interval 60     # 每 60 秒一轮

  # 常驻 (云服务器)
  nohup python3 realtime_minute.py --interval 60 --push-every 5 > /tmp/rt.log 2>&1 &

  # systemd (推荐)
  #   sudo cp scripts/realtime_minute.service /etc/systemd/system/
  #   sudo systemctl enable --now stocks-realtime
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
EM_REF = {"Referer": "https://quote.eastmoney.com/"}

def _repo_root():
    """本脚本所在仓库根 (scripts/ 的上一级)。

    不能硬编码 /root/workspace/stocks-ai-view: 在 GitHub runner 上该路径
    不存在, 写快照会 PermissionError, 而输出文件也会落到错误位置,
    导致 workflow 读到仓里旧的 realtime_minute.json 就认为"无变化"
    不提交 (2026-10-14 实测: realtime-minute 连续 success 但数据
    一直停在 00:43)。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


AI_VIEW = os.environ.get("STOCKS_AI_VIEW") or _repo_root()
SNAP_DIR = os.path.join(AI_VIEW, "data", "market")
DEFAULT_OUT = os.path.join(AI_VIEW, "data", "realtime_minute.json")
CURVE_PATH = os.path.join(AI_VIEW, "data", "market", "amount_curve.json")
LEADERS_PATH = os.path.join(AI_VIEW, "data", "market", "speed_leaders.json")
ALERTS_PATH = os.path.join(AI_VIEW, "data", "price_alerts.json")
ALERT_EVENTS_PATH = os.path.join(AI_VIEW, "data", "market", "alert_events.json")
KEY_LEVELS_PATH = os.path.join(AI_VIEW, "data", "key_levels.json")

# 交易时段 (CST)
TRADING = [(9 * 60 + 25, 11 * 60 + 30), (13 * 60 + 0, 15 * 60 + 0)]

# 限流 (与 rate_limit.py 对齐)
# 东财限流实测 (2026-10-14): QPS=10 时连续请求会被 RemoteDisconnected
# (服务端直接断开), 表现为只采到前几页。降到 2 QPS + 单页重试。
QPS_EM = 2.0
PAGE_RETRIES = 3


def _now():
    return datetime.now(CST)


def _in_trading(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return any(a <= mins <= b for a, b in TRADING)


def _get(url, ref=None, retries=2, timeout=20):
    h = dict(UA)
    h.update(ref or {})
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                return {"__error__": f"{type(e).__name__} {e}"}
            time.sleep(0.5)
    return {"__error__": "unknown"}


def _get_raw(url, ref=None, retries=2, timeout=20):
    """取原始文本 (新浪返回的是 JSON 数组但可能带 BOM/非标准头)。"""
    h = dict(UA)
    h.update(ref or {})
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.5)
    return None


# ─── 采集: 全市场快照 (push2delay, 绕 WAF) ──────────────────
FIELDS = ("f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f17,f20,f21,f62,f184")
FIELD_MAP = {
    "f12": "code", "f14": "name", "f2": "price", "f3": "chg_pct",
    "f4": "chg_amt", "f5": "volume_hand", "f6": "amount", "f7": "amplitude",
    "f8": "turnover_rate", "f9": "pe_ttm", "f10": "volume_ratio",
    "f17": "open", "f20": "total_mktcap", "f21": "float_mktcap",
    "f62": "main_net_inflow", "f184": "main_net_ratio",
}


def _collect_sina(max_pages=60):
    """新浪全市场兜底 (node=hs_a 覆盖沪深 A 股)。

    单页 num 上限 100, 故需翻 60 页; 限流门槛与东财不同,
    东财两个域都被软限流时这条仍可能通 (2026-10-14 实测)。
    返回已折算成内部口径的 dict 列表。
    """
    out = []
    for page in range(1, max_pages + 1):
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"Market_Center.getHQNodeData?page={page}&num=100&sort=changepercent"
             "&asc=0&node=hs_a")
        b = _get_raw(u, {"Referer": "https://finance.sina.com.cn/"})
        if not b:
            break
        try:
            arr = json.loads(b)
        except Exception:
            break
        if not arr:
            break
        for it in arr:
            out.append({
                "code": str(it.get("code") or ""),
                "name": it.get("name") or "",
                "price": _f(it.get("trade")),
                "chg_pct": _f(it.get("changepercent")),
                "chg_amt": _f(it.get("pricechange")),
                "open": _f(it.get("open")),
                "amount": _f(it.get("amount")),
                "volume_hand": _f(it.get("volume")),
                "amplitude": _f(it.get("amplitude")),
                "turnover_rate": _f(it.get("turnoverratio")),
                "pe_ttm": _f(it.get("per")),
                "volume_ratio": None,
                "total_mktcap": None,
                "float_mktcap": None,
                "main_net_inflow": None,
                "main_net_ratio": None,
                "_src": "sina",
            })
        if len(arr) < 100:
            break
        time.sleep(0.8)
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_market_snapshot(max_pages=30):
    """全市场实时快照 (分页, pz=200/页)。

    三级降级: push2delay → push2his → 新浪。
    东财两个域在海外出口 (GitHub runner) 与高频请求下都会软限流 —
    表现为返回 200 但 diff 为空, 或直接断开连接。新浪
    vip.stock.finance.sina.com.cn 单页上限 100 只, 限流门槛不同,
    可作最后兜底 (2026-10-14 实测容器出口可用)。
    """
    def _collect(host):
        rows_, total_ = [], None
        for page in range(1, max_pages + 1):
            u = (f"https://{host}/api/qt/clist/get"
                 f"?pn={page}&pz=200&po=1&np=1"
                 "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                 f"&fields={FIELDS}&fid=f3")
            # 单页重试: 东财限流有两种表现 — 直接断开连接
            # (RemoteDisconnected), 或返回 200 但 diff 为空。两者都要重试,
            # 否则会静默少采几页 (实测 2026-10-14: 只拿到 500/5923)。
            d = None
            for attempt in range(PAGE_RETRIES):
                cand = _get(u, EM_REF, retries=1)
                if cand and not cand.get("__error__") and \
                        (cand.get("data") or {}).get("diff"):
                    d = cand
                    break
                time.sleep(1.5 * (attempt + 1))
            if d is None:
                print(f"[realtime_minute] {host} p{page} 连续 {PAGE_RETRIES} 次空/失败",
                      file=sys.stderr)
                break
            data = d.get("data") or {}
            total_ = data.get("total") or total_
            batch = data.get("diff") or []
            if not batch:
                break
            rows_.extend(batch)
            if total_ and len(rows_) >= total_:
                break
            # 页间隔 1.5s: 东财对连续翻页的软限流是"返回空 diff"而非报错,
            # 间隔太短会持续踩到 (QPS=2 + pz=200 实测仍只在第 3 页被掐)。
            time.sleep(1.5)
        return rows_, total_

    all_rows, total = _collect("push2delay.eastmoney.com")
    # 页数够但明显缺量 → 换域重采 (total 已知时以它为准判断)
    if total and len(all_rows) < min(total, 3000):
        alt, alt_total = _collect("push2his.eastmoney.com")
        if len(alt) > len(all_rows):
            print(f"[realtime_minute] push2delay 只返回 {len(all_rows)}/{total}, "
                  f"改用 push2his 得 {len(alt)}", file=sys.stderr)
            all_rows, total = alt, alt_total or total
    # 东财两个域都不给量 → 新浪兜底 (单页 100 只, 翻 60 页)
    if len(all_rows) < 3000:
        sina = _collect_sina(max_pages=60)
        if len(sina) > len(all_rows):
            print(f"[realtime_minute] 东财只返回 {len(all_rows)}, "
                  f"改用新浪得 {len(sina)}", file=sys.stderr)
            return len(sina), sina
    rows = []
    for it in all_rows:
        r = {}
        for k, v in it.items():
            r[FIELD_MAP.get(k, k)] = v
        # 单位折算 (东财 push2 字段口径, 实测 2026-10-14):
        #   f2 价/f4 涨跌额/f17 开盘 = 分 → 元 (÷100)
        #   f3 涨跌幅/f7 振幅/f8 换手/f9 PE/f10 量比/f184 主力净占比 = 1/100 (÷100)
        #   f62 主力净流入单位已是元
        for f in ("price", "chg_amt", "open"):
            if isinstance(r.get(f), (int, float)):
                r[f] = round(r[f] / 100, 3)
        for f in ("chg_pct", "amplitude", "turnover_rate", "pe_ttm",
                  "volume_ratio", "main_net_ratio"):
            if isinstance(r.get(f), (int, float)):
                v = round(r[f] / 100, 2)
                # 涨跌幅/振幅兜底: A股日涨跌停 20cm(科创/创业)/30cm(北交所)
                # 超 35% 必然是脏数据 (首日新股除外, 但占比极小, 宁可截断)
                if f in ("chg_pct", "amplitude") and abs(v) > 35:
                    v = max(-35.0, min(35.0, v))
                r[f] = v
        rows.append(r)
    return total, rows


def summarize(rows):
    """市场概览: 涨跌停/涨跌家数/成交额/主力净流入。"""
    up = down = flat = 0
    zt = dt = 0
    amt = 0.0
    main_in = 0.0
    for r in rows:
        c = r.get("chg_pct")
        if c is None:
            continue
        if c > 0:
            up += 1
        elif c < 0:
            down += 1
        else:
            flat += 1
        if c >= 9.9:
            zt += 1
        elif c <= -9.9:
            dt += 1
        a = r.get("amount")
        if isinstance(a, (int, float)):
            amt += a
        m = r.get("main_net_inflow")
        if isinstance(m, (int, float)):
            main_in += m
    return {
        "n_stocks": len(rows),
        "advance": up, "decline": down, "flat": flat,
        "limit_up_ge10": zt, "limit_down_le-10": dt,
        "total_amount_yi": round(amt / 1e8, 1),
        "main_net_inflow_yi": round(main_in / 1e8, 2),
    }


# ─── 量能曲线 + 个股异动榜 (回放页借鉴, 2026-10-17) ──────────
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def update_amount_curve(summary):
    """每轮追加一个全市场量能 tick; 跨交易日时把上一交易日归档为 prev。

    回放页的"今日 vs 昨日同时"对比需要昨日逐时基线, 而滚动快照只留最近
    12 个 (workflow 里还会裁到 12), 单日 tick (~80 个/交易日, 每个几十字节)
    单独持久化几乎不占体积。prev 只保留最近一个交易日, 防止文件无限增长。
    """
    now = _now()
    today_str = now.strftime("%Y-%m-%d")
    curve = _load_json(CURVE_PATH, {})
    today = curve.get("today") or {}
    if today.get("date") and today["date"] != today_str:
        curve["prev"] = today
        today = {}
    if today.get("date") != today_str:
        today = {"date": today_str, "ticks": []}
    today["ticks"].append({
        "t": now.strftime("%H:%M"),
        "amount_yi": summary.get("total_amount_yi"),
        "advance": summary.get("advance"),
        "decline": summary.get("decline"),
        "zt": summary.get("limit_up_ge10"),
        "dt": summary.get("limit_down_le-10"),
        "main_yi": summary.get("main_net_inflow_yi"),
    })
    curve["today"] = today
    curve["ts"] = now.isoformat(timespec="seconds")
    _atomic_write(CURVE_PATH, curve)


def _norm_code(code):
    """统一快照 code 口径: 东财 f12 是 6 位纯数字, 新浪兜底带 sh/sz 前缀。"""
    c = str(code or "")
    for p in ("sh", "sz", "bj"):
        if c.startswith(p) and c[2:].isdigit():
            return c[2:]
    return c


def load_latest_snapshot():
    """取最近一个滚动快照 (即上一轮的 quotes/timestamp/date), 供涨速差分。"""
    try:
        snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.startswith("snapshot_"))
    except OSError:
        return None, None, None
    if not snaps:
        return None, None, None
    payload = _load_json(os.path.join(SNAP_DIR, snaps[-1]), {})
    return payload.get("quotes") or [], payload.get("timestamp"), payload.get("date")


def compute_speed_leaders(rows, prev_rows, prev_date, prev_ts):
    """相邻两轮快照逐股求差: 涨速 = 涨跌幅之差, 区间成交 = 累计成交额之差。

    amount 是当日累计值, 相邻差即区间成交; 负差多为停牌或数据抖动, 丢弃。
    跨日快照不做差分: 昨收口径不同, 差出来的是隔夜跳空而非盘中涨速。
    """
    now = _now()
    interval_min = None
    if prev_ts:
        try:
            prev_dt = datetime.fromisoformat(prev_ts)
            interval_min = max(1, round((now - prev_dt).total_seconds() / 60))
        except ValueError:
            pass
    boards = {"gainers": [], "losers": [], "amount": []}
    if not prev_rows:
        return boards, {"interval_min": interval_min,
                        "note": "no previous snapshot", "n_compared": 0}
    if prev_date != now.strftime("%Y-%m-%d"):
        return boards, {"interval_min": interval_min,
                        "note": f"prev snapshot is {prev_date}", "n_compared": 0}
    prev_map = {}
    for r in prev_rows:
        c = r.get("code")
        if c:
            prev_map[_norm_code(c)] = r
    out = []
    for r in rows:
        p = prev_map.get(_norm_code(r.get("code")))
        if not p:
            continue
        speed = None
        if isinstance(r.get("chg_pct"), (int, float)) and \
                isinstance(p.get("chg_pct"), (int, float)):
            speed = round(r["chg_pct"] - p["chg_pct"], 2)
        amt_yi = None
        if isinstance(r.get("amount"), (int, float)) and \
                isinstance(p.get("amount"), (int, float)):
            d = r["amount"] - p["amount"]
            if d > 0:
                amt_yi = round(d / 1e8, 3)
        if speed is None and amt_yi is None:
            continue
        out.append({"code": r.get("code"), "name": r.get("name"),
                    "price": r.get("price"), "chg_pct": r.get("chg_pct"),
                    "speed": speed, "interval_amount_yi": amt_yi})
    ranked = [r for r in out if r["speed"] is not None]
    boards["gainers"] = sorted(ranked, key=lambda x: -x["speed"])[:15]
    boards["losers"] = sorted(ranked, key=lambda x: x["speed"])[:15]
    amt_ranked = [r for r in out if r["interval_amount_yi"]]
    boards["amount"] = sorted(amt_ranked, key=lambda x: -x["interval_amount_yi"])[:15]
    return boards, {"interval_min": interval_min, "note": None,
                    "n_compared": len(out)}


def write_speed_leaders(boards, meta):
    now = _now()
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "interval_min": meta.get("interval_min"),
        "note": meta.get("note"),
        "n_compared": meta.get("n_compared"),
        **boards,
    }
    _atomic_write(LEADERS_PATH, payload)


# ─── 点位提醒 (借鉴个股关键价位页「点位提醒」, 2026-10-17) ─────
def load_price_alerts():
    """手工点位 (data/price_alerts.json) + 关键价位自动层 (key_levels 枢轴 R1/S1)。

    手工格式: {"alerts": [{"code": "600001", "level": 12.5,
                            "direction": "above"/"below", "note": "..."}]}
    """
    alerts = []
    for a in (_load_json(ALERTS_PATH, {}) or {}).get("alerts") or []:
        try:
            level = float(a.get("level"))
        except (TypeError, ValueError):
            continue
        direction = "above" if a.get("direction") != "below" else "below"
        alerts.append({"key": f"manual|{_norm_code(a.get('code'))}|{level}|{direction}",
                       "code": _norm_code(a.get("code")), "level": level,
                       "dir": direction, "src": "manual",
                       "note": a.get("note")})
    for code, lv in ((_load_json(KEY_LEVELS_PATH, {}) or {}).get("stocks") or {}).items():
        piv = lv.get("pivot") or {}
        for k, d in (("r1", "above"), ("s1", "below")):
            if piv.get(k) is None:
                continue
            alerts.append({"key": f"auto|{_norm_code(code)}|pivot.{k}",
                           "code": _norm_code(code), "level": float(piv[k]),
                           "dir": d, "src": "key_levels.pivot",
                           "note": f"枢轴{k.upper()} {piv[k]}"})
    return alerts


def check_alerts(rows, prev_rows, alerts):
    """逐股比较本轮 vs 上轮现价: 只有「真穿越」才触发 (上轮在界限另一侧)。

    同日同 key 只报一次 (events 里去重), 防止横盘反复过线刷屏。
    """
    if not alerts or not prev_rows:
        return []
    prev_map = {}
    for r in prev_rows:
        c = _norm_code(r.get("code"))
        if c:
            prev_map[c] = r
    cur_map = {}
    for r in rows or []:
        c = _norm_code(r.get("code"))
        if c:
            cur_map[c] = r
    by_code = {}
    for a in alerts:
        by_code.setdefault(a["code"], []).append(a)
    now = _now()
    today_str = now.strftime("%Y-%m-%d")
    events = _load_json(ALERT_EVENTS_PATH, {}) or {}
    today = events.get("today") if events.get("date") == today_str else None
    if today is None:
        today = {"items": [], "fired": {}}
    fired = today.get("fired") or {}
    hit = []
    for code, al in by_code.items():
        cur = cur_map.get(code)
        prev = prev_map.get(code)
        if not cur or not prev:
            continue
        px, ppx = cur.get("price"), prev.get("price")
        if not isinstance(px, (int, float)) or not isinstance(ppx, (int, float)):
            continue
        for a in al:
            if fired.get(a["key"]):
                continue
            crossed = (px >= a["level"] > ppx) if a["dir"] == "above" \
                else (px <= a["level"] < ppx)
            if not crossed:
                continue
            ev = {"t": now.strftime("%H:%M"), "code": code,
                  "name": cur.get("name"), "price": px, "prev_price": ppx,
                  "level": a["level"], "dir": a["dir"], "src": a["src"],
                  "note": a.get("note")}
            hit.append(ev)
            fired[a["key"]] = True
            today["items"].append(ev)
    if hit:
        today["fired"] = fired
        events["date"] = today_str
        events["today"] = today
        events["ts"] = now.isoformat(timespec="seconds")
        # 只保留最近 300 条, 防文件无限增长
        if len(today["items"]) > 300:
            today["items"] = today["items"][-300:]
        _atomic_write(ALERT_EVENTS_PATH, events)
    return hit


# ─── 落盘 + 推送 ────────────────────────────────────────────
def write_output(rows, summary, total):
    ts = _now().isoformat(timespec="seconds")
    payload = {
        "timestamp": ts,
        "source": "realtime_minute (push2delay eastmoney)",
        "date": _now().strftime("%Y-%m-%d"),
        "in_trading": _in_trading(),
        "market_total": total,
        "summary": summary,
        "quotes": rows,
        "provenance": {
            "quotes": {"source": "push2delay.eastmoney.com/api/qt/clist/get",
                       "confidence": "high", "snapshot_ts": ts},
            "summary": {"source": "computed_from_quotes", "confidence": "high",
                        "snapshot_ts": ts},
        },
    }
    os.makedirs(os.path.dirname(DEFAULT_OUT), exist_ok=True)
    tmp = DEFAULT_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DEFAULT_OUT)   # 原子替换, 避免读到半截
    # 滚动快照 (保留最近 240 个 = 一天盘中)
    os.makedirs(SNAP_DIR, exist_ok=True)
    sp = os.path.join(SNAP_DIR, f"snapshot_{_now().strftime('%Y%m%d_%H%M')}.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.startswith("snapshot_"))
    for old in snaps[:-240]:
        os.remove(os.path.join(SNAP_DIR, old))
    return DEFAULT_OUT, sp


def push_to_git(repo_dir, remote="origin", branch="main"):
    """commit + push (rebase 处理并发)。"""
    ts = _now().strftime("%Y%m%d_%H%M")
    def _run(cmd):
        return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    _run(["git", "add", "data/"])
    _run(["git", "commit", "-m", f"realtime: {ts} minute snapshot"])
    _run(["git", "fetch", remote, branch])
    _run(["git", "rebase", f"{remote}/{branch}"])
    r = _run(["git", "push", remote, branch])
    if r.returncode != 0:
        # 并发冲突 → 再拉再推
        _run(["git", "fetch", remote, branch])
        _run(["git", "rebase", f"{remote}/{branch}"])
        r = _run(["git", "push", remote, branch])
    return r.returncode == 0


# ─── 主循环 ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="只采一轮")
    ap.add_argument("--interval", type=int, default=60, help="采集间隔秒 (默认 60)")
    ap.add_argument("--push-every", type=int, default=5,
                    help="每 N 轮推送一次 git (默认 5; 0=不推)")
    ap.add_argument("--off-hours", action="store_true",
                    help="非交易时段也采 (默认只交易时段采)")
    args = ap.parse_args()

    print(f"[realtime_minute] 启动 interval={args.args if False else args.interval}s "
          f"push_every={args.push_every}")
    print(f"[realtime_minute] 输出 {DEFAULT_OUT}")
    n_round = 0
    n_push = 0
    while True:
        n_round += 1
        ts = _now().strftime("%H:%M:%S")
        trading = _in_trading()
        if not trading and not args.off_hours and not args.once:
            print(f"[{ts}] 非交易时段, 休眠 {args.interval}s")
            time.sleep(args.interval)
            continue
        try:
            prev_rows, prev_ts, prev_date = load_latest_snapshot()
            total, rows = fetch_market_snapshot()
            if not rows:
                print(f"[{ts}] 第 {n_round} 轮: 空响应", file=sys.stderr)
                time.sleep(args.interval)
                continue
            summary = summarize(rows)
            out, snap = write_output(rows, summary, total)
            update_amount_curve(summary)
            boards, bmeta = compute_speed_leaders(rows, prev_rows, prev_date, prev_ts)
            write_speed_leaders(boards, bmeta)
            hits = check_alerts(rows, prev_rows, load_price_alerts())
            if hits:
                print(f"[{ts}] 点位触发 {len(hits)} 条: "
                      + ", ".join(f"{h['code']} {h['dir']}{h['level']}"
                                   for h in hits[:5]), file=sys.stderr)
            print(f"[{ts}] 第 {n_round} 轮: {summary['n_stocks']} 只 "
                  f"涨{summary['advance']}/跌{summary['decline']} "
                  f"涨停{summary['limit_up_ge10']} "
                  f"成交额{summary['total_amount_yi']}亿 "
                  f"主力净流入{summary['main_net_inflow_yi']}亿")
            n_push += 1
            if args.push_every and n_push >= args.push_every:
                ok = push_to_git(AI_VIEW)
                print(f"  → git push {'✅' if ok else '❌'}")
                n_push = 0
        except Exception as e:
            print(f"[{ts}] 第 {n_round} 轮失败: {type(e).__name__} {e}", file=sys.stderr)
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
