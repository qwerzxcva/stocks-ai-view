#!/usr/bin/env python3
"""
build_engine_manifest.py — 生成引擎参数版本清单 (data/engine_manifest.json)

端上 Kotlin 引擎与云端 Python 引擎必须吃同一份参数、认同一个版本、对得上同一个
回测基线。此前两边只有约定没有凭证: weight_calibrator_v3.py 改了阈值, 端上
FactorWeightStrategy 照样按旧口径算, 谁也不知道。manifest 就是这份凭证:

  version       参数产出者版本号。端上 EngineManifest.kt 写死期望值, 不一致即降级
  sha256        参数文件字节指纹。文件被改过但版本没升, 校验器立刻报出来
  baseline      回测基线指标。端上算不出来(要全市场样本), 只由 verify 脚本比对
  max_age_hours 产出新鲜度门槛。超过即视为过期, 与端上 CloudFeed.STALE_HOURS 呼应

用法:
    python3 scripts/build_engine_manifest.py           # 写入 data/engine_manifest.json
    python3 scripts/build_engine_manifest.py --check   # 只读比对, 有漂移退出码 1
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, "data", "engine_manifest.json")
SCHEMA = 1
CST = timezone(timedelta(hours=8))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def num(v):
    """统一成 float 或 None —— JSON 里这些字段时而字符串时而数字, 字典里不能混。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def text(v, limit=120):
    s = str(v or "").strip()
    return s[:limit]


def version_key(v):
    """从带说明的版本行里取出稳定的版本键。

    "weight_calibrator_v3 (分层 + 相对权重)" -> "weight_calibrator_v3"
    "decision v2 (9维因子 → 可执行决策卡)"  -> "decision v2"
    端上 EXPECTED_VERSIONS 写的就是这个键, 所以这里不能带说明文字。
    """
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"([A-Za-z0-9_\-]+(?:\s+v[\d.]+)?)", s)
    return m.group(1) if m else s.split(" ")[0]


def age_hours_of(ts):
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return round((datetime.now(CST) - dt).total_seconds() / 3600.0, 1)
    return None


# ── 各引擎的参数与基线提取 ────────────────────────────────────
# 只抽「端上口径依赖它」的字段。全量拷贝文件内容没有意义: 文件本身已有 sha256。

def spec_engine_spec(d):
    """双端算法规格本体 —— 最关键的一份参数, 指标集合/投票规则/聚合公式/权重都在这里。"""
    return {
        "version": str(d.get("spec_version") or ""),
        "params": {
            "weight_policy": text(d.get("weight_policy"), 40),
            "n_engines": len(d.get("engines") or []),
            "n_weights": len(d.get("weights") or {}),
        },
        "baseline": {
            "n_engines": len(d.get("engines") or []),
            "n_device_engines": len(d.get("device_engines") or []),
            "aggregation": (d.get("aggregation") or {}).get("kind"),
        },
        "max_age_hours": 168,
    }


def spec_weight_calibration(d):
    h = (d.get("horizons") or {}).get("5") or {}
    return {
        "version": version_key(d.get("engine")),
        "params": {
            "min_n": d.get("min_n"),
            "horizons": sorted((d.get("horizons") or {}).keys()),
            "indicators": [text(x.get("indicator"), 40) for x in (d.get("indicators") or [])],
            "preferred_horizon": 5,
        },
        "baseline": {
            "horizon_5": {
                "n": h.get("n"),
                "bull_win_rate": num(h.get("bull_win_rate")),
                "bear_win_rate": num(h.get("bear_win_rate")),
                "avg_ret_pct": num(h.get("avg_ret_pct")),
            }
        },
        "max_age_hours": 48,
    }


def spec_walkforward(d):
    s = d.get("summary") or {}
    return {
        "version": "walkforward_v1",
        "params": {k: v for k, v in (d.get("params") or {}).items()},
        "baseline": {
            "n_folds_valid": s.get("n_folds_valid"),
            "mean_is_win": num(s.get("mean_is_win")),
            "mean_oos_win": num(s.get("mean_oos_win")),
            "oos_vs_is_decay": num(s.get("oos_vs_is_decay")),
        },
        "max_age_hours": 168,
    }


def spec_engine_fusion(d):
    return {
        "version": "engine_fusion_v1",
        "params": {"parts": sorted((d.get("parts") or {}).keys())},
        "baseline": {"E": num(d.get("E")), "agreement": num(d.get("agreement"))},
        "max_age_hours": 48,
    }


def spec_decision(d):
    ev = d.get("evidence") or {}
    return {
        "version": version_key(d.get("engine")),
        "params": {"dimensions": 9},
        "baseline": {
            "market_regime": text(d.get("market_regime"), 24),
            "win_rate_1d": num(ev.get("win_rate_1d")),
            "sample_1d": ev.get("sample_1d"),
            "cards": len(d.get("cards") or []),
        },
        "max_age_hours": 48,
    }


def spec_market_phase(d):
    phases = d.get("phases") or []
    return {
        "version": "market_phase_v1",
        "params": {"phase_labels": sorted((d.get("labels") or {}).keys())},
        "baseline": {"n_daily": len(d.get("daily") or []), "n_phases": len(phases)},
        "max_age_hours": 168,
    }


def spec_strategy_card(d):
    bt = d.get("backtest") or {}
    return {
        "version": "strategy_card_v1",
        "params": {"strategy": text(d.get("strategy"), 80)},
        "baseline": {
            "win_rate_5d": num(bt.get("win_rate_5d")),
            "baseline_5d": num(bt.get("baseline_5d")),
            "alpha_pp": num(bt.get("alpha_pp")),
            "n": bt.get("n"),
        },
        "max_age_hours": 168,
    }


ENGINES = [
    ("engine_spec", "data/engine_spec.json", "scripts/engine_spec.py",
     ["EngineSpec.kt", "CanonicalEngine.kt"], spec_engine_spec),
    ("weight_calibration", "data/weight_calibration.json", "scripts/weight_calibrator_v3.py",
     ["FactorWeightStrategy.kt", "SignalCalibration.kt"], spec_weight_calibration),
    ("walkforward", "data/walkforward.json", "scripts/walkforward.py",
     ["SignalCalibration.kt"], spec_walkforward),
    ("engine_fusion", "data/engine_fusion.json", "scripts/engine_fusion.py",
     ["EngineFeed.kt"], spec_engine_fusion),
    ("decision", "data/decision.json", "scripts/decision_engine.py",
     ["DecisionScreen.kt"], spec_decision),
    ("market_phase", "data/market_phase.json", "scripts/phase_runner.py",
     ["MarketStateEngine.kt"], spec_market_phase),
    ("strategy_card", "data/strategy_card.json", "scripts/strategy_card.py",
     ["StrategyCard.kt"], spec_strategy_card),
]


def build():
    engines = {}
    problems = []
    for eid, rel, producer, consumers, extract in ENGINES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            problems.append("%s: 缺文件 %s" % (eid, rel))
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:
            problems.append("%s: 解析失败 %s" % (eid, e))
            continue
        try:
            body = extract(doc)
        except Exception as e:
            problems.append("%s: 提取失败 %s" % (eid, e))
            continue
        if not body.get("version"):
            problems.append("%s: 版本字段为空" % eid)
        engines[eid] = {
            "version": body["version"],
            "file": rel,
            "sha256": sha256_of(path),
            "produced_by": producer,
            "consumers": consumers,
            "params": body.get("params") or {},
            "baseline": body.get("baseline") or {},
            "max_age_hours": body.get("max_age_hours"),
            "ts": text(doc.get("ts"), 32),
            "age_hours": age_hours_of(doc.get("ts")),
        }
    manifest = {
        "schema": SCHEMA,
        "generator": "scripts/build_engine_manifest.py",
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "engines": engines,
    }
    return manifest, problems


def comparable(manifest):
    """比对时剔掉生成时间与派生新鲜度 —— 否则每次 build / 每过 6 分钟都被判成漂移。

    age_hours 由 ts 与当前时刻算出（保留 1 位小数），隔 6 分钟必差 0.1，
    拿它比对会让 --check 变成时钟阀；ts 本身仍在比对集内，文件被换仍能抓到。
    """
    m = dict(manifest)
    m.pop("generated_at", None)
    engines = {}
    for eid, e in (m.get("engines") or {}).items():
        e = dict(e)
        e.pop("age_hours", None)
        engines[eid] = e
    m["engines"] = engines
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对不写盘")
    args = ap.parse_args()

    manifest, problems = build()
    for p in problems:
        print("PROBLEM " + p, file=sys.stderr)

    if args.check:
        if not os.path.exists(MANIFEST_PATH):
            print("FAIL manifest 不存在: %s" % MANIFEST_PATH, file=sys.stderr)
            return 1
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        if comparable(old) != comparable(manifest):
            a, b = comparable(old).get("engines", {}), manifest["engines"]
            print("FAIL manifest 与数据文件不一致:", file=sys.stderr)
            for eid in sorted(set(a) | set(b)):
                if a.get(eid) != b.get(eid):
                    print("  %s 漂移 (sha/params/baseline 有变)" % eid, file=sys.stderr)
            return 1
        print("OK manifest 一致, %d 个引擎" % len(manifest["engines"]))
        return 0 if not problems else 1

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("写入 %s (%d 个引擎)" % (os.path.relpath(MANIFEST_PATH, ROOT), len(manifest["engines"])))
    for eid, e in manifest["engines"].items():
        stale = e["age_hours"] is not None and e["max_age_hours"] and e["age_hours"] > e["max_age_hours"]
        print("  %-20s %-32s %s%s" % (eid, e["version"][:32], e["sha256"][:12], "  [STALE]" if stale else ""))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
