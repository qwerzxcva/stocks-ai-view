// data_origin.js — Cloud-first data routing for the dashboard.
//
// The local data/ directory was removed (2026-10-14): the site is deployed
// from the cloud only. Every fetch goes to the cloud warehouse first and
// falls back to the GitCode mirror when GitHub raw is unreachable.
//
// Realtime quote providers (qt.gtimg.cn / push2his.eastmoney.com) are called
// directly by stock-detail.js; this module only handles warehouse JSON.
(function () {
  'use strict';

  if (typeof window.fetchJSON !== 'function') {
    console.warn('[data_origin] fetchJSON undefined, patch inactive');
    return;
  }

  var GH = 'https://raw.githubusercontent.com/';
  var GC = 'https://gitcode.com/';

  // Cloud warehouse repos (see db-client.js for the shard routing).
  var MARKET_GH = GH + 'jiegengxiangjianxue1/db-market/main/';
  var MARKET_GC = GC + 'dufuwuxin/stocks-db-market/raw/main/';
  var AI_VIEW_GH = GH + 'qwerzxcva/stocks-ai-view/main/';

  // Legacy engine outputs that still live in the ai-view repo.
  var AI_VIEW_ONLY = /^(latest|market_now|hot_impact|enrich|ai_picks|ai_pick_v4|daily_brief|engine_v5|cycle_deep|ban_prob|ban_pick|flow_divergence|valuation|multidim|decision|self_reflect|global_fx|news_digest|cycle|sector_pool|lowpool|index_kline|search_index|kline_sim|realtime_minute|tunnel)\.json$/;

  function remote(url, timeoutMs) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs || 12000) : null;
    return fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        if (timer) clearTimeout(timer);
        if (!r.ok) throw new Error('remote ' + r.status);
        return r.json();
      })
      .catch(function (e) {
        if (timer) clearTimeout(timer);
        throw e;
      });
  }

  function tag(text, title) {
    if (window.dataOrigin === 'github') return;
    window.dataOrigin = 'github';
    var el = document.getElementById('dataSourceTag');
    if (el) {
      el.textContent = text;
      el.title = title;
      el.style.display = 'inline-block';
    }
  }

  // Resolve a legacy data/<file>.json path to its cloud origin.
  function resolve(p) {
    if (AI_VIEW_ONLY.test(p.replace(/^data\//, ''))) {
      return [AI_VIEW_GH + p, null];
    }
    if (p.indexOf('data/master/') === 0 || p.indexOf('data/market/') === 0 ||
      p.indexOf('data/sector/') === 0 || p.indexOf('data/overseas/') === 0) {
      return [MARKET_GH + p.replace(/^data\//, ''), MARKET_GC + p.replace(/^data\//, '')];
    }
    // per-stock paths: data/stocks/<shard>/<code>/...
    // 分片仓 (db-stocks-a..e) 未推送成功, 相关键已从 db-client 移除;
    // 这里相应去掉, 否则 key 为 undefined 会拼出 "undefined..." 的 URL。
    return [AI_VIEW_GH + p, null];
  }

  window.fetchJSON = function (p) {
    var r = resolve(p);
    return remote(r[0])
      .then(function (j) {
        tag('CLOUD', 'Data served live from the GitHub warehouse');
        return j;
      })
      .catch(function () {
        if (r[1]) return remote(r[1]).catch(function () { return null; });
        return null;
      });
  };

  window.fetchJSONMinute = function (p) {
    var r = resolve(p);
    return remote(r[0], 8000).catch(function () {
      return r[1] ? remote(r[1], 8000) : null;
    });
  };

  console.log('[data_origin] cloud-first routing active');
})();
