#!/usr/bin/env python3
"""
sync_gitcode.py — GitHub ↔ GitCode 双向同步工具 (本地手动版)

GitHub Actions 的 gitcode-sync.yml 每次 push/30min 自动同步;
本脚本用于本地手动同步与差异检查 (不依赖 Actions)。

用法:
  python3 sync_gitcode.py --status            # 对比两侧文件清单
  python3 sync_gitcode.py --pull-engines      # 拉 GitCode 新增引擎 → scripts/
  python3 sync_gitcode.py --push-data         # 推 data/ + scripts/ → GitCode

环境变量:
  GC_TOKEN  GitCode 个人访问令牌 (缺省用内置默认值)

平台分工见 docs/DUAL_PLATFORM.md:
  GitHub = CI + Pages + 采集 + 前端
  GitCode = 数据镜像 + 云容器算力 + 25个补充引擎
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GC_TOKEN = os.environ.get("GC_TOKEN") or "A_Ud4NyTDAsxcBJSqDyrJ2me"
GC_REPO = f"https://dufuwuxin:{GC_TOKEN}@gitcode.com/dufuwuxin/stock-system.git"
GC_NAME = "gitcode"


def git(*args, check=True, cwd=REPO):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def ensure_remote():
    """确保 gitcode remote 存在且指向带 token 的 URL。"""
    remotes = git("remote").split()
    if GC_NAME not in remotes:
        git("remote", "add", GC_NAME, GC_REPO)
    else:
        git("remote", "set-url", GC_NAME, GC_REPO)


def fetch_gitcode():
    ensure_remote()
    try:
        git("fetch", GC_NAME, "main")
        return True
    except RuntimeError as e:
        print(f"[sync] GitCode fetch 失败: {e}")
        return False


def remote_files(ref, patterns):
    """列出远端 ref 下匹配 patterns 的文件 (patterns 如 ['engines/', 'algorithms/'])。"""
    out = git("ls-tree", "-r", "--name-only", ref)
    files = [f for f in out.splitlines() if f]
    if patterns:
        files = [f for f in files if any(f.startswith(p) for p in patterns)]
    return files


def cmd_status():
    print("=" * 68)
    print("GitHub ↔ GitCode 文件差异")
    print("=" * 68)
    if not fetch_gitcode():
        return 1

    # 引擎对比: GitHub scripts/ vs GitCode engines/ + algorithms/
    gh_scripts = {os.path.basename(f) for f in os.listdir(os.path.join(REPO, "scripts"))
                  if f.endswith(".py")}
    gc_engines = {}
    for f in remote_files(f"{GC_NAME}/main", ["engines/", "algorithms/"]):
        gc_engines.setdefault(os.path.basename(f), f)

    only_gh = sorted(gh_scripts - set(gc_engines))
    only_gc = sorted(set(gc_engines) - gh_scripts)
    both = sorted(gh_scripts & set(gc_engines))

    print(f"\n[引擎/脚本] GitHub scripts/: {len(gh_scripts)} 个 | "
          f"GitCode engines/+algorithms/: {len(gc_engines)} 个")
    print(f"  共有: {len(both)} 个")
    if only_gh:
        print(f"\n  📤 GitHub 独有 ({len(only_gh)}):")
        for f in only_gh:
            print(f"     {f}")
    if only_gc:
        print(f"\n  📥 GitCode 独有 ({len(only_gc)}):")
        for f in only_gc:
            print(f"     {f}  ← {gc_engines[f]}")

    # 数据对比
    data_dir = os.path.join(REPO, "data")
    gh_data = {f for f in os.listdir(data_dir) if f.endswith(".json")} if os.path.isdir(data_dir) else set()
    gc_data = set(remote_files(f"{GC_NAME}/main", ["data/"]))
    gc_data = {os.path.basename(f) for f in gc_data}
    print(f"\n[数据] GitHub data/: {len(gh_data)} 个 json | GitCode data/: {len(gc_data)} 个 json")
    print(f"  仅本地: {sorted(gh_data - gc_data)[:8]}{' ...' if len(gh_data - gc_data) > 8 else ''}")

    print("\n提示: --pull-engines 拉取 GitCode 独有引擎; --push-data 推送到 GitCode")
    return 0


def cmd_pull_engines():
    print("[sync] 拉取 GitCode 独有引擎 → scripts/")
    if not fetch_gitcode():
        return 1
    n = 0
    for f in remote_files(f"{GC_NAME}/main", ["engines/", "algorithms/"]):
        base = os.path.basename(f)
        dst = os.path.join(REPO, "scripts", base)
        if os.path.exists(dst):
            continue
        content = git("show", f"{GC_NAME}/main:{f}", check=False)
        if not content:
            continue
        header = (f"# 来源: gitcode.com/dufuwuxin/stock-system {f}\n"
                  f"# (由 sync_gitcode.py --pull-engines 自动拉取)\n")
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(header + content)
        print(f"  ← scripts/{base}  (来自 {f})")
        n += 1
    print(f"[sync] 完成: 新增 {n} 个引擎")
    if n:
        print("[sync] 记得检查路径适配 (gitcode 侧用 ~/stockdb, 本项目用 paths.py)")
    return 0


def cmd_push_data():
    print("[sync] 推送 data/ + scripts/ → GitCode")
    if not fetch_gitcode():
        return 1
    # 用临时分支基于 GitCode main, 覆盖 data/ 与 engines/
    try:
        git("checkout", "-B", "gc-mirror", f"{GC_NAME}/main")
    except RuntimeError:
        print("[sync] 无法基于 GitCode main 建分支 (可能远端为空)")
        git("checkout", "-B", "gc-mirror")
    os.makedirs(os.path.join(REPO, "data"), exist_ok=True)
    os.makedirs(os.path.join(REPO, "engines"), exist_ok=True)
    n_data = n_eng = 0
    src_data = os.path.join(REPO, "data")
    for f in os.listdir(src_data):
        if f.endswith(".json"):
            with open(os.path.join(src_data, f), encoding="utf-8") as fh:
                content = fh.read()
            with open(os.path.join(REPO, "data", f), "w", encoding="utf-8") as fh:
                fh.write(content)
            n_data += 1
    src_scripts = os.path.join(REPO, "scripts")
    for f in os.listdir(src_scripts):
        if f.endswith(".py"):
            with open(os.path.join(src_scripts, f), encoding="utf-8") as fh:
                content = fh.read()
            with open(os.path.join(REPO, "engines", f), "w", encoding="utf-8") as fh:
                fh.write(content)
            n_eng += 1
    git("add", "-A", "data/", "engines/")
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO)
    if diff.returncode == 0:
        print("[sync] 无变化, GitCode 已是最新")
    else:
        git("config", "user.name", "stocks-sync-bot")
        git("config", "user.email", "3576982374@qq.com")
        git("commit", "-m", f"sync: data({n_data}) + engines({n_eng})")
        git("push", GC_NAME, "gc-mirror:main", "--force-with-lease")
        print(f"[sync] ✅ 已推送 {n_data} 个数据 + {n_eng} 个引擎 → GitCode")
    git("checkout", "main")
    return 0


def main():
    ap = argparse.ArgumentParser(description="GitHub ↔ GitCode 双向同步")
    ap.add_argument("--status", action="store_true", help="对比两侧文件清单")
    ap.add_argument("--pull-engines", action="store_true", help="拉取 GitCode 独有引擎")
    ap.add_argument("--push-data", action="store_true", help="推送数据+脚本到 GitCode")
    args = ap.parse_args()
    if args.pull_engines:
        return cmd_pull_engines()
    if args.push_data:
        return cmd_push_data()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
