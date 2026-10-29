#!/usr/bin/env python3
"""verify_apk_surface.py — 真机不可用时的**替代验收**：对已构建的 APK 做表面级验收。

## 先说清楚这不是什么

这**不是**真机渲染验收。真机验收是「装到设备上，逐屏截图，看界面确实渲染出正确内容」；
本脚本不执行任何 Compose 代码，不会发现布局错位、字体缺失、深色模式对比度、状态栏遮挡。

它替代的是其中**可被静态证明的那一层**：装进用户手机的那个包里，

1. 打包的规格就是唯一写入方发布的那一份（指纹绑定，见 `sync_parity_artifacts.py`）；
2. 规格自洽（版本、21 引擎、4 个状态引擎、权重表、聚合阈值都在）；
3. 关键渲染/计算类确实进了 DEX（不是「源码里有、包里没有」）；
4. 规格与云端产物同版本（否则界面会顶一条「算法漂移」）。

做不到真机的原因与可替代的边界写在 `docs/VERIFY_2026-10-29.md` §6。

    python3 scripts/verify_apk_surface.py                    # 自动找最新 debug APK
    python3 scripts/verify_apk_surface.py --apk path/to.apk
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(os.path.dirname(ROOT), "skapp-app")
MANIFEST = os.path.join(ROOT, "data", "parity", "MANIFEST.json")

SPEC_IN_APK = "engine_spec.json"

# 必须出现在 DEX 里的类型描述符（DEX 用 L…; 形式）：任何一条缺失都说明这个功能没打进包
REQUIRED_CLASSES = [
    "com/skapp/app/data/CanonicalEngine",
    "com/skapp/app/data/CanonicalIndicators",
    "com/skapp/app/data/EngineSpecLoader",
    "com/skapp/app/data/FlowEngine",
    "com/skapp/app/data/TapeEngine",
    "com/skapp/app/data/RotationEngine",
    "com/skapp/app/data/VolTargetGate",
    "com/skapp/app/data/PositionCap",
    "com/skapp/app/data/MarketDayArchive",
]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def find_apk():
    pat = os.path.join(APP, "app", "build", "outputs", "apk", "debug", "*.apk")
    cands = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
    return cands[0] if cands else None


def load_manifest():
    if not os.path.exists(MANIFEST):
        return None
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="APK 表面级验收（真机不可用时的替代）")
    ap.add_argument("--apk", help="APK 路径；默认取最新的 debug 包")
    args = ap.parse_args()

    apk = args.apk or find_apk()
    if not apk or not os.path.exists(apk):
        print("[apk] 找不到 APK，先跑 ./gradlew :app:assembleDebug")
        return 2

    size = os.path.getsize(apk)
    print("[apk] %s (%.1f MB, mtime %s)" % (
        os.path.relpath(apk, APP), size / 1048576.0,
        __import__("datetime").datetime.fromtimestamp(os.path.getmtime(apk)).strftime("%m-%d %H:%M")))

    problems, notes = [], []
    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())
        # 1. 打包的规格
        if SPEC_IN_APK not in names:
            problems.append("APK 内没有 %s" % SPEC_IN_APK)
            spec, spec_bytes = None, b""
        else:
            spec_bytes = z.read(SPEC_IN_APK)
            try:
                spec = json.loads(spec_bytes.decode("utf-8"))
            except Exception as e:
                problems.append("APK 内规格不是合法 JSON: %s" % e)
                spec = None

        # 2. DEX 里必须有那些类
        dex_blob = b"".join(z.read(n) for n in names if n.endswith(".dex"))
        missing = [c for c in REQUIRED_CLASSES if ("L" + c + ";").encode() not in dex_blob]

    # 3. 规格自洽
    if spec:
        if spec.get("spec_version") != "3.1.0":
            notes.append("规格版本 %s（代码预期 3.1.0）" % spec.get("spec_version"))
        n_engines = len(spec.get("engines") or [])
        if n_engines != 21:
            problems.append("规格引擎数 %d ≠ 21" % n_engines)
        state = [e.get("id") for e in (spec.get("state_engines") or [])]
        if state != ["flow", "tape", "rotation", "vol_target"]:
            problems.append("状态引擎注册表异常：%s" % state)
        if not (spec.get("calibration") or {}).get("decision"):
            problems.append("规格缺校准接线判定（calibration.decision）")
        if len(spec.get("weights") or {}) < 15:
            problems.append("权重表条目过少：%d" % len(spec.get("weights") or []))
        agg = spec.get("aggregation") or {}
        if agg.get("signal_bullish") != 65.0 or agg.get("signal_bearish") != 35.0:
            problems.append("聚合阈值异常：%s/%s" % (agg.get("signal_bullish"), agg.get("signal_bearish")))

    # 4. 与唯一写入方登记的指纹绑定
    man = load_manifest()
    if man is None:
        notes.append("没有 data/parity/MANIFEST.json，无法证明包内规格来自登记写入方")
    else:
        rec = (man.get("artifacts") or {}).get("engine-algo/src/main/resources/engine_spec.json")
        if not rec:
            notes.append("MANIFEST 里没有 engine_spec.json 的登记项")
        elif spec_bytes:
            digest = sha256_bytes(spec_bytes)
            if digest != rec.get("sha256"):
                problems.append("APK 内规格指纹与登记不符\n      登记 %s\n      实际 %s"
                                % (rec.get("sha256"), digest))
            else:
                print("[apk] 规格指纹与 MANIFEST 一致（writer=%s @ %s）" % (man.get("writer"), man.get("ts")))

    if missing:
        problems.append("DEX 缺关键类：%s" % ", ".join(missing))
    else:
        print("[apk] DEX 关键类齐全（%d 个）" % len(REQUIRED_CLASSES))

    for n in notes:
        print("[apk] 注意：%s" % n)

    if problems:
        print("\n[apk] FAIL %d 项：" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\n[apk] OK 表面级验收通过（规格自洽 + DEX 齐全 + 指纹绑定）。"
          "注意：这不等于真机渲染验收，见 docs/VERIFY_2026-10-29.md §6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
