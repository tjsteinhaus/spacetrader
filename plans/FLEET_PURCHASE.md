# Fleet Purchase Plan

## Credit Policy

```python
CREDIT_RESERVE  = 500_000   # never spend below this (for trade goods, contracts)
MIN_BUY_CREDITS = 600_000   # fleet manager only activates above this threshold
```

The 100k gap between these two values is the maximum the fleet manager will spend
on a single ship purchase. If a ship costs more than 100k above the reserve, it
won't buy until credits build further.

## Ship Purchase Priority (SHIP_SCORES)

| Ship type | Score | Notes |
|-----------|-------|-------|
| SHIP_LIGHT_HAULER | 100 | Seeds new teams — always first buy |
| SHIP_ORE_HOUND | 65 | Best miner, fills miner team slots |
| SHIP_MINING_DRONE | 60 | Cheap miner, requires drift-safe check |
| SHIP_SIPHON_DRONE | 55 | Gas siphoner, fills siphon team slots |
| Everything else | -1 | Never buy |

Higher score = bought first when multiple types are affordable.

## Fill-Before-Expand Policy

The bot never starts a new team until the current one is full.

```
Team is FULL when:
  - workers >= PRODUCERS_PER_TEAM_TARGET (5)
  - haulers >= HAULERS_PER_TEAM_TARGET (1)

Team is SEEDED when:
  - hauler exists, workers = 0
```

### Hauler buy logic

A new hauler is purchased when:
- No teams of that type exist yet (need to seed first team), OR
- All existing teams of that type are full AND team count < MAX cap

Otherwise haulers are skipped (all teams already have one, none are expanding).

### Worker buy logic

Workers are purchased until:
`current_workers >= active_teams × PRODUCERS_PER_TEAM_TARGET`

Example with 1 miner team, 3 current miners:
- Target = 1 × 5 = 5
- Need 2 more miners before seeding team 2

### Team caps

```python
MAX_SIPHON_TEAMS = 2   # max 2 siphon teams (10 siphoners + 2 haulers)
MAX_MINER_TEAMS  = 2   # max 2 miner teams  (10 miners   + 2 haulers)
```

Total at full build: 20 producers + 4 haulers + command + fleet manager = ~26 ships

## Fleet Manager Behavior

The fleet manager (`fleet_manager_loop`) runs as a background thread during contracts.

Each cycle it:
1. Checks `auto_buy_ships` DB setting — if disabled, skips
2. Checks credits vs `MIN_BUY_CREDITS` threshold
3. Checks `_shipyard_price_cache` to short-circuit if can't afford anything
4. Navigates `FLEET_MANAGER_SHIP` to each shipyard in `SHIPYARD_WPS`
5. Collects all eligible ships sorted by score descending
6. Buys highest-scored affordable ships, launches their loop thread immediately

## Safety Checks

| Ship type | Guard |
|-----------|-------|
| SHIP_MINING_DRONE | `_is_mining_drone_safe()` — asteroid within `NO_DRIFT_DIST_MAX` of fuel market |
| SHIP_SIPHON_DRONE | `_is_siphon_reachable()` — gas giant within `NO_DRIFT_DIST_MAX` of fuel market |

These prevent buying ships that would be stranded without fuel.

## Newly Bought Ship Launch

After purchase, the fleet manager immediately launches the correct loop thread:

| Ship type | Loop launched |
|-----------|--------------|
| SHIP_SIPHON_DRONE / SHIP_GAS_DRONE | `siphon_loop` |
| SHIP_SURVEYOR | `surveyor_loop` |
| SHIP_LIGHT_HAULER / SHIP_HEAVY_FREIGHTER | `trader_loop` (arbitrage until grouped) |
| SHIP_ORE_HOUND / SHIP_MINING_DRONE | `miner_loop` |

## Upgrade Path

The bot auto-upgrades mining laser tiers on miners when credits allow:
- `MOUNT_MINING_LASER_I` → `MOUNT_MINING_LASER_II` → `MOUNT_MINING_LASER_III`
- Repair threshold: component condition < 80% triggers repair
