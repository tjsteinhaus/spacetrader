"""
roles/trader.py — TraderRole: arbitrage trading between markets.

Mirrors v1 trader logic:
  1. Pre-check: if ship already has cargo, sell it first.
  2. Check credits vs reserve.
  3. Find best arbitrage opportunity from DB.
  4. Navigate to buy market, refresh live prices, re-evaluate.
  5. Buy in batches; abort if margin shrank.
  6. Navigate to sell market, sell in batches with 10% price floor.
  7. Backhaul: after selling, check if sell market has good outbound opp.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, agent as agent_api
import db
import discord_notify as discord
from .base import BaseRole

# Trading constants
TRADER_MIN_MARGIN     = 150
TRADER_CREDIT_RESERVE = 150_000   # keep this much in reserve for mining/repair ops
TRADER_MIN_UNITS      = 5
PRICE_FLOOR_RATIO     = 0.10   # stop selling if price drops below 10% of first batch

log = logging.getLogger(__name__)


class TraderRole(BaseRole):
    """Arbitrage trader: buy cheap, sell high between markets."""

    async def run(self, stop: asyncio.Event) -> None:
        self.log.info("TraderRole.run() started")
        await asyncio.sleep(3)  # stagger startup so miners/surveyors get first API slots
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except asyncio.CancelledError:
                self.log.info("TraderRole cancelled")
                raise
            except Exception as e:
                import traceback
                self.log.error("Trader crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        # ── Pre-check: sell any existing cargo first ──
        try:
            ship = await asyncio.wait_for(self._get_ship(), timeout=15)
        except asyncio.TimeoutError:
            self.log.warning("get_ship timed out — will retry")
            return
        inventory = [i for i in ship["cargo"].get("inventory", []) if i["units"] > 0]
        if inventory:
            item = inventory[0]
            good = item["symbol"]
            have = item["units"]
            # Force-refresh all candidate sell markets to avoid stale crash prices
            sell_candidates = [
                o for o in db.get_arbitrage_opportunities(self._cfg.system, min_margin=0)
                if o["trade_symbol"] == good
            ]
            best_sell_price = 0
            sell_wp = None
            for o in sell_candidates:
                wp = o["sell_at"]
                live_data = await self._market.get_prices(wp, force_refresh=True)
                live_price = live_data.get(good, 0)
                if live_price > best_sell_price:
                    best_sell_price = live_price
                    sell_wp = wp
            if sell_wp is None:
                sell_wp, best_sell_price = await self._market.best_sell_waypoint(good)
            # Look up best known buy price as cost floor
            buy_floor = min(
                (o["buy_price"] for o in sell_candidates if o["buy_price"] > 0),
                default=0,
            )
            # Don't sell into a crashed market — wait for recovery
            if best_sell_price < max(100, buy_floor * 0.5):
                self.log.warning(
                    "Pre-check: %s best sell price %d/u too low (buy floor %d) — waiting 5min",
                    good, best_sell_price, buy_floor,
                )
                await asyncio.sleep(300)
                return
            self.log.info("Pre-check: carrying %dx %s — selling at %s (%d/u)", have, good, sell_wp, best_sell_price)
            await self._navigate_with_refuel(sell_wp)
            await self._ensure_docked()
            sell_wp_actual = (await self._get_ship())["nav"]["waypointSymbol"]
            await self._sell_batched(good, have, sell_wp_actual, stop, min_sell_price=buy_floor)
            # Backhaul check after sell
            await self._try_backhaul(sell_wp_actual, stop)
            return

        # ── Credits check ──
        agent = await agent_api.get_my_agent(self._client)
        credits = agent["credits"]
        available = credits - TRADER_CREDIT_RESERVE
        if available < TRADER_MIN_UNITS * 100:
            self.log.info(
                "Not enough credits for trade (have %d, reserve %d) — waiting 2min",
                credits, TRADER_CREDIT_RESERVE,
            )
            await asyncio.sleep(120)
            return

        # ── Find best arbitrage opportunity ──
        opps = db.get_arbitrage_opportunities(self._cfg.system, min_margin=TRADER_MIN_MARGIN)
        opps = [o for o in opps if o["buy_price"] > 0]
        if not opps:
            self.log.info("No arbitrage opportunities in DB — refreshing all market prices now")
            for wp in self._market.known_markets or []:
                await self._market.get_prices(wp, force_refresh=True)
            opps = db.get_arbitrage_opportunities(self._cfg.system, min_margin=TRADER_MIN_MARGIN)
            opps = [o for o in opps if o["buy_price"] > 0]
        if not opps:
            self.log.info("Still no arbitrage opportunities after scan — waiting 5min")
            await asyncio.sleep(300)
            return

        best = opps[0]
        good       = best["trade_symbol"]
        buy_wp     = best["buy_at"]
        sell_wp    = best["sell_at"]
        cached_buy = best["buy_price"]

        # ── Navigate to buy market ──
        await self._navigate_with_refuel(buy_wp)
        await self._ensure_docked()

        # ── Refresh live prices and re-evaluate ──
        await self._market.get_prices(buy_wp, force_refresh=True)
        live_buy  = await self._market.get_buy_price(buy_wp, good)
        if live_buy <= 0:
            live_buy = cached_buy
        # Force-refresh sell price — stale cache can show pre-crash prices
        live_sell_data = await self._market.get_prices(sell_wp, force_refresh=True)
        live_sell = live_sell_data.get(good, best["sell_price"])
        # Scan other sell markets for this good in case sell_wp is crashed
        other_sell_wps = {o["sell_at"] for o in db.get_arbitrage_opportunities(self._cfg.system, min_margin=0)
                         if o["trade_symbol"] == good and o["sell_at"] != sell_wp}
        for wp in other_sell_wps:
            wp_data = await self._market.get_prices(wp, force_refresh=True)
            wp_sell = wp_data.get(good, 0)
            if wp_sell > live_sell:
                live_sell = wp_sell
                sell_wp = wp

        live_margin = live_sell - live_buy
        live_roi    = live_margin / live_buy if live_buy > 0 else 0
        if live_margin < TRADER_MIN_MARGIN:
            self.log.warning(
                "Margin shrank at %s: %d/u margin (%.0f%% ROI) — re-scanning",
                buy_wp, live_margin, live_roi * 100,
            )
            return

        if live_buy != cached_buy:
            self.log.info("Live buy %d/u for %s (was %d cached)", live_buy, good, cached_buy)

        # ── How many to buy ──
        ship = await self._get_ship()
        cargo  = ship["cargo"]
        space  = cargo["capacity"] - cargo["units"]
        to_buy = min(space, available // live_buy) if live_buy > 0 else 0
        if to_buy < TRADER_MIN_UNITS:
            self.log.info(
                "Not enough credits/space: can buy %d units of %s @ %d/u — waiting 2min",
                to_buy, good, live_buy,
            )
            await asyncio.sleep(120)
            return

        # ── Buy in batches ──
        bought     = 0
        total_cost = 0
        batch_size = 40
        while bought < to_buy and not stop.is_set():
            chunk = min(batch_size, to_buy - bought)
            try:
                result = await fleet_api.purchase_cargo(self._client, self.ship_symbol, good, chunk)
                tx = result.get("transaction", {})
                units_bought = tx.get("units", chunk)
                ppu = tx.get("pricePerUnit", live_buy)
                credits_after = result.get("agent", {}).get("credits", 0)
                bought += units_bought
                total_cost += units_bought * ppu
                self.log.info(
                    "Bought %dx %s @ %d/u | Credits: %d",
                    units_bought, good, ppu, credits_after,
                )
                # Stop if buy price is no longer profitable vs expected sell price
                if live_sell - ppu < TRADER_MIN_MARGIN:
                    self.log.info(
                        "Buy price %d/u leaves only %d/u margin vs sell %d/u — stopping buy",
                        ppu, live_sell - ppu, live_sell,
                    )
                    break
                # Stop if remaining credits would fall below reserve
                if credits_after < TRADER_CREDIT_RESERVE:
                    self.log.info(
                        "Credits %d near reserve %d — stopping buy",
                        credits_after, TRADER_CREDIT_RESERVE,
                    )
                    break
            except SpaceTradersError as e:
                if e.code in (4227, 4604):  # unit limit per transaction
                    batch_size = max(1, batch_size // 2)
                    continue
                self.log.warning("Buy error: %s", e)
                break

        if bought == 0:
            return

        est_profit = bought * (live_sell - live_buy)
        discord.send_trade_start(self.ship_symbol, good, buy_wp, sell_wp, bought, live_buy, est_profit)

        # ── Navigate to sell market ──
        await self._navigate_with_refuel(sell_wp)
        await self._ensure_docked()
        sell_wp_actual = (await self._get_ship())["nav"]["waypointSymbol"]

        # ── Sell in batches with price floor ──
        revenue = await self._sell_batched(good, bought, sell_wp_actual, stop, min_sell_price=live_buy)
        discord.send_trade_finish(
            self.ship_symbol, good, buy_wp, sell_wp_actual,
            bought, total_cost, revenue, revenue - total_cost,
        )

        # ── Backhaul: check if sell market has a good outbound route ──
        await self._try_backhaul(sell_wp_actual, stop)

    async def _sell_batched(
        self,
        good: str,
        total_units: int,
        sell_wp: str,
        stop: asyncio.Event,
        min_sell_price: int = 0,
    ) -> int:
        """Sell `good` in batches; stop early if price crashes below 10% of first batch or cost floor. Returns total revenue."""
        ship      = await self._get_ship()
        have      = sum(i["units"] for i in ship["cargo"].get("inventory", []) if i["symbol"] == good)
        to_sell   = min(total_units, have)
        sold      = 0
        total_rev = 0
        batch_size  = 40
        first_price = 0

        while sold < to_sell and not stop.is_set():
            chunk = min(batch_size, to_sell - sold)
            try:
                result = await fleet_api.sell_cargo(self._client, self.ship_symbol, good, chunk)
                tx         = result.get("transaction", {})
                ppu        = tx.get("pricePerUnit", 0)
                units_sold = tx.get("units", chunk)
                rev        = tx.get("totalPrice", 0)

                if first_price == 0:
                    first_price = ppu
                # Stop if price crashed below 10% of opening price
                if ppu < first_price * PRICE_FLOOR_RATIO:
                    self.log.warning(
                        "Price crashed %d/u vs opening %d/u for %s — stopping sell",
                        ppu, first_price, good,
                    )
                    break
                # Stop if selling below cost (avoid selling at a loss)
                if min_sell_price > 0 and ppu < min_sell_price * 0.85:
                    self.log.warning(
                        "Sell price %d/u below cost floor %d/u for %s — stopping sell",
                        ppu, min_sell_price, good,
                    )
                    break

                sold      += units_sold
                total_rev += rev
                db.log_transaction(sell_wp, self.ship_symbol, good, "SELL", units_sold, ppu, rev)
                self.log.info("Sold %dx %s @ %d/u = %d cr | Credits: %d",
                    units_sold, good, ppu, rev,
                    result.get("agent", {}).get("credits", 0),
                )
            except SpaceTradersError as e:
                if e.code in (4227, 4604):  # unit limit per transaction
                    batch_size = max(1, batch_size // 2)
                    continue
                self.log.warning("Sell error for %s: %s", good, e)
                break

        if sold > 0:
            self.log.info("Total: sold %dx %s = %d cr", sold, good, total_rev)
        return total_rev

    async def _try_backhaul(self, current_wp: str, stop: asyncio.Event) -> None:
        """After selling at current_wp, check if there's a good outbound buy→sell route."""
        if stop.is_set():
            return
        opps = [
            o for o in db.get_arbitrage_opportunities(self._cfg.system, min_margin=TRADER_MIN_MARGIN)
            if o["buy_at"] == current_wp
            and o["buy_price"] > 0
            and (o["sell_price"] - o["buy_price"]) / o["buy_price"] >= TRADER_MIN_ROI
        ]
        if not opps:
            return

        agent = await agent_api.get_my_agent(self._client)
        credits   = agent["credits"]
        available = credits - TRADER_CREDIT_RESERVE
        if available <= 0:
            return

        bh = opps[0]
        good   = bh["trade_symbol"]
        bh_buy = bh["buy_price"]
        ship   = await self._get_ship()
        cargo  = ship["cargo"]
        space  = cargo["capacity"] - cargo["units"]
        to_buy = min(space, available // bh_buy) if bh_buy > 0 else 0
        if to_buy < TRADER_MIN_UNITS:
            return

        self.log.info(
            "Backhaul: buying %dx %s @ %d → selling at %s",
            to_buy, good, bh_buy, bh["sell_at"],
        )

        # Buy right here (already docked)
        bought     = 0
        batch_size = 40
        while bought < to_buy and not stop.is_set():
            chunk = min(batch_size, to_buy - bought)
            try:
                result = await fleet_api.purchase_cargo(self._client, self.ship_symbol, good, chunk)
                tx = result.get("transaction", {})
                bought += tx.get("units", chunk)
                self.log.info("BH Bought %dx %s @ %d/u", tx.get("units", chunk), good, tx.get("pricePerUnit", bh_buy))
            except SpaceTradersError as e:
                if e.code == 4227:
                    batch_size = max(1, batch_size // 2)
                    continue
                break

        if bought == 0:
            return

        # Sell at destination
        await self._navigate_with_refuel(bh["sell_at"])
        await self._ensure_docked()
        bh_sell_wp = (await self._get_ship())["nav"]["waypointSymbol"]
        await self._sell_batched(good, bought, bh_sell_wp, stop)
