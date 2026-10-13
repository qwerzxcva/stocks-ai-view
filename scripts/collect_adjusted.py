#!/usr/bin/env python3
"""
collect_adjusted.py — 股票 + ETF 复权数据采集 (qfq 前复权 / hfq 后复权)

接口实测结论 (2026-10-13):
  - 个股/ETF:  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sh|sz}{code},day,,,{n},{qfq|hfq}
      qfq → data.{code}.qfqday, hfq → data.{code}.hfqday   ✅ 两者都可用
      注意: hfq 偶发返回 0 根 (重试即可), qfq 偶尔多返回 1 根 (当日未定盘)
  - 指数:     https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,{end},{n}
      fqkline 对指数返回空 → 必须用 kline/kline; 指数无复权概念 (只有 day)
  - 分页: kline/kline 的 end 必须 YYYY-MM-DD, 返回数组升序, 最早在 seg[0][0]

产出:
  data/adjusted/{code}_{adj}.json     单只全量复权
  data/adjusted_index.json            指数全史 (无复权)
  DuckDB kline_daily 表 (带 adj 列)

用法:
  python3 collect_adjusted.py --code 600519 --adj both
  python3 collect_adjusted.py --code 510500 --adj both        # ETF
  python3 collect_adjusted.py --batch-file codes.txt --adj qfq
  python3 collect_adjusted.py --index                          # 只采指数
  python3 collect_adjusted.py --to-db --code 600519            # 写入 DuckDB
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), "data", "adjusted")

INDEXES = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sh000300": "沪深300",
    "sz399006": "创业板指", "sh000688": "科创50", "sh000016": "上证50",
    "sh000905": "中证500", "sh000852": "中证1000",
}

# ETF 常见代码前缀: 51/56/58/159/511/513/515/518/520/522/525/528/558/562/563
ETF_PREFIX = ("51", "56", "58", "159", "511", "513", "515", "518",
              "520", "522", "525", "528", "558", "562", "563")


def _get(url, retries=3, timeout=20):
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                       timeout=timeout)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                print(f"    [http-fail] {type(e).__name__} {url[:70]}", file=sys.stderr)
            time.sleep(0.5)
    return {}


def _tk(code):
    """6 位代码 → 腾讯代码 (sh/sz 前缀)。"""
    code = str(code).strip()
    if code.startswith(("sh", "sz")):
        return code
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def fetch_fqkline(code, adj="qfq", n=800, max_pages=12):
    """个股/ETF 复权日K, 分页拉全量。返回 [[date,o,c,h,l,v],...]"""
    tk = _tk(code)
    allr, end, guard = {}, "", 0
    while guard < max_pages:
        guard += 1
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             f"?param={tk},day,,{end},{n},{adj}")
        d = _get(u)
        node = (d.get("data") or {}).get(tk) or {}
        arr = node.get("qfqday") or node.get("hfqday") or []
        if not arr:
            if guard == 1:
                print(f"    [warn] {tk} {adj} 首页空 (重试或该码无复权数据)",
                      file=sys.stderr)
            break
        new = 0
        for r in arr:
            if len(r) >= 6 and r[0] not in allr:
                allr[r[0]] = [r[0], float(r[1]), float(r[2]),
                              float(r[3]), float(r[4]), float(r[5])]
                new += 1
        if new == 0:
            break
        oldest = arr[0][0]          # 数组升序, 首行最早
        if oldest <= "1991-01-01" or len(arr) < n:
            break
        end = oldest
        time.sleep(0.15)
    return [allr[k] for k in sorted(allr)]


def fetch_index_kline(code, n=800, max_pages=15):
    """指数日K (无复权), 用 kline/kline 接口。"""
    tk = _tk(code)
    allr, end, guard = {}, "", 0
    while guard < max_pages:
        guard += 1
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
             f"?param={tk},day,,{end},{n}")
        d = _get(u)
        node = (d.get("data") or {}).get(tk) or {}
        arr = node.get("day") or []
        if not arr:
            break
        new = 0
        for r in arr:
            if len(r) >= 6 and r[0] not in allr:
                allr[r[0]] = [r[0], float(r[1]), float(r[2]),
                              float(r[3]), float(r[4]), float(r[5])]
                new += 1
        if new == 0:
            break
        oldest = arr[0][0]
        if oldest <= "1991-01-01" or len(arr) < n:
            break
        end = oldest
        time.sleep(0.15)
    return [allr[k] for k in sorted(allr)]


def _is_etf(code):
    c = str(code).strip()
    return c.startswith(ETF_PREFIX)


def validate_bars(bars, adj, code):
    """复权数据合理性校验。返回 (ok, issues)。

    已发现的问题 (2026-10-13 实测):
      腾讯 qfq 对超长期高分红股票会算出负价 (茅台 2001 年 qfq 开=-314.3)
      → 前复权历史价出现 <=0 即为异常, 应换源
    """
    issues = []
    if not bars:
        return False, ["空数据"]
    neg = [b for b in bars if b[1] <= 0 or b[2] <= 0]
    if neg:
        issues.append(f"{len(neg)}/{len(bars)} 根价格 <=0 (首根异常日 {neg[0][0]})")
    # 前复权: 末日价应等于当前实际价 (接口 qt 里), 后复权末日价会更高
    # 高低价关系检查
    bad_hl = [b for b in bars if b[3] < b[4]]   # high < low
    if bad_hl:
        issues.append(f"{len(bad_hl)} 根 high<low")
    bad_oc = [b for b in bars if b[1] > b[3] or b[2] > b[3]]  # open/close > high
    if len(bad_oc) > len(bars) * 0.01:
        issues.append(f"{len(bad_oc)} 根 open/close>high")
    return (len(issues) == 0), issues


def save_one(code, adj, bars, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    kind = "etf" if _is_etf(code) else "stock"
    p = os.path.join(out_dir, f"{code}_{adj}.json")
    payload = {
        "timestamp": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "tencent_fqkline",
        "code": code, "tk_code": _tk(code), "adj": adj,
        "asset_type": kind, "n": len(bars),
        "span": [bars[0][0], bars[-1][0]] if bars else None,
        "bars": bars,
        "meta": {
            "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            "adj_note": {"qfq": "前复权 (以最新价为基准向前调整)",
                         "hfq": "后复权 (以上市首日价为基准向后调整)"}.get(adj, ""),
            "encoding": "utf-8",
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return p


def to_db(code, adj, bars):
    """写入 DuckDB kline_daily (带 adj 列)。"""
    try:
        import duckdb
    except ImportError:
        print("需要 duckdb", file=sys.stderr)
        return 0
    sys.path.insert(0, HERE)
    import db_layer
    con = db_layer.connect()
    con.execute(db_layer.SCHEMA)
    for idx in db_layer.INDEXES:
        try:
            con.execute(idx)
        except Exception:
            pass
    # kline_daily 原表无 adj 列 → 加列 (DuckDB ADD COLUMN 幂等)
    try:
        con.execute("ALTER TABLE kline_daily ADD COLUMN IF NOT EXISTS adj VARCHAR")
    except Exception:
        pass
    rows = [(code, b[0], b[1], b[2], b[3], b[4], b[5], b[0][:4], adj) for b in bars]
    if rows:
        # kline_daily 无唯一约束, DuckDB 不支持裸 INSERT OR REPLACE → 先删后插
        con.execute("DELETE FROM kline_daily WHERE code = ? AND adj = ?", [code, adj])
        con.executemany(
            "INSERT INTO kline_daily "
            "(code, date, open, close, high, low, volume, year, adj) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    n = con.execute("SELECT count(*) FROM kline_daily").fetchone()[0]
    con.close()
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", action="append", help="代码 (可多次); 不带 sh/sz 前缀")
    ap.add_argument("--batch-file", help="代码清单文件 (每行一个)")
    ap.add_argument("--adj", default="both", choices=["qfq", "hfq", "both", "none"],
                    help="复权类型, none=不复权")
    ap.add_argument("--index", action="store_true", help="只采指数全史")
    ap.add_argument("--out-dir", help="输出目录")
    ap.add_argument("--to-db", action="store_true", help="同时写 DuckDB")
    ap.add_argument("--max-pages", type=int, default=12)
    args = ap.parse_args()

    out_dir = args.out_dir or DEFAULT_OUT
    codes = list(args.code or [])
    if args.batch_file:
        with open(args.batch_file, encoding="utf-8") as f:
            codes += [l.strip() for l in f if l.strip() and not l.startswith("#")]

    adjs = ["qfq", "hfq"] if args.adj == "both" else \
           ([] if args.adj == "none" else [args.adj])

    if args.index:
        print("=== 指数全史 (无复权) ===")
        all_idx = {}
        for code, name in INDEXES.items():
            bars = fetch_index_kline(code, max_pages=15)
            if bars:
                all_idx[code] = {"name": name, "n": len(bars), "span": [bars[0][0],
                                                                        bars[-1][0]],
                                 "bars": bars}
                print(f"  {code} {name}: {len(bars)} 根 "
                      f"({bars[0][0]} ~ {bars[-1][0]})")
            else:
                print(f"  {code} {name}: 空", file=sys.stderr)
            time.sleep(0.2)
        p = os.path.join(out_dir, "index_full.json")
        os.makedirs(out_dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now(CST).isoformat(timespec="seconds"),
                       "source": "tencent_kline_kline", "indices": all_idx,
                       "note": "指数无复权概念; 用 kline/kline 接口 (fqkline 对指数返回空)"},
                      f, ensure_ascii=False, separators=(",", ":"))
        print(f"→ {p} ({os.path.getsize(p)}B)")
        return 0

    if not codes:
        ap.print_help()
        return 1

    print(f"=== 复权采集 {len(codes)} 只 × adj={args.adj} ===")
    ok, fail = [], []
    for code in codes:
        kind = "ETF" if _is_etf(code) else "股票"
        for adj in adjs or ["none"]:
            if adj == "none":
                bars = fetch_index_kline(code, max_pages=args.max_pages) \
                    if code in INDEXES else []
                # 个股无复权 = 用 fqkline 的 day (接口不直接支持, 跳过)
                print(f"  [skip] {code} none: 个股/ETF 不复权请用 --adj qfq")
                continue
            bars = fetch_fqkline(code, adj, max_pages=args.max_pages)
            if not bars:
                fail.append((code, adj))
                print(f"  ❌ {code} {adj}: 空")
                continue
            ok_flag, issues = validate_bars(bars, adj, code)
            if not ok_flag:
                print(f"  ⚠️ {kind} {code} {adj}: 数据异常 {issues} (仍保存, 但需换源)")
                fail.append((code, adj, issues))
            p = save_one(code, adj, bars, out_dir)
            ok.append((code, adj, len(bars)))
            print(f"  ✅ {kind} {code} {adj}: {len(bars)} 根 "
                  f"({bars[0][0]} ~ {bars[-1][0]}) → {os.path.basename(p)}"
                  f"{' [校验通过]' if ok_flag else ''}")
            if args.to_db:
                n = to_db(code, adj, bars)
                print(f"     → DuckDB kline_daily 共 {n} 行")
            time.sleep(0.2)

    print(f"\n[done] 成功 {len(ok)} / 失败 {len(fail)}")
    if fail:
        print(f"  失败: {fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
