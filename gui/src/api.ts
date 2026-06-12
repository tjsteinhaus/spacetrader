const BASE = '/api'

async function get<T = any>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`)
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}
async function post<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) { const msg = await r.text(); throw new Error(msg || `${r.status}`) }
  return r.json()
}

export const api = {
  agent:            () => get('/agent'),
  ships:            () => get('/ships'),
  cph:              () => get('/cph'),
  logs:             (limit = 120) => get(`/logs?limit=${limit}`),
  contracts:        () => get('/contracts'),
  transactions:     (limit = 300) => get(`/transactions?limit=${limit}`),
  yields:           (window = '20m') => get(`/yields?window=${encodeURIComponent(window)}`),
  tradeRuns:        () => get('/trade-runs'),
  markets:          () => get('/markets'),
  marketPrices:     (wp: string) => get(`/markets/${encodeURIComponent(wp)}/prices`),
  refreshMarket:    (wp: string) => post(`/markets/${encodeURIComponent(wp)}/refresh`),
  arbitrage:        (min = 50) => get(`/arbitrage?min_margin=${min}`),
  waypoints:        (filter = '') => get(`/waypoints${filter ? `?filter=${encodeURIComponent(filter)}` : ''}`),
  waypointAnalysis: (sym: string) => get(`/waypoints/${encodeURIComponent(sym)}/analysis`),
  surveys:          () => get('/surveys'),
  income:           () => get('/analytics/income'),
  contractHistory:  () => get('/analytics/contracts'),
  sourcing:         (good: string) => get(`/sourcing/${encodeURIComponent(good)}`),
  settings:         () => get('/settings'),
  toggleAutoBuy:    () => post('/settings/auto-buy'),
  setCommandRole:   (role: string) => post(`/settings/command-role/${role}`),
  setShipTargets:   (targets: {type:string;max:number}[]) => post('/settings/ship-targets', { targets }),
}
