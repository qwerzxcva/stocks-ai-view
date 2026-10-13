#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/sync_algo_to_org.py ═══
# 上游 commit: bc7fe695a1d5 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""sync_algo_to_org.py — 算法单一真源同步: 本地stocks-algo → org stocks/algo/ (内容hash diff, 有变化才推)
cron 16:20 交易日. 同时兜底daily-scan产物.
"""
import os, json, re, base64, hashlib, urllib.request, glob

LOCAL = "/home/agentuser/stocks-algo"
REPO = "jiegengxiangjianxue1/stocks"
SYNC = ["ai_pick.py", "pick_algo.py", "market_phase.py", "cycle_algo.py", "predict_algo.py",
        "patterns.py", "factor_lib.py", "cyq.py", "walkforward.py", "rps_rotation.py"]

def tok_():
    return re.search(r":([^:@]+)@github\.com", open(os.path.expanduser("~/.git-credentials")).read()).group(1)

def apiM(url, method="GET", body=None):
    H = {"Authorization": f"token {tok_()}", "Accept": "application/vnd.github+json", "User-Agent": "x"}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=60))

def push_file(path, content, msg):
    try:
        c = apiM(f"https://api.github.com/repos/{REPO}/contents/{path}?ref=main")
        sha = c["sha"]
        remote_hash = hashlib.sha1(base64.b64decode(c["content"])).hexdigest()
    except Exception:
        sha, remote_hash = None, None
    local_hash = hashlib.sha1(content if isinstance(content, bytes) else content.encode()).hexdigest()
    if remote_hash == local_hash:
        return "same"
    body = {"message": msg, "content": base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    apiM(f"https://api.github.com/repos/{REPO}/contents/{path}", "PUT", body)
    return "pushed"

def main():
    changed = []
    import time
    for f in SYNC:
        p = os.path.join(LOCAL, f)
        if not os.path.exists(p): continue
        st = None
        for attempt in range(3):
            try:
                st = push_file(f"algo/{f}", open(p, "r").read(), f"sync: {f} (本地stocks-algo单一真源)")
                break
            except Exception as e:
                time.sleep(2*(attempt+1))
                st = f"err:{e}"[:50]
        if st == "pushed": changed.append(f)
        elif st and st != "same": print(f"  {f}: {st}")
    print("synced:", changed if changed else "无变化")

if __name__ == "__main__":
    main()
