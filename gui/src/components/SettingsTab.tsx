import { useState, useEffect } from 'react'
import { api } from '../api'
import { Settings } from '../types'

interface ShipTarget { type: string; max: number }

export default function SettingsTab() {
  const [settings, setSettings] = useState<Settings|null>(null)
  const [targets,  setTargets]  = useState<ShipTarget[]>([])
  const [saving,   setSaving]   = useState(false)
  const [error,    setError]    = useState<string|null>(null)
  const [msg,      setMsg]      = useState<string|null>(null)

  async function load() {
    try {
      const s: Settings = await api.settings()
      setSettings(s)
      setTargets(s.ship_buy_targets || [])
      setError(null)
    } catch (e) { setError(String(e)) }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  function flash(m: string) { setMsg(m); setTimeout(() => setMsg(null), 3000) }

  async function toggleAutoBuy() {
    try { await api.toggleAutoBuy(); await load(); flash('Auto-buy toggled') }
    catch (e) { setError(String(e)) }
  }

  async function setCommandRole(role: string) {
    try { await api.setCommandRole(role); await load(); flash(`Command role set to ${role}`) }
    catch (e) { setError(String(e)) }
  }

  async function saveTargets() {
    setSaving(true)
    try { await api.setShipTargets(targets); await load(); flash('Ship targets saved') }
    catch (e) { setError(String(e)) }
    finally { setSaving(false) }
  }

  function addRow()    { setTargets(t => [...t, { type:'', max:1 }]) }
  function removeRow(i:number) { setTargets(t => t.filter((_,j) => j !== i)) }
  function setType(i:number, v:string) { setTargets(t => t.map((r,j) => j===i ? {...r, type:v} : r)) }
  function setMax(i:number, v:number)  { setTargets(t => t.map((r,j) => j===i ? {...r, max:v}  : r)) }

  const SHIP_TYPES = ['SHIP_MINING_DRONE','SHIP_SURVEYOR','SHIP_LIGHT_HAULER',
    'SHIP_HEAVY_FREIGHTER','SHIP_COMMAND_FRIGATE','SHIP_EXPLORER','SHIP_SIPHON_DRONE']

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding:'5px 12px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0, display:'flex', justifyContent:'space-between' }}>
        <span>⚙ SETTINGS</span>
        {error && <span style={{ color:'var(--red)' }}>⚠ {error}</span>}
        {msg   && <span style={{ color:'var(--green)' }}>✓ {msg}</span>}
      </div>

      <div style={{ flex:1, overflow:'auto', padding:16 }}>
        {!settings && <div style={{ color:'var(--dim)' }}>Loading settings…</div>}
        {settings && (
          <div style={{ maxWidth:640 }}>
            {/* Auto-buy */}
            <div className="panel" style={{ marginBottom:16 }}>
              <div style={{ color:'var(--cyan)', fontWeight:700, fontSize:12, marginBottom:10 }}>AUTO-BUY SHIPS</div>
              <div style={{ display:'flex', alignItems:'center', gap:12 }}>
                <div style={{ fontSize:12 }}>
                  Status: <span style={{ color: settings.auto_buy ? 'var(--green)' : 'var(--dim)', fontWeight:700 }}>
                    {settings.auto_buy ? 'ENABLED' : 'DISABLED'}
                  </span>
                </div>
                <button
                  className={settings.auto_buy ? 'btn-danger' : 'btn-primary'}
                  onClick={toggleAutoBuy}>
                  {settings.auto_buy ? '■ Disable Auto-Buy' : '▶ Enable Auto-Buy'}
                </button>
              </div>
            </div>

            {/* Command ship role */}
            <div className="panel" style={{ marginBottom:16 }}>
              <div style={{ color:'var(--cyan)', fontWeight:700, fontSize:12, marginBottom:10 }}>COMMAND SHIP ROLE</div>
              <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                <span style={{ fontSize:12, color:'var(--dim)' }}>Current:</span>
                <span style={{ fontSize:12, fontWeight:700, color:'var(--yellow)' }}>{settings.command_role || 'IDLE'}</span>
                <button className={`btn-${settings.command_role === 'IDLE' ? 'primary' : 'default'}`} onClick={() => setCommandRole('IDLE')}>
                  Idle
                </button>
                <button className={`btn-${settings.command_role === 'HAULER' ? 'primary' : 'default'}`} onClick={() => setCommandRole('HAULER')}>
                  Hauler
                </button>
                <button className={`btn-${settings.command_role === 'TRADER' ? 'primary' : 'default'}`} onClick={() => setCommandRole('TRADER')}>
                  Trader
                </button>
              </div>
            </div>

            {/* Ship buy targets */}
            <div className="panel">
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:10 }}>
                <div style={{ color:'var(--cyan)', fontWeight:700, fontSize:12 }}>SHIP BUY TARGETS</div>
                <div style={{ display:'flex', gap:8 }}>
                  <button className="btn-default" onClick={addRow}>+ Add Row</button>
                  <button className="btn-primary" onClick={saveTargets} disabled={saving}>
                    {saving ? '⏳ Saving…' : '💾 Save'}
                  </button>
                </div>
              </div>
              <table className="data-table">
                <thead>
                  <tr><th>Ship Type</th><th style={{ textAlign:'right' }}>Max Buy</th><th></th></tr>
                </thead>
                <tbody>
                  {targets.map((r, i) => (
                    <tr key={i}>
                      <td>
                        <select value={r.type} onChange={e => setType(i, e.target.value)}
                          style={{ background:'var(--muted)', border:'1px solid var(--border)', color:'var(--text)', padding:'3px 6px', borderRadius:3, fontSize:11, width:'100%' }}>
                          <option value="">— select type —</option>
                          {SHIP_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </td>
                      <td>
                        <input type="number" min={0} max={99} value={r.max} onChange={e => setMax(i, +e.target.value)}
                          style={{ width:60, background:'var(--muted)', border:'1px solid var(--border)', color:'var(--text)', padding:'3px 6px', borderRadius:3, fontSize:12, textAlign:'right' }} />
                      </td>
                      <td>
                        <button className="btn-danger" style={{ padding:'2px 8px', fontSize:11 }} onClick={() => removeRow(i)}>✕</button>
                      </td>
                    </tr>
                  ))}
                  {targets.length === 0 && (
                    <tr><td colSpan={3} style={{ color:'var(--dim)', textAlign:'center' }}>No targets — click + Add Row</td></tr>
                  )}
                </tbody>
              </table>
              <div style={{ fontSize:11, color:'var(--dim)', marginTop:8 }}>
                The bot will auto-buy ships of these types up to the max quantity set, if Auto-Buy is enabled.
              </div>
            </div>
          </div>
        )}
      </div>
      <div className="status-bar">Settings refresh every 5s  •  Changes take effect on next bot loop</div>
    </div>
  )
}
