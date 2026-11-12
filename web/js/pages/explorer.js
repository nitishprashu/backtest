import { h, clear, icons, inr, num, cls, minutesToHM, toast } from '../dom.js';
import { api, waitJob } from '../api.js';
import { intradayChart, emaVerificationChart, COLORS, LEG_COLORS } from '../charts.js';

let playbackTimer = null;

function exitLabel(reason) {
  const labels = {
    SL: 'Leg stop-loss', TP: 'Leg target', TRAILING_SL: 'Trailing stop',
    PORTFOLIO_SL: 'Strategy stop-loss', PORTFOLIO_TP: 'Strategy target',
    PORTFOLIO_TRAILING_SL: 'Strategy trailing stop', EOD_SQUARE_OFF: 'Scheduled square-off',
    SIGNAL_EXIT: 'Opposite signal', ROLLED: 'Contract rolled', EXPIRY: 'Expiry',
    DATA_GAP: 'Data gap', BACKTEST_END: 'Backtest boundary',
  };
  return labels[reason] ? `${reason} · ${labels[reason]}` : reason;
}

function renderReplay(host, response, speedSelect, requestedTrade) {
  const day = response.chart;
  const date = response.date;
  let index = 0;
  let updateChart;
  let playButton;
  let range;
  let timeLabel;
  let pnlCards;
  let tradeBody;

  const chart = h('div', { class: 'chart' });
  const summary = pnlCards = h('div', { class: 'grid g4', style: { marginTop: '14px' } });
  const status = h('div', { class: 'hint', style: { marginTop: '8px' } });
  const table = h('div', { class: 'tbl-wrap' }, h('table', { class: 't' },
    h('thead', {}, h('tr', {}, ['Leg', 'Contract', 'Qty', 'Entry', 'Exit', 'State', 'Running P&L', 'Exit reason'].map((x, i) =>
      h('th', { class: i === 0 ? 'l' : '' }, x)))),
    tradeBody = h('tbody')));
  let verificationCard = null;
  if (day.indicator && day.indicator.verification) {
    const verification = day.indicator.verification;
    const verificationChart = h('div', { class: 'chart', style: { display: 'none' } });
    let toggle;
    toggle = h('button', { class: 'btn small', on: { click: () => {
      const showing = verificationChart.style.display !== 'none';
      verificationChart.style.display = showing ? 'none' : 'block';
      toggle.textContent = showing ? 'Show chart' : 'Hide chart';
    } } }, 'Show chart');
    verificationCard = h('div', { class: 'card', style: { marginTop: '14px' } },
      h('div', { class: 'row', style: { justifyContent: 'space-between' } },
        h('div', {}, h('h3', {}, 'NIFTY EMA verification'),
          h('div', { class: 'hint' }, `${verification.timeframe} · ${verification.lines.map((line) => line.label).join(' + ')}`)),
        toggle),
      verificationChart);
    emaVerificationChart(verificationChart, verification);
  }
  const moveTo = (next) => {
    index = Math.max(0, Math.min(374, next));
    range.value = index;
    timeLabel.textContent = minutesToHM(index);
    updateChart(index);

    const closed = day.legs.filter((leg) => leg.closed_today && index >= leg.exit_idx);
    const open = day.legs.filter((leg) => index >= leg.entry_idx && !(leg.closed_today && index >= leg.exit_idx));
    const realized = closed.reduce((sum, leg) => sum + leg.net, 0);
    const openMtm = open.reduce((sum, leg) => sum + (leg.mtm[index] || 0), 0);
    const running = realized + openMtm;
    clear(pnlCards).append(
      ...[
        ['Replay time', minutesToHM(index), `${index + 1} / 375 candles`, 0],
        ['Running P&L', inr(running, true), 'realized net + open gross MTM', running],
        ['Realized net', inr(realized, true), `${closed.length} closed trade${closed.length === 1 ? '' : 's'}`, realized],
        ['Open positions', String(open.length), `open MTM ${inr(openMtm, true)}`, 0],
      ].map(([label, value, sub, tone]) => h('div', { class: `kpi ${tone > 0 ? 'p' : tone < 0 ? 'n' : ''}` },
        h('div', { class: 'l' }, label),
        h('div', { class: `v ${label.includes('P&L') || label === 'Realized net' ? cls(tone) : ''}` }, value),
        h('div', { class: 's' }, sub))));

    clear(tradeBody);
    day.legs.forEach((leg) => {
      const entered = index >= leg.entry_idx;
      const exited = leg.closed_today && index >= leg.exit_idx;
      const state = !entered ? 'Waiting' : exited ? 'Closed' : leg.entry_date < date ? 'Carried in · open' : 'Open';
      const pnl = !entered ? '—' : exited ? inr(leg.net, true) : inr(leg.mtm[index], true);
      const entry = !entered ? '—' : leg.entry_date < date
        ? `Before open · ₹${num(leg.entry_price)}` : `${leg.entry_time} · ₹${num(leg.entry_price)}`;
      const exit = exited ? `${leg.exit_time} · ₹${num(leg.exit_price)}` : leg.exit_date > date ? 'Still open' : '—';
      const reason = exited ? exitLabel(leg.reason) : entered && !leg.closed_today ? 'SL / exit rules active' : '—';
      tradeBody.append(h('tr', {},
        h('td', { class: 'l' }, h('span', { class: `pill ${leg.side.toLowerCase()}` }, leg.id)),
        h('td', {}, `${leg.strike} ${leg.type}`), h('td', {}, leg.qty),
        h('td', {}, entry), h('td', {}, exit),
        h('td', {}, h('span', { class: 'tag' }, state)),
        h('td', { class: cls(exited ? leg.net : leg.mtm[index]) }, pnl),
        h('td', {}, reason)));
    });

    if (!day.legs.length) {
      status.textContent = response.skipped.length
        ? `No trades: ${response.skipped.join('; ')}`
        : 'No trades were triggered during this session.';
    } else {
      status.textContent = `${open.length} open · ${closed.length} closed · ${day.legs.length - open.length - closed.length} not yet entered`;
    }
    if (index >= 374 && playbackTimer) {
      clearInterval(playbackTimer);
      playbackTimer = null;
      playButton.textContent = 'Play';
    }
  };

  const startPlayback = () => {
    if (playbackTimer) clearInterval(playbackTimer);
    playbackTimer = setInterval(() => moveTo(index + 1), 1000 / Number(speedSelect.value));
    playButton.textContent = 'Pause';
  };
  speedSelect.addEventListener('change', () => {
    if (playbackTimer) startPlayback();
  });

  const initialLeg = requestedTrade && day.legs.find((leg) =>
    leg.id === `${requestedTrade.leg_id}#${requestedTrade.segment}`);
  index = initialLeg ? initialLeg.entry_idx : 0;
  range = h('input', { type: 'range', min: 0, max: 374, value: index, step: 1,
    style: { flex: '1', minWidth: '120px' }, on: { input: (e) => {
      if (playbackTimer) {
        clearInterval(playbackTimer);
        playbackTimer = null;
        playButton.textContent = 'Play';
      }
      moveTo(Number(e.target.value));
    } } });
  timeLabel = h('span', { class: 'tag', style: { minWidth: '62px', textAlign: 'center' } });

  const legend = h('div', { class: 'legend' },
    day.legs.map((leg, k) => h('span', {}, h('i', { style: { background: LEG_COLORS[k % LEG_COLORS.length] } }),
      `${leg.id} · ${leg.side[0]} ${leg.strike} ${leg.type}`)),
    h('span', {}, h('i', { style: { background: COLORS.amber } }), 'NIFTY spot'),
    ((day.indicator && day.indicator.lines) || []).map((line, k) =>
      h('span', {}, h('i', { style: { background: ['#a78bfa', '#f472b6', '#f59e0b'][k % 3] } }), line.label)));
  clear(host).append(
    h('div', { class: 'card', style: { marginTop: '14px' } },
      h('div', { class: 'row', style: { justifyContent: 'space-between' } },
        h('div', {}, h('h3', {}, `Day replay · ${date}`),
          h('div', { class: 'hint' }, `${response.trade_count} trade segment${response.trade_count === 1 ? '' : 's'} · candle-by-candle P&L and exits`)),
        timeLabel),
      summary,
      h('div', { class: 'row', style: { marginTop: '12px' } },
        h('button', { class: 'btn small', title: 'Step back one candle', on: { click: () => moveTo(index - 1) } }, h('span', { html: icons.left })),
        playButton = h('button', { class: 'btn small', on: { click: () => {
          if (playbackTimer) {
            clearInterval(playbackTimer);
            playbackTimer = null;
            playButton.textContent = 'Play';
          } else {
            if (index >= 374) moveTo(0);
            startPlayback();
          }
        } } }, 'Play'),
        h('button', { class: 'btn small', title: 'Step forward one candle', on: { click: () => moveTo(index + 1) } }, h('span', { html: icons.right })),
        range),
      status,
      chart,
      legend),
    h('div', { class: 'card', style: { marginTop: '14px' } },
      h('h3', {}, 'Trades and exits'),
      table),
    ...(verificationCard ? [verificationCard] : []));

  updateChart = intradayChart(chart, day, { replayIndex: index, replayStart: 0 });
  moveTo(index);
}

export function render(container, ctx) {
  if (playbackTimer) clearInterval(playbackTimer);
  playbackTimer = null;
  const S = ctx.S;
  const coverage = S.cfg.coverage || {};
  const runSelect = h('select', { style: { minWidth: '260px', flex: '1' } },
    h('option', { value: 'current' }, 'Current strategy'),
    ...S.runs.map((run) => h('option', { value: `saved:${run.id}` }, `Saved · ${run.name}`)));
  if (S.run && S.runs.some((run) => run.id === S.run.id)) runSelect.value = `saved:${S.run.id}`;
  const initialDate = (S.replayTrade && S.replayTrade.entry_date) || S.explorerDate || coverage.last || '';
  const dateInput = h('input', { type: 'date', value: initialDate, min: coverage.first || '', max: coverage.last || '' });
  const speedSelect = h('select', { style: { width: '130px' } },
    ...[[1, '1 min candle/sec'], [5, '5 min candles/sec'], [10, '10 min candles/sec']]
      .map(([value, label]) => h('option', { value }, label)));
  speedSelect.value = '5';
  const resultHost = h('div');
  const progress = h('div', { class: 'hint', style: { marginTop: '10px' } });
  const startButton = h('button', { class: 'btn primary', on: { click: () => replay() } }, h('span', { html: icons.play }), 'Replay day');
  container.append(
    h('div', { class: 'page-head' }, h('div', {}, h('h2', {}, 'Trade explorer'),
      h('p', {}, 'Choose a session and strategy, then watch the day unfold one candle at a time.'))),
    h('div', { class: 'card' },
      h('div', { class: 'row', style: { alignItems: 'end', flexWrap: 'wrap' } },
        h('label', { style: { flex: '1', minWidth: '260px' } }, h('div', { class: 'hint', style: { marginBottom: '5px' } }, 'Strategy'), runSelect),
        h('label', {}, h('div', { class: 'hint', style: { marginBottom: '5px' } }, 'Trading day'), dateInput),
        h('label', {}, h('div', { class: 'hint', style: { marginBottom: '5px' } }, 'Playback speed'), speedSelect),
        startButton),
      h('div', { class: 'hint', style: { marginTop: '9px' } },
        'Each step is one 1-minute candle. Current strategy uses the Strategy page settings; saved strategies reuse that run’s configuration. Replay uses historical data and does not save a backtest.'),
      progress),
    resultHost);

  async function replay() {
    if (playbackTimer) clearInterval(playbackTimer);
    playbackTimer = null;
    if (!dateInput.value) {
      toast('Choose a trading day to replay', 'err');
      return;
    }
    startButton.disabled = true;
    clear(resultHost);
    progress.textContent = 'Preparing replay…';
    try {
      let strategy = S.strategy;
      let settings = S.settings;
      let selectedRun = null;
      if (runSelect.value.startsWith('saved:')) {
        const runId = runSelect.value.slice('saved:'.length);
        selectedRun = S.run && S.run.id === runId ? S.run : await api.run(runId);
        strategy = selectedRun.strategy;
        settings = selectedRun.settings;
      }
      const job = await api.replay({ date: dateInput.value, strategy, settings });
      const result = await waitJob(job, (current) => {
        progress.textContent = current.status === 'running'
          ? (current.message || 'Simulating selected day…')
          : 'Preparing replay…';
      });
      if (result.status !== 'done') throw new Error(result.error || `Replay ${result.status}`);
      progress.textContent = selectedRun ? `Strategy: ${selectedRun.name}` : 'Strategy: current editor settings';
      renderReplay(resultHost, result.result, speedSelect, S.replayTrade);
      S.explorerDate = dateInput.value;
    } catch (e) {
      progress.textContent = '';
      toast(e.message, 'err', 8000);
    } finally {
      startButton.disabled = false;
    }
  }

  if (S.replayTrade) replay();
}
