#!/usr/bin/env python3
"""
sync_gitcode.py — GitHub ↔ GitCode 双向同步工具 (本地执行)

  GitHub  qwerzxcva/stocks-ai-view : Actions CI + Pages + 数据采集 + scripts/*.py
  GitCode dufuwuxin/stock-system   : 数据镜像 + 云容器算力 + engines/ + algorithms/

GitCode 没有 Actions API (404), CI 只能跑 GitHub, 再把数据/脚本镜像过去;
GitCode 侧云容器产出的引擎再由本工具或 .github/workflows/gitcode-sync.yml 回流 GitHub。

命令 (三选一):
  --status        对比两侧文件清单: GitHub独有 / GitCode独有 / 共有但不同
  --pull-engines  把 GitCode engines/ + algorithms/ 里 GitHub 没有的 .py 拉到 scripts/
  --push-data     把 data/*.json + scripts/*.py 推到 GitCode

token: 读环境变量 GC_TOKEN; 未设置时用内置默认值 (dufuwuxin 的 GitCode PAT, 已验
       证可 clone/push)。只走 subprocess + git, 不依赖 requests。

用法:
  python3 scripts/sync_gitcode.py --status
  python3 scripts/sync_gitcode.py --pull-engines
  python3 scripts/sync_gitcode.py --push-data
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
DATA_DIR = os.path.join(REPO_ROOT, "data")

GC_HOST = "gitcode.com"
GC_OWNER_REPO = "dufuwuxin/stock-system"
GC_REMOTE = "gitcode"
GC_BRANCH = "main"
GC_REF = f"{GC_REMOTE}/{GC_BRANCH}"
# GitCode PAT 从环境变量 GC_TOKEN 读 (勿硬编码, GitHub secret scanning 会拦截)
DEFAULT_TOKEN = os.environ.get("GC_TOKEN", "")

# 从 GitCode 拉引擎时只认这两个目录
GC_ENGINE_DIRS = ("engines", "algorithms")
# GitHub 侧脚本推到 GitCode 后的落点 (与 workflow 步骤 C 一致)
GC_SCRIPT_DEST = "engines"

PULL_HEADER = (
    "# ═══ 来源: GitCode {repo} · {src_path} ═══\n"
    "# 上游 commit: {sha:.12} · 拉取时间: {ts} · 工具: scripts/sync_gitcode.py --pull-engines\n"
    "# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。\n"
)


def gc_token():
    tok = os.environ.get("GC_TOKEN") or DEFAULT_TOKEN
    if not os.environ.get("GC_TOKEN"):
        print("! 未设置环境变量 GC_TOKEN, 使用内置默认 token")
    if not tok:
        die("GC_TOKEN 为空: 请 export GC_TOKEN=<gitcode personal access token>")
    return tok


def gc_url(token):
    return f"https://x-access-token:{token}@{GC_HOST}/{GC_OWNER_REPO}.git"


def die(msg, code=1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def redact(text):
    """打印命令前抹掉 token, 避免泄漏到日志。"""
    tok = os.environ.get("GC_TOKEN") or DEFAULT_TOKEN
    return text.replace(tok, "***") if tok else text


def run(cmd, cwd=REPO_ROOT, check=True, capture=True):
    """执行 git 子命令并回显 (token 已脱敏)。"""
    shown = redact(" ".join(cmd))
    print(f"  $ {shown}")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(cmd, cwd=cwd, env=env,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None,
                       text=True)
    if p.returncode != 0 and check:
        out = (p.stdout or "").strip()
        die(f"命令失败 (rc={p.returncode}): {shown}\n{out}")
    return p


def git(*args, cwd=REPO_ROOT, check=True, quiet=False):
    """执行 git 子命令; quiet=True 时不回显 (用于批量内部调用)。"""
    if quiet:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        p = subprocess.run(["git", *args], cwd=cwd, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0 and check:
            die(f"git {' '.join(args)} 失败 (rc={p.returncode}):\n{(p.stdout or '').strip()}")
        return p
    return run(["git", *args], cwd=cwd, check=check)


def ensure_gitcode_remote(token):
    """本地仓库配置 gitcode remote (幂等)。"""
    p = git("remote", "get-url", GC_REMOTE, check=False)
    if p.returncode == 0:
        git("remote", "set-url", GC_REMOTE, gc_url(token))
    else:
        git("remote", "add", GC_REMOTE, gc_url(token))
    print(f"✓ remote '{GC_REMOTE}' -> {GC_HOST}/{GC_OWNER_REPO}.git")


def fetch_gitcode():
    p = git("fetch", GC_REMOTE, GC_BRANCH, check=False)
    if p.returncode != 0:
        die("无法访问 GitCode (fetch 失败)。请检查: 1) 网络 2) GC_TOKEN 权限 "
            f"(需要 read_api + write_repository) 3) 仓库 {GC_OWNER_REPO} 是否存在")
    print(f"✓ 已 fetch {GC_REF}")


def ls_tree(ref):
    """返回 {path: blob_sha}, 只含 blob。"""
    p = git("ls-tree", "-r", ref, quiet=True)
    out = {}
    for line in p.stdout.splitlines():
        meta, path = line.split("\t", 1)
        mode, otype, sha = meta.split()
        if otype == "blob":
            out[path] = sha
    return out


def local_map(subdir, suffix):
    """本地目录文件 -> {文件名: (绝对路径, blob sha)}"""
    d = os.path.join(REPO_ROOT, subdir)
    if not os.path.isdir(d):
        return {}
    res = {}
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if not os.path.isfile(full) or not name.endswith(suffix):
            continue
        res[name] = (full, git("hash-object", full, quiet=True).stdout.strip())
    return res


def gc_engine_map(tree):
    """GitCode engines/ + algorithms/ 下的 .py -> {文件名: (仓库内路径, sha)}"""
    out = {}
    for path, sha in sorted(tree.items()):
        parts = path.split("/")
        if len(parts) == 2 and parts[0] in GC_ENGINE_DIRS and path.endswith(".py"):
            name = parts[1]
            # engines/ 优先于 algorithms/
            if name not in out or parts[0] == "engines":
                out[name] = (path, sha)
    return out


def gc_data_map(tree):
    return {os.path.basename(p): (p, s) for p, s in tree.items()
            if p.startswith("data/") and p.endswith(".json") and "/" not in p[len("data/"):]}


def print_group(title, items, hint=""):
    print(f"\n{title} ({len(items)}){hint}")
    if not items:
        print("  (无)")
        return
    for it in items:
        print(f"  {it}")


# ───────────────────────────────────────────────────────────────
# --status
# ───────────────────────────────────────────────────────────────
def cmd_status():
    print("== GitHub ↔ GitCode 文件清单对比 ==")
    ensure_gitcode_remote(gc_token())
    fetch_gitcode()
    tree = ls_tree(GC_REF)
    gc_eng = gc_engine_map(tree)
    gc_data = gc_data_map(tree)

    gh_scripts = local_map("scripts", ".py")
    gh_data = local_map("data", ".json")

    # ── 脚本 / 引擎 ──
    both_scripts = sorted(set(gh_scripts) & set(gc_eng))
    gh_only_s = sorted(set(gh_scripts) - set(gc_eng))
    gc_only_s = sorted(set(gc_eng) - set(gh_scripts))
    diff_s = [n for n in both_scripts if gh_scripts[n][1] != gc_eng[n][1]]

    print_group("■ 脚本/引擎 · GitHub 独有 (scripts/)", gh_only_s,
                "  → 可用 --push-data 推到 GitCode engines/")
    print_group("■ 脚本/引擎 · GitCode 独有 (engines/+algorithms/)",
                [f"{n}  [{gc_eng[n][0]}]" for n in gc_only_s],
                "  → 可用 --pull-engines 拉到 scripts/")
    print_group("■ 脚本/引擎 · 共有但内容不同",
                [f"{n}  (gh={gh_scripts[n][1][:7]} gc={gc_eng[n][1][:7]})" for n in diff_s],
                "  → push 会用 GitHub 版覆盖, pull 不会动已有文件")

    # ── 数据 ──
    both_d = sorted(set(gh_data) & set(gc_data))
    gh_only_d = sorted(set(gh_data) - set(gc_data))
    gc_only_d = sorted(set(gc_data) - set(gh_data))
    diff_d = [n for n in both_d if gh_data[n][1] != gc_data[n][1]]

    print_group("■ 数据 · GitHub 独有 (data/*.json)", gh_only_d)
    print_group("■ 数据 · GitCode 独有 (data/*.json)", gc_only_d)
    print_group("■ 数据 · 共有但内容不同", diff_d, "  → push 会用 GitHub 版覆盖")

    total_same = len(both_scripts) - len(diff_s)
    print(f"\n统计: 脚本 GitHub {len(gh_scripts)} 个 / GitCode {len(gc_eng)} 个, "
          f"共有 {len(both_scripts)} (其中一致 {total_same}, 不同 {len(diff_s)}); "
          f"数据 GitHub {len(gh_data)} / GitCode {len(gc_data)}, "
          f"不同 {len(diff_d)}")
    print("== 对比完成 ==")
    return 0


# ───────────────────────────────────────────────────────────────
# --pull-engines
# ───────────────────────────────────────────────────────────────
def cmd_pull_engines():
    print("== 从 GitCode 拉取新增引擎到 scripts/ ==")
    ensure_gitcode_remote(gc_token())
    fetch_gitcode()
    tree = ls_tree(GC_REF)
    gc_eng = gc_engine_map(tree)
    gh_scripts = local_map("scripts", ".py")

    new = sorted(set(gc_eng) - set(gh_scripts))
    shared = sorted(set(gc_eng) & set(gh_scripts))
    if not new:
        print(f"✓ GitCode engines/+algorithms/ 中没有 GitHub 缺失的 .py "
              f"(共有 {len(shared)} 个已存在, 不覆盖)")
        return 0

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    pulled = []
    for name in new:
        src_path, sha = gc_eng[name]
        dst = os.path.join(SCRIPTS_DIR, name)
        if os.path.exists(dst):
            print(f"  ! 跳过 {name}: scripts/ 已存在同名文件")
            continue
        content = git("show", f"{GC_REF}:{src_path}").stdout
        header = PULL_HEADER.format(repo=GC_OWNER_REPO, src_path=src_path,
                                    sha=sha, ts=ts)
        lines = content.split("\n")
        if lines and lines[0].startswith("#!"):
            body = lines[0] + "\n" + header + "\n".join(lines[1:])
        else:
            body = header + content
        with open(dst, "w", encoding="utf-8") as f:
            f.write(body)
        pulled.append((name, src_path))
        print(f"  ← {src_path} -> scripts/{name}")

    print(f"\n✓ 拉取 {len(pulled)} 个引擎, 跳过 {len(shared)} 个 GitHub 已有文件:")
    for name, src in pulled:
        print(f"    scripts/{name}  (来自 {src})")
    print("\n下一步: git add scripts/ && git commit -m 'feat: 吸收 GitCode 引擎'")
    print("注意: 拉下来的引擎可能依赖 GitCode 侧的 paths/数据约定, 接入前请先读文件头。")
    return 0


# ───────────────────────────────────────────────────────────────
# --push-data
# ───────────────────────────────────────────────────────────────
def cmd_push_data():
    print("== 推送 data/ + scripts/ 到 GitCode ==")
    token = gc_token()
    ensure_gitcode_remote(token)
    fetch_gitcode()

    gh_scripts = local_map("scripts", ".py")
    gh_data = local_map("data", ".json")
    gc_eng = gc_engine_map(ls_tree(GC_REF))
    overwrite = sorted(set(gh_scripts) & set(gc_eng))

    wt = os.path.join(tempfile.gettempdir(), f"gc-push-{os.getpid()}")
    if os.path.exists(wt):
        shutil.rmtree(wt)
    git("worktree", "add", "--detach", wt, GC_REF)
    print(f"✓ 基于 {GC_REF} 建立临时工作树 {wt}")
    try:
        # data/*.json 原样镜像; scripts/*.py 落到 engines/
        os.makedirs(os.path.join(wt, "data"), exist_ok=True)
        n_data = 0
        for name, (src, _) in gh_data.items():
            shutil.copy2(src, os.path.join(wt, "data", name))
            n_data += 1
        n_new = n_upd = 0
        for name, (src, _) in gh_scripts.items():
            dst_dir = os.path.join(wt, GC_SCRIPT_DEST)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(dst_dir, name))
            if name in gc_eng:
                n_upd += 1
            else:
                n_new += 1

        git("add", "-A", "data", GC_SCRIPT_DEST, cwd=wt)
        if git("diff", "--cached", "--quiet", "HEAD", cwd=wt, check=False).returncode == 0:
            print("✓ 无变化: GitCode 已是最新 (data/ 与 engines/ 与本地一致)")
            return 0

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        git("config", "user.name", "stocks-sync-bot", cwd=wt)
        git("config", "user.email", "3576982374@qq.com", cwd=wt)
        git("commit", "-m",
            f"mirror: {ts} (GitHub → GitCode 数据/脚本同步)\n\n"
            f"data: {n_data} 个 json, engines: +{n_new} 新增 / ~{n_upd} 覆盖", cwd=wt)
        print(f"✓ 本地 commit 完成: data {n_data} 个, engines 新增 {n_new} / 覆盖 {n_upd}")

        p = git("push", GC_REMOTE, f"HEAD:{GC_BRANCH}", "--force-with-lease", check=False)
        if p.returncode != 0:
            print("! push 被拒绝 (GitCode 侧历史分叉或并发提交), 尝试 fetch + merge -X ours 后重试")
            git("fetch", GC_REMOTE, GC_BRANCH)
            git("merge", "--no-edit", GC_REF, "-X", "ours", cwd=wt, check=False)
            p = git("push", GC_REMOTE, f"HEAD:{GC_BRANCH}", "--force-with-lease", check=False)
            if p.returncode != 0:
                die("GitCode push 失败 (两次均被拒绝)。大概率是 token 无 write_repository 权限, "
                    "或 GitCode 侧有保护分支规则。")
        print(f"✓ 已推送到 GitCode: https://{GC_HOST}/{GC_OWNER_REPO}")
        if overwrite:
            print(f"  覆盖 GitCode 同名引擎 {len(overwrite)} 个 (GitHub 为权威版本): "
                  + ", ".join(overwrite[:8]) + (" ..." if len(overwrite) > 8 else ""))
        return 0
    finally:
        git("worktree", "remove", "--force", wt, check=False)
        git("worktree", "prune", check=False)


def main():
    ap = argparse.ArgumentParser(
        description="GitHub ↔ GitCode 双向同步 (GitCode 无 Actions, CI 跑 GitHub 再镜像)")
    # 任务约定 --pull-engines / --push-data / --status 三选一; argparse 的 choices 不
    # 接受以 -- 开头的值, 故用互斥 flag 而非 positional choices。
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--status", action="store_true", help="对比两侧文件清单")
    g.add_argument("--pull-engines", action="store_true", dest="pull_engines",
                   help="把 GitCode engines/ + algorithms/ 里 GitHub 没有的 .py 拉到 scripts/")
    g.add_argument("--push-data", action="store_true", dest="push_data",
                   help="把 data/*.json + scripts/*.py 推到 GitCode")
    args = ap.parse_args()
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        die(f"{REPO_ROOT} 不是 git 仓库, 请在 stocks-ai-view 目录下运行")
    if args.status:
        return cmd_status()
    if args.pull_engines:
        return cmd_pull_engines()
    return cmd_push_data()


if __name__ == "__main__":
    sys.exit(main())
