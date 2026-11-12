// JSON API client. Non-GET requests carry X-Lab-Client (the server rejects cross-site writes without it).
export class ApiError extends Error {}

async function req(method, path, body) {
  const opt = { method, headers: { 'X-Lab-Client': '1' } };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new ApiError('Cannot reach the app server. Is it still running?'); }
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* non-JSON */ }
  if (!r.ok) throw new ApiError((json && json.error) || `HTTP ${r.status}`);
  return json;
}

export const api = {
  config: () => req('GET', '/api/config'),
  data: (start, end, step) => req('GET', `/api/data?start=${start}&end=${end}&step=${step}`),
  preview: (p) => req('POST', '/api/preview', p),
  startRun: (p) => req('POST', '/api/runs', p),
  replay: (p) => req('POST', '/api/replay', p),
  runs: () => req('GET', '/api/runs'),
  run: (id) => req('GET', `/api/runs/${id}`),
  trades: (id, q) => req('GET', `/api/runs/${id}/trades?` + new URLSearchParams(q)),
  explorer: (id, date) => req('GET', `/api/runs/${id}/explorer?date=${date}`),
  niftyTrades: (id, date) => req('GET', `/api/runs/${id}/nifty-trades?date=${date}`),
  niftyBacktestChart: (id) => req('GET', `/api/runs/${id}/nifty-backtest-chart`),
  chartContracts: (year = '', expiry = '') => req('GET', '/api/chart-contracts?' + new URLSearchParams({ year, expiry })),
  marketChart: (q) => req('GET', '/api/market-chart?' + new URLSearchParams(q)),
  rename: (id, name) => req('PATCH', `/api/runs/${id}`, { name }),
  remove: (id) => req('DELETE', `/api/runs/${id}`),
  compare: (ids) => req('POST', '/api/compare', { ids }),
  optimize: (p) => req('POST', '/api/optimize', p),
  repair: (p) => req('POST', '/api/repair', p),
  job: (id) => req('GET', `/api/jobs/${id}`),
  cancel: (id) => req('POST', `/api/jobs/${id}/cancel`, {}),
};

// Poll a job until it finishes; onTick(job) is called on every poll.
export async function waitJob(job, onTick, signal) {
  let j = job;
  while (j.status === 'queued' || j.status === 'running') {
    if (onTick) onTick(j);
    await new Promise((r) => setTimeout(r, 180));
    if (signal && signal.aborted) break;
    j = await api.job(job.id);
  }
  if (onTick) onTick(j);
  return j;
}
