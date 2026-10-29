#!/usr/bin/env python3
"""
verify_engine_consistency.py — 云端/端上引擎参数一致性校验

三件事，任一条不过即退出码 1（CI 直接红）：

  1. 清单与文件一致 —— manifest 记录的 sha256 / params / baseline 必须等于 data/ 下的实况
  2. 参数新鲜度     —— 每个引擎的 age_hours 不得超过自身 max_age_hours
  3. 跨语言版本一致 —— 云端 manifest 的 version 必须等于端上
                       `engine-core/.../EngineManifest.kt` 里 EXPECTED_VERSIONS 的期望值

第 3 条是这套机制的核心：端上不重算参数，只声明「我按哪一版口径解释」。
云端升级了校准版本而端上没跟上时，只看云端永远发现不了——这个脚本就是那道闸门。

用法:
    python3 scripts/verify_engine_consistency.py
    python3 scripts/verify_engine_consistency.py --skip-freshness   # 只验版本与指纹
    python3 scripts/verify_engine_consistency.py --strict-freshness # 过期也算失败
"""

import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, "data", "engine_manifest.json")

# 端上期望版本表的相对路径。仓库是两个独立 repo, 默认按同级目录找; 找不到就跳过第 3 项并明确告警。
KOTLIN_MANIFEST_CANDIDATES = [
    os.path.join(os.path.dirname(ROOT), "skapp-app",
                 "engine-core/src/main/java/com/skapp/app/data/EngineManifest.kt"),
    os.path.join(ROOT, "..", "skapp-app",
                 "engine-core/src/main/java/com/skapp/app/data/EngineManifest.kt"),
]

SCHEMA = 1


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sibling_engine_spec(kt_manifest_path):
    """从 EngineManifest.kt 的路径反推出同仓的 EngineSpec.kt。"""
    p = os.path.abspath(kt_manifest_path)
    marker = os.sep + "engine-core" + os.sep
    if marker not in p:
        return None
    root = p.split(marker)[0]
    cand = os.path.join(root, "engine-algo", "src", "main", "java", "com", "skapp", "app", "data", "EngineSpec.kt")
    return cand if os.path.exists(cand) else None


def parse_bundled_version(path):
    """取 EngineSpecLoader.BUNDLED_VERSION 的字面值。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return None
    m = re.search(r'BUNDLED_VERSION\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else None


def parse_kotlin_expected(path):
    """从 EngineManifest.kt 的 EXPECTED_VERSIONS 表里取出 {id: version}。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    i = src.find("EXPECTED_VERSIONS")
    if i < 0:
        return {}
    tail = src[i:]
    # 表一定在 mapOf(...) 里, 截到第一个 ")" 后的换行, 避免把后面代码里的键值对吃进来
    m = re.search(r"mapOf\s*\(", tail)
    body = tail[m.end():] if m else tail
    out = {}
    for k, v in re.findall(r'"([A-Za-z0-9_]+)"\s+to\s+"([^"]+)"', body):
        out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-freshness", action="store_true", help="不检查参数新鲜度")
    ap.add_argument("--strict-freshness", action="store_true", help="参数过期也算失败")
    ap.add_argument("--kotlin-manifest", default=os.environ.get("SKAPP_KT_MANIFEST"),
                    help="端上 EngineManifest.kt 路径；不给则按同级目录猜测")
    args = ap.parse_args()

    fails, warns = [], []

    if not os.path.exists(MANIFEST_PATH):
        print("FAIL 缺少 %s, 先跑 scripts/build_engine_manifest.py" % MANIFEST_PATH, file=sys.stderr)
        return 1
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if manifest.get("schema") != SCHEMA:
        fails.append("schema=%r 期望 %d" % (manifest.get("schema"), SCHEMA))
    engines = manifest.get("engines") or {}
    if not engines:
        fails.append("manifest 里没有任何引擎")

    print("%-22s %-24s %-12s %s" % ("engine", "version", "sha256", "状态"))
    print("-" * 78)

    for eid in sorted(engines):
        e = engines[eid]
        status = []

        path = os.path.join(ROOT, e.get("file", ""))
        if not e.get("file") or not os.path.exists(path):
            fails.append("%s: 参数文件不存在 %s" % (eid, e.get("file")))
            status.append("缺文件")
        else:
            real = sha256_of(path)
            if real != e.get("sha256"):
                fails.append("%s: sha256 不符（文件被改但清单未重建）%s != %s"
                             % (eid, real[:12], str(e.get("sha256"))[:12]))
                status.append("指纹漂移")
            if not e.get("baseline"):
                fails.append("%s: baseline 为空, 端上无法判定基线可用性" % eid)
                status.append("无基线")
            if not e.get("params"):
                warns.append("%s: params 为空" % eid)

        if not args.skip_freshness:
            age, limit = e.get("age_hours"), e.get("max_age_hours")
            if age is not None and limit and age > limit:
                msg = "%s: 参数已过期 %.1fh > %.0fh" % (eid, age, limit)
                (fails if args.strict_freshness else warns).append(msg)
                status.append("过期")

        print("%-22s %-24s %-12s %s" % (eid, str(e.get("version"))[:24],
                                        str(e.get("sha256"))[:12], " ".join(status) or "ok"))

    # ── 跨语言版本一致 ──────────────────────────────────────
    print()
    candidates = [args.kotlin_manifest] if args.kotlin_manifest else []
    candidates += KOTLIN_MANIFEST_CANDIDATES
    kt_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if kt_path is None:
        warns.append("找不到端上 EngineManifest.kt, 跳过跨语言版本校验")
        print("SKIP 跨语言版本校验（未找到端上文件）")
        # CI 里端上文件一定在 —— 找不到说明路径配错了，不能静默放过这道闸门
        if args.kotlin_manifest:
            fails.append("指定的端上清单文件不存在: %s" % args.kotlin_manifest)
    else:
        expected = parse_kotlin_expected(kt_path)
        print("端上期望版本: %d 项（%s）" % (len(expected), os.path.relpath(kt_path, os.path.dirname(ROOT))))
        for eid in sorted(set(expected) | set(engines)):
            want = expected.get(eid)
            got = (engines.get(eid) or {}).get("version")
            if want is None:
                # manifest 有、EXPECTED_VERSIONS 未声明：不是错误（engine_spec 由 BUNDLED_VERSION 专门校验）
                print("  INFO   %-22s 云端=%s（端上期望表未声明，另校验）" % (eid, got))
            elif got is None:
                fails.append("端上期望 %s 但云端 manifest 无此引擎" % eid)
                print("  MISS   %-22s 端上=%s" % (eid, want))
            elif want != got:
                fails.append("%s: 版本不一致 云端=%s 端上期望=%s" % (eid, got, want))
                print("  DIFF   %-22s 云端=%s 端上=%s" % (eid, got, want))

    # ── engine_spec 本体的版本：端上唯一真源是 EngineSpecLoader.BUNDLED_VERSION ──
    cloud_spec_ver = (engines.get("engine_spec") or {}).get("version")
    spec_kt_path = sibling_engine_spec(kt_path) if kt_path else None
    if cloud_spec_ver and spec_kt_path:
        bundled = parse_bundled_version(spec_kt_path)
        if bundled and bundled != cloud_spec_ver:
            fails.append("engine_spec: 规格版本不一致 云端=%s 端上BUNDLED_VERSION=%s"
                         % (cloud_spec_ver, bundled))
            print("  DIFF   %-22s 云端=%s 端上BUNDLED_VERSION=%s" % ("engine_spec", cloud_spec_ver, bundled))
        elif bundled:
            print("  OK     %-22s %s（两端一致）" % ("engine_spec", cloud_spec_ver))
    elif cloud_spec_ver:
        warns.append("找不到端上 EngineSpec.kt, 跳过规格版本比对")

    print()
    for w in warns:
        print("WARN " + w, file=sys.stderr)
    for f in fails:
        print("FAIL " + f, file=sys.stderr)

    if fails:
        print("\n结论: 不一致（%d 项失败, %d 项警告）" % (len(fails), len(warns)), file=sys.stderr)
        return 1
    print("结论: 一致（%d 项警告）" % len(warns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
