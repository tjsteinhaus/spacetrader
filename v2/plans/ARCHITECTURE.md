# Architecture — v2

## Runtime Model

v2 is **fully async** — no threads. Each ship runs as an independent
`asyncio.Task`. The orchestrator coordinates all tasks from one event loop.

```
asyncio.run(Orchestrator.run())
    │
    ├─ _heartbeat task (logs every 60s)
    ├─ fleet_manager task (background: repairs, buys, market scans)
    ├─ status_printer task (fleet table every 30s)
    │
    └─ _main_loop()
         │
         ├─ _startup()              ← auto-config, DB warm-start, market scan
         │
         └─ loop forever:
              │
              ├─ strategy.select_contract()   ← accept/negotiate contract
              │
              ├─ _assign_all_ships(ctx)
              │    └─ for each ship: create asyncio.Task(role.run(stop))
              │
              └─ asyncio.wait([ctx.done, stop])
                   └─ on contract done: _cancel_all_tasks() → loop
```

## Startup Sequence (`Orchestrator._startup`)

1. `Config.auto_configure(client)` — reads agent HQ, discovers system, asteroid, base, shipyard
2. `Navigator` + `MarketIntelligence` initialized (share the Config)
3. `MarketIntelligence.warm_start_from_db()` — loads cached prices from SQLite into memory
4. `MarketIntelligence.discover_markets()` — queries API for all waypoints with MARKETPLACE trait

## Contract Cycle

Each iteration of `_main_loop`:

1. `strategy.select_contract()` — returns active unfulfilled contract, or accepts/negotiates a new one
2. Build `ContractContext(contract_id, trade_symbol, destination, units_required, done_event, fulfill_lock)`
3. `_assign_all_ships(ctx)` — cancel stale tasks, fetch fresh ship list, assign roles
4. `asyncio.wait([ctx.done, stop])` — park until contract is fulfilled OR stop is requested
5. On `ctx.done`: cancel all tasks, sleep 10s, loop

## Role Assignment

`strategy.assign_role(ship, contract, cfg)` returns a role name string.
`_build_role(sym, role_name, ctx)` instantiates the correct class.

| Ship mounts/modules | Role assigned |
|---------------------|---------------|
| MOUNT_SURVEYOR_* (no mining) | `surveyor` |
| MOUNT_GAS_SIPHON_* or GAS_* | `siphoner` |
| MODULE_JUMP_DRIVE_* or WARP_DRIVE_* | `explorer` |
| MOUNT_MINING_LASER_* | `miner` |
| Cargo ≥40, no mining/survey mount | `hauler` (contract active) / `trader` (no contract) |
| None of the above | `idle` (no task created) |

Role overrides in `strategy.json` take priority over all logic:
```json
{ "ship_role_overrides": { "TYLERMASTERY2-5": "trader" } }
```

## ContractContext

Shared between all role coroutines working on the same contract.

```python
@dataclass
class ContractContext:
    contract_id:     str
    trade_symbol:    str          # what to mine/deliver
    destination:     str          # delivery waypoint
    units_required:  int
    units_fulfilled: int          # updated in-place by miner/hauler
    done:            asyncio.Event   # set by first role to fulfill
    fulfill_lock:    asyncio.Lock    # prevents double-fulfill
```

Any role that fulfills the contract sets `ctx.done` under `fulfill_lock`.
The orchestrator's `asyncio.wait` unblocks and starts the next cycle.

## Shared Services

All roles receive these at construction (injected by `_build_role`):

| Service | Type | Purpose |
|---------|------|---------|
| `client` | `SpaceTradersClient` | HTTP client with rate limiting + retry |
| `navigator` | `Navigator` | navigate_with_refuel, wait_arrival, can_reach |
| `market` | `MarketIntelligence` | price cache, best_sell_market, best_buy_waypoint |
| `surveys` | `SurveyPool` | shared survey cache (surveyors write, miners read) |
| `config` | `Config` | system constants, thresholds, shipyard WPs |

## Rate Limiting

`SpaceTradersClient` uses a token bucket / retry approach:
- All requests go through `client.get()` / `client.post()` / `client.patch()`
- 429 responses → sleep `retryAfter` seconds → retry
- 5xx responses → exponential backoff
- All tasks share the same client → rate budget is shared naturally

## SurveyPool

`SurveyPool` is an async-safe dict keyed by deposit symbol.

```
SurveyorRole.run():
  surveys = await fleet_api.survey(client, ship)
  await surveys_pool.add(surveys)        ← writes

MinerRole.run():
  best = await surveys_pool.get_best(good)  ← reads
  result = await fleet_api.extract_with_survey(client, ship, best)
  # on 4224 (exhausted) or expiration: pool auto-prunes
```

Expired surveys are removed on access, not on a timer. Thread-safe via
`asyncio.Lock` inside `SurveyPool`.

## Database (SQLite)

`db.py` provides synchronous helpers (wrapped with `asyncio.to_thread` where needed).

Key tables:
| Table | Purpose |
|-------|---------|
| `waypoints` | Cached waypoint data (type, traits, coords) |
| `market_listings` | Which goods each market buys/sells/exchanges |
| `market_prices` | Live prices with `last_updated` timestamp |
| `transactions` | Logged buy/sell events for revenue tracking |
| `surveys` | Cached survey results (non-expired) |
| `contracts` | Active contract snapshots |
| `bot_settings` | Key-value store (system, asteroid, base WPs) |

## Status Table

`_status_loop` prints a colored fleet table every 30 seconds:
```
SHIP                 ROLE         LOCATION           FUEL       CARGO STATUS
─────────────────────────────────────────────────────────────────────────────
TYLERMASTERY2-1      miner        X1-BX78-B12    400/400     32/40  IN_ORBIT
TYLERMASTERY2-2      hauler       X1-BX78-B7     400/400     18/40  DOCKED
TYLERMASTERY2-3      surveyor     X1-BX78-B12    100/100      0/10  IN_ORBIT
```

## Graceful Shutdown

- `Ctrl+C` / `SIGINT` → `asyncio.CancelledError` propagates → `finally` calls
  `_cancel_all_tasks()` + `discord.send_shutdown()`
- `Orchestrator.request_stop()` → sets `_stop` event → loop exits cleanly
  after current task completes

## Key Files

| File | Responsibility |
|------|---------------|
| `orchestrator.py` | The only file that creates/cancels tasks |
| `roles/base.py` | `BaseRole` ABC with `sell_junk`, navigation helpers |
| `roles/miner.py` | Heaviest role: asteroid scoring, extraction, buy fallback, arbitrage fill |
| `roles/fleet_manager.py` | Background: repair, upgrade mounts, buy ships, scan stale markets |
| `navigation.py` | All movement logic; never call `fleet_api.navigate` directly from roles |
| `market.py` | All price/market decisions; cache TTL, best_sell_market routing |
