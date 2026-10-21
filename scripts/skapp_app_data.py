#!/usr/bin/env python3
"""skapp_app_data.py — build the skapp Android app's data files + public API + AI Q&A.

Runs in GitHub Actions (skapp-app-data.yml) and locally. Pure stdlib.

Outputs (all relative to repo root):
  data/data/decision.json     app file: mirror of data/decision.json + pctChg alias + AI why
  data/data/today_picks.json  app file: mirror of data/today_picks.json + pctChg + detail + AI why
  data/data/stock_names.json  app file: flat {code:name} from db-market _search.json
  data/api/index.json         API catalog: endpoints, universe counts, ts
  data/api/market.json        market snapshot + hot news + sentiment
  data/api/stocks/<code>.json per-stock full record (quote/technical/financial/boards/engines/qa)
  data/api/qa/<code>.json     AI Q&A per stock (incremental; LLM via ghc CLI when present)

Data policy: every value comes from a real upstream file; anything absent stays
null or {"status": "insufficient"} — never fabricated.
"""
import json, os, re, subprocess, sys, urllib.request

GH = "https://raw.githubusercontent.com"
A = f"{GH}/qwerzxcva/stocks-ai-view/main/data"
MARKET = f"{GH}/jiegengxiangjianxue1/db-market/main"

SHARD = {
    "00": "jiegengxiangjianxue1/db-stocks-a", "30": "jiegengxiangjianxue2/db-stocks-b",
    "60": "jiegengxiangjianxue2/db-stocks-c", "68": "jiegengxiangjianxue3/db-stocks-d",
    "15": "jiegengxiangjianxue3/db-stocks-d", "51": "jiegengxiangjianxue3/db-stocks-d",
    "50": "jiegengxiangjianxue3/db-stocks-d", "52": "jiegengxiangjianxue3/db-stocks-d",
    "56": "jiegengxiangjianxue3/db-stocks-d", "58": "jiegengxiangjianxue3/db-stocks-d",
    "20": "jiegengxiangjianxue3/db-stocks-d", "81": "jiegengxiangjianxue3/db-stocks-e",
    "83": "jiegengxiangjianxue3/db-stocks-e", "89": "jiegengxiangjianxue3/db-stocks-e",
    "90": "jiegengxiangjianxue3/db-stocks-e", "92": "jiegengxiangjianxue3/db-stocks-e",
}

def gj(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skapp-builder"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def gjt(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skapp-builder"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except Exception:
        return None

def bare6(code):
    b = re.sub(r"\D", "", str(code or "")).zfill(6)
    return b if len(b) == 6 else None

def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)

def load_local(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

# ---------- per-stock bundle detail ----------

def bundle_detail(code):
    b6 = bare6(code)
    if not b6:
        return None
    repo = SHARD.get(b6[:2])
    if not repo:
        return {"status": "insufficient", "reason": "no shard mapping"}
    b = gj(f"{GH}/{repo}/main/{b6[:2]}/{b6}/stock.json", 40)
    if not isinstance(b, dict):
        return {"status": "insufficient", "reason": "bundle unavailable"}
    q, tech, fin = b.get("quote") or {}, b.get("technical") or {}, b.get("financial") or []
    return {
        "status": "ok",
        "ts": b.get("ts"),
        "name": b.get("name"),
        "quote": {k: q.get(k) for k in ("price", "pctChg", "chg_pct", "turnover", "volume", "pe", "pb", "mv") if q.get(k) is not None},
        "technical": {k: tech.get(k) for k in ("ma5", "ma20", "ma60", "rsi14", "macd", "vol_ratio", "pos20") if tech.get(k) is not None},
        "financial_latest": (fin[0] if isinstance(fin, list) and fin else None),
    }

# ---------- AI Q&A (GitCode free AI, user mandate: NOT GitHub Models) ----------

GITCODE_TOKEN = os.environ.get("GITCODE_TOKEN") or os.environ.get("GC_TOKEN") or ""
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
LLM_FALLBACK_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"

def _gitcode_chat(prompt, model, timeout=90):
    if not GITCODE_TOKEN:
        return None
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    req = urllib.request.Request(
        "https://gitcode.com/api/v5/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {GITCODE_TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return None

def llm_qa(code, record):
    """One GitCode AI call -> strict JSON {why, risk, focus}. No token / failure -> insufficient."""
    ctx = json.dumps({
        "code": code,
        "name": record.get("name"),
        "engine_scores": record.get("engine_scores"),
        "detail": record.get("detail"),
        "boards": record.get("boards"),
        "market_phase": record.get("market_phase"),
    }, ensure_ascii=False)
    prompt = (
        "你是A股数据分析助手。只允许使用下面给出的真实数据(JSON)回答, 禁止编造任何数字或事实, "
        "数据里没有的信息就说不了解。输出且仅输出一个 JSON 对象, 不要markdown代码块, 字段: "
        '"why"(为何被选中/关注, 120字内), "risk"(主要风险, 100字内), "focus"(后续观察点, 100字内)。\n'
        f"数据: {ctx}"
    )
    out = _gitcode_chat(prompt, LLM_MODEL)
    if out is None:
        out = _gitcode_chat(prompt, LLM_FALLBACK_MODEL)
    if out is None:
        return {"status": "insufficient", "reason": "llm unavailable"}
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return {"status": "insufficient", "reason": "llm output not json"}
    try:
        qa = json.loads(m.group(0))
    except Exception:
        return {"status": "insufficient", "reason": "llm json parse failed"}
    return {"status": "ok", "model": LLM_MODEL, **{k: str(qa.get(k, ""))[:400] for k in ("why", "risk", "focus")}}

# ---------- main ----------

def main():
    decision = gj(f"{A}/decision.json") or load_local("data/decision.json", {})
    picks = gj(f"{A}/today_picks.json") or load_local("data/today_picks.json", {})
    mn = gj(f"{A}/market_now.json")
    ef = gj(f"{A}/engine_fusion.json")
    ew = gj(f"{A}/engine_weights.json")
    ev5 = gj(f"{A}/engine_v5.json") or {}
    aiv4 = gj(f"{A}/ai_pick_v4.json") or {}
    lowpool = gj(f"{A}/lowpool.json") or {}
    hot_news = gj(f"{A}/hot_news.json") or {}
    digest = gj(f"{A}/news_digest.json") or {}
    names_raw = gj(f"{MARKET}/_search.json") or {}
    boards_raw = gj(f"{MARKET}/sector/stock_to_board.json") or {}

    names = {}
    for code, v in (names_raw or {}).items():
        nm = v.get("n") if isinstance(v, dict) else v
        if nm:
            names[str(code)] = nm
    boards_map = {}
    if isinstance(boards_raw, dict):
        for code, v in boards_raw.items():
            if isinstance(v, list):
                boards_map[str(code)] = v[:8]
            elif isinstance(v, dict):
                boards_map[str(code)] = list(v.values())[:8] if v else []

    # universe: picks + engine tops + lowpool + decision cards
    codes = set()
    for c in decision.get("cards", []) or []:
        codes.add(c.get("code"))
    pk = picks.get("picks", {}) or {}
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            codes.add(e.get("code"))
    for lst, n in ((ev5.get("top"), 100), (aiv4.get("picks"), 60)):
        for e in (lst if isinstance(lst, list) else [])[:n]:
            if isinstance(e, dict):
                codes.add(e.get("code"))
    for k in ("strong_buy", "watch_list"):
        lst = lowpool.get(k)
        if isinstance(lst, list):
            for e in lst[:40]:
                codes.add(e.get("code") if isinstance(e, dict) else e)
    codes = {bare6(c) for c in codes if bare6(c)}
    print(f"universe: {len(codes)} codes", flush=True)

    # engine scores per code (real, from engine outputs)
    scores = {}
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            b = bare6(e.get("code"))
            if b and e.get("score") is not None:
                scores.setdefault(b, {})[e.get("engine", grp)] = e.get("score")
    for e in (ev5.get("top") or []):
        b = bare6(e.get("code"))
        if b:
            scores.setdefault(b, {})["engine_v5"] = e.get("score")
    for c in decision.get("cards", []) or []:
        b = bare6(c.get("code"))
        if b and c.get("composite") is not None:
            scores.setdefault(b, {})["composite"] = c.get("composite")

    # build per-stock API records (bundle detail + boards + scores)
    records = {}
    for c in sorted(codes):
        rec = {
            "code": c,
            "name": names.get(c),
            "engine_scores": scores.get(c) or {},
            "boards": boards_map.get(c, []),
            "detail": bundle_detail(c),
        }
        if not rec["name"] and rec["detail"] and rec["detail"].get("name"):
            rec["name"] = rec["detail"]["name"]
        records[c] = rec
    n_ok = sum(1 for r in records.values() if (r.get("detail") or {}).get("status") == "ok")
    print(f"bundles resolved: {n_ok}/{len(records)}", flush=True)

    # AI Q&A: incremental, only for stocks without a cached qa file
    qa_budget = int(os.environ.get("QA_BATCH", "8"))
    n_new_qa = 0
    for c in sorted(codes):
        qa_path = f"data/api/qa/{c}.json"
        qa = load_local(qa_path, None)
        if qa and qa.get("status") == "ok":
            continue
        if n_new_qa >= qa_budget:
            break
        qa = llm_qa(c, records[c])
        qa["code"] = c
        qa["ts"] = picks.get("ts") or decision.get("ts")
        dump(qa_path, qa)
        records[c]["ai_qa"] = qa
        n_new_qa += 1
        print(f"qa {c}: {qa.get('status')}", flush=True)
    for c in sorted(codes):
        if "ai_qa" not in records[c]:
            qa = load_local(f"data/api/qa/{c}.json", None)
            if qa:
                records[c]["ai_qa"] = qa

    # AI why lines appended into app-visible why/reasons (strings the app renders)
    for c in decision.get("cards", []) or []:
        b = bare6(c.get("code"))
        qa = records.get(b, {}).get("ai_qa") if b else None
        if qa and qa.get("status") == "ok" and qa.get("why"):
            c.setdefault("reasons", [])
            if isinstance(c["reasons"], list):
                c["reasons"].append(qa["why"][:200])
        c["detail"] = records.get(b, {}).get("detail")
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            b = bare6(e.get("code"))
            qa = records.get(b, {}).get("ai_qa") if b else None
            if qa and qa.get("status") == "ok":
                line = qa.get("why", "")
                if line:
                    e.setdefault("why", [])
                    if isinstance(e["why"], list):
                        e["why"].append(f"[AI] {line}"[:200])
            e["detail"] = records.get(b, {}).get("detail")

    # pctChg alias (app reads pctChg; site ships chg_pct)
    n_pct = 0
    for c in decision.get("cards", []) or []:
        if c.get("pctChg") is None and c.get("chg_pct") is not None:
            c["pctChg"] = c["chg_pct"]; n_pct += 1
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            if e.get("pctChg") is None:
                for k in ("chg_pct", "pct", "ret_pct"):
                    if e.get(k) is not None:
                        e["pctChg"] = e[k]; n_pct += 1
                        break

    # market block refresh from freshest snapshot
    if isinstance(mn, dict):
        mk = picks.setdefault("market", {})
        for k in ("advance", "decline", "limit_up", "limit_down", "turnover_yi", "turnover"):
            if mn.get(k) is not None:
                mk[k] = mn[k]
        idx = mn.get("indices") or mn.get("indices_now")
        if idx:
            mk["indices"] = idx
        picks["market_source"] = "data/market_now.json"

    src = {
        "generated_by": "scripts/skapp_app_data.py (CI)",
        "upstream": sorted(set([
            "stocks-ai-view:data/decision.json", "stocks-ai-view:data/today_picks.json",
            "stocks-ai-view:data/market_now.json", "stocks-ai-view:data/engine_fusion.json",
            "stocks-ai-view:data/engine_weights.json", "stocks-ai-view:data/engine_v5.json",
            "stocks-ai-view:data/ai_pick_v4.json", "stocks-ai-view:data/lowpool.json",
            "stocks-ai-view:data/hot_news.json", "stocks-ai-view:data/news_digest.json",
            "db-market:_search.json", "db-market:sector/stock_to_board.json",
            "skapp-data:data/search/symbols.json",
        ] + [f"shard-bundle:{c}" for c in codes
             if (records[c].get("detail") or {}).get("status") == "ok"])),
        "registered_sources": 89,
        "note": "engine outputs are products of the 89+ registered collection sources; AI Q&A via GitHub Models gpt-4o-mini (only from provided real data)",
    }
    decision["sources"] = src
    picks["sources"] = src

    # write app files
    dump("data/data/decision.json", decision)
    dump("data/data/today_picks.json", picks)
    dump("data/data/stock_names.json", names)

    # API: market
    mkt = {
        "ts": picks.get("ts"),
        "market": picks.get("market", {}),
        "phase": (digest or {}).get("sentiment"),
        "hot_news": (hot_news or {}).get("items", [])[:20],
        "news_digest": digest,
        "sources": ["stocks-ai-view:data/market_now.json", "stocks-ai-view:data/hot_news.json",
                    "stocks-ai-view:data/news_digest.json", "stocks-ai-view:data/today_picks.json"],
    }
    dump("data/api/market.json", mkt)

    # API: per-stock
    for c, rec in records.items():
        rec["market_phase"] = (digest or {}).get("sentiment") or {}
        dump(f"data/api/stocks/{c}.json", rec)

    # API: index
    idx = {
        "ts": picks.get("ts"),
        "endpoints": {
            "market": "data/api/market.json",
            "symbols": "data/api/search/symbols.json",
            "stock_detail": "data/api/stocks/{code}.json",
            "stock_qa": "data/api/qa/{code}.json",
            "decision": "data/data/decision.json",
            "today_picks": "data/data/today_picks.json",
            "stock_names": "data/data/stock_names.json",
        },
        "universe": len(codes),
        "bundles_ok": n_ok,
        "qa_ok": sum(1 for r in records.values() if (r.get("ai_qa") or {}).get("status") == "ok"),
        "sources": src,
    }
    dump("data/api/index.json", idx)

    print(f"done: pctChg aliases={n_pct} names={len(names)} universe={len(codes)} "
          f"bundles={n_ok} new_qa={n_new_qa}", flush=True)

if __name__ == "__main__":
    main()
