#!/usr/bin/env python3
"""build_stock_sector_map.py — 个股 → 板块 映射（供 App 的板块主线使用）

## 为什么需要单独做这个文件

云端目前有**三套互不对齐**的行业体系，实测都接不上 sector_engine：

| 文件 | 层级 | 规模 | 与 sector_engine(27 板块) 的匹配 |
|---|---|---|---|
| `industry_map_em.json` | 东财三级 | 1050 只 / 80 行业 | 8/27，只覆盖 66 只股票 |
| `industry_stocks.json` | 同上（另一视角） | 84 行业 | 同上 |
| db-market `industry.json` + `stock_to_board.json` | 东财一级 | 3864 只 / 100 板块 | 8/27（一级 vs 二级） |
| `concept_map.json` | 概念 | 180 概念 | 22/27，但**每板块只有 1 只代表股** |

都不是成分股清单。数字对不上是因为层级不同：「白酒」和「白酒Ⅲ」、「大众出版」和「出版」
是两套分类，用名字模糊匹配只会错配（把「家纺」并进「服装家纺」之类）。

## 做法

不猜名字，直接用东财的板块列表接口拿到**与 sector_engine 同一套**板块及其 BK 代码，
再按 BK 拉成分股——板块天然一一对应，全程不做名称匹配。

    GET push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:2   → 行业板块列表(f12=BK, f14=名称)
    GET push2.eastmoney.com/api/qt/clist/get?fs=b:{BK}     → 该板块成分股

## 输出 data/stock_sector_map.json

    {
      "ts": "...", "engine": "build_stock_sector_map v1",
      "n_sectors": 100, "n_stocks": 3800, "n_sectors_ok": 98,
      "sectors": { "出版": {"bk": "BK0477", "grade": "B", "members": ["601811", ...]} },
      "map": { "601811": "出版" },
      "failed": ["某个板块..."],
      "note": "..."
    }

`sectors[].grade` 直接取自 sector_engine.json（跑过 sector_engine.py 之后才有；
没有就留空，App 端自己按 total 判档）。

## 注意

东财对**开发容器出口 IP 封禁**（2026-10-26 已记录在 sector-constituents.yml），
本地跑通常只能拿到一部分；GH runner 出口 IP 不同，让 CI 补齐。所以本脚本：
  - 单板块失败只记录不中断（部分成功也要落库，总比没有强）
  - 多轮重试逐步补齐（--passes）
  - 保留上一版里本次没拿到的板块（不因为一次失败就把已有数据清空）

用法:
    python3 scripts/build_stock_sector_map.py [--passes 3] [--workers 4]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT_FILE = os.path.join(DATA, "stock_sector_map.json")
CST = timezone(timedelta(hours=8))

HOSTS = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
UA = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
FIELDS = "f12,f14,f3"


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def fetch_json(fs, pz=500, pn=1, attempts=3, timeout=10):
    """拉一页 clist；东财会瞬断，失败换域名重试。"""
    last = None
    for attempt in range(attempts):
        host = HOSTS[attempt % len(HOSTS)]
        url = (f"{host}/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2"
               f"&fid=f3&fs={fs}&fields={FIELDS}")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            dd = data.get("data") or {}
            return dd.get("diff") or [], dd.get("total") or 0
        except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed for fs={fs}: {last}")


def fetch_all(fs, pz=500, max_pages=12):
    out, pn, total = [], 1, None
    while pn <= max_pages:
        diff, t = fetch_json(fs, pz=pz, pn=pn)
        if total is None:
            total = t
        if not diff:
            break
        out.extend(diff)
        if total and len(out) >= total:
            break
        pn += 1
        time.sleep(0.3)
    return out


def code_of(row):
    return str(row.get("f12") or "").strip()


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=3, help="多轮补齐（东财会瞬断）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    # 板块列表（东财行业板块，与 sector_engine 同一套命名）
    try:
        boards = fetch_all("m:90+t:2", pz=200, max_pages=3)
    except Exception as e:  # noqa: BLE001
        print(f"::error::板块列表拉取失败: {e}", file=sys.stderr)
        return 1

    name_of, bk_of = {}, {}
    for b in boards:
        bk = code_of(b)
        name = str(b.get("f14") or "").strip()
        if bk and name:
            bk_of[name] = bk
            name_of[bk] = name
    print(f"板块列表: {len(name_of)} 个")
    if not name_of:
        print("::error::板块列表为空", file=sys.stderr)
        return 1

    # 上一版：本次某个板块失败时保留旧成员，别把已有数据清掉
    prev = load_json(OUT_FILE) or {}
    prev_sectors = prev.get("sectors") or {}

    # sector_engine 的分档（可选，只用于标注，不影响成分股）
    grade_of = {}
    se = load_json(os.path.join(DATA, "sector_engine.json")) or {}
    for row in (se.get("sectors") or []):
        nm = row.get("sector")
        if nm:
            grade_of[nm] = row.get("grade") or ""

    sectors = {}
    failed = []
    for attempt in range(args.passes):
        todo = [n for n in bk_of if n not in sectors]
        if not todo:
            break
        print(f"--- pass {attempt + 1}: 待拉 {len(todo)} 个板块")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_all, f"b:{bk_of[n]}", 500, 6): n for n in todo}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    rows = fut.result()
                except Exception:  # noqa: BLE001
                    failed.append(name)
                    continue
                members = [code_of(r) for r in rows if code_of(r) and code_of(r) != "-"]
                if not members:
                    failed.append(name)
                    continue
                sectors[name] = {
                    "bk": bk_of[name],
                    "grade": grade_of.get(name, ""),
                    "members": sorted(set(members)),
                }
        failed = [n for n in bk_of if n not in sectors]
        print(f"    已成功 {len(sectors)} / {len(bk_of)}，失败 {len(failed)}")
        if not failed:
            break
        time.sleep(2)

    # 本次没拿到的，回填上一版（仅当上一版确实有成员）
    backfilled = []
    for name in failed:
        old = prev_sectors.get(name)
        if old and old.get("members"):
            sectors[name] = old
            backfilled.append(name)

    if not sectors:
        print("::error::一个板块都没拿到，保留旧文件不动", file=sys.stderr)
        return 1

    stock_map = {}
    for name, s in sectors.items():
        for code in s["members"]:
            # 同一只股票命中多个板块时取第一个（东财按涨幅排序，先命中的更强）
            stock_map.setdefault(code, name)

    out = {
        "ts": now_iso(),
        "engine": "build_stock_sector_map v1 (板块列表→BK→成分股, 不做名称模糊匹配)",
        "source": "push2.eastmoney.com clist (fs=m:90+t:2 / fs=b:{BK})",
        "n_sectors": len(bk_of),
        "n_sectors_ok": len(sectors),
        "n_stocks": len(stock_map),
        "sectors": sectors,
        "map": stock_map,
        "failed": sorted(failed),
        "backfilled": sorted(backfilled),
        "note": ("板块名与 sector_engine 同一套（东财行业板块），个股归属按成分股直接反查，"
                 "不依赖行业名匹配；backfilled 表示本次没拉到、用上一版成员补的"),
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"写入 {OUT_FILE}: {len(sectors)} 板块 / {len(stock_map)} 只股票"
          f"（回填 {len(backfilled)}，失败 {len(failed)}）")
    # 覆盖率太低说明接口大面积失败，让 CI 看见
    if len(sectors) < len(bk_of) * 0.5:
        print(f"::warning::只拿到 {len(sectors)}/{len(bk_of)} 个板块，覆盖率偏低", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
