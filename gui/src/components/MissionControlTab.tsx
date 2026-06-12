import { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import { Agent, Ship, Contract, BotLog, CPH } from '../types'
import { shipIcon, shipRole, navStatusColor, navStatusLabel, flightModeColor, fmtCr, etaStr, deadlineStr } from '../utils'

function MiniBar({ cur, cap, width = 60 }: { cur: number; cap: number; width?: number }) {
  const pct = cap > 0 ? cur / cap : 0
  const c = pct > 0.85 ? 'var(--red)' : pct > 0.6 ? 'var(--yellow)' : 'var(--green)'
  return (
    <div style={{ display:'flex', alignItems:'center', gap:4 }}>
      <div style={{ width, height:5, background:'var(--muted)', borderRadius:3 }}>
        <div style={{ width:`${pct*100}%`, height:5, background:c, borderRadius:3 }} />
      </div>
      <span style={{ color:c, fontSize:10, minWidth:36 }}>{cur}/{cap}</span>
    </div>
  )
}

export default function MissionControlTab() {
  const [agent,     setAgent]     = useState<Agent|null>(null)
  const [ships,     setShips]     = useState<Ship[]>([])
  const [contracts, setContracts] = useState<Contract[]>([])
  const [available, setAvailable] = useState<any[]>([])
  const [cph,       setCph]       = useState<CPH>({ cph_1h:0, cph_10m:0 })
  const [logs,      setLogs]      = useState<BotLog[]>([])
  const [yields20m, setYields20m] = useState<{trade_symbol:string;total_units:number}[]>([])
  const [lastUpdate,setLastUpdate]= useState('')
  const [error,     setError]     = useState<string|null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  async function refreshApi() {
    try {
      const [a, s, cphData] = await Promise.all([api.agent(), api.ships(), api.cph()])
      setAgent(a); setShips(s); setCph(cphData)
      setLastUpdate(new Date().toTimeString().slice(0,8))
      setError(null)
    } catch (e) { setError(String(e)) }
  }

  async function refreshDb() {
    try {
      const [l, cs, y] = await Promise.all([api.logs(120), api.contracts(), api.yields('20m')])
      setLogs(l)
      setContracts(cs.filter((c: Contract) => c.accepted && !c.fulfilled))
      setAvailable(cs.filter((c: Contract) => !c.accepted && !c.fulfilled))
      setYields20m(y)
    } catch {}
  }

  useEffect(() => {
    refreshApi(); refreshDb()
    const t1 = setInterval(refreshApi, 10000)
    const t2 = setInterval(refreshDb,   5000)
    return () => { clearInterval(t1); clearInterval(t2) }
  }, [])

  // Auto-scroll logs to bottom
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs])

  const cphColor = cph.cph_1h >= 0 ? 'var(--green)' : 'var(--red)'
  const cph10Color = (cph.cph_10m*6) >= 0 ? 'var(--green)' : 'var(--red)'

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      {/* Agent bar */}
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0, fontSize:12, display:'flex', gap:24, alignItems:'center', flexWrap:'wrap' }}>
        <span style={{ color:'var(--cyan)', fontWeight:700 }}>{agent?.symbol || '…'}</span>
        <span>Credits: <span style={{ color:'var(--green)', fontWeight:700 }}>{fmtCr(agent?.credits)} cr</span></span>
        <span style={{ color:'var(--dim)' }}>Ships: {agent?.shipCount ?? '—'}</span>
        <span>CPH(1h): <span style={{ color:cphColor }}>{cph.cph_1h >= 0 ? '+' : ''}{fmtCr(cph.cph_1h)} cr</span></span>
        <span>CPH→hr(10m): <span style={{ color:cph10Color }}>{cph.cph_10m*6 >= 0 ? '+' : ''}{fmtCr(cph.cph_10m*6)} cr</span></span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
      </div>

      <div style={{ flex:1, display:'flex', overflow:'hidden' }}>
        {/* LEFT: logs */}
        <div style={{ flex:'0 0 420px', borderRight:'1px solid var(--border)', display:'flex', flexDirection:'column' }}>
          <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0, color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em' }}>
            📡 LIVE BOT LOGS
          </div>
          <div ref={logRef} style={{ flex:1, overflow:'auto', padding:'6px 10px', fontFamily:'monospace', fontSize:11 }}>
            {logs.length === 0
              ? <div style={{ color:'var(--dim)' }}>No log entries yet — start play.py</div>
              : [...logs].reverse().map((l, i) => {
                  const dt = new Date(l.timestamp*1000).toTimeString().slice(0,8)
                  return (
                    <div key={i} style={{ marginBottom:1, lineHeight:1.5 }}>
                      <span style={{ color:'var(--dim)', marginRight:6 }}>{dt}</span>
                      <span dangerouslySetInnerHTML={{ __html: colorizeLog(l.message) }} />
                    </div>
                  )
                })
            }
          </div>
        </div>

        {/* RIGHT: fleet + bottom panels */}
        <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          {/* Fleet table */}
          <div style={{ flex:'0 0 auto', borderBottom:'1px solid var(--border)' }}>
            <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
              🚀 FLEET STATUS
            </div>
            <div style={{ overflow:'auto', maxHeight:240 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Ship</th><th>Role</th><th>Status</th><th>From</th><th>→ To</th>
                    <th>Mode</th><th>Fuel</th><th>Cargo</th><th>ETA / CD</th>
                  </tr>
                </thead>
                <tbody>
                  {ships.map(ship => {
                    const nav    = ship.nav || {} as any
                    const cargo  = ship.cargo || { units:0, capacity:1 }
                    const fuel   = ship.fuel  || { current:0, capacity:1 }
                    const cd     = ship.cooldown?.remainingSeconds || 0
                    const route  = nav.route || {}
                    const status = nav.status || ''
                    const mode   = nav.flightMode || 'CRUISE'
                    const dep    = status === 'IN_TRANSIT' ? (route.origin?.symbol?.split('-').pop() || '—') : '—'
                    const dst    = status === 'IN_TRANSIT' ? (route.destination?.symbol?.split('-').pop() || '—') : nav.waypointSymbol?.split('-').pop() || '—'
                    const eta    = status === 'IN_TRANSIT' ? etaStr(route.arrival) : cd > 0 ? `cd:${cd}s` : '—'
                    const role   = shipRole(ship)
                    const short  = ship.symbol.split('-').pop()

                    return (
                      <tr key={ship.symbol}>
                        <td><span style={{ marginRight:4 }}>{shipIcon(ship)}</span><span style={{ color:'var(--cyan)' }}>…-{short}</span></td>
                        <td><span style={{ color:role.color }}>{role.label}</span></td>
                        <td><span style={{ color:navStatusColor(status) }}>{navStatusLabel(status)}</span></td>
                        <td style={{ color:'var(--dim)' }}>{dep}</td>
                        <td style={{ color:'var(--yellow)' }}>{dst}</td>
                        <td><span style={{ color: status === 'IN_TRANSIT' ? flightModeColor(mode) : 'var(--dim)' }}>
                          {status === 'IN_TRANSIT' ? mode : '—'}
                        </span></td>
                        <td><MiniBar cur={fuel.current} cap={fuel.capacity} /></td>
                        <td><MiniBar cur={cargo.units} cap={cargo.capacity} /></td>
                        <td style={{ color: status === 'IN_TRANSIT' ? 'var(--cyan)' : 'var(--dim)' }}>{eta}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Bottom: contracts + stats */}
          <div style={{ flex:1, display:'flex', overflow:'hidden' }}>
            {/* Active contracts */}
            <div style={{ flex:1, borderRight:'1px solid var(--border)', display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--green)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
                📋 CONTRACTS
              </div>
              <div style={{ flex:1, overflow:'auto', padding:'8px 10px', fontSize:12 }}>
                {contracts.length > 0 ? (
                  contracts.map(c => (
                    <div key={c.id} style={{ marginBottom:10 }}>
                      {c.deliver.map(d => {
                        const pct  = d.units_required > 0 ? Math.round(d.units_fulfilled/d.units_required*100) : 0
                        const barColor = pct >= 80 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)'
                        return (
                          <div key={d.trade_symbol} style={{ marginBottom:4 }}>
                            <div style={{ display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                              <span style={{ color:barColor }}>{d.trade_symbol}</span>
                              <span style={{ color:'var(--dim)' }}>{fmtCr(d.units_fulfilled)}/{fmtCr(d.units_required)} ({pct}%)</span>
                            </div>
                            <div className="prog-track"><div className="prog-fill" style={{ width:`${pct}%`, backgroundColor:barColor }} /></div>
                            <div style={{ fontSize:11, color:'var(--dim)', marginTop:2 }}>
                              → {d.destination_symbol}  &nbsp;Reward: <span style={{ color:'var(--green)' }}>{fmtCr(c.on_fulfilled)} cr</span>
                              &nbsp; Deadline: {deadlineStr(c.deadline)}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  ))
                ) : (
                  <div style={{ color:'var(--dim)' }}>No active contracts</div>
                )}
                {available.length > 0 && (
                  <>
                    <div style={{ color:'var(--yellow)', fontWeight:700, marginBottom:4, marginTop:8, fontSize:11 }}>AVAILABLE</div>
                    {available.map(c => (
                      <div key={c.id} style={{ marginBottom:3, fontSize:11, color:'var(--dim)' }}>
                        {c.type}  {c.deliver[0]?.trade_symbol || '?'} ×{c.deliver[0]?.units_required || '?'}
                        &nbsp;→ <span style={{ color:'var(--green)' }}>{fmtCr(c.on_fulfilled)} cr</span>
                        &nbsp;exp: {deadlineStr(c.expiration)}
                      </div>
                    ))}
                  </>
                )}
              </div>
            </div>

            {/* Stats + yields */}
            <div style={{ flex:'0 0 240px', display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--yellow)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
                📊 STATS &amp; YIELDS
              </div>
              <div style={{ flex:1, overflow:'auto', padding:'8px 10px', fontSize:12 }}>
                <div style={{ marginBottom:8 }}>
                  <div style={{ color:'var(--yellow)', fontWeight:700, marginBottom:4, fontSize:11 }}>CREDITS FLOW</div>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                    <span style={{ color:'var(--dim)' }}>CPH (1h)</span>
                    <span style={{ color:cphColor }}>{cph.cph_1h >= 0 ? '+' : ''}{fmtCr(cph.cph_1h)} cr</span>
                  </div>
                  <div style={{ display:'flex', justifyContent:'space-between' }}>
                    <span style={{ color:'var(--dim)' }}>CPH (10m→)</span>
                    <span style={{ color:cph10Color }}>{cph.cph_10m*6 >= 0 ? '+' : ''}{fmtCr(cph.cph_10m*6)} cr</span>
                  </div>
                </div>
                <div style={{ color:'var(--yellow)', fontWeight:700, marginBottom:6, fontSize:11 }}>MINING YIELDS (20m)</div>
                {yields20m.length > 0 ? yields20m.map(y => (
                  <div key={y.trade_symbol} style={{ display:'flex', justifyContent:'space-between', marginBottom:3 }}>
                    <span style={{ color:'var(--green)' }}>{y.trade_symbol}</span>
                    <span>{fmtCr(y.total_units)}u</span>
                  </div>
                )) : <div style={{ color:'var(--dim)' }}>No yields yet</div>}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="status-bar">
        Updated: {lastUpdate}  •  API refresh 3s  •  Logs refresh 2s  •  Keys 1–9 switch tabs
      </div>
    </div>
  )
}

function colorizeLog(msg: string): string {
  return msg
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\b(ERROR|FAIL|failed|error)\b/gi, '<span style="color:var(--red)">$1</span>')
    .replace(/\b(WARN|warning)\b/gi, '<span style="color:var(--yellow)">$1</span>')
    .replace(/\b(OK|success|done|✓|delivered)\b/gi, '<span style="color:var(--green)">$1</span>')
    .replace(/\b(mining|survey|extract)\b/gi, '<span style="color:var(--cyan)">$1</span>')
    .replace(/\+[\d,]+\s*cr/g, s => `<span style="color:var(--green)">${s}</span>`)
    .replace(/-[\d,]+\s*cr/g,  s => `<span style="color:var(--red)">${s}</span>`)
}
