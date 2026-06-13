# Team Structure

## Concept

Ships are organized into **teams** — each team is one hauler paired with N workers.
Workers stay at their extraction point (asteroid or gas giant) and signal the hauler
when their cargo is full. The hauler collects, refines if profitable, then sells.

This keeps workers extracting continuously instead of making round trips.

## Team Types

| Type | Workers | Hauler behavior |
|------|---------|-----------------|
| `siphon` | GAS_SIPHON mount ships | Collects from gas giant, refines HYDROCARBON→FUEL if profitable, sells at base |
| `miner` | MINING_LASER mount ships | Collects from asteroid, refines ores if profitable, sells at base |

## Team Composition Targets

```python
PRODUCERS_PER_TEAM_TARGET = 5   # fill this many workers before seeding a second team
HAULERS_PER_TEAM_TARGET   = 1   # one hauler per team
MAX_SIPHON_TEAMS          = 2   # hard cap on concurrent siphon teams
MAX_MINER_TEAMS           = 2   # hard cap on concurrent miner teams
```

At full build-out: 2 siphon teams + 2 miner teams = 20 workers + 4 haulers + command + fleet manager = ~26 ships.

## Fill-Before-Expand Rule

The bot **never starts a new team until the current one is full** (5 workers + 1 hauler).

Buy sequence for each team type:
1. Buy a hauler → team is seeded (1 hauler, 0 workers)
2. Buy workers until `PRODUCERS_PER_TEAM_TARGET` reached → team is full
3. Only then buy a hauler for the next team

This ensures every team is viable before resources are spread thin.

## Data Model

Groups are stored in the DB under key `ship_groups` as a JSON array:

```json
[
  {
    "name": "Siphon Team 1",
    "type": "siphon",
    "hauler": "TYLERDEVRUN-5",
    "workers": ["TYLERDEVRUN-3", "TYLERDEVRUN-4"]
  },
  {
    "name": "Miner Team 1",
    "type": "miner",
    "hauler": "TYLERDEVRUN-6",
    "workers": ["TYLERDEVRUN-7", "TYLERDEVRUN-8"]
  }
]
```

## Worker Signaling Protocol

1. Worker cargo fills up → sets `_group_worker_ready[worker_symbol]` event
2. Worker pauses at waypoint, waits for hauler
3. Hauler detects set event, navigates to worker's waypoint
4. Hauler transfers cargo item-by-item via `transfer_cargo()` API
5. Hauler clears the event → worker resumes extraction

## Thread Model

Each ship runs one thread:
- Workers: `siphon_loop` or `miner_loop` (group-aware variant)
- Haulers: `siphon_hauler_loop` or `miner_hauler_loop`

Both hauler and worker threads share `_group_worker_ready` dict (protected by `_ship_groups_lock`).

## UI Management

Groups can be created manually in the dashboard under **Fleet → Groups tab**.
Auto-grouping (from mount detection) can be enabled via **Settings → Toggle Auto-Group**.
Ship detail modal (Group tab) shows current assignment as read-only.
