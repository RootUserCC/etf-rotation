/* 监控仪表盘：KPI、走势总览、收益/风险图表、盘中实时 */
'use strict';

Site.injectLayout('dashboard', { live: true, refresh: true });

let chart = null;
let dailyChart = null;
let ddChart = null;
let rollChart = null;
let klineMode = false;   // 走势总览：false=折线，true=K线（平均股价）
let liveChart = null;    // 盘中临时点
let lastLive = null;

// ===== 标签页 =====
const tabBar = document.getElementById('tabBar');
function activateTab(name) {
  for (const b of tabBar.children) b.classList.toggle('on', b.dataset.tab === name);
  document.querySelectorAll('.tab-panel').forEach(p => { p.hidden = p.dataset.tab !== name; });
  requestAnimationFrame(() => {
    if (name === 'overview' && chart) chart.resize();
    if (name === 'risk') [dailyChart, ddChart, rollChart].forEach(c => c && c.resize());
  });
}
tabBar.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b) activateTab(b.dataset.tab);
});

function resizeAll() {
  for (const c of [chart, dailyChart, ddChart, rollChart]) if (c) c.resize();
}
window.addEventListener('resize', resizeAll);

document.getElementById('klineToggle').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  klineMode = btn.dataset.mode === 'k';
  for (const b of btn.parentElement.children) b.classList.toggle('on', b === btn);
  if (Site.lastData) render(Site.lastData);
});

document.addEventListener('site:themechange', () => {
  if (!Site.lastData) return;
  render(Site.lastData); renderDaily(Site.lastData); renderRisk(Site.lastData); renderRolling(Site.lastData);
});

// ===== 数据加载 =====
function loadAll(force) {
  return Site.loadData(force).then(data => {
    document.getElementById('err').style.display = 'none';
    renderMemo(data);
    renderKPIs(data);
    render(data);
    renderReturns(data);
    renderAssetReturns(data);
    renderDaily(data);
    renderSignalAlert(data);
    renderRisk(data);
    renderRolling(data);
    renderLegStats(data);
    resizeAll();
    if (lastLive) patchLive(lastLive);   // loadData 会重置信号横幅，这里补回盘中预览
    return data;
  }).catch(e => {
    document.getElementById('err').style.display = 'block';
    console.error(e.stack || e);
  });
}

loadAll();
Site.autoUpdate(() => loadAll(true));
Site.bindRefresh(() => loadAll(true));
Site.pollLive(patchLive);

// ===== 盘中实时 =====
function patchLive(d) {
  lastLive = d;
  const badge = document.getElementById('liveBadge');
  const banner = document.getElementById('sigBanner');
  if (d.closed) {
    if (badge) { badge.textContent = ''; badge.style.color = ''; }
    if (banner.dataset.live) { banner.className = 'sig-banner'; banner.textContent = ''; delete banner.dataset.live; }
    return;
  }
  const a = d.avg || {};
  const sig = d.sig || {};
  const chg = v => v == null ? '' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  // 当前持仓标的：进攻仓取 159552（2024-06 后），防守仓取 512890
  const legCode = sig.holding === 'atk' ? '159552' : '512890';
  const leg = (d.etfs || []).find(e => e.code === legCode);
  if (badge) {
    badge.innerHTML = `<span class="live-dot"></span>盘中 ${d.time} · 平均股价 `
      + (a.value != null ? `<b>${a.value.toFixed(2)}</b> ` : '-- ')
      + (a.chg_pct != null ? `<span style="color:${a.chg_pct >= 0 ? '#e5484d' : '#0f9d58'}">${chg(a.chg_pct)}</span>` : '')
      + (leg ? ` · ${leg.name} <span style="color:${leg.chg_pct >= 0 ? '#e5484d' : '#0f9d58'}">${chg(leg.chg_pct)}</span>` : '');
    badge.style.color = d.stale ? '#e5484d' : '';
  }
  if (sig.note) {
    banner.className = 'sig-banner ' + (sig.preview === 'buy' ? 'buy' : (sig.preview === 'sell' ? 'sell' : 'info'));
    let txt = '【盘中预览】' + sig.note;
    if (sig.turn_price != null && sig.preview === 'none') {
      txt += `（DIF 翻转线 ${sig.turn_price}，还需 ${chg(sig.turn_pct)}）`;
    }
    banner.textContent = txt;
    banner.dataset.live = '1';
  }
  // 图表叠加"今天"的临时点
  if (a.value != null && d.today) {
    const prices = {};
    for (const e of (d.etfs || [])) prices[e.code] = e.price;
    liveChart = {
      date: d.today, avg: a.value, ohlc: a.ohlc || null,
      etfs: prices, dif: sig.dif, dea: sig.dea,
    };
    patchLiveChart();
  }
  applyLiveToStatus(d);
}

// 顶部摘要用实时值重画（只在副本上改，不动收盘序列）
function applyLiveToStatus(d) {
  const lastData = Site.lastData;
  if (!lastData || !d || d.closed) return;
  const a = d.avg || {}, sig = d.sig || {};
  const copy = Object.assign({}, lastData);
  copy.avg = (lastData.avg || []).slice();
  copy.dif = (lastData.dif || []).slice();
  copy.dea = (lastData.dea || []).slice();
  copy.legs = (lastData.legs || []).map(l => Object.assign({}, l));
  const N = copy.avg.length - 1;
  if (a.value != null) copy.avg[N] = a.value;
  if (sig.dif != null) copy.dif[N] = sig.dif;
  if (sig.dea != null) copy.dea[N] = sig.dea;
  const last = copy.legs[copy.legs.length - 1];
  if (last && last.open && last.buy_price) {
    const code = last.asset === '1000' ? '159552' : '512890';
    const e = (d.etfs || []).find(x => x.code === code)
           || (d.etfs || []).find(x => x.code === '512100');
    if (e) last.ret = e.price / last.buy_price - 1 - 0.0002;
  }
  Site.renderStatusPill(copy);
}

// 盘中图表：把"今天"的临时点叠到图上
function patchLiveChart() {
  const lastData = Site.lastData;
  if (!chart || !lastData || !liveChart || !liveChart.date) return;
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
  const avgData = (klineMode && lastData.avg_k)
    ? ext(lastData.avg_k, lc.ohlc)
    : ext(lastData.avg, lc.avg);

  chart.setOption({
    xAxis: [{ data: D }, { data: D }],
    series: [
      { id: 'avg', data: avgData },
      { id: 'etf2000e', data: ext(lastData.etf2000e, px('159552')) },
      { id: 'etfdiv', data: ext(lastData.etfdiv, px('512890')) },
      { id: 'dif', data: ext(lastData.dif, lc.dif) },
      { id: 'dea', data: ext(lastData.dea, lc.dea) },
    ],
  }, { notMerge: false, lazyUpdate: true });
}

// ===== 策略备忘（上次换仓 + 防守择优） =====
function renderMemo(data) {
  const lastTrade = data.trades[data.trades.length - 1];
  const lastSig = lastTrade ? lastTrade.date : '-';
  const defName = c => c === '159201' ? '自由现金流' : '红利低波';
  let dpTxt = '';
  const dp = data.defense_pick;
  if (dp) {
    const m1 = dp.mom_890 == null ? 'NA' : (dp.mom_890 * 100).toFixed(2) + '%';
    const m2 = dp.mom_201 == null ? 'NA' : (dp.mom_201 * 100).toFixed(2) + '%';
    dpTxt = dp.in_defense
      ? `　|　防守择优: 持有${defName(dp.holding_def)}（近20日 红利低波${m1} / 现金流${m2}）`
      : `　|　防守择优: 若切防守选${defName(dp.next_pick)}（近20日 红利低波${m1} / 现金流${m2}，截至${dp.mom_date}）`;
  }
  document.getElementById('strategyMemo').innerHTML = `上次换仓 ${lastSig}${dpTxt}`;
}

// ===== KPI 指标卡片 =====
function renderKPIs(data) {
  const nav = data.nav_strat, N = nav.length - 1;
  const s = Site.calcStats(data);

  setKPI('kpiCum', (s.cumRet * 100).toFixed(2) + '%', s.cumRet > 0 ? 'pos' : 'down',
    'kpiCumSub', `起止 ${data.dates[0]} → ${data.dates[N]}`);
  setKPI('kpiMDD', (s.mdd * 100).toFixed(2) + '%', 'down',
    'kpiMDDSub', `峰 ${data.dates[s.pDate]} · 水下 ${s.uwMax} 日`);
  setKPI('kpiAnn', (s.annRet * 100).toFixed(2) + '%', s.annRet > 0 ? 'pos' : 'down',
    'kpiAnnSub', `${s.tradingDays} 个交易日 · Calmar ${s.calmar.toFixed(2)}`);
  setKPI('kpiCurDD', (s.curDD * 100).toFixed(2) + '%', s.curDD < 0 ? 'down' : 'pos',
    'kpiCurDDSub', `距高点 ${data.dates[s.curIdx]} (${((N - s.curIdx) / 252 * 12).toFixed(0)}月)`);

  updateCustomKPI(data);

  setKPI('kpiYTD', (s.ytd >= 0 ? '+' : '') + (s.ytd * 100).toFixed(2) + '%', s.ytd >= 0 ? 'pos' : 'down',
    'kpiYTDSub', `${data.dates[s.iY]} → ${data.dates[N]}`);

  setKPI('kpiEx', (s.exAnn >= 0 ? '+' : '') + (s.exAnn * 100).toFixed(2) + '%', s.exAnn >= 0 ? 'pos' : 'down',
    'kpiExSub', `年化超额 · 累计超额 ${((s.cumRet - s.holdCum) * 100).toFixed(0)}pp vs 1000持有`);

  // 信号状态：DIF 零上/零下 + 连续升/降天数
  const dif = data.dif;
  let upDays = 0, downDays = 0;
  for (let i = N; i > 0; i--) {
    if (dif[i] > dif[i - 1]) { if (downDays) break; upDays++; }
    else if (dif[i] < dif[i - 1]) { if (upDays) break; downDays++; }
    else break;
  }
  const below = dif[N] < 0;
  const streak = upDays ? `连升${upDays}天` : downDays ? `连降${downDays}天` : '走平';
  let sigHint = `DIF ${dif[N].toFixed(3)}`;
  if (below && upDays) sigHint += ' · 零下回升，留意买点';
  else if (downDays) sigHint += ' · 拐头向下即卖出信号';
  setKPI('kpiSig', `${below ? '零下' : '零上'} ${streak}`, downDays ? 'down' : 'pos',
    'kpiSigSub', sigHint);

  setKPI('kpiUpd', data.updated || '-', '', 'kpiUpdSub', `数据截至 ${data.dates[N]}`);

  function setKPI(id, val, cls, subId, subText) {
    const el = document.getElementById(id);
    const subEl = document.getElementById(subId);
    el.textContent = val;
    el.className = 'value ' + cls;
    if (subEl) subEl.textContent = subText;
  }
}

// ===== 自定义区间收益 =====
let customMode = 'days';
function updateCustomKPI(data) {
  const nav = data.nav_strat, N = nav.length - 1;
  const daysInput = document.getElementById('kpiDays');
  const dateInput = document.getElementById('kpiDate');
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
  const ret = nav[N] / nav[i0] - 1;
  const el = document.getElementById('kpiCustom');
  el.textContent = (ret >= 0 ? '+' : '') + (ret * 100).toFixed(2) + '%';
  el.className = 'value ' + (ret >= 0 ? 'pos' : 'down');
  document.getElementById('kpiCustomSub').textContent = `${data.dates[i0]} → ${data.dates[N]}`;
}
document.getElementById('kpiDays').addEventListener('input', () => {
  customMode = 'days';
  if (Site.lastData) updateCustomKPI(Site.lastData);
});
document.getElementById('kpiDate').addEventListener('change', () => {
  customMode = 'date';
  if (Site.lastData) updateCustomKPI(Site.lastData);
});

// ===== 月度/年度收益表 =====
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
    const is1000 = holdSet.has(D[i]);
    for (const k of [D[i].slice(0, 7), D[i].slice(0, 4), 'ALL']) {
      if (!agg[k]) agg[k] = { p1000: 1, pdiv: 1 };
      if (is1000) agg[k].p1000 *= f; else agg[k].pdiv *= f;
    }
  }

  const monthLast = {}, yearLast = {};
  D.forEach((d, i) => {
    monthLast[d.slice(0, 7)] = nav[i];
    yearLast[d.slice(0, 4)] = nav[i];
  });
  const mRet = {};
  let prev = 1;
  Object.keys(monthLast).sort().forEach(m => {
    mRet[m] = monthLast[m] / prev - 1;
    prev = monthLast[m];
  });
  const years = Object.keys(yearLast).sort();
  const yRet = {};
  prev = 1;
  years.forEach(y => {
    yRet[y] = yearLast[y] / prev - 1;
    prev = yearLast[y];
  });

  const fmtContrib = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
  const brkHtml = key => {
    const a = agg[key];
    if (!a) return '';
    const c1000 = a.p1000 - 1, cdiv = a.pdiv - 1;
    return `<div class="brk"><span class="${c1000 < 0 ? 'neg' : ''}">进攻 ${fmtContrib(c1000)}</span>` +
           `<span class="${cdiv < 0 ? 'neg' : ''}">防守 ${fmtContrib(cdiv)}</span></div>`;
  };
  const cell = (key, v) => {
    if (v == null) return '<td>-</td>';
    const intensity = Math.min(0.38, Math.abs(v) * 2.2);
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
  html += `<tr class="sum-row"><td class="year">汇总</td>${'<td></td>'.repeat(12)}` +
          contribTd(totalRet) + contribTd(A.p1000 - 1) + contribTd(A.pdiv - 1) + '</tr>';
  html += '</tbody>';
  document.getElementById('retTable').innerHTML = html;
}

// ===== 进攻/防守买入持有收益 =====
function renderAssetReturns(data) {
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

  const atkStats = periodStats(atkNav, D);
  const divStats = periodStats(divNav, D);

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
  html += `<tr class="sum-row"><td class="year">汇总</td>${'<td></td>'.repeat(12)}${pairCell(atkStats.total, divStats.total)}</tr>`;
  html += '</tbody>';
  document.getElementById('assetRetTable').innerHTML = html;

  function periodStats(values, dates) {
    const monthLast = {}, yearLast = {};
    dates.forEach((d, i) => {
      monthLast[d.slice(0, 7)] = values[i];
      yearLast[d.slice(0, 4)] = values[i];
    });
    const mRet = {};
    let prev = values[0];
    Object.keys(monthLast).sort().forEach(m => {
      mRet[m] = monthLast[m] / prev - 1;
      prev = monthLast[m];
    });
    const years = Object.keys(yearLast).sort();
    const yRet = {};
    prev = values[0];
    years.forEach(y => {
      yRet[y] = yearLast[y] / prev - 1;
      prev = yearLast[y];
    });
    return { mRet, years, yRet, total: values[values.length - 1] / values[0] - 1 };
  }

  function pairCell(atk, div) {
    if (atk == null || div == null) return '<td>-</td>';
    const value = (v, cls) => `<span class="${cls}">${(v * 100).toFixed(2)}%</span>`;
    const bg = v => {
      const intensity = Math.min(0.38, Math.abs(v) * 2.2);
      const color = v < 0 ? 'var(--down)' : 'var(--up)';
      return `color-mix(in srgb, ${color} ${(intensity * 100).toFixed(0)}%, transparent)`;
    };
    return `<td class="pair" style="--atk-bg: ${bg(atk)}; --div-bg: ${bg(div)}">`
         + `${value(atk, 'atk')}<span class="sep">/</span>${value(div, 'div')}</td>`;
  }
}

// ===== 每日收益 =====
function renderDaily(data) {
  const T = Site.chartTheme();
  if (!dailyChart) dailyChart = echarts.init(document.getElementById('dailyChart'));
  const D = data.dates, nav = data.nav_strat;
  const rets = [null];
  for (let i = 1; i < D.length; i++) rets.push(+((nav[i] / nav[i - 1] - 1) * 100).toFixed(2));

  const holdArr = new Array(D.length).fill('红利低波');
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
  document.getElementById('dailyStats').textContent =
    `策略净值日收益（含费用）· 共 ${vals.length} 个交易日 · 胜率 ${(win / vals.length * 100).toFixed(1)}%` +
    ` · 日均 ${avg >= 0 ? '+' : ''}${avg.toFixed(2)}% · 单日最大 +${mx.toFixed(2)}% / ${mn.toFixed(2)}%`;

  dailyChart.setOption({
    animation: false,
    textStyle: { color: T.text },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: T.tooltipBg,
      borderColor: T.tooltipBorder,
      borderWidth: 1,
      formatter: params => {
        const p = params && params[0];
        if (!p) return '';
        const i = p.dataIndex, v = rets[i];
        if (v == null) return D[i];
        const c = v >= 0 ? T.buy : T.sell;
        return `<div style="font-weight:600;margin-bottom:4px">${D[i]}</div>`
             + `<div>日收益: <span style="color:${c};font-weight:600">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</span></div>`
             + `<div>持仓: ${holdArr[i]}</div>`
             + `<div>净值: ${nav[i].toFixed(4)}</div>`;
      }
    },
    grid: { left: 62, right: 30, top: 16, bottom: 46 },
    xAxis: { type: 'category', data: D,
      axisLine: { lineStyle: { color: T.axisLine } },
      axisLabel: { color: T.axisText } },
    yAxis: { type: 'value', scale: true,
      axisLabel: { color: T.axisText, formatter: '{value}%' },
      splitLine: { lineStyle: { color: T.splitLine } } },
    dataZoom: [
      { type: 'inside', start: 85, end: 100 },
      { type: 'slider', bottom: 8, height: 16, start: 85, end: 100,
        borderColor: T.axisLine, backgroundColor: 'transparent',
        fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
        moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText },
        dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } } }
    ],
    series: [{
      name: '日收益', type: 'bar', data: rets,
      itemStyle: { color: p => (p.value == null || p.value >= 0) ? T.buy : T.sell },
      large: true, largeThreshold: 400
    }]
  }, true);
  dailyChart.resize();
}

// ===== 信号预警 =====
function renderSignalAlert(data) {
  const el = document.getElementById('sigBanner');
  el.className = 'sig-banner';
  el.textContent = '';
  delete el.dataset.live;
  const D = data.dates, N = D.length - 1, dif = data.dif;
  if (N < 2) return;
  const d2 = dif[N - 2], d1 = dif[N - 1], d0 = dif[N];
  if (d2 == null || d1 == null || d0 == null) return;
  const last = data.legs[data.legs.length - 1];
  const holding = last && last.asset === '1000' ? 'atk' : 'div';
  // 方案C：买入 = DIF 零下拐头向上；卖出 = DIF 拐头向下（任意位置）
  const buySig = d1 < d2 && d0 > d1 && d0 < 0;
  const sellSig = d1 > d2 && d0 < d1;
  if (buySig && holding === 'div') {
    el.className = 'sig-banner buy';
    el.textContent = `⚠️ ${D[N]} 收盘触发买入信号（DIF 零下拐头向上）→ 下个交易日开盘：卖出红利低波ETF，买入进攻仓`;
  } else if (sellSig && holding === 'atk') {
    el.className = 'sig-banner sell';
    const dp = data.defense_pick;
    const defName = c => c === '159201' ? '自由现金流ETF(159201)' : '红利低波ETF(512890)';
    const pickTxt = dp && dp.next_pick ? defName(dp.next_pick) : '红利低波ETF';
    el.textContent = `⚠️ ${D[N]} 收盘触发卖出信号（DIF 拐头向下）→ 下个交易日开盘：卖出进攻仓，买入${pickTxt}`;
  }
}

// ===== 回撤曲线 =====
function renderRisk(data) {
  const T = Site.chartTheme();
  if (!ddChart) ddChart = echarts.init(document.getElementById('ddChart'));
  const D = data.dates, nav = data.nav_strat;

  const dd = [];
  let peak = -Infinity, curPeakDate = D[0], mdd = 0, mddDate = D[0], mddPeakDate = D[0];
  for (let i = 0; i < D.length; i++) {
    if (nav[i] > peak) { peak = nav[i]; curPeakDate = D[i]; }
    const v = +((nav[i] / peak - 1) * 100).toFixed(2);
    dd.push(v);
    if (v < mdd) { mdd = v; mddDate = D[i]; mddPeakDate = curPeakDate; }
  }
  document.getElementById('ddStats').textContent =
    `净值回撤曲线 · 最大回撤 ${mdd.toFixed(2)}%（${mddPeakDate} 见顶 → ${mddDate} 见底）`;

  ddChart.setOption({
    animation: false,
    textStyle: { color: T.text },
    tooltip: { trigger: 'axis',
      backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
      axisPointer: { type: 'line', lineStyle: { color: T.cross } },
      formatter: ps => {
        const p = ps && ps[0];
        return p ? `${p.axisValue}<br>回撤: <b style="color:${T.sell}">${p.value.toFixed(2)}%</b>` : '';
      } },
    grid: { left: 52, right: 20, top: 26, bottom: 26 },
    xAxis: { type: 'category', data: D, boundaryGap: false,
      axisLine: { lineStyle: { color: T.axisLine } },
      axisLabel: { color: T.axisText } },
    yAxis: { type: 'value', max: 0,
      axisLabel: { color: T.axisText, formatter: '{value}%' },
      splitLine: { lineStyle: { color: T.splitLine } } },
    dataZoom: [{ type: 'inside' }],
    series: [{
      name: '回撤', type: 'line', data: dd, symbol: 'none',
      lineStyle: { width: 1, color: T.sell },
      itemStyle: { color: T.sell },
      areaStyle: { color: T.areaDiv, opacity: 0.9 },
      sampling: 'lttb', large: true, largeThreshold: 400,
      markLine: { silent: true, symbol: 'none',
        data: [{ yAxis: mdd }],
        lineStyle: { color: T.sell, type: 'dashed', width: 0.8 },
        label: { color: T.sell, fontSize: 10, formatter: mdd.toFixed(1) + '%' } }
    }]
  }, true);
}

// ===== 滚动指标：250 日滚动超额收益 =====
function renderRolling(data) {
  const T = Site.chartTheme();
  if (!rollChart) rollChart = echarts.init(document.getElementById('rollChart'));
  const D = data.dates, nav = data.nav_strat, n = D.length;

  const swIdx = D.indexOf(data.switch_atk);
  const atkH = data.etf1000_hfq, e2 = data.etf2000e_hfq, divH = data.etfdiv_hfq;
  const atkNav = [1], divNav = [1];
  for (let i = 1; i < n; i++) {
    let r;
    if (swIdx < 0 || i <= swIdx || e2[i] == null || e2[i - 1] == null) {
      r = atkH[i] / atkH[i - 1] - 1;
    } else {
      r = e2[i] / e2[i - 1] - 1;
    }
    atkNav.push(atkNav[i - 1] * (1 + r));
    divNav.push(divNav[i - 1] * divH[i] / divH[i - 1]);
  }
  const halfNav = atkNav.map((v, i) => 0.5 * v + 0.5 * divNav[i]);

  const W = 250;
  const excess = base => nav.map((_, i) => {
    if (i < W || base[i - W] == null) return null;
    const rs = nav[i] / nav[i - W] - 1, rb = base[i] / base[i - W] - 1;
    return +((rs - rb) * 100).toFixed(1);
  });
  const exAtk = excess(atkNav), exHalf = excess(halfNav);

  rollChart.setOption({
    animation: false,
    textStyle: { color: T.text },
    tooltip: {
      trigger: 'axis',
      backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
      valueFormatter: v => (v == null ? '-' : (v > 0 ? '+' : '') + v + 'pp')
    },
    legend: { data: ['超额 vs 进攻仓持有', '超额 vs 50/50组合'], top: 4,
      textStyle: { color: T.muted, fontSize: 12 }, itemWidth: 16, itemHeight: 10 },
    grid: { left: 52, right: 30, top: 36, bottom: 46 },
    xAxis: { type: 'category', data: D, boundaryGap: false,
      axisLine: { lineStyle: { color: T.axisLine } },
      axisLabel: { color: T.axisText } },
    yAxis: { type: 'value', name: '超额(pp)', scale: true,
      nameTextStyle: { color: T.muted },
      axisLabel: { color: T.axisText },
      splitLine: { lineStyle: { color: T.splitLine } } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 8, height: 16, start: 0, end: 100,
        borderColor: T.axisLine, backgroundColor: 'transparent',
        fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
        moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText },
        dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } } }
    ],
    series: [
      { name: '超额 vs 进攻仓持有', type: 'line', data: exAtk,
        symbol: 'none', lineStyle: { width: 1.3, color: '#4d7cfe' },
        itemStyle: { color: '#4d7cfe' }, sampling: 'lttb',
        markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }],
          lineStyle: { color: T.axisText, type: 'dashed', width: 0.8 },
          label: { show: false } } },
      { name: '超额 vs 50/50组合', type: 'line', data: exHalf,
        symbol: 'none', lineStyle: { width: 1.3, color: '#7a5cff' },
        itemStyle: { color: '#7a5cff' }, sampling: 'lttb' }
    ]
  }, true);
}

// ===== 持仓段统计 =====
function renderLegStats(data) {
  const legs = data.legs || [];
  const closed = legs.filter(l => !l.open);
  const openLegs = legs.filter(l => l.open);
  const fmtPct = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%';

  const wins = closed.filter(l => l.ret > 0);
  const losses = closed.filter(l => l.ret <= 0);
  const sumW = wins.reduce((a, l) => a + l.ret, 0);
  const sumL = losses.reduce((a, l) => a + l.ret, 0);
  const avgW = wins.length ? sumW / wins.length : 0;
  const avgL = losses.length ? sumL / losses.length : 0;
  const best = closed.reduce((a, l) => (l.ret > a.ret ? l : a), closed[0]);
  const worst = closed.reduce((a, l) => (l.ret < a.ret ? l : a), closed[0]);
  const avgDays = closed.length
    ? closed.reduce((a, l) => a + l.days, 0) / closed.length : 0;

  const item = (k, v, cls, s) =>
    `<div class="stat-item"><div class="k">${k}</div>` +
    `<div class="v ${cls || ''}">${v}</div><div class="s">${s || ''}</div></div>`;

  document.getElementById('legStats').innerHTML = [
    item('已了结段数', closed.length, '', openLegs.length ? `另有 ${openLegs.length} 段持有中` : ''),
    item('段胜率', (wins.length / closed.length * 100).toFixed(1) + '%', '',
      `盈利 ${wins.length} 段 / 亏损 ${losses.length} 段`),
    item('平均盈利段', fmtPct(avgW), 'pos', ''),
    item('平均亏损段', fmtPct(avgL), 'neg', '单次换错的历史平均代价'),
    item('盈亏比', (avgW / Math.abs(avgL)).toFixed(2), '', '平均盈利 ÷ 平均亏损'),
    item('利润因子', (sumW / Math.abs(sumL)).toFixed(2), '', '总盈利 ÷ 总亏损'),
    item('最差一段', fmtPct(worst.ret), 'neg', `${worst.buy_date} → ${worst.sell_date}`),
    item('最好一段', fmtPct(best.ret), 'pos', `${best.buy_date} → ${best.sell_date}`),
    item('平均持有', avgDays.toFixed(0) + ' 个交易日', '', '每段买入→卖出'),
  ].join('');
}

// ===== 走势图 =====
function render(data) {
  const T = Site.chartTheme();
  if (!chart) chart = echarts.init(document.getElementById('chart'));
  const narrow = window.innerWidth < 640;
  const D = data.dates;

  // 换仓点 markPoint（画在平均股价线上；买进攻 ⇄ 卖红利低波是同一操作，每处只标一次）
  const buyLabel = t => (t.asset_name && t.asset_name.indexOf('2000') >= 0) ? '买2000增' : '买1000';
  const marks = [];
  for (const t of data.trades) {
    const i = D.indexOf(t.date);
    if (i < 0) continue;
    const isBuy = t.action === 'buy';
    marks.push({
      coord: [t.date, data.avg[i]],
      value: isBuy ? buyLabel(t) : '买红利低波',
      symbol: isBuy ? 'triangle' : 'arrow',
      symbolRotate: isBuy ? 0 : 180,
      symbolSize: 13,
      itemStyle: { color: isBuy ? T.buy : T.sell },
      label: { show: false }
    });
  }

  // 持仓区间 markArea：红=持有进攻仓，绿=持有红利低波ETF
  const areas = data.legs.map(l => {
    const end = l.sell_date || D[D.length - 1];
    const is1000 = l.asset === '1000';
    return [
      { xAxis: l.buy_date,
        itemStyle: { color: is1000 ? T.area : T.areaDiv },
        label: { show: false } },
      { xAxis: end }
    ];
  });

  // DIF 拐点标注：触发换仓的拐点 + 未触发的背景小灰点
  const sigIdx = new Set(data.trades.map(t => D.indexOf(t.sig_date)).filter(i => i >= 0));
  const difBgMarks = [];
  for (let i = 1; i < D.length - 1; i++) {
    const d0 = data.dif[i - 1], d1 = data.dif[i], d2 = data.dif[i + 1];
    if (d0 == null || d1 == null || d2 == null) continue;
    if (sigIdx.has(i)) continue;
    const isMax = d0 < d1 && d1 > d2, isMin = d0 > d1 && d1 < d2;
    if (isMax || isMin) {
      difBgMarks.push({ coord: [D[i], d1], symbol: 'circle', symbolSize: 3.5,
        itemStyle: { color: T.axisText, opacity: 0.45 }, label: { show: false } });
    }
  }
  const difMarks = data.trades.map(t => {
    const i = D.indexOf(t.sig_date);
    if (i < 0) return null;
    const isBuy = t.action === 'buy';
    return {
      coord: [t.sig_date, data.dif[i]],
      value: isBuy ? buyLabel(t) : '买红利低波',
      symbol: isBuy ? 'triangle' : 'arrow',
      symbolRotate: isBuy ? 0 : 180,
      symbolSize: 10,
      itemStyle: { color: isBuy ? T.buy : T.sell },
      label: { show: false }
    };
  }).filter(Boolean).concat(difBgMarks);

  // MACD 四色柱（通达信口径）
  const cRed = [], cPink = [], cGreen = [], cYellow = [];
  let prevM = null;
  for (let i = 0; i < D.length; i++) {
    const m = (data.dif[i] == null || data.dea[i] == null)
      ? null : (data.dif[i] - data.dea[i]) * 2;
    let r = null, p = null, g = null, y = null;
    if (m != null) {
      if (m >= 0) {
        if (prevM != null && prevM > 0 && m < prevM) p = m; else r = m;
      } else {
        if (prevM != null && prevM < 0 && m > prevM) y = m; else g = m;
      }
    }
    cRed.push(r); cPink.push(p); cGreen.push(g); cYellow.push(y);
    prevM = m;
  }

  const option = {
    animation: false,
    textStyle: { color: T.text },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', lineStyle: { color: T.cross }, crossStyle: { color: T.cross } },
      backgroundColor: T.tooltipBg,
      borderColor: T.tooltipBorder,
      borderWidth: 1,
      valueFormatter: v => (v == null ? '-' : v),
      formatter: params => {
        const date = params && params.length ? params[0].axisValue : '';
        const i = D.indexOf(date);
        if (i < 0) {
          // 盘中临时点
          if (!liveChart || date !== liveChart.date) return date;
          const lc = liveChart;
          let html = `<div style="font-weight:600;margin-bottom:4px">${date} `
                   + `<span style="color:#f5a623;font-size:11px">盘中</span></div>`;
          if (lc.ohlc) {
            const [o, c, lo, hi] = lc.ohlc;
            const cc = c >= o ? T.buy : T.sell;
            html += `<div style="color:${cc}">开 ${o.toFixed(2)}　高 ${hi.toFixed(2)}　低 ${lo.toFixed(2)}　收 <b>${c.toFixed(2)}</b></div>`;
          } else if (lc.avg != null) {
            html += `<div>平均股价 <b>${lc.avg.toFixed(2)}</b></div>`;
          }
          for (const [name, code, color, dp] of [
            ['中证2000增强', '159552', T.etf2000e, 3],
            ['红利低波ETF', '512890', T.etfdiv, 3]]) {
            const v = lc.etfs ? lc.etfs[code] : null;
            if (v != null) html += `<div style="color:${color}">${name} ${v.toFixed(dp)}</div>`;
          }
          html += `<div style="margin-top:4px;color:${T.muted}">DIF ${lc.dif == null ? '-' : lc.dif.toFixed(4)}`
                + `　DEA ${lc.dea == null ? '-' : lc.dea.toFixed(4)}</div>`;
          return html;
        }
        const pct = (arr, base) => {
          if (i <= 0 || arr[i] == null || base[i - 1] == null) return '';
          const p = (base[i] / base[i - 1] - 1) * 100;
          const c = p >= 0 ? T.buy : T.sell;
          return ` <span style="color:${c}">${p >= 0 ? '+' : ''}${p.toFixed(2)}%</span>`;
        };
        const rows = [
          ['平均股价', data.avg, data.avg, T.avg, 2],
          ['中证2000增强', data.etf2000e, data.etf2000e_hfq, T.etf2000e, 3],
          ['中证1000ETF', data.etf1000, data.etf1000_hfq, T.etf1000, 3],
          ['红利低波ETF', data.etfdiv, data.etfdiv_hfq, T.etfdiv, 3]
        ];
        let html = `<div style="font-weight:600;margin-bottom:4px">${date}</div>`;
        if (klineMode && data.avg_k && data.avg_k[i]) {
          const [o, c, lo, hi] = data.avg_k[i];
          const cc = c >= o ? T.buy : T.sell;
          html += `<div style="color:${cc}">开 ${o.toFixed(2)}　高 ${hi.toFixed(2)}　低 ${lo.toFixed(2)}　收 <b>${c.toFixed(2)}</b></div>`;
        }
        for (const [name, arr, base, color, dp] of rows) {
          const v = arr[i];
          html += `<div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;`
                + `background:${color};margin-right:5px"></span>${name}: `
                + `<b>${v == null ? '-' : v.toFixed(dp)}</b>${pct(arr, base)}</div>`;
        }
        const dif = data.dif[i], dea = data.dea[i];
        html += `<div style="margin-top:4px;color:${T.muted}">DIF ${dif == null ? '-' : dif.toFixed(4)}`
              + `　DEA ${dea == null ? '-' : dea.toFixed(4)}</div>`;
        return html;
      }
    },
    legend: {
      data: ['平均股价(左轴)', '中证2000增强ETF(右轴)', '红利低波ETF(右轴)', 'DIF', 'DEA'],
      top: narrow ? 2 : 10,
      type: narrow ? 'scroll' : 'plain',
      itemWidth: narrow ? 10 : 16, itemHeight: narrow ? 8 : 10,
      textStyle: { color: T.muted, fontSize: narrow ? 10 : 12 }
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: narrow ? 44 : 62, right: narrow ? 44 : 62, top: narrow ? 34 : 44,
        height: narrow ? '43%' : '44%' },
      { left: narrow ? 44 : 62, right: narrow ? 44 : 62, top: narrow ? '55%' : '56%',
        height: narrow ? '35%' : '38%' }
    ],
    xAxis: [
      { type: 'category', data: D, gridIndex: 0, boundaryGap: klineMode,
        axisLine: { lineStyle: { color: T.axisLine } },
        axisLabel: { color: T.axisText } },
      { type: 'category', data: D, gridIndex: 1, boundaryGap: false, show: false }
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: narrow ? '' : '平均股价(元)', scale: true,
        nameTextStyle: { color: T.muted },
        axisLabel: { color: T.axisText },
        splitLine: { lineStyle: { color: T.splitLine } } },
      { type: 'value', gridIndex: 0, name: narrow ? '' : 'ETF(元)', scale: true,
        nameTextStyle: { color: T.muted },
        axisLabel: { color: T.axisText },
        splitLine: { show: false } },
      { type: 'value', gridIndex: 1, name: '', scale: true,
        nameTextStyle: { color: T.muted },
        axisLabel: { color: T.axisText },
        splitLine: { lineStyle: { color: T.splitLine } } }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 70, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 8, height: 18, start: 70, end: 100,
        borderColor: T.axisLine, backgroundColor: 'transparent',
        fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
        moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText },
        dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } } }
    ],
    series: [
      ...(klineMode && data.avg_k ? [{
        id: 'avg', name: '平均股价(左轴)', type: 'candlestick', data: data.avg_k,
        xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: T.buy, color0: T.sell,
          borderColor: T.buy, borderColor0: T.sell },
        markArea: { silent: true, itemStyle: { color: T.area }, data: areas },
        markPoint: {
          symbol: 'triangle', symbolSize: 11,
          itemStyle: { color: T.buy },
          label: { show: true, fontSize: 9, position: 'top' },
          data: marks
        }
      }] : [{
        id: 'avg', name: '平均股价(左轴)', type: 'line', data: data.avg,
        xAxisIndex: 0, yAxisIndex: 0,
        symbol: 'none', lineStyle: { width: 2, color: T.avg },
        itemStyle: { color: T.avg },
        sampling: 'lttb', large: true, largeThreshold: 400,
        markArea: { silent: true, itemStyle: { color: T.area }, data: areas },
        markPoint: {
          symbol: 'triangle', symbolSize: 11,
          itemStyle: { color: T.buy },
          label: { show: true, fontSize: 9, position: 'top' },
          data: marks
        }
      }]),
      {
        id: 'etf2000e', name: '中证2000增强ETF(右轴)', type: 'line', data: data.etf2000e,
        xAxisIndex: 0, yAxisIndex: 1,
        symbol: 'none', lineStyle: { width: 1.6, color: T.etf2000e },
        itemStyle: { color: T.etf2000e },
        sampling: 'lttb', large: true, largeThreshold: 400
      },
      {
        id: 'etfdiv', name: '红利低波ETF(右轴)', type: 'line', data: data.etfdiv,
        xAxisIndex: 0, yAxisIndex: 1,
        symbol: 'none', lineStyle: { width: 1.4, color: T.etfdiv },
        itemStyle: { color: T.etfdiv },
        sampling: 'lttb', large: true, largeThreshold: 400
      },
      { name: 'MACD红', type: 'bar', data: cRed, xAxisIndex: 1, yAxisIndex: 2,
        barWidth: 4, barGap: 0, itemStyle: { color: '#ef5350' },
        tooltip: { show: false }, emphasis: { disabled: true } },
      { name: 'MACD粉', type: 'bar', data: cPink, xAxisIndex: 1, yAxisIndex: 2,
        barWidth: 4, barGap: 0, itemStyle: { color: '#2196f3' },
        tooltip: { show: false }, emphasis: { disabled: true } },
      { name: 'MACD绿', type: 'bar', data: cGreen, xAxisIndex: 1, yAxisIndex: 2,
        barWidth: 4, barGap: 0, itemStyle: { color: '#4caf50' },
        tooltip: { show: false }, emphasis: { disabled: true } },
      { name: 'MACD黄', type: 'bar', data: cYellow, xAxisIndex: 1, yAxisIndex: 2,
        barWidth: 4, barGap: 0, itemStyle: { color: '#fdd835' },
        tooltip: { show: false }, emphasis: { disabled: true } },
      {
        id: 'dif', name: 'DIF', type: 'line', data: data.dif,
        xAxisIndex: 1, yAxisIndex: 2,
        symbol: 'none', lineStyle: { width: 1, color: T.dif },
        itemStyle: { color: T.dif },
        sampling: 'lttb', large: true, largeThreshold: 200,
        markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }],
                    lineStyle: { color: T.axisText, type: 'dashed', width: 0.8 },
                    label: { show: false } },
        markPoint: {
          symbol: 'triangle', symbolSize: 8,
          itemStyle: { color: T.buy },
          label: { show: true, fontSize: 8 },
          data: difMarks
        }
      },
      {
        id: 'dea', name: 'DEA', type: 'line', data: data.dea,
        xAxisIndex: 1, yAxisIndex: 2,
        symbol: 'none', lineStyle: { width: 1, color: T.dea },
        itemStyle: { color: T.dea },
        sampling: 'lttb', large: true, largeThreshold: 200
      }
    ]
  };
  chart.setOption(option, true);
}
