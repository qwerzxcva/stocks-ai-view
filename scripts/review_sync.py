#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/review_sync.py ═══
# 上游 commit: 75aef00e387b · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""review_sync.py — 本地↔analysis-hub复盘输入/产物同步 (三号互帮: 主号产数据, 小号Actions跑LLM)
15:55 推review_input/latest.json; 16:10(及每30分)拉reviews/review_{today}.md回本地
"""
import json, os, re, base64, urllib.request, shutil, datetime

stok = open(os.path.expanduser("~/.hermes/jiegengxiangjianxue.token")).read().strip()
H = {"Authorization": f"token {stok}", "Accept": "application/vnd.github+json", "User-Agent": "x"}
REPO = "jiegengxiangjianxue/analysis-hub"

def apiM(url, method="GET", body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=60))

def push_file(path, content, msg):
    try: sha = apiM(f"https://api.github.com/repos/{REPO}/contents/{path}?ref=main")["sha"]
    except Exception: sha = None
    body = {"message": msg, "content": base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    return apiM(f"https://api.github.com/repos/{REPO}/contents/{path}", "PUT", body)["commit"]["sha"][:8]

def main():
    mode = __import__("sys").argv[1] if len(__import__("sys").argv) > 1 else "pull"
    today = str(datetime.date.today())
    if mode == "push":
        src = f"/home/agentuser/stockdb/data/news_map/review_input_{today}.json"
        if not os.path.exists(src):
            src = sorted(__import__("glob").glob("/home/agentuser/stockdb/data/news_map/review_input_*.json"))[-1]
        print("push:", push_file("review_input/latest.json", open(src, "rb").read(), f"review input {today}"))
    else:
        try:
            c = apiM(f"https://api.github.com/repos/{REPO}/contents/reviews/review_{today}.md?ref=main")
            md = base64.b64decode(c["content"]).decode()
            dst = f"/home/agentuser/stockdb/data/news_map/llm_review_{today}.md"
            open(dst, "w").write(md)
            print("pulled:", dst, len(md), "bytes")
        except Exception as e:
            print("pull skip:", str(e)[:80])

if __name__ == "__main__":
    main()
