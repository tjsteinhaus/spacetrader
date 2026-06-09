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
  - [Weighted Systems](#weighted-systems)
    - [Weighted Mining Target (`choose_mining_target`)](#weighted-mining-target-choose_mining_target)
    - [Weighted Contract Selection (`_contract_score`)](#weighted-contract-selection-_contract_score)
    - [Distance-Adjusted Sell Routing (`best_sell_market_for_cargo`)](#distance-adjusted-sell-routing-best_sell_market_for_cargo)
    - [Dynamic Ship Buy Priority (`ship_score`)](#dynamic-ship-buy-priority-ship_score)
    - [Deferred: Surveyor Targeting (Item 4)](#deferred-surveyor-targeting-item-4)
    - [Deferred: Buy/Sell Waypoint Net Revenue (Item 5)](#deferred-buysell-waypoint-net-revenue-item-5)
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
| `SELL_ROUTING_DIST_COST` | `20` | cr per distance unit used when comparing remote sell markets. A round trip costs `distance × 2 × 20` cr. Keeps miners from chasing a 500 cr premium that costs 3,000 cr in travel. |

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

Ship scoring is **dynamic** — `ship_score(type, miners, surveyors, haulers)` adjusts each
ship's value based on current fleet composition rather than using static numbers.

```python
# Base scores (before fleet-composition adjustments)
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

**Dynamic rules applied on top of base scores:**

| Condition | Effect |
|---|---|
| Fewer than 2 miners | Surveyor returns −1 (don't buy before you can mine) |
| Already have 1+ surveyor | Surveyor returns −1 (one is enough) |
| Fewer than 2 miners | Hauler returns −1 (nothing to haul yet) |
| Already have 1+ hauler | Hauler returns −1 (one is enough for now) |
| 3+ miners | Hauler gets `base + (miners − 2) × 15` bonus — delivery trips waste more miner-time as fleet grows |
| 5+ miners | Miner score drops by 30 — diminishing returns on raw extraction |

**Example at 4 miners, 0 surveyors, 0 haulers:**
- ORE_HOUND: 100 (best miner)
- MINING_DRONE: 90
- LIGHT_HAULER: 60 + (4−2)×15 = **90** (matches drone — delivery ROI is high)
- SURVEYOR: −1 (blocked: already have 1)
- Effective priority: ORE_HOUND → tie between DRONE/HAULER based on price

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
        # Score all pending contracts and pick the best one
        return max(pending, key=_contract_score)
    # Navigate COMMAND_SHIP to faction HQ and negotiate
```

**At startup or after contract completion**, the script checks for any unfulfilled
contracts. If none exist, COMMAND_SHIP navigates to the faction headquarters
(`X1-KU6-A1`) to negotiate a new one.

**Contract scoring (`_contract_score`):** When multiple unaccepted contracts are
available, the script doesn't just take the highest raw payout — it scores each one
based on how well-suited the fleet is to complete it quickly. See the
[Weighted Contract Selection](#weighted-contract-selection-_contract_score) section
for the full formula.

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
0. Choose mining target: choose_mining_target(ship_symbol, contract)
1. Preflight: navigate to ASTEROID_BASE for fuel if far away or tank < 50%
2. Navigate to mining_target, enter orbit
3. Get a survey from the shared pool (or survey independently if at non-default asteroid)
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

**Mining target selection (`choose_mining_target`):** Before navigating anywhere, each
miner calls `choose_mining_target()` to find the optimal asteroid for the current
contract. It scores all known asteroids and returns the best one. See the
[Weighted Mining Target](#weighted-mining-target-choose_mining_target) section for
the full scoring formula.

**Shared survey pool guard:** Surveys are tied to a specific asteroid. If a miner is
routed to a non-default asteroid, it uses `try_survey()` instead of the shared pool
(which contains surveys for the default `ASTEROID`). This prevents miners from
wasting survey attempts on the wrong location.

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

**Buy-from-market fallback (`_empty_loads`):** Some contract goods (e.g. ALUMINUM)
never drop from asteroid mining — only the ore variant (ALUMINUM_ORE) does. Refined
metals must be purchased from a market. The miner tracks consecutive full cargo loads
with zero contract good using `_empty_loads`. After 2 such loads it switches to the
buy path:

```
1. Check _good_exporters for a market that sells the contract good
2. Navigate to that market (best_buy_waypoint)
3. Bust the market cache (force fresh API call while docked)
4. Read purchase price from cache
5. Buy as many units as: min(free_cargo, affordable_with_reserve)
6. Navigate to delivery waypoint and deliver
7. Reset _empty_loads = 0, continue mining
```

**Why bust the cache before buying?** `best_sell_waypoint` is called for all 29 markets
while the ship is at H52 (not H51). Without a ship at H51, the API returns empty
`tradeGoods`, which would zero out the H51 cache. By calling
`_market_cache_ts.pop(_buy_wp, None)` before `get_market_prices(_buy_wp)`, we force
a fresh API call while the ship is actually docked — guaranteeing real prices.

In X1-HU91, ALUMINUM is bought from **H51** at ~159 cr/unit (exports it). H51 is at
(36, 27) — same location as H52, ~15s hop.

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
- Computes aggregate net revenue for each known market using continuous distance-adjusted scoring:
  ```
  net_value = raw_revenue − (round_trip_distance × 2 × SELL_ROUTING_DIST_COST)
  ```
- A remote market only wins if its net value after travel cost exceeds the base cluster's raw revenue
- `SELL_ROUTING_DIST_COST = 20 cr/unit` prevents chasing marginal gains that don't offset fuel + time
- Only routes to remote markets that stock FUEL (otherwise the miner could get stranded)
- Logs a breakdown (`raw: X cr, travel cost: Y cr, net: Z cr`) when routing away from base

See [Distance-Adjusted Sell Routing](#distance-adjusted-sell-routing-best_sell_market_for_cargo)
for the full explanation.

---

### Market Intelligence

```python
_market_cache: dict[str, dict[str, int]] = {}
_market_cache_ts: dict[str, float] = {}
MARKET_CACHE_TTL = 600  # 10 minutes
_good_exporters: dict[str, list[str]] = {}  # markets that sell a good (exports + exchange)
_good_buyers:    dict[str, list[str]] = {}  # markets that buy a good (imports)
```

**`get_market_prices(waypoint)`** returns `{trade_symbol: sell_price}` from cache,
refreshing via API if the cache is stale (>10 min old).

The SpaceTraders API only returns `tradeGoods` (with prices) when a ship is physically
docked at the waypoint. Without a ship present the API returns only
`imports`/`exports`/`exchange` category lists (no prices). **Critical fix:** the cache
is only overwritten when real price data is returned. If `tradeGoods` is empty (no
ship present), the existing cached prices are preserved. This prevents a scenario where
`sell_junk` calls `get_market_prices` for all 29 markets from a ship docked at H52,
gets empty data for H51/H53 (no ship there), and overwrites valid cached prices with
`{}` — causing ore to be jettisoned even though H51 imports it.

The cache also stores buy prices under `_buy_` prefixed keys, used by the
buy-from-market path.

**`scan_good_sources()`** runs at startup after `discover_markets()`. Calls the public
market endpoint for all 29 known markets (no ship required) and builds two dicts:
- `_good_exporters` — which markets export or exchange each good (used by `best_buy_waypoint`)
- `_good_buyers` — which markets import each good (used by `sell_junk` to route ore sales)

In X1-HU91, key buyers discovered:
- **H51** (same coordinates as H52, ~15s hop): imports ALUMINUM_ORE, COPPER_ORE, IRON_ORE
- **H53** (same coordinates as H52, ~15s hop): exchange for ICE_WATER, QUARTZ_SAND, SILICON_CRYSTALS
- **B7** (312 units away): exchange for all mined goods — too far for routine selling

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

## Weighted Systems

In Phase 2 of development, four key decision points in `play.py` were upgraded from
simple heuristics to fully weighted scoring functions. Each system considers multiple
real factors (distance, fuel cost, fleet composition, deposit traits) and picks the
option with the best overall score — not just the first acceptable one.

---

### Weighted Mining Target (`choose_mining_target`)

**Problem before:** Every miner always went to the global `ASTEROID` constant,
regardless of the contract. A PLATINUM_ORE contract would send miners to a
COMMON_METAL_DEPOSITS asteroid, which yields no platinum at all.

**How it works:**

```python
mining_target = choose_mining_target(ship_symbol, contract)
```

Called once at miner thread startup. Scores every known asteroid in the cache and
returns the waypoint symbol of the best one for this miner and contract.

**Scoring formula per asteroid:**

```
score = trait_score                          # base quality of the asteroid's deposits
      + 80   (if deposit trait matches contract good)
      + 20   (if round_trip ≤ 50% of fuel capacity)
      - 10   (if round_trip > 75% of fuel capacity)
      - 80   (if round_trip > fuel capacity — unreachable without multi-hop)
      + 15   (if ship is already within 100 units)
      - 10   (if ship is more than 300 units away)
      + 10   (if asteroid is within 100 units of the delivery waypoint)
      - 10   (if asteroid is more than 300 units from the delivery waypoint)
```

**Deposit trait scores (`_ASTEROID_TRAIT_SCORES`):**

| Trait | Score | Why |
|---|---|---|
| `PRECIOUS_METAL_DEPOSITS` | 50 | Gold, platinum — highest contract value |
| `COMMON_METAL_DEPOSITS` | 20 | Iron, copper — most contract goods |
| `RARE_METAL_DEPOSITS` | 30 | Uncommon but valuable |
| `MINERAL_DEPOSITS` | 10 | Quartz, silicon — rarely contracted |
| `DEEP_CRATERS` | +25 bonus | Modifies any other trait — higher extraction yield |
| `STRIPPED` | −9999 | Exhausted asteroid — never go here |

**Resource-to-deposit mapping (`GOOD_TO_DEPOSIT_TRAITS`):**

```python
GOOD_TO_DEPOSIT_TRAITS = {
    "IRON_ORE":     frozenset({"COMMON_METAL_DEPOSITS"}),
    "COPPER_ORE":   frozenset({"COMMON_METAL_DEPOSITS"}),
    "GOLD_ORE":     frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "PLATINUM_ORE": frozenset({"RARE_METAL_DEPOSITS", "PRECIOUS_METAL_DEPOSITS"}),
    ...
}
```

**Fuel efficiency factor:** Mining drones have only 80-unit fuel tanks. An asteroid
245 units away requires 123 fuel for a round trip — that's a multi-hop that adds 5+
minutes per delivery cycle. The fuel efficiency penalty makes far asteroids much less
attractive for small ships.

**Top 3 logging:** `choose_mining_target` logs the top 3 scored asteroids with scores,
deposit match status, and fuel distance so you can see exactly why a particular
asteroid was chosen.

**Fallback:** If the cache is empty or the contract good is not mineable (e.g. must be
purchased), falls back to the global `ASTEROID` constant.

---

### Weighted Contract Selection (`_contract_score`)

**Problem before:** `get_next_contract()` just took the first available unaccepted
contract. If a PLATINUM_ORE contract (highest yield per unit) and an ICE_WATER
contract (cheap, far delivery) were both available, it might take ICE_WATER and spend
hours grinding a contract worth 20% less.

**How it works:**

```python
def _contract_score(contract) -> float:
    base = payout / units_remaining          # cr per unit to deliver
    if any asteroid has matching deposit trait:
        base *= 1.20                          # +20%: we can mine this efficiently
    if good is not mineable:
        base *= 0.85                          # -15%: must buy from market, adds overhead
    base -= delivery_distance * 0.3          # penalty for far delivery waypoint
    return base
```

**Why payout-per-unit instead of raw payout?** A 200,000 cr contract for 500 units is
worse than a 120,000 cr contract for 100 units — the first takes 5× longer to
complete. Per-unit normalization makes contracts comparable regardless of size.

**Deposit match bonus (+20%):** If the system has an asteroid with the right deposit
trait for the contract good, miners can go directly to the best asteroid. Without a
match, miners would be at the wrong asteroid and get zero of the needed good.

**Non-mineable penalty (−15%):** Some goods (ALUMINUM, refined metals) never drop
from asteroids — only the ore variant does. Contracts for non-mineable goods require
the buy-from-market fallback path, which adds latency and credit risk.

**`_log_contract_scores()`:** When 2+ unaccepted contracts are available, logs a
formatted table showing each contract's good, units remaining, payout, deposit match
status, and computed score — making it easy to audit why one was chosen over another.

---

### Distance-Adjusted Sell Routing (`best_sell_market_for_cargo`)

**Problem before:** The function used a binary threshold: if the best alternative
market paid >20% AND ≥500 cr more than ASTEROID_BASE, it would route there. This
could send a miner 400 units away for a 600 cr gain while losing 16,000 cr in travel
cost — a net loss of 15,400 cr.

**How it works:**

```
net_value(market) = Σ(price × units for each cargo item at that market)
                  − round_trip_distance × 2 × SELL_ROUTING_DIST_COST
```

`SELL_ROUTING_DIST_COST = 20` cr/unit (tunable constant in `play.py`).

A remote market only wins if its `net_value` exceeds the base cluster's raw revenue.
The base cluster pays zero travel cost (it's always the refuel stop anyway).

**Safety guard:** Only routes to remote markets that stock FUEL. A market 300 units
away with no fuel would leave the miner stranded after selling.

**Log output when rerouting:**
```
[TYLERMASTERY-3] Selling at X1-KU6-F58 (net 4,820 cr vs base 3,100 cr)
  GOLD_ORE: 340 × 12 units = 4,080 cr | travel cost: 246 × 2 × 20 = 9,840 cr
  → Net: -5,760 cr  [BASE WINS]
```

**Why `SELL_ROUTING_DIST_COST = 20`?** A fuel fill at a market costs roughly 100-400
cr depending on tank size and distance. 20 cr/unit × 2 (round trip) × 100 units
(typical one-way distance to a remote market) = 4,000 cr travel overhead — a
reasonable conservative estimate that keeps miners close to base unless the gain
clearly justifies the detour.

---

### Dynamic Ship Buy Priority (`ship_score`)

**Problem before:** Ship purchase priority was a static lookup table. The same scores
applied whether you had 1 miner or 6. This meant the script might keep buying miners
(score 90) even when having a hauler (score 60) would provide much more value — a
fleet of 5 miners without a hauler means every delivery trip is still done by a miner,
wasting extraction time.

**How it works:**

```python
def ship_score(ship_type, miners, surveyors=0, haulers=0) -> int:
```

Returns the base score for `ship_type`, then applies composition-based adjustments:

| Rule | Adjustment |
|---|---|
| Miners < 2 | Surveyor → −1 (never buy before you have miners) |
| Surveyors ≥ 1 | Surveyor → −1 (one is plenty) |
| Miners < 2 | Hauler → −1 (nothing to haul) |
| Haulers ≥ 1 | Hauler → −1 (one covers all delivery needs) |
| Miners ≥ 3 | Hauler gets +`(miners − 2) × 15` bonus |
| Miners ≥ 5 | Any miner gets −30 penalty |

**Hauler value growth example:**

| Miners | Hauler score formula | Result | Best miner score |
|---|---|---|---|
| 2 | −1 (blocked) | −1 | 100 |
| 3 | 60 + (3−2)×15 | 75 | 100 |
| 4 | 60 + (4−2)×15 | 90 | 100 |
| 5 | 60 + (5−2)×15 | 105 | 70 (−30 penalty) |

At 5 miners the hauler beats another miner for the first time, making it the top
priority purchase.

**Why does hauler value grow with miner count?** Each miner that goes on a delivery
trip is a miner not mining. With 5 miners and no hauler, on average 1-2 miners are
always in transit for delivery. A dedicated hauler lets all 5 mine continuously,
effectively adding ~1-2 mining ship equivalents of throughput for much less than the
cost of 2 new mining ships.

---

### Deferred: Surveyor Targeting (Item 4)

**What:** The surveyor always navigates to the global `ASTEROID` constant, regardless
of where the miners have been routed. If miners are sent to `X1-KU6-B49`
(PRECIOUS_METAL_DEPOSITS) but the surveyor is at `X1-KU6-B9` (COMMON_METAL_DEPOSITS),
the surveys are useless — miners are at the wrong asteroid to consume them.

**Planned fix:** On each surveyor loop iteration, determine which asteroid has the
most active miners assigned to it (via `choose_mining_target` records), navigate
there, and survey that asteroid instead.

**Why deferred:** This only matters when `choose_mining_target` routes miners away
from the default `ASTEROID`. In the current system (X1-KU6), the default asteroid is
the best one for most contract goods, so the surveyor is usually correct anyway. Once
fleet size grows and miners routinely split across asteroids, this becomes high value.

**TODO location:** `surveyor_loop()` docstring in `play.py`.

---

### Deferred: Buy/Sell Waypoint Net Revenue (Item 5)

**What:** `best_sell_waypoint()` and `best_buy_waypoint()` currently return the
highest/lowest raw price, ignoring travel distance entirely. This is a narrower
problem than `best_sell_market_for_cargo` (which already uses net-value scoring)
because these functions operate on individual goods rather than full cargo loads.

**Planned fix:** Apply the same `net_revenue = price − distance × SELL_ROUTING_DIST_COST`
formula used in `best_sell_market_for_cargo`.

**Why deferred:** `best_sell_waypoint` is called during `sell_junk` which already
routes through `best_sell_market_for_cargo`. The marginal gain from also adjusting
the individual-good lookup is small. `best_buy_waypoint` matters more but requires
knowing the ship's current location at query time, which adds complexity.

**TODO location:** `best_sell_waypoint()` and `best_buy_waypoint()` docstrings in
`play.py`.

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
| Rate limit retry (429) | Only retried on `Timeout`/`ConnectionError` | Also retries on `SpaceTradersError(code=429)` with exponential backoff | When `scan_good_sources()` made 29 rapid API calls at startup, all 4 miner threads received 429 and crashed. Fix in `client.py`: retry 429 with same backoff as network errors |
| `get_market_prices` cache overwrite | Always wrote `{}` if `tradeGoods` empty | Only overwrites cache when `tradeGoods` is non-empty | Remote API calls without a ship return empty `tradeGoods`. Writing `{}` to cache erased valid H51 prices from a previous docked visit, causing ore to be jettisoned on the next cycle |
| Market cache bust before buying | `get_market_prices(_buy_wp)` used stale cache | `_market_cache_ts.pop(_buy_wp, None)` before the call | `sell_junk` (called at H52) refreshed H51 cache with empty data ~11 min after last H51 visit. 54s later when TM-1 arrived at H51, TTL hadn't expired → cache returned `{}` → `_buy_ALUMINUM = 0` → buy skipped |
| Ore jettisoned at H52 | All ORE < MIN_SELL_PRICE → jettisoned | Check `_good_buyers`; route to H51/H53 if importer known | H51 and H53 are at (36,27) — same coordinates as H52. H51 imports ALUMINUM_ORE/COPPER_ORE/IRON_ORE; H53 trades ICE_WATER/QUARTZ_SAND/SILICON_CRYSTALS. Zero extra travel cost, significant revenue per cycle |
| `scan_good_sources` | Only scanned `exports` + `exchange` | Also scans `imports` → populates `_good_buyers` | `_good_buyers` needed to know where to sell ore without cached prices. Also added to log: `"48 goods indexed, 42 buyable goods"` |
| `_good_exporters` / buy path | (didn't exist) | `scan_good_sources()` + `best_buy_waypoint()` + `_empty_loads` counter | ALUMINUM never drops from COMMON_METAL_DEPOSITS — only ALUMINUM_ORE does. After 2 empty mining loads, miners switch to buying ALUMINUM from H51 (exports it) and delivering directly |
| Mining target routing | All miners go to global `ASTEROID` | `choose_mining_target(ship, contract)` scores all asteroids | Static routing sent miners to a COMMON_METAL asteroid for PLATINUM_ORE contracts. Dynamic routing picks the asteroid with matching deposit traits + best fuel efficiency + proximity |
| Contract selection | `pending[0]` — first available contract | `max(pending, key=_contract_score)` — scored by value | First-available could pick a 200k/500-unit contract over a 120k/100-unit one; per-unit scoring + deposit match bonus + delivery distance penalty makes the better choice obvious |
| Sell market routing | Binary ">20% AND ≥500 cr" threshold | Continuous `net_value = revenue − round_trip × 2 × 20` | Binary threshold could route 400 units away for 600 cr gain (travel costs 16k cr). Net-value scoring makes this impossible — remote only wins when it clearly covers the round-trip cost |
| Ship buy scoring | Static `SHIP_SCORES` lookup | `ship_score(type, miners, surveyors, haulers)` with composition rules | Static scores meant hauler (60) never won over miner (90) even at fleet size 5, leaving all delivery trips done by miners. Dynamic scoring grows hauler value as miner count rises — at 5 miners hauler scores 105, beating any miner |

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
