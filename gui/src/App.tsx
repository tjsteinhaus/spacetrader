import { useState, useEffect } from 'react'
import MissionControlTab from './components/MissionControlTab'
import FleetTab from './components/FleetTab'
import ContractsTab from './components/ContractsTab'
import AnalyticsTab from './components/AnalyticsTab'
import MarketsTab from './components/MarketsTab'
import UniverseTab from './components/UniverseTab'
import SurveysTab from './components/SurveysTab'
import MapTab from './components/MapTab'
import SettingsTab from './components/SettingsTab'

const TABS = [
  { id: 'mission',   label: '① Mission Control' },
  { id: 'fleet',     label: '② Fleet' },
  { id: 'contracts', label: '③ Contracts' },
  { id: 'analytics', label: '④ Analytics' },
  { id: 'markets',   label: '⑤ Markets' },
  { id: 'universe',  label: '⑥ Universe' },
  { id: 'surveys',   label: '⑦ Surveys' },
  { id: 'map',       label: '⑧ Map' },
  { id: 'settings',  label: '⑨ Settings' },
]

export default function App() {
  const [active, setActive] = useState('mission')
  const [clock, setClock] = useState(() => new Date().toTimeString().slice(0, 8))
  useEffect(() => {
    const t = setInterval(() => setClock(new Date().toTimeString().slice(0, 8)), 1000)
    return () => clearInterval(t)
  }, [])

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100vh', overflow:'hidden', background:'var(--bg)' }}>
      {/* Header */}
      <header style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding:'7px 14px', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0 }}>
        <div style={{ display:'flex', alignItems:'center', gap:10 }}>
          <span style={{ fontSize:18 }}>🚀</span>
          <span style={{ color:'var(--cyan)', fontWeight:700, fontSize:13, letterSpacing:'.18em' }}>SPACETRADERS MISSION CONTROL</span>
        </div>
        <span style={{ color:'var(--dim)', fontSize:11 }}>{clock}</span>
      </header>

      {/* Tab bar */}
      <nav style={{ display:'flex', background:'var(--card)', borderBottom:'1px solid var(--border)', flexShrink:0, overflowX:'auto' }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setActive(t.id)}
            className={`tab-btn ${active === t.id ? 'active' : ''}`}>
            {t.label}
          </button>
        ))}
      </nav>

      {/* Content */}
      <main style={{ flex:1, overflow:'hidden', display:'flex', flexDirection:'column' }}>
        {active === 'mission'   && <MissionControlTab />}
        {active === 'fleet'     && <FleetTab />}
        {active === 'contracts' && <ContractsTab />}
        {active === 'analytics' && <AnalyticsTab />}
        {active === 'markets'   && <MarketsTab />}
        {active === 'universe'  && <UniverseTab />}
        {active === 'surveys'   && <SurveysTab />}
        {active === 'map'       && <MapTab />}
        {active === 'settings'  && <SettingsTab />}
      </main>
    </div>
  )
}
