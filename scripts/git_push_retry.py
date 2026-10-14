#!/usr/bin/env python3
"""
git_push_retry.py — 带退避重试的 git push, 供所有 GitHub Actions 工作流复用

为什么需要:
  仓库有 6+ 个 bot 同时往 main 推 (pipeline-bot / hot-bot / brief-bot /
  stocks-sync-bot / cloud-bot / space-pull-bot), 频率最高到每 3 分钟。
  单个工作流内的 \"push 失败就 reset 再推一次\" 在高峰期不够, 结果是
  工作流显示 success 但数据从未上台 — 2026-10-14 实测 hot-impact-realtime
  连续多轮 success, 而 market_now.json / enrich.json 的时间戳停在 10-13。

做法:
  1. git fetch + reset --soft origin/main (以远端为基, 本地改动全部保留在暂存区)
  2. git add 指定路径 (自动跳过不存在的, 避免 pathspec fatal)
  3. 有差异才 commit
  4. push, 失败则退避 5/10/20/40s 重试, 最多 5 次
  5. 每次重试都重新 fetch + reset + add, 保证叠在最新远端之上

退出码: 0 = 已推送或确认无差异; 1 = 5 次都失败 (调用方据此 fail job)

用法:
  python3 git_push_retry.py --msg \"pipeline: 20261014_0751\" \
      data/latest.json data/market_now.json data/predictions
  python3 git_push_retry.py --msg \"x\" --dir data        # 整个目录
  python3 git_push_retry.py --msg \"x\" --all             # 所有改动
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
MAX_TRIES = 5
BACKOFF = [5, 10, 20, 40]


def sh(args, check=False):
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> rc={r.returncode}\n{r.stderr[:400]}")
    return r


def stage(paths, dirs, all_changes):
    """只 add 真实存在的路径: git add 不存在的 pathspec 会 fatal (exit 1)。"""
    exist = [p for p in paths if os.path.exists(p)]
    for p in paths:
        if p not in exist:
            print(f"skip (not generated): {p}")
    for d in dirs:
        if os.path.isdir(d):
            exist.append(d)
        else:
            print(f"skip (no dir): {d}")
    if all_changes:
        exist.append("-A")
    if not exist:
        print("nothing to stage")
        return False
    sh(["git", "add", "-A", "--"] + exist)
    return True


def staged_paths(paths, dirs, all_changes):
    """本次 add 涉及的路径集合, 用于判断"是否有我们的差异"。

    不能直接看整个暂存区: 若调用方在 fetch/reset 之前 index 里已有别的改动
    (例如本地先 git add 过), git diff --cached 会把它们一并算进来,
    导致本脚本提交不属于它的内容。
    """
    if all_changes:
        return None          # None = 不过滤, 看全部
    wanted = set(p for p in paths if os.path.exists(p))
    for d in dirs:
        if os.path.isdir(d):
            wanted.add(d.rstrip("/"))
    return wanted


def has_our_changes(scope):
    """scope=None 看全部暂存区; 否则只看 scope 内的路径。"""
    if scope is None:
        return sh(["git", "diff", "--cached", "--quiet", "HEAD"]).returncode != 0
    r = sh(["git", "diff", "--cached", "--name-only", "HEAD"])
    names = set(r.stdout.split())
    for w in scope:
        for n in names:
            if n == w or n.startswith(w + "/"):
                return True
    return False


def refresh_from_remote(paths, dirs, all_changes, branch, protect):
    """把远端最新版 checkout 到工作区, 但跳过 protect 里的路径。

    为什么必须这一步: 6 个工作流同时推 main, 每个都在 run 开始时 checkout。
    若 A 在 11:34 推了新的 latest.json, B 的工作区里仍是 10:49 的旧副本,
    B 直接 add 就会把 A 的新版覆盖回旧版 (2026-10-14 实测: ai-update 推的
    11:34 版本被随后 4 个 run 覆盖成 10:49)。

    protect = 本轮刚生成、绝不能被远端旧版冲掉的文件。调用方通过
    --protect 传入 (可多次)。其余路径一律以远端为准。
    """
    protect = set(protect or [])
    if all_changes:
        want = ['.']
    else:
        want = [p for p in paths if p] + [d for d in dirs if d]
    for w in want:
        if w in protect or any(w.startswith(p.rstrip('/') + '/') for p in protect):
            continue
        sh(["git", "checkout", f"origin/{branch}", "--", w])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--msg", required=True, help="commit message")
    ap.add_argument("paths", nargs="*", help="files to stage")
    ap.add_argument("--dir", action="append", default=[], help="directory to stage")
    ap.add_argument("--all", action="store_true", help="stage everything")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--user", default="pipeline-bot")
    ap.add_argument("--email", default="741059079@qq.com")
    ap.add_argument("--protect", action="append", default=[],
                    help="本轮刚生成、不许被远端旧版覆盖的路径 (可多次)")
    args = ap.parse_args()

    sh(["git", "config", "user.name", args.user])
    sh(["git", "config", "user.email", args.email])

    for i in range(1, MAX_TRIES + 1):
        sh(["git", "fetch", "origin", args.branch])
        sh(["git", "reset", "--soft", f"origin/{args.branch}"])
        # 关键: 先把远端最新版取回工作区, 防止用本 run 开始时的旧副本
        # 覆盖其他工作流刚推送的新数据。
        refresh_from_remote(args.paths, args.dir, args.all, args.branch,
                             args.protect)
        if not stage(args.paths, args.dir, args.all):
            print("no changes to push")
            return 0
        scope = staged_paths(args.paths, args.dir, args.all)
        if not has_our_changes(scope):
            print("no staged changes in our scope (remote already up to date)")
            return 0
        ts = datetime.now(CST).strftime("%Y%m%d_%H%M")
        r = sh(["git", "commit", "-m", f"{args.msg}: {ts}"])
        if r.returncode != 0:
            print(f"commit failed: {r.stderr.strip()[:200]}")
            return 1
        p = sh(["git", "push", "origin", f"HEAD:{args.branch}"])
        if p.returncode == 0:
            print(f"pushed on attempt {i}")
            return 0
        print(f"::warning::push attempt {i}/{MAX_TRIES} failed: "
              f"{p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'unknown'}")
        if i < MAX_TRIES:
            wait = BACKOFF[min(i - 1, len(BACKOFF) - 1)]
            print(f"backing off {wait}s")
            time.sleep(wait)

    print(f"::error::{MAX_TRIES} push attempts failed; this round's data was not published")
    return 1


if __name__ == "__main__":
    sys.exit(main())
