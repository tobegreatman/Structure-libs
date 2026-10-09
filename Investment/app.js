/*! Investment OS · app.js —— 二期渐进增强：决策台 + 实验室
    设计原则：无 JS 时页面内容完整可读；本脚本只做增强，不承载内容。
    结构：工具 → 十问检查台 → 决策日志(localStorage) → 实验室×5 */
(function () {
  'use strict';

  document.documentElement.classList.add('js');

  /* ============ 工具 ============ */
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) { return [].slice.call((ctx || document).querySelectorAll(sel)); }
  function on(el, evt, fn) { if (el) el.addEventListener(evt, fn); }
  function num(v, d) { return Number(v).toFixed(d == null ? 0 : d); }
  /* 金额格式化：单位统一为「万」 */
  function wan(v) {
    if (!isFinite(v)) return '—';
    if (Math.abs(v) >= 10000) {
      var y = v / 10000;
      return (y >= 100 ? Math.round(y).toLocaleString('zh-CN') : num(y, 1).replace(/\.0$/, '')) + ' 亿';
    }
    if (Math.abs(v) >= 100) return Math.round(v).toLocaleString('zh-CN') + ' 万';
    return num(v, 1).replace(/\.0$/, '') + ' 万';
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function todayStr() {
    var d = new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }
  /* localStorage 安全封装（隐私模式等场景降级为内存） */
  var store = (function () {
    try {
      var k = '__invos_probe__';
      window.localStorage.setItem(k, '1');
      window.localStorage.removeItem(k);
      return window.localStorage;
    } catch (e) { return null; }
  })();

  /* 轻提示 toast */
  var toastTimer = null;
  function flash(msg) {
    var t = $('#jr-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'jr-toast';
      t.className = 'jr-toast';
      t.setAttribute('role', 'status');
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('is-on'); }, 2400);
  }

  /* 滑杆填充进度（webkit 渐变轨道用） */
  function paintFill(input) {
    var min = parseFloat(input.min) || 0;
    var max = parseFloat(input.max) || 100;
    var v = parseFloat(input.value);
    var p = ((v - min) / (max - min)) * 100;
    input.style.setProperty('--fill', (isFinite(p) ? p : 50) + '%');
  }
  function bindPaint(root) {
    $all('input[type="range"]', root || document).forEach(paintFill);
  }

  /* ============ 一、检查台：买入十问 + 卖出五问（同构逻辑） ============ */
  function makeQuiz(cfg) {
    var list = $(cfg.list);
    if (!list) return;
    var countEl = $(cfg.count);
    var failBar = $(cfg.fail);
    var passBar = $(cfg.pass);
    var vetoTopic = $(cfg.vetoTopic);
    var N = cfg.n;

    function read() {
      var st = { pass: 0, vetoLi: null, vetoI: 0 };
      for (var i = 1; i <= N; i++) {
        var el = list.querySelector('input[name="' + cfg.prefix + i + '"]:checked');
        if (!el) continue;
        if (el.value === 'pass') st.pass++;
        else if (!st.vetoLi) { st.vetoLi = el.closest('.deck-q'); st.vetoI = i; }
      }
      return st;
    }
    function update() {
      var st = read();
      countEl.textContent = st.pass;
      $all('.deck-q', list).forEach(function (li) { li.classList.remove('is-veto'); });
      if (st.vetoLi) {
        var topic = st.vetoLi.getAttribute('data-topic') || '这一问';
        vetoTopic.textContent = '第 ' + st.vetoI + ' 问 · ' + topic + ' —— 否决';
        st.vetoLi.classList.add('is-veto');
        failBar.classList.add('is-on');
        passBar.classList.remove('is-on');
      } else {
        failBar.classList.remove('is-on');
        passBar.classList.toggle('is-on', st.pass === N);
      }
    }
    on(list, 'change', update);
  }
  makeQuiz({ list: '#deck-list', count: '#deck-pass-count', fail: '#deck-verdict-fail', pass: '#deck-verdict-pass', vetoTopic: '#deck-veto-topic', n: 10, prefix: 'dq' });
  makeQuiz({ list: '#sell-list', count: '#sell-pass-count', fail: '#sell-verdict-fail', pass: '#sell-verdict-pass', vetoTopic: '#sell-veto-topic', n: 5, prefix: 'sq' });

  /* ============ 二、决策日志 · localStorage ============ */
  (function journal() {
    var form = $('#jr-form');
    if (!form) return;
    var KEY = 'invos_journal';
    var tbody = $('#jr-body');
    var emptyEl = $('#jr-empty');
    var statsEl = $('#jr-stats');

    var entries;
    try { entries = JSON.parse((store && store.getItem(KEY)) || '[]'); }
    catch (e) { entries = []; }
    if (!Array.isArray(entries)) entries = [];

    function save() {
      if (!store) return;
      try { store.setItem(KEY, JSON.stringify(entries)); } catch (e) { /* 空间满等异常静默 */ }
    }
    function esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    }
    function moodLabel(m) {
      return { calm: '平静', excited: '兴奋', anxious: '焦虑' }[m] || m || '—';
    }

    function statsHtml() {
      if (!entries.length) return '<span>日志为空 —— 买入键之前，先写这一笔。</span>';
      var open = 0, closed = 0, win = 0, emo = 0, scored = 0;
      entries.forEach(function (e) {
        if (e.status === 'open') open++;
        else if (e.status === 'closed') {
          closed++;
          if (typeof e.pnl === 'number' && isFinite(e.pnl)) {
            scored++;
            if (e.pnl > 0) win++;
          }
        }
        if (e.mood && e.mood !== 'calm') emo++;
      });
      var h = '<span>共 <b>' + entries.length + '</b> 笔</span>' +
              '<span>持有中 <b>' + open + '</b></span>' +
              '<span>已平仓 <b>' + closed + '</b></span>';
      if (scored) h += '<span>胜率 <b>' + Math.round(win / scored * 100) + '%</b></span>';
      if (emo) h += '<span>情绪波动时决策 <b>' + emo + '</b> 笔</span>';
      if (!store) h = '<span class="jr-warn">当前浏览器无法持久保存（可能是隐私模式），数据仅本次有效</span>' + h;
      return h;
    }
    function rowHtml(e) {
      var status;
      if (e.status === 'closed') {
        if (typeof e.pnl === 'number' && isFinite(e.pnl)) {
          var cls = e.pnl > 0 ? 'jr-win' : 'jr-loss';
          var sign = e.pnl > 0 ? '+' : '';
          status = '<span class="jr-tag jr-tag--closed">已平仓</span> <b class="' + cls + '">' + sign + num(e.pnl * 100, 1) + '%</b>';
        } else {
          status = '<span class="jr-tag jr-tag--closed">已平仓</span>';
        }
      } else {
        status = '<span class="jr-tag">持有中</span>';
      }
      return '<tr data-id="' + esc(e.id) + '">' +
        '<td>' + esc(e.date) + '</td>' +
        '<td><b>' + esc(e.ticker) + '</b>' + (e.price ? '<br><span class="jr-sub">' + esc(e.price) + ' 元</span>' : '') + '</td>' +
        '<td class="jr-thesis">' + esc(e.thesis) + '</td>' +
        '<td>' + esc(e.size) + '%</td>' +
        '<td class="jr-exit">' + esc(e.exit) + '</td>' +
        '<td>' + moodLabel(e.mood) + '</td>' +
        '<td>' + status + '</td>' +
        '<td class="jr-ops">' +
          (e.status === 'open'
            ? '<button type="button" class="jr-btn" data-act="close" data-id="' + esc(e.id) + '">平仓</button>'
            : '') +
          '<button type="button" class="jr-btn jr-btn--danger" data-act="del" data-id="' + esc(e.id) + '">删除</button>' +
        '</td></tr>';
    }
    function render() {
      tbody.innerHTML = entries.map(rowHtml).join('');
      emptyEl.hidden = entries.length > 0;
      statsEl.innerHTML = statsHtml();
    }

    /* 表单日期默认今天 */
    var dateInput = form.querySelector('[name="date"]');
    if (dateInput && !dateInput.value) dateInput.value = todayStr();

    on(form, 'submit', function (ev) {
      ev.preventDefault();
      var f = ev.target;
      var e = {
        id: 'e' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
        date: f.date.value || todayStr(),
        ticker: f.ticker.value.trim(),
        price: f.price.value,
        size: f.size.value,
        thesis: f.thesis.value.trim(),
        exit: f.exit.value.trim(),
        mood: f.mood.value,
        status: 'open',
        closePrice: null,
        pnl: null
      };
      entries.unshift(e);
      save();
      render();
      f.reset();
      dateInput.value = todayStr();
      flash('已写入日志：' + e.ticker);
    });

    /* 行内操作（事件委托）：删除 / 平仓 / 平仓确认 */
    on(tbody, 'click', function (ev) {
      var btn = ev.target.closest('.jr-btn');
      if (!btn) return;
      var id = btn.getAttribute('data-id');
      var act = btn.getAttribute('data-act');
      var entry = null;
      entries.forEach(function (x) { if (x.id === id) entry = x; });
      if (!entry) return;

      if (act === 'del') {
        if (window.confirm('删除「' + entry.ticker + '」这条日志？')) {
          entries = entries.filter(function (x) { return x.id !== id; });
          save();
          render();
          flash('已删除');
        }
      } else if (act === 'close') {
        var tr = tbody.querySelector('tr[data-id="' + id + '"]');
        if (!tr || tr.nextElementSibling && tr.nextElementSibling.classList.contains('jr-close-row')) return;
        var row = document.createElement('tr');
        row.className = 'jr-close-row';
        row.innerHTML = '<td colspan="8">卖出价 ' +
          '<input class="jr-close-price" type="number" min="0" step="any" placeholder="' + (entry.price || '') + '">' +
          '<button type="button" class="jr-btn" data-act="close-ok" data-id="' + id + '">确认平仓</button>' +
          '<button type="button" class="jr-btn" data-act="close-cancel">取消</button></td>';
        tr.after(row);
        row.querySelector('input').focus();
      } else if (act === 'close-ok') {
        var input = btn.closest('tr').querySelector('.jr-close-price');
        var cp = parseFloat(input.value);
        if (!isFinite(cp) || cp <= 0) { input.focus(); return; }
        entry.status = 'closed';
        entry.closePrice = cp;
        var bp = parseFloat(entry.price);
        entry.pnl = isFinite(bp) && bp > 0 ? (cp - bp) / bp : null;
        save();
        render();
        flash('已平仓：' + entry.ticker + (entry.pnl != null ? '（' + (entry.pnl > 0 ? '+' : '') + num(entry.pnl * 100, 1) + '%）' : ''));
      } else if (act === 'close-cancel') {
        render();
      }
    });

    /* 导出 / 清空 */
    on($('#jr-export'), 'click', function () {
      if (!entries.length) { flash('日志为空，无内容可导出'); return; }
      var blob = new Blob([JSON.stringify(entries, null, 2)], { type: 'application/json' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'investment-os-journal-' + todayStr() + '.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1200);
    });
    on($('#jr-clear'), 'click', function () {
      if (!entries.length) return;
      if (window.confirm('清空全部 ' + entries.length + ' 条日志？此操作不可恢复（建议先导出备份）。')) {
        entries = [];
        save();
        render();
        flash('日志已清空');
      }
    });

    /* 导入 JSON：按 id 去重合并，导入后按日期倒序 */
    var importFile = $('#jr-import-file');
    on($('#jr-import'), 'click', function () {
      if (!importFile) return;
      importFile.value = ''; /* 允许重复选择同一文件 */
      importFile.click();
    });
    on(importFile, 'change', function () {
      var f = importFile.files && importFile.files[0];
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        var data;
        try { data = JSON.parse(reader.result); }
        catch (e) { flash('导入失败：文件不是有效的 JSON'); return; }
        if (!Array.isArray(data)) { flash('导入失败：备份应为日志数组'); return; }
        var ids = {};
        entries.forEach(function (e) { ids[e.id] = 1; });
        var added = 0;
        data.forEach(function (e) {
          if (e && typeof e === 'object' && e.id && e.ticker && !ids[e.id]) {
            ids[e.id] = 1;
            entries.push(e);
            added++;
          }
        });
        if (!added) { flash('没有新日志：备份里的条目都已存在'); return; }
        entries.sort(function (a, b) { return String(b.date || '').localeCompare(String(a.date || '')); });
        save();
        render();
        flash('已导入 ' + added + ' 条（重复 ' + (data.length - added) + ' 条已跳过）');
      };
      reader.readAsText(f);
    });

    render();
  })();

  /* ============ 三、实验室 ============ */

  /* 01 复利实验室 */
  (function compound() {
    var p = $('#cp-p');
    if (!p) return;
    function calc() {
      var P = +p.value, r = +$('#cp-r').value / 100, n = +$('#cp-n').value;
      $('#cp-p-out').textContent = P + ' 万元';
      $('#cp-r-out').textContent = num(+$('#cp-r').value, 1).replace(/\.0$/, '') + '%';
      $('#cp-n-out').textContent = n + ' 年';
      var fin = P * Math.pow(1 + r, n);
      var simple = P * (1 + r * n);
      $('#cp-final').textContent = wan(fin);
      $('#cp-simple').textContent = wan(simple);
      $('#cp-x').textContent = '× ' + num(Math.pow(1 + r, n), 1);
      $('#cp-double').textContent = num(72 / (+$('#cp-r').value), 1) + ' 年';
    }
    ['cp-p', 'cp-r', 'cp-n'].forEach(function (id) {
      on($('#' + id), 'input', calc);
    });
    calc();
  })();

  /* 02 回撤感受器 */
  (function drawdown() {
    var a = $('#dd-a');
    if (!a) return;
    function calc() {
      var A = +a.value;
      $('#dd-a-out').textContent = A + ' 万元';
      $('#dd-10').textContent = wan(A * 0.9);
      $('#dd-30').textContent = wan(A * 0.7);
      $('#dd-50').textContent = wan(A * 0.5);
    }
    on(a, 'input', calc);
    calc();
  })();

  /* 03 估值沙盒 */
  (function sandbox() {
    var eps = $('#vs-eps');
    if (!eps) return;
    var DISC = { 50: '五折', 60: '六折', 70: '七折', 80: '八折', 90: '九折' };
    function calc() {
      var E = +eps.value, pe = +$('#vs-pe').value, disc = +$('#vs-disc').value / 100;
      var price = parseFloat($('#vs-price').value);
      $('#vs-eps-out').textContent = num(E, 1) + ' 元';
      $('#vs-pe-out').textContent = num(pe, 1) + ' 倍';
      var dRaw = +$('#vs-disc').value;
      $('#vs-disc-out').textContent = (DISC[dRaw] || dRaw + '%') + '（' + dRaw + '%）';
      var iv = E * pe, buy = iv * disc;
      $('#vs-iv').textContent = num(iv, 2);
      $('#vs-buy').textContent = num(buy, 2) + ' 元';
      var v = $('#vs-verdict');
      if (isFinite(price) && price > 0) {
        if (price > buy) {
          v.textContent = num(price, 2) + ' 元 > 买价 → 等待';
          v.className = 'lab-verdict lab-verdict--wait';
        } else {
          v.textContent = num(price, 2) + ' 元 ≤ 买价 → 进入买入区';
          v.className = 'lab-verdict lab-verdict--buy';
        }
      } else {
        v.textContent = '输入现价对照';
        v.className = 'lab-verdict';
      }
    }
    ['vs-eps', 'vs-pe', 'vs-disc', 'vs-price'].forEach(function (id) {
      on($('#' + id), 'input', calc);
    });
    calc();
  })();

  /* 04 再平衡计算器 */
  (function rebalance() {
    var t = $('#rb-t');
    if (!t) return;
    function calc() {
      var T = +t.value / 100;
      var s = parseFloat($('#rb-s').value) || 0;
      var b = parseFloat($('#rb-b').value) || 0;
      var total = s + b;
      $('#rb-t-out').textContent = Math.round(T * 100) + '%';
      var curEl = $('#rb-cur'), devEl = $('#rb-dev'), actEl = $('#rb-action');
      if (total <= 0) {
        curEl.textContent = '—';
        devEl.textContent = '—';
        devEl.className = '';
        actEl.textContent = '输入股票与债券现值';
        return;
      }
      var cur = s / total;
      var dev = cur - T;
      curEl.textContent = Math.round(cur * 100) + '%';
      devEl.textContent = (dev >= 0 ? '+' : '−') + Math.round(Math.abs(dev) * 100) + '%';
      devEl.className = Math.abs(dev) >= 0.05 ? 'dev-hot' : '';
      if (Math.abs(dev) < 0.005) {
        actEl.textContent = '比例在目标线上 —— 不动，是最好的动作';
      } else {
        var move = s - T * total; /* 正 = 卖出股票 */
        var hot = Math.abs(dev) >= 0.05;
        if (move > 0) {
          actEl.innerHTML = '卖出股票 <b>' + wan(move) + '</b> → 买入债券' + (hot ? '' : '（偏离 &lt; 5%，也可等年度窗口）');
        } else {
          actEl.innerHTML = '卖出债券 <b>' + wan(-move) + '</b> → 买入股票' + (hot ? '' : '（偏离 &lt; 5%，也可等年度窗口）');
        }
      }
    }
    ['rb-t', 'rb-s', 'rb-b'].forEach(function (id) {
      on($('#' + id), 'input', calc);
    });
    calc();
  })();

  /* 05 蒙特卡洛退休模拟 */
  (function monteCarlo() {
    var svg = $('#mc-svg');
    var rateEl = $('#mc-rate');
    if (!svg || !rateEl) return;

    var PATHS = 500, SHOW = 60, RET = 0.07, VOL = 0.15;
    var W = 560, H = 300, L = 46, R = 14, T = 16, B = 34;

    /* Box–Muller 高斯随机数（带缓存） */
    var gauss = (function () {
      var spare = null;
      return function () {
        if (spare !== null) { var s = spare; spare = null; return s; }
        var u, v, s2;
        do {
          u = Math.random() * 2 - 1;
          v = Math.random() * 2 - 1;
          s2 = u * u + v * v;
        } while (s2 >= 1 || s2 === 0);
        var mul = Math.sqrt(-2 * Math.log(s2) / s2);
        spare = v * mul;
        return u * mul;
      };
    })();

    function simulate(p, w, n) {
      var paths = [];
      for (var i = 0; i < PATHS; i++) {
        var v = p, dead = false;
        var arr = [p];
        for (var y = 1; y <= n; y++) {
          if (!dead) {
            v *= (1 + RET + VOL * gauss());
            v -= w;
            if (v <= 0) { v = 0; dead = true; }
          }
          arr.push(v);
        }
        paths.push({ series: arr, end: v, dead: dead });
      }
      return paths;
    }

    function draw(p, w, n, paths) {
      var byEnd = paths.slice().sort(function (a, b) { return a.end - b.end; });
      var cap = p * 2.2;
      for (var i = 0; i < byEnd.length; i++) {
        var m = Math.max.apply(null, byEnd[i].series);
        if (m > cap) cap = Math.min(m, p * 4);
        if (i > PATHS * 0.7) break; /* 只看头部路径，避免极端值把图压扁 */
      }
      function X(i) { return (L + (W - L - R) * (i / n)).toFixed(1); }
      function Y(v) {
        var c = Math.min(v, cap);
        return (T + (H - T - B) * (1 - c / cap)).toFixed(1);
      }
      var s = '';
      /* 轴与参考线 */
      s += '<line x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - R) + '" y2="' + (H - B) + '" class="mc-axis"/>';
      s += '<line x1="' + L + '" y1="' + Y(p) + '" x2="' + (W - R) + '" y2="' + Y(p) + '" class="mc-base"/>';
      /* 抽样路径（均匀取样） */
      for (var k = 0; k < SHOW; k++) {
        var path = paths[Math.floor(k * PATHS / SHOW)];
        if (!path) continue;
        var d = '';
        for (var j = 0; j < path.series.length; j++) {
          d += (j === 0 ? 'M' : 'L') + X(j) + ',' + Y(path.series[j]);
        }
        s += '<path class="mc-path' + (path.dead ? ' mc-path--dead' : '') + '" d="' + d + '"/>';
      }
      /* 中位数路径（加粗） */
      var medPath = byEnd[Math.floor(PATHS / 2)];
      if (medPath) {
        var md = '';
        for (var j2 = 0; j2 < medPath.series.length; j2++) {
          md += (j2 === 0 ? 'M' : 'L') + X(j2) + ',' + Y(medPath.series[j2]);
        }
        s += '<path class="mc-path mc-path--med" d="' + md + '"/>';
      }
      /* 标签 */
      s += '<text x="' + (L + 4) + '" y="' + (Y(p) - 6) + '" class="mc-label">期初 ' + wan(p) + '</text>';
      s += '<text x="' + L + '" y="' + (H - 12) + '" class="mc-label">0</text>';
      s += '<text x="' + (W - R) + '" y="' + (H - 12) + '" class="mc-label" text-anchor="end">第 ' + n + ' 年</text>';
      s += '<text x="' + (L + 4) + '" y="' + (T + 10) + '" class="mc-label">纵轴上限 ' + wan(cap) + '</text>';
      svg.innerHTML = s;
    }

    function run() {
      var p = +$('#mc-p').value, w = +$('#mc-w').value, n = +$('#mc-n').value;
      $('#mc-p-out').textContent = p + ' 万元';
      $('#mc-w-out').textContent = w + ' 万元（首年 ' + num(w / p * 100, 1) + '%）';
      $('#mc-n-out').textContent = n + ' 年';

      var paths = simulate(p, w, n);
      var alive = paths.filter(function (x) { return x.end > 0; });
      var rate = alive.length / PATHS;

      var byEnd = paths.slice().sort(function (a, b) { return a.end - b.end; });
      var p5 = byEnd[Math.max(0, Math.floor(PATHS * 0.05) - 1)];
      var med = byEnd[Math.floor(PATHS / 2)];
      var best = byEnd[PATHS - 1];

      rateEl.textContent = Math.round(rate * 100) + '%';
      rateEl.className = rate >= 0.85 ? 'mc-rate--ok' : (rate >= 0.6 ? 'mc-rate--warn' : 'mc-rate--bad');
      $('#mc-p10').textContent = p5 ? wan(p5.end) : '—';
      $('#mc-med').textContent = med ? wan(med.end) : '—';
      $('#mc-best').textContent = best ? wan(best.end) : '—';

      draw(p, w, n, paths);
    }

    ['mc-p', 'mc-w', 'mc-n'].forEach(function (id) {
      on($('#' + id), 'input', run);
    });
    on($('#mc-run'), 'click', function () {
      run();
      flash('已重新抽取 500 次命运');
    });
    run();
  })();

  /* ============ 四、训练场 ============ */

  /* 01 偏差自测：理性刻度 */
  (function biasQuiz() {
    var list = $('#bias-list');
    if (!list) return;
    var countEl = $('#bias-count'), fillEl = $('#bias-fill'), verdictEl = $('#bias-verdict');
    function calc() {
      var checked = $all('input:checked', list);
      var r = 0;
      var biases = [];
      checked.forEach(function (el) {
        if (el.value === 'r') r++;
        else if (el.dataset.bias) biases.push(el.dataset.bias);
      });
      var n = checked.length;
      countEl.textContent = r + ' / 6';
      fillEl.style.width = (r / 6 * 100) + '%';
      if (n === 0) {
        verdictEl.textContent = '每答一题更新一次 —— 盘中的你，未必是答题的你。';
      } else if (n < 6) {
        verdictEl.textContent = '已答 ' + n + ' 题 · 理性 ' + r + ' —— 选完六题见分晓。';
      } else if (r === 6) {
        verdictEl.textContent = '6 / 6 全理性 —— 但请记住：测试是冷静的，盘中的你不是。预案与日志，就是为那个时刻准备的。';
      } else if (r >= 4) {
        verdictEl.textContent = '理性 ' + r + ' / 6 —— 主要偏差：' + biases.slice(0, 2).join('、') + '。把对应解毒剂写进《投资章程》。';
      } else {
        verdictEl.textContent = '理性 ' + r + ' / 6 —— 情绪主导型。先回认知层的「行为偏差解毒剂表」，再来决策台重练十问。';
      }
    }
    on(list, 'change', calc);
    calc();
  })();

  /* 02 翻转卡：标记复习（localStorage，隐私模式自动降级为内存模式） */
  (function flashcards() {
    var grid = $('#flip-grid');
    if (!grid) return;
    var KEY = 'invos_review';
    function read() {
      try { return JSON.parse(store.getItem(KEY) || '[]'); } catch (e) { return []; }
    }
    function save(arr) {
      try { store.setItem(KEY, JSON.stringify(arr)); } catch (e) { /* 降级：仅本次会话有效 */ }
    }
    var marks = read();
    function paint() {
      $all('.flip', grid).forEach(function (card) {
        var marked = marks.indexOf(card.id) > -1;
        card.classList.toggle('is-marked', marked);
        var btn = card.querySelector('.flip-mark');
        if (btn) btn.textContent = marked ? '已标记 ✓' : '标记复习';
      });
      var c = $('#flip-count');
      if (c) c.textContent = marks.length;
    }
    on(grid, 'click', function (e) {
      var btn = e.target.closest ? e.target.closest('.flip-mark') : null;
      if (!btn) return;
      e.preventDefault(); /* 阻止 label 默认翻面 —— 这次点击是"标记"，不是"翻卡" */
      var id = btn.getAttribute('data-card');
      var i = marks.indexOf(id);
      if (i > -1) {
        marks.splice(i, 1);
        flash('已从复习清单移除');
      } else {
        marks.push(id);
        flash('已加入复习清单 —— 数据只存本机');
      }
      save(marks);
      paint();
    });
    paint();
  })();

  /* ============ 五、每日一页 · 打印本周行动卡 ============ */
  (function printWeek() {
    var btn = $('#print-week-btn');
    if (!btn) return;
    on(btn, 'click', function () {
      /* 清理可能的历史残留，再进入行动卡打印模式（默认打印仍是《投资章程》） */
      document.body.classList.remove('print-week');
      document.body.classList.add('print-week');
      function clean() {
        document.body.classList.remove('print-week');
        window.removeEventListener('afterprint', clean);
      }
      window.addEventListener('afterprint', clean);
      window.print();
    });
  })();

  /* ============ 初始化：滑杆填充 ============ */
  bindPaint(document);
})();
