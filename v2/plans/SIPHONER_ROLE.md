# Siphoner Role

## Goal

Continuously siphon gas from the nearest gas giant. Deliver contract goods when
applicable, sell junk cargo for income, and return. No team structure — each
siphoner is a solo self-sufficient loop.

## Ship Requirements

| Requirement | Details |
|-------------|---------|
| Mount | `MOUNT_GAS_SIPHON_I` or `MOUNT_GAS_SIPHON_II` or any GAS_* mount |
| Role name | `"siphoner"` |
| Class | `roles/siphoner.py:SiphonerRole` |

## Key Difference from v1

v1 siphoners required CRUISE-mode navigation because BURN was unaffordable for
the round trip. v2 uses `navigator.navigate_with_refuel()` which automatically
picks the cheapest viable flight mode. `fromCargo` refueling is handled by the
navigator — no manual cargo-sell-to-refuel needed.

v2 also has **no siphon group/team** — each siphoner is assigned independently
by the strategy and runs its own loop.

## Gas Giant Selection

At startup, the siphoner queries the DB for gas giants in the current system:

```python
gas_giants = db.get_waypoints_by_type(system_symbol, "GAS_GIANT")
# Score = negative distance from ASTEROID_BASE
# Nearest gas giant wins
giant_wp = min(gas_giants, key=lambda wp: distance(cfg.asteroid_base, wp))
```

The selected giant is cached for the duration of the run (per siphoner instance).

## SiphonerRole Loop

```
1. Navigate to gas giant (navigate_with_refuel from base or current pos)
2. Orbit
3. SIPHON LOOP:
   a. Siphon once (fleet_api.siphon)
   b. wait_cooldown()
   c. If cargo NOT full → continue loop
   d. If cargo full → PROCESS_CARGO, then return to step 1

PROCESS_CARGO (cargo full trigger):
  a. Navigate to nearest sell market for junk goods (avoid going to base if not needed)
  b. sell_junk(keep_good=contract_good)   ← keeps contract good, sells everything else
  c. If have contract good AND destination reachable:
       navigate to ASTEROID_BASE → refuel
       navigate to delivery_wp → dock
       deliver_contract(cid, good, min(have, remaining))
       if fulfilled → fulfill_contract → ctx.done.set() → return
       sell_junk()
       refuel at ASTEROID_BASE
  d. Navigate back to gas giant → orbit → resume siphon loop
```

## Contract Integration

Siphoners only deliver if:
1. `ctx` is set (a contract is active)
2. The contract good matches something the siphoner has in cargo
3. The gas giant can produce the contract good (e.g. `HYDROCARBON`, `LIQUID_NITROGEN`)

If no active contract, siphoners still run — they siphon → sell junk → repeat
for income generation.

## Fuel Strategy

| Segment | Flight mode |
|---------|-------------|
| Base → Gas Giant | `navigator.navigate_with_refuel()` — picks BURN or CRUISE |
| Gas Giant → Sell market | As above — may drift if out of range |
| Sell market → Delivery | BURN preferred if fuel permits |
| Delivery → Base | CRUISE if base is on the same route |

There is no manual CRUISE mode pinning in v2. The navigator chooses automatically
based on `can_reach(from, to, current_fuel)`.

## Goods Siphoned

Common siphon goods: `HYDROCARBON`, `LIQUID_HYDROGEN`, `LIQUID_NITROGEN`, 
`ATMOSPHERIC_GASES`.

These can be contract goods for gas-giant-adjacent contracts. If the system has
no gas giant, siphon ships should be reassigned (via `ship_role_overrides`) to
`idle` or `trader`.

## Surveyor Role (side note)

`roles/surveyor.py:SurveyorRole` is separate from the siphoner. It targets the
same asteroid as miners (via shared `AsteroidCache.choose_target`), not the gas
giant. There is no surveyor equivalent for gas giants — gas is extracted by
siphon directly.

## SurveyorRole Loop

```
1. Navigate to ASTEROID_TARGET (same as miners)
2. Orbit
3. SURVEY LOOP:
   a. fleet_api.survey() → surveys
   b. await surveys_pool.add(surveys)   ← shared pool with miners
   c. db.upsert_survey(s) for each     ← persist to SQLite
   d. wait_cooldown()
   e. loop
```

Surveyors never deliver or sell cargo — they are dedicated survey-only.
Cargo fills only from asteroid debris (passive), which is jettisoned on any
`sell_junk()` call from base roles. Surveyors rarely carry cargo.
