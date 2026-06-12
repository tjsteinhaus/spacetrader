import { useState, useEffect } from 'react'
import { api } from '../api'
import { Transaction, YieldData, TradeRun, IncomeHour } from '../types'
import { fmtCr, fmtDt } from '../utils'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell, ResponsiveContainer } from 'recharts'

const SUB_TABS = ['Transactions', 'Yields', 'Income Chart', 'Trade Runs']

export default function AnalyticsTab() {
  const [tab,          setTab]          = useState('Transactions')
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [txFilter,     setTxFilter]     = useState<'all'|'sell'|'buy'>('all')
  const [yields,       setYields]       = useState<YieldData[]>([])
  const [yWindow,      setYWindow]      = useState('20m')
  const [income,       setIncome]       = useState<IncomeHour[]>([])
  const [tradeRuns,    setTradeRuns]    = useState<TradeRun[]>([])
  const [error,        setError]        = useState<string|null>(null)

  useEffect(() => {
    if (tab === 'Transactions') {
      api.transactions(300).then(setTransactions).catch(e => setError(String(e)))
    } else if (tab === 'Yields') {
      api.yields(yWindow).then(setYields).catch(e => setError(String(e)))
    } else if (tab === 'Income Chart') {
      api.income().then(setIncome).catch(e => setError(String(e)))
    } else if (tab === 'Trade Runs') {
      api.tradeRuns().then(setTradeRuns).catch(e => setError(String(e)))
    }
  }, [tab, yWindow])

  const filteredTx = transactions.filter(t =>
    txFilter === 'all'  ? true :
    txFilter === 'sell' ? t.type === 'SELL' :
                          t.type === 'PURCHASE'
  )

  const netCredits = transactions.reduce((a, t) => t.type === 'SELL' ? a + t.total_price : a - t.total_price, 0)

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0, display:'flex', justifyContent:'space-between' }}>
        <span>📈 ANALYTICS</span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
      </div>

      {/* Sub-tabs */}
      <div style={{ display:'flex', borderBottom:'1px solid var(--border)', flexShrink:0 }}>
        {SUB_TABS.map(t => (
          <button key={t} className={`tab-btn ${tab===t?'active':''}`} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {tab === 'Transactions' && (
        <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          <div style={{ padding:'6px 12px', flexShrink:0, display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
            {(['all','sell','buy'] as const).map(f => (
              <button key={f} className={`btn-${txFilter===f?'primary':'default'}`} onClick={() => setTxFilter(f)}>
                {f === 'all' ? 'All' : f === 'sell' ? 'Sales' : 'Purchases'}
              </button>
            ))}
            <span style={{ marginLeft:12, fontSize:12 }}>
              Net: <span style={{ color: netCredits >= 0 ? 'var(--green)' : 'var(--red)', fontWeight:700 }}>
                {netCredits >= 0 ? '+' : ''}{fmtCr(netCredits)} cr
              </span>
              &nbsp;<span style={{ color:'var(--dim)' }}>({filteredTx.length} shown of {transactions.length})</span>
            </span>
          </div>
          <div style={{ flex:1, overflow:'auto' }}>
            <table className="data-table">
              <thead>
                <tr><th>Time</th><th>Ship</th><th>Type</th><th>Good</th><th>Units</th><th>Price/u</th><th>Total</th><th>WP</th><th>Trip</th></tr>
              </thead>
              <tbody>
                {filteredTx.map((t, i) => (
                  <tr key={i}>
                    <td style={{ color:'var(--dim)', fontSize:10 }}>{fmtDt(t.timestamp)}</td>
                    <td style={{ fontSize:11, color:'var(--cyan)' }}>…-{t.ship_symbol.split('-').pop()}</td>
                    <td>
                      <span style={{ color: t.type === 'SELL' ? 'var(--green)' : 'var(--red)' }}>
                        {t.type === 'SELL' ? '▲' : '▼'} {t.type}
                      </span>
                    </td>
                    <td style={{ color:'var(--yellow)' }}>{t.trade_symbol}</td>
                    <td style={{ textAlign:'right' }}>{fmtCr(t.units)}</td>
                    <td style={{ textAlign:'right' }}>{fmtCr(t.price_per_unit)}</td>
                    <td style={{ textAlign:'right', color: t.type === 'SELL' ? 'var(--green)' : 'var(--red)' }}>
                      {t.type === 'SELL' ? '+' : '-'}{fmtCr(t.total_price)}
                    </td>
                    <td style={{ fontSize:10, color:'var(--dim)' }}>{t.waypoint_symbol?.split('-').pop()}</td>
                    <td style={{ fontSize:10, color:'var(--dim)' }}>{t.trip_id ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'Yields' && (
        <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          <div style={{ padding:'6px 12px', flexShrink:0, display:'flex', gap:8, alignItems:'center' }}>
            {['20m','1h','all'].map(w => (
              <button key={w} className={`btn-${yWindow===w?'primary':'default'}`} onClick={() => setYWindow(w)}>
                {w}
              </button>
            ))}
          </div>
          <div style={{ flex:1, overflow:'auto' }}>
            <table className="data-table">
              <thead>
                <tr><th>Good</th><th style={{ textAlign:'right' }}>Total Units</th><th style={{ textAlign:'right' }}>Extractions</th><th style={{ textAlign:'right' }}>Avg/Extract</th></tr>
              </thead>
              <tbody>
                {yields.map((y,i) => (
                  <tr key={i}>
                    <td style={{ color:'var(--green)', fontWeight:700 }}>{y.trade_symbol}</td>
                    <td style={{ textAlign:'right' }}>{fmtCr(y.total_units)}</td>
                    <td style={{ textAlign:'right' }}>{y.count}</td>
                    <td style={{ textAlign:'right', color:'var(--dim)' }}>
                      {y.count > 0 ? Math.round(y.total_units/y.count) : 0}
                    </td>
                  </tr>
                ))}
                {yields.length === 0 && <tr><td colSpan={4} style={{ color:'var(--dim)' }}>No yield data</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'Income Chart' && (
        <div style={{ flex:1, overflow:'auto', padding:12 }}>
          <div style={{ color:'var(--cyan)', fontWeight:700, fontSize:12, marginBottom:12 }}>
            Hourly Income / Spend (last 12h)
          </div>
          {income.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={income}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="hour" stroke="var(--dim)" tick={{ fontSize:10 }} />
                <YAxis stroke="var(--dim)" tick={{ fontSize:10 }} tickFormatter={v => fmtCr(v)} />
                <Tooltip
                  contentStyle={{ background:'var(--card)', border:'1px solid var(--border)', color:'var(--text)' }}
                  formatter={(v: number) => [fmtCr(v) + ' cr']}
                />
                <Bar dataKey="net" radius={[2,2,0,0]}>
                  {income.map((h, i) => (
                    <Cell key={i} fill={h.net >= 0 ? '#22c55e' : '#ef4444'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <div style={{ color:'var(--dim)' }}>No income data</div>}

          {income.length > 0 && (
            <table className="data-table" style={{ marginTop:12 }}>
              <thead>
                <tr><th>Hour</th><th style={{ textAlign:'right' }}>Income</th><th style={{ textAlign:'right' }}>Spend</th><th style={{ textAlign:'right' }}>Net</th></tr>
              </thead>
              <tbody>
                {income.map((h, i) => (
                  <tr key={i}>
                    <td style={{ fontSize:10 }}>{h.hour}</td>
                    <td style={{ textAlign:'right', color:'var(--green)' }}>{fmtCr(h.income)}</td>
                    <td style={{ textAlign:'right', color:'var(--red)' }}>{fmtCr(h.spend)}</td>
                    <td style={{ textAlign:'right', color: h.net >= 0 ? 'var(--green)' : 'var(--red)' }}>
                      {h.net >= 0 ? '+' : ''}{fmtCr(h.net)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'Trade Runs' && (
        <div style={{ flex:1, overflow:'auto' }}>
          <table className="data-table">
            <thead>
              <tr><th>Trip ID</th><th>Ship</th><th>Good</th><th style={{ textAlign:'right' }}>Units</th>
                  <th style={{ textAlign:'right' }}>Buy Cost</th><th style={{ textAlign:'right' }}>Sell Rev</th>
                  <th style={{ textAlign:'right' }}>Profit</th><th style={{ textAlign:'right' }}>ROI%</th>
                  <th>From</th><th>To</th></tr>
            </thead>
            <tbody>
              {tradeRuns.map((r, i) => {
                const roi = r.buy_cost > 0 ? ((r.sell_revenue - r.buy_cost) / r.buy_cost * 100).toFixed(1) : '—'
                const roiN = r.buy_cost > 0 ? (r.sell_revenue - r.buy_cost) / r.buy_cost * 100 : 0
                return (
                  <tr key={i}>
                    <td style={{ color:'var(--dim)', fontSize:10 }}>{r.trip_id ?? '—'}</td>
                    <td style={{ color:'var(--cyan)', fontSize:11 }}>…-{r.ship_symbol?.split('-').pop()}</td>
                    <td style={{ color:'var(--yellow)' }}>{r.trade_symbol}</td>
                    <td style={{ textAlign:'right' }}>{fmtCr(r.units)}</td>
                    <td style={{ textAlign:'right', color:'var(--red)' }}>{fmtCr(r.buy_cost)}</td>
                    <td style={{ textAlign:'right', color:'var(--green)' }}>{fmtCr(r.sell_revenue)}</td>
                    <td style={{ textAlign:'right', color: r.profit >= 0 ? 'var(--green)' : 'var(--red)', fontWeight:700 }}>
                      {r.profit >= 0 ? '+' : ''}{fmtCr(r.profit)}
                    </td>
                    <td style={{ textAlign:'right', color: roiN >= 0 ? 'var(--green)' : 'var(--red)' }}>
                      {roi}{roi !== '—' ? '%' : ''}
                    </td>
                    <td style={{ fontSize:10, color:'var(--dim)' }}>{r.buy_waypoint?.split('-').pop()}</td>
                    <td style={{ fontSize:10, color:'var(--dim)' }}>{r.sell_waypoint?.split('-').pop()}</td>
                  </tr>
                )
              })}
              {tradeRuns.length === 0 && <tr><td colSpan={10} style={{ color:'var(--dim)' }}>No trade run data</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      <div className="status-bar">Sub-tabs: Transactions · Yields · Income Chart · Trade Runs</div>
    </div>
  )
}
