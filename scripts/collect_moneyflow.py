#!/usr/bin/env python3
"""
collect_moneyflow.py — 主力资金实时采集 (分钟级)

数据源 (2026-10-14 实测):
  1. push2.eastmoney.com/api/qt/stock/fflow/kline/get?klt=1   ← 分钟级, 可用
  2. push2his.eastmoney.com/.../fflow/daykline/get?klt=101    ← 日级, 可用
  3. push2delay.eastmoney.com/...                             ← 兜底 (ACAO=*)
  4. push2.eastmoney.com/api/qt/ulist.np/get                  ← 大盘指数资金流

实测结论:
  - 分钟级 klines 字段顺序: 时间, 主力净额, 超大单净, 大单净, 中单净, 小单净
    (2026-10-14 13:56,79995642.0,-485672.0,-79509965.0,-76081.0,80071723.0)
  - 大盘 f62=主力净额(元), f184=主力净占比(%)
  - 腾讯 ff_ 前缀不可用 (返回 v_pv_none_match)
  - 新浪 MoneyFlow 不可用 (Input error)

用法:
  python3 collect_moneyflow.py --out data/moneyflow.json
  python3 collect_moneyflow.py --codes 600519,000001 --top 20
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TIMEOUT = 6

# 默认关注: AI 推荐 + 场景候选 (由调用方通过 --codes 覆盖)
INDEX_SECIDS = [("1.000001", "上证指数"), ("0.399001", "深证成指"), ("1.000688", "科创50")]


def secid_of(code):
    c = str(code).zfill(6)
    return ("1." if c[:2] in ("60", "68", "90") else "0.") + c


def jget(url, retries=2):
    """GET JSON, 失败重试。东财 push2* 会间歇性 WAF
    (RemoteDisconnected / 空响应), 同 URL 重试一次常能过。"""
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception:
            if i < retries:
                time.sleep(0.4 * (i + 1))
    return None


def fetch_minute_flow(code, lmt=0):
    """分钟级主力资金流。

    注意: push2 的 fflow/kline 端点 2026-10-14 实测被 WAF 挡
    (RemoteDisconnected), 所以分钟级走 push2his 的 klt=1;
    push2his 的 klt=101 是日级。
    """
    fields = "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    urls = [
        "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
        "?secid=%s&lmt=%d&klt=1%s" % (secid_of(code), lmt, fields),
        "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
        "?secid=%s&lmt=%d&klt=1%s" % (secid_of(code), lmt, fields),
    ]
    for u in urls:
        d = jget(u)
        kl = ((d or {}).get("data") or {}).get("klines") or []
        if not kl:
            continue
        out = []
        for row in kl:
            f = row.split(",")
            if len(f) < 6:
                continue
            out.append({
                "time": f[0],
                "main_net": float(f[1]),
                "xlarge": float(f[2]),
                "large": float(f[3]),
                "medium": float(f[4]),
                "small": float(f[5]),
            })
        if out:
            return out
    return []


def fetch_day_flow(code, lmt=5):
    """日级主力资金流 (算 5 日累计)。返回 [{date, main_net, ...}]"""
    fields = "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    for host in ("push2his.eastmoney.com", "push2delay.eastmoney.com"):
        u = ("https://%s/api/qt/stock/fflow/daykline/get?secid=%s&lmt=%d&klt=101%s"
             % (host, secid_of(code), lmt, fields))
        d = jget(u)
        kl = ((d or {}).get("data") or {}).get("klines") or []
        if kl:
            out = []
            for row in kl:
                f = row.split(",")
                if len(f) < 2:
                    continue
                out.append({"date": f[0], "main_net": float(f[1])})
            return out
    return []


def fetch_realtime(code):
    """实时快照: 主力净额 f62 + 净占比 f184 + 价格 f43。"""
    u = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
         "&fields=f43,f57,f58,f62,f164,f184" % secid_of(code))
    d = jget(u)
    dd = (d or {}).get("data") or {}
    if not dd:
        return {}
    return {
        "price": (dd.get("f43") or 0) / 100 if dd.get("f43") else None,
        "name": dd.get("f58"),
        "main_net": dd.get("f62"),
        "main_net_pct": dd.get("f184"),
    }


def fetch_index_flow():
    """大盘指数资金流。"""
    secids = ",".join(s for s, _ in INDEX_SECIDS)
    u = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=%s"
         "&fields=f12,f14,f2,f3,f62,f184" % secids)
    d = jget(u)
    out = []
    for it in ((d or {}).get("data") or {}).get("diff") or []:
        name = it.get("f14")
        out.append({
            "code": it.get("f12"),
            "name": name,
            "price": it.get("f2"),
            "chg_pct": it.get("f3"),
            "main_net": it.get("f62"),
            "main_net_pct": it.get("f184"),
        })
    return out


def load_codes(n=20):
    """默认关注股: AI 推荐 + 场景候选, 各取前 n 只。"""
    codes = []
    for f, key in (("ai_pick_v4.json", "picks"), ("scenario.json", "picks"),
                   ("multidim.json", "top"), ("decision.json", "cards")):
        p = os.path.join(DATA_DIR, f)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for r in (d.get(key) or [])[:n]:
            c = r.get("code")
            if c:
                codes.append(str(c).zfill(6))
    # 去重保序
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "moneyflow.json"))
    ap.add_argument("--codes", default="", help="逗号分隔股票代码; 缺省从池文件取")
    ap.add_argument("--top", type=int, default=20, help="每只股票最多取几只")
    ap.add_argument("--no-index", action="store_true", help="跳过大盘资金流")
    args = ap.parse_args()

    if args.codes.strip():
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        codes = load_codes(args.top)

    result = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "collect_moneyflow v1 (eastmoney push2 minute-level)",
        "source": "eastmoney push2",
        "n_codes": len(codes),
        "stocks": {},
        "indices": [],
        "top_inflow": [],
        "top_outflow": [],
        "meta": {
            "fields": "klines = 时间,主力净额,超大单,大单,中单,小单 (元)",
            "units": "元; 前端 fmtYi 自动转亿/万",
            "caveat": "东财主力分类口径, 与其它券商可能不同; Research reference only",
        },
    }

    if not args.no_index:
        result["indices"] = fetch_index_flow()
        print("[mf] 大盘资金流 %d 个指数" % len(result["indices"]))

    ok = fail = 0
    for c in codes:
        rt = fetch_realtime(c)
        ms = fetch_minute_flow(c)
        if not ms and not rt:
            fail += 1
            continue
        ok += 1
        day = fetch_day_flow(c, 5)
        # 当日累计 = 分钟序列最后一条的 main_net (东财已是累计值)
        main_today = ms[-1]["main_net"] if ms else rt.get("main_net")
        # 净占比用 f184 (东财官方口径 = 主力净额/成交额)。
        # 不用分单绝对值之和自算: xlarge/large/medium/small 是各分类"净额",
        # 其绝对值之和远小于总成交额, 自算会把占比放到 40%+ 量级 (实测)。
        main_pct = rt.get("main_net_pct")
        main_5d = sum(d["main_net"] for d in day) if day else None
        result["stocks"][c] = {
            "name": rt.get("name") or "",
            "price": rt.get("price"),
            "main_net_today": main_today,
            "main_net_pct": main_pct,
            "main_net_5d": main_5d,
            "minute_series": ms[-60:],   # 只留最近 60 个分钟点, 防文件过大
        }
        # 限速: push2 频繁请求会 WAF
        time.sleep(0.15)

    # 排行
    ranked = sorted(
        [{"code": c, "name": s.get("name", ""), "main_net": s.get("main_net_today") or 0}
         for c, s in result["stocks"].items()],
        key=lambda x: -x["main_net"])
    result["top_inflow"] = [r for r in ranked if r["main_net"] > 0][:20]
    result["top_outflow"] = [r for r in ranked if r["main_net"] < 0][::-1][:20]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    print("[mf] 个股 %d 成功 / %d 失败" % (ok, fail))
    print("[mf] 净流入 TOP3: %s" % ", ".join(
        "%s %s %.1f亿" % (r["code"], r["name"], r["main_net"] / 1e8) for r in result["top_inflow"][:3]))
    print("[mf] 净流出 TOP3: %s" % ", ".join(
        "%s %s %.1f亿" % (r["code"], r["name"], r["main_net"] / 1e8) for r in result["top_outflow"][:3]))
    print("[mf] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
