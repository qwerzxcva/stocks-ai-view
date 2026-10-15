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
import tempfile
import time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
MAX_TRIES = 6
# 403 (并发 push 被拒) 需要等更久: 别的 bot 可能正持有 push lock
BACKOFF = [5, 10, 20, 40, 60]


def sh(args, check=False):
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> rc={r.returncode}\n{r.stderr[:400]}")
    return r


def stage(paths, dirs, all_changes):
    """只 add 真实存在的路径: git add 不存在的 pathspec 会 fatal (exit 1)。"""
    exist = [p for p in paths if p and os.path.exists(p)]
    for p in paths:
        if p and p not in exist:
            print(f"skip (not generated): {p}")
    for d in dirs:
        if d and os.path.isdir(d):
            exist.append(d)
        elif d:
            print(f"skip (no dir): {d}")
    if all_changes:
        exist.append("-A")
    if not exist:
        print("nothing to stage")
        return False
    sh(["git", "add", "-A", "--"] + exist)
    return True


def commit_only(paths, dirs, all_changes, msg):
    """只提交指定路径, 不把 index 里其他内容带进去。

    为什么必须这样: git add 是增量的, 而 git commit 提交整个 index。
    refresh_from_remote 里的 git checkout <ref> -- <path> 会同时写 index,
    于是 index 里混进了本不该由本轮提交的文件, git commit 一并带走
    (2026-10-14 实测: full-pipeline 提交里出现 realtime_minute.json /
    market_now.json / enrich.json, 全都不在它的显式列表内, 把别的
    工作流刚推的新数据覆盖成旧版)。

    做法: git commit -- <paths> 只提交这些路径的已暂存改动。

    路径必须已存在于 index (git 知道它), 否则 git 报
    "pathspec did not match any file(s) known to git" 并失败 —
    未生成的可选产出 (如 loss_attribution.json) 必须在这里过滤掉,
    与 stage() 的过滤保持一致。
    """
    if all_changes:
        args = ["git", "commit", "-m", msg]
    else:
        targets = []
        for p in paths:
            if p and os.path.exists(p):
                targets.append(p)
        for d in dirs:
            if d and os.path.isdir(d):
                targets.append(d)
        if not targets:
            return None
        # 只保留 git 已知的路径, 避免 pathspec 报错
        known = sh(["git", "ls-files", "--"] + targets).stdout.split()
        known_set = set(known)
        # 目录形式 (data/) 没有 ls-files 输出时要保留, 让 git 自己判断
        keep = [t for t in targets
                if t in known_set or os.path.isdir(t)]
        if not keep:
            return None
        args = ["git", "commit", "-m", msg, "--"] + keep
    return sh(args)


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


def refresh_from_remote(paths, dirs, all_changes, branch, protect,
                       base_ref=None, stash_dir=None):
    """把远端最新版 checkout 到工作区, 然后恢复 protect 里本轮改过的文件。

    为什么必须这一步: 6 个工作流同时推 main, 每个都在 run 开始时 checkout。
    若 A 在 11:34 推了新的 latest.json, B 的工作区里仍是 10:49 的旧副本,
    B 直接 add 就会把 A 的新版覆盖回旧版 (2026-10-14 实测: ai-update 推的
    11:34 版本被随后 4 个 run 覆盖成 10:49)。

    protect = 本轮刚生成、绝不能被远端旧版冲掉的文件。调用方通过
    --protect 传入 (可多次)。其余路径一律以远端为准。因为调用方常用
    --dir data 这种粗粒度路径, 而 protect 是具体文件, 单靠"跳过匹配项"
    挡不住目录级 checkout (data/ 会把 data/realtime_minute.json 一并覆盖)。

    判定"本轮是否改过"用 base_ref (默认 HEAD): 若 protect 文件与 base_ref
    版本逐字节相同, 说明它只是 run 开始时 checkout 来的旧副本, 不是本轮
    成果, 不该保护 — 否则会把旧副本推上去, 覆盖别人刚推的新版
    (2026-10-14 实测: rebuild-pools 把 ai-update 13:19 的 latest.json
    覆盖回 10:49, 正因如此)。

    stash_dir: 本轮成果的副本目录。判定为「本轮改动」就存一份,
    后续重试循环直接从 stash 恢复 — 否则第一次循环的目录级 checkout
    已把工作区冲成远端版, 第二次循环再判永远是「未改动」, 本轮成果
    再也推不上去 (2026-10-15 实测: AI 复盘 826 字未上线, 12 个 protect
    文件全部报「本轮未改动」)。
    """
    protect = [p for p in (protect or []) if p]
    if all_changes:
        want = ['.']
    else:
        want = [p for p in paths if p] + [d for d in dirs if d]
    if not want:
        return
    ref = base_ref or 'HEAD'
    backup = {}
    for p in protect:
        if os.path.isfile(p):
            try:
                with open(p, 'rb') as f:
                    content = f.read()
            except OSError:
                print(f"protect: {p} 读取失败, 跳过")
                continue
            base = sh(["git", "show", f"{ref}:{p}"])
            if base.returncode == 0 and base.stdout.encode() == content:
                print(f"protect: {p} 本轮未改动, 不作为成果保护")
                continue
            print(f"protect: {p} 本轮已改动 (工作区{len(content)}B vs base{len(base.stdout)}B), "
                  f"纳入保护")
            backup[p] = content
        elif os.path.isdir(p):
            base_dir = p.rstrip('/')
            for root, _dirs, files in os.walk(base_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        with open(fp, 'rb') as f:
                            content = f.read()
                    except OSError:
                        continue
                    b = sh(["git", "show", f"{ref}:{fp}"])
                    if b.returncode == 0 and b.stdout.encode() == content:
                        continue
                    backup[fp] = content
    # 取回远端最新版 (目录级 checkout 会冲掉工作区)
    for w in want:
        sh(["git", "checkout", f"origin/{branch}", "--", w])
    # 把本轮成果写回去 (优先从 stash 恢复, 保证重试循环间不丢失)
    items = list(backup.items())
    if stash_dir:
        for p, content in backup.items():
            sp = os.path.join(stash_dir, p)
            os.makedirs(os.path.dirname(os.path.abspath(sp)), exist_ok=True)
            with open(sp, 'wb') as f:
                f.write(content)
        items = []
        for root, _dirs, files in os.walk(stash_dir):
            for fn in files:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, stash_dir)
                with open(fp, 'rb') as f:
                    items.append((rel, f.read()))
    for p, content in items:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(content)


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

    # base_ref 只在第一次循环前取一次。若放循环内, 第一次的
    # `git reset --soft origin/main` 会把 HEAD 指向 origin/main,
    # 第二次循环再 rev-parse 拿到的就是 origin/main 而非本 run 的
    # checkout 基线, 判定基准漂移; 且 refresh_from_remote 已把工作区
    # 改成远端版, 之后再判永远是「未改动」, 本轮成果再也推不上去。
    base_ref = sh(["git", "rev-parse", "HEAD"]).stdout.strip() or "HEAD"
    print(f"push baseline: {base_ref[:12]}")

    # 本轮成果的副本: 防止重试循环里被目录级 checkout 冲掉后判不出改动
    stash_dir = os.path.join(tempfile.mkdtemp(prefix="push_stash_"), "work")
    os.makedirs(stash_dir, exist_ok=True)

    for i in range(1, MAX_TRIES + 1):
        sh(["git", "fetch", "origin", args.branch])
        sh(["git", "reset", "--soft", f"origin/{args.branch}"])
        # 关键: 先把远端最新版取回工作区, 防止用本 run 开始时的旧副本
        # 覆盖其他工作流刚推送的新数据。
        refresh_from_remote(args.paths, args.dir, args.all, args.branch,
                             args.protect, base_ref, stash_dir)
        if not stage(args.paths, args.dir, args.all):
            print("no changes to push")
            return 0
        scope = staged_paths(args.paths, args.dir, args.all)
        if not has_our_changes(scope):
            print("no staged changes in our scope (remote already up to date)")
            return 0
        ts = datetime.now(CST).strftime("%Y%m%d_%H%M")
        r = commit_only(args.paths, args.dir, args.all, f"{args.msg}: {ts}")
        if r is None or r.returncode != 0:
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
            # 403 常见于并发 push 被拒; 用墙钟打印确证 sleep 真的等了
            # (Actions 日志时间戳是同一步骤内的相对时间, 看不出 sleep 是否生效)。
            print(f"backing off {wait}s (now {datetime.now().strftime('%H:%M:%S')})")
            time.sleep(wait)
            print(f"retry at {datetime.now().strftime('%H:%M:%S')}")

    print(f"::error::{MAX_TRIES} push attempts failed; this round's data was not published")
    return 1


if __name__ == "__main__":
    sys.exit(main())
