// TypeScript types — mirrors v1 dashboard2.py / api_server.py data shapes

export interface Agent { symbol: string; credits: number; shipCount: number; headquarters: string }

export interface ShipNav {
  waypointSymbol: string; status: 'IN_TRANSIT'|'DOCKED'|'IN_ORBIT'
  flightMode: 'CRUISE'|'DRIFT'|'STEALTH'|'BURN'
  x?: number; y?: number
  route: {
    origin:       { symbol: string; x: number; y: number }
    destination:  { symbol: string; x: number; y: number }
    departureTime: string
    arrival:       string
  }
}
export interface Ship {
  symbol: string
  registration: { role: string; name: string; factionSymbol: string }
  nav: ShipNav
  cargo: { capacity: number; units: number; inventory: {symbol:string;units:number;name?:string}[] }
  fuel:    { current: number; capacity: number }
  frame:   { symbol: string; name: string; condition: number; integrity: number }
  reactor: { symbol: string; name: string; condition: number; integrity: number }
  engine:  { symbol: string; name: string; condition: number; integrity: number; speed: number }
  crew:    { current: number; capacity: number; morale: number }
  mounts:  { symbol: string; name: string; strength?: number; deposits?: string[]; condition?: number }[]
  modules: { symbol: string; name: string }[]
  cooldown: { totalSeconds: number; remainingSeconds: number }
}

export interface ContractDeliverable {
  trade_symbol: string; destination_symbol: string
  units_required: number; units_fulfilled: number
}
export interface Contract {
  id: string; faction_symbol: string|null; type: string
  accepted: boolean; fulfilled: boolean
  expiration: string|null; deadline: string|null; accepted_at?: string|null
  on_accepted: number|null; on_fulfilled: number|null
  deliver: ContractDeliverable[]
}

export interface BotLog { timestamp: number; message: string }

export interface MarketSummary {
  waypoint_symbol: string; good_count?: number; price_count?: number
  updated?: number|null; top_exports?: string|null
}
export interface MarketPrice {
  symbol: string; type?: string|null; supply: string|null; activity: string|null
  purchase: number|null; sell_price: number|null; trade_volume: number|null; last_updated?: number
}
export interface ArbitrageOpp {
  buy_waypoint: string; sell_waypoint: string; trade_symbol: string
  buy_price: number; sell_price: number; margin: number; margin_pct?: number
  buy_supply?: string|null; sell_supply?: string|null
}

export interface WaypointTrait { symbol: string; name?: string|null; description?: string|null }
export interface Waypoint {
  symbol: string; type: string; x: number; y: number
  faction?: string|null; orbits?: string|null
  traits?: WaypointTrait[]
}
export interface WaypointAnalysis {
  symbol?: string; type?: string; x?: number; y?: number
  traits?: WaypointTrait[]
  market?: { exports?: string[]; imports?: string[]; exchange?: string[] }
  shipyard?: { ship_types?: string[] }
  jump_gate?: { connections?: string[] }
  asteroids?: { deposits?: {symbol:string; count?:number; avg_yield?:number}[] }
  error?: string
}

export interface Survey {
  signature: string; waypoint_symbol: string; size: string
  expiration: string; created_at?: number|string
  deposits: { symbol: string }[]
}

export interface Transaction {
  id?: number; timestamp: number; type: string; trade_symbol: string
  units: number; price_per_unit: number; total_price: number
  waypoint_symbol?: string|null; ship_symbol?: string|null; trip_id?: string|null
}
export interface YieldData { trade_symbol: string; total_units: number; count: number; surveyed?: number }
export interface TradeRun {
  trip_id?: string|null; ship_symbol?: string|null; trade_symbol?: string|null
  units?: number; buy_cost?: number; sell_revenue?: number; profit?: number
  buy_waypoint?: string|null; sell_waypoint?: string|null
}
export interface IncomeHour { hour: string; income: number; spend: number; net: number }

export interface CPH { cph_1h: number; cph_10m: number }

export interface Settings {
  auto_buy: boolean; command_role: string
  ship_buy_targets?: { type: string; max: number }[]
}
