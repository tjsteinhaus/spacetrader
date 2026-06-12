import { useState, useEffect } from 'react'
import { api } from '../api'
import { Contract } from '../types'
import { fmtCr, deadlineStr } from '../utils'
import ContractModal from './ContractModal'

function ProgBar({ cur, req }: { cur: number; req: number }) {
  const pct = req > 0 ? Math.round(cur/req*100) : 0
  const c = pct >= 80 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)'
  return (
    <div>
      <div style={{ display:'flex', justifyContent:'space-between', marginBottom:3, fontSize:11 }}>
        <span style={{ color:c }}>{pct}%</span>
        <span style={{ color:'var(--dim)' }}>{fmtCr(cur)} / {fmtCr(req)}</span>
      </div>
      <div className="prog-track" style={{ height:8 }}>
        <div className="prog-fill" style={{ width:`${pct}%`, backgroundColor:c, height:8 }} />
      </div>
    </div>
  )
}

export default function ContractsTab() {
  const [contracts, setContracts] = useState<Contract[]>([])
  const [selected,  setSelected]  = useState<Contract|null>(null)
  const [error,     setError]     = useState<string|null>(null)

  useEffect(() => {
    async function load() {
      try { setContracts(await api.contracts()); setError(null) }
      catch (e) { setError(String(e)) }
    }
    load()
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [])

  const active    = contracts.filter(c =>  c.accepted && !c.fulfilled)
  const available = contracts.filter(c => !c.accepted && !c.fulfilled)
  const fulfilled = contracts.filter(c =>  c.fulfilled)

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--green)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0, display:'flex', justifyContent:'space-between' }}>
        <span>📋 CONTRACTS</span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
      </div>

      <div style={{ flex:1, overflow:'auto', padding:12 }}>
        {/* Active contracts */}
        {active.length > 0 && (
          <section style={{ marginBottom:20 }}>
            <div style={{ color:'var(--green)', fontWeight:700, fontSize:12, marginBottom:8 }}>
              ACTIVE ({active.length})
            </div>
            {active.map(c => (
              <div key={c.id} className="panel" style={{ marginBottom:12, cursor:'pointer' }} onClick={() => setSelected(c)}>
                <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:8 }}>
                  <div>
                    <span style={{ color:'var(--cyan)', fontWeight:700, fontSize:12 }}>{c.type}</span>
                    <span style={{ color:'var(--dim)', fontSize:11, marginLeft:8 }}>{c.faction_symbol}</span>
                  </div>
                  <div style={{ textAlign:'right', fontSize:11 }}>
                    <div style={{ color:'var(--green)' }}>+{fmtCr(c.on_fulfilled)} cr reward</div>
                    <div style={{ color:'var(--dim)' }}>Deadline: {deadlineStr(c.deadline)}</div>
                  </div>
                </div>
                {c.deliver.map(d => (
                  <div key={d.trade_symbol} style={{ marginBottom:8 }}>
                    <div style={{ display:'flex', justifyContent:'space-between', marginBottom:3, fontSize:11 }}>
                      <span style={{ color:'var(--yellow)', fontWeight:700 }}>{d.trade_symbol}</span>
                      <span style={{ color:'var(--dim)' }}>→ {d.destination_symbol}</span>
                    </div>
                    <ProgBar cur={d.units_fulfilled} req={d.units_required} />
                  </div>
                ))}
              </div>
            ))}
          </section>
        )}

        {/* Available contracts */}
        {available.length > 0 && (
          <section style={{ marginBottom:20 }}>
            <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:12, marginBottom:8 }}>
              AVAILABLE / PRE-NEGOTIATED ({available.length})
            </div>
            <table className="data-table">
              <thead>
                <tr><th>Type</th><th>Good</th><th>Units</th><th>Destination</th><th>Reward</th><th>Expires</th></tr>
              </thead>
              <tbody>
                {available.map(c => (
                  <tr key={c.id} onClick={() => setSelected(c)} style={{ cursor:'pointer' }}>
                    <td style={{ color:'var(--cyan)' }}>{c.type}</td>
                    <td style={{ color:'var(--yellow)' }}>{c.deliver[0]?.trade_symbol || '—'}</td>
                    <td>{fmtCr(c.deliver[0]?.units_required)}</td>
                    <td style={{ color:'var(--dim)' }}>{c.deliver[0]?.destination_symbol || '—'}</td>
                    <td style={{ color:'var(--green)' }}>{fmtCr(c.on_fulfilled)} cr</td>
                    <td style={{ color:'var(--dim)' }}>{deadlineStr(c.expiration)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        {/* Fulfilled */}
        {fulfilled.length > 0 && (
          <section>
            <div style={{ color:'var(--dim)', fontWeight:700, fontSize:11, marginBottom:6 }}>
              COMPLETED ({fulfilled.length})
            </div>
            <table className="data-table">
              <thead>
                <tr><th>Type</th><th>Reward</th></tr>
              </thead>
              <tbody>
                {fulfilled.map(c => (
                  <tr key={c.id}>
                    <td style={{ color:'var(--dim)' }}>{c.type}</td>
                    <td style={{ color:'var(--dim)' }}>{fmtCr(c.on_fulfilled)} cr</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        {contracts.length === 0 && (
          <div style={{ color:'var(--dim)', textAlign:'center', marginTop:40 }}>No contract data</div>
        )}
      </div>

      {selected && <ContractModal contract={selected} onClose={() => setSelected(null)} />}
      <div className="status-bar">Click contract for detail  •  Refreshes every 8s</div>
    </div>
  )
}
