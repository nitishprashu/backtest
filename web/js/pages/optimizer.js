import { h, clear, icons, inr, num, pct, cls, isNum, toast } from '../dom.js';
import { api, waitJob } from '../api.js';
import { sweepHeat } from '../charts.js';

const AXES = [['sl', 'Stop-loss % (all legs)'], ['tp', 'Target % (all legs)'], ['entry', 'Entry time']];
const DEF = { x: { param: 'sl', from: 15, to: 45, step: 10, tfrom: '09:20', tto: '10:30', tstep: 10 }, y: { param: 'tp', from: 0, to: 60, step: 20, tfrom: '09:20', tto: '10:30', tstep: 10 }, metric: 'net', split: '' };
const METRICS = [['net', 'Net P&L'], ['pf', 'Profit factor'], ['win', 'Win rate'], ['dd', 'Max drawdown'], ['sharpe', 'Sharpe'], ['oos', 'Out-of-sample P&L']];

function axisForm(a) {
  const F = (l, c) => h('label', { class: 'f' }, h('span', {}, l), c);
  const num_ = (k, step = 1) => h('input', { type: 'number', step, value: a[k], on: { change: (e) => { a[k] = parseFloat(e.target.value); } } });
  const tin = (k) => h('input', { type: 'time', value: a[k], on: { change: (e) => { a[k] = e.target.value; } } });
  const box = h('div');
  const paint = () => { clear(box).append(a.param === 'entry'
    ? h('div', { class: 'grid g3' }, F('From', tin('tfrom')), F('To', tin('tto')), F('Step (min)', num_('tstep')))
    : h('div', { class: 'grid g3' }, F('From %', num_('from', 5)), F('To %', num_('to', 5)), F('Step %', num_('step', 1)))); };
  const sel = h('select', { on: { change: (e) => { a.param = e.target.value; paint(); } } }, AXES.map(([v, l]) => h('option', { value: v, selected: a.param === v }, l)));
  paint();
  return h('div', {}, F('Parameter', sel), h('div', { style: { height: '10px' } }), box);
}

export function render(container, ctx) {
  const S = ctx.S;
  const inp = S.sweepInput || (S.sweepInput = JSON.parse(JSON.stringify(DEF)));
  const out = h('div');
  const metricSel = h('select', { style: { width: '190px' }, on: { change: (e) => { inp.metric = e.target.value; if (S.optimize) paintResult(); } } }, METRICS.map(([v, l]) => h('option', { value: v, selected: inp.metric === v }, l)));
  const run = h('button', { class: 'btn primary', on: { click: start } }, h('span', { html: icons.play }), 'Run sweep');
  container.append(h('div', { class: 'page-head' }, h('div', {}, h('h2', {}, 'Optimizer'), h('p', {}, 'Sweep two parameters over cached sessions. Look for broad plateaus, not a single lucky cell.'))),
    h('div', { class: 'grid g2' },
      h('div', { class: 'card' }, h('h3', {}, 'X axis'), axisForm(inp.x)), h('div', { class: 'card' }, h('h3', {}, 'Y axis'), axisForm(inp.y))),
    h('div', { class: 'card', style: { marginTop: '14px' } }, h('div', { class: 'row' },
      h('label', { class: 'f', style: { width: '210px' } }, h('span', {}, 'Out-of-sample starts on'), h('input', { type: 'date', value: inp.split, on: { change: (e) => { inp.split = e.target.value; } } })),
      h('div', { class: 'hint', style: { flex: 1, minWidth: '240px' } }, `Uses the strategy and settings from the builder (${S.settings.start} → ${S.settings.end}). Set a split date to see in-sample vs out-of-sample P&L in every cell.`), run)),
    h('div', { style: { height: '14px' } }), out);
  if (S.optimize) paintResult();

  async function start() {
    run.disabled = true;
    clear(out).append(h('div', { class: 'card' }, h('div', { class: 'skel', style: { height: '12px', width: '40%' } }), h('div', { class: 'hint', id: 'optmsg', style: { marginTop: '10px' } }, 'Starting…')));
    try {
      const body = { settings: S.settings, strategy: S.strategy, split: inp.split || undefined,
        x: axisBody(inp.x), y: axisBody(inp.y) };
      const job = await api.optimize(body);
      const done = await waitJob(job, (j) => { const m = document.getElementById('optmsg'); if (m) m.textContent = `${Math.round(j.progress * 100)}% · ${j.message}`; });
      if (done.status === 'error') throw new Error(done.error);
      if (done.status !== 'done') { clear(out); return; }
      S.optimize = done.result;
      paintResult();
    } catch (e) { clear(out); toast(e.message, 'err', 7000); }
    run.disabled = false;
  }
  const axisBody = (a) => (a.param === 'entry' ? { param: 'entry', from: a.tfrom, to: a.tto, step: a.tstep } : { param: a.param, from: a.from, to: a.to, step: a.step });

  function paintResult() {
    const r = S.optimize;
    let metric = inp.metric;
    if (metric === 'oos' && !r.cells.some((c) => isNum(c.oos))) metric = 'net';
    const host = h('div', { class: 'chart' });
    const ok = r.cells.filter((c) => !c.error);
    const errs = r.cells.filter((c) => c.error);
    const top = [...ok].sort((a, b) => b.net - a.net).slice(0, 10);
    const hasSplit = ok.some((c) => isNum(c.oos));
    clear(out).append(
      h('div', { class: 'card' }, h('h3', {}, 'Heat-map', h('span', { class: 'sub' }, `${ok.length} combinations in ${r.seconds.toFixed(1)}s · click a cell to load it into the builder`), metricSel), host,
        errs.length ? h('div', { class: 'hint neg' }, `${errs.length} combination(s) failed: ${errs[0].error}`) : null),
      h('div', { style: { height: '14px' } }),
      h('div', { class: 'card' }, h('h3', {}, 'Top 10 by net P&L'), h('div', { class: 'tbl-wrap' }, h('table', { class: 't' },
        h('thead', {}, h('tr', {}, [r.x.param.toUpperCase(), r.y.param.toUpperCase(), 'Net P&L', 'Profit factor', 'Win rate', 'Max DD', 'Sharpe', 'Days'].concat(hasSplit ? ['In-sample', 'Out-of-sample'] : []).map((t, i) => h('th', { class: i < 2 ? 'l' : '' }, t)))),
        h('tbody', {}, top.map((c) => h('tr', { style: { cursor: 'pointer' }, on: { click: () => pick(c) } }, h('td', { class: 'l' }, r.x.labels[c.xi]), h('td', { class: 'l' }, r.y.labels[c.yi]), h('td', { class: cls(c.net) }, h('b', {}, inr(c.net, true))),
          h('td', {}, isNum(c.pf) ? num(c.pf) : '∞'), h('td', {}, pct(c.win)), h('td', { class: 'neg' }, inr(-c.dd)), h('td', {}, num(c.sharpe)), h('td', {}, c.days),
          ...(hasSplit ? [h('td', { class: cls(c.ins) }, inr(c.ins, true)), h('td', { class: cls(c.oos) }, inr(c.oos, true))] : []))))))),
      h('div', { class: 'hint', style: { marginTop: '10px' } }, 'A cell that wins in-sample but loses out-of-sample is overfit. Prefer settings whose neighbours also look good.'));
    sweepHeat(host, r, metric, pick);
  }
  function pick(c) {
    const r = S.optimize, apply = (axis, label) => {
      const v = parseFloat(label);
      if (axis === 'entry') S.settings.entry_time = label;
      else for (const leg of S.strategy.legs) {
        if (axis === 'sl') { if (v > 0) { leg.sl_kind = '%'; leg.sl_val = v; } else { leg.sl_kind = 'Off'; leg.trail_on = false; leg.re_mode = 'Off'; } }
        else { if (v > 0) { leg.tp_kind = '%'; leg.tp_val = v; } else leg.tp_kind = 'Off'; }
      }
    };
    apply(r.x.param, r.x.labels[c.xi]); apply(r.y.param, r.y.labels[c.yi]);
    ctx.persist(); toast('Loaded into the builder', 'ok'); ctx.go('strategy');
  }
}
