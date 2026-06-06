# SpaceTraders Automation Playbook

**Agent:** TYLERMASTERY  
**Script:** `play.py`  
**Game:** SpaceTraders v2 API — https://spacetraders.io  
**Purpose:** Automate mining contracts indefinitely to accumulate credits competitively.

---

## Table of Contents

- [SpaceTraders Automation Playbook](#spacetraders-automation-playbook)
  - [Table of Contents](#table-of-contents)
  - [What the Script Does](#what-the-script-does)
  - [Architecture Overview](#architecture-overview)
  - [Configuration Reference](#configuration-reference)
    - [Waypoints](#waypoints)
    - [Ships](#ships)
    - [Economics](#economics)
    - [Ship Purchase Priority](#ship-purchase-priority)
    - [Repair \& Upgrades](#repair--upgrades)
  - [Component Deep-Dives](#component-deep-dives)
    - [Main Loop](#main-loop)
    - [Contract Sourcing](#contract-sourcing)
    - [Miner Loop](#miner-loop)
    - [Surveyor Loop](#surveyor-loop)
    - [Fleet Manager](#fleet-manager)
    - [Sell Junk \& Jettison Logic](#sell-junk--jettison-logic)
    - [Market Intelligence](#market-intelligence)
    - [Navigation](#navigation)
    - [Repair System](#repair-system)
    - [Upgrade System](#upgrade-system)
  - [Decision Log](#decision-log)
  - [Rebuild Plan](#rebuild-plan)
    - [Step 1 — Environment Setup](#step-1--environment-setup)
    - [Step 2 — SpaceTraders Account](#step-2--spacetraders-account)
    - [Step 3 — Identify Your Starting System](#step-3--identify-your-starting-system)
    - [Step 4 — Find Your Asteroid \& Markets](#step-4--find-your-asteroid--markets)
    - [Step 5 — Update Config in `play.py`](#step-5--update-config-in-playpy)
    - [Step 6 — Get Your Second Ship](#step-6--get-your-second-ship)
    - [Step 7 — Run the Script](#step-7--run-the-script)
    - [Step 8 — What to Watch For](#step-8--what-to-watch-for)
    - [Step 9 — Key First-Day Priority](#step-9--key-first-day-priority)
    - [Strategy File](#strategy-file)
    - [MCP Server](#mcp-server)
    - [Module Summary](#module-summary)

---

## What the Script Does

The script plays SpaceTraders autonomously in a loop:

```
1. Get a contract (negotiate one if none exist)
2. Accept the contract
3. Mine the required resource (COPPER_ORE, IRON_ORE, etc.) at a nearby asteroid
4. Deliver filled cargo to the contract destination
5. Fulfill the contract for the payout bonus
6. Use accumulated credits to buy more ships → repeat with larger fleet
```

Everything runs concurrently. Each ship gets its own daemon thread: miners mine,
surveyors survey, and the fleet manager buys ships — all at the same time without
blocking each other.

---

## Architecture Overview

```
Main Thread (run())
│
├── work_contract(contract)
│   ├── Thread: miner_loop(TYLERMASTERY-1)
│   ├── Thread: miner_loop(TYLERMASTERY-3)
│   ├── Thread: miner_loop(TYLERMASTERY-5)
│   ├── Thread: surveyor_loop(TYLERMASTERY-4)
│   └── Thread: fleet_manager_loop(TYLERMASTERY-2)
│       ├── Every 120s: _bg_buy_and_launch() — buys ships, spins new threads
│       └── Every 600s: _bg_negotiate_contract() — pre-negotiates next contract
│
└── After contract done:
    ├── buy_ships()          — top up fleet if credits allow
    ├── step_maintain_fleet()— repair degraded ships
    ├── step_upgrade_fleet() — install better mining lasers
    └── Loop back to step 1
```

**Threading model:** Python's `threading.Thread` with daemon=True so threads die
when the main process exits. `contract_done` (Event) signals all threads to wrap up
when the contract is fulfilled. `stop_event` signals an orderly shutdown.

**Shared state:**
- `_shared_surveys` — list of survey results; surveyors write, miners read
- `_surveys_lock` — threading.Lock protecting the survey list
- `_fulfill_lock` — prevents two miners from both calling `fulfill_contract` at once
- `_manager_lock` — prevents overlapping fleet-management ops
- `_market_cache` — dict of market prices, keyed by waypoint; refreshed every 10 min

---

## Configuration Reference

All tunable constants live at the top of `play.py`. Change these before a new run.

### Waypoints

| Constant | Value | What It Is |
|---|---|---|
| `SYSTEM` | `X1-HU91` | The star system we're operating in |
| `ASTEROID` | `X1-HU91-FD5D` | ENGINEERED_ASTEROID with COMMON_METAL_DEPOSITS + on-site FUEL exchange |
| `ASTEROID_BASE` | `X1-HU91-H52` | Nearest full market to the asteroid; MARKETPLACE + SHIPYARD |
| `SHIPYARD_WP` | `X1-HU91-H52` | Same moon — used for repairs and upgrades |
| `SHIPYARD_WPS` | `[H52, A2]` | All known shipyards; fleet manager checks both when buying ships |

**Why X1-HU91-FD5D?** A full system scan (85 waypoints) revealed FD5D is an
ENGINEERED_ASTEROID at the center cluster (-2, 26). Key advantages over B8 (old
site at +350 units away):
- K84 (contract delivery) is only 127 units from FD5D vs 408 units from B8 — 3× closer
- FD5D has an on-site FUEL exchange (no need to leave for basic refueling)
- H52 is 38 units away (MARKETPLACE + SHIPYARD) vs B7 at 195 units
- ENGINEERED_ASTEROIDs yield a wider range of goods including refined metals

B8 (old site) was COMMON_METAL_DEPOSITS and yielded only ALUMINUM_ORE, never
refined ALUMINUM — contracts for ALUMINUM were impossible to complete from B8.

### Ships

| Constant | Value | What It Is |
|---|---|---|
| `COMMAND_SHIP` | `TYLERMASTERY-1` | The first ship; always a miner if it has a mining laser |
| `FLEET_MANAGER_SHIP` | `TYLERMASTERY-2` | No mining laser; used for background fleet ops |

**Why TYLERMASTERY-2 as fleet manager?** It was the second ship acquired and has no
mining laser, so assigning it to mining would waste it. Instead it stays near the
shipyard to buy new ships immediately when credits allow, and navigates to the faction
HQ to pre-negotiate contracts.

### Economics

| Constant | Value | Rationale |
|---|---|---|
| `CREDIT_RESERVE` | `30_000` | Never spend below this floor — keeps us solvent for fuel + repairs |
| `MIN_SELL_PRICE` | `30` | cr/unit threshold below which cargo is jettisoned, not hauled |
| `MIN_FUEL_CAPACITY` | `200` | Ships with tiny tanks can't reach FD5D↔H52↔K84 reliably |
| `MIN_BUY_CREDITS` | `120_000` | Fleet manager won't even navigate to the shipyard below this — prevents constant fruitless trips when credits are low |

**Why 30k reserve?** At 30k we can still afford: fuel for all ships (~400 cr each
fill), emergency repairs, and at least partial contract delivery. At 50k (the old
value) we blocked ship purchases because SURVEYOR costs 32,918 cr — we'd need 82,918
credits just to buy the cheapest useful ship.

**Why MIN_SELL_PRICE = 30?** ICE_WATER sells for 13 cr/unit and QUARTZ_SAND for 18
cr/unit. A round-trip from asteroid FD5D to market H52 takes ~4 minutes. Mining 10
units of ICE_WATER earns 130 cr but costs fuel and time that could be spent mining
metals (worth 400-600 cr per load). Jettisoning at the asteroid recovers cargo space
immediately and lets the miner stay productive.

**Why MIN_BUY_CREDITS = 120,000?** The fleet manager used to navigate to the shipyard
every 2 minutes regardless of credits, wasting fuel and API calls. At 120k we can
afford the cheapest useful ship (SHIP_LIGHT_SHUTTLE ~86k at A2) and still have the
30k reserve. Below that, the manager parks at A1 idle.

### Ship Purchase Priority

```python
SHIP_SCORES = {
    "SHIP_ORE_HOUND":       100,  # Best miner: powerful mounts + large cargo
    "SHIP_MINING_DRONE":    90,   # Decent miner
    "SHIP_SURVEYOR":        75,   # Fills survey pool — boosts all miners' copper yield
    "SHIP_HEAVY_FREIGHTER": 65,   # Good hauler once fleet is large
    "SHIP_LIGHT_HAULER":    60,   # Useful with 2+ miners
    "SHIP_COMMAND_FRIGATE": 50,   # Versatile but expensive
    "SHIP_PROBE":           -1,   # Never buy — no laser, no surveyor, useless
}
```

**Why SHIP_SURVEYOR at 75?** Surveyors generate surveys that target specific
resources. Without surveys, extraction is random — you might mine 20 QUARTZ_SAND
cycles in a row and zero COPPER_ORE. With a copper-focused survey, the odds shift
dramatically toward the contract resource. One surveyor can fuel all miners with
better results. Previously set to -1 (never buy), which meant we never got copper.

**Why was SHIP_SURVEYOR originally -1?** The first pass of the script treated
surveyors as dead weight because they have no mining laser. This was wrong — their
value is indirect but significant: better surveys → more copper per cycle → faster
contract completion.

### Repair & Upgrades

| Constant | Value | Meaning |
|---|---|---|
| `REPAIR_THRESHOLD` | `0.80` | Trigger repair when frame/engine/reactor drops below 80% |
| `MINING_MOUNT_TIERS` | See below | Upgrade path for mining lasers |

```python
MINING_MOUNT_TIERS = [
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]
```

Ships are upgraded one tier at a time at the shipyard after each contract.

---

## Component Deep-Dives

### Main Loop

```python
def run() -> None:
    discover_markets()
    while True:
        contract = get_next_contract()
        work_contract(contract)
        buy_ships()
        step_maintain_fleet()
        step_upgrade_fleet()
        step_show_status()
```

Simple infinite loop. Each iteration:
1. Gets (or negotiates) a contract
2. Runs all miners/surveyors concurrently until contract is done
3. Post-contract cleanup: buy ships, repair, upgrade

`discover_markets()` runs once at startup to populate the known market list. The
market cache refreshes automatically every 10 minutes during operation.

---

### Contract Sourcing

```python
def get_next_contract() -> dict | None:
    cs = contracts_api.get_contracts()
    pending = [c for c in cs if not c.get("fulfilled")]
    if pending:
        return pending[0]
    # Navigate COMMAND_SHIP to faction HQ and negotiate
```

**At startup or after contract completion**, the script checks for any unfulfilled
contracts. If none exist, COMMAND_SHIP navigates to the faction headquarters
(`X1-HU91-A1`) to negotiate a new one.

**Pre-negotiation (fleet manager):** `_bg_negotiate_contract()` runs in the background
using FLEET_MANAGER_SHIP every 10 minutes. It checks if fewer than 2 unfulfilled
contracts exist; if so, it navigates to the faction HQ and negotiates. This means
when the current contract completes, the next one is already waiting — eliminating
downtime between contracts.

**Why the 2-contract limit?** SpaceTraders API enforces a limit on how many contracts
you can hold at once. We only want to pre-negotiate if we don't already have one queued.

---

### Miner Loop

```python
def miner_loop(ship_symbol, contract, contract_done, stop_event):
```

Each miner runs this loop independently:

```
1. Preflight: navigate to ASTEROID_BASE for fuel if far away or tank < 50%
2. Navigate to ASTEROID, enter orbit
3. Get a survey from the shared pool (or survey independently if pool empty)
4. Loop:
   a. Check fuel — top up if < 40%
   b. Check condition — repair if < 80%
   c. If cargo nearly full (< 5 free slots):
      - If carrying contract good: navigate to delivery waypoint, deliver
        - If delivery completes contract: fulfill_contract, set contract_done, exit
      - Else: dump junk, return to mine
   d. Wait for cooldown
   e. Extract (with survey if available, else raw)
   f. Repeat
```

**The delivery detour:** Before the long trip to the delivery destination, the miner
always stops at ASTEROID_BASE (H52) to refuel. This ensures it never runs out of fuel
mid-delivery. After delivery it refuels again at the nearest market before returning.

**Preflight fuel check:** At startup, ships only navigate to H52 for a preflight
refuel if they are *not* already at the mining cluster AND have less than 50% fuel.
Using `and` (not `or`) prevents ships at FD5D from making unnecessary refuel trips.

**Survey fallback:** If the shared survey pool is empty, the miner calls `try_survey()`
itself. This is slower (uses its own cooldown) but keeps it from mining blindly while
waiting for the surveyor to arrive.

**`contract_done` event:** The first miner to deliver the final units calls
`fulfill_contract()` under a `_fulfill_lock`. It then sets `contract_done`. All other
miners and the fleet manager see this event and exit their loops.

---

### Surveyor Loop

```python
def surveyor_loop(ship_symbol, contract, contract_done, stop_event):
```

Dedicated to non-stop surveying at the asteroid:

```
1. Navigate to ASTEROID_BASE for fuel, then ASTEROID
2. Loop:
   a. Check fuel — top up at ASTEROID_BASE if < 50%
   b. Wait cooldown
   c. Call fleet_api.survey()
   d. Publish all results to _shared_surveys
   e. Log how many hits for the contract good were found
```

**Why a dedicated surveyor?** Mining with surveys targeting COPPER_ORE dramatically
increases copper yield per cycle. Without surveys, each extraction is random across all
deposit types. With a focused survey, the game biases extractions toward the targeted
mineral. A single surveyor running continuously keeps the shared pool full so all three
miners benefit simultaneously.

**Shared survey pool:** Surveys in SpaceTraders are "consumable" by the game server —
each use can deplete it. The code tracks expiration timestamps and prunes expired
surveys automatically. Miners pick the survey with the most copper hits from the pool.

---

### Fleet Manager

```python
def fleet_manager_loop(contract, contract_done, stop_event):
    # Every 120 seconds:
    _bg_buy_and_launch(contract, contract_done, stop_event)
    _bg_negotiate_contract()
```

**`_bg_buy_and_launch()`** — ship buying logic:
1. Quick bail-out: if `credits - CREDIT_RESERVE < 10,000` skip all API calls
2. Navigate FLEET_MANAGER_SHIP to SHIPYARD_WP, dock
3. Fetch current shipyard listings
4. Score all available ship types using SHIP_SCORES
5. Filter out ships with fuel tanks below MIN_FUEL_CAPACITY (e.g., MINING_DRONE with
   80-unit tank can't make the B7↔B8↔H51 triangle reliably)
6. For each affordable ship (in score order):
   - Purchase it
   - Detect if it has a survey mount but no mining mount → assign surveyor_loop
   - Otherwise → assign miner_loop
   - Launch immediately as a new daemon thread

**`_bg_negotiate_contract()`** — contract pre-negotiation:
1. Rate-limited: at most once every 10 minutes
2. Checks if < 2 unfulfilled contracts exist (skip if already queued)
3. Navigate to faction HQ (X1-HU91-A1), orbit
4. Call `fleet_api.negotiate_contract(FLEET_MANAGER_SHIP)`
5. Log the new contract's payout

**Why the 120s interval?** Shipyard prices don't change often, and API rate limits
apply. 2 minutes is a reasonable balance between responsiveness and not hammering the API.

**Why not use COMMAND_SHIP for fleet management?** COMMAND_SHIP is the primary miner.
Diverting it to the shipyard costs 2-4 minutes of mining time per purchase. TYLERMASTERY-2
has nothing else to do, so it handles all background ops without impacting mining throughput.

---

### Sell Junk & Jettison Logic

```python
def sell_junk(ship_symbol, keep_good=None):
```

Called whenever cargo is nearly full and the ship has no contract good to deliver.

```
1. Build inventory list (exclude keep_good)
2. For each item:
   - Look up best sell price across all known markets
   - If best_price < MIN_SELL_PRICE (30 cr): jettison immediately
   - Otherwise: add to worth_selling list
3. If nothing worth selling: return
4. Find best market for remaining cargo (best_sell_market_for_cargo)
5. Navigate there if not already docked
6. Sell everything
```

**Why jettison first?** Before this change, miners hauled ICE_WATER (13 cr) and
QUARTZ_SAND (18 cr) all the way to B7, sold them for pennies, then came back. The
round trip took ~2 minutes. Each cargo slot filled with cheap junk is a slot that
could hold COPPER_ORE (100+ cr). Jettisoning instantly at the asteroid costs nothing
and keeps cargo space available for valuable minerals.

**`best_sell_market_for_cargo()`** — routing logic:
- Computes aggregate revenue (sum of price × units) for each known market
- Only reroutes if the best alternative market pays >20% more than ASTEROID_BASE
- The 20% threshold prevents constant detours for marginal gains

---

### Market Intelligence

```python
_market_cache: dict[str, dict[str, int]] = {}
_market_cache_ts: dict[str, float] = {}
MARKET_CACHE_TTL = 600  # 10 minutes
```

**`get_market_prices(waypoint)`** returns `{trade_symbol: sell_price}` from cache,
refreshing via API if the cache is stale (>10 min old).

The cache also stores buy prices under `_buy_` prefixed keys for potential future
arbitrage use.

**`discover_markets()`** runs at startup: scans all waypoints in the system for the
`MARKETPLACE` trait and populates `_known_markets`. This prevents hard-coding market
waypoints and adapts to different systems. The system has 85 waypoints across 5 pages
— the function paginates manually using raw `requests` calls to avoid the
`client.get()` wrapper which strips the `meta.total` field needed for pagination.
Without this fix, only the first 20 waypoints were scanned and 25 of 29 markets
were missed.

---

### Navigation

```python
def navigate_to(ship_symbol, destination):
```

Handles both intra-system and inter-system travel:

**Intra-system:**
1. If already there: no-op (handles IN_TRANSIT edge case too)
2. Ensure orbit (ship must be in orbit to navigate)
3. Reset flight mode to CRUISE if changed
4. Call `fleet_api.navigate()`, then `wait_for_ship()`
5. Emergency fallback: if insufficient fuel (code 4203), switch to DRIFT mode

**Inter-system:**
- Jump drive: navigate to local jump gate → `fleet_api.jump()` → navigate to final destination
- Warp drive: directly `fleet_api.warp()`
- Neither: raises SpaceTradersError (ship is stuck)

**`wait_for_ship()`** — adaptive polling:
- Short transits (<120s remaining): polls every 5 seconds
- Long transits (>120s remaining): polls every 30 seconds, logs once per minute
- Prevents console spam during the 1.5+ hour transit for newly purchased ships
- ETA display: `~4m 5s` for >60s, `~1h 29m` for >1h, `~45s` for short hops

**`nearest_refuel_point(from_wp)`** — finds the closest known market using Euclidean
distance on waypoint coordinates. Used so ships don't make unnecessarily long detours
to ASTEROID_BASE when a closer market exists.

---

### Repair System

```python
REPAIR_THRESHOLD = 0.80

def needs_repair(ship) -> bool:
    return any(condition(component) < REPAIR_THRESHOLD
               for component in (frame, engine, reactor))
```

Condition is checked proactively inside each miner's loop on every iteration. If any
component falls below 80%, the miner diverts to the shipyard for repairs before
continuing.

**Why 80%?** In SpaceTraders, degraded components don't affect performance in most
versions of the API, but they can degrade further and eventually cause failures. 80%
gives us a comfortable buffer. Waiting until 0% would mean ships could fail mid-mining.

**`repair_ship()`** checks if the repair cost would breach `CREDIT_RESERVE` before
proceeding. If it would, it logs a warning and skips — better to keep operating with
a worn ship than to go insolvent.

---

### Upgrade System

```python
def upgrade_mining_mounts(ship_symbol):
    # Current tier → target tier
    fleet_api.install_mount(ship_symbol, MINING_MOUNT_TIERS[tier + 1])
```

After each contract completes, `step_upgrade_fleet()` checks every mining ship:
- What's the best mining laser it currently has?
- Is the next tier available at the shipyard?
- Install it

This runs in the main thread after `work_contract()` returns, so it doesn't interfere
with active mining.

---

## Decision Log

A record of specific choices made during development and why.

| Decision | Old Value | New Value | Reason |
|---|---|---|---|
| `CREDIT_RESERVE` | 50,000 | 30,000 | 50k blocked all ship purchases — SURVEYOR costs 32,918 + 50k reserve = 82,918 needed. Lowered to 30k to allow buying once we hit ~63k credits |
| `SHIP_SURVEYOR` score | -1 (never buy) | 75 | Surveyors were wrongly treated as useless. Without them, miners got 0 COPPER_ORE for 20+ consecutive extracts. Score 75 prioritizes surveyor over haulers |
| `MIN_SELL_PRICE` | (didn't exist) | 30 | ICE_WATER (13 cr) and QUARTZ_SAND (18 cr) not worth hauling. Jettison at asteroid to free cargo space immediately |
| `sell_junk()` | Hauled everything to market | Jettison below threshold | Same as above — saves 2 min round-trip per haul cycle of worthless cargo |
| Dead code in `work_contract()` | Called `step_mine_contract()` / `step_deliver_contract()` | Removed | These functions don't exist anywhere in the file. Would cause NameError if contract reached end of delivery loop |
| Fleet manager contract negotiation | COMMAND_SHIP diverted to negotiate | FLEET_MANAGER_SHIP pre-negotiates in background | Keeps command ship mining; next contract ready before current one finishes |
| `ASTEROID` | `X1-HU91-B8` (COMMON_METAL_DEPOSITS) | `X1-HU91-FD5D` (ENGINEERED_ASTEROID) | B8 only yields ores; never dropped refined ALUMINUM needed for contracts. FD5D is 3× closer to K84 delivery point and yields refined metals. Full system map (85 WPs) revealed this |
| `ASTEROID_BASE` | `X1-HU91-B7` (350 units from FD5D) | `X1-HU91-H52` (38 units from FD5D) | H52 has MARKETPLACE + SHIPYARD and is the closest full-service base to the new asteroid |
| `MIN_BUY_CREDITS` | (didn't exist — always attempted) | 120,000 | Fleet manager was bouncing to shipyard every 2 min with ~60k credits, wasting fuel. Parks idle at A1 until 120k |
| `SHIPYARD_WPS` | `[H52]` single shipyard | `[H52, A2]` | A2 has SHIP_LIGHT_SHUTTLE at 86,575 cr — cheaper than H52 options. Looping multiple shipyards finds the best deal |
| Market pagination | `universe_api.get_waypoints()` (only returned first 20) | Raw paginated requests in `discover_markets()` | `client.get()` strips the `meta` envelope, breaking the pagination loop in `universe.py`. Fix: paginate directly. Result: 29 markets found vs 4 before |
| Transit log format | `~{secs}s` always | `~Xm Ys` / `~Xh Ym` | Long drifts (30+ min) logged as `~1820s` — unreadable. Now shows `~30m 20s` or `~1h 29m` |
| Preflight fuel check | `if not at_asteroid OR fuel < 50%` | `if not at_asteroid AND fuel < 50%` | OR logic triggered refuel even when ship was already at FD5D with full tank. AND only triggers when both conditions are true |
| `negotiate_contract` dock bug | `ensure_orbit` before negotiating | `ensure_docked` | SpaceTraders API requires ship to be DOCKED to negotiate (error 4244). Was orbiting instead |
| `strategy.json` | (didn't exist) | Mode + notes + target contract | Shared state file lets the MCP advisor override script behavior without code changes. Modes: `contract_grind`, `fleet_expansion`, `upgrade_first`, `idle` |
| MCP server | (didn't exist) | `mcp_server.py` with 8 tools | Exposes game state and strategy to AI advisor via VS Code MCP integration. Tools: `get_situation`, `get_market_prices`, `get_shipyard`, `analyze_contract_value`, `get_upgrade_analysis`, `get_strategy`, `set_strategy`, `negotiate_new_contract` |

---

## Rebuild Plan

Use this if the agent is reset, the script is lost, or you need to start fresh
on Monday's competition.

### Step 1 — Environment Setup

```bash
# 1. Create project folder
mkdir "space game" && cd "space game"

# 2. Create Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install python-dotenv rich requests
```

### Step 2 — SpaceTraders Account

1. Go to https://spacetraders.io or use the API directly
2. Register a new agent:
   ```bash
   curl -X POST https://api.spacetraders.io/v2/register \
     -H "Content-Type: application/json" \
     -d '{"symbol":"YOURAGENT","faction":"COSMIC"}'
   ```
3. Save the returned `token` — **you only get it once**
4. Create `.env` in the project folder:
   ```
   SPACETRADERS_TOKEN=your_token_here
   ```

### Step 3 — Identify Your Starting System

After registration, call:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://api.spacetraders.io/v2/my/ships
```

Look at your command ship's `nav.systemSymbol` (e.g., `X1-HU91`).
That goes in `SYSTEM`.

Your ship symbol (e.g., `YOURAGENT-1`) goes in `COMMAND_SHIP`.

### Step 4 — Find Your Asteroid & Markets

```bash
# List all waypoints in your system
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://api.spacetraders.io/v2/systems/X1-HU91/waypoints?limit=20"
```

Look for:
- Waypoint with `type: ASTEROID` and trait `COMMON_METAL_DEPOSITS` → `ASTEROID`
- Waypoint near the asteroid with trait `MARKETPLACE` → `ASTEROID_BASE`
- Waypoint with trait `SHIPYARD` → `SHIPYARD_WP`
- Waypoint that is the starting planet/station (faction HQ) → used in `get_next_contract()`
  and `_bg_negotiate_contract()` (hardcoded as `"X1-HU91-A1"` — update this if different)

### Step 5 — Update Config in `play.py`

```python
SYSTEM          = "X1-XXXX"         # your system
COMMAND_SHIP    = "YOURAGENT-1"      # your first ship symbol
ASTEROID        = "X1-XXXX-FD5D"    # ENGINEERED_ASTEROID (center cluster preferred)
ASTEROID_BASE   = "X1-XXXX-H52"     # nearest market to asteroid (MARKETPLACE + SHIPYARD)
SHIPYARD_WP     = "X1-XXXX-H52"     # shipyard waypoint (often same as ASTEROID_BASE)
SHIPYARD_WPS    = ["X1-XXXX-H52", "X1-XXXX-A2"]  # all known shipyards
FLEET_MANAGER_SHIP = "YOURAGENT-2"  # second ship (non-miner)
CREDIT_RESERVE  = 30_000
MIN_BUY_CREDITS = 120_000
MIN_SELL_PRICE  = 30
```

Also update the hardcoded `"X1-HU91-A1"` in two places:
- `get_next_contract()` (line ~1140)
- `_bg_negotiate_contract()` (line ~940)

Replace both with your faction HQ waypoint.

**Finding the best asteroid:** Run a full system waypoint scan (all pages), then look for:
- `ENGINEERED_ASTEROID` type with `COMMON_METAL_DEPOSITS` trait — these yield refined metals
- Compare distance from the asteroid to your contract delivery waypoints
- Prefer asteroids with nearby markets (within 50 units) to keep round-trip time low

The `discover_markets()` function does this automatically at startup — check its output
to find the 29+ markets and identify the best cluster.

### Step 6 — Get Your Second Ship

On day 1 you only have COMMAND_SHIP. FLEET_MANAGER_SHIP (`YOURAGENT-2`) must be
purchased before the fleet manager can operate.

Either:
- Let the script buy it automatically (it will use COMMAND_SHIP for fleet ops until
  YOURAGENT-2 exists — this causes some mining interruption), OR
- Manually buy a SHIP_COMMAND_FRIGATE from the shipyard before starting:
  ```bash
  curl -X POST -H "Authorization: Bearer TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"shipType":"SHIP_COMMAND_FRIGATE","waypointSymbol":"X1-XXXX-H52"}' \
    https://api.spacetraders.io/v2/my/ships
  ```

### Step 7 — Run the Script

```bash
cd "space game"
source .venv/bin/activate
python play.py
```

### Step 8 — What to Watch For

| Event | What It Means |
|---|---|
| `Found survey with Nx COPPER_ORE` | Surveyor is working; miners will use this |
| `Fleet manager: can't afford SHIP_X` | Not enough credits yet — normal |
| `Fleet manager: skipping SHIP_X — fuel tank too small` | That ship type has too small a tank for the route — ignore, it's filtered correctly |
| `Jettisoned Nx ICE_WATER` | Working as intended — cheap junk discarded at asteroid |
| `🏆 Contract fulfilled!` | Contract done; new one will be negotiated automatically |
| `📋 Pre-negotiated contract` | Fleet manager queued next contract in advance |
| `🔧 needs repair` | Ship diverted to shipyard — normal if running for hours |

### Step 9 — Key First-Day Priority

On competition day, the first 2 hours will have ships 3/4/5 still in transit.
During that time:
1. TYLERMASTERY-1 is the only active miner
2. No surveyor yet → 0 copper ore is normal
3. Once ships arrive: surveyor (TYLERMASTERY-4) goes to asteroid, survey pool fills,
   copper ore yield jumps dramatically
4. Credits accumulate; fleet manager buys more ships as soon as credits exceed
   `ship_cost + CREDIT_RESERVE`

**Target progression:**
- 0-2 hrs: 1 miner grinding, accumulating credits
- 2 hrs: ships 3/4/5 arrive — surveyor online, 3 miners active
- 4-6 hrs: enough credits to buy MINING_DRONE (~42k) or SURVEYOR (~33k) — fleet grows
- 12+ hrs: fleet of 6-8 ships running concurrently

### Strategy File

`strategy.json` is a shared state file that lets the MCP advisor (or manual edits)
influence script behavior without restarting:

```json
{
  "mode": "contract_grind",
  "notes": "Human-readable description of current goal",
  "target_contract_id": null
}
```

**Modes:**

| Mode | Behavior |
|---|---|
| `contract_grind` | Normal operation — mine and deliver contracts |
| `fleet_expansion` | Prioritize buying ships; skip upgrades to preserve credits |
| `upgrade_first` | Skip ship purchases until upgrades are installed |
| `idle` | Pause all mining; main loop sleeps 60s between checks |

The main loop reads `strategy.json` at the top of every iteration. Changes take
effect on the next loop (after the current contract finishes), not mid-contract.

---

### MCP Server

`mcp_server.py` exposes game state to an AI advisor via VS Code's MCP integration.
Registered in `.vscode/mcp.json` as `spacetraders-advisor`.

**Available tools:**

| Tool | What It Does |
|---|---|
| `get_situation()` | Credits, ships, active contract, delivery progress |
| `get_market_prices(waypoint)` | Live market prices at a waypoint |
| `get_shipyard(waypoint)` | Available ships and prices at a shipyard |
| `analyze_contract_value()` | cr/hr estimate for current contract |
| `get_upgrade_analysis()` | Which ships can be upgraded and cost |
| `get_strategy()` | Read current strategy.json |
| `set_strategy(mode, notes)` | Write new strategy (affects next loop) |
| `negotiate_new_contract()` | Trigger FLEET_MANAGER_SHIP to negotiate |

The MCP server is read-only for most tools and only mutates `strategy.json` via
`set_strategy`. It does not directly control ship movements.

---

### Module Summary

| File | Purpose |
|---|---|
| `play.py` | Main script — all logic lives here |
| `client.py` | HTTP client; handles auth header, rate limiting |
| `fleet.py` | Thin wrappers for ship endpoints (navigate, dock, extract, survey, etc.) |
| `contracts.py` | Contract endpoints (get, accept, deliver, fulfill) |
| `universe.py` | Universe endpoints (waypoints, markets, shipyards) — pagination fixed |
| `agent.py` | Agent endpoints (get credits, agent info) |
| `mcp_server.py` | MCP server exposing game state to AI advisor |
| `strategy.json` | Shared state file; controls script mode between loops |
| `.vscode/mcp.json` | Registers `spacetraders-advisor` MCP server in VS Code |
| `.env` | `SPACETRADERS_TOKEN=...` — never commit this |
