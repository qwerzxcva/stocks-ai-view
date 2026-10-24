#!/usr/bin/env python3
"""推送 stocks-algo 算法仓: 代码+文档+因子矩阵(6片)+回测网格(2件) ≈ 640MB"""
import os, sys, json, base64, urllib.request, time

TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials")):
    if "github.com" in line:
        import re
        m = re.search(r"ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+", line)
        if m:
            TOKEN = m.group(0)
            break
if not TOKEN:
    print("NO TOKEN"); sys.exit(1)

REPO = "jiegengxiangjianxue1/stocks-algo"
API = f"https://api.github.com/repos/{REPO}"
HDR = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}

def req(url, data=None, method=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                               headers=HDR, method=method)
    return json.load(urllib.request.urlopen(r, timeout=120))

def retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            print(f"  retry {i+1}: {str(e)[:80]}")
            time.sleep(3 * (i + 1))
    raise RuntimeError("retries exhausted")

def get_ref():
    try:
        return req(f"{API}/git/ref/heads/main")["object"]["sha"]
    except Exception:
        return None

def push_file(path, local, message):
    """Git Data API: blob→tree→commit→ref"""
    size = os.path.getsize(local)
    if size > 90 * 1024 * 1024:
        print(f"SKIP {local} ({size//1048576}MB > 90MB)")
        return
    base = get_ref()
    # blob
    if size > 1 * 1024 * 1024:
        # 上传原始blob
        def up():
            r = urllib.request.Request(f"{API}/git/blobs",
                data=json.dumps({"content": base64.b64encode(open(local,'rb').read()).decode(),
                                 "encoding": "base64"}).encode(), headers=HDR)
            return json.load(urllib.request.urlopen(r, timeout=300))
        blob = retry(up)
    else:
        content = open(local, encoding="utf-8", errors="replace").read()
        blob = retry(lambda: req(f"{API}/git/blobs", {"content": content, "encoding": "utf-8"}))
    # tree
    tree = retry(lambda: req(f"{API}/git/trees", {
        "base_tree": base, "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]
    }) if base else req(f"{API}/git/trees", {
        "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}))
    # commit
    commit = retry(lambda: req(f"{API}/git/commits", {
        "message": message, "tree": tree["sha"], "parents": [base] if base else []}))
    if base:
        retry(lambda: req(f"{API}/git/refs/heads/main", {"sha": commit["sha"]}, method="PATCH"))
    else:
        retry(lambda: req(f"{API}/git/refs", {"ref": "refs/heads/main", "sha": commit["sha"]}))
    print(f"PUSH_OK {path} ({size//1024}KB) -> {commit['sha'][:8]}")

def collect():
    items = []
    for f in sorted(os.listdir("/home/agentuser/stocks-algo")):
        if f.endswith(".py") or f.endswith(".md"):
            items.append((f, f"/home/agentuser/stocks-algo/{f}"))
    items.append(("ARCHITECTURE.md", "/home/agentuser/stockdb/ARCHITECTURE.md"))
    # factor_matrix 已上云(488MB), 不再从/tmp重推
    import os as _os
    for _d in ("/tmp/factor_matrix_small", "/tmp/factor_matrix"):
        if _os.path.isdir(_d):
            for f in sorted(_os.listdir(_d)):
                items.append((f"factor_matrix/{f}", f"{_d}/{f}"))
    _fm = "/tmp/factor_matrix_small"
    if os.path.isdir(_fm):
        for f in ["lead_panel_0.parquet","lead_panel_1.parquet","lead_panel_2.parquet","lead_panel_3.parquet"]:
            if os.path.exists(f"{_fm}/{f}"):
                items.append((f"backtest/{f}", f"{_fm}/{f}"))
    for f in sorted(os.listdir("/home/agentuser/stockdb/data")):
        if f in ("pools.json", "ai_picks.json"):
            items.append((f"data/{f}", f"/home/agentuser/stockdb/data/{f}"))
    items.append(("web/server.py", "/home/agentuser/stockdb/web/server.py"))
    items.append(("web/index.html", "/home/agentuser/stockdb/web/index.html"))
    return items

if __name__ == "__main__":
    items = collect()
    print(f"total {len(items)} files")
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for path, local in items:
        if only and only not in path:
            continue
        try:
            push_file(path, local, f"algo sync: {os.path.basename(path)}")
        except Exception as e:
            print(f"PUSH_FAIL {path}: {str(e)[:100]}")
