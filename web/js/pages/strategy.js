import { h, clear, icons, inr, inr2, num, toast, dLong, isNum } from '../dom.js';
import { api } from '../api.js';
import { payoffChart } from '../charts.js';

let C = null;            // ctx
let previewTimer = null, previewSeq = 0;
const P = { status: 'idle', data: null, error: null, date: '' };   // preview state
let legsHost = null, previewHost = null;

// ------------------------------------------------------------------ form helpers
const changed = () => { C.persist(); schedulePreview(); };
const F = (label, control) => h('label', { class: 'f' }, h('span', {}, label), control);

function numIn(obj, key, { min, max, step = 1, int = false, nullable = false, cb } = {}) {
  const el = h('input', { type: 'number', step, min, max, value: obj[key] ?? '' });
  el.addEventListener('change', () => {
    let v = parseFloat(el.value);
    if (!Number.isFinite(v)) { if (nullable) { obj[key] = null; el.value = ''; changed(); cb && cb(); return; } v = obj[key]; }
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    if (int) v = Math.round(v);
    obj[key] = v; el.value = v;
    changed(); cb && cb();
  });
  return el;
}
function selIn(obj, key, options, cb) {
  const el = h('select', {}, options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return h('option', { value: v, selected: String(obj[key]) === String(v) }, l); }));
  el.addEventListener('change', () => { const raw = el.value; obj[key] = typeof obj[key] === 'number' ? Number(raw) : raw; changed(); cb && cb(); });
  return el;
}
function segIn(obj, key, options, cb, styleFn) {
  const wrap = h('div', { class: 'seg' });
  const paint = () => [...wrap.children].forEach((b) => b.classList.toggle('on', String(b.dataset.v) === String(obj[key])));
  options.forEach((o) => { const [v, l] = Array.isArray(o) ? o : [o, o];
    wrap.append(h('button', { type: 'button', class: styleFn ? styleFn(v) : '', dataset: { v }, on: { click: () => { obj[key] = typeof obj[key] === 'number' ? Number(v) : v; paint(); changed(); cb && cb(); } } }, l)); });
  paint();
  return wrap;
}
function toggleIn(obj, key, label, cb) {
  const input = h('input', { type: 'checkbox', checked: !!obj[key] });
  input.addEventListener('change', () => { obj[key] = input.checked; changed(); cb && cb(); });
  return h('label', { class: 'toggle' }, input, h('span', { class: 'sw' }), label);
}
const timeIn = (obj, key) => { const el = h('input', { type: 'time', step: 60, value: obj[key] });
  el.addEventListener('change', () => { if (el.value) { obj[key] = el.value; changed(); } }); return el; };
const dateIn = (obj, key) => { const el = h('input', { type: 'date', value: obj[key] });
  el.addEventListener('change', () => { if (el.value) { obj[key] = el.value; changed(); } }); return el; };
const holdingModes = [
  ['INTRADAY', 'Intraday · square off each session'],
  ['CARRY_SINGLE', 'Carry forward · one open trade'],
  ['CARRY_FRESH_ENTRIES', 'Carry forward · allow fresh entries'],
];
const holdingHint = (mode) => mode === 'INTRADAY'
  ? 'Open positions are squared off at the configured square-off time each session.'
  : mode === 'CARRY_SINGLE'
    ? 'Hold one position across sessions and reverse on each opposite signal. Intraday square-off and daily limits are ignored.'
    : 'Carry positions across sessions while continuing to take eligible entries. Positions close on configured exits, expiry, or the backtest/data boundary.';

// ------------------------------------------------------------------ legs
const describeLeg = (l, directionalOption = false) => {
  const where = { ATM: 'ATM', 'ATM Offset': 'ATM' + (l.offset >= 0 ? '+' : '') + l.offset,
    Premium: '≈₹' + l.prem, Fixed: String(l.fixed), Greek: `${l.g_target}${l.g_metric === 'delta' ? 'Δ' : ` ${l.g_metric}`}` }[l.rule];
  const bits = [`${l.side.toUpperCase()} ${l.lots}× ${where} ${directionalOption ? 'directional option' : l.type}`];
  if (l.expiry_offset) bits.push(`+${l.expiry_offset} listed expiry`);
  if (l.sl_kind !== 'Off') bits.push(`SL ${l.sl_val}${l.sl_kind === '%' ? '%' : 'pts'}`);
  if (l.tp_kind !== 'Off') bits.push(`TP ${l.tp_val}${l.tp_kind === '%' ? '%' : 'pts'}`);
  if (l.trail_on) bits.push('trail');
  if (l.re_mode !== 'Off') bits.push(`re-entry ×${l.re_n}`);
  return bits.join(' · ');
};

function legCard(S, i, leg, { indicator = false, onRemove, onCopy } = {}) {
  const carrySingle = S.settings.holding_mode === 'CARRY_SINGLE';
  const directionalOption = indicator && (S.strategy.indicator.type === 'MACD'
    || S.strategy.indicator.type === 'EMA' && S.strategy.indicator.ema_source === 'Underlying');
  const card = h('div', { class: 'leg ' + leg.side.toLowerCase() + (indicator ? ' signal-leg' : '') });
  const rebuild = () => card.replaceWith(legCard(S, i, leg, { indicator, onRemove, onCopy }));
  const head = h('div', { class: 'leg-head' },
    h('span', { class: 'no' }, `LEG ${i + 1}`),
    h('span', { class: 'pill ' + leg.side.toLowerCase() }, describeLeg(leg, directionalOption)), h('span', { class: 'spacer' }),
    onCopy && h('button', { class: 'btn ghost small', title: 'Duplicate leg', on: { click: onCopy }, html: icons.copy }),
    onRemove && h('button', { class: 'btn ghost small', title: 'Remove leg', on: { click: onRemove }, html: icons.trash }));
  const strikeParam = leg.rule === 'ATM Offset' ? F('Steps from ATM', numIn(leg, 'offset', { min: -40, max: 40, int: true }))
    : leg.rule === 'Premium' ? F('Target premium ₹', numIn(leg, 'prem', { min: 1, max: 5000, step: 5 }))
    : leg.rule === 'Fixed' ? F('Strike', numIn(leg, 'fixed', { min: 1000, max: 60000, step: 50 }))
      : leg.rule === 'Greek' ? h('div', { class: 'hint', style: { alignSelf: 'end', paddingBottom: '10px' } }, 'closest strike to selected Greek')
        : h('div', { class: 'hint', style: { alignSelf: 'end', paddingBottom: '10px' } }, 'nearest strike to spot');
  const refreshHead = () => { const p = head.querySelector('.pill'); p.textContent = describeLeg(leg, directionalOption); card.className = 'leg ' + leg.side.toLowerCase() + (indicator ? ' signal-leg' : ''); };
  const row1 = h('div', { class: 'fields' },
    F('Action', segIn(leg, 'side', ['Buy', 'Sell'], refreshHead, (v) => v.toLowerCase())),
    directionalOption
      ? F('Option type', h('span', { class: 'hint' }, 'Buy: CE bullish / PE bearish · Sell: PE bullish / CE bearish'))
      : F('Option', segIn(leg, 'type', ['CE', 'PE'], refreshHead)),
    !indicator ? F('Expiry', selIn(leg, 'expiry_offset', [[0, 'Strategy'], [1, 'Next listed'], [2, '+2 listed']]))
      : h('div'),
    F('Strike rule', selIn(leg, 'rule', ['ATM', 'ATM Offset', 'Premium', 'Fixed', 'Greek'], rebuild)), strikeParam,
    F('Lots', numIn(leg, 'lots', { min: 1, max: 500, int: true, cb: refreshHead })));
  const rowG = leg.rule === 'Greek' ? h('div', { class: 'fields risk' },
    F('Greek', selIn(leg, 'g_metric', [['delta', 'Delta (absolute)'], ['iv', 'Implied vol %'],
      ['theta', 'Theta ₹/day (absolute)'], ['vega', 'Vega ₹/vol pt']], rebuild)),
    F(leg.g_metric === 'delta' ? 'Target absolute delta' : 'Target Greek',
      numIn(leg, 'g_target', { min: 0, step: leg.g_metric === 'delta' ? 0.01 : 0.5, cb: refreshHead })),
    F('Minimum (0 = off)', numIn(leg, 'g_min', { min: 0, step: leg.g_metric === 'delta' ? 0.01 : 0.5 })),
    F('Maximum (0 = off)', numIn(leg, 'g_max', { min: 0, step: leg.g_metric === 'delta' ? 0.01 : 0.5 })),
    h('div', { class: 'hint', style: { gridColumn: 'span 4' } }, 'Selects the strike whose Greek is closest to the target; the optional band excludes candidates outside the range.')) : null;
  const row2 = h('div', { class: 'fields risk' },
    F('Stop-loss', selIn(leg, 'sl_kind', ['Off', '%', 'pts'], rebuild)),
    F('SL value', Object.assign(numIn(leg, 'sl_val', { min: 0, step: 1 }), { disabled: leg.sl_kind === 'Off' })),
    F('Target', selIn(leg, 'tp_kind', ['Off', '%', 'pts'], rebuild)),
    F('Target value', Object.assign(numIn(leg, 'tp_val', { min: 0, step: 1 }), { disabled: leg.tp_kind === 'Off' })));
  const advanced = h('details', { class: 'sec' },
    h('summary', {}, 'Trailing stop & re-entry'),
    h('div', { class: 'body' },
      h('div', { class: 'fields risk' },
        !indicator ? F(carrySingle ? 'Re-entry (ignored in carry-one mode)' : 'Re-entry after SL',
          Object.assign(selIn(leg, 're_mode', ['Off', 'Re-execute', 'Re-cost'], rebuild), { disabled: carrySingle || leg.sl_kind === 'Off' })) : null,
        !indicator ? F('Max re-entries', Object.assign(numIn(leg, 're_n', { min: 1, max: 10, int: true }), { disabled: carrySingle || leg.re_mode === 'Off' })) : null),
      leg.sl_kind !== 'Off' ? h('div', { class: 'fields risk', style: { marginTop: '10px' } },
        h('div', { style: { alignSelf: 'end', paddingBottom: '8px' } }, toggleIn(leg, 'trail_on', 'Trailing SL', rebuild)),
        F('Trail every +% of premium', Object.assign(numIn(leg, 'trail_trig', { min: 0.5, step: 1 }), { disabled: !leg.trail_on })),
        F('Move SL by %', Object.assign(numIn(leg, 'trail_step', { min: 0.5, step: 1 }), { disabled: !leg.trail_on }))) : null));
  card.append(head, row1);
  if (rowG) card.append(rowG);
  card.append(row2, advanced);
  if (leg.sl_kind === 'Off') { leg.trail_on = false; if (leg.re_mode !== 'Off') leg.re_mode = 'Off'; }
  return card;
}

function renderLegs() {
  const S = C.S;
  clear(legsHost);
  const st = S.strategy;
  if (st.kind === 'multi') {
    legsHost.append(h('div', { class: 'card' },
      h('h3', {}, 'Legs', h('span', { class: 'sub' }, `${st.legs.length} of 8`)),
      st.legs.map((leg, i) => legCard(S, i, leg, {
        onRemove: () => { if (st.legs.length > 1) { st.legs.splice(i, 1); changed(); renderLegs(); } },
        onCopy: () => { if (st.legs.length < 8) { st.legs.splice(i + 1, 0, C.clone(leg)); changed(); renderLegs(); } } })),
      h('div', { class: 'row' }, h('button', { class: 'btn small', disabled: st.legs.length >= 8, on: { click: () => { const l = C.clone(S.cfg.strategy.legs[0]); l.side = 'Buy'; st.legs.push(l); changed(); renderLegs(); } } }, h('span', { html: icons.plus }), 'Add leg'))));
    legsHost.append(entryCard(st));
    legsHost.append(portfolioCard(st));
  } else {
    legsHost.append(indicatorCard(st));
  }
}

const COND_KINDS = [
  ['atm_iv', 'ATM implied vol (%)'], ['net_delta', 'Net delta (lots)'],
  ['net_theta', 'Net theta (₹/day)'], ['net_vega', 'Net vega (₹/vol pt)'],
  ['spot_vs_vwap', 'Spot vs VWAP'], ['spot_move', 'Spot move from open (%)'],
  ['supertrend', 'Supertrend direction'],
];
const DIRECTIONAL = { spot_vs_vwap: ['above', 'below'], supertrend: ['up', 'down'] };

function entryCard(st) {
  const en = st.entry || (st.entry = { window_end: '', conditions: [] });
  const host = h('div');
  const paint = () => {
    clear(host);
    en.conditions.forEach((c, i) => {
      const direction = DIRECTIONAL[c.kind];
      const kindSelect = selIn(c, 'kind', COND_KINDS, () => {
        const dirs = DIRECTIONAL[c.kind];
        c.value = dirs ? dirs[0] : (c.kind === 'atm_iv' ? 15 : 0);
        c.op = dirs ? 'is' : '>=';
        paint();
      });
      const opSelect = direction
        ? h('div', { class: 'hint', style: { alignSelf: 'end', paddingBottom: '10px' } }, 'is')
        : selIn(c, 'op', [['>=', 'is at least'], ['<=', 'is at most'], ['between', 'is between'],
          ...(c.kind === 'net_delta' ? [['abs<=', 'is within ±']] : [])], paint);
      const step = c.kind === 'net_delta' ? 0.05 : 1;
      host.append(h('div', { class: 'fields', style: { marginBottom: '10px', gridTemplateColumns: 'repeat(6,minmax(0,1fr))' } },
        F(i === 0 ? 'When' : 'And', kindSelect), F('Condition', opSelect),
        direction ? F('Direction', selIn(c, 'value', direction))
          : F(c.op === 'between' ? 'From' : 'Value', numIn(c, 'value', { step })),
        !direction && c.op === 'between' ? F('To', numIn(c, 'value2', { step })) : h('div'),
        c.kind === 'supertrend'
          ? h('div', { class: 'grid g3', style: { gridColumn: 'span 1' } },
            F('Period', numIn(c, 'st_period', { min: 1, max: 100, int: true })),
            F('Mult', numIn(c, 'st_mult', { min: 0.5, max: 10, step: 0.1 })),
            F('TF', selIn(c, 'st_tf', [1, 3, 5, 10, 15, 30])))
          : h('div'),
        h('div', { style: { alignSelf: 'end', textAlign: 'right' } },
          h('button', { class: 'btn ghost small', title: 'Remove', on: { click: () => {
            en.conditions.splice(i, 1); changed(); paint();
          } }, html: icons.trash }))));
    });
    if (!en.conditions.length) host.append(h('div', { class: 'hint', style: { marginBottom: '10px' } },
      'No conditions: the strategy enters at the entry time. Add Greek or spot conditions to delay entry.'));
  };
  paint();
  return h('div', { class: 'card', style: { marginTop: '14px' } },
    h('h3', {}, 'Entry conditions', h('span', { class: 'sub' }, 'all must hold · checked at the start of each candle')),
    host,
    h('div', { class: 'row' },
      h('button', { class: 'btn small', on: { click: () => {
        en.conditions.push({ kind: 'atm_iv', op: '>=', value: 15, value2: 20, st_period: 10, st_mult: 3, st_tf: 5 });
        changed(); paint();
      } } }, h('span', { html: icons.plus }), 'Add condition'),
      h('label', { class: 'f', style: { width: '210px', marginLeft: 'auto' } },
        h('span', {}, 'Wait until (last entry time)'),
        (() => {
          const el = h('input', { type: 'time', step: 60, value: en.window_end || '' });
          el.addEventListener('change', () => { en.window_end = el.value || ''; changed(); });
          return el;
        })())),
    h('div', { class: 'hint', style: { marginTop: '8px' } },
      'The strategy enters at the first eligible minute when all conditions are true. Leave the window empty to check only at the entry time. Days where conditions never hold are reported as filtered.'));
}

function portfolioCard(st) {
  const p = st.portfolio;
  const rebuild = () => { const n = portfolioCard(st); card.replaceWith(n); card = n; };
  let card = h('details', { class: 'sec', style: { marginTop: '14px' } },
    h('summary', {}, 'Strategy-level rules', h('span', { class: 'sub' }, 'combined SL/target/trailing for all legs')),
    h('div', { class: 'body' },
    h('div', { class: 'grid g4' },
      F('Combined SL', selIn(p, 'sl_kind', [['Off', 'Off'], ['₹', '₹ loss'], ['% of premium', '% loss of premium'], ['pts', 'pts combined premium']], rebuild)),
      F('Value', Object.assign(numIn(p, 'sl_val', { min: 0, step: p.sl_kind === 'pts' ? 1 : 100 }), { disabled: p.sl_kind === 'Off' })),
      F('Target', selIn(p, 'tp_kind', ['Off', '₹', '% of premium'], rebuild)), F('Value', Object.assign(numIn(p, 'tp_val', { min: 0, step: 100 }), { disabled: p.tp_kind === 'Off' }))),
    h('div', { class: 'hint', style: { marginTop: '8px' } }, 'Combined points sum each leg’s adverse premium move, scaled by its lots (one strategy lot). You can use this together with individual leg stops.'),
    h('div', { class: 'grid g4', style: { marginTop: '12px' } },
      h('div', { style: { alignSelf: 'end', paddingBottom: '8px' } }, p.sl_kind === 'Off' ? h('span', { class: 'hint' }, 'Trailing needs a stop-loss') : toggleIn(p, 'trail_on', 'Trailing SL', rebuild)),
      F('Per ₹ profit', Object.assign(numIn(p, 'trail_trig', { min: 1, step: 100 }), { disabled: !p.trail_on })),
      F('Raise SL by ₹', Object.assign(numIn(p, 'trail_step', { min: 1, step: 100 }), { disabled: !p.trail_on })))));
  if (p.sl_kind === 'Off') p.trail_on = false;
  return card;
}

function indicatorLegsCard(st) {
  const defaults = C.S.cfg.strategy.indicator_legs || [C.S.cfg.strategy.ind_leg];
  if (!Array.isArray(st.indicator_legs) || !st.indicator_legs.length) {
    st.indicator_legs = [C.clone(st.ind_leg || defaults[0])];
  }
  let host;
  const paint = () => {
    const legs = st.indicator_legs;
    st.ind_leg = legs[0];
    clear(host).append(
      h('div', { class: 'row signal-leg-title' },
        h('h3', {}, 'Options traded on each signal'),
        h('span', { class: 'tag' }, `${legs.length} of 8 legs`)),
      h('p', { class: 'hint signal-leg-hint' }, 'Each signal enters this exact basket. On an opposite signal, Reverse, Exit or Ignore controls whether it re-enters, closes or holds.'),
      h('div', { class: 'signal-leg-list' }, legs.map((leg, i) => legCard(C.S, i, leg, {
        indicator: true,
        onCopy: () => {
          if (legs.length < 8) { legs.splice(i + 1, 0, C.clone(leg)); changed(); paint(); }
        },
        onRemove: () => {
          if (legs.length > 1) { legs.splice(i, 1); changed(); paint(); }
        },
      }))),
      h('div', { class: 'row signal-leg-actions' },
        h('button', { class: 'btn small', disabled: legs.length >= 8, on: { click: () => {
          const next = C.clone(defaults[0]);
          next.type = legs.length % 2 ? 'PE' : 'CE';
          legs.push(next); changed(); paint();
        } } }, h('span', { html: icons.plus }), 'Add option leg')));
  };
  host = h('div', { class: 'card signal-legs-card' });
  paint();
  return host;
}

function indicatorCard(st) {
  const ind = st.indicator;
  const body = h('div');
  const timeframes = [['1m', '1 minute'], ['5m', '5 minutes'], ['15m', '15 minutes'],
    ['30m', '30 minutes'], ['60m', '60 minutes'], ['1d', '1 day'], ['1w', '1 week'], ['1mo', '1 month']];
  const timeframeKey = () => ({ Supertrend: 'st_timeframe', VWAP: 'vwap_timeframe',
    EMA: 'ema_timeframe', MACD: 'macd_timeframe' })[ind.type];
  const timeframeLabel = (value) => (timeframes.find(([key]) => key === value) || [value, value])[1];
  const defaultTimeframe = () => ind.type === 'Supertrend' ? '5m' : '1m';
  const emaDescription = () => ind.type === 'EMA'
    ? ind.ema_mode === 'EMA crossover'
      ? `EMA crossover · ${ind.ema_fast}/${ind.ema_slow} ${timeframeLabel(ind.ema_timeframe)} bars`
      : `Price vs EMA · ${ind.ema_period} ${timeframeLabel(ind.ema_timeframe)} bars${ind.ema_slope_filter ? ` · ${ind.ema_slope_type} slope ${ind.ema_slope_period}` : ''}`
    : 'Signal rules, schedule & risk limits';
  const paint = () => {
    clear(body);
    const key = timeframeKey();
    if (!timeframes.some(([value]) => value === ind[key])) {
      const legacySupertrend = Number(ind.st_tf);
      const migrated = Number.isFinite(legacySupertrend) ? `${legacySupertrend}m` : '';
      ind[key] = ind.type === 'Supertrend' && timeframes.some(([value]) => value === migrated)
        ? migrated : defaultTimeframe();
    }
    const selectorRow = h('div', { class: 'grid g2' },
      F('Indicator', segIn(ind, 'type', ['Supertrend', 'VWAP', 'EMA', 'MACD'], paint)),
      F('On opposite signal', selIn(ind, 'on_opposite', ['Reverse', 'Exit', 'Ignore'])));

    let parameters;
    let parameterTitle;
    let parameterSummary;
    if (ind.type === 'Supertrend') {
      parameterTitle = 'Supertrend parameters';
      parameterSummary = 'Tune indicator calculation';
      parameters = h('div', { class: 'grid g3 body' },
        F('ATR period (selected bars)', numIn(ind, 'st_period', { min: 1, max: 100, int: true })),
        F('Multiplier', numIn(ind, 'st_mult', { min: 0.5, max: 10, step: 0.1 })),
        ind.st_timeframe.endsWith('m')
          ? h('div', { style: { alignSelf: 'end', paddingBottom: '8px' } }, toggleIn(ind, 'st_prev', 'Carry state across sessions'))
          : null);
    } else if (ind.type === 'VWAP') {
      parameterTitle = 'VWAP parameters';
      parameterSummary = 'Tune indicator calculation';
      parameters = h('div', { class: 'grid g3 body' },
        F('VWAP of', selIn(ind, 'vwap_source', ['Spot', 'Premium'])),
        F('Buffer %', numIn(ind, 'vwap_buf', { min: 0, max: 5, step: 0.01 })),
        F('Confirm candles', numIn(ind, 'vwap_confirm', { min: 1, max: 20, int: true })));
    } else if (ind.type === 'MACD') {
      parameterTitle = 'MACD parameters';
      parameterSummary = `MACD crossover · ${ind.macd_fast}/${ind.macd_slow}/${ind.macd_signal} ${timeframeLabel(ind.macd_timeframe)} bars`;
      parameters = h('div', {}, h('div', { class: 'grid g4 body' },
        F('Fast EMA period', numIn(ind, 'macd_fast', { min: 1, max: 499, int: true })),
        F('Slow EMA period', numIn(ind, 'macd_slow', { min: 2, max: 500, int: true })),
        F('Signal EMA period', numIn(ind, 'macd_signal', { min: 1, max: 500, int: true }))),
      h('p', { class: 'hint' },
        'Bullish when the MACD line crosses above its signal line; bearish when it crosses below. MACD is calculated on NIFTY spot using closed candles.'));
    } else {
      parameterTitle = 'EMA parameters';
      parameterSummary = emaDescription();
      const emaControls = h('div', { class: 'grid g3 body' },
        F('EMA calculated on', selIn(ind, 'ema_source', ['Underlying', 'Option premium'], paint)),
        F('EMA signal rule', selIn(ind, 'ema_mode', ['Price vs EMA', 'EMA crossover'], paint)),
        ind.ema_mode === 'Price vs EMA'
          ? F('EMA period (selected bars)', numIn(ind, 'ema_period', { min: 1, max: 500, int: true }))
          : F('Fast EMA period (selected bars)', numIn(ind, 'ema_fast', { min: 1, max: 499, int: true })),
        ind.ema_mode === 'EMA crossover'
          ? F('Slow EMA period (selected bars)', numIn(ind, 'ema_slow', { min: 2, max: 500, int: true }))
          : h('div'));
      let emaHint;
      if (ind.ema_mode === 'Price vs EMA') {
        const slopeControls = ind.ema_slope_filter
          ? h('div', { class: 'grid g2', style: { marginTop: '10px' } },
            F('Trend average', selIn(ind, 'ema_slope_type', ['EMA', 'SMA'])),
            F('Trend average period (selected bars)', numIn(ind, 'ema_slope_period', { min: 1, max: 500, int: true })))
          : null;
        emaHint = h('p', { class: 'hint' },
          `${ind.ema_source === 'Option premium' ? 'Each selected CE/PE premium' : 'Spot'} is compared with its reference EMA. Long signals require price above the EMA and, when enabled, the selected trend average rising; short signals require price below the EMA and the average falling.`);
        parameters = h('div', {},
          h('div', { style: { marginTop: '10px' } },
            toggleIn(ind, 'ema_slope_filter', 'Require a rising/falling trend average', paint)),
          slopeControls, emaHint);
      } else {
        parameters = h('p', { class: 'hint' },
          'Bullish when the fast EMA is above the slow EMA; bearish when it is below. Signals use closed candles.');
      }
      parameters = h('div', {}, emaControls, parameters);
    }

    const indicatorDetails = h('details', { class: 'sec' },
      h('summary', {}, parameterTitle, h('span', { class: 'sub' }, parameterSummary)),
      parameters);
    const timeframeHint = ind.type === 'EMA'
      ? h('p', { class: 'hint' }, ind.ema_source === 'Option premium'
        ? `CE and PE premiums are evaluated independently using ${timeframeLabel(ind.ema_timeframe)} candles. Above EMA buys that option; below EMA sells it.`
        : `EMA periods count ${timeframeLabel(ind.ema_timeframe)} NIFTY spot candles.`)
      : ind.type === 'VWAP'
        ? h('p', { class: 'hint' }, 'Spot VWAP uses equal-weight typical prices; intraday VWAP accumulates from session open. Premium VWAP uses real volume from selected CE/PE contracts.')
        : ind.type === 'MACD'
          ? h('p', { class: 'hint' }, `MACD periods count ${timeframeLabel(ind.macd_timeframe)} NIFTY spot candles. EMA state carries across sessions; candles outside market hours are excluded.`)
        : null;
    const schedule = h('details', { class: 'sec' },
      h('summary', {}, 'Signal schedule & risk', h('span', { class: 'sub' }, 'Entry window, trade limits and exits')),
      h('div', { class: 'body' },
        h('div', { class: 'grid g4' },
          F('First signal', timeIn(ind, 'start_time')),
          F('Last entry', Object.assign(timeIn(ind, 'last_entry_time'),
            { disabled: C.S.settings.holding_mode !== 'INTRADAY' })),
          F('Intraday square-off', Object.assign(timeIn(ind, 'exit_time'),
            { disabled: C.S.settings.holding_mode !== 'INTRADAY' })),
          F(C.S.settings.holding_mode === 'CARRY_SINGLE'
            ? 'Max trades / day (ignored in carry-one mode)' : 'Max trades / day',
          Object.assign(numIn(ind, 'max_trades', { min: 1, max: 50, int: true }),
            { disabled: C.S.settings.holding_mode === 'CARRY_SINGLE' }))),
        h('div', { class: 'grid g3', style: { marginTop: '12px' } },
          h('div', { style: { alignSelf: 'end', paddingBottom: '8px' } },
            toggleIn(ind, 'initial_trend', 'Also enter if a trend is already running')),
          F(C.S.settings.holding_mode === 'CARRY_SINGLE'
            ? 'Daily max loss ₹ (ignored in carry-one mode)' : 'Daily max loss ₹ (0 = off)',
          Object.assign(numIn(ind, 'daily_max_loss', { min: 0, step: 500 }),
            { disabled: C.S.settings.holding_mode === 'CARRY_SINGLE' })),
          F(C.S.settings.holding_mode === 'CARRY_SINGLE'
            ? 'Daily target ₹ (ignored in carry-one mode)' : 'Daily target ₹ (0 = off)',
          Object.assign(numIn(ind, 'daily_target', { min: 0, step: 500 }),
            { disabled: C.S.settings.holding_mode === 'CARRY_SINGLE' })))));

    body.append(selectorRow, h('div', { class: 'divider' }),
      F('Indicator timeframe', selIn(ind, timeframeKey(), timeframes, paint)),
      F('Position holding', selIn(C.S.settings, 'holding_mode', holdingModes,
        () => { renderSettings(); paint(); })),
      h('div', { class: 'hint', style: { marginTop: '-6px' } }, holdingHint(C.S.settings.holding_mode)),
      indicatorDetails,
      h('p', { class: 'hint', style: { marginTop: '8px' } },
        'Signals use only fully closed candles. Daily, weekly, and monthly signals become available in the next session, calendar week, or calendar month.'),
      timeframeHint || '',
      h('div', { class: 'divider' }), schedule);
  };
  paint();
  return h('div', {}, h('div', { class: 'card' }, h('h3', {}, 'Indicator signals', h('span', { class: 'sub' }, 'signals use only candles that have closed')), body),
    indicatorLegsCard(st));
}

// ------------------------------------------------------------------ settings sections
function settingsCards(S) {
  const g = S.settings;
  const cov = S.cfg.coverage || {};
  const sec = (title, body, open = false) => h('details', { class: 'sec', open }, h('summary', {}, title), h('div', { class: 'body' }, body));
  const quick = (label, days) => h('button', { class: 'btn small', on: { click: () => {
    const last = cov.last || g.end; const end = new Date(last + 'T00:00:00'); const s = new Date(end);
    if (days === 'all') g.start = cov.first || g.start; else s.setDate(s.getDate() - days), g.start = s.toISOString().slice(0, 10);
    g.end = last; changed(); renderSettings(); } } }, label);
  const days = h('div', { class: 'days' }, S.cfg.weekdays.map((d) => {
    const b = h('button', { class: g.weekdays.includes(d) ? 'on' : '', on: { click: () => { g.weekdays = g.weekdays.includes(d) ? g.weekdays.filter((x) => x !== d) : [...g.weekdays, d]; b.classList.toggle('on'); changed(); } } }, d.slice(0, 3));
    return b; }));
  const presets = { 'No slippage': [0, 0, 1], 'Liquid ATM': [0.1, 1, 2], Conservative: [0.25, 2, 3], Stress: [0.5, 3, 4] };
  return [
    sec('Period & schedule', [
      h('div', { class: 'grid g2' }, F('From', dateIn(g, 'start')), F('To', dateIn(g, 'end'))),
      h('div', { class: 'row' }, quick('1M', 30), quick('3M', 91), quick('6M', 182), quick('1Y', 365), quick('2Y', 730), quick('All', 'all')),
      cov.first ? h('div', { class: 'hint' }, `Spot data available ${cov.first} → ${cov.last}`) : null,
      S.strategy.kind !== 'indicator' ? F('Position holding', selIn(g, 'holding_mode', holdingModes,
        () => { renderSettings(); renderLegs(); })) : null,
      S.strategy.kind !== 'indicator' ? h('div', { class: 'hint' }, holdingHint(g.holding_mode)) : null,
      h('div', { class: 'grid g2' }, F('Entry time', timeIn(g, 'entry_time')),
        F(g.holding_mode === 'INTRADAY' ? 'Square-off time' : 'Intraday square-off (carry ignores this)',
          Object.assign(timeIn(g, 'exit_time'), { disabled: g.holding_mode !== 'INTRADAY' }))),
      F('Trade on', h('div', { class: 'grid', style: { gap: '10px' } },
        days,
        toggleIn(g, 'expiry_day_only', 'Only trade on option expiry dates'))),
      h('div', { class: 'hint', style: { marginTop: '-4px' } },
        'Uses listed contract expiry dates (including holiday-shifted expiry sessions) and combines with the selected weekdays. The expiry selector still determines which contracts are traded.'),
    ], true),
    sec('Expiry & filters', [
      h('div', { class: 'grid g2' }, F('Expiry', selIn(g, 'expiry', S.cfg.expiry_rules)),
        h('div', { style: { alignSelf: 'end', paddingBottom: '8px' } }, toggleIn(g, 'gap_guard', 'Skip day if an expiry looks missing'))),
      g.holding_mode !== 'INTRADAY'
        ? h('div', {},
          F('Strike on expiry rollover', selIn(g, 'rollover_strike_mode', [
            ['ORIGINAL', 'Keep original strike'],
            ['RESELECT', 'Reapply configured strike rule'],
          ])),
          h('div', { class: 'hint', style: { marginTop: '6px' } },
            'Reapply the selected ATM, offset, premium, fixed, or Greek rule using the rollover session data.'))
        : null,
      S.strategy.kind === 'multi'
        ? h('div', {},
          F('ATM strike reference', selIn(g, 'atm_reference', [
            ['SPOT', 'NIFTY spot'],
            ['SYNTHETIC_FUTURE', 'Synthetic future (ATM CE − PE)'],
          ], () => { changed(); renderSettings(); })),
          h('div', { class: 'hint', style: { marginTop: '6px' } },
            'Used by ATM and ATM Offset legs. Synthetic future is estimated at the entry candle as ATM strike + CE premium − PE premium; other strike rules are unchanged.'))
        : null,
      h('div', { class: 'grid g2' }, F('Min days to expiry', numIn(g, 'dte_min', { min: 0, max: 60, int: true, nullable: true })), F('Max days to expiry', numIn(g, 'dte_max', { min: 0, max: 60, int: true, nullable: true }))),
      h('div', { class: 'hint' }, 'Leave the day-to-expiry boxes empty to trade every day. Filtered days are reported separately, never counted as data problems.')]),
    sec('Capital, lots & charges', [
      h('div', { class: 'grid g2' }, F('Capital ₹ (0 = none)', numIn(g, 'capital', { min: 0, step: 10000 })), F('Brokerage per order ₹', numIn(g, 'brokerage', { min: 0, step: 1 }))),
      h('div', { class: 'grid g2' }, F('Lot size', segIn(g, 'lot_mode', [['auto', 'Auto by expiry'], ['manual', 'Manual']], renderSettings)),
        F('Units per lot', Object.assign(numIn(g, 'lot_size', { min: 1, max: 5000, int: true }), { disabled: g.lot_mode === 'auto' }))),
      h('div', { class: 'hint' }, 'Auto uses the contract-expiry lot schedule (50 / 25 / 75 / 65). STT, exchange, IPFT, SEBI, stamp and GST follow dated schedules.')]),
    sec('Slippage model (no bid/ask in the data)', [
      h('div', { class: 'row' }, Object.entries(presets).map(([n, v]) => h('button', { class: 'btn small', on: { click: () => { [g.slip_pct, g.slip_min_ticks, g.slip_stop_mult] = v; changed(); renderSettings(); } } }, n))),
      h('div', { class: 'grid g3' }, F('% of premium / fill', numIn(g, 'slip_pct', { min: 0, step: 0.05 })), F('Minimum ticks (₹0.05)', numIn(g, 'slip_min_ticks', { min: 0, step: 0.5 })), F('Stop-exit multiplier', numIn(g, 'slip_stop_mult', { min: 1, step: 0.5 }))),
      h('div', { class: 'grid g2' }, F('Thin candle if volume below', numIn(g, 'slip_thin_volume', { min: 0, step: 50 })), F('Thin-candle multiplier', numIn(g, 'slip_thin_mult', { min: 1, step: 0.5 }))),
      h('div', { class: 'hint' }, 'Each fill pays max(% of premium, minimum ticks); stop exits pay the multiplier on top; candles with low volume pay the thin multiplier. These are modelling estimates, so calibrate them against your own contract notes.')]),
    sec('Greeks model', [
      h('div', { class: 'grid g2' },
        F('Risk-free rate %', numIn(g, 'greek_rate', { min: 0, max: 25, step: 0.25 })),
        F('Forward / carry', segIn(g, 'greek_forward', [['parity', 'From put-call parity'], ['manual', 'Manual']], renderSettings))),
      g.greek_forward === 'manual'
        ? h('div', { class: 'grid g2' }, F('Dividend yield %', numIn(g, 'greek_div_yield', { min: 0, max: 20, step: 0.1 })))
        : null,
      h('div', { class: 'hint' }, 'Black-76 estimates IV from each contract price. Forward is inferred from put-call parity each session (or set manually); time runs to 15:30 on expiry day. Greeks are unavailable when prices violate no-arbitrage bounds.')]),
    sec('Fill assumptions', [
      h('div', { class: 'grid g2' }, F('Entry fill', segIn(g, 'entry_field', [['open', 'Candle open'], ['close', 'Candle close']])), F('Square-off fill', segIn(g, 'exit_field', [['close', 'Candle close'], ['open', 'Candle open']]))),
      h('div', { class: 'hint' }, 'Stops and targets use candle high/low. If both are touched in one candle the stop wins; a gap through a stop fills at the open.')]),
  ];
}
let settingsHost = null;
function renderSettings() { clear(settingsHost); settingsCards(C.S).forEach((c) => settingsHost.append(c)); }

// ------------------------------------------------------------------ live preview
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, 550);
}
async function loadPreview() {
  const S = C.S;
  const seq = ++previewSeq;
  P.status = 'loading'; paintPreview();
  try {
    const res = await api.preview({ settings: S.settings, strategy: S.strategy, date: P.date || undefined });
    if (seq !== previewSeq) return;
    P.status = 'ok'; P.data = res; P.error = null;
  } catch (e) {
    if (seq !== previewSeq) return;
    P.status = 'error'; P.error = e.message;
  }
  paintPreview();
}
function paintPreview() {
  if (!previewHost || !previewHost.isConnected) return;
  clear(previewHost);
  const d = P.data;
  const head = h('h3', {}, 'Preview on a real day', h('span', { class: 'sub' }, P.status === 'loading' ? 'updating…' : ''));
  previewHost.append(head);
  if (P.status === 'error') { previewHost.append(h('div', { class: 'hint neg' }, P.error)); return; }
  if (!d) { previewHost.append(h('div', { class: 'skel', style: { height: '220px' } })); return; }
  if (!d.supported) { previewHost.append(h('div', { class: 'hint' }, d.message)); return; }
  const stat = (l, v, c = '') => h('div', { class: 'kpi' + (c === 'pos' ? ' p' : c === 'neg' ? ' n' : ''), style: { padding: '10px 12px' } }, h('div', { class: 'l' }, l), h('div', { class: 'v ' + c, style: { fontSize: '17px', marginTop: '3px' } }, v));
  previewHost.append(
    h('div', { class: 'row', style: { marginBottom: '10px' } },
      h('span', { class: 'tag' }, dLong(d.date)), h('span', { class: 'tag' }, `expiry ${d.expiry} (${d.dte}d)`), h('span', { class: 'tag' }, `spot ${num(d.spot, 1)}`), h('span', { class: 'tag' }, `lot ${d.lot}`),
      d.entry_time ? h('span', { class: 'tag' }, `entry ${d.entry_time}`) : null,
      d.conditions?.enabled ? h('span', { class: 'tag ' + (d.conditions.met ? 'pos' : 'neg') },
        d.conditions.met ? 'conditions met' : 'conditions not met') : null,
      h('input', { type: 'date', value: P.date || d.date, style: { width: '140px', marginLeft: 'auto' }, on: { change: (e) => { P.date = e.target.value; loadPreview(); } } })),
    h('div', { class: 'tbl-wrap', style: { maxHeight: '170px', marginBottom: '10px' } }, h('table', { class: 't' },
      h('thead', {}, h('tr', {}, ['Leg', 'Expiry', 'Strike', 'Premium', 'Fill', 'Qty', 'IV %', 'Delta'].map((t) => h('th', {}, t)))),
      h('tbody', {}, d.legs.map((l) => l.error ? h('tr', {}, h('td', { colspan: 8, class: 'neg' }, `Leg ${l.i}: ${l.error}`))
        : h('tr', {}, h('td', {}, h('span', { class: 'pill ' + l.side.toLowerCase() }, `${l.side[0]} ${l.type}`)),
          h('td', {}, l.expiry), h('td', {}, l.strike), h('td', {}, num(l.premium)), h('td', {}, num(l.fill)), h('td', {}, l.qty),
          h('td', {}, isNum(l.iv) ? num(l.iv, 1) : '—'), h('td', {}, isNum(l.delta) ? num(l.delta, 3) : '—')))))));
  if (d.payoff_error) previewHost.append(h('div', { class: 'hint' }, d.payoff_error));
  if (d.conditions?.enabled && !d.conditions.met) {
    previewHost.append(h('div', { class: 'hint neg', style: { marginBottom: '10px' } }, d.conditions.message));
  }
  if (d.payoff) {
    const host = h('div', { class: 'chart' });
    previewHost.append(h('div', { class: 'grid g2', style: { marginBottom: '10px' } },
      stat(d.net_premium >= 0 ? 'Net credit' : 'Net debit', inr(Math.abs(d.net_premium)), d.net_premium >= 0 ? 'pos' : 'neg'),
      stat('Premium traded', inr(d.premium_notional)),
      stat('Max profit', d.unlimited_profit ? 'Unlimited' : inr(d.max_profit), 'pos'), stat('Max loss', d.unlimited_loss ? 'Unlimited' : inr(d.max_loss), 'neg')),
      d.net?.complete ? h('div', { class: 'grid g4', style: { gridColumn: '1 / -1' } },
        stat('Net delta', num(d.net.delta, 3) + ' lots'),
        stat('Net gamma', num(d.net.gamma, 4)),
        stat('Net theta', inr(d.net.theta, true) + '/day'),
        stat('Net vega', inr(d.net.vega, true) + '/pt')) : null,
      host, h('div', { class: 'hint', style: { marginTop: '6px' } }, `Break-even${d.breakevens.length > 1 ? 's' : ''}: ${d.breakevens.map((b) => Math.round(b)).join(' · ') || 'none'}. Expiry payoff at entry fills, before stops, targets and charges.`));
    payoffChart(host, d);
  }
}

// ------------------------------------------------------------------ page
export function render(container, ctx) {
  C = ctx;
  const S = ctx.S;
  const st = S.strategy;
  const fileIn = h('input', { type: 'file', accept: '.json', style: { display: 'none' }, on: { change: async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try { const j = JSON.parse(await f.text()); const s = j.strategy || j; if (!s.legs) throw new Error('not a strategy file'); S.strategy = s; if (j.settings) Object.assign(S.settings, j.settings); ctx.persist(); ctx.rerender(); toast('Strategy loaded', 'ok'); }
    catch (err) { toast('Could not load: ' + err.message, 'err'); } } } });
  container.append(h('div', { class: 'page-head' },
    h('div', {}, h('h2', {}, 'Strategy builder'), h('p', {}, 'Build, preview on a real day, then run it over your history.')),
    h('div', { class: 'row' },
      h('button', { class: 'btn small', on: { click: () => fileIn.click() } }, h('span', { html: icons.upload }), 'Load'),
      h('button', { class: 'btn small', on: { click: () => {
        const blob = new Blob([JSON.stringify({ app: 'nifty_options_lab', strategy: S.strategy, settings: S.settings }, null, 2)], { type: 'application/json' });
        const a = h('a', { href: URL.createObjectURL(blob), download: 'nifty_strategy.json' }); a.click(); } } }, h('span', { html: icons.save }), 'Save'),
      h('button', { class: 'btn small ghost', on: { click: () => { ctx.resetStrategy(); ctx.rerender(); } } }, 'Reset'), fileIn)));

  const typeCard = h('div', { class: 'card', style: { marginBottom: '14px' } },
    h('div', { class: 'row', style: { justifyContent: 'space-between' } },
      h('h3', { style: { margin: 0 } }, 'Strategy type'),
      segIn(st, 'kind', [['multi', 'Multi-leg · time based'], ['indicator', 'Indicator signals']], () => { ctx.rerender(); })));
  if (st.kind === 'multi') {
    typeCard.append(h('div', { class: 'divider' }), h('div', { class: 'hint', style: { marginBottom: '8px' } }, 'Start from a template (replaces your legs):'),
      h('div', { class: 'tpl-row' }, S.cfg.templates.map((t) => h('div', { class: 'tpl', on: { click: () => {
        st.legs = t.legs.map((l, i) => ({ ...C.clone(S.cfg.strategy.legs[0]), ...l })); changed(); renderLegs(); toast(`Template: ${t.name}`, 'ok', 1800); } } },
        h('b', {}, t.name), h('div', { class: 'mini' }, t.legs.map((l) => h('span', { class: l.side === 'Buy' ? 'b' : 's' },
          `${l.side[0]}${l.type}${l.rule === 'Greek' ? ` ${l.g_target}Δ` : l.rule === 'ATM' ? '' : l.offset > 0 ? '+' + l.offset : l.offset}${l.expiry_offset ? ` +${l.expiry_offset}exp` : ''}`)))))));
  }
  legsHost = h('div'); settingsHost = h('div', { style: { marginTop: '14px' } });
  previewHost = h('div', { class: 'card' });
  const runCard = h('div', { class: 'card', style: { marginTop: '14px' } },
    h('div', { class: 'row', style: { justifyContent: 'space-between' } },
      h('div', {}, h('b', {}, 'Ready?'), h('div', { class: 'hint' }, `${S.settings.start} → ${S.settings.end}`)),
      h('button', { class: 'btn primary', disabled: !!S.running, on: { click: () => ctx.startRun() } }, h('span', { html: icons.play }), 'Run backtest')));
  container.append(h('div', { class: 'grid g-main' }, h('div', {}, typeCard, legsHost, h('div', { style: { marginTop: '18px' } }, h('h3', { style: { margin: '0 0 10px', color: 'var(--muted)', fontSize: '12px', letterSpacing: '.09em', textTransform: 'uppercase' } }, 'Backtest settings'), settingsHost)),
    h('div', { class: 'sticky' }, previewHost, runCard)));
  renderLegs(); renderSettings(); paintPreview();
  loadPreview();
}
