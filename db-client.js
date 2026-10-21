// db-client.js — Cloud data client for the market dashboard.
//
// All data is read from the cloud warehouse (GitHub raw + GitCode raw), never
// from a local copy. Realtime quotes still go direct to the quote providers
// because they are the only source that is actually realtime.
//
// Warehouse layout (verified against what actually exists upstream):
//   jiegengxiangjianxue1/db-market -> market/ indices+pools+margin
//                                 -> sector/ industry/concept + constituents
//                                 -> overseas/ live+impact+us/hk
//                                 -> _search.json (6027 code->name index)
//                                 -> master/ universe
//
// Per-stock stock.json bundles live in the shard repos:
//   jiegengxiangjianxue1/db-stocks-a -> shard 00
//   jiegengxiangjianxue2/db-stocks-b -> shard 30
//   jiegengxiangjianxue2/db-stocks-c -> shard 60
//   jiegengxiangjianxue3/db-stocks-d -> shards 68 / 15 / 51 / 50 / 52 / 56 / 58 / 20
// Shards 81/83/89/90/92 have no bundle warehouse (db-stocks-e was removed
// 2026-10-21 after carrying zero stock.json); those codes resolve null and
// callers fall back to live quote sources.
// Note: jiegengxiangjianxue2/db-stocks-a is a deprecated older layout with no
// stock.json and is deliberately NOT routed here.
(function () {
  'use strict';

  var GH = 'https://raw.githubusercontent.com/';
  var GC = 'https://gitcode.com/';

  var REPOS = {
    market: GH + 'jiegengxiangjianxue1/db-market/main/',
    marketGC: GC + 'dufuwuxin/stocks-db-market/raw/main/',
  };

  // Per-stock bundle repos: first two digits -> repo.
  // Shards absent from this map have NO built bundle warehouse; stockBundle()
  // resolves null for them and callers must fall back to live quote sources.
  var SHARD_REPOS = {
    '00': 'jiegengxiangjianxue1/db-stocks-a/',
    '30': 'jiegengxiangjianxue2/db-stocks-b/',
    '60': 'jiegengxiangjianxue2/db-stocks-c/',
    '68': 'jiegengxiangjianxue3/db-stocks-d/',
    '15': 'jiegengxiangjianxue3/db-stocks-d/',
    '51': 'jiegengxiangjianxue3/db-stocks-d/',
    '50': 'jiegengxiangjianxue3/db-stocks-d/',
    '52': 'jiegengxiangjianxue3/db-stocks-d/',
    '56': 'jiegengxiangjianxue3/db-stocks-d/',
    '58': 'jiegengxiangjianxue3/db-stocks-d/',
    '20': 'jiegengxiangjianxue3/db-stocks-d/'
  };

  // Shards that used to route to a warehouse with zero stock.json bundles.
  // Kept as an explicit list so the UI can say "not built" instead of
  // silently retrying a path that can never resolve.
  var SHARDS_WITHOUT_BUNDLES = { '81': 1, '83': 1, '89': 1, '90': 1, '92': 1 };

  var cache = {};
  var origin = 'github';

  // Fetch with per-session cache; tries GitHub first, falls back to GitCode.
  function get(url, ttl) {
    var now = Date.now();
    var hit = cache[url];
    if (hit && now - hit.t < (ttl || 60000)) return Promise.resolve(hit.v);
    return fetch(url, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { cache[url] = { t: now, v: j }; return j; })
      .catch(function () {
        if (url.indexOf(GH) === 0) {
          var alt = url.replace(GH + 'jiegengxiangjianxue1/db-market/main/',
                                REPOS.marketGC);
          if (alt !== url) return get(alt, ttl);
        }
        return null;
      });
  }

  // Like get(), but for the per-stock shard repos: no GitCode mirror exists,
  // so on failure retry once on branch master, else resolve null.
  // Shares the same per-session cache.
  function getShard(url, ttl) {
    var now = Date.now();
    var hit = cache[url];
    if (hit && now - hit.t < (ttl || 60000)) return Promise.resolve(hit.v);
    return fetch(url, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { cache[url] = { t: now, v: j }; return j; })
      .catch(function () {
        if (url.indexOf('/main/') !== -1) {
          return getShard(url.replace('/main/', '/master/'), ttl);
        }
        return null;
      });
  }

  // stockBundle: read the merged per-stock object (stock.json) once and
  // share it across kline/quote/technical/financial, so one fetch feeds every
  // dataset instead of four separate requests against the same file.
  var _bundleCache = {};
  function stockBundle(code) {
    var bare = String(code == null ? '' : code).replace(/\D/g, '').padStart(6, '0');
    if (!/^\d{6}$/.test(bare)) return Promise.resolve(null);
    if (_bundleCache[bare]) return _bundleCache[bare];
    var shard = bare.slice(0, 2);
    if (SHARDS_WITHOUT_BUNDLES[shard]) {
      _bundleCache[bare] = Promise.resolve(null);
      return _bundleCache[bare];
    }
    var repo = SHARD_REPOS[shard];
    if (!repo) return Promise.resolve(null);
    var url = GH + repo + 'main/' + shard + '/' + bare + '/stock.json';
    _bundleCache[bare] = getShard(url, 600000).then(function (j) {
      if (!j || typeof j !== 'object') return null;
      return j;
    });
    return _bundleCache[bare];
  }
  var api = {
    repos: REPOS,
    origin: function () { return origin; },

    // ── warehouse catalog ──
    index: function () { return get(REPOS.market + '_index.json', 300000); },
    search: function () { return get(REPOS.market + '_search.json', 300000); },
    shard: function (s) { return get(REPOS.market + '_by_shard/' + s + '.json', 300000); },

    // ── market-wide ──
    master: function () { return get(REPOS.market + 'master/universe.json', 600000); },
    indices: function () { return get(REPOS.market + 'market/indices.json', 60000); },
    pools: function () { return get(REPOS.market + 'market/pools.json', 60000); },
    margin: function () { return get(REPOS.market + 'market/margin.json', 300000); },

    // ── sectors ──
    industries: function () { return get(REPOS.market + 'sector/industry.json', 300000); },
    concepts: function () { return get(REPOS.market + 'sector/concept.json', 300000); },
    boardMembers: function (kind, bk) {
      return get(REPOS.market + 'sector/' + kind + '_constituents/' + bk + '.json', 600000);
    },
    boardKline: function (kind, bk) {
      return get(REPOS.market + 'sector/' + kind + '_kline/' + bk + '.csv', 600000)
        .then(function (txt) { return api.parseCsv(txt); });
    },
    // TradingView grouping: the usable membership source when the eastmoney
    // board endpoint is unreachable.
    tvGroups: function () { return get(REPOS.market + 'sector/tv_groups.json', 600000); },
    stockToBoard: function () { return get(REPOS.market + 'sector/stock_to_board.json', 600000); },

    // ── overseas ──
    overseas: function () { return get(REPOS.market + 'overseas/live.json', 60000); },
    overseasImpact: function () { return get(REPOS.market + 'overseas/impact.json', 120000); },
    overseasNews: function () { return get(REPOS.market + 'overseas/news.json', 300000); },
    overseasSentiment: function () { return get(REPOS.market + 'overseas/sentiment.json', 300000); },
    overseasOvernight: function () { return get(REPOS.market + 'overseas/overnight.json', 300000); },

    // ── picks, per session ──
    todayPicks: function () { return get(REPOS.market + 'market/today_picks.json', 120000); },
    morningReview: function () { return get(REPOS.market + 'market/morning_review.json', 120000); },
    afternoonPicks: function () { return get(REPOS.market + 'market/afternoon_picks.json', 120000); },
    entryPlans: function () { return get(REPOS.market + 'market/entry_plans.json', 120000); },
    yesterdayReview: function () { return get(REPOS.market + 'market/review_today.json', 120000); },
    closeReview: function () { return get(REPOS.market + 'market/close_review.json', 120000); },
    stopAnalysis: function () { return get(REPOS.market + 'market/stop_analysis.json', 120000); },
    engineWeights: function () { return get(REPOS.market + 'market/engine_weights.json', 600000); },
    // JSONL: one row per closed day
    reviewHistory: function () {
      return fetch(REPOS.market + 'market/review_history.jsonl', { cache: 'no-store' })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (txt) {
          var out = [];
          txt.split('\n').forEach(function (line) {
            line = line.trim();
            if (!line) return;
            try { out.push(JSON.parse(line)); } catch (e) {}
          });
          return out;
        })
        .catch(function () { return []; });
    },
    usStocks: function () { return get(REPOS.market + 'overseas/us_stocks.json', 300000); },
    hkStocks: function () { return get(REPOS.market + 'overseas/hk_stocks.json', 300000); },

    // ── per stock ──
    // Stock bundle fields are optional in practice. Callers must handle missing
    // bundles/datasets and preserve live quote/provider fallbacks.
    searchIndex: function () { return get(REPOS.market + '_search.json', 600000); },

    stockManifest: function (code) {
      return stockBundle(code).then(function (j) {
        if (!j || !j.manifest) return null;
        var m = j.manifest;
        return { code: j.code, name: j.name, ts: j.ts, datasets: m.datasets || {},
          source: 'cloud stock.json' };
      });
    },
    stockQuote: function (code) {
      return stockBundle(code).then(function (j) { return j && j.quote || null; });
    },
    stockFlow: function (code) { return Promise.resolve(null); },
    stockFinancial: function (code) {
      return stockBundle(code).then(function (j) { return j && j.financial || []; });
    },
    // Returns [{date,open,close,high,low,vol}], same shape as parseCsv and
    // the direct-provider fetchers in stock-detail.js. Only day freq exists
    // in the warehouse; any failure resolves [] so the Tencent fallback in
    // stock-detail.js keeps working.
    stockKline: function (code, freq) {
      if (freq && freq !== 'day') return Promise.resolve([]);
      return stockBundle(code).then(function (j) {
        if (!j || !Array.isArray(j.kline)) return [];
        return j.kline.map(function (r) {
          return { date: r[0], open: +r[1], close: +r[2],
                   high: +r[3], low: +r[4], vol: +r[5] };
        });
      });
    },
    stockTechnical: function (code) {
      return stockBundle(code).then(function (j) { return j && j.technical || null; });
    },
    stockBundle: function (code) { return stockBundle(code); },

    // ── csv helper: "# comment\ndate,open,close,high,low,volume" ──
    parseCsv: function (txt) {
      if (typeof txt !== 'string') return [];
      var out = [];
      txt.split('\n').forEach(function (line) {
        if (!line || line.charAt(0) === '#' || line.indexOf('date,') === 0) return;
        var p = line.split(',');
        if (p.length < 6) return;
        out.push({
          date: p[0], open: +p[1], close: +p[2],
          high: +p[3], low: +p[4], vol: +p[5]
        });
      });
      return out;
    }
  };

  window.DB = api;
})();
