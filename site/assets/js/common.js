/* ============================================================
   ETF 轮动监控 · 全站公共脚本
   导航 / 页脚注入、主题持久化、数据加载、状态胶囊、刷新与盘中轮询
   ============================================================ */
'use strict';

const Site = (() => {

  // ---------- 常量 ----------
  const NAV_ITEMS = [
    { href: 'index.html',     key: 'home',      label: '首页' },
    { href: 'dashboard.html', key: 'dashboard', label: '监控仪表盘' },
    { href: 'trades.html',    key: 'trades',    label: '交易明细' },
    { href: 'strategy.html',  key: 'strategy',  label: '策略研究' },
    { href: 'about.html',     key: 'about',     label: '关于' },
  ];

  // GitHub Pages / Cloudflare Pages 等静态托管没有 /api/update
  const STATIC_HOST = /(^|\.)(github\.io|pages\.dev|workers\.dev|netlify\.app|vercel\.app)$/.test(location.hostname);

  const THEME_KEY = 'etf-site-theme';

  // ---------- 主题 ----------
  function currentTheme() {
    return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    const btn = document.getElementById('themeBtn');
    if (btn) btn.textContent = t === 'light' ? '🌙' : '☀️';
  }
  function initTheme() {
    let t = 'dark';
    try { t = localStorage.getItem(THEME_KEY) || 'dark'; } catch (e) {}
    document.documentElement.dataset.theme = t;
  }
  initTheme();   // 尽早执行，避免闪烁

  function chartTheme() {
    const light = currentTheme() === 'light';
    if (light) return {
      axisText: '#8a94a6', axisLine: '#d9dde5', splitLine: '#eceff4',
      cross: '#9aa3b3', tooltipBg: '#ffffff', tooltipBorder: '#d9dde5',
      avg: '#222222', etf1000: '#d43f3a', etfdiv: '#2ca02c', etf2000e: '#e8940a',
      dif: '#222222', dea: '#5b8ff9',
      buy: '#d43f3a', sell: '#2ca02c',
      area: 'rgba(212,63,58,0.12)', areaDiv: 'rgba(44,160,44,0.09)',
      zoomFill: 'rgba(59,109,240,0.16)', zoomHandle: '#3b6df0', text: '#1d2733',
      muted: '#667080'
    };
    return {
      axisText: '#7d8798', axisLine: '#2c3850', splitLine: '#232e44',
      cross: '#8b95a8', tooltipBg: '#1a2233', tooltipBorder: '#2c3a55',
      avg: '#e9edf4', etf1000: '#ff6b6b', etfdiv: '#3ddc97', etf2000e: '#f5a623',
      dif: '#c9d4e8', dea: '#fca311',
      buy: '#ff6b6b', sell: '#3ddc97',
      area: 'rgba(255,107,107,0.13)', areaDiv: 'rgba(61,220,151,0.09)',
      zoomFill: 'rgba(77,124,254,0.18)', zoomHandle: '#4d7cfe', text: '#e8ecf3',
      muted: '#8b95a8'
    };
  }

  // ---------- 布局注入 ----------
  const LOGO_SVG = `
    <svg class="logo" viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#4d7cfe"/><stop offset="1" stop-color="#7a5cff"/>
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="10" fill="url(#lg)"/>
      <path d="M9 27 L17 18 L23 22 L31 12" stroke="#fff" stroke-width="2.6" fill="none"
            stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="31" cy="12" r="2.6" fill="#fff"/>
    </svg>`;

  function injectLayout(pageKey, opts) {
    opts = opts || {};
    const navHtml = NAV_ITEMS.map(n =>
      `<a href="${n.href}" class="${n.key === pageKey ? 'on' : ''}">${n.label}</a>`).join('');

    const header = document.createElement('header');
    header.className = 'topbar';
    header.innerHTML = `
      <a class="brand" href="index.html">
        ${LOGO_SVG}
        <div>
          <h1>ETF 轮动监控</h1>
          <div class="sub">平均股价(880003) · 中证2000增强 ⇄ 红利低波 · 方案C 慢速DIF(17/34)</div>
        </div>
      </a>
      <nav class="mainnav" id="mainnav">${navHtml}</nav>
      <div class="topbar-right">
        ${opts.statusPill === false ? '' : '<span class="status-pill" id="status">加载中…</span>'}
        ${opts.live ? '<span class="live-badge" id="liveBadge"></span>' : ''}
        <span class="updated" id="updated"></span>
        ${opts.refresh ? '<button class="btn" id="refreshBtn" title="刷新数据">⟳</button>' : ''}
        <button class="btn" id="themeBtn" title="切换主题">🌓</button>
        <button class="btn nav-toggle" id="navToggle" title="菜单">☰</button>
      </div>`;
    document.body.prepend(header);

    const footer = document.createElement('footer');
    footer.className = 'footer';
    footer.innerHTML = `
      <div class="footer-inner">
        <div>
          <div class="fbrand">ETF 轮动监控</div>
          <div class="fmeta" style="margin-top:6px">基于通达信平均股价指数(880003) MACD-DIF 拐点信号的量化轮动研究</div>
        </div>
        <div class="flinks">
          ${NAV_ITEMS.map(n => `<a href="${n.href}">${n.label}</a>`).join('')}
        </div>
        <div class="fmeta" id="footerUpdated"></div>
        <div class="fdisc">⚠️ 免责声明：本站仅为个人量化研究原型，全部内容（含回测结果、信号提示、持仓记录）不构成任何投资建议，勿直接用于实盘。市场有风险，投资需谨慎。</div>
      </div>`;
    document.body.appendChild(footer);

    // 主题按钮
    document.getElementById('themeBtn').addEventListener('click', () => {
      applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
      document.dispatchEvent(new CustomEvent('site:themechange'));
    });
    applyTheme(currentTheme());

    // 移动端菜单
    const toggle = document.getElementById('navToggle');
    const nav = document.getElementById('mainnav');
    toggle.addEventListener('click', () => nav.classList.toggle('open'));
    nav.addEventListener('click', e => { if (e.target.tagName === 'A') nav.classList.remove('open'); });
  }

  // ---------- 数据加载（全站共享一份 Promise） ----------
  let dataPromise = null;
  let lastData = null;

  function loadData(force) {
    if (dataPromise && !force) return dataPromise;
    dataPromise = fetch('data.json?_=' + Date.now()).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(data => {
      lastData = data;
      renderStatusPill(data);
      const up = document.getElementById('updated');
      if (up) {
        up.textContent = '数据更新: ' + data.updated + (data.avg_stale ? '（平均股价陈旧: ' + (data.avg_date || '?') + '）' : '');
        up.style.color = data.avg_stale ? '#e5484d' : '';
      }
      const fu = document.getElementById('footerUpdated');
      if (fu) fu.textContent = '数据截至 ' + (data.dates ? data.dates[data.dates.length - 1] : '-') + ' · 导出 ' + (data.updated || '-');
      return data;
    });
    return dataPromise;
  }

  // ---------- 顶栏状态胶囊（全站通用） ----------
  function renderStatusPill(data) {
    const el = document.getElementById('status');
    if (!el || !data || !data.legs) return;
    const last = data.legs[data.legs.length - 1];
    let hold = '';
    if (last && last.open) {
      const is1000 = last.asset === '1000';
      const name = last.asset_name || (is1000 ? '中证1000ETF' : '红利低波ETF');
      const pos = last.ret >= 0;
      hold = `<span class="dot ${is1000 ? 'r' : 'g'}"></span><b>${name}</b> ${pos ? '▲' : '▼'}<span class="${pos ? 'up' : 'down'}">${(last.ret * 100).toFixed(2)}%</span>`;
    } else {
      hold = '空仓';
    }
    const N = data.dates.length - 1;
    const avgNow = data.avg[N];
    const lastDIF = data.dif[N], prevDIF = data.dif[N - 5] || data.dif[0];
    const difDir = lastDIF > prevDIF ? '↑' : lastDIF < prevDIF ? '↓' : '→';
    el.innerHTML = `${hold}　|　均价 ${avgNow != null ? avgNow.toFixed(2) : '-'}　|　DIF ${lastDIF != null ? lastDIF.toFixed(4) : '-'}${difDir}`;
  }

  // ---------- 刷新（本地 /api/update，静态站退化为重载） ----------
  let refreshing = false;

  function watchUpdate(t0, el) {
    return new Promise(resolve => {
      const iv = setInterval(() => {
        fetch('/api/status?_=' + Date.now()).then(r => r.json()).then(st => {
          const p = st.progress || {};
          const secs = Math.round((Date.now() - t0) / 1000);
          if (st.running) {
            el.textContent = `正在更新行情… ${p.current ? '拉取 ' + p.current + ' ' : ''}`
              + `(${p.done || 0}/${p.total || 9}) ${secs}s`;
          } else {
            const last = st.last || {};
            if (last.ok === undefined && Date.now() - t0 < 5000) return;
            clearInterval(iv);
            if (last.ok === false) el.textContent = '更新失败: ' + (last.error || '未知错误');
            resolve(last);
          }
        }).catch(() => {});
      }, 500);
      setTimeout(() => { clearInterval(iv); resolve({ ok: true }); }, 180000);
    });
  }

  function bindRefresh(afterReload) {
    const btn = document.getElementById('refreshBtn');
    if (!btn) return;
    btn.addEventListener('click', () => {
      if (refreshing) return;
      refreshing = true;
      const updated = document.getElementById('updated');
      btn.style.transform = 'rotate(180deg)';
      btn.style.opacity = '0.5';
      const done = () => {
        refreshing = false;
        btn.style.opacity = '';
        setTimeout(() => btn.style.transform = '', 500);
      };
      if (STATIC_HOST) {
        updated.textContent = '正在重新加载…';
        loadData(true).then(d => {
          updated.textContent = '数据更新: ' + d.updated + '（静态站每日 15:10 同步）';
          if (afterReload) afterReload(d);
        }).finally(done);
        return;
      }
      const t0 = Date.now();
      updated.textContent = '正在更新行情…';
      fetch('/api/update?force=1').then(r => r.json()).then(res => {
        if (!res.ok) throw new Error(res.error || '未知错误');
        return watchUpdate(t0, updated);
      }).then(() => loadData(true)).then(d => {
        if (afterReload) afterReload(d);
      }).catch(e => {
        updated.textContent = '更新接口不可用，已重新加载';
        loadData(true).then(d => { if (afterReload) afterReload(d); });
        console.error(e);
      }).finally(done);
    });
  }

  // 页面打开时后台静默更新（仅本地服务）
  function autoUpdate(afterReload) {
    if (STATIC_HOST) {
      // 静态站：每 5 分钟自动重载一次已发布数据
      setInterval(() => {
        if (!document.hidden) loadData(true).then(d => { if (afterReload) afterReload(d); });
      }, 5 * 60 * 1000);
      return;
    }
    fetch('/api/update').then(r => r.json()).then(res => {
      if (res.ok && res.started) {
        const up = document.getElementById('updated');
        watchUpdate(Date.now(), up || { textContent: '' })
          .then(() => loadData(true))
          .then(d => { if (afterReload) afterReload(d); });
      } else if (res.ok && !res.skipped) {
        loadData(true).then(d => { if (afterReload) afterReload(d); });
      }
      if (!res.ok) console.error('数据更新失败: ' + (res.error || '未知错误'));
    }).catch(e => console.error('更新接口不可用: ' + e));
  }

  // ---------- 盘中实时轮询（仅仪表盘） ----------
  function pollLive(onLive) {
    if (STATIC_HOST) return;
    const poll = () => {
      fetch('live.json?_=' + Date.now()).then(r => r.ok ? r.json() : null).then(d => {
        if (d) onLive(d);
      }).catch(() => {});
    };
    poll();
    setInterval(() => { if (!document.hidden && !refreshing) poll(); }, 5000);
  }

  // ---------- 格式化工具 ----------
  const fmt = {
    pct: (v, dp) => v == null ? '-' : (v >= 0 ? '+' : '') + (v * 100).toFixed(dp == null ? 2 : dp) + '%',
    pct0: (v, dp) => v == null ? '-' : (v * 100).toFixed(dp == null ? 2 : dp) + '%',
    num: (v, dp) => v == null ? '-' : Number(v).toFixed(dp == null ? 3 : dp),
    cls: v => v < 0 ? 'neg' : 'pos',
  };

  // ---------- 统计计算（全站复用） ----------
  function calcStats(data) {
    const nav = data.nav_strat, N = nav.length - 1;
    const finalNAV = nav[N];
    const tradingDays = data.dates.length;
    const years = tradingDays / 252;
    const cumRet = finalNAV - 1;
    const annRet = Math.pow(finalNAV, 1 / years) - 1;

    let p = nav[0], mdd = 0, pDate = 0, uw = 0, uwMax = 0;
    for (let i = 1; i <= N; i++) {
      if (nav[i] > p) { p = nav[i]; pDate = i; uw = 0; }
      else { uw++; if (uw > uwMax) uwMax = uw; }
      const d = nav[i] / p - 1;
      if (d < mdd) mdd = d;
    }

    let curPeak = nav[0], curIdx = 0;
    for (let i = 1; i <= N; i++) { if (nav[i] > curPeak) { curPeak = nav[i]; curIdx = i; } }
    const curDD = nav[N] / curPeak - 1;

    const year = data.dates[N].slice(0, 4);
    const iY = data.dates.findIndex(d => d.slice(0, 4) === year);
    const ytd = nav[N] / (iY > 0 ? nav[iY - 1] : nav[0]) - 1;

    const h = data.etf1000_hfq;
    const holdCum = h[N] / h[0] - 1;
    const holdAnn = Math.pow(1 + holdCum, 1 / years) - 1;

    return {
      N, finalNAV, tradingDays, years, cumRet, annRet, mdd, pDate, uwMax,
      curDD, curIdx, ytd, iY, holdCum, holdAnn,
      exAnn: annRet - holdAnn,
      calmar: annRet / Math.abs(mdd),
    };
  }

  return {
    STATIC_HOST, NAV_ITEMS,
    injectLayout, loadData, chartTheme, currentTheme,
    bindRefresh, autoUpdate, pollLive, renderStatusPill,
    fmt, calcStats,
    get lastData() { return lastData; },
    get refreshing() { return refreshing; },
  };
})();
