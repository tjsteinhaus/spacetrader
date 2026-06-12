import { useState, useEffect } from 'react'
import { api } from '../api'
import { Survey } from '../types'
import { tsAgo } from '../utils'

export default function SurveysTab() {
  const [surveys, setSurveys] = useState<Survey[]>([])
  const [error,   setError]   = useState<string|null>(null)

  useEffect(() => {
    function load() {
      api.surveys().then(setSurveys).catch(e => setError(String(e)))
    }
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])

  function depositCounts(deposits: {symbol:string}[]) {
    const counts: Record<string,number> = {}
    for (const d of deposits) counts[d.symbol] = (counts[d.symbol] || 0) + 1
    return Object.entries(counts).sort((a,b) => b[1]-a[1])
      .map(([s,n]) => `${s}×${n}`).join(', ')
  }

  function sizeColor(size: string) {
    switch (size) {
      case 'LARGE':  return 'var(--green)'
      case 'MEDIUM': return 'var(--yellow)'
      case 'SMALL':  return 'var(--dim)'
      default:       return 'var(--text)'
    }
  }

  function expiresColor(expiration: string) {
    const ms = new Date(expiration).getTime() - Date.now()
    if (ms < 60_000)  return 'var(--red)'
    if (ms < 300_000) return 'var(--yellow)'
    return 'var(--green)'
  }

  const now = Date.now()
  const valid  = surveys.filter(s => new Date(s.expiration).getTime() > now)
  const expired = surveys.filter(s => new Date(s.expiration).getTime() <= now)

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0, display:'flex', justifyContent:'space-between' }}>
        <span>⛏ SURVEYS ({valid.length} valid, {expired.length} expired)</span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
      </div>
      <div style={{ flex:1, overflow:'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Signature</th><th>Waypoint</th><th>Size</th><th>Deposits</th>
              <th style={{ textAlign:'right' }}>Count</th>
              <th>Expires</th><th>Age</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {surveys.map(s => {
              const deps     = s.deposits || []
              const isExpired = new Date(s.expiration).getTime() <= now
              const expStr   = new Date(s.expiration).toTimeString().slice(0,8)
              const age      = tsAgo(s.created_at)

              return (
                <tr key={s.signature} style={{ opacity: isExpired ? 0.45 : 1 }}>
                  <td style={{ fontFamily:'monospace', fontSize:10, color:'var(--cyan)' }}>{s.signature.slice(-12)}</td>
                  <td style={{ fontSize:11, color:'var(--yellow)' }}>{s.waypoint_symbol?.split('-').pop()}</td>
                  <td><span style={{ color:sizeColor(s.size) }}>{s.size}</span></td>
                  <td style={{ fontSize:10, color:'var(--dim)', maxWidth:260, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                    {depositCounts(deps)}
                  </td>
                  <td style={{ textAlign:'right', color:'var(--dim)' }}>{deps.length}</td>
                  <td style={{ fontSize:10, color:expiresColor(s.expiration) }}>{expStr}</td>
                  <td style={{ fontSize:10, color:'var(--dim)' }}>{age}</td>
                  <td>
                    <span style={{ fontSize:10, color: isExpired ? 'var(--red)' : 'var(--green)' }}>
                      {isExpired ? 'expired' : 'valid'}
                    </span>
                  </td>
                </tr>
              )
            })}
            {surveys.length === 0 && (
              <tr><td colSpan={8} style={{ color:'var(--dim)', textAlign:'center' }}>No surveys in database</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="status-bar">
        {valid.length} valid surveys  •  Refreshes every 15s  •  Size: L=Large M=Medium S=Small
      </div>
    </div>
  )
}
