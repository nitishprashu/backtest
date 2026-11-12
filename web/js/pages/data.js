import { h, clear, inr, num, toast, dShort, icons } from '../dom.js';
import { api, waitJob } from '../api.js';

export function render(container, ctx) {
  const S = ctx.S;
  const body = h('div', {});
  container.append(h('div', { class: 'page-head' }, h('div', {}, h('h2', {}, 'Data health'), h('p', {}, `Window ${S.settings.start} → ${S.settings.end}. Check this before trusting any result.`))), body);
  body.append(h('div', { class: 'skel', style: { height: '120px' } }));
  api.data(S.settings.start, S.settings.end, S.settings.strike_interval).then((d) => paint(d)).catch((e) => { clear(body).append(h('div', { class: 'card' }, h('h3', {}, 'Could not read the data'), h('div', { class: 'neg' }, e.message))); });

  function paint(d) {
    const c = d.coverage, s = d.session || {};
    const kpi = (l, v, sub, tone) => h('div', { class: 'kpi ' + (tone === 'bad' ? 'n' : tone === 'good' ? 'p' : '') }, h('div', { class: 'l' }, l), h('div', { class: 'v' }, v), h('div', { class: 's' }, sub || ''));
    const okOpen = s.first_bar === '09:15', okClose = s.last_bar === '15:29';
    const flagged = (d.expiries || []).filter((e) => e.flag);
    clear(body).append(
      h('div', { class: 'grid g4' }, kpi('Spot candles', (c.spot_rows || 0).toLocaleString('en-IN'), `${c.first} → ${c.last}`), kpi('Option years', (c.option_years || []).join(', ') || 'none', c.source || ''),
        kpi('Contracts in window', (d.contracts || 0).toLocaleString('en-IN'), `${(d.expiries || []).length} expiries`), kpi('Sessions in window', String(d.sessions || 0), '')),
      h('div', { style: { height: '14px' } }),
      h('div', { class: 'card' }, h('h3', {}, 'Session timing check', h('span', { class: 'sub' }, 'bars should run 09:15 to 15:29')),
        h('div', { class: 'grid g4' }, kpi('First bar', s.first_bar || '—', 'expected 09:15', okOpen ? 'good' : 'bad'), kpi('Last bar', s.last_bar || '—', 'expected 15:29', okClose ? 'good' : 'bad'),
          kpi('Bars per day (median)', String(s.median_bars_per_day ?? '—'), 'full session = 375'), kpi('Complete days', `${s.days_with_375_bars ?? 0} / ${s.days ?? 0}`, '')),
        okOpen && okClose ? h('div', { class: 'hint pos', style: { marginTop: '10px' } }, 'Bar times match the 09:15–15:29 convention.') :
          h('div', { class: 'hint neg', style: { marginTop: '10px' } }, 'Bars do not span 09:15–15:29. If your timestamps mark the END of each minute, every entry is one candle late. Check before trusting results.')),
      h('div', { style: { height: '14px' } }),
      h('div', { class: 'card' }, h('h3', {}, 'Expiry completeness', h('span', { class: 'sub' }, 'gaps over 9 days or fewer than 10 strikes are flagged')),
        flagged.length ? h('div', { class: 'tbl-wrap', style: { maxHeight: '240px' } }, h('table', { class: 't' }, h('thead', {}, h('tr', {}, ['Expiry', 'Contracts', 'Strikes', 'Lowest', 'Highest', 'Gap (days)'].map((t, i) => h('th', { class: i === 0 ? 'l' : '' }, t)))),
          h('tbody', {}, flagged.map((e) => h('tr', {}, h('td', { class: 'l neg' }, e.expiry), h('td', {}, e.contracts), h('td', {}, e.strikes), h('td', {}, e.lo), h('td', {}, e.hi), h('td', {}, e.gap_days ?? '—')))))) : h('div', { class: 'hint pos' }, 'No suspicious expiry gaps in this window.')),
      h('div', { class: 'hint', style: { margin: '10px 4px' } }, `Cache: ${d.stats.cache_hits} hits / ${d.stats.cache_misses} misses · contracts loaded ${d.stats.contracts_loaded.toLocaleString('en-IN')} · candles outside 09:15–15:29 ignored: ${d.stats.option_rows_outside_session.toLocaleString('en-IN')}`),
      repair());
  }

  function repair() {
    const v = { expiry: S.settings.end, type: 'CE', strike: 24000, from: S.settings.start, to: S.settings.end };
    const F = (l, c) => h('label', { class: 'f' }, h('span', {}, l), c);
    const msg = h('div', { class: 'hint', style: { marginTop: '10px' } });
    const go = h('button', { class: 'btn', on: { click: async () => {
      go.disabled = true; msg.textContent = 'Downloading from Breeze…';
      try { const job = await api.repair(v); const done = await waitJob(job); if (done.status === 'error') throw new Error(done.error); const r = done.result; msg.textContent = `${r.table}: ${r.message} (${r.rows} rows)`; msg.className = 'hint pos'; }
      catch (e) { msg.textContent = e.message; msg.className = 'hint neg'; }
      go.disabled = false; } } }, h('span', { html: icons.download }), 'Download & upsert');
    return h('details', { class: 'sec', style: { marginTop: '14px' } }, h('summary', {}, 'Repair one contract from Breeze'), h('div', { class: 'body' },
      h('div', { class: 'hint' }, 'Backtests never write to your database. Repairs happen only here, on request, and need your Breeze credential file.'),
      h('div', { class: 'grid g4' }, F('Expiry', h('input', { type: 'date', value: v.expiry, on: { change: (e) => { v.expiry = e.target.value; } } })),
        F('Type', h('select', { on: { change: (e) => { v.type = e.target.value; } } }, h('option', {}, 'CE'), h('option', {}, 'PE'))), F('Strike', h('input', { type: 'number', step: 50, value: v.strike, on: { change: (e) => { v.strike = parseFloat(e.target.value); } } })),
        h('div', { class: 'grid g2' }, F('From', h('input', { type: 'date', value: v.from, on: { change: (e) => { v.from = e.target.value; } } })), F('To', h('input', { type: 'date', value: v.to, on: { change: (e) => { v.to = e.target.value; } } })))),
      h('div', {}, go), msg));
  }
}
