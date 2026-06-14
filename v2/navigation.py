"""
navigation.py — Async navigation helpers.

Key improvement over v1: wait_arrival() uses a single asyncio.sleep(seconds_to_arrival)
derived from the route.arrival ISO timestamp instead of a polling loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from client import SpaceTradersError
from api import fleet as fleet_api, universe as universe_api

if TYPE_CHECKING:
    from client import SpaceTradersClient
    from config import Config
    from market import MarketIntelligence

log = logging.getLogger(__name__)


def _fmt_secs(secs: float) -> str:
    """Format a duration in seconds as a human-readable string (e.g. '1h28m', '4m32s', '47s')."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def system_of(waypoint: str) -> str:
    """Extract system symbol from waypoint symbol (e.g. 'X1-HU91-B8' → 'X1-HU91')."""
    parts = waypoint.split("-")
    return f"{parts[0]}-{parts[1]}"


class Navigator:
    """All async navigation operations for the fleet."""

    def __init__(
        self,
        client: "SpaceTradersClient",
        config: "Config",
        market: "MarketIntelligence",
    ) -> None:
        self._client = client
        self._cfg = config
        self._market = market
        # Coordinate cache: waypoint → (x, y); populated lazily and from DB.
        self._coords: dict[str, tuple[int, int]] = {}
        # Jump gate cache: system → waypoint symbol of gate.
        self._jump_gate: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    async def get_coords(self, waypoint: str) -> tuple[int, int]:
        """Return (x, y) for a waypoint, DB-first then API, cached in memory."""
        if waypoint in self._coords:
            return self._coords[waypoint]
        import db
        row = db.get_waypoint_coords(waypoint)
        if row:
            self._coords[waypoint] = row
            return row
        try:
            data = await universe_api.get_waypoint(self._client, system_of(waypoint), waypoint)
            coords = (data.get("x", 0), data.get("y", 0))
        except SpaceTradersError:
            coords = (0, 0)
        self._coords[waypoint] = coords
        return coords

    def seed_coords(self, waypoints: list[dict]) -> None:
        """Pre-populate coordinate cache from a waypoints list."""
        for wp in waypoints:
            sym = wp.get("symbol", "")
            if sym:
                self._coords[sym] = (wp.get("x", 0), wp.get("y", 0))

    async def distance(self, wp_a: str, wp_b: str) -> float:
        ax, ay = await self.get_coords(wp_a)
        bx, by = await self.get_coords(wp_b)
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def wait_arrival(self, ship_symbol: str) -> None:
        """Sleep exactly until the ship arrives. Single API call, no polling."""
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        nav = ship.get("nav", {})
        if nav.get("status") != "IN_TRANSIT":
            return
        arrival_str = nav.get("route", {}).get("arrival", "")
        secs = 0
        if arrival_str:
            try:
                dt = datetime.fromisoformat(arrival_str.replace("Z", "+00:00"))
                secs = max(0, (dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                secs = 5
        if secs > 0:
            dest = nav.get("route", {}).get("destination", {}).get("symbol", "?")
            log.info("%s in transit (~%s) → %s", ship_symbol, _fmt_secs(secs), dest)
            await asyncio.sleep(secs + 0.5)  # small buffer for clock skew

    async def wait_cooldown(self, ship_symbol: str) -> None:
        """Sleep until ship cooldown expires."""
        while True:
            try:
                cd = await fleet_api.get_ship_cooldown(self._client, ship_symbol)
                remaining = cd.get("remainingSeconds", 0) if isinstance(cd, dict) else 0
                if remaining <= 0:
                    return
                log.debug("%s cooldown: %ss", ship_symbol, remaining)
                await asyncio.sleep(remaining)
            except SpaceTradersError as e:
                if e.code == 204 or "no cooldown" in str(e).lower():
                    return
                raise
            except Exception:
                # 204 No Content (empty body) causes JSON/AttributeError — treat as no cooldown
                return

    # ------------------------------------------------------------------
    # Orbit / Dock helpers
    # ------------------------------------------------------------------

    async def ensure_orbit(self, ship_symbol: str) -> None:
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        status = ship["nav"]["status"]
        if status == "DOCKED":
            await fleet_api.orbit(self._client, ship_symbol)
            log.info("%s orbiting", ship_symbol)
        elif status == "IN_TRANSIT":
            await self.wait_arrival(ship_symbol)

    async def ensure_docked(self, ship_symbol: str) -> None:
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        status = ship["nav"]["status"]
        if status == "IN_ORBIT":
            await fleet_api.dock(self._client, ship_symbol)
            log.info("%s docked", ship_symbol)
        elif status == "IN_TRANSIT":
            await self.wait_arrival(ship_symbol)
            await fleet_api.dock(self._client, ship_symbol)
            log.info("%s docked", ship_symbol)

    # ------------------------------------------------------------------
    # Refuel helper
    # ------------------------------------------------------------------

    async def refuel_if_needed(self, ship_symbol: str, threshold: int = 200) -> None:
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        fuel = ship.get("fuel", {})
        cap = fuel.get("capacity", 0)
        cur = fuel.get("current", 0)
        if cap > 0 and cur < min(threshold, cap):
            try:
                result = await fleet_api.refuel(self._client, ship_symbol)
                f = result.get("fuel", {})
                log.info("%s refueled: %s/%s", ship_symbol, f.get("current"), f.get("capacity"))
            except SpaceTradersError as e:
                log.warning("%s refuel failed: %s", ship_symbol, e)

    # ------------------------------------------------------------------
    # Core navigation
    # ------------------------------------------------------------------

    async def _find_jump_gate(self, system: str) -> str | None:
        """Return jump gate waypoint for system, cached."""
        if system not in self._jump_gate:
            gates = await universe_api.get_waypoints(self._client, system, "JUMP_GATE")
            if gates:
                self._jump_gate[system] = gates[0]["symbol"]
            else:
                return None
        return self._jump_gate.get(system)

    async def navigate_to(self, ship_symbol: str, destination: str) -> None:
        """Navigate to destination (intra- or inter-system)."""
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        nav = ship["nav"]

        # Already in transit toward destination — just wait
        if nav["status"] == "IN_TRANSIT":
            route_dest = nav.get("route", {}).get("destination", {}).get("symbol", "")
            if route_dest == destination:
                log.debug("%s already en route to %s — waiting", ship_symbol, destination)
                await self.wait_arrival(ship_symbol)
                ship = await fleet_api.get_ship(self._client, ship_symbol)
                if ship["nav"].get("flightMode", "CRUISE") != "CRUISE":
                    await fleet_api.patch_nav(self._client, ship_symbol, "CRUISE")
                return
            # In transit toward somewhere else — wait then redirect
            await self.wait_arrival(ship_symbol)
            ship = await fleet_api.get_ship(self._client, ship_symbol)
            nav = ship["nav"]

        if nav["waypointSymbol"] == destination:
            log.debug("%s already at %s", ship_symbol, destination)
            return

        # Inter-system travel
        dest_sys = system_of(destination)
        curr_sys = system_of(nav["waypointSymbol"])
        if dest_sys != curr_sys:
            await self._inter_system(ship_symbol, ship, destination, curr_sys)
            return

        # Intra-system travel
        await self.ensure_orbit(ship_symbol)
        ship = await fleet_api.get_ship(self._client, ship_symbol)
        cur_fuel = ship.get("fuel", {}).get("current", 0)

        # Use BURN mode if ship has enough fuel for the hop (v1 parity).
        # Burn costs ~2× cruise fuel but is ~2× faster.
        dest_coords = await self.get_coords(destination)
        src_coords  = await self.get_coords(ship["nav"]["waypointSymbol"])
        dist = ((dest_coords[0] - src_coords[0]) ** 2 + (dest_coords[1] - src_coords[1]) ** 2) ** 0.5
        burn_cost = round(dist) * 2
        current_mode = ship["nav"].get("flightMode", "CRUISE")
        use_burn = cur_fuel >= burn_cost and burn_cost > 0

        if use_burn and current_mode != "BURN":
            await fleet_api.patch_nav(self._client, ship_symbol, "BURN")
        elif not use_burn and current_mode != "CRUISE":
            await fleet_api.patch_nav(self._client, ship_symbol, "CRUISE")

        log.info("%s → %s%s", ship_symbol, destination, " [BURN]" if use_burn else "")
        try:
            await fleet_api.navigate(self._client, ship_symbol, destination)
        except SpaceTradersError as e:
            if e.code == 4214:  # already in transit
                await self.wait_arrival(ship_symbol)
                await self.navigate_to(ship_symbol, destination)
                return
            if e.code == 4203:  # insufficient fuel
                await self._drift_fallback(ship_symbol, destination)
                return
            if e.code == 4236:  # ship not in orbit — re-orbit and retry once
                log.warning("%s not in orbit (4236) — re-orbiting and retrying", ship_symbol)
                try:
                    await fleet_api.orbit(self._client, ship_symbol)
                except SpaceTradersError:
                    pass
                await asyncio.sleep(1)
                try:
                    await fleet_api.navigate(self._client, ship_symbol, destination)
                except SpaceTradersError as e2:
                    raise e2
                await self.wait_arrival(ship_symbol)
                # Restore CRUISE after arrival
                await fleet_api.patch_nav(self._client, ship_symbol, "CRUISE")
                return
            raise
        await self.wait_arrival(ship_symbol)
        # Restore CRUISE after BURN hop
        if use_burn:
            try:
                await fleet_api.patch_nav(self._client, ship_symbol, "CRUISE")
            except SpaceTradersError:
                pass

    async def _inter_system(
        self,
        ship_symbol: str,
        ship: dict,
        destination: str,
        curr_sys: str,
    ) -> None:
        modules = [m.get("symbol", "") for m in ship.get("modules", [])]
        has_jump = any("JUMP_DRIVE" in m for m in modules)
        has_warp = any("WARP_DRIVE" in m for m in modules)

        if has_jump:
            gate_wp = await self._find_jump_gate(curr_sys)
            if not gate_wp:
                raise SpaceTradersError(0, f"No jump gate in {curr_sys}")
            current_wp = ship["nav"]["waypointSymbol"]
            if current_wp != gate_wp:
                await self.navigate_to(ship_symbol, gate_wp)
            await self.ensure_orbit(ship_symbol)
            dest_sys = system_of(destination)
            log.info("%s jumping → %s", ship_symbol, dest_sys)
            await fleet_api.jump(self._client, ship_symbol, dest_sys)
            await self.wait_arrival(ship_symbol)
            # If jump landed at gate rather than exact destination, navigate the rest
            after = (await fleet_api.get_ship(self._client, ship_symbol))["nav"]["waypointSymbol"]
            if after != destination:
                await self.navigate_to(ship_symbol, destination)
        elif has_warp:
            await self.ensure_orbit(ship_symbol)
            log.info("%s warping → %s", ship_symbol, destination)
            await fleet_api.warp(self._client, ship_symbol, destination)
            await self.wait_arrival(ship_symbol)
        else:
            raise SpaceTradersError(0, f"{ship_symbol} has no jump/warp drive")

    async def _drift_fallback(self, ship_symbol: str, destination: str) -> None:
        """Emergency DRIFT navigation when fuel is insufficient."""
        log.warning("%s insufficient fuel for %s — trying local refuel", ship_symbol, destination)
        try:
            await self.ensure_docked(ship_symbol)
            await self.refuel_if_needed(ship_symbol, threshold=100_000)
            await fleet_api.orbit(self._client, ship_symbol)
            await fleet_api.navigate(self._client, ship_symbol, destination)
            await self.wait_arrival(ship_symbol)
            return
        except SpaceTradersError:
            pass
        log.warning("%s no fuel locally — emergency DRIFT to %s", ship_symbol, destination)
        await fleet_api.patch_nav(self._client, ship_symbol, "DRIFT")
        try:
            await fleet_api.navigate(self._client, ship_symbol, destination)
        except SpaceTradersError as e:
            if e.code != 4214:
                raise
        await self.wait_arrival(ship_symbol)
        await fleet_api.patch_nav(self._client, ship_symbol, "CRUISE")

    async def navigate_with_refuel(self, ship_symbol: str, destination: str) -> None:
        """Multi-hop navigation: makes intermediate refuel stops when needed."""
        visited: set[str] = set()
        for _hop in range(20):
            ship = await fleet_api.get_ship(self._client, ship_symbol)
            cur_wp = ship["nav"]["waypointSymbol"]
            if cur_wp == destination:
                return

            visited.add(cur_wp)

            fuel = ship.get("fuel", {})
            cur_fuel = fuel.get("current", 0)
            fuel_cap = max(fuel.get("capacity", 1), 1)

            cx, cy = await self.get_coords(cur_wp)
            dx, dy = await self.get_coords(destination)
            dist_to_dest = ((dx - cx) ** 2 + (dy - cy) ** 2) ** 0.5

            if cur_fuel > dist_to_dest:
                await self.navigate_to(ship_symbol, destination)
                return

            # Find the best reachable fuel market that makes progress.
            # Only consider markets where, after refueling to full, the
            # destination is still within range (dead-end filter).
            fuel_markets = self._market.exporters("FUEL")
            markets = fuel_markets if fuel_markets else (self._market.known_markets or [self._cfg.asteroid_base])
            best_wp: str | None = None
            best_remaining = dist_to_dest
            local_wp: str | None = None  # same-coords market (free refuel, no progress)

            for wp in markets:
                if wp == cur_wp or wp in visited:
                    continue
                wx, wy = await self.get_coords(wp)
                hop_dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
                if hop_dist > cur_fuel:
                    continue  # can't reach with current fuel
                if hop_dist < 1.0:
                    # Same-location market (e.g. C51 next to C50) — free refuel, no progress
                    if local_wp is None:
                        local_wp = wp
                    continue
                remaining = ((dx - wx) ** 2 + (dy - wy) ** 2) ** 0.5
                if remaining >= dist_to_dest:
                    continue  # doesn't make progress toward destination
                if remaining < best_remaining:
                    best_remaining = remaining
                    best_wp = wp

            # Fall back to a local same-coords refuel when no progress-making hop exists
            if best_wp is None:
                best_wp = local_wp

            if best_wp is not None:
                log.debug("%s refuel hop → %s (en route to %s)", ship_symbol, best_wp, destination)
                await self.navigate_to(ship_symbol, best_wp)
                await self.ensure_docked(ship_symbol)
                await self.refuel_if_needed(ship_symbol, threshold=100_000)
                continue

            # No fuel market can bridge the gap — try a CRUISE hop to any
            # waypoint that makes geometric progress before drifting the rest.
            # CRUISE is ~3× faster than DRIFT so even one hop helps significantly.
            # Only use a waypoint as a cruise hop if:
            #   (a) it's a fuel market (we can refuel and continue), OR
            #   (b) the destination is directly reachable from it at full tank.
            # Hopping to a dead-end non-fuel waypoint just shifts where we drift.
            best_cruise_wp: str | None = None
            best_cruise_remaining = dist_to_dest
            fuel_market_set = set(self._market.exporters("FUEL"))
            for wp, (wx, wy) in list(self._coords.items()):
                if wp == cur_wp or wp in visited:
                    continue
                hop_dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
                if hop_dist > cur_fuel:
                    continue  # can't reach in CRUISE with current fuel
                remaining = ((dx - wx) ** 2 + (dy - wy) ** 2) ** 0.5
                # Skip non-fuel waypoints from which the destination is still out of range
                if wp not in fuel_market_set and remaining > fuel_cap:
                    continue
                if remaining < best_cruise_remaining:
                    best_cruise_remaining = remaining
                    best_cruise_wp = wp

            if best_cruise_wp:
                log.debug(
                    "%s cruise hop → %s (closing drift gap to %s)",
                    ship_symbol, best_cruise_wp, destination,
                )
                await self.navigate_to(ship_symbol, best_cruise_wp)
                continue  # re-evaluate from new position with updated fuel

            log.warning("%s no reachable intermediate waypoint en route to %s — drifting", ship_symbol, destination)
            await self.navigate_to(ship_symbol, destination)
            return

        # Hop limit reached
        await self.navigate_to(ship_symbol, destination)

    async def can_reach(self, from_wp: str, to_wp: str, fuel_cap: int) -> bool:
        """Return True if a ship with fuel_cap can reach to_wp from from_wp
        via refuel hops. Uses iterative BFS over cached coords — never blocks
        the event loop."""
        await asyncio.sleep(0)  # yield once so other tasks can run
        if fuel_cap <= 0:
            return True

        def _coords(wp: str) -> tuple[int, int]:
            if wp in self._coords:
                return self._coords[wp]
            import db as _db
            row = _db.get_waypoint_coords(wp)
            if row:
                self._coords[wp] = row
                return row
            return (0, 0)

        fuel_markets = self._market.exporters("FUEL") or [self._cfg.asteroid_base]
        tx, ty = _coords(to_wp)

        reachable: set[str] = {from_wp}
        frontier: set[str] = {from_wp}
        while frontier:
            new_frontier: set[str] = set()
            for cur in frontier:
                cx, cy = _coords(cur)
                # Can we reach the destination directly from here?
                if ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5 <= fuel_cap:
                    return True
                # Expand to fuel markets reachable from here
                for wp in fuel_markets:
                    if wp in reachable:
                        continue
                    wx, wy = _coords(wp)
                    if ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5 <= fuel_cap:
                        reachable.add(wp)
                        new_frontier.add(wp)
            frontier = new_frontier
        return False

    async def nearest_refuel_point(self, from_wp: str) -> str:
        fuel_markets = self._market.exporters("FUEL")
        candidates = fuel_markets if fuel_markets else (self._market.known_markets or [self._cfg.asteroid_base])
        fx, fy = await self.get_coords(from_wp)
        best_wp = self._cfg.asteroid_base
        best_dist = float("inf")
        for wp in candidates:
            wx, wy = await self.get_coords(wp)
            dist = ((wx - fx) ** 2 + (wy - fy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist, best_wp = dist, wp
        return best_wp
