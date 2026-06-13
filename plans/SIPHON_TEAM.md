# Siphon Team

## Goal

Extract gas from gas giants continuously and convert it to credits as fast as possible.
Contract delivery is opportunistic — never block cargo flow for it.

## Ship Requirements

| Role | Required mount | Required module |
|------|---------------|-----------------|
| Worker | `MOUNT_GAS_SIPHON_I` or better | `MODULE_GAS_PROCESSOR_I` (for HYDROCARBON→FUEL) |
| Hauler | None | Sufficient cargo + fuel for CRUISE loop to gas giant and back |

## CRUISE Loop Viability Rule

The hauler must be able to run the full loop on a single tank in CRUISE mode:
- `ASTEROID_BASE → gas giant → ASTEROID_BASE`

If the hauler can't reach the gas giant in CRUISE mode, it must DRIFT — which
destroys throughput. Always verify the hauler's fuel capacity before assigning.

## Worker Loop (`siphon_loop` in group mode)

```
1. Navigate to nearest gas giant
2. Loop:
   a. Anti-clog: jettison goods with no buyer and price < MIN_SELL_PRICE
   b. If cargo full → set ready event, wait for hauler pickup
   c. Siphon gas
   d. Wait cooldown
```

Workers do NOT self-deliver when in a group. They stay at the gas giant.

## Hauler Loop (`siphon_hauler_loop`)

```
1. Navigate to gas giant, orbit
2. Loop:
   a. Check each worker's ready event
   b. If worker ready:
      - Transfer cargo from worker
      - Clear ready event (worker resumes)
   c. If hauler full OR no workers have cargo:
      - refuel_from_cargo() — use FUEL in cargo before buying market fuel
      - Navigate to ASTEROID_BASE
      - refine_cargo_for_sale() — refine HYDROCARBON→FUEL if profitable
      - _sell_siphon_goods() — sell everything
      - Return to gas giant
```

## Refining Step

| Raw good | Refined output | Module needed |
|----------|---------------|---------------|
| HYDROCARBON | FUEL | GAS_PROCESSOR_I |

Refining HYDROCARBON→FUEL only happens if:
- The ship has `MODULE_GAS_PROCESSOR_I`
- The market at ASTEROID_BASE buys FUEL at a higher price than raw HYDROCARBON (or raw price unknown)

FUEL produced by refining can also be used to refuel the hauler before the trip home
(`refuel_from_cargo` → sends `{"fromCargo": true}` to the refuel API).

## Fuel Policy

Priority order for hauler refueling:
1. Use FUEL already in cargo (`fromCargo=True`) — free, no market needed
2. Buy FUEL at ASTEROID_BASE market — only if cargo has no FUEL

This is especially valuable when the hauler is also refining HYDROCARBON→FUEL,
as it effectively self-fuels part of each run.

## Anti-Clog Policy

Workers jettison goods where:
- No known market buys the good (`_good_buyers` lookup)
- AND best known sell price < `MIN_SELL_PRICE` (30 cr/u)

Jettisoning at the gas giant keeps hold space available for valuable siphon output.

## Gas Giant Selection

The bot uses `_find_gas_giants()` to query the DB for gas giants in the current system.
It picks the nearest one to ASTEROID_BASE by coordinate distance.
All siphon teams share the same gas giant — multiple ships can siphon simultaneously.

## Throughput Math (X1-BX78 reference)

With 1 hauler + 5 siphoners:
- Siphoners produce ~15u/cycle, ~3-4 cycles/day each in solo mode
- In group mode: siphoners stay at gas giant continuously, hauler runs ~6-8 trips/day
- Estimated: 5 workers × 60u/trip × 6 trips = ~1,800u/day of gas sold
