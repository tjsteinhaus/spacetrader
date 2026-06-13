# Fleet Purchase & Maintenance

## Responsibility

`FleetManagerRole` (`roles/fleet_manager.py`) runs as a background task
independent of the contract cycle. It wakes every 300 seconds and:

1. **Repairs** damaged ships
2. **Upgrades** mining laser mounts to higher tiers
3. **Buys** new ships (when credit budget allows)
4. **Scans** the stalest market

It is assigned to the `cfg.fleet_manager_ship` (typically command ship).

## Activation Conditions

| Action | Condition |
|--------|-----------|
| Repair | Any ship has a component condition < `repair_threshold` (default 0.40) |
| Upgrade mounts | Mining ships with mounts < max available tier |
| Buy ships | Credits > `min_buy_credits` (default 600,000) AND targets not reached |
| Scan market | Always — finds market with prices older than `staleness=7200s` |

## Ship Priority (`SHIP_SCORES`)

Ships with higher scores are bought first. Only ships with score ≥ 0 are
eligible for purchase.

```python
SHIP_SCORES = {
    "SHIP_LIGHT_HAULER":    100,   # highest priority (fast delivery)
    "SHIP_ORE_HOUND":        65,   # good all-around miner
    "SHIP_MINING_DRONE":     60,   # affordable miner
    "SHIP_SIPHON_DRONE":     55,   # gas siphoning
    # all others → -1 (never buy)
}
```

## Ship Targets (`cfg.get_ship_targets()`)

Configured in `strategy.json` under `"ship_targets"`. Defaults (if not set):
```json
{
  "SHIP_ORE_HOUND": 4,
  "SHIP_MINING_DRONE": 6,
  "SHIP_LIGHT_HAULER": 2,
  "SHIP_SIPHON_DRONE": 2
}
```

`_buy_ships()` loops through ships sorted by `SHIP_SCORES` descending,
skips any type already at target count, and purchases one at a time.

## Credit Policy

```python
can_buy(ship_price) → True if:
    agent.credits - ship_price >= cfg.credit_reserve
    and agent.credits >= cfg.min_buy_credits
```

Never purchases if balance would drop below `credit_reserve` (500,000 cr).

## Shipyard Discovery

`cfg.shipyard_wps` is populated during `Config.auto_configure()`:
```python
shipyard_wps = [wp.symbol for wp in system_waypoints
                if "SHIPYARD" in wp.traits]
```

`_buy_ships()` iterates through all shipyard waypoints, queries available ships,
filters by `SHIP_SCORES >= 0`, checks affordability, purchases highest score
available across all shipyards.

## Mount Upgrade Logic (`_upgrade_mounts`)

```python
MINING_MOUNT_TIERS = [
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]
```

1. Query all ships with a mining laser mount
2. Find the **best available tier** any ship already has (or can afford)
3. For each ship with a lower tier than best:
   a. Navigate fleet_manager to the shipyard selling the better mount
   b. Buy the mount
   c. Navigate fleet_manager + target ship to same shipyard
   d. Uninstall old mount, install new mount
4. Repeat until all miners are at the same tier

Helper `_best_mount_tier(ships)` scans all fleet ships and returns the highest
MINING_MOUNT_TIERS index currently owned.

## Repair Logic (`_repair_ships`)

1. Query all ships with any component condition < `repair_threshold`
2. For each damaged ship:
   a. Navigate fleet_manager to nearest shipyard
   b. Call `fleet_api.repair_ship(client, ship_symbol)` → returns new condition
3. Log repair cost (deducted automatically from credits)

Repair is triggered only if the worst component condition falls below threshold.
Full repairs are performed (not partial) — the API repairs all components at once.

## Market Scanning (`_scan_next_market`)

Every 300s cycle, the fleet manager also visits one stale market:

```python
stale_wps = db.get_stale_markets(system, staleness=7200)  # 2hr TTL
if stale_wps:
    target = stale_wps[0]   # stalest first
    navigate to target → dock → market_api.get_market(client, target)
    → db.upsert_market_prices(target, goods)
```

This keeps price data fresh across the system, enabling better `best_sell_market`
decisions for miners and haulers.

## Strategy Integration

Fleet manager does NOT consult the contract strategy. It runs independently.
It can be disabled by setting `cfg.fleet_manager_ship = None` in `config.py`
(or removing `"fleet_manager_ship"` from `strategy.json`).

In that case, ships still self-repair (checked in `BaseRole.preflight_check()`)
but no buying or mount upgrades will occur.
