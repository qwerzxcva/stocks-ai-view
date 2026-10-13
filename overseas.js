// overseas.js — Overseas realtime panel + A-share transmission view.
//
// Reads the cloud warehouse (db-market/overseas/*) and renders:
//   1. realtime indices (US / HK / VIX) with the FTSE A50 future
//   2. overnight gap + 5d/20d momentum
//   3. global market news with keyword sentiment
//   4. the derived A-share bias, early-session guidance and sector transmission
//
// All numbers come from the warehouse; nothing is computed in the browser.
(function () {
  'use strict';

  var UP = 'var(--up)', DN = 'var(--dn)', FLAT = 'var(--tx3)';

  function col(v) { return v == null ? FLAT : (v >= 0 ? UP : DN); }
  function sgn(v) { return v == null ? '-' : (v >= 0 ? '+' : '') + Number(v).toFixed(2); }
  function pct(v) { return v == null ? '-' : sgn(v) + '%'; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function renderIndices(live) {
    var el = document.getElementById('ovIndices');
    if (!el) return;
    var items = (live && live.items) || {};
    var keys = Object.keys(items);
    if (!keys.length) { el.innerHTML = '<span class="fl">Overseas snapshot unavailable</span>'; return; }

    var order = { us_index: 0, us_sector: 1, us_vol: 2, hk_index: 3 };
    keys.sort(function (a, b) {
      return (order[items[a].group] || 9) - (order[items[b].group] || 9);
    });

    var h = '<table class="pool-table"><thead><tr><th>Index</th><th>Last</th>' +
      '<th>Chg%</th><th>Mom5</th><th>Amt</th><th>Time</th></tr></thead><tbody>';
    keys.forEach(function (k) {
      var v = items[k];
      var amt = v.amount ? (v.amount / 1e8).toFixed(0) + 'Yi' : '-';
      h += '<tr><td><b>' + esc(v.name) + '</b> <span style="color:var(--tx3);font-size:9px">' +
        esc(v.group) + '</span></td>' +
        '<td>' + Number(v.price).toFixed(2) + '</td>' +
        '<td class="' + col(v.chg_pct) + '">' + pct(v.chg_pct) + '</td>' +
        '<td id="ovMom_' + k + '" class="fl">-</td>' +
        '<td>' + amt + '</td>' +
        '<td style="color:var(--tx3);font-size:9px">' + esc(String(v.time || '').slice(5)) + '</td></tr>';
    });
    h += '</tbody></table>';

    var a50 = live.a50;
    if (a50) {
      h += '<div style="margin-top:6px;font-size:11px">' +
        '<b style="color:var(--amber)">FTSE China A50</b> ' +
        '<span style="font-weight:700;color:' + col(a50.chg_pct) + '">' +
        Number(a50.price).toFixed(0) + '</span> ' +
        '<span style="color:' + col(a50.chg_pct) + '">' + pct(a50.chg_pct) + '</span>' +
        ' <span class="fl" style="font-size:9px">offshore future, direct A-share open proxy</span></div>';
    }
    el.innerHTML = h;
  }

  function renderOvernight(ov) {
    var rows = (ov && ov.indices) || [];
    rows.forEach(function (r) {
      var cell = document.getElementById('ovMom_' + r.code);
      if (cell) {
        cell.textContent = r.mom5_pct == null ? '-' : sgn(r.mom5_pct) + '%';
        cell.className = col(r.mom5_pct);
      }
    });
  }

  function renderImpact(imp) {
    var el = document.getElementById('ovImpact');
    if (!el) return;
    if (!imp) { el.innerHTML = '<span class="fl">Impact model unavailable</span>'; return; }

    var c = imp.components || {};
    var biasCol = imp.bias === 'bullish' ? UP : (imp.bias === 'bearish' ? DN : FLAT);
    var h = '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">' +
      '<span class="fl" style="font-size:10px">A-share bias</span>' +
      '<b style="font-size:16px;color:' + biasCol + '">' + esc(imp.bias) + '</b>' +
      '<span class="fl" style="font-size:10px">composite ' + sgn(imp.composite) + '</span>' +
      '<span class="fl" style="font-size:10px">VIX ' + esc(imp.vix_state) + '</span></div>';

    h += '<div style="display:flex;flex-wrap:wrap;gap:8px;font-size:10px;margin-bottom:6px">' +
      '<span>overnight <b class="' + col(c.overnight) + '">' + sgn(c.overnight) + '</b></span>' +
      '<span>news <b class="' + col(c.news) + '">' + sgn(c.news) + '</b></span>' +
      '<span>A50 <b class="' + col(c.a50) + '">' + sgn(c.a50) + '</b></span>' +
      '<span>VIX adj <b class="' + col(c.vix_adj) + '">' + sgn(c.vix_adj) + '</b></span></div>';

    h += '<div style="padding:6px 8px;background:var(--panel2);border:1px solid var(--border2);' +
      'border-radius:6px;font-size:10px;margin-bottom:6px">' +
      '<b style="color:var(--amber)">Early session</b> ' + esc(imp.early_session_guidance) + '</div>';

    var sec = imp.sector_transmission || [];
    if (sec.length) {
      h += '<div class="fl" style="font-size:9px;margin-bottom:2px">Sector transmission</div>';
      h += '<table class="pool-table"><thead><tr><th>Overseas</th><th>A-share</th>' +
        '<th>Move</th><th>Expected</th></tr></thead><tbody>';
      sec.slice(0, 8).forEach(function (s) {
        h += '<tr><td>' + esc(s.overseas) + '</td><td>' + esc(s.a_share_sector) + '</td>' +
          '<td class="' + col(s.move_pct) + '">' + pct(s.move_pct) + '</td>' +
          '<td class="' + col(s.expected_pct) + '">' + pct(s.expected_pct) + '</td></tr>';
      });
      h += '</tbody></table>';
    }
    h += '<div class="fl" style="font-size:9px;margin-top:4px">' + esc(imp.disclaimer) + '</div>';
    el.innerHTML = h;
  }

  function renderSentiment(sen) {
    var el = document.getElementById('ovSentiment');
    if (!el) return;
    if (!sen || !sen.n_scored) { el.innerHTML = '<span class="fl">News sentiment unavailable</span>'; return; }
    var c = sen.avg_sentiment >= 0.15 ? UP : (sen.avg_sentiment <= -0.15 ? DN : FLAT);
    var h = '<div style="display:flex;gap:10px;font-size:10px;margin-bottom:5px">' +
      '<span>score <b style="color:' + c + '">' + sgn(sen.avg_sentiment) + '</b></span>' +
      '<span>label <b>' + esc(sen.label) + '</b></span>' +
      '<span class="up">' + sen.positive + ' pos</span>' +
      '<span class="dn">' + sen.negative + ' neg</span>' +
      '<span class="fl">' + sen.neutral + ' flat</span>' +
      '<span class="fl">' + sen.n_scored + '/' + sen.n_total + ' scored</span></div>';
    var tags = sen.tag_sentiment || {};
    var tk = Object.keys(tags);
    if (tk.length) {
      h += '<div style="margin-bottom:5px">' + tk.slice(0, 6).map(function (t) {
        return '<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;' +
          'border:1px solid var(--bd);border-radius:8px;color:' + col(tags[t]) + ';font-size:9px">' +
          esc(t) + ' ' + sgn(tags[t]) + '</span>';
      }).join('') + '</div>';
    }
    el.innerHTML = h;
  }

  function renderNews(sen, news) {
    var el = document.getElementById('ovNews');
    if (!el) return;
    var items = (sen && sen.items) || [];
    if (!items.length) items = (news && news.items) || [];
    if (!items.length) { el.innerHTML = '<span class="fl">No global headlines</span>'; return; }
    var h = '';
    items.slice(0, 14).forEach(function (it) {
      var sc = it.sentiment;
      h += '<div style="padding:3px 0;border-bottom:1px solid var(--bd);font-size:10px">' +
        (sc == null ? '' : '<b class="' + col(sc) + '" style="display:inline-block;min-width:30px">' +
          sgn(sc) + '</b> ') +
        '<a href="' + esc(it.url) + '" target="_blank" rel="noopener" ' +
        'style="color:var(--tx);text-decoration:none">' + esc(it.title) + '</a>' +
        ' <span class="fl" style="font-size:9px">' + esc(it.media || '') + '</span></div>';
    });
    el.innerHTML = h;
  }

  async function render() {
    if (!window.DB) return;
    var res = await Promise.allSettled([
      window.DB.overseas(), window.DB.overseasOvernight(),
      window.DB.overseasImpact(), window.DB.overseasSentiment(),
      window.DB.overseasNews()
    ]);
    var live = res[0].status === 'fulfilled' ? res[0].value : null;
    var ov = res[1].status === 'fulfilled' ? res[1].value : null;
    var imp = res[2].status === 'fulfilled' ? res[2].value : null;
    var sen = res[3].status === 'fulfilled' ? res[3].value : null;
    var news = res[4].status === 'fulfilled' ? res[4].value : null;

    renderIndices(live);
    renderOvernight(ov);
    renderImpact(imp);
    renderSentiment(sen);
    renderNews(sen, news);

    var ts = document.getElementById('ovTs');
    if (ts && imp) ts.textContent = (imp.generated_at || '').replace('T', ' ').slice(5, 16);
  }

  window.Overseas = { render: render };
  if (document.readyState !== 'loading') render();
  else document.addEventListener('DOMContentLoaded', render);
})();
