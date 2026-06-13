# Auto-Grouping

## What It Does

At bot startup (`work_contract`), before launching threads, the bot calls
`auto_group_ships()` to automatically build groups from the current fleet's
mount types — no manual assignment needed.

## When It Runs

Auto-grouping runs when either condition is true:
1. No groups exist in the DB yet (fresh start or DB cleared)
2. DB setting `auto_group_ships` = `"1"` (force rebuild on every restart)

If manual groups already exist and force=0, auto-grouping is skipped
(manual assignments are preserved).

## Toggle in Dashboard

**Settings → Toggle Auto-Group**

- ON (green): groups rebuild from mounts every restart
- OFF (yellow): manual groups from Fleet → Groups tab are used as-is

## Detection Logic

```python
siphon_workers = ships with MOUNT_GAS_SIPHON_* mount (excluding fleet manager)
miner_workers  = ships with MOUNT_MINING_LASER_* mount (excluding fleet manager + command)
haulers        = ships with HAULER/TRANSPORT role (excluding fleet manager, workers)
```

## Assignment Algorithm

Workers are distributed round-robin across available haulers,
~1 hauler per 3 workers:

```
n_haulers = max(1, ceil(len(workers) / 3))
n_haulers = min(n_haulers, len(available_haulers))

for i, hauler in enumerate(haulers[:n]):
    team_workers = [w for j, w in enumerate(workers) if j % n == i]
```

### Example — 6 siphoners, 2 haulers

```
hauler_count = ceil(6/3) = 2

Siphon Team 1: hauler=DEVRUN-5, workers=[DEVRUN-3, DEVRUN-4, DEVRUN-7]  (indices 0,1,2 → j%2==0)
Siphon Team 2: hauler=DEVRUN-6, workers=[DEVRUN-8, DEVRUN-9, DEVRUN-10] (indices 3,4,5 → j%2==1)
```

### Example — 3 siphoners, 1 hauler

```
hauler_count = min(ceil(3/3), 1) = 1

Siphon Team 1: hauler=DEVRUN-5, workers=[DEVRUN-3, DEVRUN-4, DEVRUN-7]
```

## Group Schema

Generated groups include a `name` field for display:

```json
{
  "name": "Siphon Team 1",
  "type": "siphon",
  "hauler": "TYLERDEVRUN-5",
  "workers": ["TYLERDEVRUN-3", "TYLERDEVRUN-4"]
}
```

## Manual Override

To assign ships manually instead of using auto-grouping:

1. Set Auto-Group toggle to OFF in Settings
2. Go to Fleet → Groups tab
3. Create groups with named hauler + workers
4. Save — groups persist in DB
5. Restart play.py (groups load from DB, auto-group is skipped)

## Backward Compatibility

Old groups without a `name` field continue to work — `play.py` only reads
`hauler` and `workers` fields for loop assignment. The `name` field is UI-only.
