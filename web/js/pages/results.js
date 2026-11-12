import { h, clear, icons, inr, num, pct, cls, isNum, dShort, dLong, toast } from '../dom.js';
import { api } from '../api.js';
import { equityChart, dailyBars, calendarHeat, monthlyTable, barChart, histChart, scatterChart, daySeries, niftyTradeChart, niftyBacktestChart, COLORS } from '../charts.js';

const kpi = (label, value, sub = '', tone = '') => h('div', { class: 'kpi ' + (tone === 'pos' ? 'p' : tone === 'neg' ? 'n' : '') },
  h('div', { class: 'l' }, label), h('div', { class: 'v ' + tone + (String(value).length > 13 ? ' sm' : '') }, value), h('div', { class: 's' }, sub));
const toneOf = (v) => (isNum(v) ? (v > 0 ? 'pos' : v < 0 ? 'neg' : '') : '');
const card = (title, body, sub) => h('div', { class: 'card' }, h('h3', {}, title, sub ? h('span', { class: 'sub' }, sub) : null), body);
const table = (head, rows, opts = {}) => h('div', { class: 'tbl-wrap', style: { maxHeight: opts.max || '420px' } }, h('table', { class: 't' },
  h('thead', {}, h('tr', {}, head.map((t, i) => h('th', { class: i === 0 ? 'l' : '' }, t)))), h('tbody', {}, rows)));

function strategyDescription(run) {
  if (run.kind === 'multi') return `Multi-leg · ${run.strategy.legs.length} legs`;
  const ind = run.strategy.indicator;
  const timeframe = (value) => ({ '1m': '1-minute', '5m': '5-minute', '15m': '15-minute',
    '30m': '30-minute', '60m': '60-minute', '1d': 'daily', '1w': 'weekly', '1mo': 'monthly' })[value] || value;
  if (ind.type === 'EMA') {
    const tf = timeframe(ind.ema_timeframe || '1m');
    const source = ind.ema_source === 'Option premium' ? 'option premium' : 'NIFTY spot';
    return ind.ema_mode === 'EMA crossover'
      ? `EMA crossover · ${source} · fast ${ind.ema_fast}, slow ${ind.ema_slow} ${tf} bars`
      : `Price vs EMA · ${source} · ${ind.ema_period} ${tf} bars${ind.ema_slope_filter ? ` · ${ind.ema_slope_type} slope ${ind.ema_slope_period} ${tf} bars` : ''}`;
  }
  if (ind.type === 'Supertrend') return `Supertrend · ${timeframe(ind.st_timeframe || `${ind.st_tf || 5}m`)} bars · ATR ${ind.st_period} × ${ind.st_mult}`;
  if (ind.type === 'MACD') return `MACD crossover · fast ${ind.macd_fast}, slow ${ind.macd_slow}, signal ${ind.macd_signal} · ${timeframe(ind.macd_timeframe || '1m')} NIFTY spot bars`;
  return `VWAP · ${ind.vwap_source} · ${timeframe(ind.vwap_timeframe || '1m')} bars`;
}

export function render(container, ctx) {
  const S = ctx.S, run = S.run;
  const page = h('section', { class: 'results-page' });
  container.append(page);
  if (!run) {
    page.append(h('div', { class: 'card empty' }, h('h3', {}, 'No results yet'), h('div', { class: 'hint' }, 'Build a strategy and press Run backtest, or open an earlier run.'),
      h('div', { class: 'row', style: { justifyContent: 'center', marginTop: '14px' } }, h('button', { class: 'btn primary', on: { click: () => ctx.go('strategy') } }, 'Open the builder'), S.runs.length ? h('button', { class: 'btn', on: { click: async () => { await ctx.loadRun(S.runs[0].id); ctx.rerender(); } } }, 'Open latest run') : null)));
    return;
  }
  const m = run.metrics, set = run.settings, cap = set.capital || 0;
  const strategy = run.kind === 'indicator' ? run.strategy.indicator : null;
  const schedule = strategy
    ? `signals ${strategy.start_time}–${strategy.last_entry_time}`
    : `entry ${set.entry_time} · square-off ${set.exit_time}`;
  const holding = ({ INTRADAY: 'intraday', CARRY_SINGLE: 'carry one trade',
    CARRY_FRESH_ENTRIES: 'carry with fresh entries' })[set.holding_mode] || 'intraday';
  const nameIn = h('input', { type: 'text', value: run.name, style: { fontSize: '20px', fontWeight: 800, background: 'transparent', border: '1px solid transparent', padding: '4px 8px', width: 'min(640px,60vw)' },
    on: { change: async (e) => { try { await api.rename(run.id, e.target.value); run.name = e.target.value; await ctx.refreshRuns(); toast('Renamed', 'ok', 1500); } catch (er) { toast(er.message, 'err'); } }, focus: (e) => { e.target.style.borderColor = 'var(--line2)'; }, blur: (e) => { e.target.style.borderColor = 'transparent'; } } });
  page.append(h('div', { class: 'page-head' },
    h('div', {}, nameIn,
      h('p', { style: { marginLeft: '8px' } }, `${set.start} → ${set.end} · ${schedule} · ${set.expiry} · ${holding} · ${strategyDescription(run)} · ran in ${run.elapsed.toFixed(1)}s`)),
    h('div', { class: 'row' },
      h('a', { class: 'btn small primary', href: `/api/runs/${run.id}/report.pdf`, download: '' }, h('span', { html: icons.download }), 'Full report PDF'),
      h('a', { class: 'btn small', href: `/api/runs/${run.id}/trades.csv`, download: '' }, h('span', { html: icons.download }), 'Trades CSV'),
      h('button', { class: 'btn small', on: { click: () => ctx.go('charts') } }, 'Open charts'),
      h('button', { class: 'btn small', on: { click: () => { S.strategy = ctx.clone(run.strategy); Object.assign(S.settings, run.settings); ctx.persist(); ctx.go('strategy'); toast('Loaded this run into the builder', 'ok'); } } }, 'Edit strategy'),
      h('button', { class: 'btn small', on: { click: () => ctx.go('explorer') } }, 'Replay a day'))));

  if (!m.total_trades) {
    page.append(card('No trades were generated', h('div', {}, h('div', { class: 'hint' }, 'See why sessions were skipped below.'), diagnostics(run))));
    return;
  }
  const ex = run.dashboard.extra || {};
  page.append(h('div', { class: 'grid g6' },
    kpi('Net P&L', inr(m.total_net_pnl, true), `gross ${inr(m.total_gross_pnl)}`, toneOf(m.total_net_pnl)),
    kpi('Return on capital', cap ? pct(m.total_net_pnl / cap * 100) : '—', cap ? `on ${inr(cap)}` : 'set a capital for % metrics', toneOf(m.total_net_pnl)),
    kpi('Win rate', pct(m.win_rate_pct), `${ex.win_days ?? '–'} wins · ${ex.loss_days ?? '–'} losses`),
    kpi('Profit factor', isNum(m.profit_factor) ? num(m.profit_factor) : '∞', `payoff ${isNum(m.win_loss_ratio) ? num(m.win_loss_ratio) : '∞'}`),
    kpi('Max drawdown', inr(-m.max_drawdown), isNum(m.max_drawdown_pct) ? pct(m.max_drawdown_pct) + ' of peak' : '', 'neg'),
    kpi('Sharpe', num(m.sharpe), `Sortino ${num(m.sortino)} · Calmar ${num(m.calmar)}`)));
  page.append(h('div', { class: 'grid g6', style: { marginTop: '14px' } },
    kpi('Expectancy / day', inr(m.expectancy, true), `median ${inr(ex.median_day, true)}`, toneOf(m.expectancy)),
    kpi('Avg win', inr(m.avg_win, true), '', 'pos'), kpi('Avg loss', inr(m.avg_loss, true), '', 'neg'),
    kpi('Best / worst day', `${inr(m.best_day)} / ${inr(m.worst_day)}`, `streaks ${m.max_consecutive_wins}W · ${m.max_consecutive_losses}L`),
    kpi('Charges + slippage', inr(m.total_costs + m.total_slippage_cost), `charges ${inr(m.total_costs)} · slippage ${inr(m.total_slippage_cost)}`),
    kpi('Sessions traded', `${m.total_trades} / ${m.trading_days_in_scope}`, `${m.skipped_days || 0} skipped · ${m.filtered_days || 0} filtered`)));

  const tabs = [['overview', 'Overview'], ['calendar', 'Calendar'], ['breakdown', 'Breakdown'],
    ['greeks', 'Greeks'], ['trades', 'Trades'], ['risk', 'Risk'], ['diag', 'Diagnostics']];
  const body = h('div');
  const bar = h('div', { class: 'tabs', style: { marginTop: '18px' } }, tabs.map(([id, l]) => h('button', { class: S.resultTab === id ? 'on' : '', on: { click: () => { S.resultTab = id; paint(); } } }, l)));
  const paint = () => { [...bar.children].forEach((b, i) => b.classList.toggle('on', tabs[i][0] === S.resultTab)); clear(body); ({ overview, calendar, breakdown, greeks, trades, risk, diag: (b, r) => b.append(diagnostics(r)) }[S.resultTab] || overview)(body, run, ctx); };
  page.append(bar, body);
  paint();
}

// ------------------------------------------------------------------ tabs
function overview(body, run) {
  const rows = run.dashboard.equity, cap = run.settings.capital;
  let range = 'all';
  const host = h('div', { class: 'chart' });
  const eventDates = run.trade_event_dates || run.trade_dates || [];
  if (run.kind === 'indicator') {
    const chartHost = h('div', { class: 'chart' });
    const summary = h('div', { class: 'hint' }, 'Loading the full NIFTY chart for this backtest…');
    body.append(card('Full-duration NIFTY trade verification',
      h('div', {},
        h('div', { class: 'hint', style: { marginBottom: '8px' } },
          'Scrollable 1-minute candles for the backtest period · green ▲ entries · red ▼ exits · hover for candle and execution details.'),
        summary, chartHost)));
    api.niftyBacktestChart(run.id).then((data) => {
      const entries = data.events.filter((event) => event.kind === 'ENTRY').length;
      const exits = data.events.filter((event) => event.kind === 'EXIT').length;
      const overlay = data.source === 'option'
        ? `${data.label} is option-sourced and is not plotted on the NIFTY spot price axis`
        : `overlay: ${data.lines.map((line) => line.label).join(', ') || data.label}`;
      summary.textContent = `${data.bars.length} ${data.timeframe} candles · ${entries} entries · ${exits} exits · ${overlay} · scroll horizontally to review the full period`;
      niftyBacktestChart(chartHost, data);
    }).catch((error) => {
      summary.textContent = 'Unable to load full-duration NIFTY verification chart.';
      clear(chartHost).append(h('div', { class: 'hint neg' }, error.message));
    });
  } else if (eventDates.length) {
    const dateSelect = h('select', { style: { width: '190px' } },
      eventDates.map((date) => h('option', { value: date }, dLong(date))));
    const chartHost = h('div', { class: 'chart' });
    const summary = h('div', { class: 'hint' }, 'Loading NIFTY candles and executions…');
    let chartRequest = 0;
    const loadTradeChart = async (date) => {
      const request = ++chartRequest;
      clear(chartHost).append(h('div', { class: 'hint' }, `Loading NIFTY candles for ${dLong(date)}…`));
      try {
        const data = await api.niftyTrades(run.id, date);
        if (request !== chartRequest) return;
        const entries = data.events.filter((event) => event.kind === 'ENTRY').length;
        const exits = data.events.filter((event) => event.kind === 'EXIT').length;
        summary.textContent = `${entries} entries · ${exits} exits · hover a candle for NIFTY OHLC and option execution details`;
        clear(chartHost);
        niftyTradeChart(chartHost, data);
      } catch (error) {
        if (request !== chartRequest) return;
        summary.textContent = 'Unable to load trade verification candles.';
        clear(chartHost).append(h('div', { class: 'hint neg' }, error.message));
      }
    };
    dateSelect.value = eventDates[eventDates.length - 1];
    dateSelect.addEventListener('change', () => loadTradeChart(dateSelect.value));
    body.append(card('NIFTY trade verification',
      h('div', {},
        h('div', { class: 'row', style: { justifyContent: 'space-between', marginBottom: '6px' } },
          h('div', { class: 'hint' }, 'Review all executions on a session against the underlying NIFTY price action.'),
          dateSelect),
        summary, chartHost)));
    loadTradeChart(dateSelect.value);
  }
  const draw = () => {
    let r = rows;
    if (range !== 'all' && rows.length) { const last = new Date(rows[rows.length - 1].date + 'T00:00:00'); last.setDate(last.getDate() - range); const cut = last.toISOString().slice(0, 10); r = rows.filter((x) => x.date >= cut); }
    equityChart(host, r, { capital: cap });
  };
  const btns = [['1M', 30], ['3M', 91], ['6M', 182], ['1Y', 365], ['All', 'all']].map(([l, v]) => h('button', { class: v === range ? 'on' : '', on: { click: (e) => { range = v; [...e.target.parentNode.children].forEach((b) => b.classList.remove('on')); e.target.classList.add('on'); draw(); } } }, l));
  body.append(card(cap ? 'Equity curve' : 'Cumulative P&L', h('div', {}, h('div', { class: 'row', style: { justifyContent: 'flex-end', marginBottom: '6px' } }, h('div', { class: 'seg' }, btns)), host)));
  const bars = h('div', { class: 'chart' });
  body.append(h('div', { style: { height: '14px' } }), card('Daily P&L', bars, 'traded days'));
  draw(); dailyBars(bars, rows);
}

function calendar(body, run) {
  const rows = run.dashboard.equity;
  const cal = h('div', { class: 'chart' });
  const details = h('div', { class: 'card', style: { marginTop: '14px' } },
    h('h3', {}, 'Day trade details'),
    h('div', { class: 'hint' }, 'Click a traded day tile to view its executions.'));
  let request = 0;
  const loadDay = async (date) => {
    const current = ++request;
    clear(details).append(h('h3', {}, `Trades on ${dLong(date)}`),
      h('div', { class: 'hint' }, 'Loading executions…'));
    try {
      const result = await api.trades(run.id, { date, limit: 500, sort: 'entry_time', order: 'asc' });
      if (current !== request) return;
      const executions = result.rows || [];
      const tableRows = executions.map((trade) => h('tr', {},
        h('td', { class: 'l' }, `${trade.leg_id}#${trade.segment}`),
        h('td', {}, h('span', { class: 'pill ' + trade.side.toLowerCase() }, trade.side)),
        h('td', {}, trade.option_type), h('td', {}, trade.strike), h('td', {}, dShort(trade.expiry)),
        h('td', {}, trade.entry_time), h('td', {}, num(trade.entry_price)),
        h('td', {}, trade.exit_time), h('td', {}, num(trade.exit_price)),
        h('td', {}, trade.qty), h('td', {}, trade.exit_reason),
        h('td', {}, inr(trade.gross_pnl)), h('td', {}, inr(trade.costs)),
        h('td', { class: cls(trade.net_pnl) }, inr(trade.net_pnl, true))));
      details.replaceChildren(
        h('h3', {}, `Trades on ${dLong(date)}`),
        h('div', { class: 'hint', style: { marginBottom: '10px' } },
          `${result.total} executions · net P&L ${inr(result.sum_net, true)}`),
        result.total ? table(['Leg', 'Side', 'Type', 'Strike', 'Expiry', 'Entry', 'Entry ₹', 'Exit', 'Exit ₹',
          'Qty', 'Exit reason', 'Gross', 'Charges', 'Net P&L'], tableRows)
          : h('div', { class: 'hint' }, 'No trades found for this day.'));
    } catch (error) {
      if (current !== request) return;
      details.replaceChildren(h('h3', {}, `Trades on ${dLong(date)}`),
        h('div', { class: 'hint neg' }, `Unable to load day trades: ${error.message}`));
    }
  };
  body.append(card('Daily calendar', cal, 'green = profit, red = loss, darker = smaller · click a traded day for executions'),
    details, h('div', { style: { height: '14px' } }), card('Monthly P&L', monthlyTable(run.dashboard.monthly)));
  calendarHeat(cal, rows, loadDay);
}

function breakdown(body, run) {
  const d = run.dashboard;
  const wd = h('div', { class: 'chart' }), dte = h('div', { class: 'chart' }), ex = h('div', { class: 'chart' }), hist = h('div', { class: 'chart' });
  const ivRanges = h('div', { class: 'chart' }), premiumRanges = h('div', { class: 'chart' });
  body.append(h('div', { class: 'grid g2' },
    card('By weekday', wd, 'net P&L'), card('By days to expiry', dte, 'net P&L'),
    card('Day P&L distribution', hist), card('Exit reasons', ex, 'leg executions'),
    card('Results by entry IV range', ivRanges, 'daily net P&L · average of initial-leg IVs'),
    card('Results by premium collected', premiumRanges, 'daily net P&L · short entry premium × quantity')));
  barChart(wd, d.weekday.map((r) => ({ k: r.name, v: r.net, days: r.days, win: r.win, avg: r.avg })), { label: 'k', value: 'v', tipFn: (x) => `<div class="h">${x.k}</div><div class="r"><span>Net</span><b>${inr(x.v, true)}</b></div><div class="r"><span>Days</span><b>${x.days}</b></div><div class="r"><span>Win rate</span><b>${pct(x.win)}</b></div>` });
  barChart(dte, d.dte.map((r) => ({ k: r.dte + 'd', v: r.net, days: r.days, win: r.win })), { label: 'k', value: 'v', tipFn: (x) => `<div class="h">${x.k} to expiry</div><div class="r"><span>Net</span><b>${inr(x.v, true)}</b></div><div class="r"><span>Days</span><b>${x.days}</b></div><div class="r"><span>Win rate</span><b>${pct(x.win)}</b></div>` });
  histChart(hist, d.hist);
  barChart(ex, d.exits.map((r) => ({ k: r.reason, v: r.n, net: r.net })), { label: 'k', value: 'v', horizontal: true, fmt: (v) => v, colorFn: () => COLORS.violet, tipFn: (x) => `<div class="h">${x.k}</div><div class="r"><span>Executions</span><b>${x.v}</b></div><div class="r"><span>Net P&L</span><b>${inr(x.net, true)}</b></div>` });
  const rangeTip = (x) => `<div class="h">${x.k}</div><div class="r"><span>Trade days</span><b>${x.days}</b></div><div class="r"><span>Net P&L</span><b>${inr(x.v, true)}</b></div><div class="r"><span>Avg/day</span><b>${inr(x.avg, true)}</b></div><div class="r"><span>Win rate</span><b>${pct(x.win)}</b></div>`;
  const ranges = (items) => (items || []).map((r) => ({ k: r.range, v: r.net, days: r.days, avg: r.avg, win: r.win }));
  const ivData = ranges(d.iv_ranges), premiumData = ranges(d.premium_ranges);
  if (ivData.length) barChart(ivRanges, ivData, { label: 'k', value: 'v', tipFn: rangeTip });
  else ivRanges.append(h('div', { class: 'hint' }, 'Entry IV was unavailable for this run.'));
  if (premiumData.length) barChart(premiumRanges, premiumData, { label: 'k', value: 'v', tipFn: rangeTip });
  else premiumRanges.append(h('div', { class: 'hint' }, 'No short option premium was collected.'));
}

function greeks(body, run) {
  const rows = (run.dashboard.greeks || []).filter((r) => isNum(r.iv));
  if (!rows.length) {
    body.append(card('Greeks at entry', h('div', { class: 'hint' },
      'No usable entry Greeks were computed for this run (prices may be missing or outside no-arbitrage bounds).')));
    return;
  }
  const avg = (key) => rows.reduce((total, row) => total + (row[key] || 0), 0) / rows.length;
  const entries = rows.map((row) => row.entry).sort();
  body.append(h('div', { class: 'grid g4' },
    kpi('Average entry IV', pct(avg('iv')), 'implied vol of the legs traded'),
    kpi('Average net delta', num(avg('net_delta'), 3) + ' lots', 'positive = long the market'),
    kpi('Average net theta', inr(avg('net_theta'), true) + '/day', 'positive = earns time decay', toneOf(avg('net_theta'))),
    kpi('Average net vega', inr(avg('net_vega'), true) + '/pt', 'negative = short volatility', toneOf(avg('net_vega')))));
  const iv = h('div', { class: 'chart' }), delta = h('div', { class: 'chart' });
  const theta = h('div', { class: 'chart' }), vega = h('div', { class: 'chart' });
  body.append(h('div', { style: { height: '14px' } }),
    h('div', { class: 'grid g2' },
      card('Entry implied vol by day', iv, '%'), card('Net delta at entry', delta, 'lots'),
      card('Net theta at entry', theta, '₹ per day'), card('Net vega at entry', vega, '₹ per vol point')),
    h('div', { class: 'hint', style: { margin: '10px 4px' } },
      `Entries between ${entries[0]} and ${entries[entries.length - 1]}. Greeks use Black-76; put-call parity estimates the forward from each session's data.`));
  const daily = rows.map((row) => ({ ...row, date: String(row.date).slice(0, 10) }));
  daySeries(iv, daily, { key: 'iv', kind: 'line', color: COLORS.violet, title: 'Entry IV %', fmt: (value) => num(value, 1) });
  daySeries(delta, daily, { key: 'net_delta', kind: 'bars', title: 'Net delta (lots)', fmt: (value) => num(value, 2) });
  daySeries(theta, daily, { key: 'net_theta', kind: 'bars', title: 'Net theta ₹/day', fmt: (value) => inr(value) });
  daySeries(vega, daily, { key: 'net_vega', kind: 'bars', title: 'Net vega ₹/pt', fmt: (value) => inr(value) });
}

function trades(body, run, ctx) {
  const st = ctx.S.tradeView || (ctx.S.tradeView = { offset: 0, limit: 50, sort: 'date', order: 'asc', reason: '', result: 'all' });
  const ind = run.kind === 'indicator' ? run.strategy.indicator : null;
  const spotEma = ind && ind.type === 'EMA' && ind.ema_source !== 'Option premium';
  const emaCrossover = spotEma && ind.ema_mode === 'EMA crossover';
  const reasons = (run.dashboard.exits || []).map((e) => e.reason);
  const host = h('div'), foot = h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: '10px' } });
  const cols = [['entry_date', 'Entry date'], ['dte', 'DTE'], ['leg_id', 'Leg']];
  if (run.kind === 'indicator') cols.push(['signal_direction', 'Signal']);
  cols.push(['side', 'Side'], ['option_type', 'Type'], ['strike', 'Strike'], ['expiry', 'Expiry'],
    ['qty', 'Qty'], ['entry_time', 'In'], ['entry_price', 'Entry ₹']);
  if (run.kind === 'indicator') {
    if (spotEma) {
      if (emaCrossover) cols.push(['signal_fast_ema_entry', 'Fast EMA @ Signal In'],
        ['signal_slow_ema_entry', 'Slow EMA @ Signal In']);
      else cols.push(['signal_ema_entry', 'EMA @ Signal In']);
    }
  }
  cols.push(['spot_entry', 'Spot @ In'], ['exit_date', 'Exit date'],
    ['exit_time', 'Out'], ['exit_price', 'Exit ₹']);
  if (run.kind === 'indicator') {
    if (spotEma) {
      if (emaCrossover) cols.push(['signal_fast_ema_exit', 'Fast EMA @ Signal Out'],
        ['signal_slow_ema_exit', 'Slow EMA @ Signal Out']);
      else cols.push(['signal_ema_exit', 'EMA @ Signal Out']);
    }
  }
  cols.push(['spot_exit', 'Spot @ Out'], ['exit_reason', 'Exit reason'],
    ['gross_pnl', 'Gross'], ['entry_delta', 'Entry Δ'], ['entry_iv', 'Entry IV %'], ['costs', 'Charges'], ['net_pnl', 'Net']);
  const load = async () => {
    host.style.opacity = '.5';
    try {
      const q = { offset: st.offset, limit: st.limit, sort: st.sort, order: st.order, reason: st.reason, result: st.result };
      const r = await api.trades(run.id, q);
      clear(host).append(h('div', { class: 'tbl-wrap results-trades-wrap' }, h('table', { class: 't results-trades' },
        h('thead', {}, h('tr', {}, cols.map(([k, l], i) => h('th', { class: 'sortable' + (i === 0 ? ' l' : ''), on: { click: () => { st.order = st.sort === k && st.order === 'asc' ? 'desc' : 'asc'; st.sort = k; st.offset = 0; load(); } } }, l + (st.sort === k ? (st.order === 'asc' ? ' ▲' : ' ▼') : '')))),),
        h('tbody', {}, r.rows.map((t) => h('tr', { style: { cursor: 'pointer' }, title: 'Replay this trade from entry to exit', on: { click: () => { ctx.S.explorerDate = t.entry_date; ctx.S.replayTrade = t; ctx.go('explorer'); } } },
          h('td', {}, dShort(t.entry_date)), h('td', {}, t.dte),
          h('td', { class: 'l' }, `${t.leg_id}#${t.segment}`),
          run.kind === 'indicator' ? h('td', { class: 'l' }, t.signal_direction || '—') : null,
          h('td', { class: 'l' }, h('span', { class: 'pill ' + t.side.toLowerCase() }, t.side)),
          h('td', {}, t.option_type), h('td', {}, t.strike), h('td', {}, dShort(t.expiry)), h('td', {}, t.qty),
          h('td', {}, t.entry_time), h('td', {}, num(t.entry_price)),
          spotEma ? (emaCrossover
            ? [h('td', {}, isNum(t.signal_fast_ema_entry) ? num(t.signal_fast_ema_entry, 2) : '—'),
              h('td', {}, isNum(t.signal_slow_ema_entry) ? num(t.signal_slow_ema_entry, 2) : '—')]
            : h('td', {}, isNum(t.signal_ema_entry) ? num(t.signal_ema_entry, 2) : '—')) : null,
          h('td', {}, isNum(t.spot_entry) ? num(t.spot_entry, 2) : '—'),
          h('td', {}, dShort(t.exit_date)),
          h('td', {}, t.exit_time), h('td', {}, num(t.exit_price)),
          spotEma ? (emaCrossover
            ? [h('td', {}, isNum(t.signal_fast_ema_exit) ? num(t.signal_fast_ema_exit, 2) : '—'),
              h('td', {}, isNum(t.signal_slow_ema_exit) ? num(t.signal_slow_ema_exit, 2) : '—')]
            : h('td', {}, isNum(t.signal_ema_exit) ? num(t.signal_ema_exit, 2) : '—')) : null,
          h('td', {}, isNum(t.spot_exit) ? num(t.spot_exit, 2) : '—'),
          h('td', { class: 'l' }, h('span', { class: 'tag' }, t.exit_reason)),
          h('td', { class: cls(t.gross_pnl) }, inr(t.gross_pnl)),
          h('td', {}, isNum(t.entry_delta) ? num(t.entry_delta, 3) : '—'),
          h('td', {}, isNum(t.entry_iv) ? num(t.entry_iv, 1) : '—'),
          h('td', {}, inr(t.costs)), h('td', { class: cls(t.net_pnl) }, h('b', {}, inr(t.net_pnl, true)))))))));
      clear(foot).append(h('span', { class: 'hint' }, `${r.total.toLocaleString('en-IN')} executions · net of this filter ${inr(r.sum_net, true)}`),
        h('div', { class: 'row' }, h('button', { class: 'btn small', disabled: st.offset <= 0, on: { click: () => { st.offset = Math.max(0, st.offset - st.limit); load(); } } }, h('span', { html: icons.left }), 'Prev'),
          h('span', { class: 'hint num' }, `${Math.min(r.total, st.offset + 1)}–${Math.min(r.total, st.offset + st.limit)}`),
          h('button', { class: 'btn small', disabled: st.offset + st.limit >= r.total, on: { click: () => { st.offset += st.limit; load(); } } }, 'Next', h('span', { html: icons.right }))));
    } catch (e) { toast(e.message, 'err'); }
    host.style.opacity = '1';
  };
  const reasonSel = h('select', { style: { width: '200px' }, on: { change: (e) => { st.reason = e.target.value; st.offset = 0; load(); } } }, h('option', { value: '' }, 'All exit reasons'), reasons.map((r) => h('option', { value: r, selected: st.reason === r }, r)));
  const res = h('div', { class: 'seg' }, [['all', 'All'], ['win', 'Winners'], ['loss', 'Losers']].map(([v, l]) => h('button', { class: st.result === v ? 'on' : '', on: { click: (e) => { st.result = v; st.offset = 0; [...e.target.parentNode.children].forEach((b) => b.classList.remove('on')); e.target.classList.add('on'); load(); } } }, l)));
  body.append(h('div', { class: 'card results-trades-card' },
    h('div', { class: 'row results-trade-controls', style: { marginBottom: '12px' } }, reasonSel, res),
    run.kind === 'indicator'
      ? h('p', { class: 'hint' }, 'Signal values use the last closed indicator candle (MACD shows its histogram). Spot @ In is NIFTY candle open; Spot @ Out is NIFTY candle close.')
      : h('p', { class: 'hint' }, 'Spot @ In is NIFTY candle open; Spot @ Out is NIFTY candle close.'),
    host, foot));
  load();
}

function risk(body, run) {
  const d = run.dashboard;
  const dds = d.drawdowns || [];
  body.append(card('Worst drawdowns', dds.length ? table(['Peak', 'Trough', 'Recovered', 'Depth', '% of peak', 'Days down', 'Days to recover'],
    dds.map((e) => h('tr', {}, h('td', { class: 'l' }, dShort(e.peak_date)), h('td', {}, dShort(e.trough_date)), h('td', {}, e.recovered ? dShort(e.recovery_date) : h('span', { class: 'neg' }, 'not yet')),
      h('td', { class: 'neg' }, inr(-e.depth)), h('td', {}, isNum(e.depth_pct) ? pct(e.depth_pct) : '—'), h('td', {}, e.days_to_trough), h('td', {}, e.recovered ? e.days_to_recover : '—')))) : h('div', { class: 'hint' }, 'No drawdowns.'), 'ranked by depth'));
  const sc = h('div', { class: 'chart' });
  const mm = d.maemfe || [];
  body.append(h('div', { style: { height: '14px' } }), card('Intraday excursion: MAE vs MFE', mm.length ? sc : h('div', { class: 'hint' }, 'Available for multi-leg strategies.'), 'each dot is a trading day'));
  if (mm.length) {
    scatterChart(sc, mm);
    const avg = (k) => mm.reduce((a, b) => a + b[k], 0) / mm.length;
    body.append(h('div', { class: 'grid g3', style: { marginTop: '14px' } }, kpi('Average MAE', inr(avg('mae')), 'worst moment of an average day', 'neg'), kpi('Average MFE', inr(avg('mfe')), 'best moment of an average day', 'pos'),
      kpi('Days that were green at some point but closed red', String(mm.filter((x) => x.mfe > 0 && x.net < 0).length), 'candidates for a tighter target / trail')));
  }
}

function diagnostics(run) {
  const set = run.settings;
  const wrap = h('div');
  const skipped = run.skipped || [];
  const real = skipped.filter((s) => !String(s.reason).startsWith('filter:'));
  const filt = skipped.filter((s) => String(s.reason).startsWith('filter:'));
  const block = (title, rows, note) => card(title, rows.length ? table(['Reason', 'Days', 'First date'], rows.map((s) => h('tr', {}, h('td', { class: 'l', style: { whiteSpace: 'normal' } }, s.reason), h('td', {}, s.days), h('td', {}, dShort(String(s.first).slice(0, 10)))))) : h('div', { class: 'hint pos' }, 'None.'), note);
  const dataGaps = run.metrics.data_gaps || [];
  wrap.append(card('Carried-contract data gaps',
    dataGaps.length
      ? table(['Expiry', 'Strike', 'Type', 'Download from', 'Download through', 'Missing sessions'],
        dataGaps.map((gap) => h('tr', {},
          h('td', {}, dShort(gap.expiry)), h('td', {}, gap.strike), h('td', {}, gap.option_type),
          h('td', {}, dShort(gap.from)), h('td', {}, dShort(gap.to)),
          h('td', {}, gap.missing_dates.length))))
      : h('div', { class: 'hint pos' }, 'None detected for carried contracts.'),
    'Exact held-contract sessions not found in the option data; download these expiry/strike/date ranges.'));
  wrap.append(block('Sessions skipped for data reasons', real, 'nothing was guessed or filled in'));
  wrap.append(h('div', { style: { height: '14px' } }), block('Sessions removed by your filters', filt, 'days-to-expiry filter'));
  const sm = [`${set.slip_pct}% of premium`, `min ${set.slip_min_ticks} tick(s)`, `stop ×${set.slip_stop_mult}`, set.slip_thin_volume > 0 ? `thin volume < ${set.slip_thin_volume} ×${set.slip_thin_mult}` : 'no thin-candle rule'].join(' · ');
  const rows = [['Entry fill', set.entry_field === 'open' ? 'open of the entry candle' : 'close of the entry candle'], ['Square-off fill', set.exit_field === 'close' ? 'close of the exit candle' : 'open of the exit candle'],
    ['Slippage model', sm], ['Brokerage', `₹${set.brokerage} per order`], ['Lot size', set.lot_mode === 'auto' ? 'by contract expiry (50 / 25 / 75 / 65)' : `manual ${set.lot_size}`],
    ['Expiry', set.expiry + (set.gap_guard ? ' · missing-expiry guard on' : '')], ['Capital', set.capital ? inr(set.capital) : 'not set (no % metrics)']];
  wrap.append(h('div', { style: { height: '14px' } }), card('Assumptions this run used', table(['Item', 'Value'], rows.map(([a, b]) => h('tr', {}, h('td', { class: 'l' }, a), h('td', { style: { textAlign: 'right' } }, b))), { max: '300px' })));
  wrap.append(h('div', { style: { height: '14px' } }), card('Strategy JSON', h('pre', { style: { margin: 0, maxHeight: '300px', overflow: 'auto', fontSize: '11.5px', color: 'var(--muted)', fontFamily: 'var(--mono)' } }, JSON.stringify(run.strategy, null, 2))));
  return wrap;
}
