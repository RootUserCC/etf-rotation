/* 交易明细：盈亏结构、段收益分布直方图、持仓明细（可筛选）、信号记录 */
'use strict';

Site.injectLayout('trades', {});

let distChart = null;
let legFilter = 'all';

document.getElementById('legFilter').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  legFilter = b.dataset.f;
  for (const x of b.parentElement.children) x.classList.toggle('on', x === b);
  if (Site.lastData) renderTable(Site.lastData);
});

document.addEventListener('site:themechange', () => { if (Site.lastData) renderDist(Site.lastData); });
window.addEventListener('resize', () => { if (distChart) distChart.resize(); });

// ===== 盈亏结构 =====
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
  const avgDays = closed.length ? closed.reduce((a, l) => a + l.days, 0) / closed.length : 0;
  // 分资产统计
  const atkLegs = closed.filter(l => l.asset === '1000');
  const divLegs = closed.filter(l => l.asset !== '1000');
  const atkWin = atkLegs.length ? atkLegs.filter(l => l.ret > 0).length / atkLegs.length : 0;
  const divWin = divLegs.length ? divLegs.filter(l => l.ret > 0).length / divLegs.length : 0;

  const item = (k, v, cls, s) =>
    `<div class="stat-item"><div class="k">${k}</div>` +
    `<div class="v ${cls || ''}">${v}</div><div class="s">${s || ''}</div></div>`;

  document.getElementById('legStats').innerHTML = [
    item('已了结段数', closed.length, '', openLegs.length ? `另有 ${openLegs.length} 段持有中` : ''),
    item('段胜率', (wins.length / closed.length * 100).toFixed(1) + '%', '',
      `盈利 ${wins.length} 段 / 亏损 ${losses.length} 段`),
    item('进攻段胜率', (atkWin * 100).toFixed(1) + '%', atkWin >= 0.5 ? 'pos' : 'neg', `共 ${atkLegs.length} 段`),
    item('防守段胜率', (divWin * 100).toFixed(1) + '%', divWin >= 0.5 ? 'pos' : 'neg', `共 ${divLegs.length} 段`),
    item('平均盈利段', fmtPct(avgW), 'pos', ''),
    item('平均亏损段', fmtPct(avgL), 'neg', '单次换错的历史平均代价'),
    item('盈亏比', (avgW / Math.abs(avgL)).toFixed(2), '', '平均盈利 ÷ 平均亏损'),
    item('利润因子', (sumW / Math.abs(sumL)).toFixed(2), '', '总盈利 ÷ 总亏损'),
    item('最差一段', fmtPct(worst.ret), 'neg', `${worst.buy_date} → ${worst.sell_date}`),
    item('最好一段', fmtPct(best.ret), 'pos', `${best.buy_date} → ${best.sell_date}`),
    item('平均持有', avgDays.toFixed(0) + ' 个交易日', '', '每段买入→卖出'),
  ].join('');
}

// ===== 段收益分布直方图 =====
function renderDist(data) {
  const T = Site.chartTheme();
  if (!distChart) distChart = echarts.init(document.getElementById('distChart'));
  const closed = (data.legs || []).filter(l => !l.open);
  const rets = closed.map(l => +(l.ret * 100).toFixed(2));
  if (!rets.length) return;

  const mn = Math.floor(Math.min(...rets) / 2) * 2;
  const mx = Math.ceil(Math.max(...rets) / 2) * 2;
  const bins = [];
  for (let b = mn; b < mx; b += 2) bins.push([b, b + 2]);
  const counts = bins.map(([lo, hi]) => ({
    lo, hi,
    n: rets.filter(v => v >= lo && v < hi).length
  }));

  distChart.setOption({
    animation: false,
    textStyle: { color: T.text },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: T.tooltipBg, borderColor: T.tooltipBorder, borderWidth: 1,
      formatter: ps => {
        const p = ps && ps[0];
        if (!p) return '';
        const c = counts[p.dataIndex];
        return `收益区间 ${c.lo}% ~ ${c.hi}%<br>段数: <b>${c.n}</b>`;
      }
    },
    grid: { left: 46, right: 20, top: 20, bottom: 40 },
    xAxis: {
      type: 'category',
      data: counts.map(c => `${c.lo}~${c.hi}`),
      axisLine: { lineStyle: { color: T.axisLine } },
      axisLabel: { color: T.axisText, fontSize: 10, rotate: 45 }
    },
    yAxis: {
      type: 'value', name: '段数', nameTextStyle: { color: T.muted },
      axisLabel: { color: T.axisText },
      splitLine: { lineStyle: { color: T.splitLine } },
      minInterval: 1
    },
    series: [{
      type: 'bar',
      data: counts.map(c => ({
        value: c.n,
        itemStyle: { color: c.lo >= 0 ? T.buy : T.sell, borderRadius: [3, 3, 0, 0] }
      })),
      barMaxWidth: 26
    }]
  }, true);
}

// ===== 持仓明细表 =====
function renderTable(data) {
  const fmt = v => (v == null ? '-' : Number(v).toFixed(3));
  const pct = v => (v == null ? '-' : (v * 100).toFixed(2) + '%');
  const cls = v => (v < 0 ? 'neg' : 'pos');
  const legs = [...data.legs].reverse()
    .filter(l => legFilter === 'all' || (legFilter === '1000' ? l.asset === '1000' : l.asset !== '1000'));
  const rows = legs.map(l => {
    const tag = l.asset === '1000'
      ? `<span class="tag buy">${l.asset_name || '中证1000ETF'}</span>`
      : '<span class="tag sell">红利低波ETF</span>';
    return `<tr class="${l.asset === '1000' ? 'buy' : 'sell'}">
      <td>${tag}</td>
      <td>${l.buy_date}</td><td>${fmt(l.buy_price)}</td>
      <td>${l.open ? '<b>持有中</b>' : l.sell_date}</td><td>${fmt(l.sell_price)}</td>
      <td>${l.days}</td>
      <td class="${cls(l.ret)}">${pct(l.ret)}${l.open ? '(浮)' : ''}</td>
      <td class="${cls(l.cum)}">${pct(l.cum)}</td>
    </tr>`;
  }).join('');
  document.getElementById('tbody').innerHTML = rows;
}

// ===== 信号记录表 =====
// 口径：buy=卖出红利低波、买入进攻仓；sell=卖出进攻仓、买入红利低波
// 记录内 asset_name 为进攻标的，price 为新买入腿的成交价，leg_ret 为刚了结那一段的收益
function renderSignals(data) {
  const D = data.dates;
  const rows = [...data.trades].reverse().map(t => {
    const isBuy = t.action === 'buy';
    const i = D.indexOf(t.sig_date);
    const dif = i >= 0 ? data.dif[i] : null;
    const atk = t.asset_name || '进攻仓';
    const op = isBuy
      ? `卖出 红利低波ETF → 买入 <b>${atk}</b>`
      : `卖出 ${atk} → 买入 <b>红利低波ETF</b>`;
    const legRet = t.leg_ret == null ? '-'
      : `<span class="${t.leg_ret < 0 ? 'neg' : 'pos'}" style="color:${t.leg_ret < 0 ? 'var(--down)' : 'var(--up)'}">${Site.fmt.pct(t.leg_ret)}</span>`;
    return `<tr class="${isBuy ? 'buy' : 'sell'}">
      <td>${isBuy ? '<span class="tag buy">买入进攻</span>' : '<span class="tag sell">切换防守</span>'}</td>
      <td>${t.sig_date}</td>
      <td>${t.date}</td>
      <td style="text-align:left">${op}</td>
      <td>${t.price == null ? '-' : Number(t.price).toFixed(3)}</td>
      <td>${dif == null ? '-' : dif.toFixed(4)}</td>
      <td>${legRet}</td>
    </tr>`;
  }).join('');
  document.getElementById('sigTbody').innerHTML = rows;
}

Site.loadData().then(data => {
  document.getElementById('err').style.display = 'none';
  renderLegStats(data);
  renderDist(data);
  renderTable(data);
  renderSignals(data);
  if (distChart) distChart.resize();
}).catch(e => {
  document.getElementById('err').style.display = 'block';
  console.error(e.stack || e);
});
