"""
roles/miner_hauler.py — MinerHaulerRole: tender hauler for mining drone teams.

The hauler parks at the active asteroid and waits for mining drones to signal
via asyncio.Events (set by MinerRole when cargo is full).  It transfers the
cargo to itself, then delivers contract good when present and sells junk.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, contracts as contracts_api
from .base import BaseRole, ContractContext
import groups


class MinerHaulerRole(BaseRole):
    """Tender hauler: parks at asteroid, collects ore from mining drones, delivers contract good, sells junk."""

    def __init__(self, workers: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._workers = workers

    async def run(self, stop: asyncio.Event) -> None:
        self.log.info("MinerHaulerRole started | workers=%s", self._workers)
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except Exception as e:
                import traceback
                self.log.error("MinerHauler crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        # Use the active mining target published by MinerRole.
        import roles.miner as _miner_mod
        asteroid = _miner_mod._active_mining_wp or self._cfg.asteroid

        self.log.info("MinerHauler heading to asteroid %s", asteroid)
        await self._navigate_with_refuel(asteroid)
        await self._ensure_orbit()

        # Contract context (may be None if no contract is active)
        ctx: ContractContext | None = self._ctx
        good = ctx.trade_symbol if ctx else None

        while not stop.is_set():
            # Re-read target in case miners moved
            asteroid = _miner_mod._active_mining_wp or self._cfg.asteroid

            hauler_ship  = await self._get_ship()
            hauler_cargo = hauler_ship["cargo"]
            hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

            collected = False
            for worker_sym in self._workers:
                if stop.is_set():
                    break
                evt = groups.get_worker_event(worker_sym)
                if evt is None or not evt.is_set():
                    continue

                worker_ship  = await fleet_api.get_ship(self._client, worker_sym)
                worker_cargo = worker_ship["cargo"]
                if worker_cargo["units"] == 0:
                    evt.clear()
                    continue

                worker_wp = worker_ship["nav"]["waypointSymbol"]
                if hauler_ship["nav"]["waypointSymbol"] != worker_wp:
                    await self._navigate_with_refuel(worker_wp)
                    hauler_ship  = await self._get_ship()
                    hauler_cargo = hauler_ship["cargo"]
                    hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

                if hauler_space == 0:
                    break  # full — sell first

                await self._ensure_orbit()
                for item in worker_cargo.get("inventory", []):
                    xfr = min(item["units"], hauler_space)
                    if xfr <= 0:
                        continue
                    try:
                        await fleet_api.transfer_cargo(
                            self._client, worker_sym, item["symbol"], xfr, self.ship_symbol
                        )
                        self.log.info(
                            "Transferred %dx %s from %s", xfr, item["symbol"], worker_sym
                        )
                        hauler_space -= xfr
                        collected = True
                    except SpaceTradersError as e:
                        self.log.warning("Transfer from %s failed: %s", worker_sym, e)
                    if hauler_space <= 0:
                        break

                evt.clear()

            # Decide whether to sell/deliver
            hauler_ship  = await self._get_ship()
            hauler_cargo = hauler_ship["cargo"]
            hauler_units = hauler_cargo["units"]
            if hauler_units > 0:
                hauler_space = hauler_cargo["capacity"] - hauler_units
                workers_have_cargo = any(
                    (await fleet_api.get_ship(self._client, w))["cargo"]["units"] > 0
                    for w in self._workers
                )
                if hauler_space == 0 or (not workers_have_cargo and not collected):
                    self.log.info(
                        "Hauler cargo %d/%d — heading to base",
                        hauler_units, hauler_cargo["capacity"],
                    )
                    await self._navigate_with_refuel(self._cfg.asteroid_base)
                    await self._ensure_docked()
                    await self._refuel()

                    # Deliver contract good first if we have any
                    if ctx and good and not ctx.done.is_set():
                        have_good = sum(
                            i["units"] for i in (await self._get_ship())["cargo"].get("inventory", [])
                            if i["symbol"] == good
                        )
                        if have_good > 0:
                            try:
                                fc = await contracts_api.get_contract(self._client, ctx.contract_id)
                                for dt in fc.get("terms", {}).get("deliver", []):
                                    if dt["tradeSymbol"] == good:
                                        remaining = dt["unitsRequired"] - dt["unitsFulfilled"]
                                        if remaining <= 0:
                                            ctx.done.set()
                                            break
                                        to_deliver = min(have_good, remaining)
                                        await self._navigate_with_refuel(ctx.destination)
                                        await self._ensure_docked()
                                        result = await contracts_api.deliver_contract(
                                            self._client, ctx.contract_id,
                                            self.ship_symbol, good, to_deliver,
                                        )
                                        await self._record_delivery_and_fulfill(result, ctx, good)
                                        break
                            except SpaceTradersError as e:
                                self.log.warning("MinerHauler delivery error: %s", e)

                    # Refuel from cargo (HYDROCARBON→FUEL) and refine ores if profitable
                    await self._refuel_from_cargo()
                    await self.refine_cargo_for_sale()

                    # Sell junk (keep contract good safe)
                    await self.sell_junk(keep_good=good)

                    # Return to asteroid for next round
                    asteroid = _miner_mod._active_mining_wp or self._cfg.asteroid
                    await self._navigate_with_refuel(asteroid)
                    await self._ensure_orbit()
                    continue

            if not collected:
                # If hauler drifted away from the asteroid (miners moved), reposition
                cur_wp = hauler_ship["nav"]["waypointSymbol"]
                if cur_wp != asteroid:
                    await self._navigate_with_refuel(asteroid)
                    await self._ensure_orbit()
                else:
                    await asyncio.sleep(15)
