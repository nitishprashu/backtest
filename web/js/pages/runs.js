import { h, clear, icons, inr, num, pct, cls, isNum, toast } from '../dom.js';
import { api } from '../api.js';
import { sparkline, compareChart } from '../charts.js';

const sel = new Set();

export function render(container, ctx) {
  const S = ctx.S;
  if (!S.runs.length) {
    container.append(h('div', { class: 'card empty' }, h('h3', {}, 'No saved runs yet'), h('div', { class: 'hint' }, 'Every backtest is saved here automatically so you can compare ideas later.'),
      h('div', { style: { marginTop: '14px' } }, h('button', { class: 'btn primary', on: { click: () => ctx.go('strategy') } }, 'Build a strategy'))));
    return;
  }
  const cmp = h('div'), grid = h('div', { class: 'grid g3' });
  container.append(h('div', { class: 'page-head' }, h('div', {}, h('h2', {}, 'Run history'), h('p', {}, `${S.runs.length} saved · select two or more to overlay their equity curves`))), cmp, h('div', { style: { height: '14px' } }), grid);

  const paintCompare = async () => {
    clear(cmp);
    if (sel.size < 2) return;
    try {
      const r = await api.compare([...sel]);
      const host = h('div', { class: 'chart' });
      cmp.append(h('div', { class: 'card' }, h('h3', {}, 'Cumulative P&L comparison'), host,
        h('div', { class: 'tbl-wrap', style: { marginTop: '12px', maxHeight: '260px' } }, h('table', { class: 't' },
          h('thead', {}, h('tr', {}, ['Run', 'Net P&L', 'Win rate', 'Profit factor', 'Max DD', 'Sharpe', 'Days'].map((t, i) => h('th', { class: i === 0 ? 'l' : '' }, t)))),
          h('tbody', {}, r.runs.map((x) => h('tr', {}, h('td', { class: 'l' }, x.name), h('td', { class: cls(x.metrics.total_net_pnl) }, inr(x.metrics.total_net_pnl, true)), h('td', {}, pct(x.metrics.win_rate_pct)), h('td', {}, isNum(x.metrics.profit_factor) ? num(x.metrics.profit_factor) : '∞'), h('td', { class: 'neg' }, inr(-(x.metrics.max_drawdown || 0))), h('td', {}, num(x.metrics.sharpe)), h('td', {}, x.metrics.total_trades))))))));
      compareChart(host, r.runs);
    } catch (e) { toast(e.message, 'err'); }
  };

  const paintGrid = () => {
    clear(grid);
    S.runs.forEach((r) => {
      const m = r.metrics || {}, sm = r.summary || {};
      const checked = sel.has(r.id);
      grid.append(h('div', { class: 'card runcard' + (S.run && S.run.id === r.id ? ' sel' : ''), on: { click: async (e) => { if (e.target.closest('button,input')) return; await ctx.loadRun(r.id); ctx.go('results'); } } },
        h('div', { class: 'row', style: { justifyContent: 'space-between', alignItems: 'flex-start' } },
          h('div', { style: { minWidth: 0 } }, h('b', { style: { display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '260px' } }, r.name), h('div', { class: 'hint' }, r.created.replace('T', ' '))),
          h('label', { class: 'toggle', title: 'Compare' }, h('input', { type: 'checkbox', checked, on: { change: (e) => { e.target.checked ? sel.add(r.id) : sel.delete(r.id); paintCompare(); } } }), h('span', { class: 'sw' }))),
        sparkline(sm.curve || [], { positive: (m.total_net_pnl || 0) >= 0 }),
        h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: '8px' } },
          h('div', {}, h('div', { class: 'hint' }, 'Net P&L'), h('b', { class: 'num ' + cls(m.total_net_pnl) }, inr(m.total_net_pnl, true))),
          h('div', {}, h('div', { class: 'hint' }, 'Win rate'), h('b', { class: 'num' }, pct(m.win_rate_pct))),
          h('div', {}, h('div', { class: 'hint' }, 'Profit factor'), h('b', { class: 'num' }, isNum(m.profit_factor) ? num(m.profit_factor) : '∞')),
          h('div', {}, h('div', { class: 'hint' }, 'Max DD'), h('b', { class: 'num neg' }, inr(-(m.max_drawdown || 0))))),
        h('div', { class: 'row', style: { marginTop: '10px' } },
          h('button', { class: 'btn small', on: { click: async () => { const n = prompt('Rename run', r.name); if (n) { await api.rename(r.id, n); await ctx.refreshRuns(); S.runs.find((x) => x.id === r.id).name = n; paintGrid(); } } } }, 'Rename'),
          h('button', { class: 'btn small danger', on: { click: async () => { if (!confirm('Delete this run?')) return; await api.remove(r.id); sel.delete(r.id); if (S.run && S.run.id === r.id) S.run = null; await ctx.refreshRuns(); ctx.rerender(); } }, html: icons.trash }))));
    });
  };
  paintGrid(); paintCompare();
}
