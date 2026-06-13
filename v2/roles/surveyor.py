"""
roles/surveyor.py — SurveyorRole: surveys asteroid, feeds shared survey pool.
States: ORBITING → SURVEYING → REFUELING
"""
from __future__ import annotations

import asyncio
import logging
import time

from client import SpaceTradersError
from api import fleet as fleet_api
import db
from .base import BaseRole
from .miner import _asteroid_cache, _active_mining_wp as _mwp_ref  # noqa: F401 — re-exported


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
        import roles.miner as _miner_mod  # live access to module-level _active_mining_wp
        ctx = self._ctx
        good = ctx.trade_symbol if ctx else None

        # Pick the same asteroid the miners will target.
        # Wait up to 30s for the lead miner to publish its choice before falling back.
        _wait_start = time.monotonic()
        while not _miner_mod._active_mining_wp and time.monotonic() - _wait_start < 30:
            await asyncio.sleep(1)

        ship = await self._get_ship()
        await _asteroid_cache.populate(self._client, self._cfg, self._nav)
        sx, sy = await self._nav.get_coords(ship["nav"]["waypointSymbol"])
        fuel_cap = ship.get("fuel", {}).get("capacity", 0)
        delivery_wp = ctx.destination if ctx else self._cfg.asteroid_base

        # Use the miner's chosen asteroid if available, otherwise score independently.
        if _miner_mod._active_mining_wp:
            asteroid = _miner_mod._active_mining_wp
        else:
            asteroid = _asteroid_cache.choose_target(
                good or "", sx, sy, fuel_cap, delivery_wp,
                self._cfg.asteroid, self._nav,
            )

        # Validate the surveyor can actually reach this asteroid.
        # Small-tank ships (80u) can't reach distant asteroids that have no fuel
        # market within cruising distance — they'd drift for hours.
        if fuel_cap > 0 and asteroid != self._cfg.asteroid:
            fuel_markets = self._market.exporters("FUEL")
            can_reach = any(
                True for fm in fuel_markets
                if (await self._nav.distance(asteroid, fm)) <= fuel_cap * 0.90
            ) if fuel_markets else False
            if not can_reach:
                self.log.warning(
                    "No fuel market within %d units of %s — falling back to %s",
                    int(fuel_cap * 0.90), asteroid, self._cfg.asteroid,
                )
                asteroid = self._cfg.asteroid

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
            # Re-check in case miners switched asteroids
            if _miner_mod._active_mining_wp:
                asteroid = _miner_mod._active_mining_wp

            # Refuel check (only if not at asteroid — surveying costs no fuel)
            sv_ship = await self._get_ship()
            sv_fuel = sv_ship.get("fuel", {})
            sv_wp   = sv_ship["nav"]["waypointSymbol"]
            sv_at_asteroid = sv_wp == asteroid
            cap = sv_fuel.get("capacity", 0)

            if not sv_at_asteroid:
                if cap > 0 and sv_fuel.get("current", 0) / cap < 0.50:
                    self.log.info("Fuel low — topping up")
                    await self._navigate_with_refuel(self._cfg.asteroid_base)
                    await self._ensure_docked()
                    await self._refuel()
                    await self._navigate_with_refuel(asteroid)
                    await self._ensure_orbit()
                else:
                    # Miners may have moved — reposition
                    self.log.info("Repositioning to %s", asteroid)
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
