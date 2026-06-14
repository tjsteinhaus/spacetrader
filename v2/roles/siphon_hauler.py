"""
roles/siphon_hauler.py — SiphonHaulerRole: tender hauler for siphon drone teams.

The hauler parks at the gas giant and waits for siphon drones to signal
via asyncio.Events (set by SiphonerRole when cargo is full).  It transfers
the cargo to itself, delivers contract good when present, then sells junk.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, contracts as contracts_api
from .base import BaseRole, ContractContext
import groups


class SiphonHaulerRole(BaseRole):
    """Tender hauler: parks at gas giant, collects from siphon drones, delivers contract good, sells junk."""

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

        # Contract context (may be None if no contract active)
        ctx: ContractContext | None = self._ctx
        good = ctx.trade_symbol if ctx else None

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

            # Decide whether to deliver/sell
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
                                self.log.warning("SiphonHauler delivery error: %s", e)

                    # Try to refuel from FUEL cargo before sell run (v1 parity)
                    await self._refuel_from_cargo()
                    await self.refine_cargo_for_sale()

                    # Sell junk (keep contract good safe), then return to gas giant
                    await self.sell_junk(keep_good=good)
                    await self._navigate_with_refuel(target_gg)
                    continue

            if not collected:
                await asyncio.sleep(15)
