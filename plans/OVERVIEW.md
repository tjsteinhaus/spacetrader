# Plans Overview

This folder documents the strategy and implementation plans for TYLERDEVRUN's automation bot.
Each file covers one domain. See `PLAYBOOK.md` in the root for the full architecture reference.

## Files

| File | Description |
|------|-------------|
| [TEAM_STRUCTURE.md](TEAM_STRUCTURE.md) | How ships are organized into groups, roles, and the fill-before-expand buy policy |
| [MINING_TEAM.md](MINING_TEAM.md) | Miner worker + hauler loop behavior, anti-clog, refining |
| [SIPHON_TEAM.md](SIPHON_TEAM.md) | Siphoner worker + hauler loop behavior, anti-clog, gas refining, fromCargo refuel |
| [FLEET_PURCHASE.md](FLEET_PURCHASE.md) | Ship buy priority, team-fill logic, credit reserve policy |
| [CARGO_POLICY.md](CARGO_POLICY.md) | Universal cargo usability decision tree (contract → refine → sell → jettison) |
| [AUTO_GROUPING.md](AUTO_GROUPING.md) | How ships are auto-assigned to groups from mounts at startup |

## Key Constants (play.py)

```python
PRODUCERS_PER_TEAM_TARGET = 5   # workers per team before seeding a new one
HAULERS_PER_TEAM_TARGET   = 1   # haulers per active team
MAX_SIPHON_TEAMS          = 2   # max concurrent siphon teams
MAX_MINER_TEAMS           = 2   # max concurrent miner teams
CREDIT_RESERVE            = 500_000   # never spend below this
MIN_BUY_CREDITS           = 600_000   # start buying at this threshold
```

## System

- Agent: TYLERDEVRUN
- System: X1-BX78
- Asteroid: X1-BX78-B12 (auto-detected)
- Base: X1-BX78-B7
- Gas Giant: X1-BX78-C44
