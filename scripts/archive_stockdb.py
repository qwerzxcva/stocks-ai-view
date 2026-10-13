#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/archive_stockdb.py ═══
# 上游 commit: 558e77a750a1 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""archive_stockdb.py — org stockdb 日更归档: wh/daily当年分区 + 引擎产物 (Git Data API, >40MB分片跳过)
cron 16:30 交易日
"""
import os, json, re, base64, hashlib, urllib.request, datetime

REPO = "jiegengxiangjianxue1/stockdb"
BASE = "/home/agentuser/stockdb"

def tok_():
    return re.search(r":([^:@]+)@github\.com", open(os.path.expanduser("~/.git-credentials")).read()).group(1)

def apiM(url, method="GET", body=None):
    H = {"Authorization": f"token {tok_()}", "Accept": "application/vnd.github+json", "User-Agent": "x"}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=90))

def push_file(path, content_bytes, msg):
    try:
        c = apiM(f"https://api.github.com/repos/{REPO}/contents/{path}?ref=main")
        if hashlib.sha1(base64.b64decode(c["content"])).hexdigest() == hashlib.sha1(content_bytes).hexdigest():
            return "same"
        sha = c["sha"]
    except Exception:
        sha = None
    if len(content_bytes) > 40 * 1024 * 1024:
        return "too-big"
    body = {"message": msg, "content": base64.b64encode(content_bytes).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    apiM(f"https://api.github.com/repos/{REPO}/contents/{path}", "PUT", body)
    return "pushed"

def main():
    today = str(datetime.date.today())
    yr = today[:4]
    pushed = []
    # ① wh daily当年分区
    fp = f"{BASE}/wh/daily/{yr}.parquet"
    if os.path.exists(fp):
        st = push_file(f"wh/daily/{yr}.parquet", open(fp, "rb").read(), f"wh daily {today}")
        if st == "pushed": pushed.append(f"wh/{yr}")
    # ② 引擎产物打包(小json集合)
    import io
    bundle = {}
    for f in ["decision.json","engine_fusion.json","strategy_card.json","market_phase.json",
              "kline_sim.json","intraday_signal.json","ai_picks.json"]:
        p = f"{BASE}/data/{f}"
        if os.path.exists(p):
            bundle[f] = json.load(open(p))
    if bundle:
        st = push_file(f"engine/latest.json", json.dumps(bundle, ensure_ascii=False).encode(), f"engine snapshot {today}")
        if st == "pushed": pushed.append("engine/latest.json")
    print("archived:", pushed if pushed else "无变化")

if __name__ == "__main__":
    main()
