import { useState } from 'react'
import { Ship } from '../types'
import { navStatusColor, navStatusLabel, flightModeColor, etaStr, fmtCr, condColor } from '../utils'

interface Props { ship: Ship; onClose: () => void }

function CondBar({ v }: { v: number }) {
  const pct = Math.round(v * 100)
  return (
    <div style={{ display:'flex', alignItems:'center', gap:6 }}>
      <div style={{ width:70, height:5, background:'var(--muted)', borderRadius:3 }}>
        <div style={{ width:`${pct}%`, height:5, background:condColor(v), borderRadius:3 }} />
      </div>
      <span style={{ color:condColor(v), fontSize:11 }}>{pct}%</span>
    </div>
  )
}

function Row({ l, v }: { l: string; v: React.ReactNode }) {
  return (
    <div style={{ display:'flex', justifyContent:'space-between', marginBottom:4, fontSize:12 }}>
      <span style={{ color:'var(--dim)' }}>{l}</span><span>{v}</span>
    </div>
  )
}

const TABS = ['Overview', 'Cargo', 'Mounts & Modules']

export default function ShipModal({ ship, onClose }: Props) {
  const [tab, setTab] = useState('Overview')
  const nav   = ship.nav   || {} as any
  const cargo = ship.cargo || { units:0, capacity:0, inventory:[] }
  const fuel  = ship.fuel  || { current:0, capacity:1 }
  const cd    = ship.cooldown?.remainingSeconds || 0
  const route = nav.route || {}

  const fuelPct = fuel.capacity > 0 ? fuel.current / fuel.capacity : 0
  const fuelColor = fuelPct > 0.5 ? 'var(--green)' : fuelPct > 0.25 ? 'var(--yellow)' : 'var(--red)'

  return (
    <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal-box" style={{ maxWidth:660 }}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start' }}>
          <div className="modal-title">{ship.symbol} — Ship Detail</div>
          <button className="btn-default" onClick={onClose}>✕ Close</button>
        </div>

        {/* Sub-tabs */}
        <div style={{ display:'flex', borderBottom:'1px solid var(--border)', marginBottom:12 }}>
          {TABS.map(t => (
            <button key={t} className={`tab-btn ${tab===t?'active':''}`} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>

        {tab === 'Overview' && (
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:20 }}>
            <div>
              <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, marginBottom:8 }}>NAVIGATION</div>
              <Row l="Status"   v={<span style={{ color:navStatusColor(nav.status) }}>{navStatusLabel(nav.status)}</span>} />
              <Row l="Mode"     v={<span style={{ color: nav.status==='IN_TRANSIT' ? flightModeColor(nav.flightMode) : 'var(--dim)' }}>{nav.flightMode}</span>} />
              <Row l="Location" v={<span style={{ color:'var(--yellow)' }}>{nav.waypointSymbol}</span>} />
              {nav.status === 'IN_TRANSIT' && <>
                <Row l="From"  v={<span style={{ color:'var(--dim)' }}>{route.origin?.symbol}</span>} />
                <Row l="To"    v={<span style={{ color:'var(--yellow)' }}>{route.destination?.symbol}</span>} />
                <Row l="ETA"   v={<span style={{ color:'var(--green)' }}>{etaStr(route.arrival)}</span>} />
              </>}
              {cd > 0 && <Row l="Cooldown" v={<span style={{ color:'var(--yellow)' }}>{cd}s</span>} />}

              <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, margin:'12px 0 8px' }}>FUEL</div>
              <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                <div style={{ flex:1, height:8, background:'var(--muted)', borderRadius:4 }}>
                  <div style={{ width:`${fuelPct*100}%`, height:8, background:fuelColor, borderRadius:4 }} />
                </div>
                <span style={{ fontSize:11 }}>{fuel.current}/{fuel.capacity}</span>
              </div>

              <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, margin:'12px 0 8px' }}>CREW</div>
              <Row l="Current/Cap" v={`${ship.crew?.current ?? 0} / ${ship.crew?.capacity ?? 0}`} />
              <Row l="Morale"      v={<span style={{ color:(ship.crew?.morale ?? 0)>50?'var(--green)':'var(--yellow)' }}>{ship.crew?.morale ?? 0}%</span>} />
            </div>
            <div>
              <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, marginBottom:8 }}>COMPONENTS</div>
              <Row l="Frame"   v={<CondBar v={ship.frame?.condition ?? 1} />} />
              <Row l="Reactor" v={<CondBar v={ship.reactor?.condition ?? 1} />} />
              <Row l="Engine"  v={<CondBar v={ship.engine?.condition ?? 1} />} />
              <Row l="Speed"   v={ship.engine?.speed ?? '—'} />

              <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, margin:'12px 0 8px' }}>
                CARGO [{cargo.units}/{cargo.capacity}]
              </div>
              {cargo.inventory.length > 0 ? (
                cargo.inventory.map(item => (
                  <div key={item.symbol} style={{ display:'flex', justifyContent:'space-between', fontSize:12, marginBottom:2 }}>
                    <span style={{ color:'var(--green)' }}>{item.symbol}</span>
                    <span>{fmtCr(item.units)}u</span>
                  </div>
                ))
              ) : <span style={{ color:'var(--dim)', fontSize:12 }}>Empty</span>}
            </div>
          </div>
        )}

        {tab === 'Cargo' && (
          <table className="data-table">
            <thead><tr><th>Good</th><th style={{ textAlign:'right' }}>Units</th><th>Name</th></tr></thead>
            <tbody>
              {cargo.inventory.length > 0
                ? cargo.inventory.map(item => (
                    <tr key={item.symbol}>
                      <td style={{ color:'var(--green)', fontWeight:700 }}>{item.symbol}</td>
                      <td style={{ textAlign:'right' }}>{fmtCr(item.units)}</td>
                      <td style={{ color:'var(--dim)' }}>{item.name || '—'}</td>
                    </tr>
                  ))
                : <tr><td colSpan={3} style={{ color:'var(--dim)' }}>Empty</td></tr>
              }
            </tbody>
          </table>
        )}

        {tab === 'Mounts & Modules' && (
          <table className="data-table">
            <thead><tr><th>Symbol</th><th>Name</th><th>Str</th><th>Type</th></tr></thead>
            <tbody>
              {(ship.mounts || []).map(m => (
                <tr key={m.symbol}>
                  <td style={{ color:'var(--cyan)' }}>{m.symbol}</td>
                  <td>{m.name}</td>
                  <td style={{ textAlign:'right' }}>{m.strength ?? '—'}</td>
                  <td style={{ color:'var(--dim)' }}>mount</td>
                </tr>
              ))}
              {(ship.modules || []).map(mod => (
                <tr key={mod.symbol}>
                  <td style={{ color:'var(--yellow)' }}>{mod.symbol}</td>
                  <td>{mod.name}</td>
                  <td>—</td>
                  <td style={{ color:'var(--dim)' }}>module</td>
                </tr>
              ))}
              {!ship.mounts?.length && !ship.modules?.length &&
                <tr><td colSpan={4} style={{ color:'var(--dim)' }}>None</td></tr>
              }
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
