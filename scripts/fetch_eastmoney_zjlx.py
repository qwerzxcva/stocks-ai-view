#!/usr/bin/env python3
"""fetch_eastmoney_zjlx.py — 从东财网页抓资金流历史 (浏览器渲染后取表)

为什么不用接口: 东财 push2 / datacenter 接口对同 IP 高频请求会 TLS RST,
且资金流历史只给 60 天。而 data.eastmoney.com/zjlx/{code}.html 这个页面
在浏览器里渲染后能直接读到 **122 天**的完整表格, 且含超大单/大单/中单/小单
四档拆分 —— 数据比接口更全。

实现: 用内置 browser 打开页面 → execute_js 读第 4 张表 (资金流历史)
     → 解析成结构化记录。不依赖任何隐藏接口, 走用户可见的网页。

用法:
  python3 fetch_eastmoney_zjlx.py --codes 600487,600519
  python3 fetch_eastmoney_zjlx.py --from-universe --limit 50
输出: data/moneyflow_web.json
  {"ts","source","n_codes","flows":{code:[{date,close,chg_pct,
    main_net,main_pct,xl_net,xl_pct,l_net,l_pct,m_net,m_pct,s_net,s_pct}]}}
说明: 本脚本需要 browser 工具会话, 由调用方 (AI) 驱动; 单独运行会提示。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

OUT = os.path.join(DATA_DIR, "moneyflow_web.json")

# 浏览器里执行的解析脚本: 读第 4 张表 (index 3), 返回结构化行
PARSE_JS = r"""
(function(){
  const tables = document.querySelectorAll('table');
  // 资金流历史表: 行数 > 20 且表头含「主力净流入」
  let target = null;
  for (const t of tables) {
    if (t.querySelectorAll('tr').length > 20 && /主力净流入/.test(t.innerText)) {
      target = t; break;
    }
  }
  if (!target) return JSON.stringify({ok:false, reason:'table not found',
      tables: tables.length});
  const rows = [...target.querySelectorAll('tr')].map(tr =>
    [...tr.querySelectorAll('th,td')].map(td => (td.innerText||'').trim()));
  return JSON.stringify({ok:true, rows: rows});
})()
"""


def parse_rows(rows):
    """把原始表格行解析成记录。表头有两行 (含 '净额/净占比' 子表头)。"""
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        d = str(r[0])
        if len(d) != 10 or d[4] != "-":
            continue
        try:
            out.append({
                "date": d,
                "close": float(r[1].replace(",", "")),
                "chg_pct": float(r[2].replace("%", "")),
                "main_net": r[3], "main_pct": r[4],
                "xl_net": r[5] if len(r) > 5 else None,
                "xl_pct": r[6] if len(r) > 6 else None,
                "l_net": r[7] if len(r) > 7 else None,
                "l_pct": r[8] if len(r) > 8 else None,
                "m_net": r[9] if len(r) > 9 else None,
                "m_pct": r[10] if len(r) > 10 else None,
                "s_net": r[11] if len(r) > 11 else None,
                "s_pct": r[12] if len(r) > 12 else None,
            })
        except (ValueError, IndexError):
            continue
    return out


def merge(code, flows):
    """写盘 (按 code 幂等覆盖, 保留其他股票已有数据)。"""
    doc = {}
    if os.path.exists(OUT):
        try:
            doc = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            doc = {}
    allflow = doc.get("flows") or {}
    allflow[code] = flows
    payload = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "eastmoney zjlx web page (browser-rendered, ~122 days)",
        "n_codes": len(allflow),
        "flows": allflow,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return len(allflow)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        print(__doc__)
        print("\n[提示] 本脚本由 AI 用 browser 工具驱动; 直接运行无法访问浏览器。")
        return 1
    print(f"[zjlx] 待抓 {len(codes)} 只: {codes[:5]}")
    print("[zjlx] 请在 AI 会话中逐只用 browser 打开 "
          "https://data.eastmoney.com/zjlx/{code}.html 并执行 PARSE_JS")
    return 0


if __name__ == "__main__":
    sys.exit(main())