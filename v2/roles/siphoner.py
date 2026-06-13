"""
roles/siphoner.py — SiphonerRole: siphons gas from gas giants, sells proceeds.

When part of a siphon group (registered in groups.py), the siphoner signals
its hauler instead of self-selling — the hauler comes to collect the cargo.
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api
from api import contracts as contracts_api
import db
import groups
from .base import BaseRole


class SiphonerRole(BaseRole):
    """Siphons hydrocarbons/gases from gas giants in the system and sells them."""

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except Exception as e:
                import traceback
                self.log.error("Siphoner crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        self.log.info("Siphoner started")

        gas_giants = self._find_gas_giants()
        if not gas_giants:
            self.log.warning("No gas giants in %s — idle", self._cfg.system)
            await asyncio.sleep(600)
            return

        # Pick the nearest gas giant to ASTEROID_BASE
        target_gg = gas_giants[0]  # caller can set more precise target
        best_dist = float("inf")
        for wp in gas_giants:
            dist = await self._nav.distance(wp, self._cfg.asteroid_base)
            if dist < best_dist:
                best_dist, target_gg = dist, wp

        self.log.info("Targeting gas giant %s", target_gg)
        await self._navigate_with_refuel(target_gg)
        await self._ensure_orbit()

        while not stop.is_set():
            await self._nav.wait_cooldown(self.ship_symbol)
            ship = await self._get_ship()
            cargo = ship.get("cargo", {})

            # Jettison worthless goods so they don't clog the hold
            for item in cargo.get("inventory", []):
                sym = item["symbol"]
                best_price = max(
                    (
                        (await self._market.get_prices(wp)).get(sym, 0)
                        for wp in (self._market.known_markets or [self._cfg.asteroid_base])
                    ),
                    default=0,
                )
                if best_price < 30:
                    try:
                        await fleet_api.jettison(self._client, self.ship_symbol, sym, item["units"])
                        self.log.info("Jettisoned %dx %s (%d cr/u)", item["units"], sym, best_price)
                    except SpaceTradersError:
                        pass
            # Re-fetch cargo after jettison
            ship = await self._get_ship()
            cargo = ship.get("cargo", {})

            if cargo.get("units", 0) >= cargo.get("capacity", 1):
                # ── Grouped mode: signal hauler and wait for pickup ────────
                evt = groups.get_worker_event(self.ship_symbol)
                if evt is not None:
                    evt.set()
                    self.log.info("Cargo full — waiting for hauler pickup")
                    while not stop.is_set() and evt.is_set():
                        await asyncio.sleep(10)
                    # Hauler cleared the event — resume siphoning
                    continue

                # ── Solo mode: self-sell ──────────────────────────────────
                ctx = self._ctx
                contract_good = ctx.trade_symbol if ctx and not ctx.done.is_set() else None
                await self.sell_junk(keep_good=contract_good)
                if contract_good:
                    ship_now = await self._get_ship()
                    have = sum(
                        i["units"] for i in ship_now["cargo"].get("inventory", [])
                        if i["symbol"] == contract_good
                    )
                    if have > 0:
                        await self._navigate_with_refuel(ctx.destination)
                        await self._ensure_docked()
                        try:
                            result = await contracts_api.deliver_contract(
                                self._client, ctx.contract_id, self.ship_symbol,
                                contract_good, have
                            )
                            c = result.get("contract", {})
                            for dt in c.get("terms", {}).get("deliver", []):
                                if dt["tradeSymbol"] == contract_good:
                                    f = dt["unitsFulfilled"]
                                    req = dt["unitsRequired"]
                                    ctx.units_fulfilled = f
                                    ctx.units_required = req
                                    self.log.info("%d/%d %s delivered", f, req, contract_good)
                                    if f >= req:
                                        ctx.done.set()
                        except SpaceTradersError as e:
                            self.log.warning("Delivery error: %s", e)
                await self._navigate_with_refuel(self._cfg.asteroid_base)
                await self._ensure_docked()
                await self._refuel()
                await self._navigate_with_refuel(target_gg)
                await self._ensure_orbit()
                continue

            try:
                result = await fleet_api.siphon(self._client, self.ship_symbol)
                yld = result.get("siphon", {}).get("yield", {})
                cargo_now = result.get("cargo", {})
                cd = result.get("cooldown", {}).get("remainingSeconds", 0)
                self.log.info(
                    "%dx %s | Cargo: %d/%d | CD: %ds",
                    yld.get("units", 0), yld.get("symbol", "?"),
                    cargo_now.get("units", 0), cargo_now.get("capacity", 1), cd,
                )
            except SpaceTradersError as e:
                self.log.warning("Siphon error: %s", e)
                await asyncio.sleep(15)

        self.log.info("Siphoner done")

    def _find_gas_giants(self) -> list[str]:
        with db._conn() as con:
            rows = con.execute(
                "SELECT symbol FROM waypoints WHERE system_symbol = ? AND type = 'GAS_GIANT' ORDER BY symbol",
                (self._cfg.system,),
            ).fetchall()
        return [r[0] for r in rows]
