# Week 2 Strategy

> **Note:** This strategy runs on the **v1 scripts** (`play.py`, `main.py`, etc.) in the workspace root — NOT the `v2/` folder. All code changes listed below apply to v1 files.

## Overview

Focus on arbitrage trading as the primary credit engine. Get 3 Light Haulers online as fast as possible — 1 dedicated to contract delivery, 2 running arbitrage. Surveyor first to boost early mining yield. Miners and siphoners added after hauler trio is secured, with a no-drift gate to keep them anchored near HQ.

---

## Credit Priority Order

1. **Arbitrage Trading** — 2 haulers running buy-low/sell-high routes continuously
2. **Contracts** — mining + buy-goods procurement; 1 dedicated hauler handles delivery
3. **Mining / Siphoning** — passive income; ore hounds + siphon drones added late

---

## Fleet Composition

| Ship | Role | Count | Notes |
|------|------|-------|-------|
| Command Frigate | Miner | 1 | Starting ship; mines or buys for contracts |
| Surveyor | Surveyor | up to 2 | First purchase; feeds shared survey pool (+30% yield) |
| Light Hauler | Hauler / Trader | up to 3 | #1 = contract hauler; #2 and #3 = arbitrage traders |
| Ore Hound | Miner | up to 4 | 600 fuel cap — no drift risk; bought after all 3 haulers |
| Mining Drone | Miner | up to 3 | 80 fuel cap — only bought if asteroid ≤70 units from fuel market |
| Siphon Drone | Siphoner | up to 2 | Only bought if gas giant ≤70 units from fuel market |

**Never buy:** Heavy Freighter, Command Frigate (already have one), Light Shuttle, Probe, Gas Drone

---

## SHIP_SCORES Priority

```
SHIP_SURVEYOR:     100   ← first buy
SHIP_LIGHT_HAULER:  95   ← buy all 3 before anything else (big gap enforces order)
SHIP_ORE_HOUND:     65   ← best miner, after haulers capped
SHIP_MINING_DRONE:  60   ← cheap miner, no-drift check required
SHIP_SIPHON_DRONE:  55   ← passive gas, no-drift check required
everything else:    -1   ← skip
```

---

## Bootstrap Timeline

| Credits | Purchase | Effect |
|---------|----------|--------|
| ~0      | — | Register → accept first contract → command frigate mines or buys goods |
| ~100k   | **Surveyor #1** | +30% extraction yield; surveyor runs continuously at asteroid |
| ~220k   | **Light Hauler #1** | Takes over all contract delivery (mining or buy-goods); command frigate stays at asteroid |
| ~370k   | **Light Hauler #2** | Immediately assigned as Trader; arbitrage starts |
| ~520k   | **Light Hauler #3** | 2nd Trader — 2 active arbitrage routes running |
| ~650k   | **Ore Hound #1** | Dedicated miner with 600 fuel; no drift |
| ~750k   | **Mining Drone #1** | Only if asteroid is within 70 units of fuel market |
| ~850k   | **Siphon Drone #1** | Only if gas giant is within 70 units of fuel market |
| ~1M+    | Scale up | More Ore Hounds (→4), Surveyor #2, Siphon Drone #2, Mining Drones (→3) |

---

## Role Assignment Rules

**Hauler vs Trader logic** (fleet-count-aware, automatic):
- All "hauler-eligible" ships (cargo ≥40, no mining/survey mount) sorted by callsign number ascending
- **Lowest number** → `hauler` during active contracts, `trader` when no contract
- **All others** → always `trader`
- No manual `ship_role_overrides` pins needed at start

**Hauler #1 handles both contract types:**
- If contract good is mineable (IRON_ORE, COPPER_ORE, etc.) → parks at asteroid, waits for miners, delivers
- If contract good is non-mineable (IRON, FOOD, manufactured goods) → buys from best market directly, delivers

---

## Miner Fallback Logic

Miners try to mine first. They fall back to buy-mode automatically:
1. Good is non-mineable → direct-buy immediately, no mining attempted
2. Good is on market for ≤200 cr/unit → direct-buy (cheaper than mining)
3. 5+ consecutive dry extractions (wrong ore type) → switch to buy-mode mid-contract

---

## No-Drift Gate

Before buying **Mining Drone** or **Siphon Drone**, the fleet manager checks distance:
- Mining Drone: asteroid must be ≤70 units from nearest fuel market
- Siphon Drone: gas giant must be ≤70 units from nearest fuel market
- If check fails → ship type skipped with log warning; rechecked next fleet manager cycle (every 5 min)

This prevents low-fuel-cap ships from drifting through empty space.

---

## Reset / New Game Checklist

1. `python3 register.py` — registers new agent, writes token to `.env`
2. Update constants in `play.py` for the new system (SYSTEM, ASTEROID, ASTEROID_BASE, SHIPYARD_WP)
3. Update SHIP_SCORES and fleet caps in `play.py` per the values in this doc
4. `python3 play.py` — bot starts; confirm first fleet_manager cycle buys Surveyor, not Ore Hound

---

## Code Changes Required (not yet implemented)

> All changes are in **v1 files** (workspace root, not `v2/`).

- `play.py` — Set SHIP_SCORES (currently blocks unwanted ships), set MIN_BUY_CREDITS, add DEFAULT_SHIP_TARGETS caps
- `play.py` — Fleet-count-aware hauler/trader role assignment (oldest large-cargo ship = hauler, rest = trader)
- `play.py` — `_is_siphon_reachable()` / `_is_mining_drone_safe()` no-drift checks before purchasing
- `register.py` — Verify it clears cached system constants on reset (SYSTEM, ASTEROID, etc.)

---

## Notes

- Faction and starting system change each reset — `auto_configure` handles detection automatically
- If no gas giant exists in the starting system, siphon drones are useless; the no-drift check catches this
- Siphon tender (hauler parks at gas giant, collects from drones) is a future optimization — estimated 11hr payback at ~300k cost; deferred for now
