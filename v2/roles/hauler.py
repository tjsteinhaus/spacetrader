"""
roles/hauler.py — HaulerRole: waits at asteroid for miner transfers, delivers, sells junk.
States: WAITING → COLLECTING → DELIVERING → SELLING_JUNK → RETURNING
"""
from __future__ import annotations

import asyncio
import time
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, contracts as contracts_api, agent as agent_api
import db
from constants import HAULER_DEPART_FRACTION, HAULER_MAX_WAIT_SECS, HAULER_MIN_CONTRACT_UNITS
from .base import BaseRole


class HaulerRole(BaseRole):
    """Parks at the asteroid, waits for transfers, delivers contract good, sells junk."""

    async def run(self, stop: asyncio.Event) -> None:
        assert self._ctx is not None
        ctx = self._ctx
        while not ctx.done.is_set() and not stop.is_set():
            try:
                await self._run_inner(stop)
                return
            except Exception as e:
                import traceback
                self.log.error("Hauler crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        ctx = self._ctx
        cid = ctx.contract_id
        good = ctx.trade_symbol
        delivery_wp = ctx.destination
        asteroid = self._cfg.asteroid

        self.log.info("Hauler started | good=%s dest=%s", good, delivery_wp)

        # Direct-buy path for non-mineable goods (e.g. IRON, COPPER — processed goods
        # that can't be extracted; must be purchased from a market directly).
        if not db.can_be_mined(good, self._cfg.system):
            await self._direct_buy_loop(stop, ctx, cid, good, delivery_wp)
            return

        # Preflight: fuel up at base, then head to asteroid
        self.log.info("[HAULER] preflight: navigating to base %s", self._cfg.asteroid_base)
        await self._navigate_with_refuel(self._cfg.asteroid_base)
        await self._ensure_docked()
        await self._refuel()
        self.log.info("[HAULER] preflight: navigating to asteroid %s", asteroid)
        await self._navigate_with_refuel(asteroid)
        await self._ensure_orbit()
        self.log.info("[HAULER] preflight done, entering haul loop")

        last_cargo_time = time.monotonic()
        last_log_units = -1

        while not stop.is_set() and not ctx.done.is_set():
            # Ensure at asteroid
            ship = await self._get_ship()
            if ship["nav"]["status"] == "IN_TRANSIT":
                await self._nav.wait_arrival(self.ship_symbol)
                ship = await self._get_ship()
            if ship["nav"]["waypointSymbol"] != asteroid:
                await self._navigate_with_refuel(asteroid)
                await self._ensure_orbit()
                last_cargo_time = time.monotonic()
                continue

            cargo = ship["cargo"]
            units = cargo.get("units", 0)
            capacity = cargo.get("capacity", 1)

            if units > 0:
                last_cargo_time = time.monotonic()

            if units != last_log_units:
                self.log.info("Waiting at asteroid (%d/%d cargo)", units, capacity)
                last_log_units = units

            have_good = sum(
                i["units"] for i in cargo.get("inventory", []) if i["symbol"] == good
            )
            wait_secs = time.monotonic() - last_cargo_time
            should_depart = (
                units >= capacity * HAULER_DEPART_FRACTION
                or (have_good > 0 and have_good >= min(HAULER_MIN_CONTRACT_UNITS, capacity // 4))
                or (units > 0 and wait_secs >= HAULER_MAX_WAIT_SECS)
            )

            if not should_depart:
                await asyncio.sleep(15)
                continue

            self.log.info("Departing: %d/%d cargo | %dx %s", units, capacity, have_good, good)

            # Fuel up at base before the delivery run
            await self._navigate_with_refuel(self._cfg.asteroid_base)
            await self._ensure_docked()
            await self._refuel()

            # Deliver contract good
            if have_good > 0 and not ctx.done.is_set():
                try:
                    fc = await contracts_api.get_contract(self._client, cid)
                    for dt in fc.get("terms", {}).get("deliver", []):
                        if dt["tradeSymbol"] == good:
                            remaining = dt["unitsRequired"] - dt["unitsFulfilled"]
                            if remaining <= 0:
                                ctx.done.set()
                                return
                            to_deliver = min(have_good, remaining)
                            await self._navigate_with_refuel(delivery_wp)
                            await self._ensure_docked()
                            result = await contracts_api.deliver_contract(
                                self._client, cid, self.ship_symbol, good, to_deliver
                            )
                            await self._record_delivery_and_fulfill(result, ctx, good)
                            if ctx.done.is_set():
                                return
                            break
                except SpaceTradersError as e:
                    self.log.warning("Hauler delivery error: %s", e)

            if ctx.done.is_set():
                return

            # Sell junk
            await self.sell_junk(good)

            # Return to base, then asteroid
            await self._navigate_with_refuel(self._cfg.asteroid_base)
            await self._ensure_docked()
            await self._refuel()
            await self._navigate_with_refuel(asteroid)
            await self._ensure_orbit()
            last_cargo_time = time.monotonic()
            last_log_units = -1

        self.log.info("Hauler done")

    async def _direct_buy_loop(
        self,
        stop: asyncio.Event,
        ctx: "ContractContext",
        cid: str,
        good: str,
        delivery_wp: str,
    ) -> None:
        """Buy-and-deliver loop for non-mineable contract goods (e.g. IRON)."""
        from .base import ContractContext  # noqa: F401 (type hint only)
        self.log.info("Direct-buy mode for %s → %s", good, delivery_wp)

        while not stop.is_set() and not ctx.done.is_set():
            # Refresh remaining from API
            try:
                fc = await contracts_api.get_contract(self._client, cid)
            except SpaceTradersError as e:
                self.log.warning("Contract fetch error: %s", e)
                await asyncio.sleep(30)
                continue
            remaining = 0
            for dt in fc.get("terms", {}).get("deliver", []):
                if dt["tradeSymbol"] == good:
                    remaining = dt["unitsRequired"] - dt["unitsFulfilled"]
                    break
            if remaining <= 0:
                ctx.done.set()
                return

            # Find cheapest buy market
            buy_wp = await self._market.best_buy_waypoint(good)
            if not buy_wp:
                # Fall back to DB listing query
                sources = db.can_be_bought(good, self._cfg.system)
                if sources:
                    buy_wp = sources[0]["waypoint_symbol"]
            if not buy_wp:
                self.log.warning("No market found for %s — waiting 5min", good)
                await asyncio.sleep(300)
                continue

            # Navigate directly to buy market (refuel en route if needed)
            await self._navigate_with_refuel(buy_wp)
            await self._ensure_docked()

            # Get live buy price
            await self._market.get_prices(buy_wp, force_refresh=True)
            buy_price = await self._market.get_buy_price(buy_wp, good)
            if buy_price <= 0:
                self.log.warning("No live buy price for %s at %s — waiting 5min", good, buy_wp)
                await asyncio.sleep(300)
                continue

            # Calculate how many units to buy
            me = await agent_api.get_my_agent(self._client)
            available = me["credits"] - self._cfg.credit_reserve
            ship = await self._get_ship()
            cargo = ship["cargo"]
            space = cargo["capacity"] - cargo["units"]
            to_buy = min(space, remaining, max(1, int(available // buy_price)))
            if to_buy <= 0:
                self.log.info("Not enough credits/space for %s @ %d/u — waiting 2min", good, buy_price)
                await asyncio.sleep(120)
                continue

            # Buy in batches
            bought = 0
            batch_size = 40
            while bought < to_buy and not stop.is_set():
                chunk = min(batch_size, to_buy - bought)
                try:
                    result = await fleet_api.purchase_cargo(self._client, self.ship_symbol, good, chunk)
                    tx = result.get("transaction", {})
                    bought += tx.get("units", chunk)
                    self.log.info(
                        "Bought %dx %s @ %d/u | Credits: %d",
                        tx.get("units", chunk), good, tx.get("pricePerUnit", 0),
                        result.get("agent", {}).get("credits", 0),
                    )
                except SpaceTradersError as e:
                    if e.code in (4227, 4604):
                        batch_size = max(1, batch_size // 2)
                        continue
                    self.log.warning("Buy error: %s", e)
                    break

            if bought == 0:
                await asyncio.sleep(60)
                continue

            # Deliver
            try:
                await self._navigate_with_refuel(delivery_wp)
                await self._ensure_docked()
                to_deliver = min(bought, remaining)
                result = await contracts_api.deliver_contract(
                    self._client, cid, self.ship_symbol, good, to_deliver
                )
                await self._record_delivery_and_fulfill(result, ctx, good)
                if ctx.done.is_set():
                    return
            except SpaceTradersError as e:
                self.log.warning("Delivery error: %s", e)
                await asyncio.sleep(30)
