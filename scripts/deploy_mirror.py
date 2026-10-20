#!/usr/bin/env python3
"""
deploy_mirror.py — 部署 A 仓代码到 B/C 镜像仓并按分片改 cron

三账号分片采集架构:
  A 仓 (qwerzxcva/stocks-ai-view)  → shard a + e, cron mod5=2
  B 仓 (jiegengxiangjianxue/stocks-ai-view-b) → shard b, cron mod5=4
  C 仓 (dufuwuxin/stocks-ai-view-c)→ shard c,     cron mod5=0

为什么分片而不是每仓跑全部源: 国内金融源的风控按 IP 计数
(东财 push2 约 20 次请求后必然 TLS RST), 3 个 IP 各打一遍全量等于没分散;
按源分片后单 IP 请求量降到 1/3, 且某 IP 被封不影响其他分片。

token 一律从环境变量读, 绝不硬编码 (GitHub secret scanning 会拦 push):
  export MIRROR_TOKEN_B=ghp_xxx  MIRROR_TOKEN_C=ghp_xxx

用法:
  python3 deploy_mirror.py --label B [--dry-run]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOKENS = {"B": os.environ.get("MIRROR_TOKEN_B", ""),
          "C": os.environ.get("MIRROR_TOKEN_C", "")}
OWNERS = {"B": "jiegengxiangjianxue", "C": "dufuwuxin"}
REPOS = {"B": "stocks-ai-view-b", "C": "stocks-ai-view-c"}

# cron 分钟位错峰: A=mod5 2, B=mod5 4, C=mod5 0
MINUTE_SETS = {"B": "4,9,14,19,24,29,34,39,44,49,54,59",
               "C": "0,5,10,15,20,25,30,35,40,45,50,55"}

# 每个仓跑的分片 (A 仓不动, 由 shard-collect.yml 的默认值决定)
SHARDS = {"B": "b", "C": "c"}

# 只在 A 仓跑的 workflow (单值数据, 多仓跑会 x3 请求量触发风控)
A_ONLY = {"moneyflow-realtime.yml", "kline-backfill-full.yml",
          "nightly-backfill.yml", "dataset-archive.yml",
          "engine-chain.yml", "full-pipeline.yml",
          "realtime-minute.yml", "hot-impact-realtime.yml"}

# 镜像仓必须整文件删除的 workflow:
# 它们带 push 触发器且写外部仓 (gitcode), 只在 A 仓跑——
# 镜像仓 push 后自动触发, 会跟 A 仓互相覆盖 gitcode 上的数据。
REMOVE_ON_MIRROR = {"gitcode-sync.yml"}


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("  CMD FAILED:", " ".join(cmd))
        print("  ", (r.stdout or "")[:300], (r.stderr or "")[:300])
        raise SystemExit(1)
    return r


def patch_cron_minutes(src, minutes):
    """把 schedule cron 的分钟字段替换成错峰集合。src=文件内容。"""
    out = []
    for line in src.split("\n"):
        m = re.match(r"^(\s*-?\s*cron:\s*')([0-9,*/-]+)(\s+.*)$", line)
        if m:
            line = m.group(1) + minutes + m.group(3)
        out.append(line)
    return "\n".join(out)


def disable_schedule(src):
    """注释掉 on.schedule 块, 只留 workflow_dispatch。"""
    lines, out, in_sched, sched_indent = src.split("\n"), [], False, 0
    for line in lines:
        if not in_sched:
            m = re.match(r"^(\s*)schedule:\s*$", line)
            if m:
                in_sched, sched_indent = True, len(m.group(1))
                out.append(line + "  # disabled on mirror")
                continue
        else:
            if not line.strip() or line.lstrip().startswith("#"):
                out.append(line)
                continue
            if re.match(r"^\s*-\s*cron:", line):
                out.append("    # " + line.strip())
                continue
            if len(line) - len(line.lstrip()) <= sched_indent:
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
        print("[deploy %s] 缺少 token: export MIRROR_TOKEN_%s=ghp_xxx" % (L, L))
        return 1
    owner, repo, shard = OWNERS[L], REPOS[L], SHARDS[L]
    minutes = MINUTE_SETS[L]
    url = "https://x-access-token:" + tok + "@github.com/" + owner + "/" + repo + ".git"
    print("[deploy %s] %s/%s  shard=%s  cron分钟位=%s" % (L, owner, repo, shard, minutes))

    tmp = tempfile.mkdtemp(prefix="mirror-" + L + "-")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            os.makedirs(tmp, exist_ok=True)
            run(["git", "init"], cwd=tmp)
            run(["git", "remote", "add", "origin", url], cwd=tmp)

        # 复制代码 (排除 data/.git/__pycache__)
        for item in os.listdir(SRC):
            if item in (".git", "data", "node_modules", "__pycache__"):
                continue
            s, d = os.path.join(SRC, item), os.path.join(tmp, item)
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d)
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)

        # 改 workflow
        wf = os.path.join(tmp, ".github", "workflows")
        n_keep = n_dis = 0
        for f in sorted(os.listdir(wf)):
            p = os.path.join(wf, f)
            if not f.endswith(".yml"):
                continue
            if f in REMOVE_ON_MIRROR:
                os.remove(p)
                print(f"    {f}: REMOVED (writes external repo on push, A only)")
                n_dis += 1
                continue
            with open(p, encoding="utf-8") as fh:
                body = fh.read()
            if f in A_ONLY:
                new = disable_schedule(body)
                n_dis += 1
            elif f == "shard-collect.yml":
                # 分片采集: 改 cron 错峰 + 把 SHARD 默认值改成本仓的分片
                new = patch_cron_minutes(body, minutes)
                new = re.sub(r"SHARD: \$\{\{ github\.event\.inputs\.shard \|\| '[a-e]' \}\}",
                             "SHARD: ${{ github.event.inputs.shard || '%s' }}" % shard, new)
                new = re.sub(r"SHARD_EXTRA: \$\{\{ github\.event\.inputs\.shard \|\| '[a-e]' \}\}",
                             "SHARD_EXTRA: ${{ github.event.inputs.shard || '' }}", new)
                n_keep += 1
            else:
                new = disable_schedule(body)
                n_dis += 1
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new)
        print("  workflow: %d 个改 cron+分片, %d 个禁用 schedule" % (n_keep, n_dis))

        open(os.path.join(tmp, "MIRROR.md"), "w", encoding="utf-8").write(
            "# Mirror node %s\n\n"
            "stocks-ai-view 的采集镜像节点 (多账号分片, 分散出口 IP)。\n\n"
            "- 代码唯一事实源: https://github.com/qwerzxcva/stocks-ai-view\n"
            "- 本仓分片: %s (%s)\n"
            "- 本仓 cron 分钟位: %s (mod 5)\n"
            "- 只跑分片采集; AI/复盘/归档/回溯已禁用 (避免重复烧额度与 commit 冲突)\n"
            "- 不要在本地手改代码, 会被下次部署覆盖\n"
            % (L, shard, SHARDS[L], minutes))

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
            run(["git", "commit", "-m", "mirror(%s): shard %s + cron %s" % (L, shard, minutes)], cwd=tmp)
            run(["git", "branch", "-M", "main"], cwd=tmp)
            r = subprocess.run(["git", "push", "-u", "origin", "main", "--force-with-lease"],
                               cwd=tmp, capture_output=True, text=True)
            if r.returncode != 0:
                print("  push 被拒, force 重试:", (r.stderr or "")[:120])
                run(["git", "push", "-u", "origin", "main", "--force"], cwd=tmp)
            print("  pushed: https://github.com/%s/%s" % (owner, repo))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
