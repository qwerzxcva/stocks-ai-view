#!/usr/bin/env python3
"""parity_matrix.py — 把「三道闸门」拆成逐用例 / 逐产物 / 逐文件的独立判定。

## 为什么要拆

原来 CI 只有 4 个聚合步骤（`2/4`、`2b/4`、`2c/4`、`2d/4`）：一红就知道「vol_target 那段漂了」，
但不知道**是哪一条用例、哪一个字段**；而且聚合步骤前一步失败，后面全部 `skipped`，
等于一次运行只能问出一个问题。2026-10-29 实测就是这样：2/4 红 → 2b~4/4 全 skipped。

这里把同一批校验拆成 N 个**互相独立**的项，逐项给 PASS/FAIL 与一句原因，
一部分红不影响其余项出结论：

- `spec.case.<code>`        规格黄金向量：每条样本单独判定（用内嵌 bars 重算）
- `engines.flow` / `engines.tape.<id>` / `engines.rotation.<id>` / `engines.vol_target.<id>` / `engines.params`
- `golden21.codes` / `golden21.external` / `golden21.case.<id>`
- `calib.params` / `calib.decision` / `calib.regime_cap`
- `artifact.<id>`           19 个落盘产物各自的契约
- `parity.<dst>`            10 个端上生成物各自的指纹与源指纹
- `manifest.engines`        引擎参数清单指纹
- `consistency.versions`    端云版本一致

退出码：0 全绿；1 有失败项（并逐条列出）。

    python3 scripts/parity_matrix.py            # 全量矩阵
    python3 scripts/parity_matrix.py -k tape    # 只看名字含 tape 的项
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import engine_spec  # noqa: E402
import engines_v2  # noqa: E402
import golden_21  # noqa: E402
from numcmp import deep_diff  # noqa: E402

RESULTS = []


def record(item, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # 单项异常不该拖垮整张表
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    RESULTS.append((item, ok, detail))


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


# ── 规格黄金向量：逐样本 ─────────────────────────────────────────────
def spec_items():
    old = _load("data/parity/golden_v5.json")
    for case in old.get("cases", []):
        code = case["code"]

        def check(case=case, code=code):
            if not case.get("bars"):
                return False, "用例缺内嵌 bars，无法脱离分片校验"
            fresh = engine_spec.expect_from_bars(engine_spec.bars_from_json(case["bars"]))
            if not fresh:
                return False, "重算为空（参考实现返回 None）"
            d = deep_diff(case.get("expect"), fresh, code)
            return (True, "OK") if not d else (False, d)

        record("spec.case.%s" % code, check)
    record("spec.version", lambda: (
        (True, old.get("spec_version")) if old.get("spec_version") == engine_spec.SPEC_VERSION
        else (False, "已提交 %s vs 代码 %s" % (old.get("spec_version"), engine_spec.SPEC_VERSION))))


# ── 状态引擎：逐段逐用例 ─────────────────────────────────────────────
def engine_items():
    old = _load("data/parity/golden_v2.json")
    new = engines_v2.build_golden()

    def section(name):
        def check():
            d = deep_diff(old.get(name), new.get(name), name)
            return (True, "OK") if not d else (False, d)
        return check

    for name in ("flow", "tape", "rotation", "vol_target"):
        record("engines.%s" % name, section(name))

    # 单独把每个用例拎出来，红了能指到具体用例
    for sec, key in (("tape", "cases"), ("rotation", "cases"), ("vol_target", "cases")):
        old_cases = {c.get("id", "#%d" % i): c for i, c in enumerate(old.get(sec, {}).get(key) or [])}
        new_cases = {c.get("id", "#%d" % i): c for i, c in enumerate(new.get(sec, {}).get(key) or [])}
        for cid, case in old_cases.items():
            def check(cid=cid, case=case, sec=sec, new_cases=new_cases):
                if cid not in new_cases:
                    return False, "重算结果里没有该用例"
                d = deep_diff(case, new_cases[cid], "%s.%s" % (sec, cid))
                return (True, "OK") if not d else (False, d)
            record("engines.%s.%s" % (sec, cid), check)

    # flow 的四个子场景
    for sub in ("small", "synth", "synth_bear", "tiers"):
        def check(sub=sub):
            d = deep_diff((old.get("flow") or {}).get(sub), (new.get("flow") or {}).get(sub),
                          "flow.%s" % sub)
            return (True, "OK") if not d else (False, d)
        record("engines.flow.%s" % sub, check)

    record("engines.params", lambda: (
        (True, "OK") if not deep_diff(old.get("params"), new.get("params"), "params")
        else (False, deep_diff(old.get("params"), new.get("params"), "params"))))


# ── 21 票全量黄金向量：逐用例 ────────────────────────────────────────
def golden21_items():
    old = _load("data/parity/golden_21.json")
    new = golden_21.build(os.path.join(ROOT, "data/parity/golden_v5.json"))

    for key in ("codes", "external_indicators"):
        def check(key=key):
            d = deep_diff(old.get(key), new.get(key), key)
            return (True, "OK") if not d else (False, d)
        record("golden21.%s" % key, check)

    old_cases = {c.get("id", "#%d" % i): c for i, c in enumerate(old.get("cases") or [])}
    new_cases = {c.get("id", "#%d" % i): c for i, c in enumerate(new.get("cases") or [])}
    for cid, case in old_cases.items():
        def check(cid=cid, case=case, new_cases=new_cases):
            if cid not in new_cases:
                return False, "重算结果里没有该用例"
            d = deep_diff(case, new_cases[cid], "case.%s" % cid)
            return (True, "OK") if not d else (False, d)
        record("golden21.case.%s" % cid, check)


# ── 校准参数 ────────────────────────────────────────────────────────
def calib_items():
    import calibrate_risk_regime as cal
    old = _load("data/risk_regime_params.json")
    doc = cal.recompute() if hasattr(cal, "recompute") else None
    if doc is None:
        # 脚本没有拆出纯函数：退回命令行口径，整项判定（不猜内部结构）
        record("calib.verify", lambda: _run_cli(
            ["python3", "scripts/calibrate_risk_regime.py", "--verify", "data/risk_regime_params.json"]))
        return
    record("calib.params", lambda: _cmp(old.get("params"), doc.get("chosen"), "params"))
    record("calib.decision", lambda: _cmp(
        (old.get("decision") or {}).get("wire"), (doc.get("decision") or {}).get("wire"), "wire"))
    record("calib.regime_cap", lambda: _cmp(old.get("regime_cap"), doc.get("regime_cap"), "regime_cap"))


def _cmp(a, b, path):
    d = deep_diff(a, b, path)
    return (True, "OK") if not d else (False, d)


def _run_cli(cmd):
    import subprocess
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return p.returncode == 0, out[-1] if out else "(无输出)"


# ── 落盘产物契约：逐个 ──────────────────────────────────────────────
def artifact_items():
    import engine_spec as es
    fails, warns = es.check_products(ROOT)
    fail_text = "\n".join(fails)
    warn_text = "\n".join(warns)
    for p in es.PRODUCTS:
        if p.get("artifact") is False:
            continue
        pid = p["id"]
        path = os.path.join(ROOT, p["path"])

        def check(pid=pid, path=path, fail_text=fail_text, warn_text=warn_text):
            if not os.path.exists(path):
                return False, "文件缺失（writer=%s）" % p.get("writer")
            hit = [ln for ln in fail_text.splitlines() if ln.startswith(pid + " ")]
            if hit:
                return False, hit[0]
            warn = [ln for ln in warn_text.splitlines() if ln.startswith(pid + " ")]
            return True, (warn[0] if warn else "OK")

        record("artifact.%s" % pid, check)


# ── 端上生成物指纹：逐个 ────────────────────────────────────────────
def parity_items():
    import sync_parity_artifacts as sync
    man_path = os.path.join(ROOT, "data/parity/MANIFEST.json")
    if not os.path.exists(man_path):
        record("parity.manifest", lambda: (False, "缺少 data/parity/MANIFEST.json"))
        return
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    for dst_rel, rec in (man.get("artifacts") or {}).items():
        def check(dst_rel=dst_rel, rec=rec):
            dst = os.path.join(sync.APP, dst_rel)
            if not os.path.exists(dst):
                return False, "端上文件缺失"
            actual = sync.sha256(dst)
            if actual != rec.get("sha256"):
                return False, "端上指纹不符（登记 %s… 实际 %s…）" % (rec["sha256"][:12], actual[:12])
            src = rec.get("source")
            if src:
                src_path = os.path.join(ROOT, src)
                if not os.path.exists(src_path):
                    return False, "云端源缺失: %s" % src
                if rec.get("source_sha256") and sync.sha256(src_path) != rec["source_sha256"]:
                    return False, "源已漂移（%s 变了但没重新 publish）" % src
            return True, "OK"
        record("parity.%s" % dst_rel, check)


# ── 清单与版本一致 ─────────────────────────────────────────────────
def manifest_items():
    record("manifest.engines", lambda: _run_cli(
        ["python3", "scripts/build_engine_manifest.py", "--check"]))
    record("consistency.versions", lambda: _run_cli(
        ["python3", "scripts/verify_engine_consistency.py"]))


def main():
    ap = argparse.ArgumentParser(description="细粒度闸门矩阵")
    ap.add_argument("-k", "--filter", help="只跑名字含该子串的项")
    ap.add_argument("-v", "--verbose", action="store_true", help="连通过的项也打详情")
    args = ap.parse_args()

    for fn in (spec_items, engine_items, golden21_items, calib_items,
               artifact_items, parity_items, manifest_items):
        fn()

    shown = [(i, ok, d) for (i, ok, d) in RESULTS if not args.filter or args.filter in i]
    failed = [(i, d) for (i, ok, d) in shown if not ok]
    for item, ok, detail in shown:
        if ok and not args.verbose:
            continue
        print("%-6s %-34s %s" % ("PASS" if ok else "FAIL", item, detail))
    print("\n[matrix] %d 项：%d 通过 / %d 失败%s" % (
        len(shown), len(shown) - len(failed), len(failed),
        "" if not args.filter else "（过滤 %r）" % args.filter))
    if failed:
        print("[matrix] 失败项：\n  " + "\n  ".join(i for i, _ in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
