# Cargo Usability Policy

## Decision Tree

For every good in a ship's cargo hold, apply these rules in priority order:

```
1. CAN_BE_DELIVERED_FOR_CONTRACT_LOCAL
   └─ Good matches active contract AND destination is reachable without detour
   └─ Action: deliver to contract destination

2. CAN_BE_REFINED_AND_SOLD_AT_BASE
   └─ Good is in REFINEABLE map (ore or HYDROCARBON)
   └─ Ship has the required processor module
   └─ Refined output sells for more than raw at TEAM_BASE (or raw price unknown)
   └─ Action: refine first, then sell refined output

3. CAN_BE_SOLD_AT_BASE
   └─ Market at TEAM_BASE buys this good
   └─ Best known sell price >= MIN_SELL_PRICE (30 cr/u)
   └─ Action: sell raw

4. UNUSABLE
   └─ None of the above apply
   └─ Action: JETTISON immediately
```

## Important Rules

- **Contract goods that can't be delivered locally are NOT held indefinitely.**
  They fall through to refine/sell/jettison like any other cargo.
- **Jettisoning is preferred over clogging** — a full hold stops extraction entirely.
- **Refining is always profit-checked** — never refine if raw sells for more than refined.

## Refining Maps

### Mining (requires MODULE_MINERAL_PROCESSOR_I)

| Raw | → | Refined |
|-----|---|---------|
| IRON_ORE | → | IRON |
| COPPER_ORE | → | COPPER |
| ALUMINUM_ORE | → | ALUMINUM |
| SILVER_ORE | → | SILVER |
| GOLD_ORE | → | GOLD |
| PLATINUM_ORE | → | PLATINUM |
| URANITE_ORE | → | URANITE |
| MERITIUM_ORE | → | MERITIUM |

### Siphon (requires MODULE_GAS_PROCESSOR_I)

| Raw | → | Refined |
|-----|---|---------|
| HYDROCARBON | → | FUEL |

Note: FUEL produced by siphon refining is used for the hauler's own refueling
before it buys market fuel (`fromCargo=True` in the refuel API call).

## Jettison Threshold

```python
MIN_SELL_PRICE = 30  # cr/unit — jettison if best known price is below this
```

Exceptions — never jettison if:
- A known market imports this good (even without a cached price)
- The good is the active contract's required item

## Implementation

The policy is enforced in two places:

| Location | When it runs |
|----------|-------------|
| Worker loop (before signaling hauler) | Anti-clog: jettison low-value goods before hold is full |
| Hauler sell routine | Full policy: refine → sell, with jettison fallback via `sell_junk()` |

`sell_junk()` handles routing to the best-paying market and automatically jettisons
anything that can't be sold above the minimum threshold.
