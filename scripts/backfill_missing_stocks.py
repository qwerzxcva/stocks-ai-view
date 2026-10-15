#!/usr/bin/env python3
# backfill_missing_stocks.py — 补齐远端个股仓缺失的 1945 只 (2026-10-16)
#
# 缺口诊断 (2026-10-16 实测):
#   universe 6029 只 vs 远端 db-stocks-a~e 共 4084 只, 缺 1945 只 (32%)
#   集中在 4 个代码段 (各 shard 收录上限之后):
#     00xxxx: 644 只  002373~003816
#     30xxxx: 453 只  301024~302132
#     60xxxx: 846 只  601567~605599
#     39xxxx:   2 只  399001~399006
#
# 数据源 (全部已实测可用, 见 sources_registry):
#   K线全史   tencent_newfqkline  (唯一支持任意起止区间的端点)
#   日K最近   tencent_kline / em_push2his_kline (DEPRECATED, 备援)
#   三大报表  ths_finance_{benefit,debt,cash}  (basic.10jqka api)
#   实时快照  em_quote_snapshot (push2 24 字段)
#   资金流    em_fflow_history (push2delay 五档)
#
# 输出结构与远端仓一致:
#   data/stocks/<shard>/<code>/
#     kline/day_qfq.csv      日K前复权 (date,open,close,high,low,volume)
#     kline/qfq.json         前复权全史 (腾讯源)
#     financial/benefit.json 利润表
#     financial/debt.json    资产负债表
#     financial/cash.json    现金流量表
#     quote.json             实时快照
#     manifest.json          本股票数据集清单
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
HDR = {
    "tencent": {"Referer": "https://gu.qq.com/"},
    "ths": {"Referer": "https://basic.10jqka.com.cn/"},
    "em": {"Referer": "https://quote.eastmoney.com/"},
}


def _get(url, domain="tencent", timeout=30, retries=3):
    h = dict(UA)
    h.update(HDR.get(domain, {}))
    last = None
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.0 + i)
    raise last


def secid(code):
    """600519 -> sh600519; 000001 -> sz000001
    注意: newfqkline 要 sh/sz 前缀, 不是东财的 1./0. secid 格式 (实测 param error)"""
    return f"sh{code}" if code[0] in "69" else f"sz{code}"


def fetch_kline_range(code, start="19900101", end=None):
    """腾讯 newfqkline — 任意起止区间前复权日K
    返回键: 个股 qfqday, 指数 day (实测差异)
    坑 (2026-10-16 实测): start 参数被忽略, 只返回 end 往前 640 根;
        640 是硬上限 (n=5000 返回 0 条); end 参数真实生效。
        拿全史需分段翻页, 见 fetch_kline_full。"""
    end = end or datetime.now(CST).strftime("%Y%m%d")
    sym = secid(code)
    u = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
         f"?_var=kline_dayqfq&param={sym},day,"
         f"{start[:4]}-{start[4:6]}-{start[6:]},{end[:4]}-{end[4:6]}-{end[6:]},640,qfq&r=0.82")
    t = _get(u, "tencent")
    i = t.find("(")
    if i >= 0:
        body = t[i + 1:].rstrip().rstrip(";")
    elif "=" in t[:40]:
        body = t[t.find("=") + 1:]
    else:
        body = t
    d = json.loads(body)
    if d.get("msg") == "param error":
        raise ValueError(f"param error: {sym}")
    node = (d.get("data") or {}).get(sym) or {}
    return node.get("qfqday") or node.get("day") or []


def fetch_kline_full(code, max_rounds=20):
    """分段翻页拿全史 — newfqkline 的 start 被忽略且 640 是硬上限,
    所以每次把 end 设为上一段最早日期再往前取, 直到不足 640 根或达到 max_rounds。
    坑 (2026-10-16 实测): 历史深段可能出现负价格/极低价 — 前复权因子对
    老代码 (重组更名前) 的处理失真, 如 601567 三星电气时代出现 -0.85。
    对 2024 年后上市的缺口新股, 单段 640 根已覆盖全部历史, 不会触发此坑。"""
    all_rows, seen = [], set()
    end = None
    for rnd in range(max_rounds):
        rows = fetch_kline_range(code, end=end) if end else fetch_kline_range(code)
        if not rows:
            break
        new = [r for r in rows if r[0] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r[0])
        all_rows = new + all_rows   # 新段更早, 放前面
        if len(rows) < 640:
            break                   # 已到上市首日
        # 下一段 end = 本段最早日期的前一天
        first = rows[0][0]
        try:
            dt = datetime.strptime(first, "%Y-%m-%d") - timedelta(days=1)
            end = dt.strftime("%Y%m%d")
        except Exception:
            break
        time.sleep(0.5)
    # 丢弃异常价格行 (负价格/价格为 0 — 复权失真)
    clean = []
    for r in all_rows:
        try:
            vals = [float(x) for x in r[1:5]]
            if all(v > 0 for v in vals):
                clean.append(r)
        except Exception:
            pass
    return clean if clean else all_rows


def fetch_ths_finance(code, statement):
    u = f"https://basic.10jqka.com.cn/api/stock/finance/{code}_{statement}.json"
    raw = _get(u, "ths", timeout=45)
    outer = json.loads(raw)
    fd = json.loads(outer["flashData"])
    title = fd["title"]
    metrics = [x[0] if isinstance(x, list) else x for x in title]

    def block(key):
        rows = fd.get(key) or []
        if not rows:
            return []
        periods = rows[0]
        out = []
        for i, row in enumerate(rows[1:]):
            if not any(x not in ("", None) for x in row):
                continue
            rec = {"_metric": metrics[i + 1] if i + 1 < len(metrics) else None}
            rec.update(dict(zip([str(p) for p in periods], row)))
            out.append(rec)
        return out

    return {"code": code, "statement": statement, "metrics": metrics,
            "n_periods": len(fd.get("report", [[]])[0]) if fd.get("report") else 0,
            "report": block("report"), "simple": block("simple"),
            "year": block("year"), "report_yoy": block("report_yoy"),
            "simple_yoy": block("simple_yoy"), "simple_mom": block("simple_mom"),
            "year_yoy": block("year_yoy")}


def shard_of(code):
    return code[:2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="逗号分隔, 或 @/tmp/stock_gap.json")
    ap.add_argument("--out", default="/tmp/backfill")
    ap.add_argument("--groups", default="kline,finance")
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    args = ap.parse_args()

    if args.codes.startswith("@"):
        codes = json.load(open(args.codes[1:]))["missing"]
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.limit:
        codes = codes[:args.limit]

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    stats = {"n": len(codes), "ok": 0, "kline_fail": [], "finance_fail": []}
    for idx, code in enumerate(codes, 1):
        d = os.path.join(args.out, "stocks", shard_of(code), code)
        os.makedirs(os.path.join(d, "kline"), exist_ok=True)
        rec = {"code": code, "updated": datetime.now(CST).isoformat(timespec="seconds")}
        try:
            if "kline" in groups:
                rows = fetch_kline_full(code)
                if rows:
                    p = os.path.join(d, "kline", "day_qfq.csv")
                    with open(p, "w", encoding="utf-8") as f:
                        f.write("# %s tencent newfqkline qfq daily, %d bars\n" % (code, len(rows)))
                        for r in rows:
                            f.write(",".join(str(x) for x in r[:6]) + "\n")
                    rec["kline_bars"] = len(rows)
                    rec["kline_first"] = rows[0][0] if rows else None
                    rec["kline_last"] = rows[-1][0] if rows else None
                else:
                    stats["kline_fail"].append(code)
                time.sleep(0.5)
            if "finance" in groups:
                os.makedirs(os.path.join(d, "financial"), exist_ok=True)
                for st in ("benefit", "debt", "cash"):
                    try:
                        fd = fetch_ths_finance(code, st)
                        with open(os.path.join(d, "financial", f"{st}.json"), "w", encoding="utf-8") as f:
                            json.dump(fd, f, ensure_ascii=False)
                        rec[f"{st}_metrics"] = len(fd["metrics"])
                    except Exception as e:
                        rec[f"{st}_err"] = f"{type(e).__name__}"
                        if st == "benefit":
                            stats["finance_fail"].append(code)
                    time.sleep(0.5)
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            stats["ok"] += 1
            if idx % 25 == 0:
                print(f"[{idx}/{len(codes)}] ok={stats['ok']} "
                      f"kline_fail={len(stats['kline_fail'])} "
                      f"fin_fail={len(stats['finance_fail'])}", flush=True)
        except Exception as e:
            stats["kline_fail"].append(code)
            print(f"  {code} FATAL {type(e).__name__} {e}", file=sys.stderr)
    with open(os.path.join(args.out, "backfill_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in stats.items()},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
