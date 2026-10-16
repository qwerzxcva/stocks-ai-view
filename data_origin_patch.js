// data_origin.js — Cloud-first data routing for the dashboard.
//
// Read order for every warehouse JSON:
//   1. 多仓取最新: gh_sources.js 配置的 A/B/C 三仓 raw (A 是唯一事实源,
//      B/C 是错峰采集镜像, 按 ts 取最新的那份 — 突破单仓 5 分钟 cron 下限)
//   2. 同源兜底: Pages 自己的 data/<file> (国内可直连 raw 被墙时)
//   3. GitCode 镜像: 仅 market/* 有
//
// 为什么多仓在 raw 而不在 Pages 同源: 镜像仓 (B/C) 是私有采集节点,
// 不建 Pages, 只能走 raw; 同源那份是 pages-data-sync.yml 从 A 仓 diff 过来
// 的, 有 10 分钟级延迟, 所以排在第 2 位。
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

  // GitHub raw/CDN 可能继续返回旧 JSON。数据请求统一带版本参数并禁用
  // HTTP 缓存; 这不是浏览器缓存策略, 而是线上数据版本一致性门禁。
  // force=true (点「更新」按钮) 时改用 reload, 连磁盘缓存一并绕过。
  function remote(url, timeoutMs, force) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs || 12000) : null;
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    var freshUrl = url + sep + 'v=' + Date.now();
    return fetch(freshUrl, { cache: force ? 'reload' : 'no-store', signal: ctrl ? ctrl.signal : undefined })
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

  // Resolve a legacy data/<file>.json path to every origin it may live at.
  // Returns { multi: [raw urls], same: same-origin url or null, mirror: [urls] }.
  function resolve(p) {
    var name = p.replace(/^data\//, '');
    var res = { multi: [], same: null, mirror: [] };

    if (MARKET_FILES.test(name)) {
      res.multi = [MARKET_GH + 'market/' + name];
      res.same = 'market/' + name;
      res.mirror = [MARKET_GC + 'market/' + name];
    } else if (AI_VIEW_ONLY.test(name)) {
      res.multi = multiUrls(p);
      res.same = p;
    } else if (name.indexOf('master/') === 0 || name.indexOf('market/') === 0 ||
      name.indexOf('sector/') === 0 || name.indexOf('overseas/') === 0) {
      res.multi = [MARKET_GH + name];
      res.same = name;
      res.mirror = [MARKET_GC + name];
    } else {
      var m = name.match(/^stocks\/(\d\d)\//);
      if (m) {
        var repo = SHARD_REPO[m[1]] || SHARD_DEFAULT;
        res.multi = [GH + repo + '/main/' + name.replace(/^stocks\//, '')];
        res.mirror = [GC + 'dufuwuxin/stocks-db-stocks/raw/main/' + name.replace(/^stocks\//, '')];
      } else {
        res.multi = multiUrls(p);
        res.same = p;
      }
    }
    return res;
  }

  // A/B/C 三仓的同一路径 raw URL。gh_sources.js 未加载时退回只有 A 仓。
  function multiUrls(p) {
    var repos = (globalThis.GHSources && globalThis.GHSources.REPOS) || [];
    if (!repos.length) return [AI_VIEW_GH + p];
    return repos.map(function (r) {
      return GH + r.owner + '/' + r.repo + '/' + r.branch + '/' + p;
    });
  }

  // 并发读多仓, 取 ts 最新的那份; 全部失败则抛错交给下一级兜底。
  // ts 缺失 (daily 档文件只有 date) 时按仓顺序取第一个成功的 —— A 仓在最前,
  // 它是唯一事实源, 镜像仓不能因为排序把旧版顶到前面。
  function fetchFreshest(urls, timeoutMs, force) {
    return Promise.all(urls.map(function (u) {
      return remote(u, timeoutMs, force).then(function (d) {
        return { data: d, ts: (d && d.ts) || '' };
      }).catch(function () { return null; });
    })).then(function (rs) {
      var ok = rs.filter(Boolean);
      if (!ok.length) throw new Error('all repos failed');
      ok.sort(function (a, b) {
        if (!a.ts && !b.ts) return 0;
        if (!a.ts) return 1;
        if (!b.ts) return -1;
        return b.ts.localeCompare(a.ts);
      });
      return ok[0].data;
    });
  }

  window.fetchJSON = function (p, opts) {
    var force = !!(opts && opts.force);
    var r = resolve(p);
    var chain = [];
    if (r.multi.length) chain.push(function () { return fetchFreshest(r.multi, 12000, force); });
    if (r.same) chain.push(function () { return remote(r.same, 6000, force); });
    r.mirror.forEach(function (u) {
      chain.push(function () { return remote(u, 12000, force); });
    });
    return chain.reduce(function (prev, next) { return prev.catch(next); })()
      .then(function (j) {
        if (j === null || j === undefined) throw new Error('all origins failed: ' + p);
        tag('CLOUD', 'Data served live from the GitHub warehouse');
        return j;
      })
      .catch(function () { return null; });
  };

  window.fetchJSONMinute = function (p, opts) {
    var force = !!(opts && opts.force);
    var r = resolve(p);
    var chain = [];
    if (r.multi.length) chain.push(function () { return fetchFreshest(r.multi, 8000, force); });
    if (r.same) chain.push(function () { return remote(r.same, 5000, force); });
    r.mirror.forEach(function (u) {
      chain.push(function () { return remote(u, 8000, force); });
    });
    return chain.reduce(function (prev, next) { return prev.catch(next); })();
  };

  console.log('[data_origin] cloud-first routing active (multi-repo freshest → same-origin → gitcode)');
})();
