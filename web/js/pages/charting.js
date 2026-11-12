import { h, clear, toast } from '../dom.js';
import { api } from '../api.js';
import { marketChart } from '../charts.js';

const TIMEFRAMES = ['1m', '3m', '5m', '10m', '15m', '30m', '60m', '2h', '4h', '1d', '1w', '1mo'];
const TIMEFRAME_LABELS = {
  '2h': '2 hour', '4h': '4 hour', '1d': 'Daily', '1w': 'Weekly', '1mo': 'Monthly',
};
const STUDIES = ['EMA', 'SMA', 'Bollinger', 'VWAP', 'RSI', 'MACD', 'Ichimoku', 'Supertrend', 'Volume', 'Open Interest'];
const field = (label, input) => h('label', { class: 'f chart-field' }, h('span', {}, label), input);

function studyLabel(study) {
  if (study.type === 'MACD') return `MACD ${study.fast || 12}/${study.slow || 26}/${study.signal || 9}`;
  if (study.type === 'VWAP') return 'Session VWAP';
  if (study.type === 'Ichimoku') return 'Ichimoku 9/26/52';
  if (study.type === 'Supertrend') return `Supertrend ${study.period || 10} × ${study.multiplier || 3}`;
  if (['Volume', 'Open Interest'].includes(study.type)) return study.type;
  return `${study.type} ${study.period || 20}${study.type === 'Bollinger' ? ` × ${study.multiplier || 2}` : ''}`;
}

export function render(container, ctx) {
  const state = ctx.S.chartState || (ctx.S.chartState = {
    instrument: 'spot', timeframe: '1m', studies: [], bars: [], trendlines: [],
  });
  if (!Array.isArray(state.studies)) state.studies = [];
  if (!Array.isArray(state.trendlines)) state.trendlines = [];
  if (!Array.isArray(state.bars)) state.bars = [];
  if (!Array.isArray(state.events)) state.events = [];

  const page = h('section', { class: 'chart-workspace' });
  container.append(page);
  const instrumentSelect = h('select', {}, [
    h('option', { value: 'spot', selected: state.instrument === 'spot' }, 'NIFTY spot'),
    h('option', { value: 'option', selected: state.instrument === 'option' }, 'Option contract'),
  ]);
  const timeframeSelect = h('select', {}, TIMEFRAMES.map((tf) =>
    h('option', { value: tf, selected: tf === state.timeframe }, TIMEFRAME_LABELS[tf] || tf)));
  if (!state.tradeRunId) state.tradeRunId = '';
  const tradeRunSelect = h('select', {}, [
    h('option', { value: '', selected: !state.tradeRunId }, 'Off'),
    ...ctx.S.runs.map((run) => h('option', {
      value: run.id, selected: run.id === state.tradeRunId,
    }, run.name)),
  ]);
  const yearSelect = h('select', {}), expirySelect = h('select', {});
  const strikeSelect = h('select', {}), typeSelect = h('select', {});
  const optionControls = h('div', { class: 'row chart-option-controls' },
    field('Year', yearSelect), field('Expiry', expirySelect),
    field('Strike', strikeSelect), field('Type', typeSelect));
  const chartHost = h('div', { class: 'market-chart-host chart' });
  const studySelect = h('select', {}, STUDIES.map((study) => h('option', { value: study }, study)));
  const studyHost = h('div', { class: 'row chart-study-list' });
  const drawButton = h('button', { class: 'btn small', on: { click: () => {
    state.drawingMode = !state.drawingMode;
    drawButton.classList.toggle('primary', state.drawingMode);
    drawButton.textContent = state.drawingMode ? 'Click two points to draw' : 'Trendline';
    renderChart();
  } } }, 'Trendline');
  const clearLinesButton = h('button', { class: 'btn small ghost', on: { click: () => {
    state.trendlines = [];
    renderChart();
  } } }, 'Clear lines');
  const resetYAxisButton = h('button', { class: 'btn small ghost', on: { click: () => {
    state.yDomain = null;
    renderChart();
  } } }, 'Reset Y axis');
  const chartCard = h('div', { class: 'card' },
    h('div', { class: 'chart-toolbar' },
      field('Instrument', instrumentSelect), field('Interval', timeframeSelect),
      field('Trade markers', tradeRunSelect),
      optionControls),
    h('div', { class: 'row chart-indicators' },
      field('Add indicator', studySelect),
      h('button', { class: 'btn small', on: { click: () => addStudy(studySelect.value) } }, 'Add study'),
      drawButton, clearLinesButton, resetYAxisButton, studyHost),
    chartHost);
  page.append(h('div', { class: 'page-head' },
    h('div', {}, h('h2', {}, 'Charts'), h('p', {}, 'Scroll through available spot and option history with indicators, volume, open interest, and drawing tools.')),
    h('span', { class: 'tag' }, 'NIFTY · IST')), chartCard);

  let contractInfo = null, loadGeneration = 0, loadingOlder = false, hasOlder = false;
  function setOptionControls() {
    const enabled = state.instrument === 'option';
    optionControls.style.display = enabled ? 'flex' : 'none';
    if (!enabled || !contractInfo) return;
    const years = contractInfo.years || [];
    if (!years.length) {
      toast('No option expiry years are available.', 'err');
      return;
    }
    if (!years.includes(Number(state.optionYear))) state.optionYear = contractInfo.selected_year || years[0];
    clear(yearSelect).append(...years.map((year) =>
      h('option', { value: year, selected: year === Number(state.optionYear) }, String(year))));
    const expiries = contractInfo.expiries || [];
    if (!expiries.length) {
      toast(`No option expiries are available for ${state.optionYear}.`, 'err');
      return;
    }
    if (!expiries.includes(state.expiry)) state.expiry = contractInfo.selected_expiry || expiries[0];
    clear(expirySelect).append(...expiries.map((expiry) =>
      h('option', { value: expiry, selected: expiry === state.expiry }, expiry)));
    if (!contractInfo.contracts.length || contractInfo.selected_expiry !== state.expiry) return;
    const strikes = [...new Set(contractInfo.contracts.map((contract) => contract.strike))]
      .sort((a, b) => a - b);
    if (!strikes.includes(Number(state.strike))) state.strike = strikes[Math.floor(strikes.length / 2)];
    clear(strikeSelect).append(...strikes.map((strike) =>
      h('option', { value: strike, selected: strike === Number(state.strike) }, String(strike))));
    const types = [...new Set(contractInfo.contracts.filter((contract) => contract.strike === Number(state.strike))
      .map((contract) => contract.option_type))];
    if (!types.includes(state.optionType)) state.optionType = types[0];
    clear(typeSelect).append(...types.map((type) =>
      h('option', { value: type, selected: type === state.optionType }, type)));
  }

  function paintStudies() {
    clear(studyHost);
    state.studies.forEach((study, index) => {
      const inputs = [];
      if (study.type === 'MACD') {
        for (const [key, label, fallback] of [['fast', 'Fast', 12], ['slow', 'Slow', 26], ['signal', 'Signal', 9]]) {
          inputs.push(field(label, h('input', { type: 'number', min: 2, max: 200, value: study[key] || fallback,
              on: { change: (event) => { study[key] = Number(event.target.value); renderChart(); } } })));
        }
      } else if (['EMA', 'SMA', 'Bollinger', 'RSI', 'Supertrend'].includes(study.type)) {
        inputs.push(field(study.type === 'Bollinger' ? 'Length' : 'Period',
          h('input', { type: 'number', min: 2, max: 200, value: study.period || (study.type === 'RSI' ? 14 : 20),
            on: { change: (event) => { study.period = Number(event.target.value); renderChart(); } } })));
      }
      if (['Bollinger', 'Supertrend'].includes(study.type)) {
        inputs.push(field(study.type === 'Bollinger' ? 'Std dev' : 'Multiplier',
          h('input', { type: 'number', min: 0.5, max: 10, step: 0.5,
            value: study.multiplier || (study.type === 'Bollinger' ? 2 : 3),
            on: { change: (event) => { study.multiplier = Number(event.target.value); renderChart(); } } })));
      }
      studyHost.append(h('div', { class: 'chart-study' },
        h('span', { class: 'tag' }, studyLabel(study)), ...inputs,
        h('button', { class: 'btn small ghost', title: 'Remove study', on: { click: () => {
          state.studies.splice(index, 1); paintStudies(); renderChart();
        } } }, 'Remove')));
    });
  }

  function addStudy(type) {
    if (['Volume', 'Open Interest', 'Ichimoku'].some((name) =>
      name === type && state.studies.some((study) => study.type === type))) return;
    const defaults = { type, period: type === 'RSI' ? 14 : type === 'Supertrend' ? 10 : 20 };
    if (type === 'Bollinger') defaults.multiplier = 2;
    if (type === 'Supertrend') defaults.multiplier = 3;
    if (type === 'MACD') Object.assign(defaults, { fast: 12, slow: 26, signal: 9 });
    state.studies.push(defaults);
    paintStudies();
    renderChart();
  }

  async function loadContracts(year = '', expiry = '') {
    try {
      contractInfo = await api.chartContracts(year, expiry);
      state.optionYear = contractInfo.selected_year;
      state.expiry = contractInfo.selected_expiry;
      if (!state.expiry || !contractInfo.expiries.includes(state.expiry)) {
        state.expiry = contractInfo.selected_expiry;
      }
      setOptionControls();
      await loadChart();
    } catch (error) {
      toast(error.message, 'err');
    }
  }

  function chartDataForState() {
    return {
      title: state.instrument === 'spot' ? 'NIFTY spot' :
        `NIFTY ${state.expiry} ${state.strike} ${state.optionType}`,
      instrument: state.instrument, timeframe: state.timeframe, has_older: hasOlder,
      bars: state.bars, yDomain: state.yDomain,
      events: (state.events || []).map((event) => ({
        ...event, index: state.bars.findIndex((bar) => bar.time === event.time),
      })).filter((event) => event.index >= 0),
    };
  }

  function renderChart() {
    if (!state.bars.length) return;
    marketChart(chartHost, chartDataForState(), state.studies, {
      trendlines: state.trendlines,
      drawingMode: state.drawingMode,
      focusOldest: state.focusOldest,
      onTrendlines: (lines) => {
        state.trendlines = lines;
        clearLinesButton.style.display = '';
      },
      onYAxisDomain: (domain) => { state.yDomain = domain; },
      onLoadOlder: loadOlder,
    });
    drawButton.classList.toggle('primary', Boolean(state.drawingMode));
    clearLinesButton.style.display = state.trendlines.length ? '' : 'none';
  }

  async function fetchBars(before = '') {
    state.instrument = instrumentSelect.value;
    state.timeframe = timeframeSelect.value;
    const query = { instrument: state.instrument, timeframe: state.timeframe, limit: 100 };
    if (before) query.before = before;
    if (state.instrument === 'option') Object.assign(query, {
      expiry: state.expiry, strike: state.strike, option_type: state.optionType,
    });
    if (state.tradeRunId) query.run_id = state.tradeRunId;
    return api.marketChart(query);
  }

  async function loadChart(resetDrawings = false) {
    const generation = ++loadGeneration;
    try {
      const data = await fetchBars();
      if (generation !== loadGeneration) return;
      state.bars = data.bars;
      state.events = data.events || [];
      hasOlder = data.has_older;
      state.focusOldest = false;
      if (resetDrawings) {
        state.trendlines = [];
        state.yDomain = null;
      }
      if (!state.bars.length) {
        chartHost.replaceChildren();
        toast('No candles are available for this instrument and contract.', 'err');
        return;
      }
      renderChart();
    } catch (error) {
      if (generation === loadGeneration) toast(error.message, 'err');
    }
  }

  async function loadOlder(before) {
    if (loadingOlder || !hasOlder) return;
    loadingOlder = true;
    const generation = loadGeneration;
    try {
      const data = await fetchBars(before);
      if (generation !== loadGeneration) return;
      if (!data.bars.length) {
        hasOlder = false;
        return;
      }
      const known = new Set(state.bars.map((bar) => bar.time));
      state.bars = [...data.bars.filter((bar) => !known.has(bar.time)), ...state.bars]
        .sort((a, b) => a.time.localeCompare(b.time));
      const eventKeys = new Set((state.events || []).map((event) => `${event.kind}:${event.time}:${event.strike}:${event.option_type}`));
      state.events = [...(data.events || []).filter((event) =>
        !eventKeys.has(`${event.kind}:${event.time}:${event.strike}:${event.option_type}`)), ...(state.events || [])];
      hasOlder = data.has_older;
      state.focusOldest = true;
      renderChart();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      loadingOlder = false;
    }
  }

  instrumentSelect.addEventListener('change', () => {
    state.instrument = instrumentSelect.value;
    setOptionControls();
    loadChart(true);
  });
  timeframeSelect.addEventListener('change', () => loadChart(true));
  tradeRunSelect.addEventListener('change', () => { state.tradeRunId = tradeRunSelect.value; loadChart(); });
  yearSelect.addEventListener('change', () => {
    state.optionYear = Number(yearSelect.value);
    state.expiry = '';
    loadContracts(state.optionYear);
  });
  expirySelect.addEventListener('change', async () => {
    state.expiry = expirySelect.value;
    try {
      contractInfo = await api.chartContracts(state.optionYear, state.expiry);
      state.strike = null;
      setOptionControls();
      loadChart(true);
    } catch (error) { toast(error.message, 'err'); }
  });
  strikeSelect.addEventListener('change', () => { state.strike = Number(strikeSelect.value); loadChart(true); });
  typeSelect.addEventListener('change', () => { state.optionType = typeSelect.value; loadChart(true); });
  paintStudies();
  loadContracts(state.optionYear || '', state.expiry || '');
}
