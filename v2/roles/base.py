"""
roles/base.py — Abstract base class for all ship roles.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from client import SpaceTradersError
from api import fleet as fleet_api

if TYPE_CHECKING:
    from client import SpaceTradersClient
    from config import Config
    from navigation import Navigator
    from market import MarketIntelligence
    from surveys import SurveyPool


@dataclass
class ContractContext:
    """Shared context passed to every role working on the same contract."""
    contract_id: str
    trade_symbol: str        # what we're delivering
    destination: str         # where to deliver it
    units_required: int
    units_fulfilled: int
    done: asyncio.Event      # set when contract is fulfilled
    fulfill_lock: asyncio.Lock  # prevents double-fulfillment
    # Ships that must buy (not mine) the contract good.
    # Set by the orchestrator when a direct-buy contract is detected.
    forced_buyer_ships: frozenset[str] = frozenset()
    # Ships running in mine-only/sell-only mode (don't try to deliver).
    mine_only_ships: frozenset[str] = frozenset()


class BaseRole(ABC):
    """Abstract base for all ship roles."""

    def __init__(
        self,
        ship_symbol: str,
        config: "Config",
        client: "SpaceTradersClient",
        navigator: "Navigator",
        market: "MarketIntelligence",
        surveys: "SurveyPool",
        contract_ctx: ContractContext | None = None,
    ) -> None:
        self.ship_symbol = ship_symbol
        self._cfg = config
        self._client = client
        self._nav = navigator
        self._market = market
        self._surveys = surveys
        self._ctx = contract_ctx
        self._state = "IDLE"
        self.log = logging.getLogger(f"{self.__class__.__name__}.{ship_symbol}")

    def _transition(self, new_state: str) -> None:
        if new_state != self._state:
            self.log.debug("state: %s → %s", self._state, new_state)
            self._state = new_state

    @abstractmethod
    async def run(self, stop: asyncio.Event) -> None:
        """Main coroutine. Runs until stop is set or contract is done."""

    async def _get_ship(self) -> dict:
        return await fleet_api.get_ship(self._client, self.ship_symbol)

    async def _wait_cooldown(self, ship: dict | None = None) -> None:
        await self._nav.wait_cooldown(self.ship_symbol)

    async def _ensure_orbit(self) -> None:
        await self._nav.ensure_orbit(self.ship_symbol)

    async def _ensure_docked(self) -> None:
        await self._nav.ensure_docked(self.ship_symbol)

    async def _refuel(self) -> None:
        await self._nav.refuel_if_needed(self.ship_symbol, threshold=100_000)

    async def _refuel_from_cargo(self) -> bool:
        """Use FUEL units in cargo to refuel (v1 parity: for siphon ships refining HYDROCARBON).
        Returns True if any FUEL was used from cargo."""
        ship = await self._get_ship()
        fuel_in_cargo = sum(
            i["units"] for i in ship["cargo"].get("inventory", [])
            if i["symbol"] == "FUEL"
        )
        if fuel_in_cargo <= 0:
            return False
        fuel = ship.get("fuel", {})
        cap  = fuel.get("capacity", 0)
        cur  = fuel.get("current", 0)
        need = cap - cur
        if need <= 0:
            return False
        units = min(fuel_in_cargo, need)
        try:
            await self._nav.ensure_docked(self.ship_symbol)
            result = await fleet_api.refuel(self._client, self.ship_symbol, units)
            f = result.get("fuel", {})
            self.log.info(
                "%s refueled from cargo: %d/%d (used %d FUEL units)",
                self.ship_symbol, f.get("current", cur + units), cap, units,
            )
            return True
        except SpaceTradersError as e:
            self.log.debug("refuel_from_cargo failed: %s", e)
            return False

    async def _navigate_to(self, destination: str) -> None:
        await self._nav.navigate_to(self.ship_symbol, destination)

    async def _navigate_with_refuel(self, destination: str) -> None:
        await self._nav.navigate_with_refuel(self.ship_symbol, destination)

    async def _record_delivery_and_fulfill(
        self,
        result: dict,
        ctx: "ContractContext",
        good: str,
    ) -> None:
        """
        Parse a deliver_contract API result, update ctx, call db.record_delivery(),
        and fulfill the contract if all units are delivered. Shared by all roles.
        """
        c = result.get("contract", {})
        for dt in c.get("terms", {}).get("deliver", []):
            if dt["tradeSymbol"] != good:
                continue
            f   = dt["unitsFulfilled"]
            req = dt["unitsRequired"]
            prev_f = getattr(ctx, "_last_fulfilled", 0)
            delivered_this_trip = f - prev_f
            setattr(ctx, "_last_fulfilled", f)
            ctx.units_fulfilled = f
            ctx.units_required  = req
            self.log.info("%d/%d %s delivered", f, req, good)
            # Record delivery event in DB (v1 parity)
            import db as _db
            try:
                _db.record_delivery(ctx.contract_id, good, self.ship_symbol, delivered_this_trip, f, req)
                _db.update_contract_deliverable(ctx.contract_id, good, f)
            except Exception:
                pass
            if f >= req:
                async with ctx.fulfill_lock:
                    if not ctx.done.is_set():
                        try:
                            from api import contracts as _c_api
                            res = await _c_api.fulfill_contract(self._client, ctx.contract_id)
                            ag  = res.get("agent", {})
                            credits_now = ag.get("credits", 0)
                            self.log.info("Contract fulfilled! Credits: %d", credits_now)
                            try:
                                _db.record_credits(credits_now)
                                _db.upsert_contract(res.get("contract", c), fulfilled_now=True)
                            except Exception:
                                pass
                        except SpaceTradersError as e:
                            self.log.warning("Fulfill error: %s", e)
                        ctx.done.set()
            break
        """Refine ores to processed metals when refinement is profitable (v1 parity).

        Uses MODULE_ORE_REFINERY_I or MODULE_GAS_PROCESSOR_I if present.
        Only refines when refined_price > 10 × raw_price (10:1 yield ratio).
        """
        ship = await self._get_ship()
        modules = {m.get("symbol", "") for m in ship.get("modules", [])}
        has_refinery = "MODULE_ORE_REFINERY_I" in modules
        has_gas_proc = "MODULE_GAS_PROCESSOR_I" in modules
        if not (has_refinery or has_gas_proc):
            return

        from constants import SMELTED_GOODS
        inventory = ship["cargo"].get("inventory", [])

        for item in inventory:
            raw = item["symbol"]
            refined = next((r for r, o in SMELTED_GOODS.items() if o == raw), None)
            if not refined:
                continue
            # Get best known sell prices for raw and refined
            raw_price = 0
            refined_price = 0
            for wp in (self._market.known_markets or [self._cfg.asteroid_base]):
                prices = await self._market.get_prices(wp)
                rp = prices.get(raw, 0)
                rfp = prices.get(refined, 0)
                if rp > raw_price:
                    raw_price = rp
                if rfp > refined_price:
                    refined_price = rfp
            # Refine only when refined is worth more than 10× the raw ore price
            if raw_price > 0 and refined_price > raw_price * 10:
                self.log.info(
                    "Refining %dx %s → %s (raw %d/u, refined %d/u)",
                    item["units"], raw, refined, raw_price, refined_price,
                )
                try:
                    from api import fleet as _fleet_api
                    await _fleet_api.refine(self._client, self.ship_symbol, refined)
                except Exception as e:
                    self.log.debug("Refine %s→%s failed: %s", raw, refined, e)

    async def sell_junk(self, keep_good: str | None = None) -> None:
        """Sell/jettison all cargo except keep_good."""
        ship = await self._get_ship()
        inventory = [
            i for i in ship["cargo"].get("inventory", [])
            if i["symbol"] != keep_good
        ]
        if not inventory:
            return

        worth_selling = []
        for item in inventory:
            sym = item["symbol"]
            best_price = 0
            for wp in self._market.known_markets or [self._cfg.asteroid_base]:
                p = (await self._market.get_prices(wp)).get(sym, 0)
                if p > best_price:
                    best_price = p
            # Don't jettison if a known importer exists
            if best_price < self._cfg.min_sell_price and any(
                wp in (self._market.known_markets or [])
                for wp in self._market.buyers(sym)
            ):
                best_price = self._cfg.min_sell_price
            if best_price < self._cfg.min_sell_price:
                try:
                    await fleet_api.jettison(self._client, self.ship_symbol, sym, item["units"])
                    self.log.info("Jettisoned %dx %s (%d cr/u < threshold)", item["units"], sym, best_price)
                except SpaceTradersError:
                    worth_selling.append(item)
            else:
                worth_selling.append(item)

        if not worth_selling:
            return

        target_wp = await self._market.best_sell_market_for_cargo(worth_selling)
        ship = await self._get_ship()
        current_wp = ship["nav"]["waypointSymbol"]
        if current_wp != target_wp or ship["nav"]["status"] != "DOCKED":
            await self._navigate_with_refuel(target_wp)
            await self._ensure_docked()

        ship = await self._get_ship()
        sell_wp = ship["nav"]["waypointSymbol"]
        # Force-refresh prices at the actual sell market to catch crashed/changed markets
        await self._market.get_prices(sell_wp, force_refresh=True)
        import db
        for item in ship["cargo"].get("inventory", []):
            if keep_good and item["symbol"] == keep_good:
                continue
            remaining = item["units"]
            batch_size = 40
            first_price = 0
            while remaining > 0:
                chunk = min(batch_size, remaining)
                try:
                    result = await fleet_api.sell_cargo(self._client, self.ship_symbol, item["symbol"], chunk)
                    tx = result.get("transaction", {})
                    ppu = tx.get("pricePerUnit", 0)
                    units_sold = tx.get("units", chunk)
                    rev = tx.get("totalPrice", 0)
                    if first_price == 0:
                        first_price = ppu
                    elif ppu < first_price * 0.10:
                        self.log.warning(
                            "Price crashed %d/u vs opening %d/u for %s — stopping sell",
                            ppu, first_price, item["symbol"],
                        )
                        remaining = 0
                        break
                    remaining -= units_sold
                    self.log.info(
                        "Sold %dx %s @ %d/u = %d cr",
                        units_sold, tx.get("tradeSymbol", item["symbol"]), ppu, rev,
                    )
                    db.log_transaction(
                        sell_wp, self.ship_symbol, item["symbol"], "SELL",
                        units_sold, ppu, rev,
                    )
                except SpaceTradersError as e:
                    if e.code == 4227:  # unit limit per transaction
                        batch_size = max(1, batch_size // 2)
                        continue
                    try:
                        await fleet_api.jettison(self._client, self.ship_symbol, item["symbol"], remaining)
                        self.log.info("Jettisoned %dx %s", remaining, item["symbol"])
                    except SpaceTradersError:
                        pass
                    break
        await self._refuel()
