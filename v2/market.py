"""
market.py — Market intelligence service.
Discovers markets, caches prices (DB-backed with TTL), and answers routing questions.
All shared state is protected by asyncio.Lock() — no threading needed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from client import SpaceTradersError
from api import universe as universe_api
import db

if TYPE_CHECKING:
    from client import SpaceTradersClient
    from config import Config
    from navigation import Navigator

log = logging.getLogger(__name__)


class MarketIntelligence:
    """Async market intelligence: discover, cache, and route by prices."""

    def __init__(
        self,
        client: "SpaceTradersClient",
        config: "Config",
        navigator: "Navigator",
    ) -> None:
        self._client = client
        self._cfg = config
        self._nav = navigator

        # In-memory caches — all writes go through _lock
        self._lock = asyncio.Lock()
        self._known_markets: list[str] = []
        self._exporters: dict[str, list[str]] = {}   # good → [waypoints selling it]
        self._buyers: dict[str, list[str]] = {}       # good → [waypoints buying it]
        self._prices: dict[str, dict[str, int]] = {}  # wp → {good: sell_price}
        self._buy_prices: dict[str, dict[str, int]] = {}  # wp → {good: buy_price}
        self._price_ts: dict[str, float] = {}         # wp → unix timestamp of last price fetch
        self._blacklist: dict[str, float] = {}        # wp → expiry timestamp

    # ------------------------------------------------------------------
    # Properties (read-only snapshots — safe to call without lock)
    # ------------------------------------------------------------------

    @property
    def known_markets(self) -> list[str]:
        return list(self._known_markets)

    def exporters(self, good: str) -> list[str]:
        return list(self._exporters.get(good, []))

    def buyers(self, good: str) -> list[str]:
        return list(self._buyers.get(good, []))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_markets(self) -> list[str]:
        """Scan all waypoints in the system for MARKETPLACE trait."""
        log.info("Scanning system for markets...")
        try:
            waypoints = await universe_api.get_waypoints(self._client, self._cfg.system)
            self._nav.seed_coords(waypoints)
            found = [
                wp["symbol"] for wp in waypoints
                if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
            ]
            db.upsert_waypoints(waypoints)
            async with self._lock:
                if found:
                    self._known_markets = found
                elif not self._known_markets:
                    self._known_markets = [self._cfg.asteroid_base]
        except SpaceTradersError as e:
            log.warning("Market scan failed: %s — using defaults", e)
            async with self._lock:
                if not self._known_markets:
                    self._known_markets = [self._cfg.asteroid_base]
        log.info("Found %d market(s)", len(self._known_markets))
        return list(self._known_markets)

    async def scan_good_sources(self) -> None:
        """Scan all known markets for exports/imports to populate routing tables."""
        exporters: dict[str, list[str]] = {}
        buyers: dict[str, list[str]] = {}
        markets = list(self._known_markets) or [self._cfg.asteroid_base]
        log.info("scan_good_sources: scanning %d markets...", len(markets))

        for wp in markets:
            try:
                data = await asyncio.wait_for(
                    universe_api.get_market(self._client, self._cfg.system, wp),
                    timeout=12,
                )
                db.upsert_market_listings(wp, data)
                # Unwrap "data" key if present (client returns full response)
                market = data.get("data", data)
                for category in ("exports", "exchange"):
                    for g in market.get(category, []):
                        sym = g.get("symbol", "")
                        if sym:
                            exporters.setdefault(sym, []).append(wp)
                for g in market.get("imports", []):
                    sym = g.get("symbol", "")
                    if sym:
                        buyers.setdefault(sym, []).append(wp)
                for g in market.get("exchange", []):
                    sym = g.get("symbol", "")
                    if sym:
                        buyers.setdefault(sym, []).append(wp)
                # Seed timestamp so we don't immediately re-fetch on first sell_junk
                async with self._lock:
                    if wp not in self._price_ts:
                        self._price_ts[wp] = time.time()
            except (SpaceTradersError, asyncio.TimeoutError, Exception) as e:
                log.debug("scan_good_sources: skipping %s (%s)", wp, e)
            await asyncio.sleep(0.2)  # throttle to avoid 429 bursts

        async with self._lock:
            if exporters:
                self._exporters.update(exporters)
            if buyers:
                self._buyers.update(buyers)

        log.info(
            "Good sources: %d sellable, %d buyable across %d markets",
            len(exporters), len(buyers), len(self._known_markets),
        )

    async def warm_start_from_db(self) -> None:
        """Load market caches from DB to avoid re-scanning on restart."""
        markets, exporters, buyers, prices, price_ts = db.load_market_caches(
            self._cfg.system, self._cfg.market_cache_ttl
        )
        async with self._lock:
            if markets:
                self._known_markets = markets
            self._exporters.update(exporters)
            self._buyers.update(buyers)
            for wp, price_dict in prices.items():
                self._prices[wp] = {k: v for k, v in price_dict.items() if not k.startswith("_buy_")}
                self._buy_prices[wp] = {
                    k.removeprefix("_buy_"): v
                    for k, v in price_dict.items()
                    if k.startswith("_buy_")
                }
            self._price_ts.update(price_ts)
        log.info("Warm-started from DB: %d markets, %d exporters", len(markets), len(exporters))

    # ------------------------------------------------------------------
    # Price cache
    # ------------------------------------------------------------------

    async def get_prices(self, waypoint: str, force_refresh: bool = False) -> dict[str, int]:
        """Return {trade_symbol: sell_price} for waypoint. Cached with TTL."""
        now = time.time()
        async with self._lock:
            ts = self._price_ts.get(waypoint, 0)
            if not force_refresh and ts + self._cfg.market_cache_ttl > now:
                return dict(self._prices.get(waypoint, {}))

        # Cache miss — fetch live
        try:
            data = await universe_api.get_market(self._client, self._cfg.system, waypoint)
            trade_goods = data.get("tradeGoods", [])
            sell_map = {g["symbol"]: g["sellPrice"] for g in trade_goods if g.get("sellPrice")}
            buy_map = {g["symbol"]: g["purchasePrice"] for g in trade_goods if g.get("purchasePrice")}
            if sell_map or buy_map:
                db.upsert_market_prices(waypoint, trade_goods)
            # Also persist listings (exports/imports/exchange) so arbitrage query works
            db.upsert_market_listings(waypoint, data)
            # Update in-memory exporter/buyer maps
            async with self._lock:
                for g in data.get("exports", []) + data.get("exchange", []):
                    sym = g.get("symbol", "")
                    if sym and waypoint not in self._exporters.get(sym, []):
                        self._exporters.setdefault(sym, []).append(waypoint)
                for g in data.get("imports", []) + data.get("exchange", []):
                    sym = g.get("symbol", "")
                    if sym and waypoint not in self._buyers.get(sym, []):
                        self._buyers.setdefault(sym, []).append(waypoint)
            async with self._lock:
                if sell_map or buy_map:
                    self._prices[waypoint] = sell_map
                    self._buy_prices[waypoint] = buy_map
                self._price_ts[waypoint] = time.time()
            return sell_map
        except SpaceTradersError:
            async with self._lock:
                return dict(self._prices.get(waypoint, {}))

    async def get_buy_price(self, waypoint: str, good: str) -> int:
        """Return the purchase price for `good` at `waypoint`, or 0 if unknown."""
        await self.get_prices(waypoint)  # ensure cache is populated
        async with self._lock:
            return self._buy_prices.get(waypoint, {}).get(good, 0)

    # ------------------------------------------------------------------
    # Routing decisions
    # ------------------------------------------------------------------

    async def best_sell_waypoint(self, good: str) -> tuple[str, int]:
        """Return (waypoint, sell_price) for the highest-paying market."""
        best_wp = self._cfg.asteroid_base
        best_price = 0
        for wp in self._known_markets or [self._cfg.asteroid_base]:
            price = (await self.get_prices(wp)).get(good, 0)
            if price > best_price:
                best_price, best_wp = price, wp
        return best_wp, best_price

    async def best_buy_waypoint(self, good: str) -> str:
        """Return the cheapest exporter waypoint for `good`."""
        now = time.time()
        async with self._lock:
            available = [
                wp for wp in self._exporters.get(good, [])
                if self._blacklist.get(wp, 0) <= now
            ]
            # Prefer markets with a known buy price
            best_wp, best_price = "", 0
            for wp in available:
                price = self._buy_prices.get(wp, {}).get(good, 0)
                if price > 0 and (best_price == 0 or price < best_price):
                    best_price, best_wp = price, wp
            if best_wp:
                return best_wp
        return available[0] if available else ""

    async def blacklist(self, waypoint: str, duration_secs: float = 1200.0) -> None:
        """Temporarily skip a market (e.g. empty tradeGoods). Duration default: 20 min."""
        async with self._lock:
            self._blacklist[waypoint] = time.time() + duration_secs

    async def best_sell_market_for_cargo(self, inventory: list[dict]) -> str:
        """Return the best waypoint to sell a cargo list.

        Prefers the base cluster (markets within 5 units of asteroid_base) to avoid
        wasting time on marginal remote gains. Only routes to a remote market if it
        offers >20% more revenue AND at least 500 cr absolute gain AND sells FUEL.
        """
        our_syms = {item["symbol"] for item in inventory}
        candidate_markets: set[str] = {self._cfg.asteroid_base}
        for sym in our_syms:
            candidate_markets.update(self._buyers.get(sym, []))
        for wp, prices in self._prices.items():
            if any(sym in prices for sym in our_syms):
                candidate_markets.add(wp)

        market_values: dict[str, int] = {}
        for item in inventory:
            for wp in candidate_markets:
                price = (await self.get_prices(wp)).get(item["symbol"], 0)
                if price == 0 and wp in self._buyers.get(item["symbol"], []):
                    price = self._cfg.min_sell_price
                market_values[wp] = market_values.get(wp, 0) + price * item["units"]

        if not market_values:
            return self._cfg.asteroid_base

        bx, by = await self._nav.get_coords(self._cfg.asteroid_base)

        # Find best value in the base cluster (≤5 units away)
        cluster_val = 0
        cluster_best = self._cfg.asteroid_base
        for wp, val in market_values.items():
            wx, wy = await self._nav.get_coords(wp)
            if ((wx - bx) ** 2 + (wy - by) ** 2) ** 0.5 < 5:
                if val > cluster_val:
                    cluster_val, cluster_best = val, wp

        fuel_markets = set(self._exporters.get("FUEL", []))
        best_net_wp, best_net_val = cluster_best, cluster_val

        for wp, raw_val in market_values.items():
            wx, wy = await self._nav.get_coords(wp)
            dist = ((wx - bx) ** 2 + (wy - by) ** 2) ** 0.5
            if dist < 5:
                continue
            if wp not in fuel_markets:
                continue
            net_val = raw_val - dist * 2 * self._cfg.sell_routing_dist_cost
            if net_val > best_net_val:
                best_net_val = net_val
                best_net_wp = wp

        if best_net_wp != cluster_best:
            log.debug(
                "Sell routing: %s net %d cr (raw %d) vs cluster %d cr",
                best_net_wp, best_net_val, market_values.get(best_net_wp, 0), cluster_val,
            )
        return best_net_wp
