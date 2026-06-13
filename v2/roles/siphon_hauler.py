"""
roles/siphon_hauler.py — SiphonHaulerRole: tender hauler for siphon drone teams.

The hauler parks at the gas giant and waits for siphon drones to signal
via asyncio.Events (set by SiphonerRole when cargo is full).  It transfers
the cargo to itself, then sells everything when full or when all workers are
idle with no more cargo.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api
from .base import BaseRole
import groups


class SiphonHaulerRole(BaseRole):
    """Tender hauler: parks at gas giant, collects from siphon drones, sells."""

    def __init__(self, workers: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._workers = workers

    async def run(self, stop: asyncio.Event) -> None:
        self.log.info("SiphonHaulerRole started | workers=%s", self._workers)
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except Exception as e:
                import traceback
                self.log.error("SiphonHauler crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        import db
        from api import universe as universe_api

        # Locate the nearest gas giant
        with db._conn() as con:
            gas_giants = [
                r[0] for r in con.execute(
                    "SELECT symbol FROM waypoints WHERE system_symbol = ? AND type = 'GAS_GIANT'",
                    (self._cfg.system,),
                ).fetchall()
            ]

        if not gas_giants:
            self.log.warning("No gas giants — SiphonHauler idle")
            await asyncio.sleep(600)
            return

        target_gg = gas_giants[0]
        best_dist = float("inf")
        for wp in gas_giants:
            dist = await self._nav.distance(wp, self._cfg.asteroid_base)
            if dist < best_dist:
                best_dist, target_gg = dist, wp

        self.log.info("SiphonHauler heading to gas giant %s", target_gg)
        await self._navigate_with_refuel(target_gg)

        while not stop.is_set():
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

                # Worker signalled it's full — go collect
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
                    break  # hauler full — sell before collecting more

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

                # Clear ready flag so worker resumes siphoning
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
                        "Hauler cargo %d/%d — selling",
                        hauler_units, hauler_cargo["capacity"],
                    )
                    # Sell everything then return to gas giant
                    await self.sell_junk(keep_good=None)
                    await self._navigate_with_refuel(target_gg)
                    continue

            if not collected:
                await asyncio.sleep(15)
