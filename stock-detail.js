// stock-detail.js — Per-stock detail panel: K-line, fund flow, minute line.
//
// Data priority (cloud-first, local copy removed 2026-10-14):
//   1. Cloud warehouse via window.DB  (jiegengxiangjianxue*/db-stocks-*)
//   2. Direct provider calls          (tencent kline, eastmoney fflow)
// The warehouse gives bundled day history/technical/financial fields when
// present; direct calls provide realtime quote, minute bars, and fund flow.
(function () {
  'use strict';

  var UP = '#f85149', DN = '#3fb950', BLUE = '#58a6ff';

  function prefixed(code) {
    var c = String(code).replace(/\D/g, '').padStart(6, '0');
    if (/^(60|68|90|11|13)/.test(c)) return 'sh' + c;
    if (/^(43|83|87|88|92)/.test(c)) return 'bj' + c;
    return 'sz' + c;
  }

  function bare(code) {
    var p = prefixed(code);
    return p.slice(2);
  }

  function jget(url) {
    return fetch(url).then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  var KLT_MAP = { '1m': 1, '5m': 5, '15m': 15, '30m': 30, '60m': 60,
    day: 101, week: 102, month: 103, year: 104 };

  function secid(code) {
    var b = bare(code);
    return (prefixed(code).slice(0, 2) === 'sh' ? '1.' : '0.') + b;
  }

  function fetchKlineTencent(code, n, period, adjust) {
    var p = prefixed(code);
    var klt = period || 'day';
    var tencentPeriod = { '1m': 'm1', '5m': 'm5', '15m': 'm15',
      '30m': 'm30', '60m': 'm60' }[klt] || klt;
    var cnt = n || 60;
    var fq = adjust === 'hfq' ? 'hfq' : 'qfq';
    var url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
      + p + ',' + tencentPeriod + ',,' + (klt === 'day' ? ',' : ',,') + cnt + ',' + fq;
    return jget(url).then(function (d) {
      var node = (d && d.data && d.data[p]) || {};
      var prefix = fq === 'hfq' ? 'hfq' : 'qfq';
      var key = klt === 'day' ? prefix + 'day' : (klt === 'year' ? 'year' : prefix + tencentPeriod);
      var arr = node[key] || (fq === 'qfq' ? node.qfqday : node.hfqday) || node.day || [];
      return arr.map(function (r) {
        return { date: r[0], open: +r[1], close: +r[2], high: +r[3], low: +r[4], vol: +r[5] };
      });
    });
  }

  // 东财: 全史 6019 根, fqt=1 前复权, fields2 = 日期/开/收/高/低/成交量(手)/成交额/振幅
  function fetchKlineEastmoney(code, n, period, adjust) {
    var url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
      + '?secid=' + secid(code)
      + '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58'
      + '&klt=' + (KLT_MAP[period] || 101) + '&fqt=' + (adjust === 'hfq' ? 2 : 1) + '&end=20500101&lmt=' + (n || 60);
    return jget(url).then(function (d) {
      var klines = ((d || {}).data || {}).klines || [];
      return klines.map(function (row) {
        var f = row.split(',');
        return { date: f[0], open: +f[1], close: +f[2], high: +f[3], low: +f[4], vol: +f[5] };
      });
    });
  }

  // 新浪: scale 240=日 1200=周 7200=月, 只支持日/周/月三档
  function fetchKlineSina(code, n, period) {
    var scale = { day: 240, week: 1200, month: 7200 }[period || 'day'] || 240;
    var url = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
      + 'CN_MarketData.getKLineData?symbol=' + prefixed(code)
      + '&scale=' + scale + '&ma=5&datalen=' + (n || 60);
    return jget(url).then(function (arr) {
      if (!Array.isArray(arr)) return [];
      return arr.map(function (r) {
        return { date: r.day, open: +r.open, close: +r.close,
                 high: +r.high, low: +r.low, vol: +r.volume };
      });
    });
  }

  function fetchKline(code, n, period, adjust) {
    // 云端 bundle 的前复权日线优先；非日线、后复权或缺失时走
    // 腾讯→东财→新浪直取，保留实时源回退。
    var cloudFirst = (window.DB && window.DB.stockKline)
      ? window.DB.stockKline(code, period || 'day') : Promise.resolve([]);
    return cloudFirst
      .then(function (bars) { return bars && bars.length && (adjust || 'qfq') === 'qfq' && (period || 'day') === 'day' ? bars : fetchKlineTencent(code, n, period, adjust); })
      .then(function (bars) { return bars && bars.length ? bars : fetchKlineEastmoney(code, n, period, adjust); })
      .then(function (bars) {
        var sinaPeriod = /^(day|week|month)$/.test(period || 'day') ? period : 'day';
        return bars && bars.length ? bars : fetchKlineSina(code, n, sinaPeriod);
      })
      .catch(function () { return []; });
  }

  // 主力资金流。push2his 有完整历史; push2delay 的 ACAO 是 * (更宽松),
  // 但 lmt 只返当日, 所以只作兜底。任一级返回空数组都会继续往下走。
  function fetchFundFlow(code, n) {
    var b = bare(code);
    var secid = (prefixed(code).slice(0, 2) === 'sh' ? '1.' : '0.') + b;
    var fields = '&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65';
    var url = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
      + '?secid=' + secid + '&lmt=' + (n || 30) + '&klt=101' + fields;
    return jget(url).then(function (d) {
      var klines = ((d || {}).data || {}).klines || [];
      if (klines.length) return parseFlows(klines);
      // push2his 不可达 → push2delay 兜底 (只返当日)
      return jget('https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get'
        + '?secid=' + secid + '&lmt=' + (n || 30) + '&klt=101' + fields)
        .then(function (d2) { return parseFlows(((d2 || {}).data || {}).klines || []); });
    });
  }

  function parseFlows(klines) {
    return klines.map(function (row) {
      var f = row.split(',');
      return {
        date: f[0],
        main_net: +f[1], small: +f[2], medium: +f[3],
        large: +f[4], xlarge: +f[5],
        main_pct: +f[6],
        close: f.length > 11 ? +f[11] : 0
      };
    });
  }

  function fetchMinute(code) {
    var p = prefixed(code);
    return jget('https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + p)
      .then(function (d) {
        var node = (d && d.data && d.data[p]) || {};
        var arr = [];
        if (Array.isArray(node.data)) arr = node.data;
        else if (node.data && Array.isArray(node.data.data)) arr = node.data.data;
        return arr.map(function (row) {
          var f = String(row).split(' ');
          return { t: f[0], price: +f[1], vol: +f[2], amt: +f[3] };
        }).filter(function (x) { return x.price > 0; });
      })
      .then(function (mins) {
        return mins && mins.length ? mins : fetchMinuteEastmoney(code);
      })
      .catch(function () { return fetchMinuteEastmoney(code); });
  }

  // 东财分时兜底: trends2 返回 "YYYY-MM-DD HH:MM,price,vol,avgprice"
  function fetchMinuteEastmoney(code) {
    var url = 'https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=' + secid(code)
      + '&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13'
      + '&fields2=f51,f53,f56,f58&iscr=0&ndays=1';
    return jget(url).then(function (d) {
      var trends = ((d || {}).data || {}).trends || [];
      return trends.map(function (row) {
        var f = String(row).split(',');
        return { t: (f[0] || '').slice(11), price: +f[1], vol: +f[2], amt: 0 };
      }).filter(function (x) { return x.price > 0; });
    });
  }

  // 实时报价: 腾讯 qt.gtimg 为主 (GBK 解码保证中文名不乱码),
  // 被 WAF 拦时降级东财 push2 单股接口 (字段已 /100 还原)。
  function fetchQuoteEastmoney(code) {
    var url = 'https://push2.eastmoney.com/api/qt/stock/get?secid=' + secid(code)
      + '&fields=f43,f44,f45,f46,f57,f58,f60,f116,f162,f167,f170';
    return jget(url).then(function (d) {
      var v = (d || {}).data;
      if (!v || !v.f43) return null;
      return {
        code: prefixed(code), name: v.f58 || '',
        price: v.f43 / 100, prev_close: v.f60 / 100,
        open: v.f46 / 100, high: v.f44 / 100, low: v.f45 / 100,
        chg_pct: v.f170 / 100, turnover: 0,
        pe: v.f162 / 100, mktcap: v.f116, pb: v.f167 / 100, time: ''
      };
    });
  }

  function fetchQuote(code) {
    var p = prefixed(code);
    return fetch('https://qt.gtimg.cn/q=' + p)
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) {
        var txt;
        try { txt = new TextDecoder('gbk').decode(buf); } catch (e) { txt = ''; }
        var m = txt.match(/v_([a-z0-9]+)="([^"]*)"/);
        if (!m) return null;
        var f = m[2].split('~');
        if (f.length < 40) return null;
        return {
          code: code, name: f[1], price: +f[3], prev_close: +f[4], open: +f[5],
          high: +f[33], low: +f[34], chg_pct: +f[32], turnover: +f[38],
          pe: +f[39], mktcap: (+f[44] || 0) * 1e8, pb: +f[46], time: f[30]
        };
      })
      .then(function (q) { return q || fetchQuoteEastmoney(code); })
      .catch(function () { return fetchQuoteEastmoney(code); });
  }

  // ─── charts ─────────────────────────────────────────────────
  // K 线图: 静态 SVG。时间轴 / 价位轴 / 成交量 全部常驻显示,
  // 不依赖 hover — hover 只做逐日明细放大。
  function fmtVol(v) {
    if (v == null || !isFinite(v)) return '—';
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return String(Math.round(v));
  }

  function renderKlineChart(bars) {
    if (!bars || bars.length < 5) {
      return '<div class="fl" style="font-size:9px;line-height:1.5">' +
        'K线取数失败 (tencent / eastmoney / sina 三级均未返回)<br>' +
        '可能原因: 当前网络到这三个源均被拦截</div>';
    }
    var W = 460, H = 226, PL = 6, PR = 52, PT = 8, PB = 22;
    var VOL_H = 38;
    var priceH = H - PT - PB - VOL_H - 4;
    var cw = W - PL - PR;
    var hi = -1e18, lo = 1e18, vmax = 0;
    bars.forEach(function (b) {
      hi = Math.max(hi, b.high); lo = Math.min(lo, b.low);
      vmax = Math.max(vmax, b.vol || 0);
    });
    var rng = (hi - lo) || 1;
    var bw = cw / bars.length;
    var cx = function (i) { return PL + i * bw + bw / 2; };
    var yP = function (v) { return PT + (hi - v) / rng * priceH; };
    var volTop = PT + priceH + 16;

    var out = '';
    // 价格网格 + 右侧价位刻度 (常驻, 两位小数)
    for (var g = 0; g <= 4; g++) {
      var v = hi - rng * g / 4, y = PT + priceH * g / 4;
      out += '<line x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1)
        + '" stroke="#1e2738" stroke-width="0.5"/>';
      out += '<text x="' + (W - PR + 4) + '" y="' + (y + 3).toFixed(1) + '" fill="#8b949e" font-size="8">'
        + v.toFixed(2) + '</text>';
    }
    // K 线实体 + 影线
    bars.forEach(function (b, i) {
      var x = cx(i);
      var col = b.close >= b.open ? UP : DN;
      out += '<line x1="' + x.toFixed(1) + '" y1="' + yP(b.high).toFixed(1)
        + '" x2="' + x.toFixed(1) + '" y2="' + yP(b.low).toFixed(1)
        + '" stroke="' + col + '" stroke-width="1"/>';
      var top = Math.min(yP(b.open), yP(b.close));
      var h = Math.max(1, Math.abs(yP(b.close) - yP(b.open)));
      out += '<rect x="' + (x - bw * 0.35).toFixed(1) + '" y="' + top.toFixed(1)
        + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + h.toFixed(1)
        + '" fill="' + col + '"/>';
    });
    // 成交量柱
    bars.forEach(function (b, i) {
      var x = cx(i);
      var col = b.close >= b.open ? UP : DN;
      var h = vmax > 0 ? (b.vol || 0) / vmax * VOL_H : 0;
      if (h < 0.5) h = 0.5;
      out += '<rect x="' + (x - bw * 0.35).toFixed(1) + '" y="' + (volTop + VOL_H - h).toFixed(1)
        + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + h.toFixed(1)
        + '" fill="' + col + '" opacity="0.55"/>';
    });
    // 成交量刻度 (常驻)
    out += '<text x="' + PL + '" y="' + (volTop - 3).toFixed(1) + '" fill="#8b949e" font-size="7">量 '
      + fmtVol(vmax) + '</text>';
    out += '<text x="' + PL + '" y="' + (volTop + VOL_H + 7).toFixed(1) + '" fill="#8b949e" font-size="7">0</text>';
    // 时间轴: bars<=20 时隔根标, 否则 5 等分
    var marks;
    if (bars.length <= 20) {
      marks = [];
      for (var mi = 0; mi < bars.length; mi += (bars.length > 10 ? 2 : 1)) marks.push(mi);
      if (marks[marks.length - 1] !== bars.length - 1) marks.push(bars.length - 1);
    } else {
      marks = [0, Math.floor(bars.length / 4), Math.floor(bars.length / 2),
               Math.floor(bars.length * 3 / 4), bars.length - 1];
    }
    marks.forEach(function (i) {
      var anchor = i === 0 ? 'start' : (i === bars.length - 1 ? 'end' : 'middle');
      out += '<text x="' + cx(i).toFixed(1) + '" y="' + (H - 6) + '" fill="#8b949e" font-size="8"'
        + ' text-anchor="' + anchor + '">' + (bars[i].date || '').slice(5) + '</text>';
    });
    // 最新价标签 (常驻, 贴最后一根 K 线)
    var lastBar = bars[bars.length - 1];
    var lastCol = lastBar.close >= lastBar.open ? UP : DN;
    out += '<line x1="' + PL + '" y1="' + yP(lastBar.close).toFixed(1) + '" x2="' + (W - PR)
      + '" y2="' + yP(lastBar.close).toFixed(1) + '" stroke="' + lastCol
      + '" stroke-width="0.6" stroke-dasharray="3,2" opacity="0.7"/>';
    out += '<rect x="' + (W - PR + 1) + '" y="' + (yP(lastBar.close) - 6).toFixed(1)
      + '" width="' + (PR - 3) + '" height="12" fill="' + lastCol + '" rx="2"/>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (yP(lastBar.close) + 2.5).toFixed(1)
      + '" fill="#0d1117" font-size="8" font-weight="700">' + lastBar.close.toFixed(2) + '</text>';
    // 十字光标 (默认隐藏, hover 时定位)
    out += '<g id="kx" style="display:none" pointer-events="none">'
      + '<line id="kxV" y1="' + PT + '" y2="' + (volTop + VOL_H) + '" stroke="#7d8590" stroke-width="0.5" stroke-dasharray="2,2"/>'
      + '<line id="kxH" x1="' + PL + '" x2="' + (W - PR) + '" stroke="#7d8590" stroke-width="0.5" stroke-dasharray="2,2"/>'
      + '</g>';
    var svg = '<svg id="klineSvg" viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:226px;display:block">'
      + out + '</svg>';
    // 常驻明细条: 最后一根的 日期/开/高/低/收/涨跌/成交量
    var lb = lastBar, lbChg = lb.open > 0 ? (lb.close - lb.open) / lb.open * 100 : 0;
    var lbCol = lbChg >= 0 ? UP : DN;
    var detail = '<div id="klineTip" style="font-size:10px;color:#c9d1d9;padding:3px 2px 0;line-height:1.5">'
      + '<b>' + (lb.date || '') + '</b> '
      + '开 <b>' + lb.open.toFixed(2) + '</b> '
      + '高 <b class="up">' + lb.high.toFixed(2) + '</b> '
      + '低 <b class="dn">' + lb.low.toFixed(2) + '</b> '
      + '收 <b>' + lb.close.toFixed(2) + '</b> '
      + '<span style="color:' + lbCol + '">' + (lbChg >= 0 ? '+' : '') + lbChg.toFixed(2) + '%</span> '
      + '量 <b>' + fmtVol(lb.vol) + '</b>'
      + '<span class="fl" style="font-size:9px"> · 移动鼠标看每日明细</span></div>';
    return svg + detail;
  }

  // K 线 hover: 由 renderKlineChart 的 id=kx 十字光标 + id=klineTip 组成
  function bindKlineHover(bars) {
    var svg = document.getElementById('klineSvg');
    if (!svg || !bars || !bars.length) return;
    var g = svg.querySelector('#kx'), vLine = svg.querySelector('#kxV'), hLine = svg.querySelector('#kxH');
    var tip = document.getElementById('klineTip');
    var W = 460, PL = 6, PR = 52, PT = 8, H = 210, VOL_H = 34;
    var priceH = H - PT - 18 - VOL_H - 4;
    var cw = W - PL - PR, bw = cw / bars.length;
    svg.addEventListener('mousemove', function (ev) {
      var r = svg.getBoundingClientRect();
      var sx = (ev.clientX - r.left) / r.width * W;
      var i = Math.floor((sx - PL) / bw);
      if (i < 0) i = 0;
      if (i > bars.length - 1) i = bars.length - 1;
      var b = bars[i];
      var x = PL + i * bw + bw / 2;
      g.style.display = 'block';
      vLine.setAttribute('x1', x); vLine.setAttribute('x2', x);
      var y = PT + (ev.clientY - r.top) / r.height * H;
      if (y < PT) y = PT;
      if (y > PT + priceH + 6 + VOL_H) y = PT + priceH + 6 + VOL_H;
      hLine.setAttribute('y1', y); hLine.setAttribute('y2', y);
      if (tip) {
        var pct = '';
        if (i > 0 && bars[i - 1].close) {
          var p = (b.close - bars[i - 1].close) / bars[i - 1].close * 100;
          pct = ' <span style="color:' + (p >= 0 ? UP : DN) + '">' + (p >= 0 ? '+' : '') + p.toFixed(2) + '%</span>';
        }
        var volTxt = b.vol ? (b.vol >= 1e8 ? (b.vol / 1e8).toFixed(2) + '亿手'
          : b.vol >= 1e4 ? (b.vol / 1e4).toFixed(1) + '万手' : Math.round(b.vol) + '手') : '—';
        tip.innerHTML = '<b>' + (b.date || '') + '</b> '
          + '开' + b.open.toFixed(2) + ' 高' + b.high.toFixed(2)
          + ' 低' + b.low.toFixed(2) + ' 收' + b.close.toFixed(2) + pct
          + ' <span style="color:#8b949e">量 ' + volTxt + '</span>';
      }
    });
    svg.addEventListener('mouseleave', function () {
      g.style.display = 'none';
      if (tip) tip.innerHTML = '移动鼠标查看每日 开/高/低/收 · 涨跌幅 · 成交量';
    });
  }

  function renderFundChart(flows) {
    if (!flows || !flows.length) {
      return '<div class="fl" style="font-size:9px;line-height:1.5">' +
        '资金流取数失败 (eastmoney push2his / push2delay 均未返回)<br>' +
        '可能原因: 当前网络到东财被拦, 或该股当日无资金流数据</div>';
    }
    var W = 460, H = 140, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var mx = 0;
    flows.forEach(function (f) { mx = Math.max(mx, Math.abs(f.main_net)); });
    mx = mx || 1;
    var bw = cw / flows.length;
    var y0 = PT + ch / 2;
    var out = '<line x1="' + PL + '" y1="' + y0.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y0.toFixed(1)
      + '" stroke="#263044" stroke-width="1"/>';
    flows.forEach(function (f, i) {
      var x = PL + i * bw;
      var h = Math.abs(f.main_net) / mx * (ch / 2);
      var up = f.main_net >= 0;
      out += '<rect x="' + (x + bw * 0.15).toFixed(1) + '" y="' + (up ? y0 - h : y0).toFixed(1)
        + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + Math.max(1, h).toFixed(1)
        + '" fill="' + (up ? UP : DN) + '" opacity="0.85"/>';
    });
    out += '<text x="' + (W - PR + 4) + '" y="' + (PT + 8) + '" fill="#586069" font-size="8">+' + (mx / 1e8).toFixed(1) + 'Yi</text>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (H - PB) + '" fill="#586069" font-size="8">-' + (mx / 1e8).toFixed(1) + 'Yi</text>';
    out += '<text x="' + PL + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + flows[0].date.slice(5) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:140px;display:block">' + out + '</svg>';
  }

  function renderMinuteChart(mins) {
    if (!mins || mins.length < 5) return '<div class="fl">Minute data unavailable</div>';
    var W = 460, H = 130, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var base = mins[0].price;
    var hi = -1e18, lo = 1e18;
    mins.forEach(function (m) { hi = Math.max(hi, m.price); lo = Math.min(lo, m.price); });
    var rng = (hi - lo) || 1;
    var bw = cw / mins.length;
    var pts = [];
    mins.forEach(function (m, i) {
      pts.push((PL + i * bw + bw / 2).toFixed(1) + ',' + (PT + (hi - m.price) / rng * ch).toFixed(1));
    });
    var yBase = PT + (hi - base) / rng * ch;
    var out = '<line x1="' + PL + '" y1="' + yBase.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + yBase.toFixed(1)
      + '" stroke="#263044" stroke-width="1" stroke-dasharray="3 2"/>';
    out += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + BLUE + '" stroke-width="1.2"/>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:130px;display:block">' + out + '</svg>';
  }

  function fmtYi(v) {
    if (v == null) return '-';
    var a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + 'Yi';
    if (a >= 1e4) return (v / 1e4).toFixed(0) + 'W';
    return v.toFixed(0);
  }

  function finiteNumber(v) {
    return typeof v === 'number' && isFinite(v) ? v : null;
  }

  function average(values) {
    var xs = values.filter(function (v) { return finiteNumber(v) !== null; });
    return xs.length ? xs.reduce(function (a, v) { return a + v; }, 0) / xs.length : null;
  }

  function smaAt(values, end, period) {
    if (end < period - 1) return null;
    return average(values.slice(end - period + 1, end + 1));
  }

  function emaSeries(values, period) {
    var out = [], k = 2 / (period + 1), prev = null;
    values.forEach(function (v, i) {
      if (finiteNumber(v) === null) { out.push(null); return; }
      if (prev === null) {
        if (i < period - 1) { out.push(null); return; }
        prev = average(values.slice(i - period + 1, i + 1));
      } else prev = v * k + prev * (1 - k);
      out.push(prev);
    });
    return out;
  }

  function calculateIndicators(bars, cloudTech) {
    if (!Array.isArray(bars) || bars.length < 2) return { available: false, reason: 'K线少于 2 根' };
    var closes = bars.map(function (b) { return finiteNumber(b.close); });
    var last = closes.length - 1, close = closes[last];
    if (close === null) return { available: false, reason: '最新收盘价缺失' };
    var e12 = emaSeries(closes, 12), e26 = emaSeries(closes, 26);
    var dif = closes.map(function (_, i) { return e12[i] !== null && e26[i] !== null ? e12[i] - e26[i] : null; });
    var dea = emaSeries(dif, 9);
    var deltas = [], gains = [], losses = [];
    for (var i = 1; i <= last; i++) {
      var d = closes[i] !== null && closes[i - 1] !== null ? closes[i] - closes[i - 1] : null;
      if (d !== null) { deltas.push(d); gains.push(Math.max(0, d)); losses.push(Math.max(0, -d)); }
    }
    var gain = average(gains.slice(-14)), loss = average(losses.slice(-14));
    var rsi = gain !== null && loss !== null && gain + loss > 0 ? 100 * gain / (gain + loss) : null;
    var window20 = bars.slice(-20), highs = window20.map(function (b) { return finiteNumber(b.high); });
    var lows = window20.map(function (b) { return finiteNumber(b.low); });
    var validHighs = highs.filter(function (v) { return v !== null; });
    var validLows = lows.filter(function (v) { return v !== null; });
    var support = validLows.length ? Math.min.apply(Math, validLows) : null;
    var resistance = validHighs.length ? Math.max.apply(Math, validHighs) : null;
    var ma5 = smaAt(closes, last, 5), ma20 = smaAt(closes, last, 20), ma60 = smaAt(closes, last, 60);
    var sdVals = closes.slice(-20).filter(function (v) { return v !== null; });
    var mean20 = average(sdVals), variance = mean20 === null ? null : average(sdVals.map(function (v) { return Math.pow(v - mean20, 2); }));
    var sd20 = variance === null ? null : Math.sqrt(variance);
    var vol = bars.map(function (b) { return finiteNumber(b.vol); });
    var vol5 = average(vol.slice(-5)), vol20 = average(vol.slice(-20));
    var volumeRatio = vol5 !== null && vol20 ? vol5 / vol20 : null;
    var typical = bars.map(function (b) {
      return finiteNumber(b.high) !== null && finiteNumber(b.low) !== null && close !== null
        ? (b.high + b.low + b.close) / 3 : null;
    });
    var ma3 = smaAt(closes, last, 3), ma6 = smaAt(closes, last, 6), ma10 = smaAt(closes, last, 10), ma12 = smaAt(closes, last, 12), ma24 = smaAt(closes, last, 24), ma50 = smaAt(closes, last, 50);
    var cciWindow = typical.slice(-20), cciMean = average(cciWindow), cciDev = cciMean === null ? null : average(cciWindow.map(function (v) { return v === null ? null : Math.abs(v - cciMean); }));
    var cci = cciMean !== null && cciDev ? (typical[last] - cciMean) / (0.015 * cciDev) : null;
    var wrHigh = validHighs.length ? Math.max.apply(Math, validHighs) : null, wrLow = validLows.length ? Math.min.apply(Math, validLows) : null;
    var wr14 = wrHigh > wrLow ? (wrHigh - close) / (wrHigh - wrLow) * -100 : null;
    var kdjWindow = bars.slice(-9), kHigh = Math.max.apply(Math, kdjWindow.map(function (b) { return b.high; })), kLow = Math.min.apply(Math, kdjWindow.map(function (b) { return b.low; }));
    var rsv = kHigh > kLow ? (close - kLow) / (kHigh - kLow) * 100 : null;
    var kdjK = rsv === null ? null : (50 * 2 + rsv) / 3, kdjD = kdjK === null ? null : (50 * 2 + kdjK) / 3, kdjJ = kdjK !== null && kdjD !== null ? 3 * kdjK - 2 * kdjD : null;
    var obv = 0;
    bars.forEach(function (b, i) { if (i && b.close > bars[i - 1].close) obv += b.vol || 0; else if (i && b.close < bars[i - 1].close) obv -= b.vol || 0; });
    var bias20 = ma20 ? (close - ma20) / ma20 * 100 : null;
    var bbi = average([ma3, ma6, ma12, ma24]);
    var upVol = 0, downVol = 0, arUp = 0, arDn = 0, brUp = 0, brDn = 0;
    bars.slice(-26).forEach(function (b, i, xs) { var prev = i ? xs[i - 1].close : null; if (b.close >= b.open) upVol += b.vol || 0; else downVol += b.vol || 0; arUp += b.high - b.open; arDn += b.open - b.low; if (prev !== null) { brUp += b.high - prev; brDn += prev - b.low; } });
    var vr = downVol ? upVol / downVol : null, arbr = arDn ? arUp / arDn * 100 : null, br = brDn ? brUp / brDn * 100 : null;
    var trSum = 0, plusDm = 0, minusDm = 0;
    bars.slice(-14).forEach(function (b, i, xs) {
      if (!i) return;
      var prev = xs[i - 1], tr = Math.max(b.high - b.low, Math.abs(b.high - prev.close), Math.abs(b.low - prev.close));
      var upMove = b.high - prev.high, downMove = prev.low - b.low;
      trSum += tr; plusDm += upMove > downMove && upMove > 0 ? upMove : 0; minusDm += downMove > upMove && downMove > 0 ? downMove : 0;
    });
    var plusDi = trSum ? plusDm / trSum * 100 : null, minusDi = trSum ? minusDm / trSum * 100 : null;
    var adx = plusDi !== null && minusDi !== null && plusDi + minusDi ? Math.abs(plusDi - minusDi) / (plusDi + minusDi) * 100 : null;
    var psy = bars.slice(-12).filter(function (b, i, xs) { return i && b.close > xs[i - 1].close; }).length / 11 * 100;
    var dma = ma10 !== null && ma50 !== null ? ma10 - ma50 : null, dpo = ma20 !== null ? close - ma20 : null;
    var trix1 = emaSeries(closes, 12), trix2 = emaSeries(trix1, 12), trix3 = emaSeries(trix2, 12), trix = trix3[last] !== null && trix3[last - 1] ? (trix3[last] - trix3[last - 1]) / trix3[last - 1] * 100 : null;
    var sar = isFinite(support) ? support : null, eneUpper = ma10 !== null ? ma10 * 1.11 : null, eneLower = ma10 !== null ? ma10 * 0.89 : null;
    var pos20 = resistance !== null && support !== null && resistance > support ? (close - support) / (resistance - support) : null;
    var usable = [ma5, ma20, e12[last], e26[last], dif[last], dea[last], rsi, support, resistance].filter(function (v) { return v !== null; }).length;
    var score = null, grade = 'insufficient_data';
    if (bars.length >= 60 && usable >= 8) {
      var trend = (close >= ma20 ? 20 : 0) + (ma20 >= ma60 ? 15 : 0) + (close >= e26[last] ? 10 : 0);
      var momentum = (dif[last] >= dea[last] ? 15 : 0) + (rsi >= 45 && rsi <= 70 ? 15 : (rsi > 30 && rsi < 80 ? 8 : 0));
      var location = pos20 === null ? 0 : (pos20 >= 0.25 && pos20 <= 0.8 ? 15 : 5);
      var liquidity = volumeRatio === null ? 0 : (volumeRatio >= 0.8 && volumeRatio <= 2.5 ? 10 : 5);
      score = Math.round(trend + momentum + location + liquidity);
      grade = score >= 80 ? 'A' : score >= 65 ? 'B' : score >= 50 ? 'C' : 'D';
    }
    return {
      available: true, count: bars.length, close: close, ma5: ma5, ma20: ma20, ma60: ma60,
      ema12: e12[last], ema26: e26[last], dif: dif[last], dea: dea[last],
      macdHistogram: dif[last] !== null && dea[last] !== null ? dif[last] - dea[last] : null,
      rsi14: rsi, bollMid: mean20, bollUpper: mean20 !== null && sd20 !== null ? mean20 + sd20 * 2 : null,
      bollLower: mean20 !== null && sd20 !== null ? mean20 - sd20 * 2 : null,
      support: support, resistance: resistance,
      volumeRatio: volumeRatio, pos20: pos20, cci: cci, wr14: wr14, kdjK: kdjK, kdjD: kdjD, kdjJ: kdjJ,
      obv: obv, bias20: bias20, bbi: bbi, trix: trix, vr: vr, arbr: arbr, br: br, psy: psy,
      dma: dma, dpo: dpo, sar: sar, eneUpper: eneUpper, eneLower: eneLower,
      plusDi: plusDi, minusDi: minusDi, adx: adx,
      score: score, grade: grade,
      scoreReason: score === null ? '至少需要 60 根完整 K 线及足够指标字段' : '技术分基于 K 线计算，非云端投顾评级',
      source: cloudTech ? 'cloud technical + client recomputation' : 'client recomputation from K线'
    };
  }

  function metric(v, digits, suffix) {
    return v === null || v === undefined || !isFinite(v) ? '缺失' : Number(v).toFixed(digits || 2) + (suffix || '');
  }

  function dataStatus(label, ok, detail) {
    return '<span style="color:' + (ok ? 'var(--tx3)' : '#6e7681') + '">' + label + ': ' +
      (ok ? detail : '缺失') + '</span>';
  }

  function dateRangeText(meta) {
    if (!meta || !Array.isArray(meta.range) || meta.range.length < 2) return '缺失';
    return [meta.range[0], meta.range[1]].sort().join(' 至 ');
  }

  function renderIndicators(ind, cloudTech) {
    if (!ind.available) return '<div class="fl">指标不可计算：' + ind.reason + '</div>';
    var t = cloudTech || {};
    return '<div style="font-size:9px;line-height:1.7;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px 8px">' +
      '<span>MA5 ' + metric(ind.ma5) + '</span><span>MA20 ' + metric(ind.ma20) + '</span><span>MA60 ' + metric(ind.ma60) + '</span>' +
      '<span>EMA12 ' + metric(ind.ema12) + '</span><span>EMA26 ' + metric(ind.ema26) + '</span><span>RSI14 ' + metric(ind.rsi14) + '</span>' +
      '<span>MACD DIF ' + metric(ind.dif, 3) + '</span><span>DEA ' + metric(ind.dea, 3) + '</span><span>柱 ' + metric(ind.macdHistogram, 3) + '</span>' +
      '<span>BOLL上 ' + metric(ind.bollUpper) + '</span><span>BOLL中 ' + metric(ind.bollMid) + '</span><span>BOLL下 ' + metric(ind.bollLower) + '</span>' +
      '<span>量比 ' + metric(ind.volumeRatio) + '</span><span>20日位 ' + metric(ind.pos20 === null ? null : ind.pos20 * 100, 1, '%') + '</span>' +
      '<span>支撑 ' + metric(ind.support) + '</span><span>阻力 ' + metric(ind.resistance) + '</span>' +
      '<span>CCI20 ' + metric(ind.cci, 1) + '</span><span>WR14 ' + metric(ind.wr14, 1) + '</span><span>KDJ ' + metric(ind.kdjK, 1) + '/' + metric(ind.kdjD, 1) + '/' + metric(ind.kdjJ, 1) + '</span>' +
      '<span>OBV ' + metric(ind.obv, 0) + '</span><span>BIAS20 ' + metric(ind.bias20, 1, '%') + '</span><span>BBI ' + metric(ind.bbi) + '</span>' +
      '<span>TRIX ' + metric(ind.trix, 3) + '</span><span>VR ' + metric(ind.vr) + '</span><span>ARBR ' + metric(ind.arbr, 1) + '/' + metric(ind.br, 1) + '</span>' +
      '<span>DMI+/- ' + metric(ind.plusDi, 1) + '/' + metric(ind.minusDi, 1) + '</span><span>ADX ' + metric(ind.adx, 1) + '</span>' +
      '<span>PSY12 ' + metric(ind.psy, 1, '%') + '</span><span>DMA ' + metric(ind.dma) + '</span><span>DPO ' + metric(ind.dpo) + '</span>' +
      '<span>SAR ' + metric(ind.sar) + '</span><span>ENE ' + metric(ind.eneLower) + '-' + metric(ind.eneUpper) + '</span>' +
      '<span>云端MACD ' + metric(t.macd, 3) + '</span></div>';
  }

  function renderPlan(ind) {
    if (!ind.available || ind.score === null || ind.close === null) {
      return '<div class="fl" style="line-height:1.5">交易计划：数据不足，未计算综合分、入场、止损或目标，避免用缺失数据伪造建议。</div>';
    }
    var entryLow = Math.max(ind.support || 0, ind.close * 0.97);
    var entryHigh = Math.min(ind.resistance || ind.close, ind.close * 1.01);
    var stop = ind.support !== null ? ind.support * 0.97 : null;
    var target = ind.resistance !== null ? ind.resistance : null;
    var riskUp = ind.bollUpper, riskDown = ind.bollLower;
    return '<div style="font-size:10px;line-height:1.7;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 10px">' +
      '<span>综合分 <b>' + ind.score + '/100 (' + ind.grade + ')</b></span><span>数据量 <b>' + ind.count + '根</b></span>' +
      '<span>入场范围 <b>' + metric(entryLow) + ' - ' + metric(entryHigh) + '</b></span><span>止损边界 <b>' + metric(stop) + '</b></span>' +
      '<span>目标区间 <b>' + metric(target) + '</b></span><span>止涨边界 <b>' + metric(riskUp) + '</b></span>' +
      '<span>止跌边界 <b>' + metric(riskDown) + '</b></span><span>等级依据 <b>MA/MACD/RSI/位置/量比</b></span>' +
      '</div><div class="fl" style="font-size:9px">仅为可复现技术规则，不构成投资建议；若支撑失效，原计划失效。</div>';
  }

  var ADJUSTS = [{ k: 'qfq', label: '前复权' }, { k: 'hfq', label: '后复权' }];
  var PERIODS = [{ k: '1m', label: '1m' }, { k: '5m', label: '5m' },
    { k: '15m', label: '15m' }, { k: '30m', label: '30m' },
    { k: '60m', label: '60m' }, { k: 'day', label: 'D' },
    { k: 'week', label: 'W' }, { k: 'month', label: 'M' }, { k: 'year', label: 'Y' }];
  var curPeriod = 'day';
  var curAdjust = 'qfq';

  function render(code) {
    var det = document.getElementById('symDetail');
    if (!det) return;
    det.style.display = 'block';
    curPeriod = 'day';
    det.innerHTML = '<b style="font-size:13px">' + code + '</b> ' +
      '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" ' +
      'style="float:right;cursor:pointer;color:var(--tx3)">x</span>' +
      '<br><span class="fl">Loading (tencent kline + eastmoney flow)...</span>';

    var b = bare(code);
    Promise.all([
      fetchQuote(code), fetchKline(code, 60, curPeriod, curAdjust),
      fetchFundFlow(code, 30), fetchMinute(code),
      window.DB ? window.DB.stockTechnical(b) : Promise.resolve(null),
      window.DB ? window.DB.stockQuote(b) : Promise.resolve(null),
      window.DB ? window.DB.stockManifest(b) : Promise.resolve(null),
      window.DB ? window.DB.stockFinancial(b) : Promise.resolve([]),
      window.DB ? window.DB.stockToBoard() : Promise.resolve(null)
    ]).then(function (res) {
      var q = res[0], bars = res[1], flows = res[2], mins = res[3], tech = res[4], wh = res[5];
      var manifest = res[6], financial = res[7], s2b = res[8];
      if (!q && wh && wh.close != null) {
        q = { code: b, name: '云端快照', price: +wh.close, prev_close: 0,
          open: +wh.open || 0, high: +wh.high || 0, low: +wh.low || 0,
          chg_pct: +wh.chg_pct || 0, turnover: 0, pe: 0, mktcap: 0, pb: 0,
          time: manifest && manifest.ts ? manifest.ts : '' };
      }
      var indicators = calculateIndicators(bars, tech);
      var h = '';
      // board membership, so a stock's sector context is visible without
      // leaving the detail panel
      var mine = s2b && s2b.stocks ? s2b.stocks[b] : null;
      if (mine) {
        var tags = (mine.sector || []).concat(mine.industry || []);
        if (tags.length) {
          h += '<div style="margin:3px 0">' + tags.slice(0, 4).map(function (t) {
            return '<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;' +
              'border:1px solid var(--bd);border-radius:8px;color:var(--blue);font-size:9px">' +
              t + '</span>';
          }).join('') + '</div>';
        }
      }
      if (q) {
        var col = q.chg_pct >= 0 ? 'var(--up)' : 'var(--dn)';
        h += '<b style="font-size:14px">' + q.name + ' (' + q.code + ')</b> ' +
          '<span class="fl" style="font-size:9px">' + (q.time || '') + '</span> ' +
          '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" ' +
          'style="float:right;cursor:pointer;color:var(--tx3)">x</span><br>';
        h += '<span style="font-size:18px;font-weight:700;color:' + col + '">' + q.price.toFixed(2) + '</span> ' +
          '<span style="color:' + col + '">' + (q.chg_pct >= 0 ? '+' : '') + q.chg_pct.toFixed(2) + '%</span> ';
        h += '<span class="fl" style="font-size:10px">Turnover ' + q.turnover.toFixed(2) + '% ' +
          'PE ' + (q.pe > 0 ? q.pe.toFixed(1) : 'n/a') + ' ' +
          'Cap ' + (q.mktcap / 1e8).toFixed(0) + 'Yi PB ' + (q.pb > 0 ? q.pb.toFixed(1) : '-') + '</span><br>';
      }
      var dataset = manifest && manifest.datasets ? manifest.datasets : {};
      var klineMeta = dataset.kline || null, finMeta = dataset.financial || null;
      h += '<div style="font-size:9px;line-height:1.6;border-top:1px solid var(--bd);margin-top:4px;padding-top:3px">' +
        '<b>数据状态</b> ' + dataStatus('K线', !!bars && bars.length > 1,
          (klineMeta && klineMeta.range ? dateRangeText(klineMeta) : ((bars || []).length + ' 根'))) + ' · ' +
        dataStatus('财务', Array.isArray(financial) && financial.length > 0,
          (finMeta && finMeta.range ? dateRangeText(finMeta) : financial.length + ' 期')) + ' · ' +
        dataStatus('资金流', !!flows && flows.length > 0, (flows || []).length + ' 日') +
        '<br><span class="fl">快照 ' + ((manifest && manifest.ts) || '缺失') +
        '；日线优先来自云端 stock.json（默认前复权），周期/复权切换走实时源，报价来自实时源，资金流来自 Eastmoney；缺失字段不计分。</span></div>';
      h += '<div style="margin-top:4px"><b style="font-size:10px">技术参数与支撑阻力</b>' +
        '<span class="fl" style="font-size:9px">（' + indicators.source + '）</span></div>';
      h += renderIndicators(indicators, tech);
      h += '<div style="margin-top:4px"><b style="font-size:10px">综合分与交易边界</b></div>' + renderPlan(indicators);
      h += '<div style="font-size:9px;color:var(--tx3);margin-top:3px">算法分析：' +
        (indicators.score === null ? indicators.scoreReason :
          (indicators.grade === 'A' || indicators.grade === 'B' ? '趋势与动量条件相对占优，但需等待价格/支撑确认。' :
            '趋势或动量条件不足，优先观察并等待结构改善。')) + '</div>';
      if (Array.isArray(financial) && financial.length) {
        h += '<div style="margin-top:5px"><b style="font-size:10px">财务摘要</b> <span class="fl" style="font-size:9px">' +
          ((finMeta && finMeta.source) || 'cloud stock.json') + '；非全股覆盖</span></div>';
        h += '<div style="overflow:auto"><table class="pool-table" style="font-size:9px"><thead><tr><th>报告期</th><th>ROE</th><th>净利率</th><th>负债率</th><th>营收TTM</th><th>EPS</th></tr></thead><tbody>';
        financial.slice().sort(function (a, b2) { return String(b2.report_date || '').localeCompare(String(a.report_date || '')); }).slice(0, 4).forEach(function (f) {
          h += '<tr><td>' + (f.report_date || '缺失') + '</td><td>' + (f.roe || '缺失') + '</td><td>' + (f.net_margin || '缺失') + '</td><td>' + (f.debt_ratio || '缺失') + '</td><td>' + (f.revenue_ttm || '缺失') + '</td><td>' + (f.eps || '缺失') + '</td></tr>';
        });
        h += '</tbody></table></div>';
      } else {
        h += '<div class="fl" style="font-size:9px;margin-top:5px">财务摘要：云端 stock.json 未提供，未用财务缺失数据计算分数。</div>';
      }
      h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">Minute (' + (mins ? mins.length : 0) + ' pts)</div>';
      h += renderMinuteChart(mins);
      h += '<div style="display:flex;align-items:center;gap:4px;margin:6px 0 2px;flex-wrap:wrap">' +
        '<span class="fl" style="font-size:9px">K-line (' + (bars ? bars.length : 0) + ' bars)</span>';
      ADJUSTS.forEach(function (a) {
        var adjustOn = a.k === curAdjust;
        h += '<button data-adjust="' + a.k + '" style="font-size:9px;padding:1px 6px;border-radius:8px;cursor:pointer;border:1px solid ' +
          (adjustOn ? 'var(--accent)' : 'var(--border)') + ';background:' + (adjustOn ? 'rgba(137,87,229,.2)' : 'transparent') + ';color:' +
          (adjustOn ? 'var(--accent)' : 'var(--tx3)') + ';font-weight:700">' + a.label + '</button>';
      });
      PERIODS.forEach(function (p) {
        var on = p.k === curPeriod;
        h += '<button data-klt="' + p.k + '" style="font-size:9px;padding:1px 6px;border-radius:8px;cursor:pointer;' +
          'border:1px solid ' + (on ? 'var(--accent)' : 'var(--border)') + ';' +
          'background:' + (on ? 'rgba(137,87,229,.2)' : 'transparent') + ';' +
          'color:' + (on ? 'var(--accent)' : 'var(--tx3)') + ';font-weight:700">' + p.label + '</button>';
      });
      h += '</div><div id="klineChartBox">' + renderKlineChart(bars) + '</div>';
      h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">Main fund flow (last ' +
        (flows ? flows.length : 0) + ' days) - eastmoney push2his</div>';
      h += '<div id="flowChartBox">' + renderFundChart(flows) + '</div>';
      if (flows && flows.length) {
        var inflow = flows.filter(function (f) { return f.main_net > 0; })
          .reduce(function (s, f) { return s + f.main_net; }, 0);
        var outflow = flows.filter(function (f) { return f.main_net < 0; })
          .reduce(function (s, f) { return s + f.main_net; }, 0);
        h += '<div style="display:flex;gap:10px;font-size:10px;margin:4px 0">' +
          '<span>Inflow <b class="up">' + fmtYi(inflow) + '</b></span>' +
          '<span>Outflow <b class="dn">' + fmtYi(outflow) + '</b></span>' +
          '<span>Net <b class="' + (inflow + outflow >= 0 ? 'up' : 'dn') + '">' + fmtYi(inflow + outflow) + '</b></span></div>';
        h += '<table class="pool-table" style="margin-top:6px"><thead><tr><th>Date</th><th>Main</th><th>%</th>' +
          '<th>XLarge</th><th>Large</th><th>Medium</th><th>Small</th><th>Close</th></tr></thead><tbody>';
        flows.slice(0, 12).forEach(function (f) {
          h += '<tr><td>' + f.date.slice(5) + '</td>' +
            '<td class="' + (f.main_net >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.main_net) + '</td>' +
            '<td class="' + (f.main_net >= 0 ? 'up' : 'dn') + '">' + f.main_pct.toFixed(2) + '</td>' +
            '<td class="' + (f.xlarge >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.xlarge) + '</td>' +
            '<td class="' + (f.large >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.large) + '</td>' +
            '<td class="' + (f.medium >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.medium) + '</td>' +
            '<td class="' + (f.small >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.small) + '</td>' +
            '<td>' + (f.close ? f.close.toFixed(2) : '-') + '</td></tr>';
        });
        h += '</tbody></table>';
        h += '<div class="fl" style="font-size:9px;margin-top:4px">Research reference only, not investment advice.</div>';
      }
      det.innerHTML = h;
      bindKlineHover(bars);
      Array.prototype.forEach.call(det.querySelectorAll('button[data-adjust]'), function (btn) {
        btn.onclick = function () {
          curAdjust = btn.dataset.adjust;
          var box = document.getElementById('klineChartBox');
          if (box) box.innerHTML = '<span class="fl" style="font-size:9px">Loading...</span>';
          fetchKline(code, 60, curPeriod, curAdjust).then(function (nb) {
            if (box) box.innerHTML = renderKlineChart(nb);
            bindKlineHover(nb);
            Array.prototype.forEach.call(det.querySelectorAll('button[data-adjust]'), function (b2) {
              var on2 = b2.dataset.adjust === curAdjust;
              b2.style.border = '1px solid ' + (on2 ? 'var(--accent)' : 'var(--border)');
              b2.style.background = on2 ? 'rgba(137,87,229,.2)' : 'transparent';
              b2.style.color = on2 ? 'var(--accent)' : 'var(--tx3)';
            });
          });
        };
      });
      Array.prototype.forEach.call(det.querySelectorAll('button[data-klt]'), function (btn) {
        btn.onclick = function () {
          curPeriod = btn.dataset.klt;
          var box = document.getElementById('klineChartBox');
          if (box) box.innerHTML = '<span class="fl" style="font-size:9px">Loading...</span>';
          fetchKline(code, 60, curPeriod, curAdjust).then(function (nb) {
            if (box) box.innerHTML = renderKlineChart(nb);
            bindKlineHover(nb);
            Array.prototype.forEach.call(det.querySelectorAll('button[data-klt]'), function (b2) {
              var on2 = b2.dataset.klt === curPeriod;
              b2.style.border = '1px solid ' + (on2 ? 'var(--accent)' : 'var(--border)');
              b2.style.background = on2 ? 'rgba(137,87,229,.2)' : 'transparent';
              b2.style.color = on2 ? 'var(--accent)' : 'var(--tx3)';
            });
          });
        };
      });
    });
  }

  window.StockDetail = {
    render: render,
    fetchKline: fetchKline,
    fetchFundFlow: fetchFundFlow,
    fetchMinute: fetchMinute,
    fetchQuote: fetchQuote,
    renderKlineChart: renderKlineChart,
    bindKlineHover: bindKlineHover
  };
})();
