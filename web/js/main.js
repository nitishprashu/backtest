import { h, $, clear, icons, toast, inr } from './dom.js';
import { api, waitJob } from './api.js';
import * as strategyPage from './pages/strategy.js';
import * as resultsPage from './pages/results.js';
import * as explorerPage from './pages/explorer.js';
import * as optimizerPage from './pages/optimizer.js';
import * as runsPage from './pages/runs.js';
import * as dataPage from './pages/data.js';
import * as chartingPage from './pages/charting.js';

const KEY = 'nlab.v5';
const PAGES = [
  ['strategy', 'Strategy', icons.strategy, strategyPage],
  ['results', 'Results', icons.results, resultsPage],
  ['charts', 'Charts', icons.charts, chartingPage],
  ['explorer', 'Explorer', icons.explorer, explorerPage],
  ['optimizer', 'Optimizer', icons.optimizer, optimizerPage],
  ['runs', 'Runs', icons.runs, runsPage],
  ['data', 'Data', icons.data, dataPage],
];

export const S = {
  cfg: null, settings: null, strategy: null, run: null, runs: [], page: 'strategy',
  resultTab: 'overview', running: null, optimize: null, sweepInput: null,
};

const clone = (o) => JSON.parse(JSON.stringify(o));
export function persist() {
  try {
    const strategy = S.strategy && S.strategy.kind === 'indicator'
      ? { ...S.strategy, indicator: { ...S.strategy.indicator } }
      : S.strategy;
    if (strategy && strategy.kind === 'indicator') delete strategy.indicator.expression;
    localStorage.setItem(KEY, JSON.stringify({ settings: S.settings, strategy }));
  } catch { /* private mode */ }
}

export const ctx = {
  S, persist, toast,
  go(page) { if (page !== S.page) { S.page = page; history.replaceState(null, '', '#' + page); } render(); },
  rerender: () => render(),
  startRun, loadRun, refreshRuns,
  resetStrategy() { S.strategy = clone(S.cfg.strategy); persist(); },
  clone,
};

async function refreshRuns() {
  try { S.runs = (await api.runs()).runs; } catch { /* ignore */ }
  paintRail();
}

async function loadRun(id) {
  S.run = await api.run(id);
  S.resultTab = S.resultTab || 'overview';
  return S.run;
}

async function startRun(extra = {}) {
  if (S.running) { toast('A run is already in progress', ''); return; }
  const payload = { settings: S.settings, strategy: S.strategy, ...extra };
  const banner = h('div', { class: 'runbanner' });
  document.body.append(banner);
  const bar = h('div', { class: 'progress' }, h('div'));
  document.body.append(bar);
  const paint = (j) => {
    const p = Math.round((j.progress || 0) * 100);
    bar.firstChild.style.width = p + '%';
    clear(banner).append(
      h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('b', {}, 'Running backtest'), h('span', { class: 'muted num' }, p + '%')),
      h('div', { class: 'bar' }, h('div', { style: { width: p + '%' } })),
      h('div', { class: 'hint' }, j.message || 'Starting…'),
      h('div', { class: 'row', style: { marginTop: '10px' } }, h('button', { class: 'btn small danger', on: { click: () => api.cancel(S.running.id) } }, h('span', { html: icons.stop }), 'Stop')));
  };
  try {
    const job = await api.startRun(payload);
    S.running = job;
    paint(job);
    const done = await waitJob(job, (j) => { S.running = j; paint(j); });
    if (done.status === 'error') throw new Error(done.error);
    if (done.status === 'cancelled') { toast('Run cancelled'); return; }
    await loadRun(done.result.run_id);
    await refreshRuns();
    S.page = 'results';
    history.replaceState(null, '', '#results');
    const m = S.run.metrics;
    toast(m.total_trades ? `Done in ${S.run.elapsed.toFixed(1)}s · net ${inr(m.total_net_pnl, true)} over ${m.total_trades} days` : 'Finished with no trades; see Diagnostics', m.total_trades ? 'ok' : 'err', 6000);
  } catch (e) {
    toast(e.message, 'err', 8000);
  } finally {
    S.running = null;
    banner.remove();
    bar.remove();
    render();
  }
}

function paintRail() {
  const rail = $('.rail');
  if (!rail) return;
  clear(rail);
  rail.append(h('div', { class: 'logo', html: '<svg width="24" height="24" viewBox="0 0 32 32"><path d="M6 22l6-8 5 4 9-11" stroke="white" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>' }));
  for (const [id, label, icon] of PAGES) {
    const badge = id === 'runs' && S.runs.length ? h('span', { class: 'badge' }, S.runs.length) : null;
    rail.append(h('button', { class: 'nav' + (S.page === id ? ' active' : ''), title: label, on: { click: () => ctx.go(id) } }, h('span', { html: icon }), label, badge));
  }
}

function paintTop() {
  const top = $('.topbar');
  clear(top);
  const cov = (S.cfg && S.cfg.coverage) || {};
  const chips = [];
  if (S.cfg.coverage_error) chips.push(h('span', { class: 'chip bad' }, h('i'), 'Data: ' + S.cfg.coverage_error.slice(0, 90)));
  else {
    chips.push(h('span', { class: 'chip' }, h('i'), `Spot ${cov.first || '?'} → ${cov.last || '?'}`));
    if (cov.option_years) chips.push(h('span', { class: 'chip' }, 'Options ' + (cov.option_years.join(', ') || 'none')));
  }
  top.append(
    h('div', { class: 'brand' }, h('h1', {}, 'NIFTY Options Lab'), h('small', {}, 'v' + S.cfg.version + ' · local · nothing leaves this machine')),
    h('div', { class: 'chips' }, chips), h('div', { class: 'spacer' }),
    h('button', { class: 'btn primary', disabled: !!S.running, on: { click: () => startRun() } }, h('span', { html: icons.play }), 'Run backtest'));
}

function render() {
  paintRail();
  paintTop();
  const content = $('.content');
  clear(content);
  const page = PAGES.find((p) => p[0] === S.page) || PAGES[0];
  try { page[3].render(content, ctx); } catch (e) {
    console.error(e);
    content.append(h('div', { class: 'card empty' }, h('h3', {}, 'Something went wrong drawing this page'), h('div', { class: 'hint' }, String(e && e.message || e))));
  }
}

async function boot() {
  const app = $('#app');
  app.append(h('div', { class: 'shell' }, h('nav', { class: 'rail' }), h('div', { class: 'main' }, h('header', { class: 'topbar' }), h('main', { class: 'content' }))));
  try { S.cfg = await api.config(); } catch (e) {
    app.innerHTML = '';
    app.append(h('div', { class: 'card empty', style: { margin: '80px auto', maxWidth: '520px' } }, h('h3', {}, 'Cannot reach the app server'), h('div', { class: 'hint' }, e.message)));
    return;
  }
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { /* ignore */ }
  S.settings = Object.assign(clone(S.cfg.settings), (saved && saved.settings) || {});
  S.strategy = (saved && saved.strategy && saved.strategy.legs) ? saved.strategy : clone(S.cfg.strategy);
  const hash = location.hash.replace('#', '');
  if (PAGES.some((p) => p[0] === hash)) S.page = hash;
  await refreshRuns();
  if (S.runs.length && (S.page === 'results' || S.page === 'explorer' || S.page === 'charts')) {
    try { await loadRun(S.runs[0].id); } catch { /* ignore */ }
  }
  render();
  window.addEventListener('hashchange', () => { const p = location.hash.replace('#', ''); if (PAGES.some((x) => x[0] === p) && p !== S.page) { S.page = p; render(); } });
}
boot();
