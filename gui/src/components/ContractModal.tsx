import { useState, useEffect } from 'react'
import { api } from '../api'
import { Contract } from '../types'
import { fmtCr, deadlineStr, tsAgo } from '../utils'

interface Props { contract: Contract; onClose: () => void }

export default function ContractModal({ contract: c, onClose }: Props) {
  const [sourcing, setSourcing] = useState<Record<string,any>>({})

  useEffect(() => {
    for (const d of c.deliver) {
      api.sourcing(d.trade_symbol).then(s => setSourcing(prev => ({ ...prev, [d.trade_symbol]: s }))).catch(() => {})
    }
  }, [c.id])

  return (
    <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal-box" style={{ maxWidth:660 }}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start' }}>
          <div className="modal-title">Contract — {c.type}</div>
          <button className="btn-default" onClick={onClose}>✕ Close</button>
        </div>

        <div style={{ fontSize:12, marginBottom:12 }}>
          <div style={{ display:'flex', gap:20, flexWrap:'wrap', marginBottom:8 }}>
            <div><span style={{ color:'var(--dim)' }}>Faction: </span><span style={{ color:'var(--cyan)' }}>{c.faction_symbol}</span></div>
            <div><span style={{ color:'var(--dim)' }}>Reward: </span><span style={{ color:'var(--green)' }}>{fmtCr(c.on_fulfilled)} cr</span></div>
            <div><span style={{ color:'var(--dim)' }}>Advance: </span><span style={{ color:'var(--yellow)' }}>{fmtCr(c.on_accepted)} cr</span></div>
            <div><span style={{ color:'var(--dim)' }}>Deadline: </span>{deadlineStr(c.deadline)}</div>
            <div><span style={{ color:'var(--dim)' }}>Accepted: </span><span>{tsAgo(c.accepted_at)} ago</span></div>
          </div>
        </div>

        {c.deliver.map(d => {
          const pct = d.units_required > 0 ? Math.round(d.units_fulfilled/d.units_required*100) : 0
          const barColor = pct >= 80 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)'
          const src = sourcing[d.trade_symbol]
          return (
            <div key={d.trade_symbol} style={{ marginBottom:16 }}>
              <div style={{ display:'flex', justifyContent:'space-between', marginBottom:4 }}>
                <span style={{ color:barColor, fontWeight:700 }}>{d.trade_symbol}</span>
                <span style={{ color:'var(--dim)', fontSize:11 }}>{fmtCr(d.units_fulfilled)} / {fmtCr(d.units_required)} units &nbsp; ({pct}%)</span>
              </div>
              <div className="prog-track" style={{ height:10 }}>
                <div className="prog-fill" style={{ width:`${pct}%`, backgroundColor:barColor, height:10 }} />
              </div>
              <div style={{ fontSize:11, color:'var(--dim)', marginTop:3 }}>
                Deliver to: <span style={{ color:'var(--yellow)' }}>{d.destination_symbol}</span>
                &nbsp; Remaining: <span style={{ color:'var(--red)' }}>{fmtCr(d.units_required - d.units_fulfilled)} units</span>
              </div>

              {src && (
                <div style={{ marginTop:8, padding:'8px 10px', background:'var(--muted)', borderRadius:4 }}>
                  <div style={{ color:'var(--cyan)', fontSize:11, fontWeight:700, marginBottom:4 }}>SOURCING ANALYSIS</div>
                  {src.markets?.length > 0 ? (
                    src.markets.slice(0,3).map((m: any) => (
                      <div key={m.waypoint} style={{ fontSize:11, display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                        <span style={{ color:'var(--yellow)' }}>{m.waypoint}</span>
                        <span>{m.activity || '—'} &nbsp; supply: {m.supply || '—'}</span>
                        <span style={{ color:'var(--green)' }}>{fmtCr(m.purchase_price)} cr/u</span>
                      </div>
                    ))
                  ) : <div style={{ color:'var(--dim)', fontSize:11 }}>No market data</div>}
                  {src.haulers?.length > 0 && (
                    <div style={{ fontSize:11, marginTop:4, color:'var(--dim)' }}>
                      Assigned haulers: {src.haulers.join(', ')}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
