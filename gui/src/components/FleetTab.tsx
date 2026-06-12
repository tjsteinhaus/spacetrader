import { useState, useEffect } from 'react'
import { api } from '../api'
import { Ship } from '../types'
import { shipIcon, shipRole, navStatusColor, navStatusLabel, flightModeColor, etaStr, fmtCr } from '../utils'
import ShipModal from './ShipModal'

function FuelBar({ cur, cap }: { cur: number; cap: number }) {
  const pct = cap > 0 ? cur / cap : 0
  const c = pct > 0.5 ? 'var(--green)' : pct > 0.25 ? 'var(--yellow)' : 'var(--red)'
  return (
    <div style={{ display:'flex', alignItems:'center', gap:4 }}>
      <div style={{ width:55, height:5, background:'var(--muted)', borderRadius:3 }}>
        <div style={{ width:`${pct*100}%`, height:5, background:c, borderRadius:3 }} />
      </div>
      <span style={{ fontSize:10, color:c }}>{cur}</span>
    </div>
  )
}
function CargoBar({ cur, cap }: { cur: number; cap: number }) {
  const pct = cap > 0 ? cur / cap : 0
  const c = pct > 0.85 ? 'var(--red)' : pct > 0.6 ? 'var(--yellow)' : 'var(--green)'
  return (
    <div style={{ display:'flex', alignItems:'center', gap:4 }}>
      <div style={{ width:55, height:5, background:'var(--muted)', borderRadius:3 }}>
        <div style={{ width:`${pct*100}%`, height:5, background:c, borderRadius:3 }} />
      </div>
      <span style={{ fontSize:10, color:'var(--dim)' }}>{cur}/{cap}</span>
    </div>
  )
}

export default function FleetTab() {
  const [ships,   setShips]   = useState<Ship[]>([])
  const [selected, setSelected] = useState<Ship|null>(null)
  const [error, setError] = useState<string|null>(null)

  useEffect(() => {
    async function load() {
      try { setShips(await api.ships()); setError(null) }
      catch (e) { setError(String(e)) }
    }
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0, display:'flex', justifyContent:'space-between' }}>
        <span>🚀 FLEET ({ships.length} ships)</span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
      </div>
      <div style={{ flex:1, overflow:'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Ship</th><th>Frame</th><th>Role</th><th>Status</th>
              <th>Location</th><th>From</th><th>→ Dest</th><th>Mode</th>
              <th>Fuel</th><th>Cargo</th><th>ETA</th><th>Cooldown</th>
            </tr>
          </thead>
          <tbody>
            {ships.map(ship => {
              const nav    = ship.nav   || {} as any
              const cargo  = ship.cargo || { units:0, capacity:0 }
              const fuel   = ship.fuel  || { current:0, capacity:1 }
              const cd     = ship.cooldown?.remainingSeconds || 0
              const route  = nav.route || {}
              const status = nav.status || ''
              const mode   = nav.flightMode || ''
              const role   = shipRole(ship)
              const short  = ship.symbol.split('-').pop()
              const dep    = status === 'IN_TRANSIT' ? (route.origin?.symbol?.split('-').pop() || '—') : '—'
              const dst    = status === 'IN_TRANSIT' ? (route.destination?.symbol?.split('-').pop() || '—') : '—'
              const eta    = status === 'IN_TRANSIT' ? etaStr(route.arrival) : '—'

              return (
                <tr key={ship.symbol} onClick={() => setSelected(ship)} style={{ cursor:'pointer' }}>
                  <td>
                    <span style={{ marginRight:4 }}>{shipIcon(ship)}</span>
                    <span style={{ color:'var(--cyan)' }}>…-{short}</span>
                  </td>
                  <td style={{ color:'var(--dim)', fontSize:11 }}>{ship.frame?.symbol?.replace('FRAME_','') || '—'}</td>
                  <td><span style={{ color:role.color }}>{role.label}</span></td>
                  <td><span style={{ color:navStatusColor(status) }}>{navStatusLabel(status)}</span></td>
                  <td style={{ fontSize:11, color:'var(--yellow)' }}>{nav.waypointSymbol?.split('-').pop()}</td>
                  <td style={{ color:'var(--dim)' }}>{dep}</td>
                  <td style={{ color: dst !== '—' ? 'var(--yellow)' : 'var(--dim)' }}>{dst}</td>
                  <td><span style={{ color: status === 'IN_TRANSIT' ? flightModeColor(mode) : 'var(--dim)', fontSize:11 }}>
                    {status === 'IN_TRANSIT' ? mode : '—'}
                  </span></td>
                  <td><FuelBar cur={fuel.current} cap={fuel.capacity} /></td>
                  <td><CargoBar cur={cargo.units} cap={cargo.capacity} /></td>
                  <td style={{ color:'var(--cyan)', fontSize:11 }}>{eta}</td>
                  <td style={{ color: cd > 0 ? 'var(--yellow)' : 'var(--dim)', fontSize:11 }}>
                    {cd > 0 ? `${cd}s` : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {ships.length === 0 && <div style={{ padding:'20px', color:'var(--dim)', textAlign:'center' }}>No ships — is the API server running?</div>}
      </div>
      {selected && <ShipModal ship={selected} onClose={() => setSelected(null)} />}
      <div className="status-bar">Click row for ship detail  •  Refreshes every 4s  •  {ships.length} ships</div>
    </div>
  )
}
