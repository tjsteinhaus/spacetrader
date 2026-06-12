# Week 2 Strategy & Full Meta Progression

> **Note:** This strategy runs on the **v1 scripts** (`play.py`, `main.py`, etc.) in the workspace root — NOT the `v2/` folder.

---

## Full Meta Progression (All Stages)

```
Stage 1  ──────────────────────────────────────────────────────────────
  Early mining + contracts
  Command Frigate mines, does contracts, accumulates credits
  Target: ~500k total credits

Stage 2  ──────────────────────────────────────────────────────────────
  3× Light Haulers + Arbitrage  ← YOU ARE HERE
  Surveyor → Hauler #1 (contract) → Hauler #2 + #3 (traders)
  2 trader haulers running buy-low/sell-high continuously
  Target: 200–500k cr/hour from arbitrage alone

Stage 3  ──────────────────────────────────────────────────────────────
  Probe Price Network  (trigger: credits > 500k, haulers online)
  Buy N cheap SHIP_PROBEs, park one at each market permanently
  Every market has live prices → better arbitrage routing, better sourcing
  See: /memories/repo/plan_probe_price_network.md for full implementation plan

Stage 4  ──────────────────────────────────────────────────────────────
  Command Frigate → find refinery → ore→metal route
  Buy Command Frigate (has JUMP_DRIVE_I) → use jump gate to scout neighbors
  Run: python3 scout_supply_chain.py  to map production chains in your system
  Goal: find REFINERY waypoint → route IRON_ORE there → sell IRON for 4–5× markup

Stage 5  ──────────────────────────────────────────────────────────────
  Tier 2 manufacturing  (STEEL, ELECTRONICS)
  IRON + COPPER → STEEL   |   SILICON_CRYSTALS + COPPER → ELECTRONICS
  Requires FABRICATOR waypoint (usually ORBITAL_STATION or SPACE_STATION)
  Each step ~3–5× value multiply; deep import demand from shipyards

Stage 6  ──────────────────────────────────────────────────────────────
  Full vertical integration  (leaderboard play)
  Own the entire chain: mines → refineries → fabricators → delivery
  Add haulers for each chain leg; traders capture cross-system spreads
  ship components (SHIP_PLATING, MACHINERY, CIRCUIT_BOARDS) = highest margins
```

---

## Critical Path — Credit Milestones

| Credits | Action | Unlocks |
|---------|--------|---------|
| ~0 | Register → first contract | Command Frigate mines |
| ~100k | **Buy Surveyor** | +30% extraction yield |
| ~220k | **Buy Hauler #1** | Contract delivery freed; command frigate stays mining |
| ~370k | **Buy Hauler #2** | First arbitrage trader online |
| ~520k | **Buy Hauler #3** | 2 traders running — arbitrage credit engine at full speed |
| ~650k | **Buy Ore Hound #1** | Dedicated 600-fuel miner; no drift risk |
| ~750k+ | Scale miners | More Ore Hounds (→4), Surveyor #2, Siphon Drones if safe |
| ~1M | **Deploy Probe Network** | Live prices at every market; unlock Stage 3 |
| ~1.5M | **Buy Command Frigate** | Jump drive → scout adjacent systems for refinery |
| ~2M+ | **Start ore→metal route** | Stage 4; 1 hauler pivots to refinery chain |

---

## Stage 2 Detail — Arbitrage Trading

### Overview

Focus on arbitrage trading as the primary credit engine. Get 3 Light Haulers online as fast as possible — 1 dedicated to contract delivery, 2 running arbitrage. Surveyor first to boost early mining yield. Miners and siphoners added after hauler trio is secured, with a no-drift gate to keep them anchored near HQ.

### Credit Priority Order

1. **Arbitrage Trading** — 2 haulers running buy-low/sell-high routes continuously
2. **Contracts** — mining + buy-goods procurement; 1 dedicated hauler handles delivery
3. **Mining / Siphoning** — passive income; ore hounds + siphon drones added late

### Fleet Composition

| Ship | Role | Count | Notes |
|------|------|-------|-------|
| Command Frigate | Miner | 1 | Starting ship; mines or buys for contracts |
| Surveyor | Surveyor | up to 2 | First purchase; feeds shared survey pool (+30% yield) |
| Light Hauler | Hauler / Trader | up to 3 | #1 = contract hauler; #2 and #3 = arbitrage traders |
| Ore Hound | Miner | up to 4 | 600 fuel cap — no drift risk; bought after all 3 haulers |
| Mining Drone | Miner | up to 3 | 80 fuel cap — only bought if asteroid ≤70 units from fuel market |
| Siphon Drone | Siphoner | up to 2 | Only bought if gas giant ≤70 units from fuel market |

**Never buy:** Heavy Freighter, Command Frigate (already have one), Light Shuttle, Probe, Gas Drone

### SHIP_SCORES Priority

```
SHIP_SURVEYOR:     100   ← first buy
SHIP_LIGHT_HAULER:  95   ← buy all 3 before anything else (big gap enforces order)
SHIP_ORE_HOUND:     65   ← best miner, after haulers capped
SHIP_MINING_DRONE:  60   ← cheap miner, no-drift check required
SHIP_SIPHON_DRONE:  55   ← passive gas, no-drift check required
everything else:    -1   ← skip
```

### Role Assignment Rules

**Hauler vs Trader logic** (fleet-count-aware, automatic):
- All "hauler-eligible" ships (cargo ≥40, no mining/survey mount) sorted by callsign number ascending
- **Lowest number** → `hauler` during active contracts, `trader` when no contract
- **All others** → always `trader`
- No manual `ship_role_overrides` pins needed at start

**Hauler #1 handles both contract types:**
- If contract good is mineable (IRON_ORE, COPPER_ORE, etc.) → parks at asteroid, waits for miners, delivers
- If contract good is non-mineable (IRON, FOOD, manufactured goods) → buys from best market directly, delivers

### Miner Fallback Logic

Miners try to mine first. They fall back to buy-mode automatically:
1. Good is non-mineable → direct-buy immediately, no mining attempted
2. Good is on market for ≤200 cr/unit → direct-buy (cheaper than mining)
3. 5+ consecutive dry extractions (wrong ore type) → switch to buy-mode mid-contract

### No-Drift Gate

Before buying **Mining Drone** or **Siphon Drone**, the fleet manager checks distance:
- Mining Drone: asteroid must be ≤70 units from nearest fuel market
- Siphon Drone: gas giant must be ≤70 units from nearest fuel market
- If check fails → ship type skipped with log warning; rechecked next fleet manager cycle (every 5 min)

This prevents low-fuel-cap ships from drifting through empty space.

---

## Stage 4 Detail — Supply Chain & Manufacturing

### Supply Chain Cheat Sheet

**Tier 1 — Smelting** (requires `REFINERY` waypoint trait)
```
IRON_ORE      →  IRON        (4–5× value)
COPPER_ORE    →  COPPER      (4–5× value)
ALUMINUM_ORE  →  ALUMINUM    (4–5× value)
GOLD_ORE      →  GOLD        (3–4× value)
SILVER_ORE    →  SILVER      (3–4× value)
PLATINUM_ORE  →  PLATINUM    (5–8× value)
URANITE_ORE   →  URANITE     (5–8× value)
MERITIUM_ORE  →  MERITIUM    (8–12× value)
```

**Tier 2 — Fabrication** (requires `FABRICATOR` or `INDUSTRIAL` waypoint trait)
```
IRON + COPPER                    →  STEEL
SILICON_CRYSTALS + COPPER        →  ELECTRONICS
IRON + SILICON_CRYSTALS          →  MACHINERY
GOLD + ELECTRONICS               →  MICROPROCESSORS
```

**Tier 3 — Advanced Components** (requires `OUTFITTING` waypoint)
```
STEEL + ELECTRONICS              →  SHIP_PLATING
MACHINERY + ELECTRONICS          →  REACTOR_COMPONENTS
MICROPROCESSORS + SHIP_PLATING   →  ADVANCED_CIRCUITRY
```

**How to find production waypoints:**
```bash
# List all waypoints with their traits (check for REFINERY, FABRICATOR, etc.)
python3 status.py waypoints

# Full supply chain map — shows all production paths in the game
python3 scout_supply_chain.py

# Check what your local markets already export/import
python3 status.py arbitrage
```

**Key waypoint traits to look for:**
| Trait | What it does |
|-------|-------------|
| `REFINERY` | Accepts ores, exports smelted metals |
| `FABRICATOR` | Accepts metals, exports fabricated goods |
| `INDUSTRIAL` | Broad manufacturing capability |
| `OUTFITTING` | Ship component production |
| `RESEARCH_FACILITY` | Produces advanced/rare goods |

### How to Run a Refinery Route

Once you find a `REFINERY` waypoint:
1. Buy `IRON_ORE` from an asteroid market (miners already flood these → cheap buy price)
2. Haul to refinery — sell `IRON_ORE` to refinery's import market
3. Buy `IRON` from refinery's export market (it produces what it imports)
4. Haul `IRON` to any importer → sells for 4–5× the ore purchase price

One dedicated hauler running this loop beats 2–3 raw-ore arbitrage routes.

---

## Implementation Status

> All week2 code is **fully implemented** in `play.py`. Nothing pending.

| Feature | Status | Location in play.py |
|---------|--------|---------------------|
| `trader_loop()` with arbitrage + backhaul | ✅ Done | Lines 2317–2928 |
| `hauler_loop()` for contract delivery | ✅ Done | Lines 1584–1728 |
| `ship_score()` with dynamic fleet caps | ✅ Done | Lines 3614–3671 |
| Hauler/Trader role assignment in `work_contract()` | ✅ Done | Lines 3364–3550 |
| `_is_mining_drone_safe()` no-drift check | ✅ Done | Lines 3590–3596 |
| `_is_siphon_reachable()` no-drift check | ✅ Done | Lines 3599–3611 |
| SHIP_SCORES (surveyor first, haulers before miners) | ✅ Done | Lines 176–186 |
| Miner buy-mode fallback | ✅ Done | Lines 1828–1850 |
| Pre-sell price check in trader (wait for recovery) | ✅ Done | trader_loop_inner |
| Backhaul on trader return trip | ✅ Done | trader_loop_inner |

---

## Reset / New Game Checklist

1. `python3 register.py` — registers new agent, writes token to `.env`
2. `python3 play.py` — `auto_configure()` detects system, asteroid, and shipyard automatically
3. Confirm first fleet_manager cycle buys Surveyor, not Ore Hound (check logs)
4. After Hauler #2 purchased, verify arbitrage routes appear in logs

---

## Notes

- Faction and starting system change each reset — `auto_configure` handles detection automatically
- If no gas giant exists in the starting system, siphon drones are useless; the no-drift check catches this
- Siphon tender (hauler parks at gas giant, collects from drones) is a future optimization — estimated 11hr payback at ~300k cost; deferred for now
- Probe price network plan saved at: `/memories/repo/plan_probe_price_network.md` — implement after Hauler #3 + 1M credits
