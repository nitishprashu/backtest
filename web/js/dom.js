// Tiny DOM + formatting helpers (no framework).
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  const add = (c) => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.append(c.nodeType ? c : document.createTextNode(String(c)));
  };
  children.forEach(add);
  return el;
}

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

// ---------------------------------------------------------------- formatting
const nIN = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const nIN2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
export const inr = (v, sign = false) => {
  if (!isNum(v)) return '—';
  const s = nIN.format(Math.abs(Math.round(v)));
  return (v < 0 ? '-' : sign && v > 0 ? '+' : '') + '₹' + s;
};
export const inr2 = (v) => (isNum(v) ? (v < 0 ? '-' : '') + '₹' + nIN2.format(Math.abs(v)) : '—');
export const num = (v, d = 2) => (isNum(v) ? v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');
export const pct = (v, d = 1) => (isNum(v) ? v.toFixed(d) + '%' : '—');
export const cls = (v) => (isNum(v) ? (v > 0 ? 'pos' : v < 0 ? 'neg' : '') : '');
export const parseDate = (s) => new Date(s + 'T00:00:00');
export const dShort = (s) => parseDate(s).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' });
export const dLong = (s) => parseDate(s).toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
export const toISO = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
export const minutesToHM = (i) => { const t = 9 * 60 + 15 + i; return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0'); };

// ---------------------------------------------------------------- toast + tooltip
export function toast(msg, kind = '', ms = 4200) {
  const box = document.getElementById('toasts');
  const el = h('div', { class: 'toast ' + kind }, msg);
  box.append(el);
  setTimeout(() => el.remove(), ms);
}
const tipEl = () => document.getElementById('tip');
export function showTip(evt, html) {
  const t = tipEl();
  t.innerHTML = html;
  t.hidden = false;
  const pad = 14, w = t.offsetWidth, hgt = t.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + hgt > innerHeight - 8) y = evt.clientY - hgt - pad;
  t.style.left = x + 'px';
  t.style.top = y + 'px';
}
export const hideTip = () => { tipEl().hidden = true; };
// Tooltips are built as HTML strings, so any text that can come from the user or the data (run names,
// labels) must be escaped. `value` is trusted markup built by the callers from formatted numbers.
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const tipRow = (label, value, color) =>
  `<div class="r"><span style="color:${esc(color) || 'var(--muted)'}">${esc(label)}</span><b>${value}</b></div>`;

// ---------------------------------------------------------------- icons (stroke icons, 24x24)
const svg = (p) => `<svg viewBox="0 0 24 24">${p}</svg>`;
export const icons = {
  strategy: svg('<path d="M4 6h10M4 12h16M4 18h8"/><circle cx="17" cy="6" r="2.2"/><circle cx="15" cy="18" r="2.2"/>'),
  results: svg('<path d="M4 19V5M4 19h16"/><path d="M8 15l3-4 3 2 5-6"/>'),
  charts: svg('<path d="M4 19V5M4 19h16"/><path d="M7 15l4-5 3 3 5-7"/>'),
  explorer: svg('<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.2-4.2"/><path d="M8.5 11h5M11 8.5v5"/>'),
  optimizer: svg('<rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="14" width="6" height="6" rx="1.5"/>'),
  runs: svg('<path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="8"/>'),
  data: svg('<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>'),
  play: svg('<path d="M7 5l12 7-12 7z" fill="currentColor"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  trash: svg('<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>'),
  copy: svg('<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>'),
  download: svg('<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>'),
  save: svg('<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>'),
  upload: svg('<path d="M12 16V5M7 9l5-5 5 5M5 20h14"/>'),
  left: svg('<path d="M15 5l-7 7 7 7"/>'),
  right: svg('<path d="M9 5l7 7-7 7"/>'),
  stop: svg('<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>'),
};
