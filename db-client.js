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
// Per-stock detail (kline / quote / flow / technical) is NOT in the warehouse:
// the shard repos db-stocks-a..e and db-market/data/stocks/ return 404 (the
// bulk push never completed). stock-detail.js fetches those directly from
// Tencent -> Eastmoney -> Sina instead.
(function () {
  'use strict';

  var GH = 'https://raw.githubusercontent.com/';
  var GC = 'https://gitcode.com/';

  var REPOS = {
    market: GH + 'jiegengxiangjianxue1/db-market/main/',
    marketGC: GC + 'dufuwuxin/stocks-db-market/raw/main/',
  };

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
    // 分片仓 (db-stocks-a/b/c/d/e) 与 db-market/data/stocks/ 均未推送成功
    // (实测全部 404, 只有 _search.json 的名称索引在)。个股明细因此不走仓库,
    // 由 stock-detail.js 的腾讯→东财→新浪三源降级直取; 这里只保留仓库里
    // 真实存在的数据: 名称索引 + 大盘/板块/海外。
    searchIndex: function () { return get(REPOS.market + '_search.json', 600000); },

    stockManifest: function (code) { return Promise.resolve(null); },
    stockQuote: function (code) { return Promise.resolve(null); },
    stockFlow: function (code) { return Promise.resolve(null); },
    stockKline: function (code, freq) { return Promise.resolve([]); },
    stockTechnical: function (code) { return Promise.resolve(null); },

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
