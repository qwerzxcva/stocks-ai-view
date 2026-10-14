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

  // Everything that lives under db-market/market/. Review and session
  // artefacts are produced by scripts/ in ai-view but published to the
  // market repo, so they must resolve there and not to ai-view/data.
  var MARKET_FILES = /^(today_picks|morning_review|afternoon_picks|entry_plans|review_today|close_review|stop_analysis|engine_weights|review_history|universe|overseas_live|overseas_impact)\.(json|jsonl)$/;

  // Legacy engine outputs that still live in the ai-view repo.
  var AI_VIEW_ONLY = /^(latest|market_now|hot_impact|enrich|ai_picks|ai_pick_v4|daily_brief|engine_v5|cycle_deep|ban_prob|ban_pick|flow_divergence|valuation|multidim|decision|self_reflect|global_fx|news_digest|cycle|sector_pool|lowpool|index_kline|search_index|kline_sim|realtime_minute|tunnel|scenario|edge_report|anomaly_report|signal_audit|validation_report|risk_plan|portfolio_plan|weights_history|winrate_matrix|walkforward|sample_balance|loss_attribution|rps_rotation|patterns_smoke|factor_smoke|audit_data_report|numeric_validator|cloud_analysis|ai_review|theme_to_stocks|hotspot_infer|social|fund|moneyflow|global|global_kline)\.(json|txt)$/;

  // db-stocks-a..e shard repos. Verified reachable (2026-10-14).
  var SHARD_REPO = {
    '00': 'jiegengxiangjianxue1/db-stocks-a',
    '30': 'jiegengxiangjianxue2/db-stocks-b',
    '60': 'jiegengxiangjianxue2/db-stocks-c'
  };
  var SHARD_DEFAULT = 'jiegengxiangjianxue3/db-stocks-d';

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
    var name = p.replace(/^data\//, '');
    if (MARKET_FILES.test(name)) {
      return [MARKET_GH + 'market/' + name, MARKET_GC + 'market/' + name];
    }
    if (AI_VIEW_ONLY.test(name)) {
      return [AI_VIEW_GH + p, null];
    }
    if (name.indexOf('master/') === 0 || name.indexOf('market/') === 0 ||
      name.indexOf('sector/') === 0 || name.indexOf('overseas/') === 0) {
      return [MARKET_GH + name, MARKET_GC + name];
    }
    // per-stock: data/stocks/<shard>/<code>/... -> db-stocks-*
    var m = name.match(/^stocks\/(\d\d)\//);
    if (m) {
      var repo = SHARD_REPO[m[1]] || SHARD_DEFAULT;
      return [GH + repo + '/main/' + name.replace(/^stocks\//, ''),
              GC + 'dufuwuxin/stocks-db-stocks/raw/main/' + name.replace(/^stocks\//, '')];
    }
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
