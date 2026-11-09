/* 首页：Hero 统计 + 净值对比曲线 + 信号横幅 */
'use strict';

Site.injectLayout('home', {});

let navChart = null;

function renderHero(data) {
  const s = Site.calcStats(data);
  const set = (id, v, cls, subId, sub) => {
    const el = document.getElementById(id);
    el.textContent = v;
    if (cls) el.className = 'v ' + cls;
    if (subId) document.getElementById(subId).textContent = sub;
  };
  set('hsCum', (s.cumRet * 100).toFixed(1) + '%', s.cumRet >= 0 ? 'up' : 'down',
    'hsCumSub', `${data.dates[0]} 起`);
  set('hsAnn', (s.annRet * 100).toFixed(1) + '%', s.annRet >= 0 ? 'up' : 'down',
    'hsAnnSub', `Calmar ${s.calmar.toFixed(2)}`);
  set('hsMdd', (s.mdd * 100).toFixed(1) + '%', 'down',
    'hsMddSub', `峰值 ${data.dates[s.pDate]}`);
  const closedTrades = (data.trades || []).length;
  set('hsTrades', String(closedTrades), null,
    'hsTradesSub', `${s.tradingDays} 个交易日`);
  const last = data.legs[data.legs.length - 1];
  if (last && last.open) {
    const is1000 = last.asset === '1000';
    set('hsHold', last.asset_name || (is1000 ? '中证1000ETF' : '红利低波ETF'),
      is1000 ? 'up' : 'down',
      'hsHoldSub', `${last.buy_date} 起 · 浮动 ${Site.fmt.pct(last.ret)}`);
  } else {
    set('hsHold', '空仓', null, 'hsHoldSub', '');
  }
}

// 最新收盘信号横幅（与仪表盘同口径）
function renderSignalAlert(data) {
  const el = document.getElementById('sigBanner');
  el.className = ''; el.textContent = '';
  const D = data.dates, N = D.length - 1, dif = data.dif;
  if (N < 2) return;
  const d2 = dif[N - 2], d1 = dif[N - 1], d0 = dif[N];
  if (d2 == null || d1 == null || d0 == null) return;
  const last = data.legs[data.legs.length - 1];
  const holding = last && last.asset === '1000' ? 'atk' : 'div';
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

function renderNavChart(data) {
  const T = Site.chartTheme();
  const el = document.getElementById('navChart');
  if (!navChart) navChart = echarts.init(el);
  const D = data.dates, n = D.length;
  const swIdx = D.indexOf(data.switch_atk);
  const atkH = data.etf1000_hfq, e2 = data.etf2000e_hfq, divH = data.etfdiv_hfq;

  // 进攻仓拼接净值（与仪表盘滚动指标同口径）
  const atkNav = [1], divNav = [1];
  for (let i = 1; i < n; i++) {
    const rAtk = (swIdx < 0 || i <= swIdx || e2[i] == null || e2[i - 1] == null)
      ? atkH[i] / atkH[i - 1] - 1
      : e2[i] / e2[i - 1] - 1;
    atkNav.push(atkNav[i - 1] * (1 + rAtk));
    divNav.push(divNav[i - 1] * divH[i] / divH[i - 1]);
  }

  navChart.setOption({
    animation: false,
    textStyle: { color: T.text },
    tooltip: {
      trigger: 'axis',
      backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
      valueFormatter: v => (v == null ? '-' : Number(v).toFixed(3)),
      axisPointer: { type: 'line', lineStyle: { color: T.cross } }
    },
    legend: {
      data: ['轮动策略', '进攻仓持有(拼接)', '红利低波持有'],
      top: 6, textStyle: { color: T.muted, fontSize: 12 }, itemWidth: 16, itemHeight: 10
    },
    grid: { left: 56, right: 24, top: 40, bottom: 50 },
    xAxis: {
      type: 'category', data: D, boundaryGap: false,
      axisLine: { lineStyle: { color: T.axisLine } },
      axisLabel: { color: T.axisText }
    },
    yAxis: {
      type: 'value', scale: true, name: '净值',
      nameTextStyle: { color: T.muted },
      axisLabel: { color: T.axisText },
      splitLine: { lineStyle: { color: T.splitLine } }
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 8, height: 16, start: 0, end: 100,
        borderColor: T.axisLine, backgroundColor: 'transparent',
        fillerColor: T.zoomFill, handleStyle: { color: T.zoomHandle },
        moveHandleStyle: { color: T.zoomHandle }, textStyle: { color: T.axisText },
        dataBackground: { lineStyle: { color: T.splitLine }, areaStyle: { color: T.splitLine } } }
    ],
    series: [
      { name: '轮动策略', type: 'line', data: data.nav_strat, symbol: 'none',
        lineStyle: { width: 2.2, color: '#4d7cfe' }, itemStyle: { color: '#4d7cfe' },
        areaStyle: { color: 'rgba(77,124,254,0.10)' },
        sampling: 'lttb', large: true, largeThreshold: 400 },
      { name: '进攻仓持有(拼接)', type: 'line', data: atkNav, symbol: 'none',
        lineStyle: { width: 1.3, color: T.etf2000e }, itemStyle: { color: T.etf2000e },
        sampling: 'lttb', large: true, largeThreshold: 400 },
      { name: '红利低波持有', type: 'line', data: divNav, symbol: 'none',
        lineStyle: { width: 1.3, color: T.etfdiv }, itemStyle: { color: T.etfdiv },
        sampling: 'lttb', large: true, largeThreshold: 400 }
    ]
  }, true);
}

function renderAll(data) {
  renderHero(data);
  renderSignalAlert(data);
  renderNavChart(data);
  if (navChart) navChart.resize();
}

window.addEventListener('resize', () => { if (navChart) navChart.resize(); });
document.addEventListener('site:themechange', () => { if (Site.lastData) renderNavChart(Site.lastData); });

Site.loadData().then(renderAll).catch(e => {
  document.getElementById('err').style.display = 'block';
  console.error(e.stack || e);
});
