#!/usr/bin/env python3
"""golden_21.py — 21 票全量黄金向量（18 技术票 + 3 外部增强票）。

## 之前缺的是什么

`golden_v5.json` 只有 18 票：板块联动（SectorResonance）、资金流一致性
（CapitalFlowConsistency）、涨停池（InZTPool/InDTPool）这三票需要云端外部数据，
端上拿不到，于是「21 票一致性」只执法了 18 票——真正会漂的恰恰是端上拿不到、
只能靠云端注入的那 3 票：一旦云端改了口径，端上照旧用旧规则拼分数，没有任何测试会红。

## 做法

外部票的**判定规则仍然由云端参考实现本体产出**（`engine_v5.build_votes`），
本脚本只伪造 `all_sources.json` / `moneyflow.json` 的输入：

- 板块共振：`data.sector` 里给该股所属行业一个确定的当日涨幅
- 资金流：`moneyflow.json` 里给 3 日累计 / 当日主力净额
- 涨停池：`data.quote` 里 `kind=ZT/DT` 的明细，含连板数

外部数据目录通过 monkeypatch `engine_v5._data_dir` 指向临时夹具目录，
保证结果不随仓库里实际的 `data/moneyflow.json` 变化——黄金向量必须可复现。

输出 `data/parity/golden_21.json`：K 线按代码存一份，场景单独定义，
用例把「代码 × 场景」组合起来。端上 `Canonical21VoteParityTest` 注入同一批外部票，
比对全部 21 票与聚合结果。

    python3 scripts/golden_21.py --golden data/parity/golden_21.json
    python3 scripts/golden_21.py --selftest
"""
import argparse
import json

from numcmp import deep_diff
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import engine_v5 as E5  # noqa: E402

SPEC_VERSION = "3.1.0"
EXTERNAL_INDICATORS = ("SectorResonance", "CapitalFlowConsistency", "InZTPool", "InDTPool")

# 场景：每一个都刻意踩到不同的外部票分支
SCENARIOS = [
    {
        "id": "sector_hot_zt",
        "industry": "半导体",
        "sector": [{"sector": "半导体", "chg_pct": 3.6}],
        "zt": [{"code": "@CODE@", "lianban": 4}],
        "dt": [],
        "moneyflow": {"net_main_wan": 12000.0, "net3_wan": 45000.0},
    },
    {
        "id": "sector_cold_dt",
        "industry": "房地产",
        "sector": [{"sector": "房地产", "chg_pct": -2.8}],
        "zt": [],
        "dt": [{"code": "@CODE@", "lianban": 1}],
        "moneyflow": {"net_main_wan": -20000.0, "net3_wan": -80000.0},
    },
    {
        "id": "neutral_mixed",
        "industry": "银行",
        "sector": [{"sector": "银行", "chg_pct": 0.4}],
        "zt": [],
        "dt": [],
        "moneyflow": {"net_main_wan": 9000.0, "net3_wan": -3000.0},
    },
    {
        "id": "no_external",
        "industry": None,
        "sector": None,
        "zt": [],
        "dt": [],
        "moneyflow": None,
    },
]


def bare(code):
    return str(code).lower().lstrip("shszbj").zfill(6)


def build_sources(scn, code):
    """把场景展开成 all_sources / moneyflow 夹具。"""
    code6 = bare(code)
    if scn["sector"] is None:
        return None, None
    all_sources = {
        "data": {
            "sector": scn["sector"],
            "quote": [
                {"kind": "ZT", "detail": [dict(x, code=x["code"].replace("@CODE@", code6)) for x in scn["zt"]]},
                {"kind": "DT", "detail": [dict(x, code=x["code"].replace("@CODE@", code6)) for x in scn["dt"]]},
            ],
        }
    }
    moneyflow = None
    if scn["moneyflow"] is not None:
        moneyflow = {"flows": {code6: dict(scn["moneyflow"])}}
    return all_sources, moneyflow


def load_weights():
    """校准权重（与 engine_v5 --weights 默认路径一致）。

    不加载就会用等权，端上却用 spec.weights，两端分数会差到 0.8 分——
    那种差不是「算法漂移」，是把两份不同的权重当成同一份在比。
    """
    p = os.path.join(ROOT, "data", "engine_weights.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: float(v) for k, v in (raw.get("weights") or {}).items()}


def run_cases(v5_cases, weights=None):
    """用云端参考实现逐用例产出 21 票。"""
    codes = []
    cases = []
    for c in v5_cases:
        code = c["code"]
        bars = c["bars"]
        codes.append({"code": code, "bars": bars})
        close = [b["c"] for b in bars]
        high = [b["h"] for b in bars]
        low = [b["l"] for b in bars]
        vol = [b["v"] for b in bars]

        for scn in SCENARIOS:
            all_sources, moneyflow = build_sources(scn, code)
            tmp = tempfile.mkdtemp()
            if moneyflow is not None:
                with open(os.path.join(tmp, "moneyflow.json"), "w", encoding="utf-8") as fh:
                    json.dump(moneyflow, fh)
            old = E5._data_dir
            E5._data_dir = lambda d=tmp: d
            try:
                source_info = None
                if all_sources is not None:
                    source_info = {"all_sources": all_sources, "industry": scn["industry"] or ""}
                res = E5.score_stock(close, high, low, vol, weights=weights,
                                     source_info=source_info, code_bare=bare(code))
            finally:
                E5._data_dir = old
            if res is None:
                continue
            ext = [v for v in res["votes"] if v["indicator"] in EXTERNAL_INDICATORS]
            cases.append({
                "id": "%s/%s" % (code, scn["id"]),
                "code": code,
                "scenario": scn["id"],
                "external": ext,
                "expect": {
                    "votes": [{"indicator": v["indicator"], "vote": v["vote"], "strength": v["strength"]}
                              for v in res["votes"]],
                    "score": res["score"],
                    "raw": res["raw"],
                    "resonance": res["resonance"],
                    "bull": res["bull"],
                    "bear": res["bear"],
                    "neutral": res["neutral"],
                },
            })
    return codes, cases


def build(v5_path):
    with open(v5_path, encoding="utf-8") as fh:
        v5 = json.load(fh)
    codes, cases = run_cases(v5["cases"], weights=load_weights())
    return {
        "spec_version": SPEC_VERSION,
        "engine": "v5-21votes",
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        ).isoformat(timespec="seconds"),
        "source": "stocks-ai-view/scripts/golden_21.py",
        "note": "外部票由云端 engine_v5.build_votes 本体产出（夹具化 all_sources/moneyflow），"
                "权重取 data/engine_weights.json（与端上 spec.weights 同源）；"
                "端上必须注入同一批外部票并复现 21 票聚合；票数不足 21 的用例（no_external）是刻意保留的无外部数据基线",
        "external_indicators": list(EXTERNAL_INDICATORS),
        "scenarios": [{k: v for k, v in s.items() if k != "moneyflow"}
                      | ({"moneyflow": s["moneyflow"]} if s["moneyflow"] else {}) for s in SCENARIOS],
        "codes": codes,
        "cases": cases,
    }


def main():
    ap = argparse.ArgumentParser(description="21 票全量黄金向量")
    ap.add_argument("--golden", help="输出路径")
    ap.add_argument("--v5", default=os.path.join(ROOT, "data", "parity", "golden_v5.json"))
    ap.add_argument("--verify", help="重算并比对已提交的 21 票黄金向量")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    g = build(args.v5)
    if args.verify:
        if not os.path.exists(args.verify):
            print("[golden_21] 缺少 %s" % args.verify)
            return 1
        with open(args.verify, encoding="utf-8") as fh:
            old = json.load(fh)
        for key in ("codes", "cases", "external_indicators"):
            d = deep_diff(old.get(key), g.get(key), key)
            if d:
                print("[golden_21] DRIFT %s —— %s（云端外部票或聚合改过，需重生成并同步端上夹具）"
                      % (key, d))
                return 1
        print("[golden_21] OK 21 票黄金向量与参考实现一致（%d 用例）" % len(g["cases"]))
        return 0
    if args.selftest:
        for c in g["cases"]:
            print(c["id"], len(c["expect"]["votes"]), "票", c["expect"]["score"],
                  c["expect"]["signal"] if "signal" in c["expect"] else "",
                  [(v["indicator"], v["vote"], v["strength"]) for v in c["external"]])
        return 0
    if args.golden:
        os.makedirs(os.path.dirname(os.path.abspath(args.golden)) or ".", exist_ok=True)
        with open(args.golden, "w", encoding="utf-8") as fh:
            json.dump(g, fh, ensure_ascii=False)
            fh.write("\n")
        print("golden 21 -> %s (%d 用例, %d 只票, %.1f KB)" % (
            args.golden, len(g["cases"]), len(g["codes"]), os.path.getsize(args.golden) / 1024.0))
        return 0
    print(json.dumps({"cases": len(g["cases"]), "codes": len(g["codes"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
