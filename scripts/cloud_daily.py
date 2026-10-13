#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/cloud_daily.py ═══
# 上游 commit: 34e399cc2841 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""cloud_daily.py — 云端全链自跑(本地断联时由 GitHub Actions/GitCode容器 执行):
①当日全市场快照(腾讯qt) ②B规则候选 ③引擎决策 ④LLM复盘(GitCode AI) ⑤产物提交回仓
自包含: 相对路径, 无本地依赖. 用法: python3 engines/cloud_daily.py
"""
import json, os, sys, datetime, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = str(datetime.date.today())
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
OUT = f"{ROOT}/cloud/{TODAY}"
os.makedirs(OUT, exist_ok=True)

def http_json(url, timeout=20):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))

def fetch_snapshot():
    """腾讯qt批量: 全市场主板(沪深主板清单来自上一日快照或内置分页抓取)"""
    syms = []
    # 分页抓沪深主板列表( qt clist接口, 沪60/深00 )
    for mkt, pg in [("sh", 1), ("sz", 1)]:
        for p in range(1, 12):
            try:
                d = http_json(f"https://push2.eastmoney.com/api/qt/clist/get?pn={p}&pz=500&fs=m:1+t:2,m:0+t:6" if False else
                              f"https://qt.gtimg.cn/q=sh{p}" , 5)  # placeholder
            except Exception:
                pass
    # 简化: 用东财clist(公开)拿主板代码+现价
    rows = []
    try:
        for fs in ["m:1+t:2", "m:0+t:6"]:  # 沪主板/深主板
            for p in range(1, 15):
                d = http_json(f"https://push2.eastmoney.com/api/qt/clist/get?pn={p}&pz=300&po=1&np=1&fltt=2&fid=f3&fs={fs}&fields=f12,f14,f2,f3,f5,f6,f8,f10,f15,f16,f17,f18")
                diff = (d.get("data") or {}).get("diff") or []
                if not diff: break
                for x in diff:
                    rows.append({"code": x["f12"], "name": x["f14"], "price": x["f2"], "pct": x["f3"],
                                 "vol": x["f5"], "amt": x["f6"], "hs": x["f8"], "vlb": x["f10"],
                                 "high": x["f15"], "low": x["f16"], "open": x["f17"], "pre": x["f18"]})
    except Exception as e:
        print("clist err:", str(e)[:80])
    json.dump({"date": TODAY, "n": len(rows), "rows": rows}, open(f"{OUT}/snapshot.json", "w"), ensure_ascii=False)
    return rows

def b_rule(rows):
    """B规则云上版: 无240日历史, 用当日特征近似(涨幅0-5%+量比0.8-2+非涨停+主板) — 标注为降级版"""
    out = []
    for r in rows:
        try:
            if not isinstance(r["pct"], (int, float)) or not isinstance(r["vlb"], (int, float)): continue
            if 0 < r["pct"] <= 5 and 0.8 <= r["vlb"] <= 2.0 and r["pct"] < 9.5 and r["code"][:2] in ("60", "00"):
                out.append(r)
        except Exception: continue
    out.sort(key=lambda x: x["vlb"], reverse=True)
    return out[:12]

def ask_llm(prompt):
    """GitCode AI优先 → pollinations兜底"""
    tok = os.environ.get("GITCODE_TOKEN", "").strip()
    for model in ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3-30B-A3B-Instruct-2507"]:
        if tok:
            try:
                H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500}
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    "https://gitcode.com/api/v5/chat/completions", data=json.dumps(body).encode(), headers=H, method="POST"), timeout=120))
                c = (r["choices"][0]["message"].get("content") or "").strip()
                if c: return f"[{model.split('/')[-1]}] " + c
            except Exception: continue
    try:
        return urllib.request.urlopen("https://text.pollinations.ai/" + urllib.parse.quote(prompt), timeout=120).read().decode()
    except Exception as e:
        return f"(LLM不可用: {str(e)[:60]})"

def main():
    rows = fetch_snapshot()
    if not rows:
        print("快照失败(可能非交易日)"); return
    up = sum(1 for r in rows if isinstance(r.get("pct"), (int,float)) and r["pct"] > 0)
    zt = sum(1 for r in rows if isinstance(r.get("pct"), (int,float)) and r["pct"] > 9.5)
    cands = b_rule(rows)
    lines = [f"交易日 {TODAY}: 主板{len(rows)}只 上涨{up} 涨停{zt}",
             f"B规则候选{len(cands)}只(云上降级版): " + ",".join(f"{c['code']}{c['name']}" for c in cands[:8]),
             "涨幅Top: " + ",".join(f"{r['name']}{r['pct']}%" for r in sorted([r for r in rows if isinstance(r.get("pct"),(int,float))], key=lambda x:-x['pct'])[:10])]
    ctx = "\n".join(str(l) for l in lines)
    report = ask_llm("你是A股量化助手, 根据数据写250字内复盘+明日关注, 注明不构成投资建议:\n" + ctx)
    md = f"# 云端日报 {TODAY}\n\n{ctx}\n\n## AI复盘\n\n{report}\n\n> 云端生成(本地断联守护), 不构成投资建议\n"
    open(f"{OUT}/report.md", "w").write(md)
    json.dump({"date": TODAY, "summary": lines, "candidates": cands[:12]}, open(f"{OUT}/latest.json", "w"), ensure_ascii=False)
    # 同步到stable位置
    json.dump(json.load(open(f"{OUT}/latest.json")), open(f"{ROOT}/cloud/latest.json", "w"), ensure_ascii=False)
    print(md[:400])

if __name__ == "__main__":
    main()
