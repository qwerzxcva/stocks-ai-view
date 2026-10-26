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
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
from concurrent.futures import ThreadPoolExecutor, as_completed

GH = "https://raw.githubusercontent.com"
A = f"{GH}/qwerzxcva/stocks-ai-view/main/data"
MARKET = os.environ.get("MARKET_BASE") or f"{GH}/jiegengxiangjianxue1/db-market/main"

SHARD = {
    "00": "jiegengxiangjianxue1/db-stocks-a", "30": "jiegengxiangjianxue2/db-stocks-b",
    "60": "jiegengxiangjianxue2/db-stocks-c", "68": "jiegengxiangjianxue3/db-stocks-d",
    "15": "jiegengxiangjianxue3/db-stocks-d", "51": "jiegengxiangjianxue3/db-stocks-d",
    "50": "jiegengxiangjianxue3/db-stocks-d", "52": "jiegengxiangjianxue3/db-stocks-d",
    "56": "jiegengxiangjianxue3/db-stocks-d", "58": "jiegengxiangjianxue3/db-stocks-d",
    "20": "jiegengxiangjianxue3/db-stocks-d",
    # shards 81/83/89/90/92 无 bundle 仓 (db-stocks-e 已于 2026-10-21 删除,
    # 该仓 0 个 stock.json); 这些代码走实时源, 不查分片仓。
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

def norm_code(code):
    """A 股: 6 位(000001); 港股: hk+5 位(hk00700) — app 自选里两种都有。"""
    s = str(code or "").strip().lower()
    if s.startswith("hk"):
        d = re.sub(r"\D", "", s).zfill(5)
        return f"hk{d}" if d != "00000" else None
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
    b6 = norm_code(code)
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
    # 消息面: 情绪/影响/传导/主线(站点已有的四个引擎产出)
    news_extra = gj(f"{A}/news_extra.json") or {}
    news_impact = gj(f"{A}/news_impact.json") or {}
    news_trans = gj(f"{A}/news_transmission.json") or {}
    hot_themes = gj(f"{A}/hot_themes.json") or {}
    hot_impact = gj(f"{A}/hot_impact.json") or {}
    # 板块: db-market 的行业/概念/TradingView 分组 + 站点板块引擎
    industries = gj(f"{MARKET}/sector/industry.json") or {}
    concepts = gj(f"{MARKET}/sector/concept.json") or {}
    tv_groups = gj(f"{MARKET}/sector/tv_groups.json") or {}
    sector_pool = gj(f"{A}/sector_pool.json") or {}
    industry_map = gj(f"{A}/industry_map_em.json") or {}
    industry_stocks = gj(f"{A}/industry_stocks.json") or {}
    names_raw = gj(f"{MARKET}/_search.json") or {}
    boards_raw = gj(f"{MARKET}/sector/stock_to_board.json") or {}

    names = {}
    for code, v in (names_raw or {}).items():
        nm = v.get("n") if isinstance(v, dict) else v
        if nm:
            names[str(code)] = nm
    # skapp-data symbols.json 也带 code->name(含港股 hk00700), 合并进来
    for e in (gj(f"{GH}/jiegengxiangjianxue/skapp-data/main/data/search/symbols.json") or []):
        if isinstance(e, dict) and (e.get("s") or e.get("symbol")) and (e.get("n") or e.get("name")):
            names.setdefault(str(e.get("s") or e.get("symbol")), e.get("n") or e.get("name"))
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
    # 用户常问的自选股/权重股种子(与 app 默认自选一致): 保证这些一定有详情和 AI 问答
    SEED_CODES = os.environ.get("SEED_CODES", "").split(",") + [
        "600519", "300750", "002594", "600941", "hk00700", "000001", "601318",
        "600036", "000858", "601888", "600900", "000651", "002415", "300059",
        "601012", "600276", "000333", "002304", "600030", "601166", "600887", "601398",
    ]
    for c in SEED_CODES:
        codes.add(norm_code(c))
    codes.discard(None)

    # 名称兜底: db-market/symbols 不覆盖港股, 用腾讯行情接口(与 app 同源)补真实名称
    missing = [c for c in sorted(codes) if not names.get(c)]
    for i in range(0, len(missing), 20):
        batch = missing[i:i + 20]
        q = ",".join(c if c.startswith("hk") else ("sh" + c if c[0] == "6" else "sz" + c) for c in batch)
        try:
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}", headers={"User-Agent": "skapp-builder"})
            txt = urllib.request.urlopen(req, timeout=20).read().decode("gbk", errors="replace")
            for c in batch:
                key = c if c.startswith("hk") else ("sh" + c if c[0] == "6" else "sz" + c)
                m = re.search(rf'v_{key}="[^~"]*~([^~]+)~', txt)
                if m and m.group(1).strip():
                    names[c] = m.group(1).strip()
        except Exception as e:
            print(f"tencent name fallback failed: {e}", flush=True)
            break

    print(f"universe: {len(codes)} codes (incl. {len(SEED_CODES)} seeds)", flush=True)

    # engine scores per code (real, from engine outputs)
    scores = {}
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            b = norm_code(e.get("code"))
            if b and e.get("score") is not None:
                scores.setdefault(b, {})[e.get("engine", grp)] = e.get("score")
    for e in (ev5.get("top") or []):
        b = norm_code(e.get("code"))
        if b:
            scores.setdefault(b, {})["engine_v5"] = e.get("score")
    for c in decision.get("cards", []) or []:
        b = norm_code(c.get("code"))
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

    # AI Q&A: incremental; 顺序=推股/决策/自选种子优先, 其余按代码序, 保证热点股先有问答
    qa_budget = int(os.environ.get("QA_BATCH", "8"))
    seed_set = {norm_code(c) for c in SEED_CODES}
    scored = [c for c in sorted(codes) if records[c].get("engine_scores")]
    prio = [c for c in sorted(codes) if c in seed_set] + [c for c in scored if c not in seed_set]
    prio_set = set(prio)
    rest = [c for c in sorted(codes) if c not in prio_set]
    n_new_qa = 0
    for c in prio + rest:
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
        b = norm_code(c.get("code"))
        qa = records.get(b, {}).get("ai_qa") if b else None
        if qa and qa.get("status") == "ok" and qa.get("why"):
            c.setdefault("reasons", [])
            if isinstance(c["reasons"], list):
                c["reasons"].append(qa["why"][:200])
        c["detail"] = records.get(b, {}).get("detail")
    for grp in ("signals", "values"):
        for e in pk.get(grp, []) or []:
            b = norm_code(e.get("code"))
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

    # ── 分层新鲜度: 分别记录每类上游的实际 ts 与年龄, 让 app 能区分
    #    「实时行情还在更新」与「引擎已收盘停跑」, 而不是只显示一个总 ts。
    now = __import__('datetime').datetime.now(
        __import__('datetime').timezone(__import__('datetime').timedelta(hours=8)))
    def _age(ts):
        if not ts:
            return None
        try:
            from datetime import datetime as _dt
            t = _dt.fromisoformat(str(ts).replace('Z', '+00:00'))
            if t.tzinfo is None:
                t = t.replace(tzinfo=now.tzinfo)
            return max(0, int((now - t).total_seconds() // 60))
        except Exception:
            return None
    layers = {
        "realtime": (mn or {}).get("ts"),
        "engine": decision.get("ts"),
        "picks": picks.get("ts"),
        "news": (hot_news or {}).get("ts"),
    }
    freshness = {k: {"ts": v, "age_min": _age(v)} for k, v in layers.items()}
    freshness["generated_at"] = now.isoformat()
    freshness["note"] = (
        "引擎类数据(engine/picks)在 A 股收盘后不再刷新属正常; "
        "realtime 为盘中实时行情, 收盘后转为收盘快照。"
    )
    decision["data_freshness"] = freshness
    picks["data_freshness"] = freshness
    print("freshness:", {k: v.get("age_min") for k, v in freshness.items()
                          if isinstance(v, dict)}, flush=True)

    src = {
        "generated_by": "scripts/skapp_app_data.py (CI)",
        "upstream": sorted(set([
            "stocks-ai-view:data/decision.json", "stocks-ai-view:data/today_picks.json",
            "stocks-ai-view:data/market_now.json", "stocks-ai-view:data/engine_fusion.json",
            "stocks-ai-view:data/engine_weights.json", "stocks-ai-view:data/engine_v5.json",
            "stocks-ai-view:data/ai_pick_v4.json", "stocks-ai-view:data/lowpool.json",
            "stocks-ai-view:data/hot_news.json", "stocks-ai-view:data/news_digest.json",
            "stocks-ai-view:data/news_extra.json", "stocks-ai-view:data/news_impact.json",
            "stocks-ai-view:data/news_transmission.json", "stocks-ai-view:data/hot_themes.json",
            "stocks-ai-view:data/sector_pool.json", "stocks-ai-view:data/industry_map_em.json",
            "db-market:sector/industry.json", "db-market:sector/concept.json",
            "db-market:sector/tv_groups.json",
            "db-market:_search.json", "db-market:sector/stock_to_board.json",
            "skapp-data:data/search/symbols.json",
        ] + [f"shard-bundle:{c}" for c in codes
             if (records[c].get("detail") or {}).get("status") == "ok"])),
        "registered_sources": 190,
        "news_sources_registered": 13,
        "note": "engine outputs are products of the 190 registered collection sources; news/sectors mirror the site's own engine outputs; AI Q&A via GitCode free AI (only from provided real data)",
    }
    decision["sources"] = src
    picks["sources"] = src

    # write app files
    dump("data/data/decision.json", decision)
    dump("data/data/today_picks.json", picks)
    dump("data/data/stock_names.json", names)

    # ── API: 消息面(移植站点 news_* 四个引擎产出) ──
    news_items = (hot_news.get("items") or []) if isinstance(hot_news.get("items"), list) else []
    impact_items = news_impact.get("items") if isinstance(news_impact.get("items"), list) else []
    n_input = news_impact.get("n_input", news_impact.get("n"))
    n_mapped = news_impact.get("n_mapped")
    unmapped_count = news_impact.get("n_unmapped")
    if unmapped_count is None and isinstance(n_input, int) and isinstance(n_mapped, int):
        unmapped_count = max(0, n_input - n_mapped)
    news_source_ts = (hot_news.get("ts") or digest.get("ts") or
                      news_impact.get("input_ts") or news_impact.get("ts"))
    news_status = ("ok" if news_items and news_source_ts and unmapped_count is not None
                   else "insufficient_data")
    news_api = {
        "ts": news_source_ts,
        "source_ts": news_source_ts,
        "status": news_status,
        "unmapped_count": unmapped_count if unmapped_count is not None else "insufficient_data",
        "items": news_items[:100],
        "impacts": (news_impact.get("impacts") or impact_items)[:50]
                    if isinstance(news_impact.get("impacts") or impact_items, list) else [],
        "digest": {
            "ts": digest.get("ts"),
            "date": digest.get("date"),
            "stale": digest.get("stale"),
            "total": digest.get("total"),
            "sentiment": digest.get("sentiment"),
            "global_score": digest.get("global_score"),
            "global_mood": digest.get("global_mood"),
            "cat_counts": digest.get("cat_counts"),
            # categories 实际是 dict: 分类名 -> 该分类新闻列表
            "categories": {
                k: (v[:10] if isinstance(v, list) else v)
                for k, v in (digest.get("categories") or {}).items()
            } if isinstance(digest.get("categories"), dict) else {},
            "themes": (hot_themes.get("themes") or [])[:20]
                      if isinstance(hot_themes.get("themes"), list) else [],
        },
        "hot_news": {
            "ts": hot_news.get("ts"),
            "n_items": hot_news.get("n_items"),
            "sources_ok": hot_news.get("sources_ok"),
            "items": (hot_news.get("items") or [])[:40],
            "groups": (hot_news.get("groups") or [])[:20] if isinstance(hot_news.get("groups"), list) else (hot_news.get("groups") or {}),
        },
        "impact": {
            "schema_version": news_impact.get("schema_version", "unknown"),
            "ts": news_impact.get("ts") or news_impact.get("generated_at"),
            "engine": news_impact.get("engine"),
            "source_ts": news_impact.get("input_ts") or news_impact.get("ts") or news_impact.get("generated_at"),
            "n_input": n_input,
            "n_mapped": n_mapped,
            "unmapped_count": unmapped_count if unmapped_count is not None else "insufficient_data",
            "status": news_impact.get("status") or news_impact.get("coverage_status") or "insufficient_data",
            "stale": news_impact.get("stale", news_impact.get("source_status") != "live"),
            "provenance": news_impact.get("provenance") or news_impact.get("meta") or {},
            "source_registry_count": (news_impact.get("meta") or {}).get("source_registry_count", 190),
            "news_source_registry_count": (news_impact.get("meta") or {}).get("news_source_registry_count", 13),
            "items": (news_impact.get("items") or [])[:50] if isinstance(news_impact.get("items"), list) else [],
            "impacts": (news_impact.get("impacts") or news_impact.get("items") or [])[:50] if isinstance(news_impact.get("impacts") or news_impact.get("items"), list) else [],
        },
        "transmission": {
            "ts": news_trans.get("ts"),
            "items": ((news_trans.get("items") or news_trans.get("transmissions") or [])[:30]
                      if isinstance(news_trans.get("items") or news_trans.get("transmissions"), list) else []),
        },
        "themes": {
            "ts": hot_themes.get("ts"),
            "engine": hot_themes.get("engine"),
            "n_themes": hot_themes.get("n_themes"),
            "themes": (hot_themes.get("themes") or [])[:20] if isinstance(hot_themes.get("themes"), list) else [],
            "hot_reports": (hot_themes.get("hot_reports") or [])[:10] if isinstance(hot_themes.get("hot_reports"), list) else [],
        },
        "extra": {
            "ts": news_extra.get("ts"),
            "items": (news_extra.get("items") or [])[:20] if isinstance(news_extra.get("items"), list) else [],
        },
        "sources": [
            "stocks-ai-view:data/hot_news.json", "stocks-ai-view:data/news_digest.json",
            "stocks-ai-view:data/news_impact.json", "stocks-ai-view:data/news_transmission.json",
            "stocks-ai-view:data/hot_themes.json", "stocks-ai-view:data/news_extra.json",
            "stocks-ai-view:data/hot_impact.json",
        ],
    }
    dump("data/api/news.json", news_api)

    def _board_list(raw):
        """db-market sector/*.json 形状: {generated_at, source, n, boards:[{bk,name,total_mktcap,n_stocks}]}"""
        if not isinstance(raw, dict):
            return []
        boards = raw.get("boards")
        if not isinstance(boards, list):
            return []
        return [b for b in boards if isinstance(b, dict)][:200]

    # Fetch constituent files for every published industry/concept board.
    board_members = {}
    _ind_boards = _board_list(industries)
    _con_boards = _board_list(concepts)
    board_jobs = {}
    for b, kind in ([(x, "industry") for x in _ind_boards] +
                    [(x, "concept") for x in _con_boards]):
        bk = b.get("bk")
        if bk:
            board_jobs[(bk, kind)] = b

    def _fetch_board(job):
        (bk, kind), board = job
        # 优先本仓当日重建的成分股（可用 SECTOR_LOCAL_DIR 覆盖目录，便于容错测试），
        # db-market 仅作兜底（上游停更时不得当当日用）
        local = os.path.join(os.environ.get("SECTOR_LOCAL_DIR", os.path.join("data", "sector")),
                             "%s_constituents" % kind, "%s.json" % bk)
        if os.path.exists(local):
            try:
                with open(local, encoding="utf-8") as f:
                    return bk, kind, board, local, json.load(f), None
            except Exception as exc:
                err = "local_parse:%s" % type(exc).__name__  # 降级到云端兜底，不直接放弃
        else:
            err = None
        path = f"{MARKET}/sector/{kind}_constituents/{bk}.json"
        try:
            data = gj(path)
        except Exception as exc:
            return bk, kind, board, path, None, (err or "remote:%s" % type(exc).__name__)
        if not isinstance(data, dict):
            return bk, kind, board, path, None, (err or "remote_empty")
        return bk, kind, board, path, data, err

    source_errors = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_board, job) for job in board_jobs.items()]
        for future in as_completed(futures):
            bk, kind, board, path, data, err = future.result()
            if err:
                source_errors.append({"board": bk, "kind": kind, "error": err})
            members = data.get("members") if isinstance(data, dict) else None
            valid_members = [
                {"code": str(m["code"]), "name": m.get("name") or str(m["code"])}
                for m in (members or [])
                if isinstance(m, dict) and m.get("code")
            ] if isinstance(members, list) else []
            source_ts = ((data or {}).get("generated_at") or (data or {}).get("ts") or
                         (industries if kind == "industry" else concepts).get("generated_at"))
            age_days = None
            if source_ts:
                try:
                    _t = datetime.fromisoformat(str(source_ts).replace("Z", "+00:00"))
                    if _t.tzinfo is None:
                        _t = _t.replace(tzinfo=CST)
                    age_days = round((datetime.now(CST) - _t).total_seconds() / 86400, 2)
                except Exception:
                    age_days = None
            if not valid_members and isinstance(data, dict) and data.get("empty_confirmed") \
                    and age_days is not None and age_days <= 2:
                member_status = "empty"
            elif not valid_members:
                member_status = "missing"
            elif age_days is not None and age_days > 2:
                member_status = "stale"
            else:
                member_status = "ok"
            board_members[bk] = {
                "name": board.get("name"),
                "kind": kind,
                "n": data.get("n") if isinstance(data, dict) else None,
                "members": valid_members[:200],
                "source_ts": source_ts,
                "age_days": age_days,
                "source": path,
                "status": member_status,
                "usable": member_status == "ok",
                "counted_fresh": member_status in ("ok", "empty"),
                "fetch_error": err,
            }
    print(f"board_members fetched: {len(board_members)} / {len(board_jobs)}", flush=True)

    # ── API: 板块(移植站点的行业/概念/板块池) ──
    def _industry_list(raw):
        """db-market sector/industry.json 或站点 industry_map_em.json → 统一列表"""
        out = []
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    out.append({"name": v.get("name") or k, "code": k,
                                "chg": v.get("chg_pct") or v.get("pct") or v.get("chg"),
                                "n": v.get("n") or v.get("count")})
                elif isinstance(v, list):
                    out.append({"name": k, "code": k, "members": v[:50]})
        elif isinstance(raw, list):
            out = [x for x in raw if isinstance(x, dict)]
        return out
    def _board_list(raw):
        """db-market sector/*.json 形状: {generated_at, source, n, boards:[{bk,name,total_mktcap,n_stocks}]}"""
        if not isinstance(raw, dict):
            return []
        boards = raw.get("boards")
        if not isinstance(boards, list):
            return []
        return [b for b in boards if isinstance(b, dict)][:200]
    industry_source_ts = industries.get("generated_at") or industries.get("ts")
    concept_source_ts = concepts.get("generated_at") or concepts.get("ts")
    board_by_name = {}
    for board in _ind_boards + _con_boards:
        if board.get("name"):
            board_by_name.setdefault(board["name"], board)
    for tier in ("tier1", "tier2", "tier3", "tier_down"):
        rows = sector_pool.get(tier)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            board = board_by_name.get(row.get("sector"))
            member_data = board_members.get(board.get("bk")) if board else None
            row["members"] = member_data.get("members", []) if member_data else []
            row["source_ts"] = member_data.get("source_ts") if member_data else None
            row["source"] = member_data.get("source") if member_data else None
            row["status"] = member_data.get("status", "insufficient_data") if member_data else "insufficient_data"
    sector_pool["source_ts"] = (sector_pool.get("ts") or
                                 (sector_pool.get("meta") or {}).get("ts"))
    sector_pool["source"] = (sector_pool.get("meta") or {}).get("source")
    for board in _ind_boards:
        board.update({"source_ts": industry_source_ts, "source": "db-market:sector/industry.json"})
        detail = board_members.get(board.get("bk"))
        board["status"] = detail.get("status", "insufficient_data") if detail else "insufficient_data"
        board["usable"] = bool(detail.get("usable")) if detail else False
        board["age_days"] = detail.get("age_days") if detail else None
        board["members"] = detail.get("members", []) if detail else []
    for board in _con_boards:
        board.update({"source_ts": concept_source_ts, "source": "db-market:sector/concept.json"})
        detail = board_members.get(board.get("bk"))
        board["status"] = detail.get("status", "insufficient_data") if detail else "insufficient_data"
        board["usable"] = bool(detail.get("usable")) if detail else False
        board["age_days"] = detail.get("age_days") if detail else None
        board["members"] = detail.get("members", []) if detail else []
    # ── 新鲜度门禁：不让旧板块冒充当日 ──
    _fresh_ok = [bk for bk, v in board_members.items() if v.get("status") == "ok"]
    _empty = [bk for bk, v in board_members.items() if v.get("status") == "empty"]
    _fresh = _fresh_ok + _empty  # 空板块（接口确认当日无成分）也算当日数据
    _stale = [bk for bk, v in board_members.items() if v.get("status") == "stale"]
    _missing = [bk for bk, v in board_members.items() if v.get("status") == "missing"]
    _total_boards = len(board_members) or 1
    _sp_date = sector_pool.get("date")
    _today = datetime.now(CST).strftime("%Y-%m-%d")
    _age_days = [v["age_days"] for v in board_members.values() if v.get("age_days") is not None]
    _reasons = []
    if not board_members:
        _status = "insufficient_data"
        _reasons.append("板块成员全部获取失败")
    elif len(_fresh) == 0:
        _status = "stale"
        _reasons.append("全部板块成员超过 2 天未更新")
    elif len(_stale) + len(_missing) > _total_boards * 0.2:
        _status = "degraded"
        _reasons.append("过时 %d / 缺失 %d 超过总量 20%%" % (len(_stale), len(_missing)))
    else:
        _status = "ok"
    if _sp_date != _today:
        _status = "degraded" if _status == "ok" else _status
        _reasons.append("板块池日期 %s != 今日 %s" % (_sp_date, _today))
    sectors_api = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source_ts": {"industry": industry_source_ts, "concept": concept_source_ts},
        "status": _status,
        "degraded_reason": "；".join(_reasons) or None,
        "freshness": {
            "asof": _today,
            "max_age_days": 2,
            "fresh_boards": len(_fresh),
            "fresh_with_members": len(_fresh_ok),
            "empty_boards": len(_empty),
            "stale_boards": len(_stale),
            "missing_boards": len(_missing),
            "total_boards": len(board_members),
            "oldest_age_days": max(_age_days) if _age_days else None,
            "sector_pool_date": _sp_date,
            "requirement_met": len(_fresh) == len(board_members) and len(board_members) > 0,
            "fetch_error_count": len(source_errors),
            "stale_board_ids": _stale[:20],
            "missing_board_ids": _missing[:20],
            "degraded_board_ids": (_stale + _missing)[:20],
        },
        "source_errors": source_errors[:20],
        "industries": _ind_boards,
        "concepts": _con_boards,
        "industry_map_em": _industry_list(industry_map)[:80],
        "sector_pool": sector_pool,
        "industry_stocks": industry_stocks if isinstance(industry_stocks, dict) else {},
        "sector_pool_source_ts": sector_pool.get("ts") or sector_pool.get("generated_at"),
        # 预生成 Top 板块的成分股(全量 200+ 板块逐一拉太大, 按市值/数量取前 30)
        "board_members": board_members,
        "sources": [
            "db-market:sector/industry.json", "db-market:sector/concept.json",
            "db-market:sector/tv_groups.json", "db-market:sector/stock_to_board.json",
            "stocks-ai-view:data/sector_pool.json", "stocks-ai-view:data/industry_map_em.json",
            "stocks-ai-view:data/industry_stocks.json",
        ],
    }
    dump("data/api/sectors.json", sectors_api)
    print(f"[sectors] status={_status} fresh={len(_fresh)} stale={len(_stale)} "
          f"missing={len(_missing)} pool_date={_sp_date}", flush=True)
    if "--only-sectors" in sys.argv:
        return 0

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
            "news": "data/api/news.json",
            "sectors": "data/api/sectors.json",
        },
        "universe": len(codes),
        "bundles_ok": n_ok,
        "qa_ok": sum(1 for r in records.values() if (r.get("ai_qa") or {}).get("status") == "ok"),
        "sources": src,
    }
    dump("data/api/index.json", idx)
    # 数据源台账：登记/可用/已接执行器/缺口/不可用，供 App 状态页读取
    try:
        import build_sources_status
        dump("data/api/sources.json", build_sources_status.build())
        import build_source_disposition
        build_source_disposition.main()
        import build_sectors_health
        build_sectors_health.main()
    except Exception as exc:
        print(f"[sources] status build skipped: {type(exc).__name__} {exc}", file=sys.stderr)

    print(f"done: pctChg aliases={n_pct} names={len(names)} universe={len(codes)} "
          f"bundles={n_ok} new_qa={n_new_qa}", flush=True)

if __name__ == "__main__":
    main()
