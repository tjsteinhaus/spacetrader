# Strategy

## Overview

v2 uses a **Strategy protocol** to decouple "what to do" from "how to run it".
The `Orchestrator` only knows how to run tasks — the strategy decides which
contract to pursue and which role each ship should have.

This makes it easy to swap strategies without touching the orchestrator.

## Strategy Protocol (`strategy.py`)

```python
class Strategy(Protocol):
    async def select_contract(
        self, client, config, db
    ) -> Contract | None:
        ...

    def assign_role(
        self, ship: Ship, contract: Contract | None, cfg: Config
    ) -> str:
        ...

    def should_buy_ships(self, credits: int, cfg: Config) -> bool:
        ...
```

All three methods must be implemented. The orchestrator calls them on each cycle.

## Available Strategies

### `ContractGrindStrategy` (default)

Mines or buys contract goods until the contract is fulfilled. Highest-value
contracts are prioritized.

#### `select_contract()` logic

```
1. Query active contracts (already accepted + not fulfilled)
   → return first one if any exist

2. Query available contracts (not yet accepted)
   → pick one with payout >= min_contract_payout (default 0)
   → accept it via API

3. If no contracts available:
   → navigate command_ship to faction_hq_wp
   → negotiate new contract → accept → return
```

#### `assign_role()` logic

Evaluated top-to-bottom. First match wins:

| Priority | Condition | Role assigned |
|----------|-----------|---------------|
| 1 | Ship symbol in `ship_role_overrides` | Override value |
| 2 | Has `MOUNT_SURVEYOR_*` AND no mining mount | `"surveyor"` |
| 3 | Has `MOUNT_GAS_SIPHON_*` or any GAS mount | `"siphoner"` |
| 4 | Has `MODULE_JUMP_DRIVE_*` or `WARP_DRIVE_*` | `"explorer"` |
| 5 | Has `MOUNT_MINING_LASER_*` | `"miner"` |
| 6 | Cargo capacity ≥ 40 and no mounts | `"hauler"` (contract) / `"trader"` (no contract) |
| 7 | None of the above | `"idle"` (no task created) |

#### `should_buy_ships()`

```python
return credits >= cfg.min_buy_credits
```

### `IdleStrategy`

Does nothing. Useful for manual operation or testing.

```python
select_contract()  → return None (never accept a contract)
assign_role()      → return "idle" for all ships
should_buy_ships() → return False
```

## `strategy.json` Configuration

Located at `v2/strategy.json`. Read by `load_strategy()` at startup.

```json
{
  "strategy": "contract_grind",
  "min_contract_payout": 50000,
  "ship_role_overrides": {
    "TYLERMASTERY2-5": "trader",
    "TYLERMASTERY2-6": "idle"
  },
  "ship_targets": {
    "SHIP_ORE_HOUND": 4,
    "SHIP_MINING_DRONE": 6,
    "SHIP_LIGHT_HAULER": 2
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `strategy` | str | `"contract_grind"` | Which strategy to use |
| `min_contract_payout` | int | `0` | Skip contracts below this payout |
| `ship_role_overrides` | dict | `{}` | Pin specific ships to specific roles |
| `ship_targets` | dict | built-in defaults | Max ships to buy per type |

## Role Override Use Cases

`ship_role_overrides` is the primary knob for fleet customization:

```json
{ "TYLERMASTERY2-5": "trader" }   // force hauler-class ship to trade instead
{ "TYLERMASTERY2-6": "idle" }      // park a ship
{ "TYLERMASTERY2-3": "surveyor" }  // force a ship to only survey
```

Overrides are checked **before** mount detection — they bypass all logic.

## Hauler vs Trader Disambiguation

A cargo ship (capacity ≥ 40, no mining mount) is assigned:
- `"hauler"` — if a contract is active (ctx is set)
- `"trader"` — if no contract is active (idle period between contracts)

This ensures cargo ships are always productively assigned. During the gap
between contracts, they run arbitrage instead of sitting idle.

## Contract Negotiation Flow

```
select_contract():
  active = db.get_active_contracts()
  if active → return active[0]

  available = await contract_api.list_contracts(client)
  best = max(available, key=lambda c: c.terms.payment.on_fulfilled)
  if best.payout >= min_contract_payout:
      await contract_api.accept(client, best.id)
      return best

  # No acceptable contracts → negotiate new one
  await navigator.navigate_with_refuel(command_ship, cfg.faction_hq_wp)
  new_contract = await contract_api.negotiate(client, command_ship)
  await contract_api.accept(client, new_contract.id)
  return new_contract
```

The command ship navigates to HQ only when no contracts exist. HQ is the faction
headquarters waypoint (`cfg.faction_hq_wp`), typically where the agent started.

## Adding a New Strategy

1. Create a class implementing the `Strategy` protocol in `strategy.py`
2. Add a new key to `load_strategy()`:
```python
def load_strategy(path="strategy.json") -> Strategy:
    data = json.load(open(path))
    match data.get("strategy", "contract_grind"):
        case "contract_grind": return ContractGrindStrategy(data)
        case "idle":           return IdleStrategy()
        case "my_new_strat":   return MyNewStrategy(data)
        case _:                raise ValueError(...)
```
3. Update `strategy.json` to use the new name
