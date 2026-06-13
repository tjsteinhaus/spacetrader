# Plans Overview — v2

This folder documents the strategy and implementation plans for the v2 async bot.
Each file covers one domain. See `orchestrator.py` for the runtime entry point.

## Key Difference from v1

v2 is **fully async** (asyncio, no threading). Each ship runs as an independent
`asyncio.Task` coroutine. There are no thread locks — coordination uses
`asyncio.Event` and `asyncio.Lock` instead.

## Files

| File | Description |
|------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Orchestrator, async task model, startup flow, role lifecycle |
| [STRATEGY.md](STRATEGY.md) | Strategy protocol, role assignment logic, contract selection |
| [MINER_ROLE.md](MINER_ROLE.md) | MinerRole loop: asteroid selection, survey-assisted extraction, delivery, buy fallback |
| [SIPHONER_ROLE.md](SIPHONER_ROLE.md) | SiphonerRole loop: gas giant siphoning, sell-and-return cycle |
| [FLEET_PURCHASE.md](FLEET_PURCHASE.md) | FleetManagerRole: ship buying, mount upgrades, repair, market scanning |
| [CARGO_POLICY.md](CARGO_POLICY.md) | Universal cargo decision tree: deliver → sell → jettison |
| [STRATEGY.md](STRATEGY.md) | ContractGrindStrategy, IdleStrategy, role override pinning |

## Module Map

| File | Purpose |
|------|---------|
| `run.py` | Entry point — creates `Orchestrator`, calls `asyncio.run(orch.run())` |
| `orchestrator.py` | Central loop: startup → select contract → assign roles → wait for done |
| `strategy.py` | Protocol + implementations for contract selection and role assignment |
| `config.py` | `Config` dataclass — auto-detects system, asteroid, base from DB/API |
| `constants.py` | `SHIP_SCORES`, `MINING_MOUNT_TIERS`, `HAULER_DEPART_FRACTION`, etc. |
| `navigation.py` | `Navigator` — navigate_with_refuel, wait_arrival, wait_cooldown, can_reach |
| `market.py` | `MarketIntelligence` — price cache, best_sell_market, best_buy_waypoint |
| `surveys.py` | `SurveyPool` — async-safe shared survey cache for miners/surveyors |
| `db.py` | SQLite helpers: waypoints, market listings, prices, transactions, surveys |
| `models.py` | Pydantic v2 schemas for all SpaceTraders API response shapes |
| `roles/miner.py` | `MinerRole` |
| `roles/hauler.py` | `HaulerRole` |
| `roles/siphoner.py` | `SiphonerRole` |
| `roles/surveyor.py` | `SurveyorRole` |
| `roles/trader.py` | `TraderRole` (arbitrage when no contract) |
| `roles/explorer.py` | `ExplorerRole` (jump/warp ships) |
| `roles/fleet_manager.py` | `FleetManagerRole` (background maintenance + buying) |
| `roles/base.py` | `BaseRole` ABC + `ContractContext` dataclass |

## Key Constants (`constants.py`)

```python
SHIP_SCORES = {
    "SHIP_LIGHT_HAULER":    100,   # top priority
    "SHIP_ORE_HOUND":        65,
    "SHIP_MINING_DRONE":     60,
    "SHIP_SIPHON_DRONE":     55,
    # everything else: -1 (never buy)
}

MINING_MOUNT_TIERS = [
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]

HAULER_DEPART_FRACTION  = 0.50   # depart when ≥50% full
HAULER_MAX_WAIT_SECS    = 300    # depart after 5 min regardless of cargo
HAULER_MIN_CONTRACT_UNITS = 30   # depart if ≥30 contract units accumulated
```

## Config Constants (`config.py`)

```python
credit_reserve        = 500_000   # never spend below this
min_buy_credits       = 600_000   # fleet manager activates above this
repair_threshold      = 0.40      # repair when any component < 40% condition
min_sell_price        = 30        # jettison goods below this (cr/u)
cheap_buy_threshold   = 200       # switch to direct-buy if market price ≤ this
dry_extract_threshold = 10        # switch to buy mode after 10 consecutive miss-extractions
```

## System Info (auto-detected at startup)

- Agent: TYLERMASTERY2 (or current agent symbol from `.env`)
- System: auto-detected from agent HQ
- Asteroid: scored and selected by `AsteroidCache` in `roles/miner.py`
- Base: nearest ASTEROID_BASE to detected asteroid
- Gas Giant: nearest GAS_GIANT to base (for siphoners)
