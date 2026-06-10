"""
roles/surveyor.py — SurveyorRole: surveys asteroid, feeds shared survey pool.
States: ORBITING → SURVEYING → REFUELING
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api
import db
from .base import BaseRole
from .miner import _asteroid_cache


class SurveyorRole(BaseRole):
    """Continuously surveys the asteroid and publishes results to the shared pool."""

    async def run(self, stop: asyncio.Event) -> None:
        ctx = self._ctx
        while (ctx is None or not ctx.done.is_set()) and not stop.is_set():
            try:
                await self._run_inner(stop)
                return
            except Exception as e:
                import traceback
                self.log.error("Surveyor crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        ctx = self._ctx
        good = ctx.trade_symbol if ctx else None

        # Pick the same asteroid the miners will target
        ship = await self._get_ship()
        await _asteroid_cache.populate(self._client, self._cfg, self._nav)
        sx, sy = await self._nav.get_coords(ship["nav"]["waypointSymbol"])
        fuel_cap = ship.get("fuel", {}).get("capacity", 0)
        delivery_wp = ctx.destination if ctx else self._cfg.asteroid_base
        asteroid = _asteroid_cache.choose_target(
            good or "", sx, sy, fuel_cap, delivery_wp,
            self._cfg.asteroid, self._nav,
        )
        self.log.info("Surveyor started | target=%s", asteroid)
        wp0 = ship["nav"]["waypointSymbol"]
        f0 = ship.get("fuel", {})
        fuel_pct = f0.get("current", 0) / max(f0.get("capacity", 1), 1)

        if fuel_pct < 0.90 and wp0 != asteroid:
            nearest = await self._nav.nearest_refuel_point(wp0)
            if wp0 != nearest:
                await self._navigate_to(nearest)
            await self._ensure_docked()
            await self._refuel()

        await self._navigate_with_refuel(asteroid)
        await self._ensure_orbit()

        while not stop.is_set() and (ctx is None or not ctx.done.is_set()):
            # Refuel check (only if not at asteroid — surveying is free)
            sv_ship = await self._get_ship()
            sv_fuel = sv_ship.get("fuel", {})
            sv_at = sv_ship["nav"]["waypointSymbol"] == asteroid
            cap = sv_fuel.get("capacity", 0)

            if not sv_at and cap > 0 and sv_fuel.get("current", 0) / cap < 0.50:
                self.log.info("Fuel low — topping up")
                await self._navigate_with_refuel(self._cfg.asteroid_base)
                await self._ensure_docked()
                await self._refuel()
                await self._navigate_with_refuel(asteroid)
                await self._ensure_orbit()

            if stop.is_set() or (ctx and ctx.done.is_set()):
                break

            try:
                await self._nav.wait_cooldown(self.ship_symbol)
                if stop.is_set() or (ctx and ctx.done.is_set()):
                    break

                result = await fleet_api.survey(self._client, self.ship_symbol)
                surveys = result.get("surveys", [])
                if surveys:
                    for sv in surveys:
                        db.upsert_survey(sv)
                    await self._surveys.add(surveys)

                    if good:
                        focused = [
                            s for s in surveys
                            if any(d["symbol"] == good for d in s.get("deposits", []))
                        ]
                        total = sum(
                            sum(1 for d in s["deposits"] if d["symbol"] == good)
                            for s in focused
                        )
                    else:
                        total = len(surveys)

                    pool_size = await self._surveys.size()
                    self.log.info(
                        "%d survey(s) added (%d target hits) | pool: %d",
                        len(surveys), total, pool_size,
                    )
            except SpaceTradersError as e:
                self.log.warning("Survey error: %s", e)
                await asyncio.sleep(10)

        self.log.info("Surveyor done")
