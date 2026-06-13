"""
roles/miner_hauler.py — MinerHaulerRole: tender hauler for mining drone teams.

The hauler parks at the active asteroid and waits for mining drones to signal
via asyncio.Events (set by MinerRole when cargo is full).  It transfers the
cargo to itself, then sells everything when full or when all workers are idle.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api
from .base import BaseRole
import groups


class MinerHaulerRole(BaseRole):
    """Tender hauler: parks at asteroid, collects ore from mining drones, sells."""

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

            # Decide whether to sell
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
                        "Hauler cargo %d/%d — selling at base",
                        hauler_units, hauler_cargo["capacity"],
                    )
                    await self._navigate_with_refuel(self._cfg.asteroid_base)
                    await self._ensure_docked()
                    await self._refuel()
                    await self.sell_junk(keep_good=None)
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
