#!/usr/bin/env python3
"""
collect_klineshare.py — KlineShare 看板免 key 数据接入 CI

背景 (2026-10-14 逆向结论):
  klineshare.cn/v1 主站 API 全部 401 (需 X-API-Key)
  **hq.klineshare.cn/api/* 看板前端自用接口全免鉴权** ← 本脚本只用这批

实测可用 (免 key, 2026-10-14 复测确认):
  /api/realtime?symbol=600519.SH   实时快照 (OHLC/成交额/涨跌/昨收) ✅
  /api/plates                      板块列表 ✅ 19375 字节 JSON

实测不可用 (返回 HTML SPA fallback, 非 JSON):
  /api/limit-up /api/limit-down /api/break-limit-up
  /api/yesterday-limit-up /api/ladder /api/pcp /api/temperature
  → 破甲子代理初次探测时这些返回过 JSON, 但复测全部 200+HTML。
    可能是它当时带了不同的 Referer/cookie, 或服务端按请求特征分流。
    本脚本仍保留这些端点的调用代码 (ks_endpoint), 环境变化时可能恢复。

实测不可用:
  /api/kline  HTTP 500 (参数未调通, 可能需 key)

symbol 格式: 600519.SH / 000001.SZ / 000001.BJ (不是 sh600519!)

CLI 用法 (保留, 便于手动排查):
  python3 collect_klineshare.py --realtime 600519.SH
  python3 collect_klineshare.py --limit-up
  python3 collect_klineshare.py --temperature
  python3 collect_klineshare.py --collect          # CI 用: 一次采全部落盘

输出 (--collect):
  data/klineshare_hq.json  {ts, source, realtime:{}, limit_up:[], limit_down:[],
                            break_limit_up:[], ladder:[], pcp:{}, temperature:[],
                            plates:[]}
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

HQ = "https://hq.klineshare.cn/api"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://hq.klineshare.cn/"}


def http_json(url, timeout=15, retries=2):
    last = "unknown"
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace")), None
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
            if i < retries:
                time.sleep(0.5 * (i + 1))
    return None, last


def norm_symbol(s):
    """归一化成 600519.SH 风格 (看板不接受 sh600519)。"""
    s = str(s).strip()
    if "." in s:
        return s.upper()
    if s.startswith(("sh", "sz", "bj")) and len(s) == 8:
        pfx, code = s[:2], s[2:]
        return "%s.%s" % (code, {"sh": "SH", "sz": "SZ", "bj": "BJ"}[pfx])
    c = s.zfill(6)
    if c.startswith(("60", "68", "9")):
        return "%s.SH" % c
    return "%s.SZ" % c


# ─── 看板接口 ─────────────────────────────────────────────────
def ks_realtime(symbols):
    """批量实时快照。每只之间至少 1.2s 间隔 — 看板限流严格,
    连续快请求会返回 HTML (实测 30 只连续打第 3 只起就 fallback)。
    """
    out, errs = {}, []
    for i, s in enumerate(symbols):
        if i:
            time.sleep(1.2)
        sym = norm_symbol(s)
        d, err = http_json("%s/realtime?symbol=%s" % (HQ, urllib.parse.quote(sym)),
                            retries=1)
        if d is None:
            # 限流时再等 3s 试一次
            time.sleep(3.0)
            d, err = http_json("%s/realtime?symbol=%s" % (HQ, urllib.parse.quote(sym)),
                                retries=1)
        if d is None:
            errs.append("realtime(%s): %s" % (sym, err))
            continue
        out[sym] = d
    return out, errs


def ks_endpoint(path, params=None):
    """看板接口。限流严格: 连续请求会返回 HTML (SPA fallback) 而非 JSON,
    所以这里对非 JSON 响应显式重试并拉长间隔 (实测 0.5s 不够, 要 2s+)。
    """
    url = "%s/%s" % (HQ, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt, wait in enumerate((0, 2.0, 4.0)):
        if wait:
            time.sleep(wait)
        d, err = http_json(url, retries=1)
        if d is not None:
            return d, None
        # HTML fallback / 限流 → 继续重试
    return None, err


def collect(limit_symbols=8):
    """一次采全部免 key 接口, 返回结构化 dict。任何一级失败不影响其他。

    顺序有讲究: 先采 pools/temperature/pcp/plates (低频接口, 各 1 次),
    最后采 realtime (N 只 × 1.2s, 最耗时)。这样即使 realtime 被限流,
    重要的池数据已经拿到。
    """
    ts = datetime.now(CST).isoformat(timespec="seconds")
    reasons = []
    ok_sources = []

    # 1. 涨停/跌停/炸板/连板天梯 (各 1 次请求, 优先拿)
    pools = {}
    for key, path in (("limit_up", "limit-up"), ("limit_down", "limit-down"),
                      ("break_limit_up", "break-limit-up"),
                      ("yesterday_limit_up", "yesterday-limit-up"),
                      ("ladder", "ladder")):
        d, err = ks_endpoint(path)
        if d is None:
            reasons.append("%s: %s" % (key, err))
        else:
            pools[key] = d
            ok_sources.append(key)
        time.sleep(1.0)

    # 2. 涨跌幅分布 + 市场温度
    pcp, err = ks_endpoint("pcp")
    if pcp is None:
        reasons.append("pcp: %s" % err)
    else:
        ok_sources.append("pcp")
    time.sleep(1.0)
    temp, err = ks_endpoint("temperature")
    if temp is None:
        reasons.append("temperature: %s" % err)
    else:
        ok_sources.append("temperature")
    time.sleep(1.0)

    # 3. 板块列表
    plates, err = ks_endpoint("plates")
    if plates is None:
        reasons.append("plates: %s" % err)
    else:
        ok_sources.append("plates")

    # 4. 关注股实时快照 (最耗时, 放最后)
    symbols = []
    try:
        pdir = os.path.join(DATA_DIR, "predictions")
        if os.path.isdir(pdir):
            import glob as _g
            snaps = sorted(_g.glob(os.path.join(pdir, "engine_v5_*.json")))
            if snaps:
                d = json.load(open(snaps[-1], encoding="utf-8"))
                symbols = [r.get("code") for r in (d.get("top") or []) if r.get("code")]
    except Exception:
        pass
    if not symbols:
        symbols = ["600519", "000001", "600036", "300750", "002594",
                   "601318", "600900", "000858"]
    symbols = symbols[:limit_symbols]
    realtime, errs = ks_realtime(symbols)
    reasons.extend(errs)
    if realtime:
        ok_sources.append("realtime")

    return {
        "ts": ts,
        "engine": "collect_klineshare v1 (hq.klineshare.cn 免 key 看板接口)",
        "source": "hq.klineshare.cn",
        "n_symbols": len(symbols),
        "realtime": realtime,
        "pools": pools,
        "pcp": pcp,
        "temperature": temp,
        "plates": plates,
        "ok_sources": ok_sources,
        "reason": "; ".join(reasons) if reasons else None,
        "meta": {
            "symbol_format": "600519.SH (不接受 sh600519)",
            "free": "看板 /api/* 全免鉴权; 主站 klineshare.cn/v1 需 X-API-Key",
            "kline": "/api/kline 实测 500, 未调通, 暂不可用",
            "caveat": "Research reference only, not investment advice",
        },
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "klineshare_hq.json"))
    ap.add_argument("--collect", action="store_true",
                    help="一次采全部免 key 接口并落盘 (CI 用)")
    ap.add_argument("--realtime", nargs="*", default=None, metavar="SYMBOL",
                    help="实时快照")
    ap.add_argument("--kline", nargs="*", default=None, metavar="SYMBOL",
                    help="K 线 (实测 500, 保留)")
    ap.add_argument("--trend", nargs="*", default=None, metavar="SYMBOL",
                    help="当日分时")
    ap.add_argument("--batch", nargs="*", default=None, metavar="SYMBOL",
                    help="批量快照")
    ap.add_argument("--search", default=None, help="搜索股票/板块")
    ap.add_argument("--plates", action="store_true", help="板块列表")
    ap.add_argument("--plate-stocks", default=None, help="板块成分股")
    ap.add_argument("--limit-up", action="store_true", help="涨停池")
    ap.add_argument("--limit-down", action="store_true", help="跌停池")
    ap.add_argument("--break-limit-up", action="store_true", help="炸板池")
    ap.add_argument("--yesterday-limit-up", action="store_true", help="昨涨停池")
    ap.add_argument("--ladder", action="store_true", help="连板天梯")
    ap.add_argument("--pcp", action="store_true", help="涨跌幅分布")
    ap.add_argument("--temperature", action="store_true", help="市场温度")
    ap.add_argument("--us", nargs="*", default=None, metavar="SYMBOL", help="美股")
    ap.add_argument("--period", default=None)
    ap.add_argument("--count", type=int, default=None)
    args = ap.parse_args()

    # CI 模式
    if args.collect or not any([args.realtime, args.kline, args.trend, args.batch,
                                args.search, args.plates, args.plate_stocks,
                                args.limit_up, args.limit_down, args.break_limit_up,
                                args.yesterday_limit_up, args.ladder, args.pcp,
                                args.temperature, args.us]):
        out = collect()
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("[klineshare] %d 个接口可用: %s" % (len(out["ok_sources"]),
                                              ", ".join(out["ok_sources"])))
        print("[klineshare] realtime %d 只 | 涨停 %d | 温度点 %d" % (
            len(out.get("realtime") or {}),
            len((out.get("pools") or {}).get("limit_up") or []),
            len(((out.get("temperature") or {}).get("points")) or [])))
        if out.get("reason"):
            print("[klineshare] 部分失败: %s" % out["reason"][:200])
        print("[klineshare] -> %s" % args.out)
        return 0

    # CLI 单接口模式 (手动排查用)
    payload = {}
    if args.realtime:
        d, errs = ks_realtime(args.realtime)
        for k, v in d.items():
            payload["realtime:" + k] = v
        for e in errs:
            payload["realtime:" + e.split("(")[1].split(")")[0]] = {"error": e}
    if args.plates:
        payload["plates"], _ = ks_endpoint("plates")
    if args.limit_up:
        payload["limit_up"], _ = ks_endpoint("limit-up")
    if args.limit_down:
        payload["limit_down"], _ = ks_endpoint("limit-down")
    if args.break_limit_up:
        payload["break_limit_up"], _ = ks_endpoint("break-limit-up")
    if args.yesterday_limit_up:
        payload["yesterday_limit_up"], _ = ks_endpoint("yesterday-limit-up")
    if args.ladder:
        payload["ladder"], _ = ks_endpoint("ladder")
    if args.pcp:
        payload["pcp"], _ = ks_endpoint("pcp")
    if args.temperature:
        payload["temperature"], _ = ks_endpoint("temperature")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
