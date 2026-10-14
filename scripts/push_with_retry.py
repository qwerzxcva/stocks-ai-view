#!/usr/bin/env python3
"""push_with_retry.py — 抢在 CI 提交之间把自己的 commit 推上去。

背景: 仓里有 6+ 个 bot 工作流每 5 分钟推 main, 人工 push 经常遇到
"fetch first"。做法: fetch → rebase → push, 失败退避重试。

用法: python3 push_with_retry.py [--max-tries 8]
"""
import argparse
import subprocess
import sys
import time

REPO = "/root/workspace/stocks-ai-view"


def sh(cmd):
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tries", type=int, default=8)
    args = ap.parse_args()

    for attempt in range(1, args.max_tries + 1):
        print("[try %d] fetch + rebase" % attempt, flush=True)
        sh(["git", "fetch", "origin", "main"])
        r = sh(["git", "rebase", "origin/main"])
        if r.returncode != 0:
            print("  rebase 失败: %s" % (r.stderr or "")[:150].replace("\n", " "))
            sh(["git", "rebase", "--abort"])
            time.sleep(10)
            continue
        r = sh(["git", "push", "origin", "main"])
        if r.returncode == 0:
            print("  PUSHED OK")
            print(sh(["git", "log", "--oneline", "-2"]).stdout)
            return 0
        print("  push 被拒: %s" % (r.stderr or "")[:150].replace("\n", " "))
        time.sleep(min(60, 8 * attempt))
    print("超过最大重试次数")
    return 1


if __name__ == "__main__":
    sys.exit(main())
