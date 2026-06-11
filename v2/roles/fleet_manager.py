"""
roles/fleet_manager.py — FleetManagerRole: buys ships, repairs, upgrades mounts.
States: MONITORING → PURCHASING → REPAIRING → UPGRADING
"""
from __future__ import annotations

import asyncio
import logging

from client import SpaceTradersError
from api import fleet as fleet_api, agent as agent_api, universe as universe_api
import db
from constants import SHIP_SCORES, MINING_MOUNT_TIERS
from .base import BaseRole


class FleetManagerRole(BaseRole):
    """Monitors fleet health, buys new ships, repairs damaged ones, upgrades mining mounts."""

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._run_inner(stop)
            except Exception as e:
                import traceback
                self.log.error("FleetManager crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(60)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        self.log.info("FleetManager started")

        while not stop.is_set():
            ships = await fleet_api.get_my_ships(self._client)

            # 1. Repair damaged ships
            for ship in ships:
                if stop.is_set():
                    return
                sym = ship["symbol"]
                def _cond(c: dict) -> float:
                    v = c.get("condition", 1.0)
                    return float(v) / 100.0 if v > 1.0 else float(v)
                worst = min(_cond(ship.get(c, {})) for c in ("frame", "engine", "reactor"))
                if worst < self._cfg.repair_threshold:
                    await self._repair_ship(sym)

            # 2. Upgrade mining mounts
            for ship in ships:
                if stop.is_set():
                    return
                sym = ship["symbol"]
                if not any("MINING" in m.get("symbol", "") for m in ship.get("mounts", [])):
                    continue
                while not stop.is_set():
                    refreshed = await fleet_api.get_ship(self._client, sym)
                    if refreshed.get("nav", {}).get("status") == "IN_TRANSIT":
                        break
                    tier = _best_mount_tier(refreshed)
                    if tier >= len(MINING_MOUNT_TIERS) - 1:
                        break
                    prev = tier
                    await self._upgrade_mounts(sym, refreshed)
                    refreshed2 = await fleet_api.get_ship(self._client, sym)
                    if _best_mount_tier(refreshed2) == prev:
                        break

            # 3. Buy new ships (if enabled)
            if self._cfg.auto_buy_enabled():
                await self._buy_ships()

            # 4. Scan the highest-priority stale market to keep arbitrage DB fresh
            await self._scan_next_market()

            # Sleep between management cycles
            await asyncio.sleep(300)

        self.log.info("FleetManager done")

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------

    async def _repair_ship(self, ship_symbol: str) -> None:
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        if ship.get("nav", {}).get("status") == "IN_TRANSIT":
            self.log.debug("%s in transit — deferring repair", ship_symbol)
            return

        self.log.warning("%s needs repair — heading to shipyard", ship_symbol)
        await self._nav.navigate_with_refuel(ship_symbol, self._cfg.shipyard_wp)
        await self._nav.ensure_docked(ship_symbol)

        try:
            cost_data = await fleet_api.get_repair_cost(self._client, ship_symbol)
            cost = cost_data.get("transaction", {}).get("totalCost", 0)
            me = await agent_api.get_my_agent(self._client)
            if me["credits"] - cost < self._cfg.credit_reserve:
                self.log.warning("Can't afford repair (%d cr) — skipping", cost)
                return
            result = await fleet_api.repair(self._client, ship_symbol)
            tx = result.get("transaction", {})
            self.log.info("%s repaired for %d cr", ship_symbol, tx.get("totalPrice", tx.get("totalCost", 0)))
        except SpaceTradersError as e:
            self.log.warning("Repair failed for %s: %s", ship_symbol, e)

    # ------------------------------------------------------------------
    # Upgrades
    # ------------------------------------------------------------------

    async def _upgrade_mounts(self, ship_symbol: str, ship: dict) -> None:
        tier = _best_mount_tier(ship)
        if tier >= len(MINING_MOUNT_TIERS) - 1:
            return
        target = MINING_MOUNT_TIERS[tier + 1]
        current = MINING_MOUNT_TIERS[tier] if tier >= 0 else "none"
        self.log.info("Upgrading %s: %s → %s", ship_symbol, current, target)

        buy_wp = await self._market.best_buy_waypoint(target)
        if not buy_wp:
            self.log.warning("No market selling %s for %s", target, ship_symbol)
            return

        await self._nav.navigate_with_refuel(ship_symbol, buy_wp)
        await self._nav.ensure_docked(ship_symbol)
        try:
            await fleet_api.purchase_cargo(self._client, ship_symbol, target, 1)
        except SpaceTradersError as e:
            self.log.warning("Could not purchase %s: %s", target, e)
            return

        await self._nav.navigate_with_refuel(ship_symbol, self._cfg.shipyard_wp)
        await self._nav.ensure_docked(ship_symbol)
        try:
            result = await fleet_api.install_mount(self._client, ship_symbol, target)
            ag = result.get("agent", {})
            self.log.info("Installed %s on %s! Credits: %d", target, ship_symbol, ag.get("credits", 0))
        except SpaceTradersError as e:
            self.log.warning("Mount install failed (%s on %s): %s", target, ship_symbol, e)

    # ------------------------------------------------------------------
    # Ship purchasing
    # ------------------------------------------------------------------

    async def _buy_ships(self) -> None:
        """Buy ships from the highest-priority available type at each shipyard."""
        me = await agent_api.get_my_agent(self._client)
        credits = me["credits"]
        if credits < self._cfg.min_buy_credits:
            return

        for shipyard_wp in self._cfg.shipyard_wps:
            try:
                yard_data = await universe_api.get_shipyard(
                    self._client, self._cfg.system, shipyard_wp
                )
                available = yard_data.get("ships", [])
                if not available:
                    continue

                # Score and sort by SHIP_SCORES
                scored = sorted(
                    [(SHIP_SCORES.get(s.get("type", ""), -1), s) for s in available],
                    key=lambda t: t[0],
                    reverse=True,
                )
                for score, ship_info in scored:
                    if score < 0:
                        continue
                    ship_type = ship_info.get("type", "")
                    purchase_price = ship_info.get("purchasePrice", 0)
                    if purchase_price <= 0:
                        continue
                    if credits - purchase_price < self._cfg.credit_reserve:
                        continue

                    # Check if a custom target list restricts purchases
                    targets = self._cfg.get_ship_targets()
                    if targets:
                        matching = next((t for t in targets if t.get("type") == ship_type), None)
                        if not matching:
                            continue
                        # Count current ships of this type
                        current_ships = await fleet_api.get_my_ships(self._client)
                        existing_count = sum(
                            1 for s in current_ships
                            if s.get("registration", {}).get("role", "") == ship_type
                            or any(
                                m.get("symbol", "").startswith(ship_type.replace("SHIP_", ""))
                                for m in s.get("mounts", [])
                            )
                        )
                        if existing_count >= matching.get("max", 99):
                            continue

                    self.log.info(
                        "Buying %s at %s for %d cr (balance: %d)",
                        ship_type, shipyard_wp, purchase_price, credits,
                    )
                    try:
                        result = await fleet_api.purchase_ship(
                            self._client, ship_type, shipyard_wp
                        )
                        new_ship = result.get("ship", {})
                        new_sym = new_ship.get("symbol", "?")
                        ag = result.get("agent", {})
                        credits = ag.get("credits", credits - purchase_price)
                        self.log.info("Purchased %s! Credits now: %d", new_sym, credits)
                        break  # one purchase per shipyard per cycle
                    except SpaceTradersError as e:
                        self.log.warning("Ship purchase failed (%s): %s", ship_type, e)
                        break

            except SpaceTradersError as e:
                self.log.debug("Shipyard %s: %s", shipyard_wp, e)


    async def _scan_next_market(self, staleness: float = 7_200.0) -> None:
        """Navigate to the highest-priority stale/unvisited market and refresh live prices.

        Priority: never-visited first, then most listings, then oldest data.
        """
        import time
        with db._conn() as con:
            rows = con.execute(
                """
                SELECT ml.waypoint_symbol,
                       COUNT(*)                          AS listing_count,
                       COALESCE(MAX(mp.last_updated), 0) AS newest_price
                FROM   market_listings ml
                LEFT JOIN market_prices mp
                       ON ml.waypoint_symbol = mp.waypoint_symbol
                WHERE  ml.waypoint_symbol LIKE ?
                GROUP  BY ml.waypoint_symbol
                HAVING newest_price < ?
                ORDER  BY (newest_price = 0) DESC,
                          listing_count DESC,
                          newest_price ASC
                LIMIT  1
                """,
                (f"{self._cfg.system}-%", time.time() - staleness),
            ).fetchall()

        if not rows:
            return  # all markets are fresh

        target_wp, listing_count, _ = rows[0]
        self.log.info("Scanning market %s (%d listings)", target_wp, listing_count)
        try:
            await self._nav.navigate_with_refuel(self.ship_symbol, target_wp)
            await self._nav.ensure_docked(self.ship_symbol)
            prices = await self._market.get_prices(target_wp, force_refresh=True)
            goods_count = len(prices)
            if goods_count:
                self.log.info("%s: %d goods priced", target_wp, goods_count)
            else:
                self.log.debug("%s: no live trade goods (listing-only market)", target_wp)
        except SpaceTradersError as e:
            self.log.warning("Market scan %s failed: %s", target_wp, e)

def _best_mount_tier(ship: dict) -> int:
    symbols = {m.get("symbol", "") for m in ship.get("mounts", [])}
    for i in range(len(MINING_MOUNT_TIERS) - 1, -1, -1):
        if MINING_MOUNT_TIERS[i] in symbols:
            return i
    return -1
