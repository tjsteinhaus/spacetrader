# Cargo Policy

## Overview

All cargo decisions in v2 follow a single priority order:

```
1. Keep contract good (if CTX has one)
2. Sell anything that has a profitable market
3. Jettison anything below min_sell_price threshold
```

This logic is centralized in `BaseRole.sell_junk(keep_good)` and is reused by
every role. Roles never implement their own cargo-sell loops.

## `sell_junk(keep_good=None)`

Defined in `roles/base.py`. Called by all roles after delivering or when cargo
is full.

```python
async def sell_junk(self, keep_good: str | None = None):
    inventory = [item for item in ship.cargo.inventory
                 if item.symbol != keep_good]
    for item in inventory:
        best_wp = market.best_sell_market_for_cargo(item.symbol, system)
        if best_wp is None:
            # No market found → jettison
            await fleet_api.jettison(client, ship, item.symbol, item.units)
        else:
            price = market.get_sell_price(item.symbol, best_wp)
            if price < cfg.min_sell_price:
                # Price too low → jettison
                await fleet_api.jettison(client, ship, item.symbol, item.units)
            else:
                await navigator.navigate_with_refuel(ship, best_wp)
                await fleet_api.dock(client, ship)
                await market_api.sell_cargo(client, ship, item.symbol, item.units)
                db.upsert_transaction(...)
```

## Decision Tree

```
For each cargo item (except keep_good):
    ├─ Find best_sell_market_for_cargo(symbol)
    │   ├─ Market found AND price >= min_sell_price?
    │   │   └─ Navigate → dock → sell
    │   ├─ Market found BUT price < min_sell_price?
    │   │   └─ JETTISON
    │   └─ No market found in DB?
    │       └─ JETTISON
    └─ Contracts good always kept — never in the loop
```

## `min_sell_price` Threshold (default 30 cr/unit)

Prevents selling goods so cheap they're not worth the fuel. Goods below this
price are jettisoned instead.

Set in `config.py` or `strategy.json`:
```json
{ "min_sell_price": 30 }
```

Common low-value goods that often get jettisoned: `ICE_WATER`, `QUARTZ_SAND`,
`SILICON_CRYSTALS` (at certain markets).

## `best_sell_market_for_cargo`

In `market.py`:
```python
def best_sell_market_for_cargo(self, symbol: str, system: str) -> str | None:
    # 1. Check DB for all markets in system that buy this good
    # 2. Filter to markets with a known price (not stale beyond 24h)
    # 3. Return the waypoint symbol with the highest sell_price
    # Returns None if no known buyers
```

If the cached price is stale, the role may navigate to a market only to find
prices changed. The fleet manager's market scan (every 2hr) mitigates this.

## TraderRole: Price-Crash Guard (`_sell_batched`)

When selling in batches (large quantities), the trader stops early if:
- Current sell price < 10% of the first-batch sell price
- OR current sell price < `cfg.min_sell_price`

```python
PRICE_FLOOR_RATIO = 0.10

async def _sell_batched(self, symbol, total_units, waypoint):
    first_price = None
    sold = 0
    while sold < total_units:
        live = await market_api.get_market(client, waypoint)
        price = live.sell_price_for(symbol)
        if first_price is None:
            first_price = price
        if price < first_price * PRICE_FLOOR_RATIO:
            break   # price crashed — stop selling, move on
        if cfg.min_sell_price and price < cfg.min_sell_price:
            break
        batch = min(TRADE_BATCH_SIZE, total_units - sold)
        await market_api.sell_cargo(client, ship, symbol, batch)
        sold += batch
```

## Arbitrage Fill (`MinerRole._fill_arbitrage`)

When in direct-buy mode, the miner fills spare cargo with the highest-margin
arbitrage good from the buy market to the delivery waypoint.

```
1. Get all goods sold at current market
2. For each good (excluding contract good):
   a. buy_price = market.get_buy_price(symbol, current_wp)
   b. sell_price = market.get_sell_price(symbol, delivery_wp)
   c. margin = sell_price - buy_price
3. Pick good with highest positive margin
4. Buy to fill spare capacity
5. These are sold via sell_junk() when the miner returns
```

## Trader Backhaul (`TraderRole._try_backhaul`)

After selling at a destination market, the trader checks if the current market
has good outbound opportunities before navigating back:

```
After sell completes at market B:
  check market B's buy prices for goods that sell well elsewhere
  → if a backhaul trip exists with margin > TRADER_MIN_MARGIN: take it
  → otherwise: return to base or find next arbitrage route
```

## Credit Reserves During Cargo Operations

| Operation | Credit check |
|-----------|-------------|
| Direct buy (miner) | `credits - buy_cost >= max(5000, credit_reserve // 6)` |
| Arbitrage fill | `credits - fill_cost >= credit_reserve // 6` |
| Trader buy | `credits - total_cost >= TRADER_CREDIT_RESERVE (150,000)` |
| Fleet purchase | `credits - ship_cost >= credit_reserve (500,000)` |

## What Gets Jettisoned

In practice, the most commonly jettisoned goods are:
- `ICE_WATER` — very common in asteroids, few markets pay well
- `QUARTZ_SAND` — similar
- Anything under 30 cr/unit at the nearest market

Jettison is free and instant. There is no penalty.
