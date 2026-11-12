// Charts (d3 v7, SVG). Every chart redraws on resize and shares one tooltip.
import { h, clear, inr, num, pct, isNum, parseDate, dShort, dLong, minutesToHM, showTip, hideTip, tipRow, esc } from './dom.js';

export const LEG_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#f59e0b', '#22c55e', '#60a5fa', '#fb7185', '#34d399'];
export const COLORS = { green: '#22c55e', red: '#ef4444', accent: '#22d3ee', violet: '#8b5cf6', pink: '#f472b6', amber: '#f59e0b', muted: '#8b97b8', line: 'rgba(255,255,255,.08)' };
const PALETTE = ['#22d3ee', '#a78bfa', '#f472b6', '#f59e0b', '#22c55e', '#60a5fa', '#fb7185', '#34d399'];

// diverging red -> neutral -> green scale around zero
export function pnlColor(maxAbs) {
  const neg = d3.interpolateRgb('#1b2540', '#ef4444'), pos = d3.interpolateRgb('#1b2540', '#22c55e');
  return (v) => (!isNum(v) ? '#1b2540' : v >= 0 ? pos(Math.min(1, v / (maxAbs || 1)) * 0.95) : neg(Math.min(1, -v / (maxAbs || 1)) * 0.95));
}

function mount(el, draw) {
  if (el._ro) el._ro.disconnect();
  el._w = 0;
  const run = (force = false) => {
    const w = Math.floor(el.clientWidth);
    if (w < 40 || (!force && w === el._w)) return;
    el._w = w;
    clear(el);
    draw(w);
  };
  el._ro = new ResizeObserver(() => run());
  el._ro.observe(el);
  run();
  return () => run(true);
}
const svgBase = (el, w, hgt) => d3.select(el).append('svg').attr('viewBox', `0 0 ${w} ${hgt}`).attr('height', hgt);
const fmtK = (v) => {
  const a = Math.abs(v);
  return (v < 0 ? '-' : '') + (a >= 1e7 ? (a / 1e7).toFixed(1) + 'Cr' : a >= 1e5 ? (a / 1e5).toFixed(1) + 'L' : a >= 1e3 ? (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'k' : a.toFixed(0));
};
const yAxis = (g, scale, w, fmt = fmtK, ticks = 5) => {
  g.append('g').attr('class', 'gridline').call(d3.axisLeft(scale).ticks(ticks).tickSize(-w).tickFormat(''));
  g.append('g').attr('class', 'axis').call(d3.axisLeft(scale).ticks(ticks).tickSize(0).tickPadding(8).tickFormat(fmt));
};
const sign = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

// ---------------------------------------------------------------- equity + drawdown
export function equityChart(el, rows, { capital } = {}) {
  mount(el, (W) => {
    const data = rows.map((r) => ({ ...r, d: parseDate(r.date) }));
    if (!data.length) return;
    const m = { l: 58, r: 14, t: 10, b: 24 }, h1 = 250, gap = 16, h2 = 90, H = m.t + h1 + gap + h2 + m.b;
    const svg = svgBase(el, W, H);
    const iw = W - m.l - m.r;
    const x = d3.scaleTime().domain(d3.extent(data, (d) => d.d)).range([0, iw]);
    const base = data[0].equity - data[0].net;
    const ext = d3.extent(data.map((d) => d.equity).concat([base]));
    const pad = (ext[1] - ext[0]) * 0.08 || 1;
    const y1 = d3.scaleLinear().domain([ext[0] - pad, ext[1] + pad]).range([h1, 0]);
    const ddMin = d3.min(data, (d) => d.dd) || -1;
    const y2 = d3.scaleLinear().domain([Math.min(ddMin * 1.1, -1), 0]).range([h2, 0]);
    const g1 = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const g2 = svg.append('g').attr('transform', `translate(${m.l},${m.t + h1 + gap})`);
    const id = 'eq' + Math.random().toString(36).slice(2, 8);
    const defs = svg.append('defs');
    const lg = defs.append('linearGradient').attr('id', id).attr('x1', 0).attr('x2', 0).attr('y1', 0).attr('y2', 1);
    lg.append('stop').attr('offset', '0%').attr('stop-color', COLORS.accent).attr('stop-opacity', 0.38);
    lg.append('stop').attr('offset', '100%').attr('stop-color', COLORS.accent).attr('stop-opacity', 0.0);
    const lr = defs.append('linearGradient').attr('id', id + 'r').attr('x1', 0).attr('x2', 0).attr('y1', 0).attr('y2', 1);
    lr.append('stop').attr('offset', '0%').attr('stop-color', COLORS.red).attr('stop-opacity', 0.0);
    lr.append('stop').attr('offset', '100%').attr('stop-color', COLORS.red).attr('stop-opacity', 0.55);
    yAxis(g1, y1, iw);
    g1.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y1(base)).attr('y2', y1(base)).attr('stroke', 'rgba(255,255,255,.28)').attr('stroke-dasharray', '4 4');
    g1.append('path').datum(data).attr('fill', `url(#${id})`)
      .attr('d', d3.area().x((d) => x(d.d)).y0(h1).y1((d) => y1(d.equity)).curve(d3.curveMonotoneX));
    g1.append('path').datum(data).attr('fill', 'none').attr('stroke', COLORS.accent).attr('stroke-width', 2.2)
      .attr('d', d3.line().x((d) => x(d.d)).y((d) => y1(d.equity)).curve(d3.curveMonotoneX));
    g2.append('g').attr('class', 'gridline').call(d3.axisLeft(y2).ticks(3).tickSize(-iw).tickFormat(''));
    g2.append('g').attr('class', 'axis').call(d3.axisLeft(y2).ticks(3).tickSize(0).tickPadding(8).tickFormat(fmtK));
    g2.append('path').datum(data).attr('fill', `url(#${id}r)`)
      .attr('d', d3.area().x((d) => x(d.d)).y0(0).y1((d) => y2(d.dd)).curve(d3.curveStepAfter));
    g2.append('path').datum(data).attr('fill', 'none').attr('stroke', COLORS.red).attr('stroke-width', 1.2)
      .attr('d', d3.line().x((d) => x(d.d)).y((d) => y2(d.dd)).curve(d3.curveStepAfter));
    g2.append('g').attr('class', 'axis').attr('transform', `translate(0,${h2})`).call(d3.axisBottom(x).ticks(Math.max(3, iw / 110)).tickSize(0).tickPadding(8));
    g1.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5).text(capital ? 'Equity' : 'Cumulative P&L');
    g2.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5).text('Drawdown');
    // crosshair
    const cross = svg.append('g').attr('transform', `translate(${m.l},${m.t})`).style('display', 'none');
    const vline = cross.append('line').attr('y1', 0).attr('y2', h1 + gap + h2).attr('stroke', 'rgba(255,255,255,.3)');
    const dot = cross.append('circle').attr('r', 4.5).attr('fill', COLORS.accent).attr('stroke', '#07101f').attr('stroke-width', 2);
    const bis = d3.bisector((d) => d.d).center;
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', h1 + gap + h2).attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const [mx] = d3.pointer(ev);
        const d = data[bis(data, x.invert(mx))];
        cross.style('display', null);
        vline.attr('x1', x(d.d)).attr('x2', x(d.d));
        dot.attr('cx', x(d.d)).attr('cy', y1(d.equity));
        showTip(ev, `<div class="h">${dLong(d.date)}</div>` + tipRow(capital ? 'Equity' : 'Cum. P&L', inr(d.equity)) +
          tipRow('Day P&L', d.traded ? `<span class="${sign(d.net)}">${inr(d.net, true)}</span>` : 'no trade') +
          tipRow('Drawdown', `<span class="neg">${inr(d.dd)}</span>`));
      }).on('mouseleave', () => { cross.style('display', 'none'); hideTip(); });
  });
}

// ---------------------------------------------------------------- daily P&L bars
export function dailyBars(el, rows) {
  mount(el, (W) => {
    const data = rows.filter((r) => r.traded).map((r) => ({ ...r, d: parseDate(r.date) }));
    if (!data.length) return;
    const m = { l: 58, r: 14, t: 8, b: 24 }, H = 190, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleBand().domain(data.map((d) => d.date)).range([0, iw]).paddingInner(0.18);
    const ext = d3.extent(data, (d) => d.net);
    const y = d3.scaleLinear().domain([Math.min(ext[0], 0), Math.max(ext[1], 0)]).nice().range([ih, 0]);
    yAxis(g, y, iw, fmtK, 4);
    g.selectAll('rect').data(data).join('rect').attr('x', (d) => x(d.date)).attr('width', Math.max(1, x.bandwidth()))
      .attr('y', (d) => y(Math.max(0, d.net))).attr('height', (d) => Math.max(1, Math.abs(y(d.net) - y(0))))
      .attr('rx', Math.min(2, x.bandwidth() / 3)).attr('fill', (d) => (d.net >= 0 ? COLORS.green : COLORS.red)).attr('opacity', 0.9)
      .on('mousemove', (ev, d) => showTip(ev, `<div class="h">${dLong(d.date)}</div>` + tipRow('Day P&L', `<span class="${sign(d.net)}">${inr(d.net, true)}</span>`) + tipRow('Charges', inr(d.costs))))
      .on('mouseleave', hideTip);
    g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.25)');
    const ticks = data.filter((_, i) => i % Math.max(1, Math.round(data.length / Math.max(3, iw / 90))) === 0);
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`)
      .call(d3.axisBottom(x).tickValues(ticks.map((d) => d.date)).tickSize(0).tickPadding(8).tickFormat((s) => dShort(s)));
  });
}

// ---------------------------------------------------------------- calendar heat-map (one strip per year, data range only)
export function calendarHeat(el, rows, onSelect) {
  mount(el, (W) => {
    const data = rows.filter((r) => r.traded);
    if (!data.length) return;
    const byDate = new Map(data.map((r) => [r.date, r]));
    const maxAbs = d3.quantile(data.map((r) => Math.abs(r.net)).sort(d3.ascending), 0.95) || 1;
    const color = pnlColor(maxAbs);
    const fmt = d3.timeFormat('%Y-%m-%d');
    const years = [...new Set(rows.map((r) => +r.date.slice(0, 4)))].sort();
    const left = 40, gap = 3;
    // weeks needed per year (first to last day present in the series)
    const spans = years.map((yr) => {
      const ds = rows.filter((r) => +r.date.slice(0, 4) === yr).map((r) => parseDate(r.date));
      const lo = d3.min(ds), hi = d3.max(ds), start = d3.timeMonday.floor(lo);
      return { yr, lo, hi, start, weeks: d3.timeMonday.count(start, hi) + 1 };
    });
    const maxWeeks = d3.max(spans, (s) => s.weeks);
    const cell = Math.max(8, Math.min(26, Math.floor((W - left - 8) / maxWeeks) - gap));
    const rowH = 5 * (cell + gap) + 40, H = spans.length * rowH + 4;
    const svg = svgBase(el, W, H);
    spans.forEach((sp, yi) => {
      const g = svg.append('g').attr('transform', `translate(${left},${yi * rowH + 30})`);
      g.append('text').attr('x', -left).attr('y', -22).attr('fill', '#cbd5f5').attr('font-size', 12).attr('font-weight', 800).text(sp.yr);
      ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].forEach((t, i) => g.append('text').attr('x', -8).attr('y', i * (cell + gap) + cell * 0.72).attr('text-anchor', 'end').attr('fill', COLORS.muted).attr('font-size', 9).text(t));
      const cells = [];
      for (let d = new Date(sp.lo); d <= sp.hi; d = d3.timeDay.offset(d, 1)) {
        const wd = (d.getDay() + 6) % 7;
        if (wd > 4) continue;
        cells.push({ d: new Date(d), col: d3.timeMonday.count(sp.start, d), row: wd });
      }
      g.selectAll('rect').data(cells).join('rect').attr('x', (c) => c.col * (cell + gap)).attr('y', (c) => c.row * (cell + gap)).attr('width', cell).attr('height', cell).attr('rx', 4)
        .attr('fill', (c) => { const r = byDate.get(fmt(c.d)); return r ? color(r.net) : 'rgba(255,255,255,.035)'; })
        .style('cursor', (c) => byDate.has(fmt(c.d)) && onSelect ? 'pointer' : null)
        .on('mousemove', (ev, c) => { const key = fmt(c.d), r = byDate.get(key); showTip(ev, `<div class="h">${dLong(key)}</div>` + (r ? tipRow('Day P&L', `<span class="${sign(r.net)}">${inr(r.net, true)}</span>`) : '<span class="muted">no trade</span>')); })
        .on('mouseleave', hideTip)
        .on('click', (_, c) => {
          const key = fmt(c.d);
          if (byDate.has(key) && onSelect) onSelect(key);
        });
      const seen = new Set();
      cells.filter((c) => c.d.getDate() <= 7 || (c.col === 0 && !seen.size)).forEach((c) => {
        const k = c.d.getMonth();
        if (seen.has(k)) return;
        seen.add(k);
        g.append('text').attr('x', c.col * (cell + gap)).attr('y', -6).attr('fill', COLORS.muted).attr('font-size', 10).text(d3.timeFormat('%b')(c.d));
      });
    });
  });
}

// ---------------------------------------------------------------- monthly table (HTML heat cells)
export function monthlyTable(rows) {
  const years = [...new Set(rows.map((r) => r.year))].sort();
  const maxAbs = d3.max(rows, (r) => Math.abs(r.pnl)) || 1;
  const color = pnlColor(maxAbs);
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const idx = new Map(rows.map((r) => [r.year + '-' + r.month, r]));
  const th = (t) => h('th', {}, t);
  const head = h('tr', {}, th('Year'), ...MON.map(th), th('Total'));
  const body = years.map((yr) => {
    let tot = 0;
    const tds = MON.map((_, i) => {
      const r = idx.get(yr + '-' + (i + 1));
      if (!r) return h('td', { style: { color: 'var(--dim)' } }, '·');
      tot += r.pnl;
      return h('td', { style: { background: color(r.pnl), color: '#f4f7ff', borderRadius: '6px' }, title: `${r.days} trade days` }, inr(r.pnl));
    });
    return h('tr', {}, h('td', { class: 'l' }, h('b', {}, yr)), ...tds, h('td', { class: sign(tot) }, h('b', {}, inr(tot, true))));
  });
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 't' }, h('thead', {}, head), h('tbody', {}, body)));
}

// ---------------------------------------------------------------- categorical bars
export function barChart(el, items, { value = 'v', label = 'k', fmt = inr, horizontal = false, height = 200, colorFn, tipFn } = {}) {
  mount(el, (W) => {
    if (!items.length) return;
    const color = colorFn || ((d) => (d[value] >= 0 ? COLORS.green : COLORS.red));
    if (horizontal) {
      const m = { l: 120, r: 50, t: 6, b: 6 }, rowH = 26, H = m.t + m.b + items.length * rowH;
      const svg = svgBase(el, W, H);
      const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
      const iw = W - m.l - m.r;
      const x = d3.scaleLinear().domain([0, d3.max(items, (d) => Math.abs(d[value])) || 1]).range([0, iw]);
      const y = d3.scaleBand().domain(items.map((d) => d[label])).range([0, items.length * rowH]).padding(0.28);
      g.selectAll('rect').data(items).join('rect').attr('y', (d) => y(d[label])).attr('height', y.bandwidth()).attr('width', (d) => x(Math.abs(d[value]))).attr('rx', 5)
        .attr('fill', (d) => color(d)).attr('opacity', 0.9)
        .on('mousemove', (ev, d) => showTip(ev, tipFn ? tipFn(d) : `<div class="h">${esc(d[label])}</div>` + tipRow('Value', fmt(d[value])))).on('mouseleave', hideTip);
      g.selectAll('.lb').data(items).join('text').attr('x', -8).attr('y', (d) => y(d[label]) + y.bandwidth() / 2 + 4).attr('text-anchor', 'end').attr('fill', '#cbd5f5').attr('font-size', 11.5).text((d) => d[label]);
      g.selectAll('.vl').data(items).join('text').attr('x', (d) => x(Math.abs(d[value])) + 6).attr('y', (d) => y(d[label]) + y.bandwidth() / 2 + 4).attr('fill', COLORS.muted).attr('font-size', 11).text((d) => fmt(d[value]));
      return;
    }
    const m = { l: 52, r: 8, t: 10, b: 26 }, H = height, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleBand().domain(items.map((d) => d[label])).range([0, iw]).padding(0.28);
    const ext = d3.extent(items, (d) => d[value]);
    const y = d3.scaleLinear().domain([Math.min(0, ext[0]), Math.max(0, ext[1])]).nice().range([ih, 0]);
    yAxis(g, y, iw, fmt === inr ? fmtK : (v) => fmt(v), 4);
    g.selectAll('rect').data(items).join('rect').attr('x', (d) => x(d[label])).attr('width', x.bandwidth()).attr('y', (d) => y(Math.max(0, d[value])))
      .attr('height', (d) => Math.max(1, Math.abs(y(d[value]) - y(0)))).attr('rx', 5).attr('fill', (d) => color(d)).attr('opacity', 0.9)
      .on('mousemove', (ev, d) => showTip(ev, tipFn ? tipFn(d) : `<div class="h">${esc(d[label])}</div>` + tipRow('Value', fmt(d[value])))).on('mouseleave', hideTip);
    g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.25)');
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).tickSize(0).tickPadding(8));
  });
}

// ---------------------------------------------------------------- histogram
export function histChart(el, bins, { height = 200 } = {}) {
  mount(el, (W) => {
    if (!bins.length) return;
    const m = { l: 40, r: 8, t: 8, b: 26 }, iw = W - m.l - m.r, ih = height - m.t - m.b;
    const svg = svgBase(el, W, height);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleLinear().domain([bins[0].lo, bins[bins.length - 1].hi]).range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(bins, (b) => b.n)]).nice().range([ih, 0]);
    yAxis(g, y, iw, (v) => v, 4);
    g.selectAll('rect').data(bins).join('rect').attr('x', (b) => x(b.lo) + 1).attr('width', (b) => Math.max(1, x(b.hi) - x(b.lo) - 2)).attr('y', (b) => y(b.n))
      .attr('height', (b) => ih - y(b.n)).attr('rx', 3).attr('fill', (b) => ((b.lo + b.hi) / 2 >= 0 ? COLORS.green : COLORS.red)).attr('opacity', 0.85)
      .on('mousemove', (ev, b) => showTip(ev, `<div class="h">${inr(b.lo)} to ${inr(b.hi)}</div>` + tipRow('Days', b.n))).on('mouseleave', hideTip);
    if (x.domain()[0] < 0 && x.domain()[1] > 0) g.append('line').attr('x1', x(0)).attr('x2', x(0)).attr('y1', 0).attr('y2', ih).attr('stroke', 'rgba(255,255,255,.4)').attr('stroke-dasharray', '3 3');
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(Math.max(3, iw / 90)).tickSize(0).tickPadding(8).tickFormat(fmtK));
  });
}

// ---------------------------------------------------------------- MAE / MFE scatter
export function scatterChart(el, pts, { height = 260 } = {}) {
  mount(el, (W) => {
    if (!pts.length) return;
    const m = { l: 56, r: 12, t: 10, b: 34 }, iw = W - m.l - m.r, ih = height - m.t - m.b;
    const svg = svgBase(el, W, height);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleLinear().domain([d3.min(pts, (p) => p.mae) * 1.05, 0]).nice().range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(pts, (p) => p.mfe) * 1.05 || 1]).nice().range([ih, 0]);
    yAxis(g, y, iw, fmtK, 5);
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(6).tickSize(0).tickPadding(8).tickFormat(fmtK));
    g.append('text').attr('x', iw / 2).attr('y', ih + 30).attr('text-anchor', 'middle').attr('fill', COLORS.muted).attr('font-size', 10.5).text('Worst intraday P&L (MAE)  →  better');
    g.append('text').attr('x', -ih / 2).attr('y', -42).attr('transform', 'rotate(-90)').attr('text-anchor', 'middle').attr('fill', COLORS.muted).attr('font-size', 10.5).text('Best intraday P&L (MFE)');
    g.selectAll('circle').data(pts).join('circle').attr('cx', (p) => x(p.mae)).attr('cy', (p) => y(p.mfe)).attr('r', 4.4)
      .attr('fill', (p) => (p.net >= 0 ? COLORS.green : COLORS.red)).attr('opacity', 0.75).attr('stroke', '#07101f')
      .on('mousemove', (ev, p) => showTip(ev, `<div class="h">${dLong(p.date)}</div>` + tipRow('Final', `<span class="${sign(p.net)}">${inr(p.net, true)}</span>`) + tipRow('MAE', inr(p.mae)) + tipRow('MFE', inr(p.mfe)))).on('mouseleave', hideTip);
  });
}

// ---------------------------------------------------------------- generic per-day series (line or bars)
export function daySeries(el, rows, { key, kind = 'line', color = COLORS.accent, fmt = (v) => num(v, 2), title = '', height = 190, signed = false }) {
  mount(el, (W) => {
    const data = rows.filter((r) => isNum(r[key])).map((r) => ({ ...r, d: parseDate(r.date), v: r[key] }));
    if (!data.length) return;
    const m = { l: 56, r: 12, t: 16, b: 24 }, iw = W - m.l - m.r, ih = height - m.t - m.b;
    const svg = svgBase(el, W, height);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const ext = d3.extent(data, (d) => d.v);
    const lo = kind === 'bars' || signed ? Math.min(0, ext[0]) : ext[0] - (ext[1] - ext[0]) * 0.1;
    const hi = kind === 'bars' || signed ? Math.max(0, ext[1]) : ext[1] + (ext[1] - ext[0]) * 0.1 || ext[1] + 1;
    const y = d3.scaleLinear().domain([lo, hi]).nice().range([ih, 0]);
    yAxis(g, y, iw, (v) => fmt(v), 4);
    g.append('text').attr('x', 4).attr('y', -4).attr('fill', COLORS.muted).attr('font-size', 10.5).text(title);
    if (kind === 'bars') {
      const x = d3.scaleBand().domain(data.map((d) => d.date)).range([0, iw]).paddingInner(0.2);
      g.selectAll('rect').data(data).join('rect').attr('x', (d) => x(d.date)).attr('width', Math.max(1, x.bandwidth())).attr('y', (d) => y(Math.max(0, d.v))).attr('height', (d) => Math.max(1, Math.abs(y(d.v) - y(0))))
        .attr('fill', (d) => (d.v >= 0 ? COLORS.green : COLORS.red)).attr('opacity', 0.85)
        .on('mousemove', (ev, d) => showTip(ev, `<div class="h">${dLong(d.date)}</div>` + tipRow(title, fmt(d.v)))).on('mouseleave', hideTip);
      g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.25)');
      const ticks = data.filter((_, i) => i % Math.max(1, Math.round(data.length / Math.max(3, iw / 90))) === 0);
      g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).tickValues(ticks.map((d) => d.date)).tickSize(0).tickPadding(8).tickFormat((t) => dShort(t)));
    } else {
      const x = d3.scaleTime().domain(d3.extent(data, (d) => d.d)).range([0, iw]);
      if (signed) g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.25)').attr('stroke-dasharray', '4 4');
      g.append('path').datum(data).attr('fill', 'none').attr('stroke', color).attr('stroke-width', 2).attr('d', d3.line().x((d) => x(d.d)).y((d) => y(d.v)).curve(d3.curveMonotoneX));
      g.selectAll('circle').data(data).join('circle').attr('cx', (d) => x(d.d)).attr('cy', (d) => y(d.v)).attr('r', data.length > 80 ? 0 : 2.6).attr('fill', color);
      g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(Math.max(3, iw / 110)).tickSize(0).tickPadding(8));
      const bis = d3.bisector((d) => d.d).center;
      svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', ih).attr('fill', 'transparent')
        .on('mousemove', (ev) => { const d = data[bis(data, x.invert(d3.pointer(ev)[0]))]; showTip(ev, `<div class="h">${dLong(d.date)}</div>` + tipRow(title, fmt(d.v))); }).on('mouseleave', hideTip);
    }
  });
}

// ---------------------------------------------------------------- intraday explorer
export function intradayChart(el, d, replay = null) {
  let replayIndex = replay ? replay.replayIndex : null;
  const replayStart = replay ? replay.replayStart : 0;
  const redraw = mount(el, (W) => {
    const N = d.mtm.length;
    const m = { l: 62, r: 62, t: 10, b: 26 }, h1 = 250, gap = 20, h2 = 150;
    const indicatorPaneLines = (d.indicator && d.indicator.source === 'spot' ? d.indicator.pane_lines : []) || [];
    const indicatorHistogram = d.indicator && d.indicator.pane_histogram || [];
    const hOsc = indicatorPaneLines.length ? 100 : 0;
    const H = m.t + h1 + gap + hOsc + (hOsc ? gap : 0) + h2 + m.b, iw = W - m.l - m.r;
    const svg = svgBase(el, W, H);
    const x = d3.scaleLinear().domain([0, N - 1]).range([0, iw]);
    const reveal = (values, start = 0) => replayIndex == null ? values : values.map((v, i) => i >= start && i <= replayIndex ? v : null);
    const mtm = reveal(d.mtm, replayStart);
    const mtmExt = d3.extent(mtm.filter(isNum).concat([0]));
    const pad = (mtmExt[1] - mtmExt[0]) * 0.1 || 1;
    const yM = d3.scaleLinear().domain([mtmExt[0] - pad, mtmExt[1] + pad]).range([h1, 0]);
    const sp = reveal(d.spot.c);
    const spotHigh = d.spot.h ? reveal(d.spot.h) : sp;
    const spotLow = d.spot.l ? reveal(d.spot.l) : sp;
    const sExt = d3.extent(sp.filter(isNum).concat(spotHigh.filter(isNum), spotLow.filter(isNum)));
    const sPad = (sExt[1] - sExt[0]) * 0.1 || 1;
    const yS = d3.scaleLinear().domain([sExt[0] - sPad, sExt[1] + sPad]).range([h1, 0]);
    const indicatorLines = (d.indicator && d.indicator.source === 'spot' ? d.indicator.lines : []) || [];
    const indicatorValues = indicatorLines.flatMap((line) => reveal(line.values).filter(isNum));
    if (indicatorValues.length) {
      const iExt = d3.extent(indicatorValues.concat(sExt));
      const iPad = (iExt[1] - iExt[0]) * 0.1 || 1;
      yS.domain([iExt[0] - iPad, iExt[1] + iPad]);
    }
    const g1 = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const oscTop = m.t + h1 + gap;
    const gOsc = hOsc ? svg.append('g').attr('transform', `translate(${m.l},${oscTop})`) : null;
    const g2Top = oscTop + hOsc + (hOsc ? gap : 0);
    const g2 = svg.append('g').attr('transform', `translate(${m.l},${g2Top})`);
    const id = 'ix' + Math.random().toString(36).slice(2, 8);
    const defs = svg.append('defs');
    // area split above / below zero via clip paths
    defs.append('clipPath').attr('id', id + 'u').append('rect').attr('width', iw).attr('height', Math.max(0, yM(0)));
    defs.append('clipPath').attr('id', id + 'd').append('rect').attr('y', yM(0)).attr('width', iw).attr('height', Math.max(0, h1 - yM(0)));
    yAxis(g1, yM, iw, fmtK, 5);
    g1.append('g').attr('class', 'axis').attr('transform', `translate(${iw},0)`).call(d3.axisRight(yS).ticks(5).tickSize(0).tickPadding(8).tickFormat(d3.format(',.0f')));
    const area = d3.area().defined(isNum).x((_, i) => x(i)).y0(yM(0)).y1((v) => yM(v)).curve(d3.curveStepAfter);
    g1.append('path').datum(mtm).attr('d', area).attr('fill', COLORS.green).attr('opacity', 0.28).attr('clip-path', `url(#${id}u)`);
    g1.append('path').datum(mtm).attr('d', area).attr('fill', COLORS.red).attr('opacity', 0.28).attr('clip-path', `url(#${id}d)`);
    g1.append('path').datum(mtm).attr('fill', 'none').attr('stroke', '#e6eaf5').attr('stroke-width', 1.8).attr('d', d3.line().defined(isNum).x((_, i) => x(i)).y((v) => yM(v)).curve(d3.curveStepAfter));
    g1.append('line').attr('x1', 0).attr('x2', iw).attr('y1', yM(0)).attr('y2', yM(0)).attr('stroke', 'rgba(255,255,255,.35)').attr('stroke-dasharray', '4 4');
    if (d.spot.o && d.spot.h && d.spot.l) {
      const opens = reveal(d.spot.o);
      const closes = sp;
      const candleWidth = Math.max(1, Math.min(5, iw / N * 0.62));
      opens.forEach((open, i) => {
        if (!isNum(open) || !isNum(closes[i]) || !isNum(spotHigh[i]) || !isNum(spotLow[i])) return;
        const up = closes[i] >= open;
        const color = up ? COLORS.green : COLORS.red;
        g1.append('line').attr('x1', x(i)).attr('x2', x(i))
          .attr('y1', yS(spotHigh[i])).attr('y2', yS(spotLow[i]))
          .attr('stroke', color).attr('stroke-width', 1);
        g1.append('rect').attr('x', x(i) - candleWidth / 2)
          .attr('y', yS(Math.max(open, closes[i])))
          .attr('width', candleWidth)
          .attr('height', Math.max(1, Math.abs(yS(open) - yS(closes[i]))))
          .attr('fill', color).attr('stroke', color);
      });
    }
    g1.append('path').datum(sp).attr('fill', 'none').attr('stroke', COLORS.amber).attr('stroke-width', 1.3).attr('opacity', 0.85)
      .attr('d', d3.line().defined(isNum).x((_, i) => x(i)).y((v) => yS(v)));
    indicatorLines.forEach((line, i) => {
      const values = reveal(line.values);
      g1.append('path').datum(values).attr('fill', 'none').attr('stroke', PALETTE[(i + 1) % PALETTE.length])
        .attr('stroke-width', 1.7).attr('d', d3.line().defined(isNum).x((_, j) => x(j)).y((v) => yS(v)));
    });
    if (gOsc) {
      const oscillatorValues = indicatorPaneLines.flatMap((line) => reveal(line.values).filter(isNum))
        .concat(reveal(indicatorHistogram).filter(isNum));
      const oscExtent = d3.extent(oscillatorValues.concat([0]));
      const oscPad = Math.max(Math.abs(oscExtent[0]), Math.abs(oscExtent[1])) * 0.1 || 1;
      const yOsc = d3.scaleLinear().domain([Math.min(0, oscExtent[0] - oscPad), Math.max(0, oscExtent[1] + oscPad)]).range([hOsc, 0]);
      yAxis(gOsc, yOsc, iw, (v) => v.toFixed(2), 3);
      gOsc.append('line').attr('x1', 0).attr('x2', iw).attr('y1', yOsc(0)).attr('y2', yOsc(0))
        .attr('stroke', 'rgba(255,255,255,.3)').attr('stroke-dasharray', '3 4');
      reveal(indicatorHistogram).forEach((value, i) => {
        if (!isNum(value)) return;
        const yValue = yOsc(value);
        gOsc.append('rect').attr('x', x(i) - 2).attr('y', Math.min(yValue, yOsc(0)))
          .attr('width', 4).attr('height', Math.max(1, Math.abs(yValue - yOsc(0))))
          .attr('fill', value >= 0 ? COLORS.green : COLORS.red).attr('opacity', 0.55);
      });
      indicatorPaneLines.forEach((line, i) => gOsc.append('path').datum(reveal(line.values))
        .attr('fill', 'none').attr('stroke', PALETTE[(i + 1) % PALETTE.length])
        .attr('stroke-width', 1.6).attr('d', d3.line().defined(isNum).x((_, j) => x(j)).y((v) => yOsc(v))));
      gOsc.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5)
        .text(`${d.indicator.label} · MACD / signal / histogram`);
    }
    g1.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5)
      .text(`Combined P&L (white, left)  ·  NIFTY spot (amber, right)${indicatorLines.length ? `  ·  ${indicatorLines.map((line) => line.label).join(' / ')}` : ''}`);
    // leg premiums
    const legs = d.legs;
    const allP = legs.flatMap((l) => reveal(l.close).filter(isNum));
    const pExt = d3.extent(allP.length ? allP : [0, 1]);
    const yP = d3.scaleLinear().domain([pExt[0] * 0.95, pExt[1] * 1.05 || 1]).range([h2, 0]);
    g2.append('g').attr('class', 'gridline').call(d3.axisLeft(yP).ticks(4).tickSize(-iw).tickFormat(''));
    g2.append('g').attr('class', 'axis').call(d3.axisLeft(yP).ticks(4).tickSize(0).tickPadding(8));
    const tdx = (i) => minutesToHM(i);
    g2.append('g').attr('class', 'axis').attr('transform', `translate(0,${h2})`)
      .call(d3.axisBottom(x).tickValues(d3.range(0, N, 30)).tickSize(0).tickPadding(8).tickFormat(tdx));
    legs.forEach((l, k) => {
      const col = PALETTE[k % PALETTE.length];
      g2.append('path').datum(reveal(l.close)).attr('fill', 'none').attr('stroke', col).attr('stroke-width', 1.5).attr('opacity', 0.9)
        .attr('d', d3.line().defined(isNum).x((_, i) => x(i)).y((v) => yP(v)));
      if (l.entry_date === d.date && (replayIndex == null || replayIndex >= l.entry_idx)) g2.append('circle').attr('cx', x(l.entry_idx)).attr('cy', yP(l.entry_price)).attr('r', 5).attr('fill', COLORS.green).attr('stroke', '#07101f').attr('stroke-width', 2);
      if (l.closed_today && (replayIndex == null || replayIndex >= l.exit_idx)) g2.append('circle').attr('cx', x(l.exit_idx)).attr('cy', yP(l.exit_price)).attr('r', 5).attr('fill', COLORS.red).attr('stroke', '#07101f').attr('stroke-width', 2);
    });
    const marks = new Map();
    legs.forEach((l) => {
      if (l.entry_date === d.date && (replayIndex == null || replayIndex >= l.entry_idx)) marks.set(l.entry_idx, 'entry');
      if (l.closed_today && (replayIndex == null || replayIndex >= l.exit_idx) && !marks.has(l.exit_idx)) marks.set(l.exit_idx, 'exit');
    });
    [...marks].forEach(([i, kind]) => {
      [g1, ...(gOsc ? [gOsc] : []), g2].forEach((g, gi) => g.append('line').attr('x1', x(i)).attr('x2', x(i)).attr('y1', 0).attr('y2', gi === 0 ? h1 : gi === 1 && gOsc ? hOsc : h2)
        .attr('stroke', kind === 'entry' ? COLORS.green : COLORS.red).attr('stroke-dasharray', '3 4').attr('opacity', 0.55));
    });
    if (replayIndex != null) [g1, ...(gOsc ? [gOsc] : []), g2].forEach((g, gi) => g.append('line').attr('x1', x(replayIndex)).attr('x2', x(replayIndex)).attr('y1', 0).attr('y2', gi === 0 ? h1 : gi === 1 && gOsc ? hOsc : h2)
      .attr('stroke', COLORS.accent).attr('stroke-width', 1.5).attr('opacity', 0.9));
    g2.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5).text('Option premiums  ·  ● entry  ● exit');
    // crosshair
    const cross = svg.append('g').attr('transform', `translate(${m.l},${m.t})`).style('display', 'none');
    const vline = cross.append('line').attr('y1', 0).attr('y2', h1 + gap + hOsc + (hOsc ? gap : 0) + h2).attr('stroke', 'rgba(255,255,255,.35)');
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', h1 + gap + hOsc + (hOsc ? gap : 0) + h2).attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const i = Math.max(0, Math.min(N - 1, Math.round(x.invert(d3.pointer(ev)[0]))));
        cross.style('display', null);
        vline.attr('x1', x(i)).attr('x2', x(i));
        showTip(ev, `<div class="h">${minutesToHM(i)}</div>` + tipRow('P&L', `<span class="${sign(mtm[i])}">${inr(mtm[i], true)}</span>`) +
          tipRow('NIFTY', num(sp[i], 1), COLORS.amber) +
          indicatorLines.map((line, k) => tipRow(line.label, num(line.values[i], 2), PALETTE[(k + 1) % PALETTE.length])).join('') +
          indicatorPaneLines.map((line, k) => tipRow(line.label, num(line.values[i], 4), PALETTE[(k + 1) % PALETTE.length])).join('') +
          (isNum(indicatorHistogram[i]) ? tipRow('Histogram', num(indicatorHistogram[i], 4)) : '') +
          legs.map((l, k) => tipRow(`${l.side[0]} ${l.strike} ${l.type}`, num(l.close[i], 2), PALETTE[k % PALETTE.length])).join(''));
      }).on('mouseleave', () => { cross.style('display', 'none'); hideTip(); });
  });
  return (index) => { replayIndex = index; redraw(); };
}

// ---------------------------------------------------------------- EMA verification chart
export function emaVerificationChart(el, data) {
  mount(el, (W) => {
    const bars = data.bars || [];
    if (!bars.length) return;
    const m = { l: 62, r: 14, t: 14, b: 30 }, H = 330, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleLinear().domain([-0.5, bars.length - 0.5]).range([0, iw]);
    const values = bars.flatMap((bar) => [bar.high, bar.low])
      .concat((data.lines || []).flatMap((line) => line.values.filter(isNum)));
    const ext = d3.extent(values);
    const pad = (ext[1] - ext[0]) * 0.06 || 1;
    const y = d3.scaleLinear().domain([ext[0] - pad, ext[1] + pad]).range([ih, 0]);
    yAxis(g, y, iw, (v) => d3.format(',.0f')(v), 5);
    const candleWidth = Math.max(1.5, Math.min(8, iw / bars.length * 0.68));
    bars.forEach((bar, i) => {
      const up = bar.close >= bar.open;
      const color = up ? COLORS.green : COLORS.red;
      const cx = x(i);
      if (bar.selected) {
        g.append('rect').attr('x', cx - candleWidth * 0.62).attr('y', 0)
          .attr('width', candleWidth * 1.24).attr('height', ih)
          .attr('fill', COLORS.accent).attr('opacity', 0.035);
      }
      g.append('line').attr('x1', cx).attr('x2', cx).attr('y1', y(bar.high)).attr('y2', y(bar.low))
        .attr('stroke', color).attr('stroke-width', 1);
      g.append('rect').attr('x', cx - candleWidth / 2)
        .attr('y', y(Math.max(bar.open, bar.close)))
        .attr('width', candleWidth)
        .attr('height', Math.max(1, Math.abs(y(bar.open) - y(bar.close))))
        .attr('fill', color).attr('stroke', color);
    });
    (data.lines || []).forEach((line, i) => {
      g.append('path').datum(line.values).attr('fill', 'none')
        .attr('stroke', PALETTE[(i + 1) % PALETTE.length]).attr('stroke-width', 1.8)
        .attr('d', d3.line().defined(isNum).x((_, j) => x(j)).y((v) => y(v)));
    });
    const changes = data.changes || [];
    const triangle = d3.symbol().type(d3.symbolTriangle).size(72);
    changes.forEach((change) => {
      const bar = bars[change.index];
      if (!bar) return;
      const bullish = change.direction > 0;
      g.append('path').attr('d', triangle()).attr('transform',
        `translate(${x(change.index)},${y(bullish ? bar.low : bar.high) + (bullish ? 8 : -8)})${bullish ? '' : ' rotate(180)'}`)
        .attr('fill', bullish ? COLORS.green : COLORS.red).attr('stroke', '#07101f').attr('stroke-width', 1);
    });
    const tickCount = Math.max(3, Math.floor(iw / 115));
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(tickCount).tickFormat((index) => bars[Math.round(index)]?.label || '').tickSize(0).tickPadding(8));
    g.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5)
      .text('NIFTY candles · signal markers are based on the labeled candle close');
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', ih)
      .attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const i = Math.max(0, Math.min(bars.length - 1,
          Math.round(x.invert(d3.pointer(ev)[0]))));
        const bar = bars[i];
        const marker = changes.find((change) => change.index === i);
        showTip(ev, `<div class="h">${esc(bar.label)}</div>` +
          tipRow('Open', num(bar.open, 2)) + tipRow('High', num(bar.high, 2)) +
          tipRow('Low', num(bar.low, 2)) + tipRow('Close', num(bar.close, 2)) +
          (data.lines || []).map((line, k) => tipRow(line.label, num(line.values[i], 2),
            PALETTE[(k + 1) % PALETTE.length])).join('') +
          (marker ? tipRow('Signal', `${marker.reversal ? 'Reversal · ' : ''}${marker.direction > 0 ? 'Bullish' : 'Bearish'}`) : ''));
      }).on('mouseleave', hideTip);
  });
}

// ---------------------------------------------------------------- NIFTY trade execution verification
export function niftyTradeChart(el, data) {
  mount(el, (W) => {
    const bars = data.bars || [];
    if (!bars.length) return;
    const m = { l: 62, r: 14, t: 14, b: 30 }, H = 330, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleLinear().domain([-0.5, bars.length - 0.5]).range([0, iw]);
    const ext = d3.extent(bars.flatMap((bar) => [bar.high, bar.low]));
    const pad = (ext[1] - ext[0]) * 0.06 || 1;
    const y = d3.scaleLinear().domain([ext[0] - pad, ext[1] + pad]).range([ih, 0]);
    yAxis(g, y, iw, (value) => d3.format(',.0f')(value), 5);
    const candleWidth = Math.max(1.5, Math.min(8, iw / bars.length * 0.68));
    bars.forEach((bar, i) => {
      const color = bar.close >= bar.open ? COLORS.green : COLORS.red;
      const cx = x(i);
      g.append('line').attr('x1', cx).attr('x2', cx).attr('y1', y(bar.high)).attr('y2', y(bar.low))
        .attr('stroke', color).attr('stroke-width', 1);
      g.append('rect').attr('x', cx - candleWidth / 2)
        .attr('y', y(Math.max(bar.open, bar.close))).attr('width', candleWidth)
        .attr('height', Math.max(1, Math.abs(y(bar.open) - y(bar.close))))
        .attr('fill', color).attr('stroke', color);
    });
    const events = data.events || [];
    const eventsAt = new Map();
    events.forEach((event) => {
      if (!eventsAt.has(event.index)) eventsAt.set(event.index, []);
      eventsAt.get(event.index).push(event);
    });
    const triangle = d3.symbol().type(d3.symbolTriangle).size(64);
    events.forEach((event) => {
      const bar = bars[event.index];
      if (!bar) return;
      const sameKind = eventsAt.get(event.index).filter((item) => item.kind === event.kind);
      const offset = sameKind.indexOf(event) * 9;
      const entry = event.kind === 'ENTRY';
      g.append('path').attr('d', triangle()).attr('transform',
        `translate(${x(event.index)},${y(entry ? bar.low : bar.high) + (entry ? 8 + offset : -8 - offset)})${entry ? '' : ' rotate(180)'}`)
        .attr('fill', entry ? COLORS.green : COLORS.red).attr('stroke', '#07101f').attr('stroke-width', 1);
    });
    const tickCount = Math.max(3, Math.floor(iw / 105));
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(tickCount)
        .tickFormat((index) => minutesToHM(Math.max(0, Math.min(bars.length - 1, Math.round(index)))))
        .tickSize(0).tickPadding(8));
    g.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted).attr('font-size', 10.5)
      .text('NIFTY candles · green ▲ entry · red ▼ exit');
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', ih)
      .attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const i = Math.max(0, Math.min(bars.length - 1, Math.round(x.invert(d3.pointer(ev)[0]))));
        const bar = bars[i];
        const candleTime = minutesToHM(i);
        const executionRows = (eventsAt.get(i) || []).map((event) => {
          const label = `${event.kind} · ${event.side} ${event.strike} ${event.option_type} · ${event.leg_id}#${event.segment}`;
          const details = `${num(event.option_price, 2)} option fill${event.reason ? ` · ${event.reason}` : ''} · net ${inr(event.net_pnl, true)}`;
          return tipRow(label, esc(details), event.kind === 'ENTRY' ? COLORS.green : COLORS.red);
        }).join('');
        showTip(ev, `<div class="h">${dLong(data.date)} ${candleTime}</div>` +
          tipRow('Open', num(bar.open, 2)) + tipRow('High', num(bar.high, 2)) +
          tipRow('Low', num(bar.low, 2)) + tipRow('Close', num(bar.close, 2)) +
          (executionRows || '<div class="r"><span>No trade execution at this candle</span></div>'));
      }).on('mouseleave', hideTip);
  });
}

// ---------------------------------------------------------------- full-duration indicator trade verification
export function niftyBacktestChart(el, data) {
  const bars = data.bars || [];
  if (!bars.length) return;
  const barWidth = 8, pane = data.pane, height = pane ? 470 : 370;
  const margin = { left: 62, right: 12, top: 22, bottom: 34 };
  const priceHeight = pane ? 300 : height;
  const paneTop = 320;
  const paneBottom = height - margin.bottom - 20;
  const count = bars.length;
  const lines = data.lines || [];
  const eventMap = new Map();
  (data.events || []).forEach((event) => {
    if (!eventMap.has(event.index)) eventMap.set(event.index, []);
    eventMap.get(event.index).push(event);
  });
  const axis = h('canvas', { style: { display: 'block', flex: `0 0 ${margin.left}px`, width: `${margin.left}px`, height: `${height}px` } });
  const scroll = h('div', { style: { overflowX: 'auto', overflowY: 'hidden', flex: '1', minWidth: '0' } });
  const inner = h('div', { style: { position: 'relative', width: `${count * barWidth}px`, height: `${height}px` } });
  const canvas = h('canvas', { style: { position: 'sticky', left: '0', display: 'block', height: `${height}px`, zIndex: '1' } });
  inner.append(canvas);
  scroll.append(inner);
  clear(el).append(h('div', { style: { display: 'flex', width: '100%', height: `${height}px` } }, axis, scroll));
  const context = canvas.getContext('2d');
  const axisContext = axis.getContext('2d');
  let frame = 0;
  let canvasWidth = 0;
  const axisWidth = margin.left;
  const draw = () => {
    frame = 0;
    const width = Math.floor(scroll.clientWidth);
    if (width < 40) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvasWidth !== width || canvas.height !== Math.round(height * dpr)) {
      canvasWidth = width;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      axis.width = Math.round(axisWidth * dpr);
      axis.height = Math.round(height * dpr);
    }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    axisContext.setTransform(dpr, 0, 0, dpr, 0, 0);
    axisContext.clearRect(0, 0, axisWidth, height);
    const plotHeight = priceHeight - margin.top - margin.bottom;
    const scrollLeft = scroll.scrollLeft;
    const first = Math.max(0, Math.floor(scrollLeft / barWidth));
    const last = Math.min(count - 1, Math.ceil((scrollLeft + width) / barWidth));
    const visibleValues = [];
    for (let index = first; index <= last; index += 1) {
      visibleValues.push(bars[index].high, bars[index].low);
      lines.forEach((line) => {
        if (Number.isFinite(line.values[index])) visibleValues.push(line.values[index]);
      });
    }
    let [minPrice, maxPrice] = d3.extent(visibleValues.filter(Number.isFinite));
    if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice)) return;
    const padding = (maxPrice - minPrice) * 0.05 || 1;
    minPrice -= padding;
    maxPrice += padding;
    const priceY = (value) => margin.top + (maxPrice - value) / (maxPrice - minPrice) * plotHeight;
    const y = priceY;
    context.font = '11px sans-serif';
    context.textBaseline = 'middle';
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = maxPrice - (maxPrice - minPrice) * tick / 4;
      const py = priceY(value);
      context.strokeStyle = 'rgba(255,255,255,.08)';
      context.beginPath(); context.moveTo(0, py); context.lineTo(width, py); context.stroke();
      axisContext.fillStyle = '#8b97b8';
      axisContext.textAlign = 'right';
      axisContext.fillText(d3.format(',.0f')(value), axisWidth - 8, py);
      axisContext.strokeStyle = 'rgba(255,255,255,.08)';
      axisContext.beginPath(); axisContext.moveTo(axisWidth - 4, py); axisContext.lineTo(axisWidth, py); axisContext.stroke();
    }
    const titleStart = bars[first].date;
    const titleEnd = bars[last].date;
    context.fillStyle = '#8b97b8';
    context.textAlign = 'left';
    context.textBaseline = 'top';
    context.fillText(`NIFTY · ${dLong(titleStart)}${titleEnd !== titleStart ? ` – ${dLong(titleEnd)}` : ''} · ${data.timeframe} candles`, 4, 3);
    lines.forEach((line, lineIndex) => {
      context.beginPath();
      let drawing = false;
      for (let index = first; index <= last; index += 1) {
        const value = line.values[index];
        if (!Number.isFinite(value)) {
          drawing = false;
          continue;
        }
        const x = index * barWidth - scrollLeft;
        if (drawing) context.lineTo(x, y(value));
        else {
          context.moveTo(x, y(value));
          drawing = true;
        }
      }
      context.strokeStyle = PALETTE[(lineIndex + 1) % PALETTE.length];
      context.lineWidth = 1.8;
      context.stroke();
    });
    const candleWidth = Math.max(1, barWidth * 0.62);
    const labelStep = Math.max(1, Math.ceil((last - first + 1) / Math.max(3, width / 110)));
    let lastLabelIndex = -Infinity;
    for (let index = first; index <= last; index += 1) {
      const x = index * barWidth - scrollLeft;
      const bar = bars[index];
      const isNewSession = index === 0 || bar.date !== bars[index - 1].date;
      const up = bar.close >= bar.open;
      const color = up ? COLORS.green : COLORS.red;
      if (isNewSession) {
        context.strokeStyle = 'rgba(255,255,255,.16)';
        context.beginPath(); context.moveTo(x, margin.top); context.lineTo(x, priceHeight - margin.bottom); context.stroke();
      }
      context.strokeStyle = color;
      context.beginPath(); context.moveTo(x, y(bar.high)); context.lineTo(x, y(bar.low)); context.stroke();
      context.fillStyle = color;
      const bodyTop = y(Math.max(bar.open, bar.close));
      const bodyBottom = y(Math.min(bar.open, bar.close));
      context.fillRect(x - candleWidth / 2, bodyTop, candleWidth, Math.max(1, bodyBottom - bodyTop));
      const events = eventMap.get(index) || [];
      events.forEach((event, eventIndex) => {
        const entry = event.kind === 'ENTRY';
        const markerY = y(entry ? bar.low : bar.high) + (entry ? 8 : -8) +
          (entry ? 1 : -1) * eventIndex * 7;
        context.fillStyle = entry ? COLORS.green : COLORS.red;
        context.beginPath();
        context.moveTo(x, markerY + (entry ? 4 : -4));
        context.lineTo(x - 4, markerY + (entry ? -3 : 3));
        context.lineTo(x + 4, markerY + (entry ? -3 : 3));
        context.closePath();
        context.fill();
      });
      const enoughLabelSpace = index - lastLabelIndex >= labelStep;
      const showDate = isNewSession && enoughLabelSpace;
      if (showDate || (enoughLabelSpace && (index === first || index % labelStep === 0))) {
        context.fillStyle = '#8b97b8';
        context.textAlign = 'center';
        context.textBaseline = 'top';
        const tickLabel = data.timeframe.endsWith('m') && !isNewSession
          ? bar.time : dShort(bar.date);
        context.fillText(tickLabel, Math.max(35, Math.min(width - 35, x)), (pane ? height : priceHeight) - margin.bottom + 8);
        lastLabelIndex = index;
      }
    }
    context.strokeStyle = 'rgba(255,255,255,.22)';
    const priceAxisY = priceHeight - margin.bottom;
    context.beginPath(); context.moveTo(0, priceAxisY); context.lineTo(width, priceAxisY); context.stroke();
    if (pane) {
      const paneValues = [];
      pane.lines.forEach((line) => {
        for (let index = first; index <= last; index += 1) {
          if (Number.isFinite(line.values[index])) paneValues.push(line.values[index]);
        }
      });
      for (let index = first; index <= last; index += 1) {
        if (Number.isFinite(pane.histogram[index])) paneValues.push(pane.histogram[index]);
      }
      let [minValue, maxValue] = d3.extent(paneValues.filter(Number.isFinite));
      if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) {
        minValue = -1;
        maxValue = 1;
      }
      const extent = Math.max(Math.abs(minValue), Math.abs(maxValue)) || 1;
      minValue = -extent * 1.1;
      maxValue = extent * 1.1;
      const oscY = (value) => paneBottom - (value - minValue) /
        (maxValue - minValue) * (paneBottom - paneTop);
      const zeroY = oscY(0);
      context.strokeStyle = 'rgba(255,255,255,.08)';
      context.fillStyle = '#8b97b8';
      context.font = '11px sans-serif';
      context.textAlign = 'right';
      context.textBaseline = 'middle';
      for (let tick = 0; tick <= 2; tick += 1) {
        const value = maxValue - (maxValue - minValue) * tick / 2;
        const py = oscY(value);
        context.beginPath(); context.moveTo(0, py); context.lineTo(width, py); context.stroke();
        axisContext.fillStyle = '#8b97b8';
        axisContext.textAlign = 'right';
        axisContext.fillText(value.toFixed(2), axisWidth - 8, py);
        axisContext.strokeStyle = 'rgba(255,255,255,.08)';
        axisContext.beginPath(); axisContext.moveTo(axisWidth - 4, py); axisContext.lineTo(axisWidth, py); axisContext.stroke();
      }
      context.fillStyle = '#8b97b8';
      context.textAlign = 'left';
      context.textBaseline = 'top';
      context.fillText(pane.label, 4, paneTop - 17);
      context.strokeStyle = 'rgba(255,255,255,.28)';
      context.beginPath(); context.moveTo(0, zeroY); context.lineTo(width, zeroY); context.stroke();
      for (let index = first; index <= last; index += 1) {
        const value = pane.histogram[index];
        if (!Number.isFinite(value)) continue;
        const x = index * barWidth - scrollLeft;
        const yValue = oscY(value);
        context.fillStyle = value >= 0 ? 'rgba(34,197,94,.55)' : 'rgba(239,68,68,.55)';
        context.fillRect(x - barWidth * 0.3, Math.min(zeroY, yValue),
          Math.max(1, barWidth * 0.6), Math.max(1, Math.abs(zeroY - yValue)));
      }
      pane.lines.forEach((line, lineIndex) => {
        context.beginPath();
        let drawing = false;
        for (let index = first; index <= last; index += 1) {
          const value = line.values[index];
          if (!Number.isFinite(value)) {
            drawing = false;
            continue;
          }
          const x = index * barWidth - scrollLeft;
          if (drawing) context.lineTo(x, oscY(value));
          else {
            context.moveTo(x, oscY(value));
            drawing = true;
          }
        }
        context.strokeStyle = PALETTE[(lineIndex + 1) % PALETTE.length];
        context.lineWidth = 1.8;
        context.stroke();
      });
      context.strokeStyle = 'rgba(255,255,255,.22)';
      context.beginPath(); context.moveTo(0, paneBottom); context.lineTo(width, paneBottom); context.stroke();
    }
  };
  const scheduleDraw = () => {
    if (!frame) frame = requestAnimationFrame(draw);
  };
  scroll.addEventListener('scroll', scheduleDraw, { passive: true });
  const resizeObserver = new ResizeObserver(scheduleDraw);
  resizeObserver.observe(el);
  canvas.addEventListener('mousemove', (event) => {
    const rect = canvas.getBoundingClientRect();
    const index = Math.max(0, Math.min(count - 1,
      Math.floor((event.clientX - rect.left + scroll.scrollLeft) / barWidth)));
    const bar = bars[index];
    const executions = (eventMap.get(index) || []).map((item) => {
      const label = `${item.kind} ${item.time ? `@ ${item.time} · ` : '· '}${item.side} ${item.strike} ${item.option_type} · ${item.leg_id}#${item.segment}`;
      const value = `${num(item.option_price, 2)} option fill${item.reason ? ` · ${item.reason}` : ''} · net ${inr(item.net_pnl, true)}`;
      return tipRow(label, esc(value), item.kind === 'ENTRY' ? COLORS.green : COLORS.red);
    }).join('');
    showTip(event, `<div class="h">${dLong(bar.date)} · ${bar.time} ${data.timeframe}</div>` +
      tipRow('Open', num(bar.open, 2)) + tipRow('High', num(bar.high, 2)) +
      tipRow('Low', num(bar.low, 2)) + tipRow('Close', num(bar.close, 2)) +
      lines.map((line, lineIndex) => tipRow(line.label,
        Number.isFinite(line.values[index]) ? num(line.values[index], 2) : '—',
        PALETTE[(lineIndex + 1) % PALETTE.length])).join('') +
      (pane ? pane.lines.map((line, lineIndex) => tipRow(line.label,
        Number.isFinite(line.values[index]) ? num(line.values[index], 4) : '—',
        PALETTE[(lineIndex + 1) % PALETTE.length])).join('') +
        tipRow('Histogram', Number.isFinite(pane.histogram[index]) ? num(pane.histogram[index], 4) : '—') : '') +
      (executions || '<div class="r"><span>No trade execution at this candle</span></div>'));
  });
  canvas.addEventListener('mouseleave', hideTip);
  scheduleDraw();
}

// ---------------------------------------------------------------- standalone market chart
export function marketChart(el, data, studies = [], options = {}) {
  const bars = data.bars || [];
  if (!bars.length) return;
  const ema = (values, period) => {
    const out = Array(values.length).fill(NaN), alpha = 2 / (period + 1);
    for (let i = period - 1; i < values.length; i += 1) {
      if (i === period - 1 || !Number.isFinite(out[i - 1])) {
        const seed = values.slice(i - period + 1, i + 1);
        if (seed.every(Number.isFinite)) out[i] = seed.reduce((a, b) => a + b, 0) / period;
      } else out[i] = alpha * values[i] + (1 - alpha) * out[i - 1];
    }
    return out;
  };
  const sma = (values, period) => values.map((_, i) => {
    if (i < period - 1) return NaN;
    const sample = values.slice(i - period + 1, i + 1);
    return sample.every(Number.isFinite) ? sample.reduce((a, b) => a + b, 0) / period : NaN;
  });
  const close = bars.map((bar) => bar.close), lines = [], panes = [], clouds = [];
  const highs = bars.map((bar) => bar.high), lows = bars.map((bar) => bar.low);
  let trendlines = options.trendlines || [];
  let futureBars = 0;
  const midpoint = (period) => close.map((_, i) => {
    if (i + 1 < period) return NaN;
    return (Math.max(...highs.slice(i - period + 1, i + 1)) +
      Math.min(...lows.slice(i - period + 1, i + 1))) / 2;
  });
  studies.forEach((study) => {
    const period = Math.max(2, Math.min(200, Number(study.period) || 20));
    if (study.type === 'EMA') lines.push({ name: `EMA ${period}`, values: ema(close, period) });
    if (study.type === 'SMA') lines.push({ name: `SMA ${period}`, values: sma(close, period) });
    if (study.type === 'Bollinger') {
      const mid = sma(close, period);
      const upper = mid.map((v, i) => {
        if (!Number.isFinite(v)) return NaN;
        const slice = close.slice(i - period + 1, i + 1);
        const deviation = Math.sqrt(slice.reduce((sum, value) => sum + (value - v) ** 2, 0) / period);
        return v + deviation * (Number(study.multiplier) || 2);
      });
      lines.push({ name: 'BB upper', values: upper, color: COLORS.violet },
        { name: 'BB lower', values: upper.map((v, i) => 2 * mid[i] - v), color: COLORS.violet },
        { name: 'BB mid', values: mid, color: COLORS.muted });
    }
    if (study.type === 'VWAP') {
      let total = 0, volume = 0, typicalTotal = 0, count = 0;
      const values = bars.map((bar) => {
        const typical = (bar.high + bar.low + bar.close) / 3, weight = Number(bar.volume) || 0;
        total += typical * weight; volume += weight; typicalTotal += typical; count += 1;
        return volume ? total / volume : typicalTotal / count;
      });
      lines.push({ name: 'VWAP', values, color: COLORS.amber });
    }
    if (study.type === 'RSI') {
      const n = Math.max(2, Math.min(100, period)), values = Array(close.length).fill(NaN);
      let gain = 0, loss = 0;
      for (let i = 1; i < close.length; i += 1) {
        const change = close[i] - close[i - 1];
        if (i <= n) {
          gain += Math.max(change, 0); loss += Math.max(-change, 0);
          if (i === n) values[i] = loss ? 100 - 100 / (1 + gain / loss) : 100;
        } else {
          gain = (gain * (n - 1) + Math.max(change, 0)) / n;
          loss = (loss * (n - 1) + Math.max(-change, 0)) / n;
          values[i] = loss ? 100 - 100 / (1 + gain / loss) : 100;
        }
      }
      panes.push({ name: `RSI ${n}`, values, domain: [0, 100], thresholds: [30, 70] });
    }
    if (study.type === 'MACD') {
      const fastN = Math.max(2, Number(study.fast) || 12);
      const slowN = Math.max(fastN + 1, Number(study.slow) || 26);
      const signalN = Math.max(2, Number(study.signal) || 9);
      const fast = ema(close, fastN), slow = ema(close, slowN);
      const macd = fast.map((value, i) => value - slow[i]), signal = ema(macd, signalN);
      panes.push({ name: `MACD ${fastN}/${slowN}/${signalN}`, values: macd, signal,
        histogram: macd.map((value, i) => value - signal[i]) });
    }
    if (study.type === 'Ichimoku') {
      const tenkan = midpoint(9), kijun = midpoint(26), spanB = midpoint(52);
      const spanA = tenkan.map((value, i) => (value + kijun[i]) / 2);
      const shifted = (values) => [...Array(26).fill(NaN), ...values];
      clouds.push({ first: shifted(spanA), second: shifted(spanB) });
      lines.push({ name: 'Tenkan-sen', values: tenkan, color: COLORS.accent },
        { name: 'Kijun-sen', values: kijun, color: COLORS.amber },
        { name: 'Senkou A', values: shifted(spanA), color: COLORS.green },
        { name: 'Senkou B', values: shifted(spanB), color: COLORS.red });
      futureBars = Math.max(futureBars, 26);
    }
    if (study.type === 'Supertrend') {
      const n = Math.max(2, Math.min(100, period));
      const multiplier = Math.max(.5, Math.min(10, Number(study.multiplier) || 3));
      const atr = Array(close.length).fill(NaN), upper = Array(close.length).fill(NaN);
      const lower = Array(close.length).fill(NaN), trend = Array(close.length).fill(NaN);
      const tr = close.map((value, i) => Math.max(highs[i] - lows[i],
        i ? Math.abs(highs[i] - close[i - 1]) : 0, i ? Math.abs(lows[i] - close[i - 1]) : 0));
      for (let i = n - 1; i < close.length; i += 1) {
        atr[i] = i === n - 1 ? tr.slice(0, n).reduce((a, b) => a + b, 0) / n
          : (atr[i - 1] * (n - 1) + tr[i]) / n;
        const mid = (highs[i] + lows[i]) / 2;
        const basicUpper = mid + multiplier * atr[i], basicLower = mid - multiplier * atr[i];
        upper[i] = i === n - 1 || basicUpper < upper[i - 1] || close[i - 1] > upper[i - 1]
          ? basicUpper : upper[i - 1];
        lower[i] = i === n - 1 || basicLower > lower[i - 1] || close[i - 1] < lower[i - 1]
          ? basicLower : lower[i - 1];
        trend[i] = i === n - 1 ? (close[i] >= mid ? 1 : -1)
          : trend[i - 1] === 1 ? (close[i] < lower[i] ? -1 : 1)
            : (close[i] > upper[i] ? 1 : -1);
      }
      lines.push({ name: 'Supertrend up', values: trend.map((v, i) => v > 0 ? lower[i] : NaN), color: COLORS.green },
        { name: 'Supertrend down', values: trend.map((v, i) => v < 0 ? upper[i] : NaN), color: COLORS.red });
    }
    if (study.type === 'Volume') {
      panes.push({ name: 'Volume', histogram: bars.map((bar) => Number(bar.volume) || 0),
        histogramColors: bars.map((bar) => bar.close >= bar.open ? COLORS.green : COLORS.red), domain: [0, 1] });
    }
    if (study.type === 'Open Interest') {
      if (bars.some((bar) => Number.isFinite(bar.oi))) {
        panes.push({ name: 'Open Interest', values: bars.map((bar) => Number.isFinite(bar.oi) ? bar.oi : NaN) });
      }
    }
  });
  mount(el, (W) => {
    const margin = { left: 62, right: 16, top: 24, bottom: 30 };
    const priceHeight = 350, paneHeight = 115, gap = 16;
    const plotHeight = priceHeight + panes.length * (paneHeight + gap);
    const navigatorHeight = 36, navigatorGap = 12;
    const navigatorTop = margin.top + plotHeight + margin.bottom + navigatorGap;
    const H = navigatorTop + navigatorHeight + 10;
    const iw = W - margin.left - margin.right, svg = svgBase(el, W, H);
    const plot = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);
    const x = d3.scaleLinear().domain([-.5, bars.length + futureBars - .5]).range([0, iw]);
    const prices = bars.flatMap((bar) => [bar.high, bar.low])
      .concat(...lines.map((line) => line.values.filter(Number.isFinite)));
    let [low, high] = d3.extent(prices);
    const padding = (high - low) * .06 || 1;
    const initialYDomain = [low - padding, high + padding];
    let manualYDomain = Array.isArray(data.yDomain) && data.yDomain.length === 2;
    const y = d3.scaleLinear().domain(data.yDomain || initialYDomain).nice().range([priceHeight, 0]);
    const xGrid = plot.append('g').attr('class', 'gridline').attr('transform', `translate(0,${priceHeight})`);
    const xAxis = plot.append('g').attr('class', 'axis').attr('transform', `translate(0,${priceHeight})`);
    const yGrid = plot.append('g').attr('class', 'gridline');
    const yAxisGroup = plot.append('g').attr('class', 'axis');
    const priceLayer = plot.append('g'), eventLayer = plot.append('g'), drawingLayer = plot.append('g');
    const paneGroups = panes.map((pane, i) => ({
      pane, g: plot.append('g').attr('transform', `translate(0,${priceHeight + gap + i * (paneHeight + gap)})`),
    }));
    const navigatorX = x.copy();
    const navigatorY = d3.scaleLinear().domain(d3.extent(close)).range([navigatorHeight - 5, 5]);
    const navigator = svg.append('g').attr('class', 'chart-range-navigator')
      .attr('transform', `translate(${margin.left},${navigatorTop})`);
    navigator.append('rect').attr('class', 'chart-range-track')
      .attr('width', iw).attr('height', navigatorHeight).attr('rx', 4);
    navigator.append('path').datum(close).attr('class', 'chart-range-area')
      .attr('d', d3.area().defined(Number.isFinite)
        .x((_, i) => navigatorX(i)).y0(navigatorHeight).y1((value) => navigatorY(value)));
    navigator.append('path').datum(close).attr('class', 'chart-range-line')
      .attr('d', d3.line().defined(Number.isFinite)
        .x((_, i) => navigatorX(i)).y((value) => navigatorY(value)));
    const brush = d3.brushX().extent([[0, 0], [iw, navigatorHeight]]);
    const brushGroup = navigator.append('g').attr('class', 'chart-range-brush').call(brush);
    svg.append('text').attr('x', margin.left).attr('y', 15).attr('fill', COLORS.muted).attr('font-size', 11)
      .text(`${data.title} · ${data.timeframe} · ${bars[0].time.replace('T', ' ')} → ${bars[bars.length - 1].time.replace('T', ' ')}`);
    const draw = (scaleX) => {
      const sx = scaleX || x;
      if (!manualYDomain) {
        const firstVisible = Math.max(0, Math.floor(sx.invert(0)));
        const lastVisible = Math.min(bars.length - 1, Math.ceil(sx.invert(iw)));
        const visiblePrices = [];
        for (let index = firstVisible; index <= lastVisible; index += 1) {
          visiblePrices.push(bars[index].high, bars[index].low);
          lines.forEach((line) => {
            if (Number.isFinite(line.values[index])) visiblePrices.push(line.values[index]);
          });
          clouds.forEach((cloud) => {
            if (Number.isFinite(cloud.first[index])) visiblePrices.push(cloud.first[index]);
            if (Number.isFinite(cloud.second[index])) visiblePrices.push(cloud.second[index]);
          });
        }
        const extent = d3.extent(visiblePrices.filter(Number.isFinite));
        if (Number.isFinite(extent[0]) && Number.isFinite(extent[1])) {
          const padding = (extent[1] - extent[0]) * .06 || 1;
          y.domain([extent[0] - padding, extent[1] + padding]).nice();
        }
      }
      const ticks = Math.min(10, bars.length);
      const tickFormat = (i) => {
        const bar = bars[Math.max(0, Math.min(bars.length - 1, Math.round(i)))];
        return bar ? bar.time.replace('T', ' ').slice(5) : '';
      };
      xGrid.call(d3.axisBottom(sx).ticks(ticks).tickSize(-priceHeight).tickFormat(''));
      xAxis.call(d3.axisBottom(sx).ticks(ticks).tickSize(0).tickPadding(8).tickFormat(tickFormat));
      yGrid.call(d3.axisLeft(y).ticks(6).tickSize(-iw).tickFormat(''));
      yAxisGroup.call(d3.axisLeft(y).ticks(6).tickFormat((v) =>
        Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })));
      priceLayer.selectAll('*').remove();
      eventLayer.selectAll('*').remove();
      drawingLayer.selectAll('*').remove();
      priceLayer.selectAll('.ichimoku-cloud').data(clouds).join('path')
        .attr('class', 'ichimoku-cloud')
        .attr('d', (cloud) => d3.area().defined((_, i) =>
          Number.isFinite(cloud.first[i]) && Number.isFinite(cloud.second[i]))
          .x((_, i) => sx(i)).y0((_, i) => y(cloud.first[i])).y1((_, i) => y(cloud.second[i]))
          (cloud.first))
        .attr('fill', (cloud) => cloud.first.filter(Number.isFinite).slice(-1)[0] >=
          cloud.second.filter(Number.isFinite).slice(-1)[0] ? COLORS.green : COLORS.red)
        .attr('opacity', .14);
      priceLayer.selectAll('.study-line').data(lines).join('path').attr('class', 'study-line')
        .attr('fill', 'none').attr('stroke', (line, i) => line.color || PALETTE[i % PALETTE.length])
        .attr('stroke-width', 1.5)
        .attr('d', (line) => d3.line().defined(Number.isFinite)
          .x((value, i) => sx(i)).y((value) => y(value))(line.values));
      const candleWidth = Math.max(1, Math.min(12, Math.abs(sx(1) - sx(0)) * .64));
      const candle = priceLayer.selectAll('.candle').data(bars).join('g')
        .attr('class', 'candle').attr('transform', (_, i) => `translate(${sx(i)},0)`);
      candle.append('line').attr('y1', (bar) => y(bar.high)).attr('y2', (bar) => y(bar.low))
        .attr('stroke', (bar) => bar.close >= bar.open ? COLORS.green : COLORS.red);
      candle.append('rect').attr('x', -candleWidth / 2)
        .attr('y', (bar) => y(Math.max(bar.open, bar.close))).attr('width', candleWidth)
        .attr('height', (bar) => Math.max(1, Math.abs(y(bar.open) - y(bar.close))))
        .attr('fill', (bar) => bar.close >= bar.open ? COLORS.green : COLORS.red);
      const events = (data.events || []).filter((event) => Number.isFinite(event.price));
      eventLayer.selectAll('.trade-marker').data(events).join('path').attr('class', 'trade-marker')
        .attr('d', d3.symbol().type(d3.symbolTriangle).size(70))
        .attr('transform', (event) => `translate(${sx(event.index)},${y(event.price)}) rotate(${event.kind === 'ENTRY' ? 0 : 180})`)
        .attr('fill', (event) => event.kind === 'ENTRY' ? COLORS.green : COLORS.red)
        .attr('stroke', '#07101f').attr('stroke-width', 1);
      const indexByTime = new Map(bars.map((bar, index) => [bar.time, index]));
      drawingLayer.selectAll('.trendline').data(trendlines).join('line')
        .attr('class', 'trendline').attr('x1', (line) => sx(indexByTime.get(line.startTime) ?? 0))
        .attr('x2', (line) => sx(indexByTime.get(line.endTime) ?? 0))
        .attr('y1', (line) => y(line.startPrice)).attr('y2', (line) => y(line.endPrice))
        .attr('stroke', COLORS.amber).attr('stroke-width', 2).attr('stroke-dasharray', '6 3');
      paneGroups.forEach(({ pane, g }) => {
        g.selectAll('*').remove();
        const range = pane.domain || d3.extent([...pane.values, ...(pane.signal || [])].filter(Number.isFinite));
        const values = pane.histogram || pane.values || [];
        let domain = range && range[0] !== range[1] ? range : d3.extent(values.filter(Number.isFinite));
        if (!domain || !Number.isFinite(domain[0]) || !Number.isFinite(domain[1])) domain = [0, 1];
        if (pane.histogram) domain = [0, Math.max(1, d3.max(pane.histogram) || 1)];
        if (domain[0] === domain[1]) domain = [domain[0] - 1, domain[1] + 1];
        const oy = d3.scaleLinear().domain(domain).nice().range([paneHeight, 0]);
        g.append('g').attr('class', 'gridline').call(d3.axisLeft(oy).ticks(3).tickSize(-iw).tickFormat(''));
        g.append('g').attr('class', 'axis').call(d3.axisLeft(oy).ticks(3));
        (pane.thresholds || []).forEach((value) => g.append('line').attr('x1', 0).attr('x2', iw)
          .attr('y1', oy(value)).attr('y2', oy(value)).attr('stroke', COLORS.muted).attr('stroke-dasharray', '4 3'));
        if (pane.histogram) g.selectAll('.histogram').data(pane.histogram
          .map((value, index) => ({ value, index })).filter((item) => Number.isFinite(item.value))).join('rect')
          .attr('class', 'histogram').attr('x', (item) => sx(item.index) - candleWidth / 2).attr('width', candleWidth)
          .attr('y', (item) => oy(Math.max(0, item.value)))
          .attr('height', (item) => Math.max(1, Math.abs(oy(item.value) - oy(0))))
          .attr('fill', (item) => pane.histogramColors?.[item.index] ||
            (item.value >= 0 ? COLORS.green : COLORS.red)).attr('opacity', .65);
        if (pane.values) {
          const oscillatorLines = [{ values: pane.values, color: COLORS.accent },
            ...(pane.signal ? [{ values: pane.signal, color: COLORS.amber }] : [])];
          g.selectAll('.oscillator-line').data(oscillatorLines).join('path').attr('class', 'oscillator-line')
            .attr('fill', 'none').attr('stroke', (line) => line.color).attr('stroke-width', 1.4)
            .attr('d', (line) => d3.line().defined(Number.isFinite)
              .x((value, i) => sx(i)).y((value) => oy(value))(line.values));
        }
        g.append('text').attr('x', 6).attr('y', 12).attr('fill', COLORS.muted)
          .attr('font-size', 10).text(pane.name);
      });
    };
    draw(x);
    let olderRequested = false;
    let trendStart = null;
    const zoom = d3.zoom().scaleExtent([1, 25]).extent([[0, 0], [iw, H]])
      .translateExtent([[-iw * (bars.length + futureBars), 0], [iw, H]])
      .on('zoom', (event) => {
        const visibleX = event.transform.rescaleX(x);
        draw(visibleX);
        const range = [
          Math.max(0, Math.min(iw, navigatorX(visibleX.invert(0)))),
          Math.max(0, Math.min(iw, navigatorX(visibleX.invert(iw)))),
        ];
        brushGroup.call(brush.move, range);
        const pointerPan = event.sourceEvent &&
          ['mousemove', 'pointermove', 'touchmove'].includes(event.sourceEvent.type);
        if (pointerPan && event.transform.x > 0 && visibleX.invert(0) < 2 && !olderRequested &&
            data.has_older && options.onLoadOlder) {
          olderRequested = true;
          options.onLoadOlder(bars[0].time);
        }
      });
    let lastBrushStart = null;
    brush.on('brush end', (event) => {
      if (!event.sourceEvent) return;
      if (!event.selection) {
        lastBrushStart = null;
        svg.call(zoom.transform, d3.zoomIdentity);
        return;
      }
      const [start, end] = event.selection.map((position) => navigatorX.invert(position));
      const span = x(end) - x(start);
      const scale = Math.min(25, Math.max(1, iw / span));
      svg.call(zoom.transform, d3.zoomIdentity.scale(scale).translate(-x(start), 0));
      if (event.type === 'end') {
        const reachedOldest = lastBrushStart !== null && lastBrushStart > 1 &&
          event.selection[0] <= 1;
        lastBrushStart = event.selection[0];
        if (reachedOldest && data.has_older && options.onLoadOlder) {
          options.onLoadOlder(bars[0].time);
        }
      }
    });
    svg.call(zoom).on('dblclick.zoom', null);
    brushGroup.call(brush.move, [0, iw]);
    if (options.focusOldest && bars.length > 100) {
      svg.call(zoom.transform, d3.zoomIdentity.scale(Math.min(25, (bars.length + futureBars) / 100)));
    }
    let yDrag = null;
    plot.append('rect').attr('x', -margin.left).attr('y', 0).attr('width', margin.left - 4)
      .attr('height', priceHeight).attr('fill', 'transparent').style('cursor', 'ns-resize')
      .on('pointerdown', (event) => {
        event.stopPropagation();
        yDrag = { y: event.clientY, domain: y.domain() };
        event.currentTarget.setPointerCapture(event.pointerId);
      })
      .on('pointermove', (event) => {
        if (!yDrag) return;
        const center = (yDrag.domain[0] + yDrag.domain[1]) / 2;
        const factor = Math.exp((event.clientY - yDrag.y) / 120);
        const halfRange = (yDrag.domain[1] - yDrag.domain[0]) * factor / 2;
        const domain = [center - halfRange, center + halfRange];
        y.domain(domain);
        manualYDomain = true;
        options.onYAxisDomain?.(domain);
        draw(d3.zoomTransform(svg.node()).rescaleX(x));
      })
      .on('pointerup pointercancel', () => { yDrag = null; });
    const overlay = plot.append('rect').attr('width', iw)
      .attr('height', priceHeight + panes.length * (paneHeight + gap))
      .attr('fill', 'transparent').style('pointer-events', 'all').style('cursor', 'crosshair');
    const cross = plot.append('line').attr('y1', 0)
      .attr('y2', priceHeight + panes.length * (paneHeight + gap))
      .attr('stroke', 'rgba(255,255,255,.35)').style('display', 'none').style('pointer-events', 'none');
    overlay.on('mousemove', (event) => {
      const sx = d3.zoomTransform(svg.node()).rescaleX(x);
      const [mx] = d3.pointer(event, plot.node());
      const index = Math.max(0, Math.min(bars.length - 1, Math.round(sx.invert(mx))));
      const bar = bars[index];
      cross.attr('x1', sx(index)).attr('x2', sx(index)).style('display', null);
      const executions = (data.events || []).filter((item) => item.index === index);
      showTip(event, `<div class="h">${bar.time.replace('T', ' ')}</div>` +
        tipRow('Open', num(bar.open)) + tipRow('High', num(bar.high)) +
        tipRow('Low', num(bar.low)) + tipRow('Close', num(bar.close)) +
        tipRow('Volume', num(bar.volume, 0)) +
        (Number.isFinite(bar.oi) ? tipRow('Open interest', num(bar.oi, 0)) : '') +
        executions.map((item) => tipRow(`${item.kind} ${item.side} ${item.option_type} ${item.strike}`,
          `${num(item.price)} · ${inr(item.net_pnl, true)}`)).join(''));
    }).on('mouseleave', () => { cross.style('display', 'none'); hideTip(); })
      .on('mousedown', (event) => {
        if (!options.drawingMode) return;
        event.stopPropagation();
        const sx = d3.zoomTransform(svg.node()).rescaleX(x);
        const [mx, my] = d3.pointer(event, plot.node());
        const index = Math.max(0, Math.min(bars.length - 1, Math.round(sx.invert(mx))));
        const point = { time: bars[index].time, price: y.invert(my) };
        if (!trendStart) {
          trendStart = point;
          return;
        }
        trendlines = [...trendlines, {
          startTime: trendStart.time, startPrice: trendStart.price,
          endTime: point.time, endPrice: point.price,
        }];
        trendStart = null;
        options.onTrendlines?.(trendlines);
        draw(d3.zoomTransform(svg.node()).rescaleX(x));
      });
  });
}

// ---------------------------------------------------------------- payoff at expiry
export function payoffChart(el, p) {
  mount(el, (W) => {
    const xs = p.payoff.x, ys = p.payoff.y;
    const m = { l: 58, r: 14, t: 12, b: 28 }, H = 250, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleLinear().domain([xs[0], xs[xs.length - 1]]).range([0, iw]);
    const ext = d3.extent(ys.concat([0]));
    const pad = (ext[1] - ext[0]) * 0.08 || 1;
    const y = d3.scaleLinear().domain([ext[0] - pad, ext[1] + pad]).range([ih, 0]);
    const id = 'po' + Math.random().toString(36).slice(2, 8);
    const defs = svg.append('defs');
    defs.append('clipPath').attr('id', id + 'u').append('rect').attr('width', iw).attr('height', Math.max(0, y(0)));
    defs.append('clipPath').attr('id', id + 'd').append('rect').attr('y', y(0)).attr('width', iw).attr('height', Math.max(0, ih - y(0)));
    yAxis(g, y, iw, fmtK, 5);
    const pts = xs.map((v, i) => [v, ys[i]]);
    const area = d3.area().x((d) => x(d[0])).y0(y(0)).y1((d) => y(d[1]));
    g.append('path').datum(pts).attr('d', area).attr('fill', COLORS.green).attr('opacity', 0.3).attr('clip-path', `url(#${id}u)`);
    g.append('path').datum(pts).attr('d', area).attr('fill', COLORS.red).attr('opacity', 0.3).attr('clip-path', `url(#${id}d)`);
    g.append('path').datum(pts).attr('fill', 'none').attr('stroke', '#e6eaf5').attr('stroke-width', 2).attr('d', d3.line().x((d) => x(d[0])).y((d) => y(d[1])));
    g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.35)').attr('stroke-dasharray', '4 4');
    g.append('line').attr('x1', x(p.spot)).attr('x2', x(p.spot)).attr('y1', 0).attr('y2', ih).attr('stroke', COLORS.amber).attr('stroke-dasharray', '3 3');
    g.append('text').attr('x', x(p.spot) + 5).attr('y', 10).attr('fill', COLORS.amber).attr('font-size', 10.5).text('spot ' + Math.round(p.spot));
    p.breakevens.forEach((b) => {
      g.append('circle').attr('cx', x(b)).attr('cy', y(0)).attr('r', 5).attr('fill', COLORS.accent).attr('stroke', '#07101f').attr('stroke-width', 2);
      g.append('text').attr('x', x(b)).attr('y', y(0) - 10).attr('text-anchor', 'middle').attr('fill', COLORS.accent).attr('font-size', 10.5).text(Math.round(b));
    });
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(Math.max(3, iw / 80)).tickSize(0).tickPadding(8).tickFormat(d3.format(',.0f')));
    const bis = d3.bisector((v) => v).center;
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', ih).attr('fill', 'transparent')
      .on('mousemove', (ev) => { const i = bis(xs, x.invert(d3.pointer(ev)[0])); showTip(ev, `<div class="h">NIFTY at expiry ${Math.round(xs[i])}</div>` + tipRow('P&L', `<span class="${sign(ys[i])}">${inr(ys[i], true)}</span>`)); })
      .on('mouseleave', hideTip);
  });
}

// ---------------------------------------------------------------- optimizer heat-map
export function sweepHeat(el, res, metric, onPick) {
  mount(el, (W) => {
    const xl = res.x.labels, yl = res.y.labels;
    const cells = res.cells.filter((c) => !c.error && isNum(c[metric]));
    if (!cells.length) return;
    const m = { l: 70, r: 12, t: 10, b: 34 }, cw = Math.max(34, Math.min(90, (W - m.l - m.r) / xl.length)), ch = 36;
    const iw = cw * xl.length, ih = ch * yl.length, H = m.t + ih + m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const vals = cells.map((c) => c[metric]);
    const lowerBetter = metric === 'dd';
    const maxAbs = d3.max(vals.map(Math.abs)) || 1;
    const scale = metric === 'net' || metric === 'oos' || metric === 'ins' || metric === 'sharpe'
      ? pnlColor(maxAbs) : (() => { const e = d3.extent(vals), t = d3.scaleLinear().domain(lowerBetter ? [e[1], e[0]] : e); return (v) => d3.interpolateRgb('#3b1d3f', '#16a34a')(t(v)); })();
    const best = cells.reduce((a, b) => ((lowerBetter ? b[metric] < a[metric] : b[metric] > a[metric]) ? b : a));
    const fmt = metric === 'pf' || metric === 'sharpe' ? (v) => v.toFixed(2) : metric === 'win' ? (v) => v.toFixed(0) + '%' : (v) => fmtK(v);
    g.selectAll('rect').data(cells).join('rect').attr('x', (c) => c.xi * cw + 1).attr('y', (c) => (yl.length - 1 - c.yi) * ch + 1).attr('width', cw - 2).attr('height', ch - 2).attr('rx', 6)
      .attr('fill', (c) => scale(c[metric])).attr('stroke', (c) => (c === best ? '#fff' : 'transparent')).attr('stroke-width', 1.6).style('cursor', 'pointer')
      .on('click', (ev, c) => onPick && onPick(c))
      .on('mousemove', (ev, c) => showTip(ev, `<div class="h">${res.x.param.toUpperCase()} ${xl[c.xi]} · ${res.y.param.toUpperCase()} ${yl[c.yi]}</div>` + tipRow('Net P&L', inr(c.net, true)) + tipRow('Profit factor', isNum(c.pf) ? c.pf.toFixed(2) : '∞') + tipRow('Win rate', pct(c.win)) + tipRow('Max DD', inr(-c.dd)) + (isNum(c.ins) ? tipRow('In-sample', inr(c.ins, true)) + tipRow('Out-of-sample', inr(c.oos, true)) : '') + '<div class="h" style="margin-top:4px">click to load into the builder</div>'))
      .on('mouseleave', hideTip);
    g.selectAll('.v').data(cells).join('text').attr('x', (c) => c.xi * cw + cw / 2).attr('y', (c) => (yl.length - 1 - c.yi) * ch + ch / 2 + 4).attr('text-anchor', 'middle').attr('fill', '#fff').attr('font-size', 11).attr('font-weight', 600).attr('pointer-events', 'none').text((c) => fmt(c[metric]));
    g.selectAll('.xl').data(xl).join('text').attr('x', (_, i) => i * cw + cw / 2).attr('y', ih + 18).attr('text-anchor', 'middle').attr('fill', COLORS.muted).attr('font-size', 11).text((t) => t);
    g.selectAll('.yl').data(yl).join('text').attr('x', -8).attr('y', (_, i) => (yl.length - 1 - i) * ch + ch / 2 + 4).attr('text-anchor', 'end').attr('fill', COLORS.muted).attr('font-size', 11).text((t) => t);
  });
}

// ---------------------------------------------------------------- multi-run compare
export function compareChart(el, runs) {
  mount(el, (W) => {
    const series = runs.map((r, i) => ({ ...r, color: PALETTE[i % PALETTE.length], pts: r.curve.map((c) => ({ d: parseDate(c.date), v: c.cum })) })).filter((s) => s.pts.length);
    if (!series.length) return;
    const m = { l: 62, r: 14, t: 10, b: 26 }, H = 300, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = svgBase(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const all = series.flatMap((s) => s.pts);
    const x = d3.scaleTime().domain(d3.extent(all, (p) => p.d)).range([0, iw]);
    const ext = d3.extent(all, (p) => p.v);
    const y = d3.scaleLinear().domain([Math.min(ext[0], 0), Math.max(ext[1], 0)]).nice().range([ih, 0]);
    yAxis(g, y, iw, fmtK, 5);
    g.append('line').attr('x1', 0).attr('x2', iw).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', 'rgba(255,255,255,.25)').attr('stroke-dasharray', '4 4');
    series.forEach((s) => g.append('path').datum(s.pts).attr('fill', 'none').attr('stroke', s.color).attr('stroke-width', 2).attr('d', d3.line().x((p) => x(p.d)).y((p) => y(p.v)).curve(d3.curveMonotoneX)));
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(Math.max(3, iw / 110)).tickSize(0).tickPadding(8));
    const bis = d3.bisector((p) => p.d).center;
    svg.append('rect').attr('transform', `translate(${m.l},${m.t})`).attr('width', iw).attr('height', ih).attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const d0 = x.invert(d3.pointer(ev)[0]);
        showTip(ev, `<div class="h">${d3.timeFormat('%d %b %Y')(d0)}</div>` + series.map((s) => tipRow(s.name.slice(0, 26), inr(s.pts[bis(s.pts, d0)].v, true), s.color)).join(''));
      }).on('mouseleave', hideTip);
  });
  return PALETTE;
}

// ---------------------------------------------------------------- tiny sparkline (returns an <svg> element)
export function sparkline(values, { w = 220, h: hh = 46, positive } = {}) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${hh}`);
  svg.setAttribute('class', 'spark');
  svg.setAttribute('preserveAspectRatio', 'none');
  if (!values || values.length < 2) return svg;
  const x = d3.scaleLinear().domain([0, values.length - 1]).range([2, w - 2]);
  const e = d3.extent(values.concat([0]));
  const y = d3.scaleLinear().domain(e).range([hh - 3, 3]);
  const up = positive === undefined ? values[values.length - 1] >= 0 : positive;
  const col = up ? COLORS.green : COLORS.red;
  const line = d3.line().x((_, i) => x(i)).y((v) => y(v)).curve(d3.curveMonotoneX)(values);
  const area = d3.area().x((_, i) => x(i)).y0(y(0)).y1((v) => y(v)).curve(d3.curveMonotoneX)(values);
  svg.innerHTML = `<path d="${area}" fill="${col}" opacity=".16"/><path d="${line}" fill="none" stroke="${col}" stroke-width="1.8"/>`;
  return svg;
}
