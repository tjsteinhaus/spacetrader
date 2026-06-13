# Mining Team

## Goal

Extract ore from asteroids continuously and convert it to credits as fast as possible.
Contract delivery is opportunistic — never block cargo flow for it.

## Ship Requirements

| Role | Required mount | Required module |
|------|---------------|-----------------|
| Worker | `MOUNT_MINING_LASER_I` or better | `MODULE_MINERAL_PROCESSOR_I` (for refining) |
| Hauler | None | `MODULE_CARGO_HOLD_II` or better |

## Worker Loop (`miner_loop` in group mode)

```
1. Navigate to assigned asteroid (ASTEROID constant, auto-configured)
2. Loop:
   a. Anti-clog: jettison any goods with no known buyer and price < MIN_SELL_PRICE (30 cr/u)
   b. If cargo full → set ready event, wait for hauler pickup
   c. Extract ore (with survey if available, else raw)
   d. Wait cooldown
```

Workers do NOT self-deliver when in a group. They wait for the hauler.

## Hauler Loop (`miner_hauler_loop`)

```
1. Navigate to ASTEROID, orbit
2. Loop:
   a. Check each worker's ready event
   b. If worker is ready:
      - Navigate to worker's waypoint
      - Transfer all cargo from worker
      - Clear worker's ready event (worker resumes)
   c. If hauler full OR no workers have cargo:
      - Navigate to ASTEROID_BASE
      - refine_cargo_for_sale() — refine profitable ores
      - sell_junk() — sell everything remaining
      - Return to ASTEROID
```

## Refining Step

Before selling, the hauler checks each ore against the refine map:

| Raw ore | Refined output | Module needed |
|---------|---------------|---------------|
| IRON_ORE | IRON | MINERAL_PROCESSOR_I |
| COPPER_ORE | COPPER | MINERAL_PROCESSOR_I |
| ALUMINUM_ORE | ALUMINUM | MINERAL_PROCESSOR_I |
| SILVER_ORE | SILVER | MINERAL_PROCESSOR_I |
| GOLD_ORE | GOLD | MINERAL_PROCESSOR_I |
| PLATINUM_ORE | PLATINUM | MINERAL_PROCESSOR_I |
| URANITE_ORE | URANITE | MINERAL_PROCESSOR_I |
| MERITIUM_ORE | MERITIUM | MINERAL_PROCESSOR_I |

Refining only happens if:
- The ship has `MODULE_MINERAL_PROCESSOR_I`
- The market buys the refined good at a higher price than the raw ore (or raw price is unknown)

## Anti-Clog Policy

Before signaling the hauler, workers jettison goods that are:
- Not in any known market's buy list (`_good_buyers`)
- AND best known sell price < `MIN_SELL_PRICE` (30 cr/u)

This prevents worthless minerals (e.g. SILICON_CRYSTALS with no nearby buyer) from
consuming hold space and reducing extraction efficiency.

## Asteroid Selection

The bot auto-selects the best asteroid via `choose_mining_target()`:
- Scores asteroids by deposit traits (PRECIOUS_METAL_DEPOSITS > RARE_METAL_DEPOSITS > COMMON_METAL_DEPOSITS)
- Prefers asteroids near a fuel market (safety for small-tank ships)
- Stored in DB as `ASTEROID` key, updated when a better asteroid is found

## Surveyor Support

Surveyor ships continuously survey the asteroid and publish results to `_shared_surveys`.
Miners consume surveys to get better extraction yields. If no survey is available,
miners extract without one (lower yield but no downtime).
