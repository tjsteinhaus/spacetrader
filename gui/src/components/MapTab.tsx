import { useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../api'
import { Waypoint, Ship } from '../types'
import { shipIcon, shipRole } from '../utils'

const WP_ICONS: Record<string,string> = {
  PLANET:              '🪐',
  GAS_GIANT:           '⭕',
  MOON:                '🌙',
  ORBITAL_STATION:     '🛸',
  JUMP_GATE:           '🌀',
  ASTEROID_FIELD:      '🪨',
  ASTEROID:            '🪨',
  ENGINEERED_ASTEROID: '⚙',
  ASTEROID_BASE:       '🏭',
  NEBULA:              '☁',
  DEBRIS_FIELD:        '💥',
  GRAVITY_WELL:        '🌑',
  ARTIFICIAL_GRAVITY_WELL:'🌑',
  FUEL_STATION:        '⛽',
}

function lerp(a: number, b: number, t: number) { return a + (b-a)*t }

function shipPosition(ship: Ship): {x:number;y:number} {
  const nav   = ship.nav   || {} as any
  const route = nav.route  || {} as any
  if (nav.status !== 'IN_TRANSIT' || !route.origin || !route.destination) {
    return { x: nav.x ?? route.destination?.x ?? 0, y: nav.y ?? route.destination?.y ?? 0 }
  }
  const dep  = new Date(route.departureTime).getTime()
  const arr  = new Date(route.arrival).getTime()
  const now  = Date.now()
  const t    = Math.min(1, Math.max(0, (now - dep) / (arr - dep || 1)))
  return {
    x: lerp(route.origin.x, route.destination.x, t),
    y: lerp(route.origin.y, route.destination.y, t),
  }
}

const SHIP_COLORS = [
  'var(--cyan)','var(--green)','var(--yellow)','var(--magenta)','var(--red)',
  '#60a5fa','#fb923c','#a78bfa'
]

export default function MapTab() {
  const [waypoints, setWaypoints] = useState<Waypoint[]>([])
  const [ships,     setShips]     = useState<Ship[]>([])
  const [hovered,   setHovered]   = useState<string|null>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const [dim, setDim] = useState({ w:800, h:500 })
  const frameRef = useRef<number>(0)
  const shipsRef = useRef<Ship[]>([])
  shipsRef.current = ships

  useEffect(() => {
    api.waypoints().then(setWaypoints).catch(() => {})
    api.ships().then(setShips).catch(() => {})
    const t = setInterval(async () => {
      try { setShips(await api.ships()) } catch {}
    }, 10000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const obs = new ResizeObserver(entries => {
      for (const e of entries) {
        setDim({ w: e.contentRect.width, h: e.contentRect.height })
      }
    })
    if (svgRef.current?.parentElement) obs.observe(svgRef.current.parentElement)
    return () => obs.disconnect()
  }, [])

  // Animate ship positions
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let raf: number
    function frame() { setTick(t => t+1); raf = requestAnimationFrame(frame) }
    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [])

  if (waypoints.length === 0) {
    return (
      <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
        <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center', color:'var(--dim)' }}>
          Loading system map…
        </div>
      </div>
    )
  }

  const xs = waypoints.map(w => w.x)
  const ys = waypoints.map(w => w.y)
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 50
  const scaleX = (dim.w - pad*2) / ((maxX-minX) || 1)
  const scaleY = (dim.h - pad*2) / ((maxY-minY) || 1)
  const scale  = Math.min(scaleX, scaleY)

  function toSvg(x: number, y: number) {
    return {
      cx: pad + (x - minX) * scale,
      cy: pad + (y - minY) * scale,
    }
  }

  const shipColorMap: Record<string,string> = {}
  ships.forEach((s, i) => { shipColorMap[s.symbol] = SHIP_COLORS[i % SHIP_COLORS.length] })

  return (
    <div style={{ flex:1, display:'flex', overflow:'hidden' }}>
      {/* SVG Map */}
      <div style={{ flex:1, position:'relative', overflow:'hidden' }}>
        <svg ref={svgRef} width={dim.w} height={dim.h} style={{ display:'block', background:'var(--bg)' }}>
          {/* Waypoints */}
          {waypoints.map(w => {
            const { cx, cy } = toSvg(w.x, w.y)
            const icon = WP_ICONS[w.type] || '·'
            const isHov = hovered === w.symbol
            return (
              <g key={w.symbol} onMouseEnter={() => setHovered(w.symbol)} onMouseLeave={() => setHovered(null)} style={{ cursor:'pointer' }}>
                <circle cx={cx} cy={cy} r={isHov ? 12 : 8} fill="none" stroke={isHov ? 'var(--cyan)' : 'var(--border)'} strokeWidth={1} />
                <text x={cx} y={cy+4} textAnchor="middle" fontSize={11}>{icon}</text>
                {isHov && (
                  <text x={cx} y={cy-16} textAnchor="middle" fontSize={9} fill="var(--cyan)">{w.symbol.split('-').pop()}</text>
                )}
              </g>
            )
          })}

          {/* Ships */}
          {ships.map((s, i) => {
            const pos   = shipPosition(s)
            const { cx, cy } = toSvg(pos.x, pos.y)
            const color = shipColorMap[s.symbol]
            return (
              <g key={s.symbol}>
                <circle cx={cx} cy={cy} r={5} fill={color} opacity={0.85} />
                <text x={cx+7} y={cy+4} fontSize={8} fill={color}>{s.symbol.split('-').pop()}</text>
              </g>
            )
          })}
        </svg>
      </div>

      {/* Right: legend + ships list */}
      <div style={{ flex:'0 0 220px', borderLeft:'1px solid var(--border)', display:'flex', flexDirection:'column', overflow:'hidden' }}>
        <div style={{ padding:'5px 10px', background:'var(--card)', borderBottom:'1px solid var(--border)', color:'var(--cyan)', fontSize:11, fontWeight:700, letterSpacing:'.1em', flexShrink:0 }}>
          🗺 MAP
        </div>
        <div style={{ flex:1, overflow:'auto', padding:'8px 10px' }}>
          <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, marginBottom:6 }}>LEGEND</div>
          {Object.entries(WP_ICONS).slice(0,8).map(([t, icon]) => (
            <div key={t} style={{ fontSize:11, marginBottom:3 }}>
              <span style={{ marginRight:6 }}>{icon}</span>
              <span style={{ color:'var(--dim)' }}>{t.replace(/_/g,' ')}</span>
            </div>
          ))}

          <div style={{ color:'var(--yellow)', fontWeight:700, fontSize:11, margin:'12px 0 6px' }}>SHIPS</div>
          {ships.map(s => {
            const role  = shipRole(s)
            const color = shipColorMap[s.symbol]
            const nav   = s.nav || {} as any
            const fuel  = s.fuel || { current:0, capacity:1 }
            const cargo = s.cargo || { units:0, capacity:0 }
            return (
              <div key={s.symbol} style={{ marginBottom:8 }}>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:11 }}>
                  <span style={{ color }}>{shipIcon(s)} …-{s.symbol.split('-').pop()}</span>
                  <span style={{ color:role.color, fontSize:10 }}>{role.label}</span>
                </div>
                <div style={{ fontSize:10, color:'var(--dim)', marginTop:1 }}>
                  {nav.waypointSymbol?.split('-').pop()}
                  <span style={{ marginLeft:6 }}>⛽{fuel.current}/{fuel.capacity}</span>
                  <span style={{ marginLeft:6 }}>📦{cargo.units}/{cargo.capacity}</span>
                </div>
              </div>
            )
          })}
        </div>
        <div className="status-bar" style={{ fontSize:9 }}>Ships update every 3s  •  Hover WP for label</div>
      </div>
    </div>
  )
}
