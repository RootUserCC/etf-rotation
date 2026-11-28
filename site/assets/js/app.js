/* ============================================================
   ETF 轮动监控终端 · 统一单页应用脚本
   涵盖：主题切换、锚点高亮、数据加载与刷新、盘中实时轮询、
   状态卡与 KPI、自定义区间、6大 ECharts 图表、热力表、台账筛选
   ============================================================ */
'use strict';

(() => {
  const STATIC_HOST = /(^|\.)(github\.io|pages\.dev|workers\.dev|netlify\.app|vercel\.app)$/.test(location.hostname);
  const THEME_KEY = 'etf-site-theme';
  const STRAT_KEY = 'etf-site-strategy';

  // 全局状态
  let lastData = null;
  let lastLive = null;
  let liveChart = null;
  let refreshing = false;
  let activeStratId = (() => {
    try { return localStorage.getItem(STRAT_KEY) || 'T1'; } catch (e) { return 'T1'; }
  })();

  function stratPalette() {
    const light = currentTheme() === 'light';
    return light
      ? { 'T1': '#4f46e5', 'T2+': '#d97706', 'T3': '#059669', 'T3+': '#9333ea' }
      : { 'T1': '#6366f1', 'T2+': '#f59e0b', 'T3': '#10b981', 'T3+': '#c084fc' };
  }

  function resolveStratId(data) {
    const order = (data && data.strategy_order) || [];
    if (order.includes(activeStratId)) return activeStratId;
    activeStratId = (data && data.default_strategy) || order[0] || 'T1';
    return activeStratId;
  }

  function activeView(data) {
    if (!data) return null;
    if (!data.strategies) return data;
    const sid = resolveStratId(data);
    const s = data.strategies[sid];
    if (!s) return data;
    return Object.assign({}, data, {
      strategy_id: s.id,
      strategy_name: s.name,
      strategy_short: s.short_name,
      strategy_badge: s.badge,
      exec_mode: s.exec_mode,
      offense_desc: s.offense_desc,
      defense_desc: s.defense_desc,
      exec_desc: s.exec_desc,
      nav_strat: s.nav_strat,
      hold_spans: s.hold_spans,
      trades: s.trades,
      legs: s.legs,
      last_signal: s.last_signal,
      strat_metrics: s.metrics,
    });
  }

  // 图表实例
  const charts = {
    market: null,
    nav: null,
    dd: null,
    roll: null,
    daily: null,
    dist: null,
  };

  // UI 控制状态
  let klineMode = false;
  let marketRangeDays = 250;
  let navRangeDays = 0;
  let navLogScale = false;
  let customMode = 'days';
  let ledgerMode = 'legs';
  let legFilter = 'all';
  let ledgerYear = 'all';

  // ---------- 工具函数 ----------
  const $ = id => document.getElementById(id);
  const fmtPct = (v, dp = 2) => v == null ? '--' : (v >= 0 ? '+' : '') + (v * 100).toFixed(dp) + '%';
  const fmtNum = (v, dp = 3) => v == null ? '--' : Number(v).toFixed(dp);

  function currentTheme() {
    return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  }

  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    const sun = $('themeIconSun');
    const moon = $('themeIconMoon');
    if (sun && moon) {
      sun.style.display = t === 'light' ? 'none' : '';
      moon.style.display = t === 'light' ? '' : 'none';
    }
    const btn = $('themeBtn');
    if (btn) btn.title = t === 'light' ? '切换到深色主题' : '切换到浅色主题';
  }

  function chartTheme() {
    const light = currentTheme() === 'light';
    if (light) {
      return {
        text: '#0b1220',
        muted: '#475569',
        axisText: '#64748b',
        axisLine: '#d5dde8',
        splitLine: '#edf1f7',
        cross: '#94a3b8',
        tooltipBg: 'rgba(255, 255, 255, 0.96)',
        tooltipBorder: '#d5dde8',
        avg: '#0b1220',
        etf1000: '#e11d48',
        etfdiv: '#059669',
        etf2000e: '#d97706',
        accent: '#4f46e5',
        accentFillTop: 'rgba(79, 70, 229, 0.16)',
        accentFillBot: 'rgba(79, 70, 229, 0.01)',
        dif: '#1e293b',
        dea: '#d97706',
        buy: '#e11d48',
        sell: '#059669',
        areaAtk: 'rgba(225, 29, 72, 0.075)',
        areaDiv: 'rgba(5, 150, 105, 0.065)',
        ddFill: 'rgba(5, 150, 105, 0.14)',
        zoomFill: 'rgba(79, 70, 229, 0.14)',
        zoomHandle: '#4f46e5',
      };
    }
    return {
      text: '#f3f6fc',
      muted: '#9aa7bd',
      axisText: '#73829c',
      axisLine: '#232e44',
      splitLine: 'rgba(148, 163, 184, 0.09)',
      cross: '#64748b',
      tooltipBg: 'rgba(13, 18, 30, 0.96)',
      tooltipBorder: '#28354e',
      avg: '#e2e8f0',
      etf1000: '#fb7185',
      etfdiv: '#34d399',
      etf2000e: '#fbbf24',
      accent: '#6366f1',
      accentFillTop: 'rgba(99, 102, 241, 0.24)',
      accentFillBot: 'rgba(99, 102, 241, 0.01)',
      dif: '#cbd5e1',
      dea: '#fbbf24',
      buy: '#fb7185',
      sell: '#34d399',
      areaAtk: 'rgba(244, 63, 94, 0.095)',
      areaDiv: 'rgba(16, 185, 129, 0.075)',
      ddFill: 'rgba(16, 185, 129, 0.16)',
      zoomFill: 'rgba(99, 102, 241, 0.20)',
      zoomHandle: '#6366f1',
    };
  }

  function calcZoomStart(totalLen, days) {
    if (!days || days <= 0 || days >= totalLen) return 0;
    return Math.max(0, +((1 - days / totalLen) * 100).toFixed(2));
  }

  // ---------- 核心统计计算 ----------
  function calcStats(data) {
    const nav = data.nav_strat;
    const N = nav.length - 1;
    const finalNAV = nav[N];
    const tradingDays = data.dates.length;
    const years = tradingDays / 252;
    const cumRet = finalNAV - 1;
    const annRet = Math.pow(finalNAV, 1 / years) - 1;

    let p = nav[0], mdd = 0, pDate = 0, mddPeakDate = 0, mddDate = 0, uw = 0, uwMax = 0;
    for (let i = 1; i <= N; i++) {
      if (nav[i] > p) { p = nav[i]; pDate = i; uw = 0; }
      else { uw++; if (uw > uwMax) uwMax = uw; }
      const d = nav[i] / p - 1;
      if (d < mdd) { mdd = d; mddPeakDate = pDate; mddDate = i; }
    }

    let curPeak = nav[0], curIdx = 0;
    for (let i = 1; i <= N; i++) {
      if (nav[i] > curPeak) { curPeak = nav[i]; curIdx = i; }
    }
    const curDD = nav[N] / curPeak - 1;

    const year = data.dates[N].slice(0, 4);
    const iY = data.dates.findIndex(d => d.slice(0, 4) === year);
    const ytd = nav[N] / (iY > 0 ? nav[iY - 1] : nav[0]) - 1;

    const h = data.etf1000_hfq;
    const holdCum = h[N] / h[0] - 1;
    const holdAnn = Math.pow(1 + holdCum, 1 / years) - 1;

    return {
      N, finalNAV, tradingDays, years, cumRet, annRet,
      mdd, mddPeakDate, mddDate, uwMax,
      curDD, curIdx, year, ytd, iY, holdCum, holdAnn,
      exAnn: annRet - holdAnn,
      calmar: annRet / Math.abs(mdd || 1),
    };
  }

  function buildBenchNavs(data) {
    const D = data.dates, n = D.length;
    const swIdx = D.indexOf(data.switch_atk);
    const atkH = data.etf1000_hfq, e2 = data.etf2000e_hfq, divH = data.etfdiv_hfq;
    const atkNav = [1], divNav = [1];
    for (let i = 1; i < n; i++) {
      const rAtk = (swIdx < 0 || i <= swIdx || e2[i] == null || e2[i - 1] == null)
        ? atkH[i] / atkH[i - 1] - 1
        : e2[i] / e2[i - 1] - 1;
      atkNav.push(atkNav[i - 1] * (1 + rAtk));
      divNav.push(divNav[i - 1] * divH[i] / divH[i - 1]);
    }
    return { atkNav, divNav };
  }

  // ---------- 顶部新鲜度 & 运行状态 ----------
  function renderFreshness(data) {
    const chip = $('freshnessChip');
    const txt = $('updatedText');
    const N = data.dates.length - 1;
    const lastDate = data.dates[N];
    if (data.avg_stale) {
      chip.classList.add('stale');
      txt.innerHTML = `均价滞后 ${data.avg_date || lastDate}`;
      chip.title = `导出 ${data.updated || '-'} · 平均股价数据日期 ${data.avg_date || '?'}`;
    } else {
      chip.classList.remove('stale');
      txt.innerHTML = `<span class="ftext-long">数据截至 </span><span class="mono">${lastDate}</span>`;
      chip.title = `最近导出: ${data.updated || '-'}`;
    }
    const fmeta = $('footerMeta');
    if (fmeta) fmeta.textContent = `数据截至 ${lastDate} · 导出 ${data.updated || '-'}`;

    // 系统状态卡
    $('sysLastDate').textContent = lastDate;
    $('sysUpdated').textContent = data.updated || '-';
    $('sysSource').innerHTML = `${data.avg_source || '-'}${data.avg_stale ? ' <span class="badge badge-amber" style="margin-left:4px">数据陈旧</span>' : ''}`;
    $('sysLen').textContent = `${data.dates.length} 个交易日（${data.dates[0]} 起）`;
    const errs = (data.data_errors || []).length;
    $('sysHealth').innerHTML = errs
      ? `<span class="badge badge-amber">${errs} 条数据告警（见 update.log）</span>`
      : '<span class="badge badge-div">全部通道正常</span>';
  }

  // ---------- 四策略切换卡片条 & 05区对比总表 ----------
  function renderStrategyBar(data) {
    const bar = $('stratBar');
    if (!bar || !data.strategies) return;
    const order = data.strategy_order || Object.keys(data.strategies);
    const curId = resolveStratId(data);
    const pal = stratPalette();
    bar.innerHTML = order.map(sid => {
      const s = data.strategies[sid];
      if (!s) return '';
      const m = s.metrics || {};
      const on = sid === curId;
      const dot = pal[sid] || '#3b82f6';
      return `<button type="button" class="strat-tab${on ? ' on' : ''}" data-strat="${sid}" role="tab" aria-selected="${on}">
        <div class="strat-tab-head">
          <span class="strat-tab-title"><span class="strat-dot" style="background:${dot}"></span>${s.short_name || sid}</span>
          <span class="strat-tab-tag">${s.badge || ''}</span>
        </div>
        <div class="strat-tab-metrics">
          <span class="strat-ann">年化 ${fmtPct(m.ann_ret)}</span>
          <span class="strat-mdd">回撤 ${fmtPct(m.mdd)}</span>
          <span class="strat-calmar">Calmar ${m.calmar != null ? Number(m.calmar).toFixed(2) : '--'}</span>
        </div>
        <div class="strat-tab-codes">
          <span title="进攻: ${s.offense_desc}">攻: ${s.offense_desc}</span>
          <span title="防守: ${s.defense_desc}">防: ${s.defense_desc}</span>
        </div>
      </button>`;
    }).join('');
  }

  function renderStrategyCompareTable(data) {
    const tbody = $('stratCompareTbody');
    if (!tbody || !data.strategies) return;
    const order = data.strategy_order || Object.keys(data.strategies);
    const curId = resolveStratId(data);
    const pal = stratPalette();
    const sub = $('compareTableSub');
    if (sub && data.dates && data.dates.length) {
      sub.textContent = `区间 ${data.dates[0]} ~ ${data.dates[data.dates.length - 1]}（${data.dates.length} 交易日），后复权真实成交拆段，含双边万1佣金（点击任意行切换策略）`;
    }
    tbody.innerHTML = order.map(sid => {
      const s = data.strategies[sid];
      if (!s) return '';
      const m = s.metrics || {};
      const on = sid === curId;
      const dot = pal[sid] || '#3b82f6';
      return `<tr data-strat="${sid}" class="${on ? 'is-active' : ''}">
        <td class="text-col"><span style="display:inline-flex;align-items:center;gap:6px"><span class="strat-dot" style="background:${dot}"></span><b>${s.name}</b></span></td>
        <td class="text-col mono">${s.offense_desc}</td>
        <td class="text-col mono">${s.defense_desc}</td>
        <td class="text-col">${s.exec_desc}</td>
        <td class="pos mono"><b>${fmtPct(m.cum_ret, 1)}</b></td>
        <td class="pos mono"><b>${fmtPct(m.ann_ret, 2)}</b></td>
        <td class="neg mono">${fmtPct(m.mdd, 2)}</td>
        <td class="mono"><b>${m.calmar != null ? Number(m.calmar).toFixed(2) : '--'}</b></td>
        <td class="mono">${m.sharpe != null ? Number(m.sharpe).toFixed(2) : '--'}</td>
        <td class="${(m.ytd || 0) >= 0 ? 'pos' : 'neg'} mono">${fmtPct(m.ytd, 2)}</td>
        <td class="mono">${m.trades != null ? m.trades : '--'}</td>
      </tr>`;
    }).join('');
  }

  function selectStrategy(sid) {
    if (!lastData || !lastData.strategies || !lastData.strategies[sid]) return;
    activeStratId = sid;
    try { localStorage.setItem(STRAT_KEY, sid); } catch (e) {}
    renderAll(lastData);
  }

  // ---------- 信号横幅（收盘信号） ----------
  function renderSignalAlert(data) {
    const el = $('sigBanner');
    el.className = 'sig-banner';
    el.innerHTML = '';
    delete el.dataset.live;
    const ls = data.last_signal;
    const alertSvg = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;
    if (!ls) return;
    const sid = data.strategy_id || 'T1';
    if (ls.action === 'buy') {
      el.className = 'sig-banner buy';
      el.innerHTML = `${alertSvg}<span>【${sid}】${ls.date} 收盘触发买入信号（DIF 零下拐头向上）→ 下一交易日开盘：切换至进攻仓 <b>${ls.next_offense}</b></span>`;
    } else if (ls.action === 'sell') {
      el.className = 'sig-banner sell';
      const timing = data.exec_mode === 'a3_asym' ? '今日尾盘（14:55后确认）' : '下一交易日开盘';
      el.innerHTML = `${alertSvg}<span>【${sid}】${ls.date} 收盘触发卖出信号（通过 S1+S2 检验）→ ${timing}：切换至防守仓 <b>${ls.next_defense}</b></span>`;
    } else if (ls.blocked_reason) {
      el.className = 'sig-banner info';
      el.innerHTML = `${alertSvg}<span>【${sid}】${ls.date} 880003 DIF 拐头向下，但被防洗盘过滤拦截（${ls.blocked_reason}）→ 维持当前仓位不动</span>`;
    }
  }

  // ---------- 顶部持仓卡 + 信号监测卡 ----------
  function renderStatusCards(data, liveOverride) {
    const N = data.dates.length - 1;
    const last = data.legs[data.legs.length - 1];
    const posCard = $('posCard');

    if (last && last.open) {
      const isAtk = last.asset === '1000';
      posCard.className = 'card pos-card ' + (isAtk ? 'is-atk' : 'is-div');
      const roleEl = $('posRoleBadge');
      roleEl.className = 'badge ' + (isAtk ? 'badge-atk' : 'badge-div');
      roleEl.textContent = isAtk ? '当前持仓 · 进攻仓' : '当前持仓 · 防守仓';
      $('posModeBadge').textContent = `${data.strategy_id || 'T1'} · ${data.strategy_badge || 'S1+S2'}`;

      const codes = last.codes || [];
      const isMulti = codes.length > 1;
      if (!isMulti && codes.length === 1) {
        $('posAssetName').textContent = codes[0].name;
        $('posAssetCode').textContent = codes[0].code;
      } else {
        $('posAssetName').textContent = last.asset_name || (isAtk ? '进攻组合' : '防守组合');
        $('posAssetCode').textContent = isMulti ? `${codes.length}只组合` : '';
      }

      let floatRet = last.ret;
      let priceDisplay = `${last.buy_price_text || fmtNum(last.buy_price, 3)} → ${last.sell_price_text || fmtNum(last.sell_price, 3)}`;
      const liveMap = {};
      if (liveOverride && !liveOverride.closed) {
        for (const e of (liveOverride.etfs || [])) {
          if (e && e.price != null) liveMap[e.code] = e.price;
        }
      }

      if (codes.length) {
        let hasLive = false;
        let wSum = 0, wRet = 0;
        const pills = codes.map(item => {
          const lp = liveMap[item.code] != null ? liveMap[item.code] : item.last_price;
          if (liveMap[item.code] != null) hasLive = true;
          const r = item.buy_price ? (lp / item.buy_price - 1) : 0;
          wSum += item.weight;
          wRet += item.weight * r;
          return `<span class="holding-pill"><b>${Math.round(item.weight * 100)}% ${item.code}</b> ${item.name} <span>${fmtNum(item.buy_price, 3)}→${fmtNum(lp, 3)}</span></span>`;
        });
        $('posHoldings').innerHTML = isMulti ? pills.join('') : '';
        if (hasLive && wSum > 0) {
          floatRet = wRet / wSum - 0.0002;
          if (!isMulti) {
            const c0 = codes[0];
            const lp = liveMap[c0.code] != null ? liveMap[c0.code] : c0.last_price;
            priceDisplay = `${fmtNum(c0.buy_price, 3)} → ${fmtNum(lp, 3)}`;
          } else {
            priceDisplay = '见上方组合明细';
          }
        } else if (isMulti) {
          priceDisplay = '见上方组合明细';
        }
      } else {
        $('posHoldings').innerHTML = '';
      }

      const retEl = $('posFloatRet');
      retEl.textContent = fmtPct(floatRet);
      retEl.className = 'pos-ret-val ' + (floatRet >= 0 ? 'up' : 'down');

      $('posBuyDate').textContent = last.buy_date;
      $('posPrices').textContent = priceDisplay;
      $('posDays').textContent = `${last.days} 天`;
      $('posNav').textContent = `${data.nav_strat[N].toFixed(3)}x`;
    } else {
      posCard.className = 'card pos-card';
      $('posRoleBadge').className = 'badge badge-neutral';
      $('posRoleBadge').textContent = '空仓';
      $('posAssetName').textContent = '当前无持仓';
      $('posAssetCode').textContent = '';
      $('posHoldings').innerHTML = '';
      $('posFloatRet').textContent = '--';
    }

    // 信号监测区
    let avgNow = data.avg[N];
    let avgChg = N > 0 ? (data.avg[N] / data.avg[N - 1] - 1) : null;
    let difNow = data.dif[N];
    let deaNow = data.dea[N];
    let isLiveVal = false;

    if (liveOverride && !liveOverride.closed) {
      if (liveOverride.avg && liveOverride.avg.value != null) {
        avgNow = liveOverride.avg.value;
        avgChg = liveOverride.avg.chg_pct != null ? liveOverride.avg.chg_pct / 100 : avgChg;
        isLiveVal = true;
      }
      if (liveOverride.sig) {
        if (liveOverride.sig.dif != null) difNow = liveOverride.sig.dif;
        if (liveOverride.sig.dea != null) deaNow = liveOverride.sig.dea;
      }
    }

    $('sigAvgVal').textContent = avgNow != null ? avgNow.toFixed(2) : '--';
    const avgSub = $('sigAvgSub');
    if (avgChg != null) {
      avgSub.innerHTML = `<span class="${avgChg >= 0 ? 'up' : 'down'}">${fmtPct(avgChg)}</span> <span class="faint">${isLiveVal ? '盘中实时' : data.dates[N]}</span>`;
    } else {
      avgSub.textContent = data.dates[N];
    }

    // DIF 连续升降天数
    const difArr = data.dif;
    let upDays = 0, downDays = 0;
    for (let i = N; i > 0; i--) {
      if (difArr[i] > difArr[i - 1]) { if (downDays) break; upDays++; }
      else if (difArr[i] < difArr[i - 1]) { if (upDays) break; downDays++; }
      else break;
    }
    const below = difNow < 0;
    const streak = upDays ? `连升 ${upDays} 天` : downDays ? `连降 ${downDays} 天` : '走平';
    $('sigDifZone').innerHTML = `<span class="${below ? 'down' : 'up'}">${below ? '零轴下' : '零轴上'}</span>`;
    const difEl = $('sigDifVal');
    difEl.textContent = difNow != null ? difNow.toFixed(4) : '--';
    difEl.className = 'sv ' + (downDays ? 'down' : upDays ? 'up' : '');
    $('sigDifSub').textContent = streak;

    // S1 零下回落比值 & S2 风格比值 R-DIF(12/26) 实时防洗盘状态（盘中优先用 live.json 实时值）
    let relDrop = data.s1_rel_drop ? data.s1_rel_drop[N] : null;
    const s1Th = data.s1_threshold || 0.001;
    let rDifNow = data.r_dif ? data.r_dif[N] : null;
    let rStrongNow = data.r_strong ? !!data.r_strong[N] : false;
    if (liveOverride && !liveOverride.closed && liveOverride.sig) {
      if (liveOverride.sig.s1_rel_drop != null) relDrop = liveOverride.sig.s1_rel_drop;
      if (liveOverride.sig.r_dif != null) rDifNow = liveOverride.sig.r_dif;
      if (liveOverride.sig.r_strong != null) rStrongNow = !!liveOverride.sig.r_strong;
    }
    const s1Ready = !below || (relDrop != null && relDrop > s1Th);

    if (rStrongNow) {
      $('sigBarTag').innerHTML = '<span class="up">S2 屏蔽卖出</span>';
    } else if (below && !s1Ready) {
      $('sigBarTag').innerHTML = '<span class="muted">S1 过滤微跌</span>';
    } else {
      $('sigBarTag').innerHTML = '<span class="down">卖出通道放行</span>';
    }
    $('sigDeaVal').textContent = relDrop != null ? `${(relDrop * 1000).toFixed(2)}‰` : (deaNow != null ? deaNow.toFixed(4) : '--');
    const rTxt = rDifNow != null ? `R-DIF ${rDifNow >= 0 ? '+' : ''}${rDifNow.toFixed(4)}${rStrongNow ? '(走强)' : ''}` : `DEA ${deaNow != null ? deaNow.toFixed(4) : '--'}`;
    $('sigHintSub').textContent = `S1阈 ${(s1Th * 1000).toFixed(1)}‰ · ${rTxt}`;

    // 更新 01 走势图执行口径副标题
    const execHint = $('marketExecHint');
    if (execHint && data.exec_desc) {
      execHint.textContent = `${data.strategy_id || 'T1'}：${data.exec_desc} · 攻 ${data.offense_desc || ''} / 防 ${data.defense_desc || ''}`;
    }

    // 上次换仓 & 防守池择优
    const lastTrade = data.trades[data.trades.length - 1];
    if (lastTrade) {
      const actTag = lastTrade.action === 'buy'
        ? `<span class="badge badge-atk">买入进攻</span>`
        : `<span class="badge badge-div">切换防守</span>`;
      $('lastTradeSummary').innerHTML = `上次换仓 <b class="mono" style="color:var(--text)">${lastTrade.date}</b> ${actTag}`;
    }

    const dp = data.defense_pick;
    const dpEl = $('defensePickSummary');
    if (dp) {
      const defLabel = c => c === '159201' ? '自由现金流(159201)' : '红利低波(512890)';
      const m1 = dp.mom_890 == null ? 'NA' : fmtPct(dp.mom_890, 1);
      const m2 = dp.mom_201 == null ? 'NA' : fmtPct(dp.mom_201, 1);
      const pick = dp.in_defense ? dp.holding_def : dp.next_pick;
      const prefix = dp.in_defense ? '防守择优持有' : '若切防守优选';
      dpEl.innerHTML =
        `<span class="faint" style="font-family:var(--font-sans)">${prefix}</span>` +
        `<span class="badge badge-accent">${defLabel(pick)}</span>` +
        `<span class="faint">20日动量: 512890 <span class="${(dp.mom_890 || 0) >= 0 ? 'up' : 'down'}">${m1}</span> / 159201 <span class="${(dp.mom_201 || 0) >= 0 ? 'up' : 'down'}">${m2}</span></span>`;
    } else if (data.defense_desc) {
      dpEl.innerHTML =
        `<span class="faint" style="font-family:var(--font-sans)">防守配置</span>` +
        `<span class="badge badge-accent">${data.defense_desc}</span>`;
    } else {
      dpEl.innerHTML = '';
    }

    renderIntradayRadar(data, liveOverride);
  }

  // ---------- 盘中分时买点雷达（x_turn 投影 + 阳线过滤 + 10:30/11:30/14:00节点） ----------
  function renderIntradayRadar(data, liveOverride) {
    const box = $('intradayRadar');
    if (!box) return;
    const badge = $('intraStatusBadge');
    const pricesEl = $('intraPrices');
    const checksEl = $('intraChecks');
    const hintEl = $('intraHint');

    const pill = (text, cls = '') => `<span class="check-pill${cls ? ' ' + cls : ''}">${text}</span>`;
    const fmtSignPct = v => v == null ? '--' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`;

    // 1) 盘中实时模式：直接消费 live.json 的 sig.intraday_buy
    const liveIb = (liveOverride && !liveOverride.closed && liveOverride.sig) ? liveOverride.sig.intraday_buy : null;
    if (liveIb) {
      box.className = 'intra-radar'
        + (liveIb.status === 'triggered' ? ' is-triggered'
          : liveIb.status === 'blocked_gap' ? ' is-blocked'
          : (liveIb.status === 'watch' || liveIb.status === 'wait_time') ? ' is-watch' : '');

      const badgeCls = liveIb.status === 'triggered' ? 'badge-atk'
        : liveIb.status === 'blocked_gap' ? 'badge-amber'
        : (liveIb.status === 'watch' || liveIb.status === 'wait_time') ? 'badge-accent'
        : 'badge-neutral';
      badge.className = 'badge ' + badgeCls;
      badge.textContent = liveIb.label || '盘中监控';

      if (liveIb.eligible) {
        pricesEl.innerHTML =
          `<span>拐点价 <b>${Number(liveIb.x_turn).toFixed(2)}</b> <span class="${(liveIb.x_turn_pct_cur || 0) <= 0 ? 'up' : 'faint'}">(${fmtSignPct(liveIb.x_turn_pct_cur)})</span></span>` +
          `<span>+0.5%缓冲 <b>${Number(liveIb.x_turn_buf).toFixed(2)}</b></span>` +
          `<span class="faint">零轴 ${Number(liveIb.x_zero).toFixed(2)}</span>`;

        const c1 = liveIb.cross_buf ? pill('✓ 过+0.5%缓冲价', 'ok')
          : liveIb.cross_turn ? pill('✓ 已过拐点价', 'ok')
          : pill(`○ 未过拐点 (${fmtSignPct(liveIb.x_turn_pct_cur)})`);
        const c2 = liveIb.yang_ok
          ? (liveIb.not_gap_trap ? pill('✓ 阳线·非高开', 'ok') : pill('✓ 阳线(高开慎追)', 'warn'))
          : pill('✕ 阴线/高开低走拦截', 'warn');
        const c3 = liveIb.in_cp_window
          ? pill('✓ 推荐检查节点', 'pass-cool')
          : liveIb.time_ok
            ? pill(`✓ ≥10:00 · 下节点 ${liveIb.next_cp}`, 'pass-cool')
            : pill('○ 10:00 前暂观察');
        checksEl.innerHTML = c1 + c2 + c3;
      } else {
        pricesEl.innerHTML =
          `<span>转降临界 <b>${Number(liveIb.x_turn).toFixed(2)}</b> (${fmtSignPct(liveIb.x_turn_pct_cur)})</span>` +
          `<span class="faint">零轴 ${Number(liveIb.x_zero).toFixed(2)}</span>`;
        checksEl.innerHTML = '';
      }
      hintEl.textContent = liveIb.hint || '';
      return;
    }

    // 2) 休市 / 静态站模式：基于 data.intraday_buy 展示下一交易日盘前预案
    const ib = data.intraday_buy;
    if (!ib) {
      box.className = 'intra-radar';
      badge.className = 'badge badge-neutral';
      badge.textContent = '暂无数据';
      pricesEl.textContent = '--';
      checksEl.innerHTML = '';
      hintEl.textContent = '请运行 export_json.py 更新下一交易日拐点预告。';
      return;
    }

    const lastLeg = (data.legs || [])[data.legs.length - 1];
    const isAtk = !!(lastLeg && lastLeg.open && lastLeg.asset === '1000');
    const ls = data.last_signal || {};
    const tStr = Number(ib.x_turn).toFixed(2);
    const tbStr = Number(ib.x_turn_buf).toFixed(2);
    const zStr = Number(ib.x_zero).toFixed(2);
    const tPct = fmtPct(ib.x_turn_pct, 2);
    const tbPct = fmtPct(ib.x_turn_buf_pct, 2);

    if (ls.action === 'buy') {
      box.className = 'intra-radar is-triggered';
      badge.className = 'badge badge-atk';
      badge.textContent = '日线买点已确认';
      pricesEl.innerHTML =
        `<span>下日转降临界 <b>${tStr}</b> (${tPct})</span>` +
        `<span class="faint">零轴 ${zStr}</span>`;
      checksEl.innerHTML =
        pill('✓ 日线 DIF 已零下拐头', 'ok') +
        pill('建议下日 09:30~09:35 开盘执行', 'pass-cool');
      hintEl.textContent = `${ls.date} 收盘已正式确认日线买入信号，下一交易日开盘切换至 ${ls.next_offense || '进攻仓'}；若已在盘中提前介入则继续持有。`;
    } else if (isAtk) {
      box.className = 'intra-radar';
      badge.className = 'badge badge-neutral';
      badge.textContent = '已持进攻仓';
      pricesEl.innerHTML =
        `<span>下日转降临界 <b>${tStr}</b> (${tPct})</span>` +
        `<span class="faint">零轴 ${zStr}</span>`;
      checksEl.innerHTML = '';
      hintEl.textContent = `当前持进攻仓无需分时买点；下一交易日 880003 跌破 ${tStr}（${tPct}）时 DIF 转降（仍须通过 S1+S2 检验才切防守）。`;
    } else if (ib.eligible_buy) {
      box.className = 'intra-radar is-watch';
      badge.className = 'badge badge-accent';
      badge.textContent = '下日盘中待监控';
      pricesEl.innerHTML =
        `<span>拐点价 <b>${tStr}</b> <span class="up">(${tPct})</span></span>` +
        `<span>+0.5%缓冲 <b>${tbStr}</b> (${tbPct})</span>` +
        `<span class="faint">零轴 ${zStr}</span>`;
      checksEl.innerHTML =
        pill(`① 880003 ≥ ${tStr} (稳健 ${tbStr})`, 'pass-cool') +
        pill('② 880003 与进攻 ETF 走阳(防高开)') +
        pill('③ 10:30 / 11:30 / 14:00 节点');
      hintEl.textContent = `防守期预案（基准 ${ib.base_date} 收 ${Number(ib.base_close).toFixed(2)}）：下日 ≥10:00 站稳拐点且走阳可限价分时切入（最简 11:30→13:00）；若 15:00 收盘回落拐点下，次日 09:35 认错卖出转现金/T+0 货基防锁仓。`;
    } else {
      box.className = 'intra-radar';
      badge.className = 'badge badge-neutral';
      badge.textContent = '非零下待买态';
      pricesEl.innerHTML =
        `<span>下日拐点临界 <b>${tStr}</b> (${tPct})</span>` +
        `<span class="faint">零轴 ${zStr}</span>`;
      checksEl.innerHTML = '';
      hintEl.textContent = `当前 DIF=${Number(ib.dif_last).toFixed(4)}（${ib.dif_below_zero ? '零下未下行' : '零轴上方'}），暂不启用盘中分时买点。`;
    }
  }

  // ---------- 核心 KPI 指标 ----------
  function renderKPIs(data) {
    const s = calcStats(data);
    const D = data.dates;

    const setVal = (id, text, cls) => {
      const el = $(id);
      el.textContent = text;
      el.className = 'kpi-val ' + (cls || '');
    };

    setVal('kpiCum', fmtPct(s.cumRet), s.cumRet >= 0 ? 'up' : 'down');
    $('kpiCumTag').textContent = `净值 ${s.finalNAV.toFixed(2)}x`;
    $('kpiCumSub').textContent = `${D[0]} → ${D[s.N]}`;

    setVal('kpiAnn', fmtPct(s.annRet), s.annRet >= 0 ? 'up' : 'down');
    $('kpiCalmarTag').textContent = `Calmar ${s.calmar.toFixed(2)}`;
    $('kpiAnnSub').textContent = `${s.tradingDays} 个交易日（约 ${s.years.toFixed(1)} 年）`;

    setVal('kpiMDD', fmtPct(s.mdd), 'down');
    $('kpiUwTag').textContent = `最长水下 ${s.uwMax} 日`;
    $('kpiMDDSub').textContent = `${D[s.mddPeakDate]} → ${D[s.mddDate]}`;

    setVal('kpiYTD', fmtPct(s.ytd), s.ytd >= 0 ? 'up' : 'down');
    $('kpiYtdYear').textContent = `${s.year} 年`;
    $('kpiYTDSub').textContent = `${D[s.iY]} → ${D[s.N]}`;

    setVal('kpiCurDD', fmtPct(s.curDD), s.curDD < -0.0001 ? 'down' : 'up');
    const monthsUw = ((s.N - s.curIdx) / 252 * 12).toFixed(1);
    $('kpiCurDDSub').textContent = s.curDD < -0.0001
      ? `高点 ${D[s.curIdx]}（已 ${monthsUw} 个月）`
      : `当前处于历史新高 (${D[s.N]})`;

    setVal('kpiEx', fmtPct(s.exAnn), s.exAnn >= 0 ? 'up' : 'down');
    $('kpiExSub').textContent = `累计超额 ${((s.cumRet - s.holdCum) * 100).toFixed(0)}pp（基准年化 ${(s.holdAnn * 100).toFixed(1)}%）`;

    // 段胜率与盈亏比汇总
    const closed = (data.legs || []).filter(l => !l.open);
    const wins = closed.filter(l => l.ret > 0);
    const losses = closed.filter(l => l.ret <= 0);
    const avgW = wins.length ? wins.reduce((a, l) => a + l.ret, 0) / wins.length : 0;
    const avgL = losses.length ? losses.reduce((a, l) => a + l.ret, 0) / losses.length : 0;
    const winRate = closed.length ? wins.length / closed.length : 0;
    const plRatio = avgL ? avgW / Math.abs(avgL) : 0;

    setVal('kpiWinRatio', `${(winRate * 100).toFixed(1)}% / ${plRatio.toFixed(2)}`, winRate >= 0.5 ? 'up' : '');
    $('kpiTradesTag').textContent = `${closed.length} 段已了结`;
    $('kpiWinSub').textContent = `盈 ${wins.length} 段 (${fmtPct(avgW, 1)}) · 亏 ${losses.length} 段 (${fmtPct(avgL, 1)})`;

    updateCustomKPI(data);
  }

  function updateCustomKPI(data) {
    const nav = data.nav_strat, N = nav.length - 1;
    const daysInput = $('kpiDays');
    const dateInput = $('kpiDate');
    if (!dateInput.min) { dateInput.min = data.dates[0]; dateInput.max = data.dates[N]; }

    let i0;
    if (customMode === 'date' && dateInput.value) {
      i0 = data.dates.findIndex(d => d >= dateInput.value);
      if (i0 < 0) i0 = N;
      daysInput.value = N - i0;
    } else {
      customMode = 'days';
      let d = parseInt(daysInput.value, 10);
      if (isNaN(d) || d < 1) d = 1;
      if (d > N) d = N;
      i0 = N - d;
      dateInput.value = data.dates[i0];
    }

    const activeDays = parseInt(daysInput.value, 10);
    for (const btn of $('quickDays').children) {
      btn.classList.toggle('on', Number(btn.dataset.d) === activeDays && customMode === 'days');
    }

    const ret = nav[N] / nav[i0] - 1;
    const el = $('kpiCustom');
    el.textContent = fmtPct(ret);
    el.className = 'kpi-val ' + (ret >= 0 ? 'up' : 'down');
  }

  // ---------- 图表 1：市场走势总览 + MACD ----------
  function renderMarketChart(data) {
    const T = chartTheme();
    if (!charts.market) charts.market = echarts.init($('marketChart'));
    const narrow = window.innerWidth < 640;
    const D = data.dates;
    const startPct = calcZoomStart(D.length, marketRangeDays);

    const buyLabel = t => (t.asset_name && t.asset_name.indexOf('2000') >= 0) ? '买2000增' : '买1000';
    const marks = [];
    for (const t of data.trades) {
      const i = D.indexOf(t.date);
      if (i < 0) continue;
      const isBuy = t.action === 'buy';
      marks.push({
        coord: [t.date, data.avg[i]],
        value: isBuy ? buyLabel(t) : '切防守',
        symbol: isBuy ? 'triangle' : 'arrow',
        symbolRotate: isBuy ? 0 : 180,
        symbolSize: 11,
        itemStyle: { color: isBuy ? T.buy : T.sell },
        label: { show: false }
      });
    }

    const areas = data.legs.map(l => {
      const end = l.sell_date || D[D.length - 1];
      const isAtk = l.asset === '1000';
      return [
        { xAxis: l.buy_date, itemStyle: { color: isAtk ? T.areaAtk : T.areaDiv }, label: { show: false } },
        { xAxis: end }
      ];
    });

    const sigIdx = new Set(data.trades.map(t => D.indexOf(t.sig_date)).filter(i => i >= 0));
    const difBgMarks = [];
    for (let i = 1; i < D.length - 1; i++) {
      const d0 = data.dif[i - 1], d1 = data.dif[i], d2 = data.dif[i + 1];
      if (d0 == null || d1 == null || d2 == null || sigIdx.has(i)) continue;
      if ((d0 < d1 && d1 > d2) || (d0 > d1 && d1 < d2)) {
        difBgMarks.push({
          coord: [D[i], d1], symbol: 'circle', symbolSize: 3.5,
          itemStyle: { color: T.axisText, opacity: 0.4 }, label: { show: false }
        });
      }
    }
    const difMarks = data.trades.map(t => {
      const i = D.indexOf(t.sig_date);
      if (i < 0) return null;
      const isBuy = t.action === 'buy';
      return {
        coord: [t.sig_date, data.dif[i]],
        value: isBuy ? buyLabel(t) : '切防守',
        symbol: isBuy ? 'triangle' : 'arrow',
        symbolRotate: isBuy ? 0 : 180,
        symbolSize: 9,
        itemStyle: { color: isBuy ? T.buy : T.sell },
        label: { show: false }
      };
    }).filter(Boolean).concat(difBgMarks);

    const cRed = [], cPink = [], cGreen = [], cYellow = [];
    let prevM = null;
    for (let i = 0; i < D.length; i++) {
      const m = (data.dif[i] == null || data.dea[i] == null) ? null : (data.dif[i] - data.dea[i]) * 2;
      let r = null, p = null, g = null, y = null;
      if (m != null) {
        if (m >= 0) { if (prevM != null && prevM > 0 && m < prevM) p = m; else r = m; }
        else { if (prevM != null && prevM < 0 && m > prevM) y = m; else g = m; }
      }
      cRed.push(r); cPink.push(p); cGreen.push(g); cYellow.push(y);
      prevM = m;
    }

    // 若处于防守待买态，在 880003 走势上叠加盘中分时买点拐点价 x_turn 虚线参考
    const liveIb = (lastLive && !lastLive.closed && lastLive.sig) ? lastLive.sig.intraday_buy : null;
    const xTurnVal = (liveIb && liveIb.eligible && liveIb.x_turn != null)
      ? Number(liveIb.x_turn)
      : ((data.intraday_buy && data.intraday_buy.eligible_buy && data.intraday_buy.x_turn != null)
          ? Number(data.intraday_buy.x_turn) : null);
    const avgMarkLine = xTurnVal != null ? {
      silent: true,
      symbol: 'none',
      data: [{ yAxis: xTurnVal }],
      lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.1 },
      label: {
        show: true,
        position: 'insideEndTop',
        color: '#f59e0b',
        fontSize: 10.5,
        fontFamily: 'monospace',
        formatter: `盘中拐点 ${xTurnVal.toFixed(2)}`
      }
    } : undefined;

    charts.market.setOption({
      animation: false,
      textStyle: { color: T.text, fontFamily: '"Plus Jakarta Sans", "Noto Sans SC", sans-serif' },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', lineStyle: { color: T.cross, type: 'dashed' }, crossStyle: { color: T.cross } },
        backgroundColor: T.tooltipBg,
        borderColor: T.tooltipBorder,
        borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        formatter: params => {
          const date = params && params.length ? params[0].axisValue : '';
          const i = D.indexOf(date);
          if (i < 0) {
            if (!liveChart || date !== liveChart.date) return date;
            const lc = liveChart;
            let html = `<div style="font-weight:700;margin-bottom:5px;font-family:monospace">${date} <span style="color:#f59e0b;font-size:11px">盘中实时</span></div>`;
            if (lc.ohlc) {
              const [o, c, lo, hi] = lc.ohlc;
              html += `<div style="font-family:monospace;color:${c >= o ? T.buy : T.sell};margin-bottom:4px">开 ${o.toFixed(2)} 高 ${hi.toFixed(2)} 低 ${lo.toFixed(2)} 收 <b>${c.toFixed(2)}</b></div>`;
            } else if (lc.avg != null) {
              html += `<div>平均股价 <b style="font-family:monospace">${lc.avg.toFixed(2)}</b></div>`;
            }
            for (const [name, code, color] of [['中证2000增强', '159552', T.etf2000e], ['红利低波ETF', '512890', T.etfdiv]]) {
              const v = lc.etfs ? lc.etfs[code] : null;
              if (v != null) html += `<div style="color:${color}">${name}: <b style="font-family:monospace">${v.toFixed(3)}</b></div>`;
            }
            html += `<div style="margin-top:5px;padding-top:4px;border-top:1px solid ${T.tooltipBorder};color:${T.muted};font-family:monospace">DIF ${lc.dif == null ? '-' : lc.dif.toFixed(4)} · DEA ${lc.dea == null ? '-' : lc.dea.toFixed(4)}</div>`;
            return html;
          }
          const pct = (arr, base) => {
            if (i <= 0 || arr[i] == null || base[i - 1] == null) return '';
            const p = (base[i] / base[i - 1] - 1) * 100;
            return ` <span style="color:${p >= 0 ? T.buy : T.sell};font-family:monospace">${p >= 0 ? '+' : ''}${p.toFixed(2)}%</span>`;
          };
          const rows = [
            ['平均股价', data.avg, data.avg, T.avg, 2],
            ['中证2000增强', data.etf2000e, data.etf2000e_hfq, T.etf2000e, 3],
            ['中证1000ETF', data.etf1000, data.etf1000_hfq, T.etf1000, 3],
            ['红利低波ETF', data.etfdiv, data.etfdiv_hfq, T.etfdiv, 3]
          ];
          let html = `<div style="font-weight:700;margin-bottom:5px;font-family:monospace">${date}</div>`;
          if (klineMode && data.avg_k && data.avg_k[i]) {
            const [o, c, lo, hi] = data.avg_k[i];
            html += `<div style="font-family:monospace;color:${c >= o ? T.buy : T.sell};margin-bottom:4px">开 ${o.toFixed(2)} 高 ${hi.toFixed(2)} 低 ${lo.toFixed(2)} 收 <b>${c.toFixed(2)}</b></div>`;
          }
          for (const [name, arr, base, color, dp] of rows) {
            const v = arr[i];
            if (v == null) continue;
            html += `<div style="display:flex;justify-content:space-between;gap:14px"><span><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${color};margin-right:6px"></span>${name}</span><span style="font-family:monospace"><b>${v.toFixed(dp)}</b>${pct(arr, base)}</span></div>`;
          }
          const dif = data.dif[i], dea = data.dea[i];
          html += `<div style="margin-top:5px;padding-top:4px;border-top:1px solid ${T.tooltipBorder};color:${T.muted};font-family:monospace">DIF ${dif == null ? '-' : dif.toFixed(4)}　DEA ${dea == null ? '-' : dea.toFixed(4)}</div>`;
          return html;
        }
      },
      legend: {
        data: ['平均股价(左轴)', '中证2000增强ETF(右轴)', '红利低波ETF(右轴)', 'DIF', 'DEA'],
        top: 6,
        left: 'center',
        type: narrow ? 'scroll' : 'plain',
        itemWidth: 14, itemHeight: 8,
        textStyle: { color: T.muted, fontSize: narrow ? 10.5 : 12 }
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: narrow ? 42 : 58, right: narrow ? 42 : 58, top: 46, height: '46%' },
        { left: narrow ? 42 : 58, right: narrow ? 42 : 58, top: '61%', height: '28%' }
      ],
      xAxis: [
        {
          type: 'category', data: D, gridIndex: 0, boundaryGap: klineMode,
          axisLine: { lineStyle: { color: T.axisLine } },
          axisLabel: { color: T.axisText, fontSize: 11 }
        },
        { type: 'category', data: D, gridIndex: 1, boundaryGap: false, show: false }
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0, name: narrow ? '' : '平均股价', scale: true,
          nameTextStyle: { color: T.muted, fontSize: 11 },
          axisLabel: { color: T.axisText, fontSize: 11 },
          splitLine: { lineStyle: { color: T.splitLine } }
        },
        {
          type: 'value', gridIndex: 0, name: narrow ? '' : 'ETF(元)', scale: true,
          nameTextStyle: { color: T.muted, fontSize: 11 },
          axisLabel: { color: T.axisText, fontSize: 11 },
          splitLine: { show: false }
        },
        {
          type: 'value', gridIndex: 1, scale: true,
          axisLabel: { color: T.axisText, fontSize: 11 },
          splitLine: { lineStyle: { color: T.splitLine } }
        }
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: startPct, end: 100 },
        {
          type: 'slider', xAxisIndex: [0, 1], bottom: 8, height: 18, start: startPct, end: 100,
          borderColor: T.axisLine, backgroundColor: 'transparent',
          fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
          moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText, fontSize: 10 },
          dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } }
        }
      ],
      series: [
        ...(klineMode && data.avg_k ? [{
          id: 'avg', name: '平均股价(左轴)', type: 'candlestick', data: data.avg_k,
          xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: T.buy, color0: T.sell, borderColor: T.buy, borderColor0: T.sell },
          markArea: { silent: true, data: areas },
          markPoint: { data: marks },
          ...(avgMarkLine ? { markLine: avgMarkLine } : {})
        }] : [{
          id: 'avg', name: '平均股价(左轴)', type: 'line', data: data.avg,
          xAxisIndex: 0, yAxisIndex: 0, symbol: 'none',
          lineStyle: { width: 1.8, color: T.avg }, itemStyle: { color: T.avg },
          sampling: 'lttb', large: true, largeThreshold: 400,
          markArea: { silent: true, data: areas },
          markPoint: { data: marks },
          ...(avgMarkLine ? { markLine: avgMarkLine } : {})
        }]),
        {
          id: 'etf2000e', name: '中证2000增强ETF(右轴)', type: 'line', data: data.etf2000e,
          xAxisIndex: 0, yAxisIndex: 1, symbol: 'none',
          lineStyle: { width: 1.5, color: T.etf2000e }, itemStyle: { color: T.etf2000e },
          sampling: 'lttb', large: true, largeThreshold: 400
        },
        {
          id: 'etfdiv', name: '红利低波ETF(右轴)', type: 'line', data: data.etfdiv,
          xAxisIndex: 0, yAxisIndex: 1, symbol: 'none',
          lineStyle: { width: 1.4, color: T.etfdiv }, itemStyle: { color: T.etfdiv },
          sampling: 'lttb', large: true, largeThreshold: 400
        },
        { name: 'MACD红', type: 'bar', data: cRed, xAxisIndex: 1, yAxisIndex: 2, barWidth: 3.5, barGap: 0, itemStyle: { color: '#ef5350' }, tooltip: { show: false }, emphasis: { disabled: true } },
        { name: 'MACD粉', type: 'bar', data: cPink, xAxisIndex: 1, yAxisIndex: 2, barWidth: 3.5, barGap: 0, itemStyle: { color: '#3b82f6' }, tooltip: { show: false }, emphasis: { disabled: true } },
        { name: 'MACD绿', type: 'bar', data: cGreen, xAxisIndex: 1, yAxisIndex: 2, barWidth: 3.5, barGap: 0, itemStyle: { color: '#26a69a' }, tooltip: { show: false }, emphasis: { disabled: true } },
        { name: 'MACD黄', type: 'bar', data: cYellow, xAxisIndex: 1, yAxisIndex: 2, barWidth: 3.5, barGap: 0, itemStyle: { color: '#eab308' }, tooltip: { show: false }, emphasis: { disabled: true } },
        {
          id: 'dif', name: 'DIF', type: 'line', data: data.dif, xAxisIndex: 1, yAxisIndex: 2,
          symbol: 'none', lineStyle: { width: 1.2, color: T.dif }, itemStyle: { color: T.dif },
          sampling: 'lttb', large: true, largeThreshold: 200,
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }], lineStyle: { color: T.axisText, type: 'dashed', width: 0.8 }, label: { show: false } },
          markPoint: { data: difMarks }
        },
        {
          id: 'dea', name: 'DEA', type: 'line', data: data.dea, xAxisIndex: 1, yAxisIndex: 2,
          symbol: 'none', lineStyle: { width: 1.1, color: T.dea }, itemStyle: { color: T.dea },
          sampling: 'lttb', large: true, largeThreshold: 200
        }
      ]
    }, true);
  }

  // ---------- 图表 2：四策略净值同屏对比曲线 ----------
  function renderNavChart(data) {
    const T = chartTheme();
    if (!charts.nav) charts.nav = echarts.init($('navChart'));
    const D = data.dates;
    const { atkNav, divNav } = buildBenchNavs(data);
    const startPct = calcZoomStart(D.length, navRangeDays);
    const pal = stratPalette();
    const curId = resolveStratId(data);
    const order = (data.strategies && data.strategy_order) ? data.strategy_order : [];

    const legendNames = [];
    const seriesList = [];

    if (order.length) {
      for (const sid of order) {
        const s = data.strategies[sid];
        if (!s) continue;
        const isActive = sid === curId;
        const col = pal[sid] || T.accent;
        const label = s.short_name || sid;
        legendNames.push(label);
        const item = {
          name: label,
          type: 'line',
          data: s.nav_strat,
          symbol: 'none',
          z: isActive ? 5 : 3,
          lineStyle: { width: isActive ? 2.6 : 1.5, color: col, opacity: isActive ? 1 : 0.72 },
          itemStyle: { color: col },
          sampling: 'lttb', large: true, largeThreshold: 400
        };
        if (isActive) {
          item.areaStyle = {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: T.accentFillTop },
              { offset: 1, color: T.accentFillBot }
            ])
          };
        }
        seriesList.push(item);
      }
    } else {
      legendNames.push('轮动策略');
      seriesList.push({
        name: '轮动策略', type: 'line', data: data.nav_strat, symbol: 'none',
        lineStyle: { width: 2.2, color: T.accent }, itemStyle: { color: T.accent },
        sampling: 'lttb', large: true, largeThreshold: 400
      });
    }

    legendNames.push('159552(拼接)持有', '512890红利低波持有');
    seriesList.push(
      {
        name: '159552(拼接)持有', type: 'line', data: atkNav, symbol: 'none',
        lineStyle: { width: 1.2, type: 'dashed', color: T.etf2000e, opacity: 0.75 }, itemStyle: { color: T.etf2000e },
        sampling: 'lttb', large: true, largeThreshold: 400
      },
      {
        name: '512890红利低波持有', type: 'line', data: divNav, symbol: 'none',
        lineStyle: { width: 1.2, type: 'dashed', color: T.etfdiv, opacity: 0.75 }, itemStyle: { color: T.etfdiv },
        sampling: 'lttb', large: true, largeThreshold: 400
      }
    );

    charts.nav.setOption({
      animation: false,
      textStyle: { color: T.text },
      tooltip: {
        trigger: 'axis',
        backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: T.cross, type: 'dashed' } },
        formatter: ps => {
          if (!ps || !ps.length) return '';
          let html = `<div style="font-weight:700;margin-bottom:5px;font-family:monospace">${ps[0].axisValue}</div>`;
          for (const p of ps) {
            if (p.value == null) continue;
            const ret = (p.value - 1) * 100;
            html += `<div style="display:flex;justify-content:space-between;gap:14px"><span><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${p.color};margin-right:6px"></span>${p.seriesName}</span><span style="font-family:monospace"><b>${Number(p.value).toFixed(3)}</b> <span style="color:${ret >= 0 ? T.buy : T.sell}">(${ret >= 0 ? '+' : ''}${ret.toFixed(1)}%)</span></span></div>`;
          }
          return html;
        }
      },
      legend: {
        data: legendNames,
        top: 6, left: 'center', type: window.innerWidth < 760 ? 'scroll' : 'plain',
        textStyle: { color: T.muted, fontSize: 12 }, itemWidth: 14, itemHeight: 8
      },
      grid: { left: 54, right: 24, top: 46, bottom: 48 },
      xAxis: {
        type: 'category', data: D, boundaryGap: false,
        axisLine: { lineStyle: { color: T.axisLine } },
        axisLabel: { color: T.axisText, fontSize: 11 }
      },
      yAxis: {
        type: navLogScale ? 'log' : 'value',
        scale: true,
        name: navLogScale ? '净值(对数)' : '净值',
        nameTextStyle: { color: T.muted, fontSize: 11 },
        axisLabel: { color: T.axisText, fontSize: 11, formatter: v => Number(v).toFixed(navLogScale ? 1 : 2) },
        splitLine: { lineStyle: { color: T.splitLine } }
      },
      dataZoom: [
        { type: 'inside', start: startPct, end: 100 },
        {
          type: 'slider', bottom: 8, height: 16, start: startPct, end: 100,
          borderColor: T.axisLine, backgroundColor: 'transparent',
          fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
          moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText, fontSize: 10 },
          dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } }
        }
      ],
      series: seriesList
    }, true);
  }

  // ---------- 月度 / 年度收益热力表 ----------
  function renderReturns(data) {
    const D = data.dates, nav = data.nav_strat;
    const holdSet = new Set();
    (data.hold_spans || []).forEach(([s, e]) => {
      let on = false;
      for (const d of D) { if (d === s) on = true; if (on) holdSet.add(d); if (d === e) on = false; }
    });

    const agg = {};
    for (let i = 1; i < D.length; i++) {
      const f = nav[i] / nav[i - 1];
      const isAtk = holdSet.has(D[i]);
      for (const k of [D[i].slice(0, 7), D[i].slice(0, 4), 'ALL']) {
        if (!agg[k]) agg[k] = { p1000: 1, pdiv: 1 };
        if (isAtk) agg[k].p1000 *= f; else agg[k].pdiv *= f;
      }
    }

    const monthLast = {}, yearLast = {};
    D.forEach((d, i) => { monthLast[d.slice(0, 7)] = nav[i]; yearLast[d.slice(0, 4)] = nav[i]; });
    const mRet = {}, yRet = {};
    let prev = 1;
    Object.keys(monthLast).sort().forEach(m => { mRet[m] = monthLast[m] / prev - 1; prev = monthLast[m]; });
    const years = Object.keys(yearLast).sort();
    prev = 1;
    years.forEach(y => { yRet[y] = yearLast[y] / prev - 1; prev = yearLast[y]; });

    const fmtC = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const brkHtml = key => {
      const a = agg[key];
      if (!a) return '';
      const c1 = a.p1000 - 1, c2 = a.pdiv - 1;
      return `<div class="brk"><span class="${c1 < 0 ? 'neg' : ''}">攻${fmtC(c1)}</span><span class="${c2 < 0 ? 'neg' : ''}">防${fmtC(c2)}</span></div>`;
    };
    const cell = (key, v) => {
      if (v == null) return '<td class="faint">-</td>';
      const intensity = Math.min(0.30, Math.abs(v) * 1.9);
      const color = v < 0 ? 'var(--down)' : 'var(--up)';
      return `<td class="${v < 0 ? 'neg' : 'pos'}" style="background: color-mix(in srgb, ${color} ${(intensity * 100).toFixed(0)}%, transparent)">${(v * 100).toFixed(2)}%${brkHtml(key)}</td>`;
    };
    const contribTd = v => `<td class="${v < 0 ? 'neg' : 'pos'}">${(v * 100).toFixed(2)}%</td>`;

    let html = '<thead><tr><th>年份</th>';
    for (let m = 1; m <= 12; m++) html += `<th>${m}月</th>`;
    html += '<th>全年</th><th>进攻贡献</th><th>防守贡献</th></tr></thead><tbody>';
    for (const y of years) {
      html += `<tr><td class="year">${y}</td>`;
      for (let m = 1; m <= 12; m++) {
        const key = `${y}-${String(m).padStart(2, '0')}`;
        html += cell(key, key in mRet ? mRet[key] : null);
      }
      const ay = agg[y] || { p1000: 1, pdiv: 1 };
      html += cell(y, yRet[y]) + contribTd(ay.p1000 - 1) + contribTd(ay.pdiv - 1) + '</tr>';
    }
    const A = agg['ALL'] || { p1000: 1, pdiv: 1 };
    const totalRet = nav[nav.length - 1] / nav[0] - 1;
    html += `<tr class="sum-row"><td class="year">汇总</td>${'<td></td>'.repeat(12)}${contribTd(totalRet)}${contribTd(A.p1000 - 1)}${contribTd(A.pdiv - 1)}</tr></tbody>`;
    $('retTable').innerHTML = html;
  }

  function renderAssetReturns(data) {
    const D = data.dates;
    const { atkNav, divNav } = buildBenchNavs(data);

    const periodStats = values => {
      const mLast = {}, yLast = {};
      D.forEach((d, i) => { mLast[d.slice(0, 7)] = values[i]; yLast[d.slice(0, 4)] = values[i]; });
      const mRet = {}, yRet = {};
      let prev = values[0];
      Object.keys(mLast).sort().forEach(m => { mRet[m] = mLast[m] / prev - 1; prev = mLast[m]; });
      const years = Object.keys(yLast).sort();
      prev = values[0];
      years.forEach(y => { yRet[y] = yLast[y] / prev - 1; prev = yLast[y]; });
      return { mRet, years, yRet, total: values[values.length - 1] / values[0] - 1 };
    };

    const atkStats = periodStats(atkNav);
    const divStats = periodStats(divNav);

    const bg = v => {
      const intensity = Math.min(0.28, Math.abs(v) * 1.8);
      const color = v < 0 ? 'var(--down)' : 'var(--up)';
      return `color-mix(in srgb, ${color} ${(intensity * 100).toFixed(0)}%, transparent)`;
    };
    const pairCell = (a, b) => {
      if (a == null || b == null) return '<td class="faint">-</td>';
      return `<td class="pair" style="--atk-bg:${bg(a)};--div-bg:${bg(b)}"><span class="atk">${(a * 100).toFixed(1)}%</span><span class="sep">/</span><span class="div">${(b * 100).toFixed(1)}%</span></td>`;
    };

    let html = '<thead><tr><th>年份</th>';
    for (let m = 1; m <= 12; m++) html += `<th>${m}月</th>`;
    html += '<th>全年</th></tr></thead><tbody>';
    for (const y of atkStats.years) {
      html += `<tr><td class="year">${y}</td>`;
      for (let m = 1; m <= 12; m++) {
        const key = `${y}-${String(m).padStart(2, '0')}`;
        html += pairCell(atkStats.mRet[key], divStats.mRet[key]);
      }
      html += pairCell(atkStats.yRet[y], divStats.yRet[y]) + '</tr>';
    }
    html += `<tr class="sum-row"><td class="year">汇总</td>${'<td></td>'.repeat(12)}${pairCell(atkStats.total, divStats.total)}</tr></tbody>`;
    $('assetRetTable').innerHTML = html;
  }

  // ---------- 图表 3：历史回撤 ----------
  function renderDrawdown(data) {
    const T = chartTheme();
    if (!charts.dd) charts.dd = echarts.init($('ddChart'));
    const D = data.dates, nav = data.nav_strat;

    const dd = [];
    let peak = -Infinity, curPeakDate = D[0], mdd = 0, mddDate = D[0], mddPeakDate = D[0];
    for (let i = 0; i < D.length; i++) {
      if (nav[i] > peak) { peak = nav[i]; curPeakDate = D[i]; }
      const v = +((nav[i] / peak - 1) * 100).toFixed(2);
      dd.push(v);
      if (v < mdd) { mdd = v; mddDate = D[i]; mddPeakDate = curPeakDate; }
    }
    $('ddStats').textContent = `最大回撤 ${mdd.toFixed(2)}%（${mddPeakDate} → ${mddDate}）`;

    charts.dd.setOption({
      animation: false,
      textStyle: { color: T.text },
      tooltip: {
        trigger: 'axis',
        backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: T.cross, type: 'dashed' } },
        formatter: ps => {
          const p = ps && ps[0];
          return p ? `<div style="font-family:monospace;font-weight:700">${p.axisValue}</div><div>回撤: <b style="color:${T.sell};font-family:monospace">${p.value.toFixed(2)}%</b></div>` : '';
        }
      },
      grid: { left: 48, right: 18, top: 20, bottom: 28 },
      xAxis: { type: 'category', data: D, boundaryGap: false, axisLine: { lineStyle: { color: T.axisLine } }, axisLabel: { color: T.axisText, fontSize: 10.5 } },
      yAxis: { type: 'value', max: 0, axisLabel: { color: T.axisText, fontSize: 10.5, formatter: '{value}%' }, splitLine: { lineStyle: { color: T.splitLine } } },
      dataZoom: [{ type: 'inside' }],
      series: [{
        name: '回撤', type: 'line', data: dd, symbol: 'none',
        lineStyle: { width: 1.2, color: T.sell }, itemStyle: { color: T.sell },
        areaStyle: { color: T.ddFill },
        sampling: 'lttb', large: true, largeThreshold: 400,
        markLine: {
          silent: true, symbol: 'none', data: [{ yAxis: mdd }],
          lineStyle: { color: T.sell, type: 'dashed', width: 0.9 },
          label: { color: T.sell, fontSize: 10, formatter: mdd.toFixed(1) + '%' }
        }
      }]
    }, true);
  }

  // ---------- 图表 4：滚动 250 日超额 ----------
  function renderRolling(data) {
    const T = chartTheme();
    if (!charts.roll) charts.roll = echarts.init($('rollChart'));
    const D = data.dates, nav = data.nav_strat;
    const { atkNav, divNav } = buildBenchNavs(data);
    const halfNav = atkNav.map((v, i) => 0.5 * v + 0.5 * divNav[i]);

    const W = 250;
    const excess = base => nav.map((_, i) => {
      if (i < W || base[i - W] == null) return null;
      return +(((nav[i] / nav[i - W] - 1) - (base[i] / base[i - W] - 1)) * 100).toFixed(1);
    });

    charts.roll.setOption({
      animation: false,
      textStyle: { color: T.text },
      tooltip: {
        trigger: 'axis',
        backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        valueFormatter: v => (v == null ? '-' : (v > 0 ? '+' : '') + v + 'pp')
      },
      legend: { data: ['vs 进攻仓持有', 'vs 50/50组合'], top: 4, left: 48, textStyle: { color: T.muted, fontSize: 11.5 }, itemWidth: 14, itemHeight: 8 },
      grid: { left: 48, right: 18, top: 34, bottom: 28 },
      xAxis: { type: 'category', data: D, boundaryGap: false, axisLine: { lineStyle: { color: T.axisLine } }, axisLabel: { color: T.axisText, fontSize: 10.5 } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: T.axisText, fontSize: 10.5, formatter: '{value}pp' }, splitLine: { lineStyle: { color: T.splitLine } } },
      dataZoom: [{ type: 'inside' }],
      series: [
        {
          name: 'vs 进攻仓持有', type: 'line', data: excess(atkNav), symbol: 'none',
          lineStyle: { width: 1.5, color: T.accent }, itemStyle: { color: T.accent }, sampling: 'lttb',
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }], lineStyle: { color: T.axisText, type: 'dashed', width: 0.9 }, label: { show: false } }
        },
        {
          name: 'vs 50/50组合', type: 'line', data: excess(halfNav), symbol: 'none',
          lineStyle: { width: 1.3, color: T.etf2000e }, itemStyle: { color: T.etf2000e }, sampling: 'lttb'
        }
      ]
    }, true);
  }

  // ---------- 图表 5：每日策略收益 ----------
  function renderDaily(data) {
    const T = chartTheme();
    if (!charts.daily) charts.daily = echarts.init($('dailyChart'));
    const D = data.dates, nav = data.nav_strat;
    const rets = [null];
    for (let i = 1; i < D.length; i++) rets.push(+((nav[i] / nav[i - 1] - 1) * 100).toFixed(2));

    const holdArr = new Array(D.length).fill('防守仓');
    (data.hold_spans || []).forEach(([s, e]) => {
      let on = false;
      for (let i = 0; i < D.length; i++) {
        if (D[i] === s) on = true;
        if (on) holdArr[i] = '进攻仓';
        if (D[i] === e) on = false;
      }
    });

    const vals = rets.slice(1).filter(v => v != null);
    const win = vals.filter(v => v > 0).length;
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    const mx = Math.max(...vals), mn = Math.min(...vals);
    $('dailyStats').textContent = `日胜率 ${(win / vals.length * 100).toFixed(1)}% · 日均 ${avg >= 0 ? '+' : ''}${avg.toFixed(2)}% · 极值 +${mx.toFixed(2)}% / ${mn.toFixed(2)}%`;

    charts.daily.setOption({
      animation: false,
      textStyle: { color: T.text },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        formatter: params => {
          const p = params && params[0];
          if (!p) return '';
          const i = p.dataIndex, v = rets[i];
          if (v == null) return D[i];
          return `<div style="font-family:monospace;font-weight:700;margin-bottom:4px">${D[i]}</div>`
            + `<div>日收益: <b style="color:${v >= 0 ? T.buy : T.sell};font-family:monospace">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</b></div>`
            + `<div>持仓: ${holdArr[i]} · 净值 <span style="font-family:monospace">${nav[i].toFixed(4)}</span></div>`;
        }
      },
      grid: { left: 48, right: 18, top: 16, bottom: 44 },
      xAxis: { type: 'category', data: D, axisLine: { lineStyle: { color: T.axisLine } }, axisLabel: { color: T.axisText, fontSize: 10.5 } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: T.axisText, fontSize: 10.5, formatter: '{value}%' }, splitLine: { lineStyle: { color: T.splitLine } } },
      dataZoom: [
        { type: 'inside', start: 85, end: 100 },
        {
          type: 'slider', bottom: 6, height: 14, start: 85, end: 100,
          borderColor: T.axisLine, backgroundColor: 'transparent',
          fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
          moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText, fontSize: 10 },
          dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } }
        }
      ],
      series: [{
        name: '日收益', type: 'bar', data: rets,
        itemStyle: { color: p => (p.value == null || p.value >= 0) ? T.buy : T.sell },
        large: true, largeThreshold: 400
      }]
    }, true);
  }

  // ---------- 图表 6：持仓段收益分布直方图 ----------
  function renderDist(data) {
    const T = chartTheme();
    if (!charts.dist) charts.dist = echarts.init($('distChart'));
    const closed = (data.legs || []).filter(l => !l.open);
    const rets = closed.map(l => +(l.ret * 100).toFixed(2));
    if (!rets.length) return;

    const mn = Math.floor(Math.min(...rets) / 2) * 2;
    const mx = Math.ceil(Math.max(...rets) / 2) * 2;
    const counts = [];
    for (let b = mn; b < mx; b += 2) {
      counts.push({ lo: b, hi: b + 2, n: rets.filter(v => v >= b && v < b + 2).length });
    }

    charts.dist.setOption({
      animation: false,
      textStyle: { color: T.text },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
        textStyle: { color: T.text, fontSize: 12 },
        formatter: ps => {
          const p = ps && ps[0];
          if (!p) return '';
          const c = counts[p.dataIndex];
          return `<div style="font-family:monospace">收益区间 ${c.lo}% ~ ${c.hi}%</div><div>持仓段数: <b>${c.n}</b></div>`;
        }
      },
      grid: { left: 42, right: 16, top: 20, bottom: 42 },
      xAxis: {
        type: 'category', data: counts.map(c => `${c.lo}~${c.hi}%`),
        axisLine: { lineStyle: { color: T.axisLine } },
        axisLabel: { color: T.axisText, fontSize: 10, rotate: 40 }
      },
      yAxis: {
        type: 'value', minInterval: 1,
        axisLabel: { color: T.axisText, fontSize: 10.5 },
        splitLine: { lineStyle: { color: T.splitLine } }
      },
      series: [{
        type: 'bar',
        data: counts.map(c => ({
          value: c.n,
          itemStyle: { color: c.lo >= 0 ? T.buy : T.sell, borderRadius: [3, 3, 0, 0] }
        })),
        barMaxWidth: 22
      }]
    }, true);
  }

  // ---------- 持仓段统计矩阵 ----------
  function renderLegStats(data) {
    const legs = data.legs || [];
    const closed = legs.filter(l => !l.open);
    const openLeg = legs.find(l => l.open);
    if (!closed.length) return;

    const wins = closed.filter(l => l.ret > 0);
    const losses = closed.filter(l => l.ret <= 0);
    const sumW = wins.reduce((a, l) => a + l.ret, 0);
    const sumL = losses.reduce((a, l) => a + l.ret, 0);
    const avgW = wins.length ? sumW / wins.length : 0;
    const avgL = losses.length ? sumL / losses.length : 0;
    const best = closed.reduce((a, l) => (l.ret > a.ret ? l : a), closed[0]);
    const worst = closed.reduce((a, l) => (l.ret < a.ret ? l : a), closed[0]);
    const avgDays = closed.reduce((a, l) => a + l.days, 0) / closed.length;

    const atkLegs = closed.filter(l => l.asset === '1000');
    const divLegs = closed.filter(l => l.asset !== '1000');
    const atkWin = atkLegs.length ? atkLegs.filter(l => l.ret > 0).length / atkLegs.length : 0;
    const divWin = divLegs.length ? divLegs.filter(l => l.ret > 0).length / divLegs.length : 0;

    const cell = (k, v, cls, s) =>
      `<div class="stat-cell"><div class="k">${k}</div><div class="v ${cls || ''}">${v}</div><div class="s">${s || ''}</div></div>`;

    $('legStats').innerHTML = [
      cell('已了结段数', closed.length, '', openLeg ? '另 1 段持有中' : '全部已了结'),
      cell('总段胜率', (wins.length / closed.length * 100).toFixed(1) + '%', wins.length >= losses.length ? 'up' : 'down', `盈 ${wins.length} / 亏 ${losses.length}`),
      cell('进攻段胜率', (atkWin * 100).toFixed(1) + '%', atkWin >= 0.5 ? 'up' : 'down', `共 ${atkLegs.length} 段`),
      cell('防守段胜率', (divWin * 100).toFixed(1) + '%', divWin >= 0.5 ? 'up' : 'down', `共 ${divLegs.length} 段`),
      cell('平均盈利段', fmtPct(avgW), 'up', '盈利笔均值'),
      cell('平均亏损段', fmtPct(avgL), 'down', '单次试错平均代价'),
      cell('盈亏比', avgL ? (avgW / Math.abs(avgL)).toFixed(2) : '-', '', '平均盈利 ÷ 平均亏损'),
      cell('利润因子', sumL ? (sumW / Math.abs(sumL)).toFixed(2) : '-', '', '总盈利 ÷ 总亏损'),
      cell('最好一段', fmtPct(best.ret), 'up', `${best.buy_date} → ${best.sell_date}`),
      cell('最差一段', fmtPct(worst.ret), 'down', `${worst.buy_date} → ${worst.sell_date}`),
      cell('平均持有', avgDays.toFixed(1) + ' 日', '', '每段买入 → 卖出'),
      cell('换仓次数', (data.trades || []).length, '', `约 ${((data.trades || []).length / (data.dates.length / 252)).toFixed(1)} 次/年`),
    ].join('');
  }

  // ---------- 交易台账与信号记录 ----------
  function populateYearSelect(data) {
    const sel = $('ledgerYear');
    const prev = sel.value;
    const years = [...new Set(data.dates.map(d => d.slice(0, 4)))].sort().reverse();
    sel.innerHTML = '<option value="all">全部年份</option>'
      + years.map(y => `<option value="${y}">${y} 年</option>`).join('');
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  }

  function renderLedger(data) {
    const allLegs = [...data.legs].reverse();
    const allTrades = [...data.trades].reverse();
    $('legsCount').textContent = `(${allLegs.length})`;
    $('sigsCount').textContent = `(${allTrades.length})`;

    const legs = allLegs.filter(l => {
      if (legFilter === '1000' && l.asset !== '1000') return false;
      if (legFilter === 'div' && l.asset === '1000') return false;
      if (ledgerYear !== 'all' && !l.buy_date.startsWith(ledgerYear)) return false;
      return true;
    });

    $('legsTbody').innerHTML = legs.map(l => {
      const isAtk = l.asset === '1000';
      const badge = isAtk
        ? `<span class="badge badge-atk">${l.asset_name || '进攻仓'}</span>`
        : `<span class="badge badge-div">${l.asset_name || '防守仓'}</span>`;
      const bp = l.buy_price_text || fmtNum(l.buy_price, 3);
      const sp = l.sell_price_text || fmtNum(l.sell_price, 3);
      return `<tr class="${isAtk ? 'buy' : 'sell'}${l.open ? ' row-open' : ''}">
        <td>${badge}</td>
        <td>${l.buy_date}</td>
        <td class="mono">${bp}</td>
        <td>${l.open ? '<span class="badge badge-accent">持有中</span>' : l.sell_date}</td>
        <td class="mono">${sp}</td>
        <td>${l.days}</td>
        <td class="${l.ret < 0 ? 'neg' : 'pos'}">${fmtPct(l.ret)}${l.open ? ' 浮' : ''}</td>
        <td class="${l.cum < 0 ? 'neg' : 'pos'}">${fmtPct(l.cum)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8" style="text-align:center;padding:24px" class="faint">当前筛选条件下无记录</td></tr>';

    const D = data.dates;
    const trades = allTrades.filter(t => {
      if (legFilter === '1000' && t.action !== 'buy') return false;
      if (legFilter === 'div' && t.action !== 'sell') return false;
      if (ledgerYear !== 'all' && !t.date.startsWith(ledgerYear)) return false;
      return true;
    });

    $('sigsTbody').innerHTML = trades.map(t => {
      const isBuy = t.action === 'buy';
      const idx = D.indexOf(t.sig_date);
      const dif = idx >= 0 ? data.dif[idx] : t.dif;
      const op = (t.sell_desc && t.buy_desc)
        ? `卖出 ${t.sell_desc} → 买入 <b>${t.buy_desc}</b>`
        : (isBuy ? `卖出 防守仓 → 买入 <b>${t.asset_name || '进攻仓'}</b>` : `卖出 ${t.asset_name || '进攻仓'} → 买入 <b>防守仓</b>`);
      const timing = t.exec_timing ? ` <span class="faint">${t.exec_timing}</span>` : '';
      const px = t.price_text || fmtNum(t.price, 3);
      return `<tr class="${isBuy ? 'buy' : 'sell'}">
        <td>${isBuy ? '<span class="badge badge-atk">买入进攻</span>' : '<span class="badge badge-div">切换防守</span>'}</td>
        <td>${t.sig_date}</td>
        <td>${t.date}${timing}</td>
        <td class="text-col">${op}</td>
        <td class="mono">${px}</td>
        <td>${dif == null ? '-' : Number(dif).toFixed(4)}</td>
        <td class="${(t.leg_ret || 0) < 0 ? 'neg' : 'pos'}">${t.leg_ret == null ? '-' : fmtPct(t.leg_ret)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" style="text-align:center;padding:24px" class="faint">当前筛选条件下无记录</td></tr>';
  }

  // ---------- 盘中实时叠加 ----------
  function patchLive(d) {
    lastLive = d;
    const chip = $('liveChip');
    const banner = $('sigBanner');

    if (!d || d.closed) {
      chip.className = 'live-chip';
      chip.innerHTML = '';
      if (banner.dataset.live) {
        banner.className = 'sig-banner';
        banner.innerHTML = '';
        delete banner.dataset.live;
        if (lastData) renderSignalAlert(activeView(lastData));
      }
      if (lastData) renderStatusCards(activeView(lastData), null);
      return;
    }

    const a = d.avg || {}, sig = d.sig || {};
    const chg = v => v == null ? '' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    const legCode = sig.holding === 'atk' ? '159552' : '512890';
    const leg = (d.etfs || []).find(e => e.code === legCode);

    chip.className = 'live-chip show';
    chip.innerHTML = `<span class="live-dot"></span><span class="mono">${d.time}</span>`
      + (a.value != null ? ` 均价 <b class="mono">${a.value.toFixed(2)}</b> <span class="mono ${a.chg_pct >= 0 ? 'up' : 'down'}">${chg(a.chg_pct)}</span>` : '')
      + (leg ? ` · ${leg.name} <span class="mono ${leg.chg_pct >= 0 ? 'up' : 'down'}">${chg(leg.chg_pct)}</span>` : '');

    const ib = sig.intraday_buy;
    if (ib && ib.status === 'triggered') {
      banner.className = 'sig-banner buy';
      banner.textContent = `【盘中分时买点 ${d.time} · ${ib.label}】${ib.hint}`;
      banner.dataset.live = '1';
    } else if (ib && ib.status === 'blocked_gap') {
      banner.className = 'sig-banner info';
      banner.textContent = `【盘中防陷阱拦截 ${d.time}】${ib.hint}`;
      banner.dataset.live = '1';
    } else if (ib && ib.status === 'wait_time') {
      banner.className = 'sig-banner info';
      banner.textContent = `【盘中预览 ${d.time} · ${ib.label}】${ib.hint}`;
      banner.dataset.live = '1';
    } else if (sig.note) {
      const cls = sig.preview === 'buy' ? 'buy' : (sig.preview === 'sell' ? 'sell' : 'info');
      banner.className = 'sig-banner ' + cls;
      let txt = `【盘中预览 ${d.time}】${sig.note}`;
      if (sig.turn_price != null && sig.preview === 'none') {
        txt += `（DIF 翻转临界均价 ${sig.turn_price}，还需 ${chg(sig.turn_pct)}）`;
      }
      banner.textContent = txt;
      banner.dataset.live = '1';
    }

    if (a.value != null && d.today) {
      const prices = {};
      for (const e of (d.etfs || [])) prices[e.code] = e.price;
      liveChart = { date: d.today, avg: a.value, ohlc: a.ohlc || null, etfs: prices, dif: sig.dif, dea: sig.dea };
      patchLiveChart();
    }
    if (lastData) renderStatusCards(activeView(lastData), d);
  }

  function patchLiveChart() {
    if (!charts.market || !lastData || !liveChart || !liveChart.date) return;
    const lc = liveChart;
    const D = lastData.dates.slice();
    const lastIsToday = D[D.length - 1] === lc.date;
    if (!lastIsToday) D.push(lc.date);

    const ext = (arr, v) => {
      const a = (arr || []).slice();
      if (!a.length) return [v];
      if (lastIsToday) a[a.length - 1] = v; else a.push(v);
      return a;
    };
    const px = code => (lc.etfs && lc.etfs[code] != null) ? lc.etfs[code] : null;
    const avgData = (klineMode && lastData.avg_k) ? ext(lastData.avg_k, lc.ohlc) : ext(lastData.avg, lc.avg);

    // 盘中同步更新 880003 走势上的拐点价 x_turn 虚线参考
    const liveIb = (lastLive && !lastLive.closed && lastLive.sig) ? lastLive.sig.intraday_buy : null;
    const xTurnVal = (liveIb && liveIb.eligible && liveIb.x_turn != null)
      ? Number(liveIb.x_turn)
      : ((lastData.intraday_buy && lastData.intraday_buy.eligible_buy && lastData.intraday_buy.x_turn != null)
          ? Number(lastData.intraday_buy.x_turn) : null);
    const avgMarkLine = xTurnVal != null ? {
      silent: true,
      symbol: 'none',
      data: [{ yAxis: xTurnVal }],
      lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.1 },
      label: {
        show: true,
        position: 'insideEndTop',
        color: '#f59e0b',
        fontSize: 10.5,
        fontFamily: 'monospace',
        formatter: `盘中拐点 ${xTurnVal.toFixed(2)}`
      }
    } : { data: [] };

    charts.market.setOption({
      xAxis: [{ data: D }, { data: D }],
      series: [
        { id: 'avg', data: avgData, markLine: avgMarkLine },
        { id: 'etf2000e', data: ext(lastData.etf2000e, px('159552')) },
        { id: 'etfdiv', data: ext(lastData.etfdiv, px('512890')) },
        { id: 'dif', data: ext(lastData.dif, lc.dif) },
        { id: 'dea', data: ext(lastData.dea, lc.dea) },
      ],
    }, { notMerge: false, lazyUpdate: true });
  }

  // ---------- 全量渲染与图表缩放 ----------
  function renderAll(data) {
    lastData = data;
    const view = activeView(data);
    $('err').style.display = 'none';
    renderFreshness(view);
    renderStrategyBar(data);
    renderStrategyCompareTable(data);
    renderSignalAlert(view);
    renderStatusCards(view, lastLive);
    renderKPIs(view);
    renderMarketChart(view);
    renderNavChart(data);
    renderReturns(view);
    renderAssetReturns(view);
    renderDrawdown(view);
    renderRolling(view);
    renderDaily(view);
    renderDist(view);
    renderLegStats(view);
    populateYearSelect(view);
    renderLedger(view);
    resizeAll();
    if (lastLive && !lastLive.closed) patchLive(lastLive);
  }

  function rerenderCharts() {
    if (!lastData) return;
    const view = activeView(lastData);
    renderStrategyBar(lastData);
    renderStrategyCompareTable(lastData);
    renderMarketChart(view);
    renderNavChart(lastData);
    renderDrawdown(view);
    renderRolling(view);
    renderDaily(view);
    renderDist(view);
    if (liveChart && lastLive && !lastLive.closed) patchLiveChart();
  }

  function resizeAll() {
    Object.values(charts).forEach(c => c && c.resize());
  }

  // ---------- 数据请求与更新轮询 ----------
  function loadData() {
    return fetch('data.json?_=' + Date.now()).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(data => {
      renderAll(data);
      return data;
    }).catch(e => {
      $('err').style.display = 'block';
      console.error(e.stack || e);
    });
  }

  function watchUpdate(t0) {
    const txt = $('updatedText');
    return new Promise(resolve => {
      const iv = setInterval(() => {
        fetch('/api/status?_=' + Date.now()).then(r => r.json()).then(st => {
          const p = st.progress || {};
          const secs = Math.round((Date.now() - t0) / 1000);
          if (st.running) {
            txt.textContent = `更新中 ${p.current ? p.current + ' ' : ''}(${p.done || 0}/${p.total || 9}) ${secs}s`;
          } else {
            const last = st.last || {};
            if (last.ok === undefined && Date.now() - t0 < 5000) return;
            clearInterval(iv);
            if (last.ok === false) txt.textContent = '更新失败';
            resolve(last);
          }
        }).catch(() => {});
      }, 500);
      setTimeout(() => { clearInterval(iv); resolve({ ok: true }); }, 180000);
    });
  }

  function triggerRefresh() {
    if (refreshing) return;
    refreshing = true;
    const btn = $('refreshBtn');
    const txt = $('updatedText');
    btn.classList.add('spinning');
    const done = () => { refreshing = false; btn.classList.remove('spinning'); };

    if (STATIC_HOST) {
      txt.textContent = '重新加载…';
      loadData().finally(done);
      return;
    }
    const t0 = Date.now();
    txt.textContent = '拉取行情…';
    fetch('/api/update?force=1').then(r => r.json()).then(res => {
      if (!res.ok) throw new Error(res.error || '未知错误');
      return watchUpdate(t0);
    }).then(() => loadData()).catch(e => {
      console.error(e);
      return loadData();
    }).finally(done);
  }

  function initAutoUpdate() {
    if (STATIC_HOST) {
      setInterval(() => { if (!document.hidden) loadData(); }, 5 * 60 * 1000);
      return;
    }
    fetch('/api/update').then(r => r.json()).then(res => {
      if (res.ok && res.started) {
        watchUpdate(Date.now()).then(() => loadData());
      } else if (res.ok && !res.skipped) {
        loadData();
      }
    }).catch(() => {});

    const poll = () => {
      fetch('/api/live?_=' + Date.now()).then(r => r.ok ? r.json() : null).then(d => {
        if (d) patchLive(d);
      }).catch(() => {});
    };
    poll();
    setInterval(() => { if (!document.hidden && !refreshing) poll(); }, 5000);
    // 切回标签页时立刻刷新一次实时行情，避免多等 5 秒
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && !refreshing) poll();
    });
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    // 主题切换
    applyTheme(currentTheme());
    $('themeBtn').addEventListener('click', () => {
      applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
      rerenderCharts();
    });

    // 刷新按钮
    $('refreshBtn').addEventListener('click', triggerRefresh);

    // 移动端导航折叠
    const toggle = $('navToggle');
    const nav = $('anchorNav');
    const setOpen = open => {
      nav.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', String(open));
    };
    toggle.addEventListener('click', () => setOpen(!nav.classList.contains('open')));
    nav.addEventListener('click', e => { if (e.target.tagName === 'A') setOpen(false); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && nav.classList.contains('open')) setOpen(false); });

    // 滚动监听（Scroll-spy）
    const navLinks = [...nav.querySelectorAll('a')];
    const sections = navLinks.map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
    if ('IntersectionObserver' in window && sections.length) {
      const io = new IntersectionObserver(entries => {
        for (const en of entries) {
          if (en.isIntersecting) {
            const id = '#' + en.target.id;
            navLinks.forEach(a => a.classList.toggle('on', a.getAttribute('href') === id));
          }
        }
      }, { rootMargin: '-18% 0px -68% 0px', threshold: 0 });
      sections.forEach(s => io.observe(s));
    }

    // 四策略切换（顶部卡片条 + 05区对比总表点击联动）
    const stratBar = $('stratBar');
    if (stratBar) {
      stratBar.addEventListener('click', e => {
        const b = e.target.closest('[data-strat]');
        if (!b) return;
        selectStrategy(b.dataset.strat);
      });
    }
    const compareTbody = $('stratCompareTbody');
    if (compareTbody) {
      compareTbody.addEventListener('click', e => {
        const tr = e.target.closest('tr[data-strat]');
        if (!tr) return;
        selectStrategy(tr.dataset.strat);
      });
    }

    // 自定义区间收益
    $('quickDays').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      customMode = 'days';
      $('kpiDays').value = b.dataset.d;
      if (lastData) updateCustomKPI(activeView(lastData));
    });
    $('kpiDays').addEventListener('input', () => {
      customMode = 'days';
      if (lastData) updateCustomKPI(activeView(lastData));
    });
    $('kpiDate').addEventListener('change', () => {
      customMode = 'date';
      if (lastData) updateCustomKPI(activeView(lastData));
    });

    // 走势图区间 & 折线/K线
    $('marketRange').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      marketRangeDays = Number(b.dataset.days);
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      if (lastData && charts.market) {
        const start = calcZoomStart(lastData.dates.length, marketRangeDays);
        charts.market.dispatchAction({ type: 'dataZoom', start, end: 100 });
      }
    });
    $('klineToggle').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      klineMode = b.dataset.mode === 'k';
      for (const x of b.parentElement.children) {
        const active = x === b;
        x.classList.toggle('on', active);
        x.setAttribute('aria-pressed', String(active));
      }
      if (lastData) {
        renderMarketChart(activeView(lastData));
        if (liveChart && lastLive && !lastLive.closed) patchLiveChart();
      }
    });

    // 净值图区间 & 线性/对数
    $('navRange').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      navRangeDays = Number(b.dataset.days);
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      if (lastData && charts.nav) {
        const start = calcZoomStart(lastData.dates.length, navRangeDays);
        charts.nav.dispatchAction({ type: 'dataZoom', start, end: 100 });
      }
    });
    $('navScaleToggle').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      navLogScale = b.dataset.scale === 'log';
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      if (lastData) renderNavChart(lastData);
    });

    // 月度收益表视图切换
    $('retViewToggle').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      const isStrat = b.dataset.view === 'strat';
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      $('retTable').style.display = isStrat ? '' : 'none';
      $('assetRetTable').style.display = isStrat ? 'none' : '';
      $('retCardTitle').textContent = isStrat ? '月度 / 年度收益热力表' : '进攻 / 防守买入持有收益对比';
      $('retCardSub').textContent = isStrat
        ? '小字为当期 进攻 / 防守 乘积贡献（切换日归入新仓位）'
        : '每格 = 进攻(橙) / 防守(蓝) 买入持有收益 · 不含轮动信号与交易费用';
    });

    // 交易台账切换 & 筛选
    $('ledgerModeToggle').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      ledgerMode = b.dataset.mode;
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      $('legsWrap').style.display = ledgerMode === 'legs' ? '' : 'none';
      $('sigsWrap').style.display = ledgerMode === 'signals' ? '' : 'none';
    });
    $('legFilter').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      legFilter = b.dataset.f;
      for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
      if (lastData) renderLedger(activeView(lastData));
    });
    $('ledgerYear').addEventListener('change', e => {
      ledgerYear = e.target.value;
      if (lastData) renderLedger(activeView(lastData));
    });

    // 窗口缩放
    let raf = 0;
    window.addEventListener('resize', () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = 0; resizeAll(); });
    });
  }

  // ---------- 启动 ----------
  bindEvents();
  loadData();
  initAutoUpdate();
})();
