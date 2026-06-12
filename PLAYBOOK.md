# SpaceTraders Automation Playbook

**Current Agent:** TYLERDEVRUN (system X1-BX78, post-June 2026 reset)  
**Previous Agent:** TYLERMASTERY (system X1-HU91, same codebase)  
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
    - [Hauler Loop](#hauler-loop)
    - [Siphon Loop](#siphon-loop)
    - [Trader Loop](#trader-loop)
    - [Explorer Loop](#explorer-loop)
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
    - [Deferred → Implemented: Surveyor Targeting](#deferred--implemented-surveyor-targeting)
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
│   ├── Thread: miner_loop(TYLERDEVRUN-1)        ← 400 fuel cap; mines + delivers
│   ├── Thread: miner_loop(TYLERDEVRUN-X)        ← each additional miner ship
│   ├── Thread: hauler_loop(TYLERDEVRUN-X)       ← parks at asteroid, accepts transfers, delivers
│   ├── Thread: surveyor_loop(TYLERDEVRUN-X)     ← surveys at active mining asteroid
│   ├── Thread: siphon_loop(TYLERDEVRUN-X)       ← siphons gas giants passively
│   ├── Thread: trader_loop(TYLERDEVRUN-X)       ← buys low / sells high (arbitrage)
│   ├── Thread: explorer_loop(TYLERDEVRUN-X)     ← jumps to nearby systems, scans markets
│   └── Thread: fleet_manager_loop(TYLERDEVRUN-2)
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
- `_active_mining_wp` — set by `choose_mining_target` on the lead miner; surveyors follow this
- `_hauler_symbols` — registered hauler ship symbols; miners offload cargo to haulers at the asteroid
- `_siphoner_symbols` — registered siphon drone symbols
- `_trader_symbols` — registered trader ship symbols
- `_explorer_symbols` — registered explorer ship symbols

---

## Configuration Reference

All tunable constants live at the top of `play.py`. Change these before a new run.

### Waypoints

| Constant | Value | What It Is |
|---|---|---|
| `SYSTEM` | `X1-BX78` | Current star system (post-reset) |
| `ASTEROID` | `X1-BX78-B42` | COMMON_METAL_DEPOSITS; 122 units from B7; chosen by `choose_mining_target` for IRON_ORE |
| `ASTEROID_BASE` | `X1-BX78-B7` | Nearest full market/shipyard to the asteroid |
| `SHIPYARD_WP` | `X1-BX78-A2` | Primary shipyard (at faction HQ) |
| `SHIPYARD_WPS` | `[A2, C45, H56]` | All known shipyards; fleet manager checks all when buying |
| `FACTION_HQ_WP` | `X1-BX78-A1` | Faction HQ — negotiate contracts here |

**Legacy (X1-HU91 / TYLERMASTERY):**
- `ASTEROID` was `X1-HU91-FD5D` (ENGINEERED_ASTEROID with on-site fuel)
- `ASTEROID_BASE` was `X1-HU91-H52` (38 units from FD5D)
- See Decision Log for why those specific choices were made

**Why X1-BX78-B42?** System scan found B42 (COMMON_METAL_DEPOSITS) at 122 units from B7 — well within the 400 fuel cap. The first-run `auto_configure` wrongly saved J86 (DEEP_CRATERS + PRECIOUS_METAL_DEPOSITS, 610 units from B7), which caused all miners to DRIFT. B42 was dynamically selected by `choose_mining_target` once the asteroid cache populated, and has been persisted to DB. See Decision Log for full details.

### Ships

| Constant | Value | What It Is |
|---|---|---|
| `COMMAND_SHIP` | `TYLERDEVRUN-1` | Primary miner (400 fuel cap, mining laser) |
| `FLEET_MANAGER_SHIP` | `TYLERDEVRUN-2` | No mining laser; background fleet ops, contract negotiation |

**Current fleet (X1-BX78):**
- `TYLERDEVRUN-1` — miner, 400 fuel cap; at B7 / mining B42
- `TYLERDEVRUN-2` — fleet manager, 400 fuel cap; patrols shipyards + faction HQ
- `TYLERDEVRUN-3` — surveyor, 80 fuel cap; at B42
- `TYLERDEVRUN-4` — surveyor, 80 fuel cap; at B42

**Note on surveyors:** 80-unit fuel cap surveyors can still reach B42 (122 units from B7) but only barely — they DRIFT if the tank starts below the trip distance. Initial transit from B7 may drift; once at B42 they stay and survey in place.

**Legacy (X1-HU91 / TYLERMASTERY):**
- `COMMAND_SHIP` was `TYLERMASTERY-1`
- `FLEET_MANAGER_SHIP` was `TYLERMASTERY-2`

**Why FLEET_MANAGER_SHIP as fleet manager?** It has no mining laser, so assigning it to mining would waste it. Instead it stays near the shipyard to buy new ships immediately when credits allow, and navigates to the faction HQ to pre-negotiate contracts.

### Economics

| Constant | Value | Rationale |
|---|---|---|
| `CREDIT_RESERVE` | `50_000` | Never spend below this floor — keeps us solvent for fuel, repairs, and emergency buys |
| `MIN_SELL_PRICE` | `30` | cr/unit threshold below which cargo is jettisoned, not hauled |
| `NO_DRIFT_DIST_MAX` | `70` | Max distance (units) from a fuel market for small-tank ships (MINING_DRONE, SIPHON_DRONE) to operate safely without drifting |
| `MIN_BUY_CREDITS` | `100_000` | Fleet manager starts buying once we clear this threshold |
| `SELL_ROUTING_DIST_COST` | `20` | cr per distance unit deducted from remote-market revenue. A round trip costs `distance × 2 × 20` cr. Prevents chasing premiums that don't offset fuel + time. |
| `CHEAP_BUY_THRESHOLD` | `200` | cr/unit — buy a mineable good from the market instead of mining if its purchase price is ≤ this (e.g. trivially cheap ore) |
| `DRY_EXTRACT_THRESHOLD` | `5` | Consecutive extractions yielding zero of the contract good before the miner escalates to buy mode early |
| `MIN_CONTRACT_PAYOUT` | `30_000` | Skip unaccepted contracts with onFulfilled < this and try to negotiate a better one |

**Why 50k reserve?** Ensures we can afford emergency repairs, fuel for the full fleet, and still have slack for price spikes. This is higher than the old 30k value because we now run more ship roles (haulers, traders, explorers) simultaneously, all of which need credits to operate.

**Why MIN_SELL_PRICE = 30?** ICE_WATER sells for 13 cr/unit and QUARTZ_SAND for 18
cr/unit. Hauling low-value goods wastes a miner's time or a hauler's cargo run that could carry contract goods worth 400-600 cr per load. Jettisoning at the asteroid recovers cargo space immediately.

**Why MIN_BUY_CREDITS = 100,000?** At 100k we can afford a SURVEYOR (~33k) or a LIGHT_HAULER (~50-80k) and still have the 50k reserve. The fleet manager parks at the faction HQ until credits clear this threshold to avoid fruitless shipyard trips.

**Why NO_DRIFT_DIST_MAX instead of MIN_FUEL_CAPACITY?** Small-tank ships (80-unit MINING_DRONE, SIPHON_DRONE) are safe to buy if the asteroid or gas giant is within ~70 units of a fuel market — they can refuel without drifting. A blanket 200-unit fuel capacity filter blocked these cheap ships even when the route was trivial.

### Ship Purchase Priority

Ship scoring is **dynamic** — `ship_score(type, miners, surveyors, haulers)` adjusts each
ship's value based on current fleet composition. The **Week 2 strategy** prioritises
infrastructure (surveyor + haulers) before raw extraction capacity.

```python
# Base scores — Week 2 ordering (higher = buy first; -1 = never buy)
SHIP_SCORES = {
    "SHIP_SURVEYOR":        100,  # First buy — boosts all miners immediately
    "SHIP_LIGHT_HAULER":     95,  # Buy all 3 before anything else (big gap enforces order)
    "SHIP_ORE_HOUND":        65,  # Best miner — only after all 3 haulers purchased
    "SHIP_MINING_DRONE":     60,  # Cheap miner — no-drift check required
    "SHIP_SIPHON_DRONE":     55,  # Passive gas — no-drift check required
    "SHIP_HEAVY_FREIGHTER":  -1,  # Never buy
    "SHIP_COMMAND_FRIGATE":  -1,  # Already have one
    "SHIP_GAS_DRONE":        -1,  # Never buy
    "SHIP_LIGHT_SHUTTLE":    -1,  # Never buy — tiny cargo, wrong role
    "SHIP_PROBE":            -1,  # Never buy
}
```

**Dynamic gate rules applied on top of base scores:**

| Ship Type | Gate Condition | Effect |
|---|---|---|
| `SHIP_SURVEYOR` | surveyors ≥ 1 | → −1 (one surveyor is enough) |
| `SHIP_LIGHT_HAULER` | haulers ≥ 3 | → −1 (fleet is fully staffed for hauling) |
| `SHIP_ORE_HOUND` | haulers < 3 | → −1 (must fill all 3 hauler slots first) |
| `SHIP_ORE_HOUND` | miners ≥ 8 | → `max(base − 30, 10)` (diminishing returns) |
| `SHIP_MINING_DRONE` | haulers < 3 | → −1 (same: haulers first) |
| `SHIP_MINING_DRONE` | asteroid too far from fuel market (`_is_mining_drone_safe()`) | → −1 (would DRIFT constantly) |
| `SHIP_MINING_DRONE` | miners ≥ 8 | → `max(base − 30, 10)` |
| `SHIP_SIPHON_DRONE` | haulers < 3 | → −1 |
| `SHIP_SIPHON_DRONE` | siphoners ≥ 2 | → −1 |
| `SHIP_SIPHON_DRONE` | gas giant too far from fuel market (`_is_siphon_reachable()`) | → −1 |

**No-drift gate (`NO_DRIFT_DIST_MAX = 70`):** `_is_mining_drone_safe()` checks the distance from `ASTEROID` to the nearest fuel market. If > 70 units, small-tank drones would need to DRIFT to refuel — rejecting the purchase until the asteroid routing changes.

**Why surveyor first (score 100)?** A single surveyor running continuously keeps the shared survey pool full so all 3+ miners get focused surveys. Without it, extraction is random — 20+ consecutive cycles with zero contract good is not unusual on the wrong deposit.

**Why 3 haulers before more miners (score 95)?** Each miner that leaves the asteroid for a delivery run is a miner not mining. With 3 dedicated haulers, all miners stay at the asteroid continuously. The haulers absorb all cargo transfers via `fleet_api.transfer_cargo()`. At fleet size 4–5 miners, 3 haulers is the sweet spot — more haulers are idle, fewer means miners still deliver.

**Buy-list DB override:** The dashboard can set `ship_buy_list` in the bot_settings DB table to override the hardcoded SHIP_SCORES. Format: `[{"type": "SHIP_SURVEYOR", "max": 1}, ...]`. Empty list = use hardcoded defaults.

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
1. Preflight fuel check:
   - Small-tank ships (≤80 fuel): always top up to 100% before heading out
   - Normal ships: refuel at ASTEROID_BASE if fuel < 90% AND not already at asteroid
2. Preflight delivery shortcut: if ship already carries enough contract goods, deliver now
3. Decide mine vs buy:
   - Non-mineable goods (not in MINEABLE_GOODS) → direct buy path
   - Goods where no scanned asteroid has the matching deposit trait → buy path
   - Goods with market price ≤ CHEAP_BUY_THRESHOLD (200 cr/u) → buy path (trivially cheap)
   - Otherwise: navigate to mining_target and begin mining
4. Mining inner loop:
   a. If haula ship is at asteroid and cargo full → transfer entire cargo to hauler, continue mining
   b. Check fuel — top up if < 60% (small tanks) or < 40% (normal ships), but only when not at asteroid
   c. Check condition — repair at shipyard if < 80%
   d. If cargo nearly full (< 5 free slots) OR have contract good while not at asteroid:
      - If contract good in cargo: navigate to delivery waypoint, deliver
        - If delivery completes contract: fulfill_contract, set contract_done, exit
      - Else: sell junk, reset _empty_loads counter, optionally buy from market
   e. Wait for cooldown
   f. Extract with survey (from shared pool if at default asteroid) or raw extract
   g. Track _dry_extractions — escalate to buy mode after DRY_EXTRACT_THRESHOLD (5) consecutive misses
   h. Repeat
```

**Stationary mining with haulers:** When a hauler is registered at the asteroid
(`_hauler_symbols` non-empty), miners check for an available hauler using
`_get_available_hauler(mining_target)` when their cargo fills. If found, they call
`fleet_api.transfer_cargo()` to offload everything to the hauler, then immediately
continue mining without leaving the asteroid. This eliminates miner delivery trips.

**Small-tank preflight logic:** Ships with ≤ 80-unit fuel caps (`_small_tank = True`)
must start fully fuelled — they can't reach the delivery waypoint or repair yard from
the asteroid on a partial tank. The threshold is 100% for small tanks vs. 90% for
normal ships.

**Direct buy mode (`_direct_buy`):** If the contract good physically cannot drop from
any scanned asteroid (checked via `db.can_be_mined(good, SYSTEM)`), or if it's not in
`MINEABLE_GOODS`, the miner skips the asteroid entirely and goes straight to the
exporter market. On low credits it falls back to mining junk goods for income, then
retries the buy.

**`_dry_extractions` counter:** Each extraction that yields zero of the contract good
increments this counter. After `DRY_EXTRACT_THRESHOLD` (5) consecutive misses, the
miner escalates to buy mode early — treating the situation the same as 3 empty cargo
loads. Resets when the contract good is found.

**`contract_done` event:** The first miner (or hauler) to deliver the final units calls
`fulfill_contract()` under a `_fulfill_lock`. It then sets `contract_done`. All other
miners and the fleet manager see this event and exit their loops.

**Buy-from-market fallback (`_empty_loads`):** After 3 full cargo loads with zero
contract good (or after `DRY_EXTRACT_THRESHOLD` dry extractions), the miner switches to
the buy path. It uses `best_buy_waypoint(good)` to find the cheapest exporter and buys
as many units as `min(free_cargo, affordable_with_reserve)`. Trade volume limits are
respected: a `4604` error causes the batch size to halve and retry.

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

### Hauler Loop

```python
def hauler_loop(ship_symbol, contract, contract_done, stop_event):
```

Dedicated to cargo pickup, delivery, and junk selling — miners never leave the asteroid.

```
1. Preflight: refuel at ASTEROID_BASE, navigate to ASTEROID, enter orbit
2. Loop:
   a. Accept cargo transfers from stationary miners (miners call fleet_api.transfer_cargo)
   b. Departure decision — depart when any of:
      - cargo ≥ 50% full (HAULER_DEPART_FRACTION = 0.50)
      - have ≥ 30 units of the contract good
      - have any cargo AND no new transfers for 5 minutes (HAULER_MAX_WAIT_SECS = 300)
   c. Refuel at ASTEROID_BASE
   d. If carrying contract good: navigate to delivery waypoint, deliver
      - If contract complete: fulfill_contract, set contract_done, exit
   e. sell_junk() for remaining non-contract cargo (routes to best market)
   f. Return to ASTEROID_BASE to refuel, then back to ASTEROID
```

**Why haulers are more efficient than mining delivery trips:** When a miner makes a
delivery round-trip (asteroid → base → delivery → base → asteroid), it's offline for
the entire trip — typically 10–30 min depending on delivery distance. A dedicated
hauler absorbs all those trips while all miners stay at the asteroid extracting 100%
of the time. With 3 haulers and 4+ miners, the effective throughput gain is equivalent
to adding ~1–2 free miners.

**Cargo transfer at the asteroid:** Miners check `_get_available_hauler(mining_target)`:
a hauler orbiting the same waypoint with ≥ 10 cargo slots free. If found, the miner
calls `fleet_api.transfer_cargo()` to offload everything, then continues mining.

---

### Siphon Loop

```python
def siphon_loop(ship_symbol, stop_event):
```

Passive income from gas giants — no contract needed.

```
1. Find all gas giants in the system (from DB, type = GAS_GIANT)
2. Pick the gas giant closest to ASTEROID_BASE
3. Navigate to it (via navigate_with_refuel)
4. Loop:
   a. Wait cooldown
   b. siphon() — yields HYDROCARBON, LIQUID_HYDROGEN, LIQUID_NITROGEN, etc.
   c. When cargo full: sell at best market, return to gas giant
```

**Gate check (`_is_siphon_reachable()`):** Fleet manager only buys a SIPHON_DRONE if
at least one gas giant is within `NO_DRIFT_DIST_MAX` (70 units) of a fuel market. Gas
giants often sit near system centre, far from any market, which would strand small
ships. The gate enforces safety before purchase.

---

### Trader Loop

```python
def trader_loop(ship_symbol, stop_event):
```

Arbitrage: buy low at one market, sell high at another.

**Constants:**
| Constant | Value | Meaning |
|---|---|---|
| `TRADER_MIN_MARGIN` | `150 cr/unit` | Skip opportunities below this buy-sell spread |
| `TRADER_MIN_ROI` | `10%` | Minimum return on investment per trip |
| `TRADER_CREDIT_RESERVE` | `150_000 cr` | Don't spend below this floor when buying cargo |

```
1. Load arbitrage opportunities from DB (db.get_arbitrage_opportunities)
2. Filter out the active contract good (let hauler handle it uncontested)
3. If viable opportunities exist:
   a. Check live buy price at source market (cache bust while docked)
   b. Verify margin still ≥ TRADER_MIN_MARGIN after seeing real price
   c. Re-score in case a better route appeared since the cache refreshed
   d. Buy up to min(cargo_capacity, affordable) units in batches
   e. Pre-sell check at destination: wait up to 2×5min if market is depressed
   f. Sell in batches; stop early if price crashes to <10% of opening price
   g. Backhaul: if the sell waypoint also has a good buy opportunity, buy before
      the empty return trip (eliminates dead-leg travel cost)
4. If no viable opportunities: scout a stale market instead of sitting idle
   - Picks the market with the oldest or missing price data
   - Navigates there, docks, force-refreshes prices
   - Skips unresponsive markets for 2 hours (_scout_skip)
5. Wait 5 minutes between scans if no arbitrage or scouting available
```

**Backhaul logic:** After selling, the trader checks if the destination market has
goods that can be profitably bought and sold elsewhere. If margin ≥ `TRADER_MIN_MARGIN`
and ROI ≥ `TRADER_MIN_ROI`, it loads up and sells at the backhaul destination before
returning home. This turns every round-trip into a two-leg profit opportunity.

**Price crash guard:** During batch sells, if the current price drops to <10% of the
first batch's price (market absorption), the trader stops selling and dumps remaining
stock via `sell_junk()` to avoid a total loss.

**Trip ID logging:** Each buy→sell (and backhaul) run gets a UUID-prefixed `trip_id`
logged in the DB transactions table so profit-per-trip can be reconstructed in analytics.

---

### Explorer Loop

```python
def explorer_loop(ship_symbol, stop_event):
```

Scans nearby systems for price intelligence and contract opportunities.

```
1. Navigate to jump gate in home system
2. Call fleet_api.scan_systems() to discover nearby reachable systems
3. For each system (closest first, up to 5):
   a. Jump to that system's jump gate
   b. Call fleet_api.scan_waypoints() to find markets
   c. For up to 3 markets: get_market() to retrieve export/import/exchange data
   d. Log any ore prices significantly higher than home system (EXPLORER_PRICE_BOOST = 1.30)
   e. Update shared market cache + DB for hauler routing decisions
4. After sweeping all nearby systems: rest EXPLORER_REST_SECS (600s = 10 min)
5. Repeat, resetting visited set
```

**Why explore?** Remote system markets sometimes pay 30-100% more for refined metals
or ores than the home system. An explorer that identifies these opportunities allows
the hauler to route deliveries there for a significant credit bonus. The explorer also
finds arbitrage price pairs that the trader can exploit across systems.

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

**Scoring formula per asteroid (`score_asteroid_for_miner`):**

```
score = trait_score                          # sum of _ASTEROID_TRAIT_SCORES for all traits

# Resource match (highest-weight factor)
+ 80.0  if deposit trait for contract good is present on this asteroid

# Fuel efficiency: round_trip = base_dist × 2 / fuel_capacity (ratio)
+ 20.0  if ratio ≤ 1.0  (fits in one tank — very efficient)
- 10.0  if ratio ≤ 2.0  (one refuel stop each way)
- 35.0  if ratio ≤ 4.0  (two+ refuel stops — costly for small ships)
- 80.0  if ratio  > 4.0  (extremely far; major inefficiency)

# First-trip drift penalty (new):
- 60.0  if (dist_from_ship + base_dist) > fuel_cap
         (ship can't cruise to asteroid AND return to base on current fuel — will drift)

# Distance from ship's current position (initial travel cost)
+ 15.0  if dist_from_ship < 50 units (already nearby)
- 10.0  if dist_from_ship > 200 units (expensive first trip)

# Delivery proximity (minor — asteroid closer to delivery WP = shorter hauler trips)
+ 10.0  if dist_to_delivery_wp < 100 units
- 10.0  if dist_to_delivery_wp > 500 units
```

**Deposit trait scores (`_ASTEROID_TRAIT_SCORES`):**

| Trait | Score | Why |
|---|---|---|
| `PRECIOUS_METAL_DEPOSITS` | 50 | Gold, platinum — highest contract value |
| `RARE_METAL_DEPOSITS` | 40 | Uncommon but valuable |
| `COMMON_METAL_DEPOSITS` | 20 | Iron, copper — most contract goods |
| `DEEP_CRATERS` | 15 | Modifies other traits — higher extraction yield |
| `MINERAL_DEPOSITS` | 10 | Quartz, silicon — rarely contracted |
| `HOLLOWED_INTERIOR` | 5 | Minor quality boost |
| `EXPLOSIVE_GASES` | −5 | Mostly gas products, low ore yield |
| `DEBRIS_CLUSTER` | −5 | Poor deposit quality |
| `UNSTABLE_COMPOSITION` | −5 | Poor deposit quality |
| `RADIOACTIVE` | −10 | Very poor ore content |
| `STRIPPED` | −9999 | Exhausted — never go here |

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
245 units away requires 490 fuel for a round trip — far beyond one tank. The fuel
ratio tiers impose escalating penalties: one refuel stop (ratio ≤ 2.0) only costs -10
points, but two+ stops (ratio ≤ 4.0) cost -35, and extremely far asteroids cost -80.
The **first-trip drift penalty** (-60) is the most important for small ships: if the
ship can't even cruise *to* the asteroid and back on a full tank, it will be forced
to drift the return leg — adding 20–55 minutes per cycle.

**Top 3 logging:** `choose_mining_target` logs the top 3 scored asteroids with their
scores, deposit match status, and base distance so you can see exactly why a particular
asteroid was chosen.

**Persistence:** The winner is written back to the `agent_config` DB table so restarts
use the same asteroid without re-running the scoring.

**Fallback:** If the cache is empty or the contract good is not mineable, falls back to
the global `ASTEROID` constant.

---

### Weighted Contract Selection (`_contract_score`)

**Problem before:** `get_next_contract()` just took the first available unaccepted
contract. If a PLATINUM_ORE contract (highest yield per unit) and an ICE_WATER
contract (cheap, far delivery) were both available, it might take ICE_WATER and spend
hours grinding a contract worth 20% less.

**How it works:**

```python
def _contract_score(contract) -> float:
    payout_per_unit = payout / units_remaining   # cr per unit to deliver

    if any asteroid has matching deposit trait:
        payout_per_unit *= 1.20                  # +20%: we can mine this efficiently

    if good is not mineable:
        payout_per_unit *= 0.85                  # −15%: must buy from market, adds overhead

    payout_per_unit -= delivery_distance * 0.3   # penalty for far delivery waypoint

    return payout_per_unit
```

**`get_next_contract()` priority order:**

1. **Already-accepted contracts** — we're committed; pick highest payout among them
2. **High-value unaccepted contracts** (onFulfilled ≥ `MIN_CONTRACT_PAYOUT = 30,000 cr`), ranked by `_contract_score`
3. **Negotiate a fresh contract** at the faction HQ — trying for a better one
4. **Fall back** to the best-scoring unaccepted contract even if below MIN_CONTRACT_PAYOUT
5. Accept whatever was negotiated if nothing else is available

**Why payout-per-unit instead of raw payout?** A 200,000 cr contract for 500 units is
worse than a 120,000 cr contract for 100 units — the first takes 5× longer to
complete. Per-unit normalization makes contracts comparable regardless of size.

**Deposit match bonus (+20%):** If the system has an asteroid with the right deposit
trait for the contract good, miners can go directly to the best asteroid. Without a
match, miners would be at the wrong asteroid and get zero of the needed good.

**Non-mineable penalty (−15%):** Some goods (ALUMINUM, refined metals) never drop
from asteroids — only the ore variant does. Contracts for non-mineable goods require
the buy-from-market fallback path, which adds latency and credit risk.

**MIN_CONTRACT_PAYOUT gate:** Contracts paying < 30,000 cr on fulfillment are skipped
in favour of negotiating a better one. The script will attempt to negotiate, then fall
back if the negotiated contract is also below threshold.

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
# Cluster markets (≤5 units from ASTEROID_BASE) get ZERO travel penalty —
# we're going there to refuel anyway. e.g. H51 and H53 at the same coordinates as H52.
cluster_val = max revenue among all co-located cluster markets

# Remote markets: must sell FUEL and beat the cluster on net value
net_value(market) = Σ(price × units for each cargo item)
                  − round_trip_distance × 2 × SELL_ROUTING_DIST_COST

# Winner: highest net_value beats cluster_val
```

`SELL_ROUTING_DIST_COST = 20` cr/unit (tunable constant in `play.py`).

**Candidate filtering:** Only markets known to import or exchange our cargo goods are
scored — not all 27 markets. This avoids API calls for markets that clearly won't buy.

**Safety guard:** Only routes to remote markets that stock FUEL. A market 300 units
away with no fuel would leave the miner stranded after selling.

**Log output when rerouting:**
```
Sell routing: X1-BX78-K87 net 4,820 cr (raw 14,660 − 9,840 travel cost) vs cluster 3,100 cr
```

**Why `SELL_ROUTING_DIST_COST = 20`?** A fuel fill at a market costs roughly 100-400
cr depending on tank size and distance. 20 cr/unit × 2 (round trip) × 100 units
(typical one-way distance to a remote market) = 4,000 cr travel overhead — a
reasonable conservative estimate that keeps miners close to base unless the gain
clearly justifies the detour.

---

### Dynamic Ship Buy Priority (`ship_score`)

**Problem before:** Ship purchase priority was a static lookup table and the "dynamic"
rules were based on miner/hauler counts in the old way. The new Week2 strategy flips
the priority entirely: surveyor and haulers come first, miners come last.

**How it works:**

```python
def ship_score(ship_type, current_miner_count, current_surveyor_count, current_hauler_count) -> int:
```

See the [Ship Purchase Priority](#ship-purchase-priority) section for the full base
scores and gate rules. Summary:

| Phase | What to buy | Why |
|---|---|---|
| 0 miners, 0 surveyors | SURVEYOR first | Boosts miners immediately — biggest single ROI |
| 0 haulers | LIGHT_HAULER × 3 | Lock miners to asteroid, eliminate delivery trips |
| 3 haulers, 0 miners | ORE_HOUND or MINING_DRONE | Now miners can stay at asteroid; haulers do all travel |
| 3 haulers, n miners | SIPHON_DRONE (if gas giant reachable) | Passive income; no contract needed |

**No-drift gates:**
- `_is_mining_drone_safe()`: checks `waypoint_distance(ASTEROID, nearest_fuel_market) ≤ NO_DRIFT_DIST_MAX (70)`
- `_is_siphon_reachable()`: same check for any gas giant in the system

---

### Deferred → Implemented: Surveyor Targeting

**What:** The surveyor originally always navigated to the global `ASTEROID` constant,
regardless of where miners were routed. This is now **partially implemented**.

**Current behaviour:** At each iteration of the surveyor loop, the surveyor reads
`_active_mining_wp` (set by `choose_mining_target` on the lead miner) and
repositions to that asteroid if it has changed. The surveyor also validates it can
physically reach the target on one tank before committing (falls back to `ASTEROID`
if the target has no fuel market within range).

**Remaining gap:** If miners are split across multiple asteroids (future fleet
expansion), the surveyor still follows only the single lead miner's target. Full
multi-miner consensus tracking (follow the asteroid with the most miners) is not yet
implemented.

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

## Current Reset: TYLERDEVRUN / X1-BX78

### System Layout

| Waypoint | Type | x | y | Notes |
|---|---|---|---|---|
| X1-BX78-B7 | ASTEROID_BASE | 171 | -301 | Main base — MARKETPLACE + SHIPYARD |
| X1-BX78-B42 | ASTEROID | 74 | -375 | **Active mining asteroid** — COMMON_METAL_DEPOSITS (122 units from B7) |
| X1-BX78-B8 | ASTEROID | ~160 | ~-290 | HOLLOWED_INTERIOR + MINERAL_DEPOSITS (close but poor) |
| X1-BX78-B9 | ASTEROID | ~90 | ~-380 | MINERAL_DEPOSITS only |
| X1-BX78-J86 | ASTEROID | 717 | -29 | DEEP_CRATERS + PRECIOUS_METAL_DEPOSITS — **too far** (610 units from B7, miner drifts) |
| X1-BX78-J62 | ASTEROID_BASE | -698 | -167 | Contract delivery point (medicine contract); 879 units from B7 |
| X1-BX78-H55 | PLANET | 45 | -6 | Previous contract delivery point; MARKETPLACE |
| X1-BX78-A1 | PLANET | 4 | -25 | Faction HQ — negotiate contracts here |
| X1-BX78-A2 | MOON | 4 | -25 | Shipyard #1 (same coords as A1, ~323 units from B7) |
| X1-BX78-C45 | ORBITAL_STATION | ~11 | 116 | Shipyard #2 |
| X1-BX78-H56 | MOON | 45 | -6 | Shipyard #3 |
| X1-BX78-B6 | FUEL_STATION | -39 | -185 | 240 units from B7 — first hop westbound |
| X1-BX78-C46 | FUEL_STATION | 11 | 116 | 447 units from B7 |
| X1-BX78-I60 | FUEL_STATION | -222 | -53 | 465 units from B7 — second hop to J62 |
| X1-BX78-J61 | FUEL_STATION | -582 | -139 | 770 units from B7 — third hop to J62 |
| X1-BX78-I59 | JUMP_GATE | -436 | -104 | 638 units from B7; **active** — connects to X1-HS10, X1-VR75, X1-BY80, X1-HU32 |

### Fuel Hop Chain: B7 → J62 (879 units)

The miner can't CRUISE 879 units in one go (400 fuel cap). The correct multi-hop route:

```
B7 (start, 400 fuel) 
  → B6 (240 units, FUEL_STATION — refuel)
  → I60 (226 units from B6, FUEL_STATION — refuel)
  → J61 (370 units from I60, FUEL_STATION — refuel)
  → J62 (119 units from J61, destination)
```

Each leg is within the 400-unit fuel cap. **This required a bug fix** — see Decision Log.

### MEDICINE Contract (current)

- **Contract:** `cmqb1vckylp7cui6ymqqyxm9z` — PROCUREMENT, accepted, 0/24 MEDICINE → X1-BX78-J62
- **Payout:** 158,600 cr on fulfillment
- **Best source:** X1-BX78-D47 (EXPORT, ~2,379 cr/unit, vol=20, 327 units from B7)
- **All 24 units already in cargo** — just needs delivery via the 4-hop route above

### Known Issues Fixed This Session

1. **Wrong asteroid in DB** (see Decision Log): first-run saved J86 (610 units) instead of B42 (122 units) — every miner leg was a 55-min DRIFT
2. **`navigate_with_refuel` dead-end check** (see Decision Log): was rejecting B6 as an intermediate hop because J62 is >400 units from B6, causing drift to J62 instead of 4-hop cruise
3. **`choose_mining_target` doesn't persist to DB**: now writes winner back to `agent_config` table so restarts don't regress to the initial DB value

### Jump Gate

X1-BX78-I59 is **live** (no construction needed). Connects to 4 systems. Currently unreachable on a single tank from B7 (638 units) but accessible via B6 → I60 → I59 with the fixed multi-hop routing. Useful for a future explorer ship.

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
| DB asteroid wrong on restart (TYLERDEVRUN/X1-BX78) | `auto_configure` saves first-run asteroid (J86, 610 units from B7) | `choose_mining_target` now writes winner back to DB via `db.save_agent_config` | On first run the asteroid cache wasn't populated yet, so `auto_configure` picked J86 (best trait score: DEEP_CRATERS + PRECIOUS_METAL_DEPOSITS = 65 pts). But J86 is 610 units from B7, beyond the miner's 400 fuel cap → every leg was a 55-min DRIFT. `choose_mining_target` dynamically picks B42 (COMMON_METAL_DEPOSITS, 122 units, correct for IRON_ORE) but didn't persist it. Fixed: winner is now written to DB so restarts use the correct asteroid immediately. DB manually patched to B42. |
| `navigate_with_refuel` dead-end check | Rejected intermediate hop if `remaining_dist > fuel_cap` | Removed: allow any hop that makes progress toward destination | Old check required each intermediate fuel market to be reachable from the destination in a single full tank. This rejected B6 (240 units from B7) because B6→J62 = 659 units > 400 fuel cap. Result: direct drift to J62 (879 units, ~55 min) instead of 4-hop cruise (B7→B6→I60→J61→J62, ~20 min total). Fixed: any reachable hop that reduces distance-to-destination is accepted; the 10-iteration loop chains them automatically. |
| `SHIP_SCORES` Week2 reorder | ORE_HOUND=100, MINING_DRONE=90, SURVEYOR=75 (old strategy) | SURVEYOR=100, LIGHT_HAULER=95, ORE_HOUND=65, MINING_DRONE=60, SIPHON_DRONE=55 | Old order bought more miners before infrastructure. With 5 miners and no hauler, all delivery trips blocked miners. New strategy: 1 surveyor + 3 haulers first, then miners. Haulers keep all miners at the asteroid continuously; ROI on 3 haulers exceeds ROI on 3 additional miners at fleet size 4+. |
| `MIN_FUEL_CAPACITY` blanket filter | 200 units (blocked MINING_DRONE with 80-unit tank) | Removed; replaced with `NO_DRIFT_DIST_MAX = 70` | Old filter rejected all small-tank ships regardless of route distance. MINING_DRONEs are safe when the asteroid is ≤70 units from a fuel market. `_is_mining_drone_safe()` checks the actual route before purchase instead of a blanket capacity gate. |
| `CREDIT_RESERVE` | 30,000 | 50,000 | The old 30k value was set specifically to allow SURVEYOR purchase (~33k). With the Week2 fleet strategy (haulers + traders + explorers), we need a larger buffer for simultaneous multi-role operations. 50k covers emergency repairs + fuel for a 6-ship fleet + one market buy. |
| `MIN_BUY_CREDITS` | 120,000 | 100,000 | Lowered because the cheapest useful purchase is now a SURVEYOR (~33k). At 100k we can buy a SURVEYOR or LIGHT_HAULER and retain the 50k reserve. The old 120k threshold was calibrated for a 86k LIGHT_SHUTTLE which is now never purchased. |
| Dedicated hauler loop | (didn't exist — miners delivered themselves) | `hauler_loop()` with stationary miner cargo transfers | Miners now transfer entire cargo to hauler while remaining at the asteroid. Hauler shuttles to delivery + sell markets. Eliminates all miner travel overhead; at 4 miners the effective throughput gain is ~1-1.5 extra miner equivalents. |
| Siphon loop | (didn't exist) | `siphon_loop()` targeting closest gas giant | Passive income stream from HYDROCARBON, LIQUID_HYDROGEN, etc. Guarded by `_is_siphon_reachable()` distance check before purchase. |
| Trader loop | (didn't exist) | `trader_loop()` with arbitrage + backhaul + market scouting | Buys low/sells high using DB-backed arbitrage opportunities. When idle, scouts stale markets to keep price data fresh. Backhaul logic fills the otherwise-empty return leg for additional profit per trip. |
| Explorer loop | (didn't exist) | `explorer_loop()` jumping to nearby systems | Scans remote system markets for price arbitrage leads and higher-paying ore buyers. Updates shared cache so haulers can route to better sell markets. |
| Surveyor follows active mining target | Always navigated to global `ASTEROID` | Reads `_active_mining_wp`; repositions if miners have moved | Eliminates surveys being generated at the wrong asteroid when `choose_mining_target` routes miners elsewhere. Falls back to `ASTEROID` if the target is out of range for the surveyor's fuel cap. |
| `DRY_EXTRACT_THRESHOLD` | (didn't exist — used only `_empty_loads` after 2 loads) | `DRY_EXTRACT_THRESHOLD = 5` consecutive zero-yield extractions | Old system waited for a full cargo load before escalating to buy mode. New threshold triggers early escalation after 5 consecutive misses regardless of cargo level — much faster response when the wrong deposit type is being mined. |
| `MIN_CONTRACT_PAYOUT` | (didn't exist — accepted any contract) | `MIN_CONTRACT_PAYOUT = 30_000` | Prevents wasting fleet capacity on trivial contracts. If onFulfilled < 30k, the script tries to negotiate a better contract before accepting the low-value one. |
| `CHEAP_BUY_THRESHOLD` | (didn't exist) | `CHEAP_BUY_THRESHOLD = 200 cr/unit` | Some mineable goods (e.g. SILICON_CRYSTALS) can be bought for 50 cr/unit — trivially cheaper than the fuel cost to mine them. Below this threshold, miners buy from market instead, freeing the asteroid slot for higher-value extractions. |

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
ASTEROID        = "X1-XXXX-B42"     # closest COMMON_METAL_DEPOSITS within fuel cap
ASTEROID_BASE   = "X1-XXXX-B7"      # nearest market (MARKETPLACE + SHIPYARD)
SHIPYARD_WP     = "X1-XXXX-A2"      # primary shipyard (usually at faction HQ)
SHIPYARD_WPS    = ["X1-XXXX-A2", "X1-XXXX-C45", "X1-XXXX-H56"]  # all known shipyards
FLEET_MANAGER_SHIP = "YOURAGENT-2"  # second ship (non-miner)
FACTION_HQ_WP   = "X1-XXXX-A1"     # faction HQ — for contract negotiation
CREDIT_RESERVE  = 30_000
MIN_BUY_CREDITS = 120_000
MIN_SELL_PRICE  = 30
```

**Critical:** Do NOT just pick the highest-trait-score asteroid (`auto_configure` does this at first run and will pick J86 / 610 units / DRIFT hell). Instead:
1. Run the script once to populate the asteroid cache
2. Check the logs for `choose_mining_target` output — it will show scores for B42 vs J86
3. Verify the DB has the correct ASTEROID value: `SELECT value FROM agent_config WHERE key='ASTEROID'`
4. If wrong, manually update: `UPDATE agent_config SET value='X1-BX78-B42' WHERE callsign='YOURAGENT' AND key='ASTEROID'`

Also update `FACTION_HQ_WP` constant and search for any hardcoded `"X1-HU91-A1"` strings — replace all with your faction HQ waypoint.

**Finding the best asteroid:** Run a full system waypoint scan (all pages), then look for:
- Asteroids with `COMMON_METAL_DEPOSITS` or `ENGINEERED_ASTEROID` type
- Distance from asteroid to B7 must be ≤ miner fuel cap (400 for COMMAND_FRIGATE, 80 for MINING_DRONE)
- Distance from asteroid to contract delivery must be manageable via fuel hop chain
- Prefer asteroids with fuel stations nearby (within 50 units) to keep round-trip time low

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
1. TYLERDEVRUN-1 (or COMMAND_SHIP) is the only active miner
2. Surveyors (TYLERDEVRUN-3/4) may not arrive at B42 for several minutes — no surveys initially
3. Once surveyors arrive: survey pool fills, yield quality improves
4. Credits accumulate; fleet manager buys more ships as soon as credits exceed `ship_cost + CREDIT_RESERVE`

**Watch for the asteroid selection:** First run `auto_configure` may pick a high-trait but far asteroid (e.g., J86 in X1-BX78). Check `choose_mining_target` log output. If it picks a close asteroid (B42), all is well. If miners are DRIFTing (~55 min legs), the wrong asteroid is active — check and fix the DB.

**Target progression:**
- 0-2 hrs: 1 miner grinding, accumulating credits
- 2 hrs: surveyors arrive at B42 — yield improves
- 4-6 hrs: enough credits to buy MINING_DRONE (~42k) or SURVEYOR (~33k) — fleet grows
- 12+ hrs: fleet of 4-6 ships running concurrently

**Fuel hop chains:** Any delivery point more than 400 units away requires the `navigate_with_refuel` multi-hop path. Map the fuel stations between B7 and your delivery point before the run to confirm the chain exists.

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
