#!/usr/bin/env python3
"""converge_mirror_c.py — 把 C 仓收敛回「纯采集镜像」定位。

C 仓 (dufuwuxin/stocks-ai-view-c) 的采集分片与 cron 错峰已由
scripts/deploy_mirror.py 正确配置 (只有 shard-collect / bulk-collect 保留
schedule, 其余 19 个已禁用)。剩下两处与「纯采集镜像」冲突:

  1. 开着 Pages (dufuwuxin.github.io/stocks-ai-view-c/) —— 合并后只保留
     A 仓一个站点, 这个入口会让用户看到第二份可能不同步的数据。
  2. pages-sync.yml 每 10 分钟把 A 仓 25 个文件 diff 到自己的 data/, 让它的
     前端走同源优先。A 仓 Pages 源就是 main 根目录, data/ 与 index.html
     天然同源, 这套同步已无意义, 白占 Actions 分钟。

本脚本只做这两件事 + 重写 MIRROR.md 说明定位, 不动 schedule。

用法: python3 converge_mirror_c.py [--dry-run]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

SRC = '/root/workspace/stocks-ai-view'
OWNER, REPO = 'dufuwuxin', 'stocks-ai-view-c'
DELETE_FILES = {'pages-sync.yml'}


def token():
    url = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], cwd=SRC).decode().strip()
    return re.search(r'https://[^:]+:([^@]+)@github\.com', url).group(1)


def api(path, method='GET', body=None, tok=None):
    req = urllib.request.Request(
        'https://api.github.com' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'token ' + tok, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print('  CMD FAILED:', ' '.join(cmd))
        print('  ', (r.stdout or '')[:300], (r.stderr or '')[:300])
        raise SystemExit(1)
    return r


MIRROR_MD = """# Mirror node C

stocks-ai-view 的采集镜像节点 (多账号分片, 分散出口 IP)。

- 代码唯一事实源: https://github.com/qwerzxcva/stocks-ai-view
- 本仓分片: c
- 本仓 cron 分钟位: 0,5,10,...,55 (mod 5)
- 只跑 shard-collect 与 bulk-collect; 其余 workflow 的 schedule 已由
  scripts/deploy_mirror.py 禁用 (避免与 A 仓重复执行整条流水线,
  重复烧 Actions 额度且 commit 互相覆盖)
- **本仓不承载站点**: 唯一站点是
  https://qwerzxcva.github.io/stocks-ai-view/ (A 仓, Pages 源即 main
  根目录, data/ 与 index.html 同源)。前端 gh_sources.js 并发读三仓 raw
  取 ts 最新的那份, 本仓只作为第三个采集出口与容错节点。
- 不要在本地手改代码, 会被下次 deploy_mirror.py 覆盖
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    tok = token()

    url = 'https://x-access-token:' + tok + '@github.com/' + OWNER + '/' + REPO + '.git'
    tmp = tempfile.mkdtemp(prefix='mirror-c-')
    try:
        r = run(['git', 'clone', '--depth', '1', url, tmp], check=False)
        if r.returncode != 0:
            print('clone failed:', (r.stderr or '')[:200])
            return 1

        n_del = 0
        for f in DELETE_FILES:
            p = os.path.join(tmp, '.github', 'workflows', f)
            if os.path.exists(p):
                os.remove(p)
                n_del += 1
                print('deleted .github/workflows/%s' % f)

        with open(os.path.join(tmp, 'MIRROR.md'), 'w', encoding='utf-8') as fh:
            fh.write(MIRROR_MD)
        print('MIRROR.md rewritten (collection-only, no site)')

        if args.dry_run:
            print('[dry-run] 不提交, 改动留在', tmp)
            return 0

        run(['git', 'config', 'user.name', 'mirror-deploy-bot'], cwd=tmp)
        run(['git', 'config', 'user.email', '3576982374@qq.com'], cwd=tmp)
        run(['git', 'add', '-A'], cwd=tmp)
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=tmp).returncode == 0:
            print('无变化, 跳过提交')
        else:
            run(['git', 'commit', '-m',
                 'mirror(c): collection-only node — drop pages-sync, no site'],
                cwd=tmp)
            r = run(['git', 'push', '-u', 'origin', 'main', '--force-with-lease'],
                    cwd=tmp, check=False)
            if r.returncode != 0:
                print('push 被拒, force 重试:', (r.stderr or '')[:120])
                run(['git', 'push', '-u', 'origin', 'main', '--force'], cwd=tmp)
            print('pushed: https://github.com/%s/%s' % (OWNER, REPO))

        st, _ = api('/repos/%s/%s/pages' % (OWNER, REPO), 'DELETE', tok=tok)
        if st == 204:
            print('C 仓 Pages 已关闭')
        elif st == 404:
            print('C 仓 Pages 未开启或已关闭')
        else:
            print('关闭 Pages 返回 http', st, '(可在仓设置里手动关)')
        return 0
    finally:
        if not args.dry_run:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
