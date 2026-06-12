export function tsAgo(ts: number|string|null|undefined): string {
  if (ts == null || ts === '') return 'never'
  const ms = typeof ts === 'string' ? new Date(ts).getTime() : ts * 1000
  const ago = (Date.now() - ms) / 1000
  if (ago < 60) return `${Math.floor(ago)}s`
  if (ago < 3600) return `${Math.floor(ago/60)}m`
  return `${Math.floor(ago/3600)}h`
}

export function deadlineStr(iso: string|null|undefined): string {
  if (!iso) return '—'
  const dt = new Date(iso)
  const delta = (dt.getTime() - Date.now()) / 1000
  const hours = Math.floor(delta / 3600)
  if (hours < 0) return 'EXPIRED'
  if (hours < 24) return `${hours}h left`
  return `${Math.floor(hours/24)}d ${hours%24}h`
}

export function etaStr(arrival: string|null|undefined): string {
  if (!arrival) return '—'
  const secs = Math.floor((new Date(arrival).getTime() - Date.now()) / 1000)
  if (secs <= 0) return 'arriving'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function shipIcon(ship: any): string {
  const mounts: string[] = (ship.mounts || []).map((m: any) => m.symbol || '')
  const status = ship.nav?.status || ''
  if (status === 'IN_TRANSIT') return '🚀'
  if (mounts.some((m: string) => m.includes('MINING_LASER'))) return '⛏️'
  if (mounts.some((m: string) => m.includes('SURVEYING'))) return '🔭'
  if (status === 'DOCKED') return '⚓'
  return '🛸'
}

export function shipRole(ship: any): { label: string; color: string } {
  const mounts: string[] = (ship.mounts || []).map((m: any) => m.symbol || '')
  if (mounts.some((m: string) => m.includes('SURVEYING'))) return { label: 'surveyor', color: 'var(--magenta)' }
  if (mounts.some((m: string) => m.includes('MINING_LASER'))) return { label: 'miner', color: 'var(--green)' }
  return { label: 'hauler/cmd', color: 'var(--yellow)' }
}

export function shipActivity(ship: any): string {
  const nav = ship.nav || {}
  const status = nav.status || ''
  const cd = ship.cooldown?.remainingSeconds || 0
  const mounts: string[] = (ship.mounts || []).map((m: any) => m.symbol || '')
  if (status === 'IN_TRANSIT') {
    const dest = nav.route?.destination?.symbol || '?'
    return `→ ${dest} (${etaStr(nav.route?.arrival)})`
  }
  if (cd > 0) {
    if (mounts.some((m: string) => m.includes('MINING_LASER'))) return `Mining  cd:${cd}s`
    if (mounts.some((m: string) => m.includes('SURVEYING'))) return `Surveying  cd:${cd}s`
    return `Cooling  ${cd}s`
  }
  if (status === 'DOCKED') return 'Docked'
  return 'Idle'
}

export function navStatusColor(s: string) {
  return s === 'IN_TRANSIT' ? 'var(--yellow)' : s === 'DOCKED' ? 'var(--green)' : 'var(--cyan)'
}
export function navStatusLabel(s: string) {
  return s === 'IN_TRANSIT' ? 'TRANSIT' : s === 'DOCKED' ? 'DOCKED' : 'ORBIT'
}
export function flightModeColor(m: string) {
  return m === 'DRIFT' ? 'var(--red)' : m === 'BURN' ? '#ff8800' : m === 'STEALTH' ? 'var(--magenta)' : 'var(--cyan)'
}
export function supplyColor(s: string|null|undefined) {
  if (!s) return 'var(--text)'
  return ['ABUNDANT','HIGH'].includes(s) ? 'var(--green)' : ['LIMITED','SCARCE'].includes(s) ? 'var(--red)' : 'var(--yellow)'
}
export function condColor(v: number) {
  return v > 0.8 ? 'var(--green)' : v > 0.5 ? 'var(--yellow)' : 'var(--red)'
}
export function fmtCr(n: number|null|undefined) { return (n ?? 0).toLocaleString() }
export function fmtDt(ts: number|null|undefined) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${(d.getMonth()+1).toString().padStart(2,'0')}/${d.getDate().toString().padStart(2,'0')} ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`
}
