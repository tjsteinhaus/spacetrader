import { useState, useEffect } from 'react'
import { api } from '../api'
import { Waypoint, WaypointAnalysis } from '../types'
import { fmtCr } from '../utils'

export default function UniverseTab() {
  const [waypoints,  setWaypoints]  = useState<Waypoint[]>([])
  const [filter,     setFilter]     = useState('')
  const [selected,   setSelected]   = useState<string|null>(null)
  const [analysis,   setAnalysis]   = useState<WaypointAnalysis|null>(null)
  const [loadingAna, setLoadingAna] = useState(false)
  const [error,      setError]      = useState<string|null>(null)

  useEffect(() => {
    const t = setTimeout(() => {
      api.waypoints(filter).then(setWaypoints).catch(e => setError(String(e)))
    }, 300)
    return () => clearTimeout(t)
  }, [filter])

  async function selectWaypoint(sym: string) {
    setSelected(sym)
    setLoadingAna(true)
    try { setAnalysis(await api.waypointAnalysis(sym)) }
    catch { setAnalysis(null) }
    finally { setLoadingAna(false) }
  }

  return (
    <div style={{ flex:1, display:'flex', overflow:'hidden' }}>
      {/* Left: waypoint table */}
      <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden', borderRight:'1px solid var(--border)' }}>
        <div style={{ padding:'6px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0, display:'flex', gap:8, alignItems:'center' }}>
          <span style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em' }}>🌌 UNIVERSE</span>
          <input
            type="text" placeholder="Filter by type, trait, symbol…"
            value={filter} onChange={e => setFilter(e.target.value)}
            style={{ flex:1, background:'var(--muted)', border:'1px solid var(--border)', color:'var(--text)', padding:'3px 8px', borderRadius:3, fontSize:12 }}
          />
          {error && <span style={{ color:'var(--red)', fontSize:11 }}>⚠ {error}</span>}
        </div>
        <div style={{ flex:1, overflow:'auto' }}>
          <table className="data-table">
            <thead>
              <tr><th>Waypoint</th><th>Type</th><th>X</th><th>Y</th><th>Traits</th><th>Orbits</th></tr>
            </thead>
            <tbody>
              {waypoints.map(w => (
                <tr key={w.symbol} onClick={() => selectWaypoint(w.symbol)} style={{ cursor:'pointer', background: selected === w.symbol ? 'var(--muted)' : 'transparent' }}>
                  <td style={{ color:'var(--cyan)', fontWeight:700 }}>{w.symbol.split('-').pop()}</td>
                  <td style={{ color:'var(--yellow)' }}>{w.type}</td>
                  <td style={{ textAlign:'right', color:'var(--dim)', fontSize:11 }}>{w.x}</td>
                  <td style={{ textAlign:'right', color:'var(--dim)', fontSize:11 }}>{w.y}</td>
                  <td style={{ fontSize:11, color:'var(--dim)', maxWidth:200, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                    {(w.traits || []).map(t => t.symbol || t).join(', ') || '—'}
                  </td>
                  <td style={{ fontSize:11, color:'var(--dim)' }}>{w.orbits || '—'}</td>
                </tr>
              ))}
              {waypoints.length === 0 && (
                <tr><td colSpan={6} style={{ color:'var(--dim)', textAlign:'center' }}>No waypoints</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="status-bar">{waypoints.length} waypoints  •  Click row for analysis</div>
      </div>

      {/* Right: analysis panel */}
      <div style={{ flex:'0 0 340px', display:'flex', flexDirection:'column', overflow:'hidden' }}>
        <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
          🔍 ANALYSIS
        </div>
        <div style={{ flex:1, overflow:'auto', padding:'10px 12px', fontSize:12 }}>
          {!selected && <div style={{ color:'var(--dim)' }}>Select a waypoint on the left</div>}
          {loadingAna && <div style={{ color:'var(--dim)' }}>Loading…</div>}
          {selected && !loadingAna && !analysis && <div style={{ color:'var(--dim)' }}>No analysis data</div>}
          {analysis && !loadingAna && (
            <>
              <div style={{ color:'var(--cyan)', fontWeight:700, marginBottom:8 }}>{selected}</div>
              {analysis.market && (
                <div style={{ marginBottom:10 }}>
                  <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:4 }}>MARKET</div>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                    <span style={{ color:'var(--dim)' }}>Exports</span>
                    <span style={{ fontSize:11 }}>{analysis.market.exports?.join(', ') || '—'}</span>
                  </div>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                    <span style={{ color:'var(--dim)' }}>Imports</span>
                    <span style={{ fontSize:11 }}>{analysis.market.imports?.join(', ') || '—'}</span>
                  </div>
                  <div style={{ display:'flex', justifyContent:'space-between' }}>
                    <span style={{ color:'var(--dim)' }}>Exchange</span>
                    <span style={{ fontSize:11 }}>{analysis.market.exchange?.join(', ') || '—'}</span>
                  </div>
                </div>
              )}
              {analysis.shipyard && (
                <div style={{ marginBottom:10 }}>
                  <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:4 }}>SHIPYARD</div>
                  <div style={{ fontSize:11, color:'var(--dim)' }}>{analysis.shipyard.ship_types?.join(', ') || 'Details unavailable'}</div>
                </div>
              )}
              {analysis.jump_gate && (
                <div style={{ marginBottom:10 }}>
                  <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:4 }}>JUMP GATE</div>
                  <div style={{ fontSize:11, color:'var(--dim)' }}>Connected systems: {analysis.jump_gate.connections?.length || 0}</div>
                </div>
              )}
              {analysis.asteroids && (
                <div style={{ marginBottom:10 }}>
                  <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:4 }}>MINING POTENTIAL</div>
                  {analysis.asteroids.deposits?.slice(0,6).map(d => (
                    <div key={d.symbol} style={{ display:'flex', justifyContent:'space-between', fontSize:11, marginBottom:2 }}>
                      <span>{d.symbol}</span>
                      <span style={{ color:'var(--dim)' }}>
                        {d.count ? `${d.count}×` : ''}
                        {d.avg_yield ? ` ~${Math.round(d.avg_yield)}u` : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {analysis.traits && analysis.traits.length > 0 && (
                <div>
                  <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:4 }}>TRAITS</div>
                  {analysis.traits.map((t: any) => (
                    <div key={t.symbol} style={{ fontSize:11, marginBottom:3 }}>
                      <span style={{ color:'var(--cyan)' }}>{t.symbol}</span>
                      {t.description && <span style={{ color:'var(--dim)', marginLeft:6, fontSize:10 }}>{t.description}</span>}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
