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

    async def _navigate_to(self, destination: str) -> None:
        await self._nav.navigate_to(self.ship_symbol, destination)

    async def _navigate_with_refuel(self, destination: str) -> None:
        await self._nav.navigate_with_refuel(self.ship_symbol, destination)

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
