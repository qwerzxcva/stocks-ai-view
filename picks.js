// picks.js — session-aware picks panel.
//
// Three datasets, all from the cloud warehouse, shown side by side:
//   morning_review.json   how the morning picks actually traded
//   afternoon_picks.json  what is actionable now, with the reason
//   review_today.json     yesterday's picks vs today
//
// The morning snapshot is never overwritten, so the page can show both the
// morning call and the afternoon revision at once.
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
  function el(id) { return document.getElementById(id); }

  function groupLabel(g) {
    return { signals: 'Signal resonance', values: 'Low position',
             limit_up: 'Limit-up' }[g] || g;
  }

  // ── morning: what actually happened ──
  function renderMorning(m) {
    var box = el('pkMorning');
    if (!box) return;
    if (!m || !m.groups) { box.innerHTML = '<div class="fl">Morning review not generated yet</div>'; return; }
    var h = '<div class="fl" style="font-size:9px;margin-bottom:4px">' +
      esc((m.generated_at || '').replace('T', ' ').slice(5, 16)) + ' CST</div>';
    h += '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px">';
    for (var g in m.groups) {
      var s = m.groups[g];
      h += '<span style="font-size:10px">' + esc(groupLabel(g)) +
        ' <b class="' + col(s.avg_ret_pct) + '">' + pct(s.avg_ret_pct) + '</b>' +
        ' <span class="fl">hit ' + s.hit_rate_pct + '% (n=' + s.n + ')</span></span>';
    }
    h += '</div>';
    h += '<table class="pool-table"><thead><tr><th>Code</th><th>Name</th>' +
      '<th>Chg%</th><th>Now</th><th>Turn</th></tr></thead><tbody>';
    var rows = [];
    for (var g2 in m.groups) rows = rows.concat(m.groups[g2].rows || []);
    rows.sort(function (a, b) { return (b.chg_pct || 0) - (a.chg_pct || 0); });
    rows.slice(0, 12).forEach(function (r) {
      h += '<tr><td>' + esc(r.code) + '</td><td>' + esc(r.name) + '</td>' +
        '<td class="' + col(r.chg_pct) + '">' + pct(r.chg_pct) + '</td>' +
        '<td>' + (r.price != null ? r.price.toFixed(2) : '-') + '</td>' +
        '<td>' + (r.turnover != null ? r.turnover.toFixed(2) + '%' : '-') + '</td></tr>';
    });
    h += '</tbody></table>';
    box.innerHTML = h;
  }

  // ── afternoon: actionable now, with reasons ──
  function renderAfternoon(a) {
    var box = el('pkAfternoon');
    if (!box) return;
    if (!a || !a.picks) { box.innerHTML = '<div class="fl">Afternoon picks not generated yet</div>'; return; }
    var h = '<div class="fl" style="font-size:9px;margin-bottom:4px">' +
      esc((a.generated_at || '').replace('T', ' ').slice(5, 16)) +
      ' CST · ' + a.n_actionable + ' actionable / ' + a.n_candidates + ' candidates</div>';

    var mk = a.market || {}, ov = a.overseas || {};
    h += '<div style="display:flex;flex-wrap:wrap;gap:10px;font-size:10px;margin-bottom:6px">' +
      '<span>Phase <b>' + esc(mk.phase) + '</b> <span class="fl">' + esc(mk.phase_desc) + '</span></span>' +
      '<span>Overseas <b class="' + col(ov.composite) + '">' + esc(ov.bias) + '</b></span>' +
      '<span>Limit-up <b>' + esc(mk.limit_up) + '</b></span></div>';

    h += '<table class="pool-table"><thead><tr><th>Code</th><th>Name</th><th>Grp</th>' +
      '<th>Now</th><th>Today</th><th>AM</th><th>Buy zone</th><th>Stop</th>' +
      '<th>Risk/Reward</th><th>Turn</th></tr></thead><tbody>';
    (a.picks || []).slice(0, 15).forEach(function (p) {
      var z = p.entry_zone || [];
      h += '<tr><td>' + esc(p.code) + '</td><td>' + esc(p.name) + '</td>' +
        '<td class="fl">' + esc(groupLabel(p.group)) + '</td>' +
        '<td>' + (p.price != null ? p.price.toFixed(2) : '-') + '</td>' +
        '<td class="' + col(p.chg_pct) + '">' + pct(p.chg_pct) + '</td>' +
        '<td class="' + col(p.morning_chg_pct) + '">' + pct(p.morning_chg_pct) + '</td>' +
        '<td>' + (z.length === 2 ? z[0].toFixed(2) + '-' + z[1].toFixed(2) : '-') + '</td>' +
        '<td class="dn">' + (p.stop_loss != null ? p.stop_loss.toFixed(2) : '-') + '</td>' +
        '<td class="fl">' + (p.risk_pct != null ? p.risk_pct + '%/' + p.reward_pct + '%' : '-') + '</td>' +
        '<td>' + (p.turnover != null ? p.turnover.toFixed(2) + '%' : '-') + '</td></tr>';
    });
    h += '</tbody></table>';

    // reasons for the top few, so the reader sees why each one passed
    h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">Why these</div>';
    (a.picks || []).slice(0, 4).forEach(function (p) {
      h += '<div style="font-size:10px;padding:2px 0;border-bottom:1px solid var(--bd)">' +
        '<b>' + esc(p.name) + '</b> <span class="fl">score ' + p.afternoon_score + '</span><br>' +
        '<span class="fl">' + esc((p.afternoon_reasons || []).join(' · ')) + '</span></div>';
    });

    if (a.rejected && a.rejected.length) {
      h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">Rejected (' + a.rejected.length + ')</div>';
      h += '<div style="font-size:10px">' + a.rejected.slice(0, 8).map(function (r) {
        return '<span style="margin-right:8px"><b>' + esc(r.name) + '</b> ' +
          '<span class="fl">' + esc(r.reason) + '</span></span>';
      }).join('') + '</div>';
    }
    h += '<div class="fl" style="font-size:9px;margin-top:6px">' + esc(a.disclaimer) + '</div>';
    box.innerHTML = h;
  }

  // ── yesterday: closed fact ──
  function renderYesterday(r) {
    var box = el('pkYesterday');
    if (!box) return;
    if (!r || !r.n_picks) { box.innerHTML = '<div class="fl">No closed review yet</div>'; return; }
    var h = '<div style="display:flex;flex-wrap:wrap;gap:10px;font-size:11px">' +
      '<span>Picks <b>' + r.n_picks + '</b></span>' +
      '<span>Up <b class="up">' + r.n_up + '</b></span>' +
      '<span>Hit <b class="' + col(r.hit_rate_pct - 50) + '">' + r.hit_rate_pct + '%</b></span>' +
      '<span>Avg <b class="' + col(r.avg_ret_pct) + '">' + pct(r.avg_ret_pct) + '</b></span>' +
      '<span class="fl">vs ' + esc(r.prev_snapshot) + '</span></div>';
    var best = r.best || [], worst = r.worst || [];
    if (best.length) {
      h += '<div style="margin-top:5px;font-size:10px">Best ' +
        best.map(function (x) { return '<b class="up">' + esc(x.name) + ' ' + pct(x.chg_pct) + '</b>'; }).join(' · ') + '</div>';
    }
    if (worst.length) {
      h += '<div style="font-size:10px">Worst ' +
        worst.map(function (x) { return '<b class="dn">' + esc(x.name) + ' ' + pct(x.chg_pct) + '</b>'; }).join(' · ') + '</div>';
    }
    h += '<div class="fl" style="font-size:9px;margin-top:4px">' + esc(r.note) + '</div>';
    box.innerHTML = h;
  }

  // ── history: one row per closed day ──
  function renderHistory(rows) {
    var box = el('pkHistory');
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="fl">No archived review yet</div>';
      return;
    }
    rows = rows.slice().sort(function (a, b) {
      return String(b.date || '').localeCompare(String(a.date || ''));
    });
    var n = rows.length;
    var hits = rows.map(function (r) { return r.hit_rate_buyable_pct; })
      .filter(function (x) { return x != null; });
    var avgs = rows.map(function (r) { return r.avg_ret_buyable_pct; })
      .filter(function (x) { return x != null; });
    var mean = function (xs) { return xs.length ? xs.reduce(function (a, b) { return a + b; }, 0) / xs.length : null; };

    var h = '<div style="display:flex;flex-wrap:wrap;gap:12px;font-size:11px;margin-bottom:6px">' +
      '<span>Days <b>' + n + '</b></span>' +
      '<span>Mean hit <b class="' + col((mean(hits) || 0) - 50) + '">' +
      (mean(hits) != null ? mean(hits).toFixed(1) + '%' : '-') + '</b></span>' +
      '<span>Mean avg <b class="' + col(mean(avgs) || 0) + '">' +
      (mean(avgs) != null ? (mean(avgs) >= 0 ? '+' : '') + mean(avgs).toFixed(2) + '%' : '-') + '</b></span>' +
      '<span class="fl">buyable picks only (limit-up excluded)</span></div>';

    h += '<table class="pool-table"><thead><tr><th>Date</th><th>Phase</th>' +
      '<th>n</th><th>Hit (buyable)</th><th>Avg (buyable)</th>' +
      '<th>Hit (all)</th><th>Avg (all)</th>' +
      '<th>Score edge</th><th>Stop</th><th>Tgt</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td>' + esc(r.date) + '</td>' +
        '<td class="fl">' + esc(r.market_phase) + '</td>' +
        '<td>' + esc(r.n_buyable) + '</td>' +
        '<td class="' + col((r.hit_rate_buyable_pct || 0) - 50) + '">' +
        (r.hit_rate_buyable_pct != null ? r.hit_rate_buyable_pct + '%' : '-') + '</td>' +
        '<td class="' + col(r.avg_ret_buyable_pct) + '">' + pct(r.avg_ret_buyable_pct) + '</td>' +
        '<td class="fl">' + (r.hit_rate_all_pct != null ? r.hit_rate_all_pct + '%' : '-') + '</td>' +
        '<td class="fl">' + pct(r.avg_ret_all_pct) + '</td>' +
        '<td class="fl">' + (r.score_edge_pct != null ? (r.score_edge_pct >= 0 ? '+' : '') + r.score_edge_pct : '-') + '</td>' +
        '<td class="dn">' + esc(r.stop_hit) + '</td>' +
        '<td class="up">' + esc(r.target_hit) + '</td></tr>';
    });
    h += '</tbody></table>';

    // per-group history, which is where a failing group shows up
    var groups = {};
    rows.forEach(function (r) {
      var bg = r.by_group || {};
      for (var g in bg) {
        (groups[g] = groups[g] || []).push({ date: r.date, v: bg[g] });
      }
    });
    var gk = Object.keys(groups);
    if (gk.length) {
      h += '<div class="fl" style="font-size:9px;margin:8px 0 2px">By group (mean over archived days)</div>';
      h += '<table class="pool-table"><thead><tr><th>Group</th><th>Days</th>' +
        '<th>Mean hit</th><th>Mean avg</th></tr></thead><tbody>';
      gk.forEach(function (g) {
        var xs = groups[g];
        var hs = xs.map(function (x) { return x.v.hit_rate_pct; }).filter(function (x) { return x != null; });
        var as = xs.map(function (x) { return x.v.avg_ret_pct; }).filter(function (x) { return x != null; });
        h += '<tr><td>' + esc(groupLabel(g)) + '</td><td>' + xs.length + '</td>' +
          '<td class="' + col((mean(hs) || 0) - 50) + '">' +
          (mean(hs) != null ? mean(hs).toFixed(1) + '%' : '-') + '</td>' +
          '<td class="' + col(mean(as) || 0) + '">' +
          (mean(as) != null ? (mean(as) >= 0 ? '+' : '') + mean(as).toFixed(2) + '%' : '-') + '</td></tr>';
      });
      h += '</tbody></table>';
    }
    h += '<div class="fl" style="font-size:9px;margin-top:6px">' +
      'Historical records. Past performance does not imply future results.</div>';
    box.innerHTML = h;
  }

  async function render() {
    if (!window.DB) return;
    var res = await Promise.allSettled([
      window.DB.morningReview(), window.DB.afternoonPicks(), window.DB.yesterdayReview(),
      window.DB.reviewHistory()
    ]);
    renderMorning(res[0].status === 'fulfilled' ? res[0].value : null);
    renderAfternoon(res[1].status === 'fulfilled' ? res[1].value : null);
    renderYesterday(res[2].status === 'fulfilled' ? res[2].value : null);
    renderHistory(res[3].status === 'fulfilled' ? res[3].value : null);
  }

  window.Picks = { render: render };
  if (document.readyState !== 'loading') render();
  else document.addEventListener('DOMContentLoaded', render);
})();
