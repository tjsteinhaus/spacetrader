import { useState, useEffect } from 'react'
import { api } from '../api'
import { MarketSummary, MarketPrice, ArbitrageOpp } from '../types'
import { fmtCr, supplyColor } from '../utils'

const RIGHT_TABS = ['Prices', 'Arbitrage']

export default function MarketsTab() {
  const [markets,     setMarkets]     = useState<MarketSummary[]>([])
  const [selected,    setSelected]    = useState<string|null>(null)
  const [prices,      setPrices]      = useState<MarketPrice[]>([])
  const [arbitrage,   setArbitrage]   = useState<ArbitrageOpp[]>([])
  const [rightTab,    setRightTab]    = useState('Prices')
  const [refreshing,  setRefreshing]  = useState(false)
  const [minMargin,   setMinMargin]   = useState(50)
  const [error,       setError]       = useState<string|null>(null)

  useEffect(() => {
    api.markets().then(setMarkets).catch(e => setError(String(e)))
  }, [])

  useEffect(() => {
    if (selected && rightTab === 'Prices') {
      api.marketPrices(selected).then(setPrices).catch(() => {})
    }
    if (rightTab === 'Arbitrage') {
      api.arbitrage(minMargin).then(setArbitrage).catch(() => {})
    }
  }, [selected, rightTab, minMargin])

  async function handleRefresh() {
    if (!selected) return
    setRefreshing(true)
    try { await api.refreshMarket(selected); const p = await api.marketPrices(selected); setPrices(p) }
    catch (e) { setError(String(e)) }
    finally { setRefreshing(false) }
  }

  function selectMarket(wp: string) {
    setSelected(wp)
    setPrices([])
    api.marketPrices(wp).then(setPrices).catch(() => {})
  }

  return (
    <div style={{ flex:1, display:'flex', overflow:'hidden' }}>
      {/* Left: market list */}
      <div style={{ flex:'0 0 300px', borderRight:'1px solid var(--border)', display:'flex', flexDirection:'column' }}>
        <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
          🛒 MARKETS ({markets.length})
          {error && <span style={{ color:'var(--red)', marginLeft:8 }}>⚠ {error}</span>}
        </div>
        <div style={{ flex:1, overflow:'auto' }}>
          {markets.map(m => (
            <div key={m.waypoint_symbol}
              onClick={() => selectMarket(m.waypoint_symbol)}
              style={{
                padding:'6px 10px', cursor:'pointer', borderBottom:'1px solid var(--border)',
                background: selected === m.waypoint_symbol ? 'var(--muted)' : 'transparent',
                transition:'background .1s'
              }}>
              <div style={{ color: selected === m.waypoint_symbol ? 'var(--cyan)' : 'var(--text)', fontSize:12, fontWeight:700 }}>
                {m.waypoint_symbol.split('-').pop()}
              </div>
              <div style={{ color:'var(--dim)', fontSize:10, marginTop:2, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>
                {m.waypoint_symbol}
              </div>
              {m.top_exports && (
                <div style={{ fontSize:10, color:'var(--green)', marginTop:2 }}>
                  ↑ {m.top_exports}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Right panel */}
      <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
        <div style={{ display:'flex', alignItems:'center', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0 }}>
          {RIGHT_TABS.map(t => (
            <button key={t} className={`tab-btn ${rightTab===t?'active':''}`} onClick={() => setRightTab(t)}>{t}</button>
          ))}
          {rightTab === 'Prices' && selected && (
            <button className="btn-primary" style={{ marginLeft:8 }} onClick={handleRefresh} disabled={refreshing}>
              {refreshing ? '⏳ Refreshing…' : '↻ Refresh Listings'}
            </button>
          )}
          {rightTab === 'Arbitrage' && (
            <div style={{ marginLeft:8, display:'flex', alignItems:'center', gap:6, fontSize:12 }}>
              <span style={{ color:'var(--dim)' }}>Min margin:</span>
              <input type="number" value={minMargin} onChange={e => setMinMargin(+e.target.value)}
                style={{ width:60, background:'var(--muted)', border:'1px solid var(--border)', color:'var(--text)', padding:'2px 4px', borderRadius:3, fontSize:12 }} />
              <button className="btn-default" onClick={() => api.arbitrage(minMargin).then(setArbitrage)}>Apply</button>
            </div>
          )}
        </div>

        {rightTab === 'Prices' && (
          <div style={{ flex:1, overflow:'auto' }}>
            {!selected && (
              <div style={{ color:'var(--dim)', padding:20, textAlign:'center' }}>Select a market on the left</div>
            )}
            {selected && prices.length === 0 && (
              <div style={{ color:'var(--dim)', padding:20, textAlign:'center' }}>Loading prices…</div>
            )}
            {prices.length > 0 && (
              <table className="data-table">
                <thead>
                  <tr><th>Good</th><th style={{ textAlign:'right' }}>Purchase</th><th style={{ textAlign:'right' }}>Sell</th>
                      <th>Supply</th><th>Activity</th><th style={{ textAlign:'right' }}>Trade Vol</th><th>Type</th></tr>
                </thead>
                <tbody>
                  {prices.map(p => (
                    <tr key={p.symbol}>
                      <td style={{ fontWeight:700, color:'var(--cyan)' }}>{p.symbol}</td>
                      <td style={{ textAlign:'right', color:'var(--red)' }}>{fmtCr(p.purchase)} cr</td>
                      <td style={{ textAlign:'right', color:'var(--green)' }}>{fmtCr(p.sell_price)} cr</td>
                      <td><span style={{ color:supplyColor(p.supply) }}>{p.supply}</span></td>
                      <td style={{ color:'var(--dim)' }}>{p.activity || '—'}</td>
                      <td style={{ textAlign:'right' }}>{fmtCr(p.trade_volume)}</td>
                      <td style={{ color:'var(--dim)', fontSize:10 }}>{p.type}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {rightTab === 'Arbitrage' && (
          <div style={{ flex:1, overflow:'auto' }}>
            {arbitrage.length === 0 ? (
              <div style={{ color:'var(--dim)', padding:20, textAlign:'center' }}>No arbitrage opportunities (min margin: {minMargin} cr)</div>
            ) : (
              <table className="data-table">
                <thead>
                  <tr><th>Good</th><th>Buy At</th><th style={{ textAlign:'right' }}>Buy Price</th>
                      <th>Sell At</th><th style={{ textAlign:'right' }}>Sell Price</th>
                      <th style={{ textAlign:'right' }}>Margin</th><th style={{ textAlign:'right' }}>Margin%</th></tr>
                </thead>
                <tbody>
                  {arbitrage.map((a, i) => (
                    <tr key={i}>
                      <td style={{ color:'var(--yellow)', fontWeight:700 }}>{a.trade_symbol}</td>
                      <td style={{ fontSize:11 }}>{a.buy_waypoint?.split('-').pop()}</td>
                      <td style={{ textAlign:'right', color:'var(--red)' }}>{fmtCr(a.buy_price)} cr</td>
                      <td style={{ fontSize:11 }}>{a.sell_waypoint?.split('-').pop()}</td>
                      <td style={{ textAlign:'right', color:'var(--green)' }}>{fmtCr(a.sell_price)} cr</td>
                      <td style={{ textAlign:'right', color:'var(--green)', fontWeight:700 }}>+{fmtCr(a.margin)} cr</td>
                      <td style={{ textAlign:'right', color:'var(--cyan)' }}>
                        {a.buy_price > 0 ? (a.margin/a.buy_price*100).toFixed(1) : '—'}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
        <div className="status-bar">
          {selected ? `Selected: ${selected}  •  ` : ''}
          Click market to view prices  •  Refresh Listings updates db from live API
        </div>
      </div>
    </div>
  )
}
