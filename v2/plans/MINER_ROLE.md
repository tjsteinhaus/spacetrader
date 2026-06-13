# Miner Role

## Goal

Extract the contract good from the best asteroid continuously, deliver it to the
contract destination, and sell junk cargo as a side income. When mining is
unproductive (dry asteroid, non-mineable good, or cheap market price), switch to
direct-buy mode.

## Ship Requirements

| Requirement | Details |
|-------------|---------|
| Mount | `MOUNT_MINING_LASER_I` or better |
| Module | `MODULE_MINERAL_PROCESSOR_I` (optional — for refining) |
| Role name | `"miner"` |
| Class | `roles/miner.py:MinerRole` |

## Asteroid Selection (`AsteroidCache`)

A module-level `AsteroidCache` instance is shared across all miners.
On first call it scores every asteroid in the system:

```python
score(asteroid) =
  sum(ASTEROID_TRAIT_SCORES[trait] for trait in asteroid.traits)
  + 80  if the contract good's deposit trait is present
  + 20  if round-trip to base fits in one tank
  - 80  if round-trip needs 4+ tanks
  + 15  if ship is already within 50 units
  - dist_from_ship * 0.10
  - dist_to_delivery * 0.12   # closer to delivery = faster cycle time
```

`ASTEROID_TRAIT_SCORES` (from `constants.py`):
- `PRECIOUS_METAL_DEPOSITS` → +100
- `RARE_METAL_DEPOSITS` → +80
- `COMMON_METAL_DEPOSITS` → +60
- `MINERAL_DEPOSITS` → +40
- `STRIPPED` → -9999 (excluded)

All miners in the same run target the **same** asteroid (deterministic scoring).
Surveyors use the same `choose_target` logic → they always survey the correct asteroid.

## MinerRole Loop

```
1. populate AsteroidCache (idempotent — runs once across all miners)
2. choose_target(good, ship_pos, fuel_cap, delivery_wp)
3. Preflight:
   a. Refuel at ASTEROID_BASE if < 90% fuel and not already at asteroid
   b. If already holding contract good → deliver it first
4. Decide mode:
   - DIRECT_BUY if: good is non-mineable, OR no deposit in system, OR market price ≤ cheap_buy_threshold
   - MINE otherwise
5. Navigate to mining_target (MINE mode) or asteroid_base (BUY mode)

MINE LOOP:
  a. Refresh survey from shared pool (SurveyPool.get_best(good))
  b. Get ship state
  c. Refuel at base if fuel < 40%
  d. Repair check: if worst component condition < repair_threshold → repair at shipyard
  e. If cargo full or ≥ enough contract good → DELIVER
  f. If 10+ dry extractions → switch to BUY mode
  g. Extract (with survey if available, else raw)
  h. wait_cooldown

DELIVER:
  a. Navigate to ASTEROID_BASE → dock → refuel
  b. Navigate to delivery_wp → dock
  c. deliver_contract(cid, good, min(have, remaining))
  d. If unitsFulfilled >= unitsRequired → fulfill_contract → ctx.done.set()
  e. sell_junk(keep_good=good)
  f. Navigate back to ASTEROID_BASE → refuel → return to mining_target

BUY LOOP (empty_loads >= 3):
  a. Navigate to best_buy_waypoint(good)
  b. Check reachability — skip if can't reach buy market or delivery without drifting
  c. Buy min(free_space, affordable, still_needed) units
  d. Per-transaction limit: start at 10,000, halve on error 4604
  e. Arbitrage fill: if buy mode and spare capacity, buy highest-margin good for delivery_wp
  f. Deliver and return
```

## Survey Coordination

- Miners call `SurveyPool.get_best(good)` to get the best available survey
- On extraction error `4224` (exhausted) or "survey" keyword errors: try next survey in pool, then fall back to `fleet_api.extract`
- Surveys are garbage-collected from the pool on expiration (checked on access)
- Surveyors call `SurveyPool.add(surveys)` which feeds the same pool

## Buy Fallback Trigger

The miner tracks `dry_extractions` (extractions that yielded something other than
the contract good). After `cfg.dry_extract_threshold` (default 10) consecutive
misses, it switches to buy mode if a market sells the good.

This prevents miners from wasting cycles on asteroids with no relevant deposits.

## Direct-Buy Decision

```python
is_mineable = good in MINEABLE_GOODS
no_deposit  = is_mineable and not db.can_be_mined(good, system)

direct_buy = bool(
    buy_wp
    and (
        not is_mineable
        or no_deposit
        or (buy_price > 0 and buy_price <= cfg.cheap_buy_threshold)
    )
)
```

When `direct_buy` is True:
- Ship navigates to buy market, not asteroid
- Reserve ratio is loosened (1/6 of credit_reserve instead of full reserve)
- `_fill_arbitrage()` is called to fill spare capacity with high-margin goods

## Arbitrage Fill (`_fill_arbitrage`)

When in direct-buy mode, after buying the contract good, the miner checks:
1. Get buy prices at current waypoint, sell prices at delivery waypoint
2. Find goods with `sell_price > buy_price`
3. Buy the highest-margin good to fill remaining cargo space
4. These are sold incidentally when `sell_junk` runs after delivery

## Credit Reserves in Buy Mode

| Condition | Reserve applied |
|-----------|----------------|
| Direct-buy mode | `max(5_000, credit_reserve // 6)` |
| Mining mode, need > 5 more units | `credit_reserve` (full) |
| Mining mode, nearly done (≤ 5 remaining) | `max(5_000, credit_reserve // 4)` |

## HaulerRole (companion for mineable contracts)

`roles/hauler.py:HaulerRole` is assigned when the contract good is mineable
AND the ship has cargo ≥ 40 but no mining mount.

```
1. Preflight: refuel at base → navigate to asteroid → orbit
2. WAIT LOOP:
   a. Check cargo: units, have_good, wait time
   b. Depart when:
      - units >= capacity × HAULER_DEPART_FRACTION (50%)
      - have_good >= HAULER_MIN_CONTRACT_UNITS (30) OR cargo_capacity // 4
      - cargo > 0 AND wait >= HAULER_MAX_WAIT_SECS (300s)
3. Depart:
   a. Navigate to ASTEROID_BASE → dock → refuel
   b. Deliver to delivery_wp → dock → deliver_contract
   c. If fulfilled → fulfill_contract → ctx.done.set() → return
   d. sell_junk(keep_good)
   e. Return to ASTEROID_BASE → refuel → return to asteroid
```

`HaulerRole` also has a `_direct_buy_loop` for non-mineable goods (e.g. IRON):
buys from best market, delivers, loops until contract fulfilled.

## State Tracking

`MinerRole` does NOT persist state between restarts. On each `run()` call:
- Re-populates `AsteroidCache` (idempotent)
- Re-checks current cargo for preflight delivery
- Re-evaluates direct-buy vs mine mode
