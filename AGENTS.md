# AGENTS.md — stocks-ai-view project rules

## 1. Subagent domain lease (mandatory)

Prevents duplicate crawling and wasted API quota. Claim before dispatching,
release when done.

```bash
python3 scripts/domain_lease.py --available
python3 scripts/domain_lease.py --claim <agent> <domain>
python3 scripts/domain_lease.py --release <agent> <domain> --status done \
  --output <file> --note "<conclusion>"
```

`done` locks the domain permanently. Claims auto-expire after 24h.

## 2. Cloud-only deployment (2026-10-14)

The local database and the local website are both retired. The site is
deployed from the cloud and reads data straight from the cloud warehouse.

- No local `data/` directory; no local DuckDB.
- Every read goes to GitHub raw first, GitCode raw as fallback
  (`data_origin_patch.js`).
- Algorithms and engines run on GitCode Spaces, then push their output back
  to overwrite the GitHub data the site reads.
- Warehouse layout: see `~/workspace/stocks-db/README.md`.
- Shard routing lives in two places that must stay in sync:
  `stocks-db/scripts/sync_repos.py` and `stocks-ai-view/db-client.js`.

## 3. Accuracy discipline

1. Every page-consumed JSON carries a `stale` marker (`age_hours` + threshold).
2. Every field carries `provenance` (`source` + `confidence` + `snapshot_ts`).
3. After changing data, run `python3 scripts/data_audit.py --check`.
4. Limit-up/down counts use the eastmoney `tc` authoritative counter; the wide
   caliber value is stored in `*_wide`.

## 4. Crawling and reverse-engineering boundary

- Allowed: correct UA / Referer / Cookie (including anonymous sessions) and
  gentle rate limiting (1-2 QPS).
- Not allowed: CAPTCHA solving, browser fingerprint spoofing, proxy rotation.
- WAF-blocked sources: record the mechanism and provide an alternative. Do not
  try to break through.
- Sources blocked locally are handed to the GitHub Actions egress
  (`.github/workflows/v2-extras-collect.yml`).

## 5. Credentials

Tokens embedded in git remotes and site cookies are never restated and never
written into output files. Reference them masked.

## 6. Git

- Conventional Commits.
- `git pull --rebase` before push.
- Main branch is `main`.
- Data conflicts: take the remote Actions output as the base, then overlay the
  local audit fields (`stale` / `provenance`).
- Never commit someone else's uncommitted work; never `git commit -a` when the
  workspace holds foreign changes.

## 7. Key paths

| Purpose | Path |
|---|---|
| Source registry (single source of truth) | `scripts/sources_registry.py` |
| Unified query interface | `scripts/query_api.py` |
| DB layer (DuckDB, cloud only) | `scripts/db_layer.py` |
| Cycle pool v2 | `scripts/cycle_pool_v2.py` -> `data/cycle_pool_v2.json` |
| Skill algorithms | `scripts/skill_algos.py` |
| Data validation | `scripts/data_audit.py` |
| v2 collection pipeline | `scripts/collect_v2_extras.py` |
| Jisilu collection | `scripts/collect_jisilu.py` |
| Cloud warehouse builder | `~/workspace/stocks-db/scripts/` |
| Research output | `~/workspace/stocks-data-live/data/research/` |
