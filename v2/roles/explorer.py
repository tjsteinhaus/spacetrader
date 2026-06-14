"""
roles/explorer.py — ExplorerRole: jumps to adjacent systems, scans markets for arbitrage.
States: JUMPING → SCANNING → LOGGING_ARBITRAGE → RESTING
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, universe as universe_api
import db
from .base import BaseRole


class ExplorerRole(BaseRole):
    """Explores adjacent systems via jump gate and logs arbitrage opportunities."""

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except Exception as e:
                import traceback
                self.log.error("Explorer crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        from navigation import system_of
        self.log.info("Explorer started")

        while not stop.is_set():
            ship = await self._get_ship()
            has_jump  = any("JUMP_DRIVE" in m.get("symbol", "") for m in ship.get("modules", []))
            has_sensor = any("SENSOR" in m.get("symbol", "") for m in ship.get("mounts", []))

            if not has_jump:
                # No jump drive — fall back to probe market patrol mode (v1 parity).
                # Pick an assigned market from DB bot_setting or default to asteroid_base,
                # then continually refresh prices there.
                import db as _db
                import time as _time
                patrol_wp = _db.get_bot_setting(
                    f"probe_patrol_{self.ship_symbol}", self._cfg.asteroid_base
                )
                self.log.info(
                    "%s has no jump drive — probe patrol mode at %s", self.ship_symbol, patrol_wp
                )
                await self._navigate_with_refuel(patrol_wp)
                await self._ensure_docked()
                await self._market.get_prices(patrol_wp, force_refresh=True)
                self.log.info("Probe patrolled %s — sleeping 300s", patrol_wp)
                await asyncio.sleep(300)
                continue

            # Ensure we have a jump gate to work from
            curr_sys = system_of(ship["nav"]["waypointSymbol"])
            gates = await universe_api.get_waypoints(self._client, curr_sys, "JUMP_GATE")
            if not gates:
                self.log.warning("No jump gate in %s — explorer idle", curr_sys)
                await asyncio.sleep(600)
                continue

            # Navigate to jump gate
            gate_wp = gates[0]["symbol"]
            if ship["nav"]["waypointSymbol"] != gate_wp:
                await self._navigate_with_refuel(gate_wp)

            # Scan adjacent systems from the gate
            try:
                await self._ensure_orbit()
                scan_result = await fleet_api.scan_systems(self._client, self.ship_symbol)
                adjacent = scan_result.get("systems", [])
                self.log.info("Scanning %d adjacent systems", len(adjacent))
            except SpaceTradersError as e:
                self.log.warning("System scan failed: %s", e)
                await asyncio.sleep(60)
                continue

            # Jump to each adjacent system, scan waypoints and markets
            for sys_data in adjacent[:3]:  # limit to 3 per sweep to avoid time sink
                if stop.is_set():
                    break
                target_sys = sys_data.get("symbol", "")
                if not target_sys:
                    continue

                try:
                    # Jump to any waypoint in the target system
                    target_wps = await universe_api.get_waypoints(self._client, target_sys)
                    if not target_wps:
                        continue
                    target_wp = target_wps[0]["symbol"]

                    self.log.info("Jumping → %s", target_sys)
                    await self._ensure_orbit()
                    await fleet_api.jump(self._client, self.ship_symbol, target_sys)
                    await self._nav.wait_arrival(self.ship_symbol)

                    db.upsert_waypoints(target_wps)

                    # Scan markets in this system for arbitrage
                    markets = [
                        wp["symbol"] for wp in target_wps
                        if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
                    ]
                    for mwp in markets[:5]:
                        try:
                            market_data = await universe_api.get_market(
                                self._client, target_sys, mwp
                            )
                            db.upsert_market_listings(mwp, market_data)
                            await self._log_arbitrage(mwp, market_data, target_sys)
                        except SpaceTradersError:
                            pass

                    # Return home
                    await self._ensure_orbit()
                    await fleet_api.jump(self._client, self.ship_symbol, curr_sys)
                    await self._nav.wait_arrival(self.ship_symbol)

                except SpaceTradersError as e:
                    self.log.warning("Jump/scan failed for %s: %s", target_sys, e)

            # Rest before next sweep
            self.log.info("Explorer sweep done — resting 10 min")
            await asyncio.sleep(600)

        self.log.info("Explorer done")

    async def _log_arbitrage(
        self, waypoint: str, market_data: dict, system: str
    ) -> None:
        """Log goods priced >30% above home system prices."""
        home_prices = {}
        for wp in self._market.known_markets:
            for good, price in (await self._market.get_prices(wp)).items():
                if good not in home_prices or price > home_prices[good]:
                    home_prices[good] = price

        for tg in market_data.get("tradeGoods", []):
            sym = tg.get("symbol", "")
            sell = tg.get("sellPrice", 0)
            home = home_prices.get(sym, 0)
            if home > 0 and sell > home * 1.30:
                markup = (sell - home) / home * 100
                self.log.info(
                    "Arbitrage: %s @ %s = %d cr (+%.0f%% vs home %d cr)",
                    sym, waypoint, sell, markup, home,
                )
