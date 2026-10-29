#!/usr/bin/env python3
"""sync_parity_artifacts.py — 规格与黄金向量同步到端上的**唯一写入方**。

## 要解决的并发覆盖

`skapp-app` 里有两类文件是「生成物、单边覆盖」：谁写谁赢，没有任何合并语义。

- `engine-algo/src/main/resources/engine_spec.json`（端上读的规格）
- `testdata/parity/golden_{v5,v2,21}.json`（parity 测试夹具）

2026-10-29 实测过一次并发：两个会话同时在同一个工作区里改 skapp，
任一方的 `cp`/重生成都会把另一方的版本顶掉，而且**顶掉之后没有任何东西会红**——
测试照样跑，只是拿的不是同一份规格。本脚本把这件事收敛成一个入口：

    publish  拿 flock → 跑四道 verify 闸门 → 原子写（tmp + os.replace）→ 登记 sha256
    check    重算磁盘指纹比对 MANIFEST：发现「别的进程改过」就红
    who      看租约状态与最近一次登记

租约只在 publish 期间持有（flock 只对活着的文件描述符有效，跨会话长期占锁是不成立的）；
跨会话的**事后**防护靠 MANIFEST 指纹：别人再写一次，`check` 下次就报出「不是我写的那版」。

## 为什么指纹比时间戳可靠

`mtime` 会被任何一次 `cp` 刷新，`sha256` 不会。`check` 比的是内容：
文件被外部改过 → 指纹不符 → 明确报出「谁写的、什么时候、现在是什么」。

## 不做什么

不替云端生成产物（那是 `engine_spec.py --out` / `golden_21.py --golden` 的事），
只负责**最后一次同步到端上**并留下可追溯记录。生成与同步分开，是为了让
「云端生成」和「端上拿到」之间有一道必须显式通过的门。

    python3 scripts/sync_parity_artifacts.py publish --writer "session-a"
    python3 scripts/sync_parity_artifacts.py check
"""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(os.path.dirname(ROOT), "skapp-app")  # 端上仓库与本仓同级
LOCK = os.path.join(ROOT, "data", "parity", ".publish.lock")
WRITER = os.path.join(ROOT, "data", "parity", ".writer.json")
MANIFEST = os.path.join(ROOT, "data", "parity", "MANIFEST.json")

# (云端源, 端上目标)。目标路径相对 skapp-app 根。
TARGETS = [
    ("data/engine_spec.json", "engine-algo/src/main/resources/engine_spec.json"),
    ("data/parity/golden_v5.json", "testdata/parity/golden_v5.json"),
    ("data/parity/golden_v2.json", "testdata/parity/golden_v2.json"),
    ("data/parity/golden_21.json", "testdata/parity/golden_21.json"),
    # 端上集成夹具（CloudArtifactIntegrationTest / UiRenderContractTest 读）：以前是手工 cp，
    # 同样无合并语义，一并收敛到本入口，check 才能报出「被别的会话改过」。
    ("data/engine_v5.json", "testdata/cloud/engine_v5.json"),
    ("data/engine_spec.json", "testdata/cloud/engine_spec.json"),
    ("data/risk_budget.json", "testdata/cloud/risk_budget.json"),
    ("data/engine_fusion.json", "testdata/cloud/engine_fusion.json"),
    ("data/market_phase.json", "testdata/cloud/market_phase.json"),
    ("data/limit_history.json", "testdata/cloud/limit_history.json"),
]

# publish 前必须全绿的四道闸门：任何一道红就不许同步（宁可不同步，也不要同步一份自相矛盾的规格）
GATES = [
    ["python3", "scripts/engines_v2.py", "--verify", "data/parity/golden_v2.json"],
    ["python3", "scripts/golden_21.py", "--verify", "data/parity/golden_21.json"],
    ["python3", "scripts/calibrate_risk_regime.py", "--verify", "data/risk_regime_params.json"],
    ["python3", "scripts/engine_spec.py", "--check-products"],
]

POLICY = {
    "single_writer": "scripts/sync_parity_artifacts.py publish",
    "forbidden": "手工 cp / 其它脚本直接写这些目标文件；一旦绕过，check 会报指纹不符",
    "rationale": "这两类文件是生成物、无合并语义；并发会话谁写谁赢且不会报错，必须收敛到一个入口",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.now(CST).isoformat(timespec="seconds")


def default_writer():
    return os.environ.get("SKAPP_WRITER") or "%s:%d" % (socket.gethostname(), os.getpid())


def acquire(writer, blocking=False):
    """拿 flock。返回 (fd, holder) —— fd 为 None 表示没拿到。"""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = None
        if os.path.exists(WRITER):
            try:
                with open(WRITER, encoding="utf-8") as fh:
                    holder = json.load(fh)
            except Exception:
                holder = None
        os.close(fd)
        return None, holder
    with open(WRITER, "w", encoding="utf-8") as fh:
        json.dump({"writer": writer, "since": now(), "pid": os.getpid()}, fh, ensure_ascii=False, indent=1)
    return fd, {"writer": writer, "since": now()}


def release(fd):
    if fd is None:
        return
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def run_gates():
    failures = []
    for cmd in GATES:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = (p.stdout or p.stderr or "").strip().splitlines()
        line = tail[-1] if tail else "(无输出)"
        status = "OK  " if p.returncode == 0 else "FAIL"
        print("  [%s] %s → %s" % (status, " ".join(cmd[1:]), line))
        if p.returncode != 0:
            failures.append(" ".join(cmd[1:]))
    return failures


def missing_sources():
    """云端源里当前不存在的那些。缺一条 = 该目标从 manifest 消失、check 从此不再看它。"""
    return [src_rel for src_rel, _ in TARGETS if not os.path.exists(os.path.join(ROOT, src_rel))]


def duplicate_targets():
    """目标路径必须唯一，否则 entries 里后者静默覆盖前者（登记数看着少了一项，没任何报错）。

    同源多目标**不是**重复：一个源文件可以有多个消费方（规格既进端上主资源，
    又被 `CloudArtifactIntegrationTest` 从云端产物目录读），两边都得是同一份字节。
    """
    seen, dups = set(), []
    for _src_rel, dst_rel in TARGETS:
        if dst_rel in seen:
            dups.append(dst_rel)
        seen.add(dst_rel)
    return dups

def multi_target_sources():
    """同一云端产物服务多个端上目标的登记，供人工确认「这是有意的多消费方，不是登记重复」。"""
    by_src = {}
    for src_rel, dst_rel in TARGETS:
        by_src.setdefault(src_rel, []).append(dst_rel)
    return {s: t for s, t in by_src.items() if len(t) > 1}

def cmd_publish(writer, force=False):
    dups = duplicate_targets()
    if dups:
        print("[publish] 拒绝同步：TARGETS 里目标路径重复 → %s" % ", ".join(dups))
        return 6
    failures = run_gates()
    if failures and not force:
        print("\n[publish] 拒绝同步：闸门未通过 → %s" % "; ".join(failures))
        print("[publish] 先用对应脚本重生成再同步；确认要强行同步用 --force（会写进 manifest 留痕）")
        return 4

    missing = missing_sources()
    if missing and not force:
        print("\n[publish] 拒绝同步：缺少云端产物 → %s" % ", ".join(missing))
        print("[publish] 缺一条就会少一条监控（该目标从 manifest 消失、check 不再比它）；先生成再用 --force 强制同步留痕")
        return 7

    fd, holder = acquire(writer)
    if fd is None:
        print("[publish] 拒绝同步：租约被占用 → %s" % json.dumps(holder, ensure_ascii=False))
        print("[publish] 并发会话正在写这些文件；等它 release，或确认它已停后再试")
        return 3
    try:
        entries = {}
        skipped = []
        for src_rel, dst_rel in TARGETS:
            src = os.path.join(ROOT, src_rel)
            dst = os.path.join(APP, dst_rel)
            if not os.path.exists(src):
                print("[publish] 缺少云端产物 %s，跳过（已记入 gates.failed）" % src_rel)
                skipped.append(src_rel)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            entries[dst_rel] = {
                "source": src_rel,
                # 源指纹：端上指纹只证明「没人改过端上」，证明不了「云端重生成后没重发」
                "source_sha256": sha256(src),
                "sha256": sha256(dst),
                "bytes": os.path.getsize(dst),
            }
            print("[publish] %s ← %s (%d B)" % (dst_rel, src_rel, entries[dst_rel]["bytes"]))

        manifest = {
            "ts": now(),
            "writer": writer,
            "policy": POLICY,
            "gates": {"failed": failures + ["缺少云端产物 %s" % x for x in skipped],
                      "forced": bool(failures or skipped)},
            "artifacts": entries,
        }
        tmp = MANIFEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, MANIFEST)
        print("[publish] 已登记 %s（%d 个文件，writer=%s）" % (
            os.path.relpath(MANIFEST, ROOT), len(entries), writer))
        for src_rel, dsts in sorted(multi_target_sources().items()):
            if all(d in entries for d in dsts):
                print("[publish] 同源多目标（非重复）：%s → %s" % (src_rel, ", ".join(dsts)))
        if skipped:
            print("[publish] 失败：缺 %d 个云端产物（%s），本次未同步且 manifest 已标 gates.failed"
                  % (len(skipped), ", ".join(skipped)))
            return 7
        return 0
    finally:
        release(fd)


def cmd_check():
    if not os.path.exists(MANIFEST):
        print("[check] 没有 MANIFEST.json：这些文件还没有归属记录，先跑一次 publish")
        return 5
    with open(MANIFEST, encoding="utf-8") as fh:
        man = json.load(fh)
    bad = []
    for dst_rel, rec in (man.get("artifacts") or {}).items():
        dst = os.path.join(APP, dst_rel)
        rec_sha = rec.get("sha256")
        if not os.path.exists(dst):
            bad.append((dst_rel, "文件缺失", rec_sha, None))
            continue
        actual = sha256(dst)
        if actual != rec_sha:
            bad.append((dst_rel, "指纹不符", rec_sha, actual))
            continue
        # 端上没被动过，还要问一句：云端源是不是已经重生成、只是没重发 publish
        src_rel = rec.get("source")
        src_sha = rec.get("source_sha256")
        if not src_rel or not src_sha:
            bad.append((dst_rel, "旧格式登记（无 source_sha256），无法判云端漂移", None,
                        "重新 publish 一次"))
            continue
        src = os.path.join(ROOT, src_rel)
        if not os.path.exists(src):
            bad.append((dst_rel, "源缺失：%s 不存在" % src_rel, src_sha, "(源不可得)"))
            continue
        src_now = sha256(src)
        if src_now != src_sha:
            bad.append((dst_rel, "源已漂移：%s 变了但没重新 publish（端上是旧产物）" % src_rel,
                        src_sha, src_now))
    if bad:
        print("[check] 以下生成物与登记不一致（writer=%s @ %s）："
              % (man.get("writer"), man.get("ts")))
        for rel, why, exp, act in bad:
            print("  - %s：%s" % (rel, why))
            print("      登记 %s" % exp)
            print("      实际 %s" % (act or "(无)"))
        print("[check] 处置：确认没有并发会话在写之后，用 publish 重新同步一次（会刷新登记）")
        return 1
    print("[check] OK %d 个生成物与登记指纹一致（writer=%s @ %s）"
          % (len(man.get("artifacts") or {}), man.get("writer"), man.get("ts")))
    return 0


def cmd_who():
    holder = None
    if os.path.exists(WRITER):
        try:
            with open(WRITER, encoding="utf-8") as fh:
                holder = json.load(fh)
        except Exception:
            holder = None
    fd, cur = acquire(default_writer())
    busy = fd is None
    release(fd)
    print("[who] 租约状态：%s" % ("被占用 → %s" % json.dumps(holder, ensure_ascii=False) if busy else "空闲"))
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        print("[who] 最近一次登记：writer=%s @ %s，%d 个文件"
              % (man.get("writer"), man.get("ts"), len(man.get("artifacts") or {})))
    else:
        print("[who] 尚无 MANIFEST.json")
    print("[who] 唯一写入方：%s" % POLICY["single_writer"])
    return 0


def main():
    ap = argparse.ArgumentParser(description="规格与黄金向量同步（唯一写入方）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--writer", default=default_writer())
    p.add_argument("--force", action="store_true", help="闸门红也强制同步（写进 manifest 留痕）")
    sub.add_parser("check")
    sub.add_parser("who")
    args = ap.parse_args()

    if args.cmd == "publish":
        return cmd_publish(args.writer, force=args.force)
    if args.cmd == "check":
        return cmd_check()
    return cmd_who()


if __name__ == "__main__":
    sys.exit(main())
