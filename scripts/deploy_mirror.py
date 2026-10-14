#!/usr/bin/env python3
"""
deploy_mirror.py — 部署 A 仓代码到 B/C 仓并错峰 cron

策略:
  - B/C 仓只跑采集链路 (full-pipeline / realtime-minute / realtime-poll),
    这些是决定数据新鲜度的关键路径, cron 错峰到 mod5=4 / mod5=0
  - AI/复盘/归档/回溯类 workflow 在 B/C 仓一律禁用 (on: workflow_dispatch only),
    避免三仓重复烧 LLM 额度、重复归档、重复 commit 造成冲突
  - 所有 workflow 的 push 到 origin 的 remote 名保持 origin (各自仓)

用法: python3 deploy_mirror.py --label B
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# token 一律从环境变量读, 绝不硬编码 (GitHub secret scanning 会拦 push)
# export MIRROR_TOKEN_B=ghp_xxx  MIRROR_TOKEN_C=ghp_xxx
TOKENS = {"B": os.environ.get("MIRROR_TOKEN_B", ""),
          "C": os.environ.get("MIRROR_TOKEN_C", "")}
OWNERS = {"B": "jiegengxiangjianxue", "C": "dufuwuxin"}
REPOS = {"B": "stocks-ai-view-b", "C": "stocks-ai-view-c"}

# 错峰: A 落 mod5=2, B 落 mod5=4, C 落 mod5=0
MINUTE_SETS = {
    "B": "4,9,14,19,24,29,34,39,44,49,54,59",
    "C": "0,5,10,15,20,25,30,35,40,45,50,55",
}

# B/C 仓保留 schedule 的 workflow (采集链路, 决定数据新鲜度)
KEEP_SCHEDULE = {"full-pipeline.yml", "realtime-minute.yml", "realtime-poll.yml"}

# B/C 仓完全禁用的 workflow (会重复烧额度/重复归档/造成 commit 冲突)
DISABLE = {"nightly-backfill.yml", "dataset-archive.yml", "gitcode-sync.yml",
           "space-pull.yml", "cloud-analysis.yml", "ai-update.yml",
           "daily-brief.yml", "daily-llm-review.yml", "hot-impact-realtime.yml",
           "multi-tier-screener.yml", "pool-v4-daily.yml", "rebuild-pools.yml"}


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("  CMD FAILED:", " ".join(cmd))
        print("  stdout:", (r.stdout or "")[:400])
        print("  stderr:", (r.stderr or "")[:400])
        raise SystemExit(1)
    return r


def patch_cron(src, minutes):
    """把 schedule cron 的分钟字段替换成错峰集合。src = 文件内容, 返回新内容。"""
    out = []
    for line in src.split("\n"):
        m = re.match(r"^(\s*-?\s*cron:\s*')([0-9,*/\-]+)(\s+.*)$", line)
        if m:
            line = m.group(1) + minutes + m.group(3)
        out.append(line)
    return "\n".join(out)


def disable_schedule(src):
    """把 on.schedule 移除, 只留 workflow_dispatch。src = 文件内容, 返回新内容。"""
    lines = src.split("\n")
    out, in_sched = [], False
    for line in lines:
        if re.match(r"^\s*schedule:\s*$", line):
            in_sched = True
            out.append(line + "  # disabled on mirror (avoid duplicate runs)")
            continue
        if in_sched:
            if re.match(r"^\s*-\s*cron:", line):
                out.append("    # " + line.strip())
                continue
            in_sched = False
        out.append(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=["B", "C"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    L = args.label
    tok = TOKENS[L]
    if not tok:
        print("[deploy %s] 缺少 token: 请先 export MIRROR_TOKEN_%s=ghp_xxx" % (L, L))
        return 1
    owner, repo = OWNERS[L], REPOS[L]
    minutes = MINUTE_SETS[L]
    url = "https://x-access-token:" + tok + "@github.com/" + owner + "/" + repo + ".git"

    print("[deploy %s] %s/%s  cron 分钟位: %s" % (L, owner, repo, minutes))

    tmp = tempfile.mkdtemp(prefix="mirror-" + L + "-")
    try:
        # clone (允许空仓)
        r = subprocess.run(["git", "clone", "--depth", "1", url, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  clone 失败, 尝试 init:", (r.stderr or "")[:200])
            os.makedirs(tmp, exist_ok=True)
            run(["git", "init"], cwd=tmp)
            run(["git", "remote", "add", "origin", url], cwd=tmp)

        # 复制代码 (排除 data/ 本地产物与 .git)
        for item in os.listdir(SRC):
            if item in (".git", "data", "node_modules", "__pycache__"):
                continue
            s = os.path.join(SRC, item)
            d = os.path.join(tmp, item)
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d)
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "data", "predictions"), exist_ok=True)

        # 改 workflow
        wf_dir = os.path.join(tmp, ".github", "workflows")
        n_keep = n_dis = 0
        for f in sorted(os.listdir(wf_dir)):
            p = os.path.join(wf_dir, f)
            if not f.endswith(".yml"):
                continue
        for f in sorted(os.listdir(wf_dir)):
            p = os.path.join(wf_dir, f)
            if not f.endswith(".yml"):
                continue
            with open(p, encoding="utf-8") as fh:
                body = fh.read()          # 先读
            if f in DISABLE:
                new_body = disable_schedule(body)
                n_dis += 1
            elif f in KEEP_SCHEDULE:
                new_body = patch_cron(body, minutes)
                n_keep += 1
            else:
                new_body = disable_schedule(body)
                n_dis += 1
            with open(p, "w", encoding="utf-8") as fh:   # 后写
                fh.write(new_body)
        print("  workflow: %d 个错峰保留, %d 个禁用 schedule" % (n_keep, n_dis))

        # 标记镜像身份
        marker = os.path.join(tmp, "MIRROR.md")
        open(marker, "w", encoding="utf-8").write(
            "# Mirror node %s\n\n"
            "该仓是 stocks-ai-view 的镜像节点, 仅用于多账号 cron 错峰 (2 分钟级数据粒度)。\n\n"
            "- 代码唯一事实源: https://github.com/qwerzxcva/stocks-ai-view\n"
            "- 本仓 cron 分钟位: %s (mod 5)\n"
            "- 只跑采集链路; AI/复盘/归档/回溯已禁用 schedule\n"
            "- 不要在本地手改代码, 会被下次部署覆盖\n" % (L, minutes)
        )

        if args.dry_run:
            print("  [dry-run] 不提交")
            return 0

        run(["git", "config", "user.name", "mirror-deploy-bot"], cwd=tmp)
        run(["git", "config", "user.email", "3576982374@qq.com"], cwd=tmp)
        run(["git", "add", "-A"], cwd=tmp)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tmp)
        if r.returncode == 0:
            print("  无变化, 跳过提交")
        else:
            run(["git", "commit", "-m", "mirror(%s): deploy with cron %s" % (L, minutes)], cwd=tmp)
            run(["git", "branch", "-M", "main"], cwd=tmp)
            r = subprocess.run(["git", "push", "-u", "origin", "main", "--force-with-lease"],
                               cwd=tmp, capture_output=True, text=True)
            if r.returncode != 0:
                print("  push 被拒, 重试 force:", (r.stderr or "")[:200])
                run(["git", "push", "-u", "origin", "main", "--force"], cwd=tmp)
            print("  pushed: https://github.com/%s/%s" % (owner, repo))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
