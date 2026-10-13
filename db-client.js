// db-client.js — Cloud data client for the market dashboard.
//
// All data is read from the cloud warehouse (GitHub raw + GitCode raw), never
// from a local copy. Realtime quotes still go direct to the quote providers
// because they are the only source that is actually realtime.
//
// Warehouse layout (see _index.json in the market repo):
//   jiegengxiangjianxue1/db-market  -> master/, market/, sector/, overseas/
//   jiegengxiangjianxue2/db-stocks-a -> shards 00/30/60
//   jiegengxiangjianxue2/db-stocks-b -> shards 15/50/51/52/56/58/68
//   jiegengxiangjianxue3/db-stocks-c -> remaining shards
(function () {
  'use strict';

  var GH = 'https://raw.githubusercontent.com/';
  var GC = 'https://gitcode.com/';

  var REPOS = {
    market: GH + 'jiegengxiangjianxue1/db-market/main/',
    marketGC: GC + 'dufuwuxin/stocks-db-market/raw/main/',
    stocksA: GH + 'jiegengxiangjianxue2/db-stocks-a/main/',
    stocksB: GH + 'jiegengxiangjianxue2/db-stocks-b/main/',
    stocksC: GH + 'jiegengxiangjianxue3/db-stocks-c/main/',
    stocksGC: GC + 'dufuwuxin/stocks-db-stocks/raw/main/'
  };

  var SHARDS_A = ['00', '30', '60'];
  var SHARDS_B = ['15', '50', '51', '52', '56', '58', '68'];

  var cache = {};
  var origin = 'github';

  function pickStockRepo(shard) {
    if (SHARDS_A.indexOf(shard) >= 0) return REPOS.stocksA;
    if (SHARDS_B.indexOf(shard) >= 0) return REPOS.stocksB;
    return REPOS.stocksC;
  }

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
          var alt = url.replace(GH + 'jiegengxiangjianxue1/db-market/main/', REPOS.marketGC)
                       .replace(GH + 'jiegengxiangjianxue2/db-stocks-a/main/', REPOS.stocksGC)
                       .replace(GH + 'jiegengxiangjianxue2/db-stocks-b/main/', REPOS.stocksGC)
                       .replace(GH + 'jiegengxiangjianxue3/db-stocks-c/main/', REPOS.stocksGC);
          if (alt !== url) return get(alt, ttl);
        }
        return null;
      });
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

    // ── overseas ──
    overseas: function () { return get(REPOS.market + 'overseas/indices.json', 60000); },
    usStocks: function () { return get(REPOS.market + 'overseas/us_stocks.json', 300000); },
    hkStocks: function () { return get(REPOS.market + 'overseas/hk_stocks.json', 300000); },

    // ── per stock ──
    stockManifest: function (code) {
      var shard = String(code).slice(0, 2);
      return get(pickStockRepo(shard) + shard + '/' + code + '/manifest.json', 300000);
    },
    stockQuote: function (code) {
      var shard = String(code).slice(0, 2);
      return get(pickStockRepo(shard) + shard + '/' + code + '/money/quote.json', 30000);
    },
    stockFlow: function (code) {
      var shard = String(code).slice(0, 2);
      return get(pickStockRepo(shard) + shard + '/' + code + '/money/fflow.json', 300000);
    },
    stockKline: function (code, freq) {
      var shard = String(code).slice(0, 2);
      var f = freq || 'day_qfq';
      return get(pickStockRepo(shard) + shard + '/' + code + '/kline/' + f + '.csv', 300000)
        .then(function (txt) { return api.parseCsv(txt); });
    },
    stockTechnical: function (code) {
      var shard = String(code).slice(0, 2);
      return get(pickStockRepo(shard) + shard + '/' + code + '/technical/latest.json', 300000);
    },

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
