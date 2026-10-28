"""hive_ml.py — E5 机器学习域 (三头独立 + 分歧门控)
策略 nextday_head / strongup_head / p4hit_head: 三个已验证模型独立打分
策略 disagree_gate: 三头分歧大 → 置信降档
只读 factors_wide 最新截面, 与其他引擎零耦合。
"""
import duckdb
import pickle

FM = "/home/agentuser/stockdb/factor_matrix.duckdb"
MODELS = {
    "nextday": "/home/agentuser/stockdb/t1/data/lgb_nextday.pkl",
    "strongup": "/home/agentuser/stockdb/t1/data/lgb_strongup.pkl",
    "p4hit": "/home/agentuser/stockdb/t1/data/lgb_p4hit.pkl",
}


def run(ctx):
    con = duckdb.connect(FM, read_only=True)
    con.execute("SET memory_limit='3GB'; SET temp_directory='/tmp/ducktmp'")
    heads = {}
    feats_ref = None
    for k, path in MODELS.items():
        try:
            d = pickle.load(open(path, "rb"))
            heads[k] = d
            feats_ref = feats_ref or d["features"]
        except Exception:
            pass
    if not heads:
        return {"engine": "E5", "domain": "ml_ensemble", "signals": [], "meta": {"err": "no models"}}
    F = ", ".join(f'"{f}"' for f in feats_ref)
    df = con.execute(f"""
      SELECT symbol AS sym, {F} FROM factors_wide
      WHERE date = (SELECT max(date) FROM factors_wide)
        AND (symbol LIKE 'sh60%' OR symbol LIKE 'sz00%')
    """).fetchdf()
    con.close()

    prob = {}
    for k, d in heads.items():
        X = df[d["features"]].fillna(0).values.astype("float32")
        prob[k] = d["model"].predict_proba(X)[:, 1]

    import numpy as np
    P = np.vstack(list(prob.values()))
    agree = 1 - P.std(axis=0) / (P.mean(axis=0) + 1e-9)   # 三头一致性
    fused = P.mean(axis=0) * (0.7 + 0.3 * agree)

    df["s"] = fused
    df = df.sort_values("s", ascending=False)
    sig = []
    for _, r in df.head(150).iterrows():
        i = df.index.get_loc(r.name) if r.name in df.index else None
        sig.append({"sym": r.sym, "score": round(float(r.s), 3), "conf": round(float(agree[i]) if i is not None else 0.5, 3),
                    "strategy": "ml3_fused",
                    "why": " ".join(f"{k}={prob[k][i]:.2f}" for k in prob if i is not None)})
    return {"engine": "E5", "domain": "ml_ensemble", "signals": sig,
            "meta": {"n": len(sig), "models": list(heads)}}
