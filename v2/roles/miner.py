"""
roles/miner.py — MinerRole: mines at asteroid, delivers contract good, sells junk.
States: IDLE → MINING → CARGO_FULL → DELIVERING → SELLING_JUNK → REFUELING → REPAIRING → BUYING
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from client import SpaceTradersError
from api import fleet as fleet_api, contracts as contracts_api, agent as agent_api
from constants import MINEABLE_GOODS, GOOD_TO_DEPOSIT_TRAITS, ASTEROID_TRAIT_SCORES, ASTEROID_TYPES
import db
import groups
from .base import BaseRole

if TYPE_CHECKING:
    from roles.base import ContractContext

log = logging.getLogger(__name__)


class AsteroidCache:
    """Scores all asteroids in the system and caches the result. Shared across miners."""

    def __init__(self) -> None:
        self._scored: list[dict] = []
        self._traits: dict[str, frozenset[str]] = {}
        self._coords: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def populate(self, client, config, navigator) -> None:
        async with self._lock:
            if self._scored:
                return
            from api import universe as universe_api
            waypoints = db.get_all_waypoints(config.system)
            if not waypoints:
                try:
                    waypoints = await universe_api.get_waypoints(client, config.system)
                except SpaceTradersError as e:
                    log.warning("Asteroid cache fetch failed: %s", e)
                    return

            coords: dict[str, tuple[int, int]] = {}
            traits_map: dict[str, frozenset[str]] = {}
            base_candidates: list[str] = []
            market_wps: list[str] = []

            for wp in waypoints:
                sym = wp["symbol"]
                coords[sym] = (wp.get("x", 0), wp.get("y", 0))
                wp_traits = frozenset(t["symbol"] for t in wp.get("traits", []))
                traits_map[sym] = wp_traits
                if wp["type"] == "ASTEROID_BASE":
                    base_candidates.append(sym)
                if "MARKETPLACE" in wp_traits:
                    market_wps.append(sym)
                navigator.seed_coords([wp])

            if not base_candidates:
                base_candidates = market_wps or [config.asteroid_base]

            results: list[dict] = []
            for wp in waypoints:
                if wp["type"] not in ASTEROID_TYPES:
                    continue
                sym = wp["symbol"]
                traits = traits_map.get(sym, frozenset())
                score = sum(ASTEROID_TRAIT_SCORES.get(t, 0) for t in traits)
                if score <= -9000:
                    continue

                ax, ay = coords[sym]
                nearest_base, n_dist = config.asteroid_base, float("inf")
                for bc in base_candidates:
                    bx, by = coords.get(bc, (0, 0))
                    d = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                    if d < n_dist:
                        n_dist, nearest_base = d, bc

                results.append({
                    "symbol": sym,
                    "x": ax,
                    "y": ay,
                    "traits": traits,
                    "trait_score": score,
                    "nearest_base": nearest_base,
                    "base_dist": n_dist,
                })

            self._scored = results
            self._traits = {r["symbol"]: r["traits"] for r in results}
            self._coords = coords
            log.info("Asteroid cache: %d candidates", len(results))

    def choose_target(
        self,
        good: str,
        ship_x: int,
        ship_y: int,
        fuel_cap: int,
        delivery_wp: str,
        default: str,
        navigator,
    ) -> str:
        if not self._scored:
            return default

        deposit_traits = GOOD_TO_DEPOSIT_TRAITS.get(good, frozenset())

        def score(ast: dict) -> float:
            s = float(ast["trait_score"])
            if deposit_traits & ast["traits"]:
                s += 80.0
            round_trip = ast["base_dist"] * 2.0
            if fuel_cap > 0:
                ratio = round_trip / fuel_cap
                if ratio <= 1.0:
                    s += 20.0
                elif ratio <= 2.0:
                    s -= 10.0
                elif ratio <= 4.0:
                    s -= 35.0
                else:
                    s -= 80.0
            dist_from_ship = ((ast["x"] - ship_x) ** 2 + (ast["y"] - ship_y) ** 2) ** 0.5
            if dist_from_ship < 50:
                s += 15.0
            else:
                s -= min(150.0, dist_from_ship * 0.10)

            # First-trip drift penalty: ship burns dist_from_ship getting here
            # then needs base_dist to return.  If that exceeds the fuel tank the
            # ship will be forced to drift home on the very first trip.
            if fuel_cap > 0 and (dist_from_ship + ast["base_dist"]) > fuel_cap:
                s -= 60.0

            # Penalize by distance to delivery waypoint — closer = faster cycle time.
            del_x, del_y = self._coords.get(delivery_wp, (0, 0))
            dist_to_delivery = ((ast["x"] - del_x) ** 2 + (ast["y"] - del_y) ** 2) ** 0.5
            s -= dist_to_delivery * 0.12  # ~12 pts per 100 units to delivery

            return s

        scored = sorted(self._scored, key=score, reverse=True)
        return scored[0]["symbol"] if scored else default


# Module-level cache shared across all miners in the same run
_asteroid_cache = AsteroidCache()

# Tracks which asteroid active miners chose — surveyor follows this.
_active_mining_wp: str = ""


class MinerRole(BaseRole):
    """Mines contract goods at the best asteroid, delivers, and sells junk."""

    async def run(self, stop: asyncio.Event) -> None:
        assert self._ctx is not None
        while not self._ctx.done.is_set() and not stop.is_set():
            try:
                await self._run_inner(stop)
                return
            except Exception as e:
                import traceback
                self.log.error("Miner crashed: %s\n%s", e, traceback.format_exc())
                await asyncio.sleep(30)

    async def _run_inner(self, stop: asyncio.Event) -> None:
        ctx = self._ctx
        good = ctx.trade_symbol
        delivery_wp = ctx.destination
        cid = ctx.contract_id

        self.log.info("Miner started | good=%s dest=%s", good, delivery_wp)

        # Populate asteroid scoring cache (idempotent)
        await _asteroid_cache.populate(self._client, self._cfg, self._nav)

        # Choose best asteroid
        ship = await self._get_ship()
        ship_x, ship_y = await self._nav.get_coords(ship["nav"]["waypointSymbol"])
        fuel_cap = ship.get("fuel", {}).get("capacity", 0)
        mining_target = _asteroid_cache.choose_target(
            good, ship_x, ship_y, fuel_cap, delivery_wp, self._cfg.asteroid, self._nav
        )
        # Use shared surveys — both miner and surveyor now use the same choose_target logic
        # so they will always converge on the same asteroid.
        use_shared_surveys = True
        self.log.info("Mining target: %s", mining_target)

        # Publish the chosen asteroid so surveyors can follow.
        global _active_mining_wp
        _active_mining_wp = mining_target

        # Preflight refuel
        wp0 = ship["nav"]["waypointSymbol"]
        f0 = ship.get("fuel", {})
        fuel_pct = f0.get("current", 0) / max(f0.get("capacity", 1), 1)
        if fuel_pct < 0.90 and wp0 != mining_target:
            if wp0 != self._cfg.asteroid_base:
                await self._navigate_with_refuel(self._cfg.asteroid_base)
            await self._ensure_docked()
            await self._refuel()

        # Preflight delivery shortcut: if we already have enough good, deliver first
        cargo0 = ship.get("cargo", {})
        have_preflight = sum(
            i["units"] for i in cargo0.get("inventory", []) if i["symbol"] == good
        )
        if have_preflight > 0 and not ctx.done.is_set():
            await self._deliver_if_have(cid, good, delivery_wp, have_preflight)
            if ctx.done.is_set():
                return

        # Decide mine vs direct-buy
        buy_wp = await self._market.best_buy_waypoint(good)
        buy_price = await self._market.get_buy_price(buy_wp, good) if buy_wp else 0
        is_mineable = good in MINEABLE_GOODS
        no_deposit = is_mineable and not db.can_be_mined(good, self._cfg.system)

        # Forced buyer: the orchestrator assigned this ship to buy (not mine) the contract good.
        forced_buyer = self.ship_symbol in (ctx.forced_buyer_ships if ctx else frozenset())
        mine_only    = self.ship_symbol in (ctx.mine_only_ships    if ctx else frozenset())

        direct_buy = bool(
            buy_wp
            and (
                not is_mineable
                or no_deposit
                or (buy_price > 0 and buy_price <= self._cfg.cheap_buy_threshold)
                or forced_buyer
            )
        )

        if mine_only:
            # This ship mines for income; it never delivers the contract good directly.
            direct_buy = False

        if direct_buy:
            reason = (
                "forced buyer" if forced_buyer and is_mineable and not no_deposit
                else "no deposit" if no_deposit
                else "cheap" if is_mineable
                else "non-mineable"
            )
            self.log.info("%s: direct-buy mode (%s @ %d cr/u)", good, reason, buy_price)
            if (no_deposit or not is_mineable or forced_buyer) and buy_wp:
                # Check reachability before committing to a distant buy market
                _db_ship = await self._get_ship()
                _db_cur = _db_ship["nav"]["waypointSymbol"]
                can_buy = await self._nav.can_reach(_db_cur, buy_wp, fuel_cap)
                can_deliver = await self._nav.can_reach(buy_wp, delivery_wp, fuel_cap)
                if not can_buy or not can_deliver:
                    self.log.warning(
                        "%s: route %s→%s→%s unreachable (fuel cap %d) — waiting for contract",
                        good, _db_cur, buy_wp, delivery_wp, fuel_cap,
                    )
                    await asyncio.sleep(600)
                    return
                await self._navigate_with_refuel(buy_wp)
            else:
                await self._navigate_with_refuel(self._cfg.asteroid_base)
            active_survey = None
            empty_loads = 3
        else:
            await self._navigate_with_refuel(mining_target)
            await self._ensure_orbit()
            active_survey = await self._surveys.get_best(good) if use_shared_surveys else None
            if active_survey is None:
                active_survey = await self._do_survey(good)
            empty_loads = 0
            dry_extractions = 0

        while not stop.is_set() and not ctx.done.is_set():
            # Refresh survey from shared pool
            if active_survey is None and use_shared_surveys:
                active_survey = await self._surveys.get_best(good)

            # Get current ship state
            loop_ship = await self._get_ship()
            fuel = loop_ship.get("fuel", {})
            at_asteroid = loop_ship["nav"]["waypointSymbol"] == mining_target
            at_buy_wp = direct_buy and loop_ship["nav"]["waypointSymbol"] == buy_wp

            # Refuel if needed (not while at asteroid — mining is free)
            fuel_cap2 = fuel.get("capacity", 0)
            if not at_asteroid and not at_buy_wp and fuel_cap2 > 0:
                if fuel.get("current", 0) / fuel_cap2 < 0.40:
                    self.log.info("Fuel low, topping up at base")
                    await self._navigate_with_refuel(self._cfg.asteroid_base)
                    await self._ensure_docked()
                    await self._refuel()
                    if not direct_buy:
                        await self._navigate_with_refuel(mining_target)
                        await self._ensure_orbit()
                        active_survey = (
                            await self._surveys.get_best(good) if use_shared_surveys else None
                        ) or await self._do_survey(good)

            # Repair check
            if loop_ship.get("frame") or loop_ship.get("engine"):
                def _cond(c: dict) -> float:
                    v = c.get("condition", 1.0)
                    return float(v) / 100.0 if v > 1.0 else float(v)
                worst = min(_cond(loop_ship.get(c, {})) for c in ("frame", "engine", "reactor"))
                if worst < self._cfg.repair_threshold:
                    self.log.warning("Condition %.0f%% — repairing", worst * 100)
                    await self._repair()
                    await self._navigate_with_refuel(mining_target)
                    await self._ensure_orbit()
                    active_survey = (
                        await self._surveys.get_best(good) if use_shared_surveys else None
                    ) or await self._do_survey(good)

            loop_cargo = loop_ship.get("cargo", {})
            loop_space = loop_cargo.get("capacity", 0) - loop_cargo.get("units", 0)
            have_cached = sum(
                i["units"] for i in loop_cargo.get("inventory", []) if i["symbol"] == good
            )

            skip_to_buy = direct_buy and empty_loads >= 3 and not ctx.done.is_set()
            force_buy = (
                not direct_buy
                and dry_extractions >= self._cfg.dry_extract_threshold
                and bool(await self._market.best_buy_waypoint(good))
                and not ctx.done.is_set()
            )

            if loop_space < 5 or (have_cached > 0 and not at_asteroid) or skip_to_buy or force_buy:
                # ── Grouped mode: cargo full → signal hauler and wait ─────────
                if loop_space < 5 and not direct_buy and groups.is_grouped_worker(self.ship_symbol):
                    evt = groups.get_worker_event(self.ship_symbol)
                    if evt is not None:
                        evt.set()
                        self.log.info("Cargo full — waiting for hauler pickup")
                        while not stop.is_set() and evt.is_set():
                            await asyncio.sleep(10)
                        # Hauler cleared the event — resume mining
                        continue

                if have_cached > 0 and not ctx.done.is_set():
                    if not direct_buy:
                        empty_loads = 0
                    dry_extractions = 0
                    await self._deliver(cid, good, delivery_wp, have_cached)
                    if ctx.done.is_set():
                        return
                    if not direct_buy:
                        nearest = await self._nav.nearest_refuel_point(delivery_wp)
                        await self._navigate_with_refuel(nearest)
                        await self._ensure_docked()
                        await self._refuel()
                        await self._navigate_with_refuel(mining_target)
                        await self._ensure_orbit()
                else:
                    if force_buy and empty_loads < 3:
                        self.log.warning("%d dry extractions — switching to buy mode", dry_extractions)
                        empty_loads = 3
                        dry_extractions = 0
                    empty_loads += 1
                    if not direct_buy:
                        await self._navigate_with_refuel(self._cfg.asteroid_base)
                        await self._ensure_docked()
                        await self._refuel()
                        await self.sell_junk(good)
                        await self._navigate_with_refuel(self._cfg.asteroid_base)
                        await self._ensure_docked()
                        await self._refuel()

                    if empty_loads >= 3 and not ctx.done.is_set():
                        await self._buy_good(cid, good, direct_buy, mining_target, use_shared_surveys)
                        buy_wp = await self._market.best_buy_waypoint(good)
                        active_survey = await self._surveys.get_best(good) if use_shared_surveys else None

                continue

            if ctx.done.is_set():
                break

            # Navigate to asteroid if we drifted away (e.g. after preflight delivery)
            if not at_asteroid:
                self.log.info("Not at asteroid (%s) — navigating to %s", loop_ship["nav"]["waypointSymbol"], mining_target)
                await self._navigate_with_refuel(mining_target)
                await self._ensure_orbit()
                active_survey = (
                    await self._surveys.get_best(good) if use_shared_surveys else None
                ) or await self._do_survey(good)
                continue

            # Extract
            try:
                if active_survey:
                    try:
                        result = await fleet_api.extract_with_survey(
                            self._client, self.ship_symbol, active_survey
                        )
                    except SpaceTradersError as e:
                        if e.code in (4224, 4000) or "survey" in str(e).lower():
                            active_survey = await self._surveys.get_best(good) if use_shared_surveys else None
                            if active_survey:
                                result = await fleet_api.extract_with_survey(
                                    self._client, self.ship_symbol, active_survey
                                )
                            else:
                                result = await fleet_api.extract(self._client, self.ship_symbol)
                        else:
                            raise
                else:
                    result = await fleet_api.extract(self._client, self.ship_symbol)

                yld = result.get("extraction", {}).get("yield", {})
                cargo_now = result.get("cargo", {})
                cd = result.get("cooldown", {}).get("remainingSeconds", 0)
                have_now = sum(
                    i["units"] for i in cargo_now.get("inventory", []) if i["symbol"] == good
                )
                self.log.info(
                    "%dx %s | %s: %d | Cargo: %d/%d | CD: %ds",
                    yld.get("units", 0), yld.get("symbol", "?"),
                    good, have_now,
                    cargo_now.get("units", 0), cargo_now.get("capacity", 1),
                    cd,
                )
                if yld.get("symbol") == good:
                    dry_extractions = 0
                else:
                    dry_extractions += 1

                if yld.get("symbol"):
                    db.log_extraction(
                        mining_target, self.ship_symbol,
                        active_survey.get("signature") if active_survey else None,
                        yld["symbol"], yld.get("units", 0),
                    )
                await self._nav.wait_cooldown(self.ship_symbol)
            except SpaceTradersError as e:
                if e.code != 4228:  # 4228 = cargo full, handled above
                    self.log.error("Extract error: %s", e)
                    await asyncio.sleep(5)

        self.log.info("Miner done")

    # ------------------------------------------------------------------
    # Delivery helpers
    # ------------------------------------------------------------------

    async def _deliver_if_have(
        self, cid: str, good: str, delivery_wp: str, have: int
    ) -> None:
        """Preflight shortcut: deliver if we already have enough."""
        ctx = self._ctx
        try:
            fc = await contracts_api.get_contract(self._client, cid)
            for dt in fc.get("terms", {}).get("deliver", []):
                if dt["tradeSymbol"] == good:
                    remaining = dt["unitsRequired"] - dt["unitsFulfilled"]
                    if have >= remaining > 0:
                        await self._navigate_with_refuel(self._cfg.asteroid_base)
                        if ctx.done.is_set():
                            return
                        await self._ensure_docked()
                        await self._refuel()
                        await self._navigate_with_refuel(delivery_wp)
                        await self._ensure_docked()
                        to_deliver = min(have, remaining)
                        result = await contracts_api.deliver_contract(
                            self._client, cid, self.ship_symbol, good, to_deliver
                        )
                        await self._check_fulfilled(result, cid, good)
                    break
        except SpaceTradersError as e:
            self.log.debug("Preflight delivery check: %s", e)

    async def _deliver(
        self, cid: str, good: str, delivery_wp: str, have: int
    ) -> None:
        ctx = self._ctx
        # Cap to remaining needed
        try:
            fc = await contracts_api.get_contract(self._client, cid)
            for dt in fc.get("terms", {}).get("deliver", []):
                if dt["tradeSymbol"] == good:
                    remaining = dt["unitsRequired"] - dt["unitsFulfilled"]
                    if remaining <= 0:
                        ctx.done.set()
                        return
                    have = min(have, remaining)
                    break
        except SpaceTradersError:
            pass

        await self._navigate_with_refuel(self._cfg.asteroid_base)
        await self._ensure_docked()
        await self._refuel()
        await self._navigate_with_refuel(delivery_wp)
        await self._ensure_docked()
        try:
            result = await contracts_api.deliver_contract(
                self._client, cid, self.ship_symbol, good, have
            )
            await self._check_fulfilled(result, cid, good)
            # Sell any arbitrage goods we brought along
            await self.sell_junk(keep_good=good)
        except SpaceTradersError as e:
            if not ctx.done.is_set():
                self.log.warning("Delivery error: %s", e)

    async def _check_fulfilled(self, result: dict, cid: str, good: str) -> None:
        ctx = self._ctx
        c = result.get("contract", {})
        for dt in c.get("terms", {}).get("deliver", []):
            if dt["tradeSymbol"] == good:
                f = dt["unitsFulfilled"]
                req = dt["unitsRequired"]
                ctx.units_fulfilled = f
                ctx.units_required = req
                delivered_this_trip = f - getattr(ctx, "_last_fulfilled", 0)
                setattr(ctx, "_last_fulfilled", f)
                self.log.info("%d/%d %s delivered", f, req, good)
                try:
                    db.record_delivery(cid, good, self.ship_symbol, delivered_this_trip, f, req)
                except Exception:
                    pass
                # Update DB so dashboard reflects live progress
                try:
                    import db as _db
                    _db.update_contract_deliverable(ctx.contract_id, good, f)
                except Exception:
                    pass
                if f >= req:
                    async with ctx.fulfill_lock:
                        if not ctx.done.is_set():
                            try:
                                res = await contracts_api.fulfill_contract(self._client, cid)
                                ag = res.get("agent", {})
                                credits_now = ag.get("credits", 0)
                                self.log.info("Contract fulfilled! Credits: %d", credits_now)
                                try:
                                    db.record_credits(credits_now)
                                except Exception:
                                    pass
                            except SpaceTradersError as e:
                                self.log.warning("Fulfill error: %s", e)
                            ctx.done.set()

    async def _buy_good(
        self,
        cid: str,
        good: str,
        direct_buy: bool,
        mining_target: str,
        use_shared_surveys: bool,
    ) -> None:
        ctx = self._ctx
        buy_wp = await self._market.best_buy_waypoint(good)
        if not buy_wp:
            return

        # Skip if the ship can't reach the buy market without drifting
        ship_now = await self._get_ship()
        fuel_cap = ship_now.get("fuel", {}).get("capacity", 0)
        current_wp = ship_now["nav"]["waypointSymbol"]
        reachable = await self._nav.can_reach(current_wp, buy_wp, fuel_cap)
        if not reachable:
            self.log.warning(
                "Buy market %s unreachable without drift (fuel cap %d) — waiting for contract",
                buy_wp, fuel_cap,
            )
            await asyncio.sleep(300)
            return

        await self._navigate_with_refuel(buy_wp)
        await self._ensure_docked()

        # Sell any junk cargo before buying so all slots are free for the contract good.
        junk_ship = await self._get_ship()
        has_junk = any(
            i["symbol"] != good and i.get("units", 0) > 0
            for i in junk_ship["cargo"].get("inventory", [])
        )
        if has_junk:
            await self.sell_junk(keep_good=good)
            # Refuel after junk run in case we moved to sell
            await self._navigate_with_refuel(buy_wp)
            await self._ensure_docked()

        buy_price = await self._market.get_buy_price(buy_wp, good)
        if buy_price <= 0:
            # Force cache refresh while docked
            prices = await self._market.get_prices(buy_wp)
            buy_price = prices.get(good, 0)

        if buy_price > 0:
            try:
                me = await agent_api.get_my_agent(self._client)
                ship_now = await self._get_ship()
                free = ship_now["cargo"]["capacity"] - ship_now["cargo"]["units"]

                still_need = 9999
                try:
                    fc2 = await contracts_api.get_contract(self._client, cid)
                    for dt2 in fc2.get("terms", {}).get("deliver", []):
                        if dt2["tradeSymbol"] == good:
                            still_need = dt2["unitsRequired"] - dt2["unitsFulfilled"]
                            break
                except SpaceTradersError:
                    pass

                buy_reserve = (
                    max(5_000, self._cfg.credit_reserve // 6)
                    if direct_buy
                    else self._cfg.credit_reserve
                    if still_need > 5
                    else max(5_000, self._cfg.credit_reserve // 4)
                )
                affordable = max(0, (me["credits"] - buy_reserve) // buy_price)
                to_buy = min(free, affordable, max(0, still_need))
                if to_buy > 0:
                    # Buy in chunks to respect per-transaction market limits
                    tx_limit = 10_000  # start high; will shrink on error 4604
                    remaining_to_buy = to_buy
                    while remaining_to_buy > 0:
                        batch = min(remaining_to_buy, tx_limit)
                        try:
                            result = await fleet_api.purchase_cargo(
                                self._client, self.ship_symbol, good, batch
                            )
                        except SpaceTradersError as e:
                            if e.code == 4604:
                                # Per-transaction limit hit — halve and retry
                                tx_limit = max(1, batch // 2)
                                self.log.info("Transaction limit hit, retrying with %d units", tx_limit)
                                continue
                            raise
                        ag = result.get("agent", {})
                        tx = result.get("transaction", {})
                        bought = tx.get("units", batch)
                        self.log.info(
                            "Bought %dx %s @ %d/u = %d cr | balance: %d",
                            bought, good, buy_price, tx.get("totalPrice", 0), ag.get("credits", 0),
                        )
                        db.log_transaction(
                            buy_wp, self.ship_symbol, good, "PURCHASE",
                            bought, buy_price, tx.get("totalPrice", 0),
                        )
                        remaining_to_buy -= bought
                        # Update tx_limit from actual transaction if smaller
                        if bought < batch:
                            tx_limit = bought
                else:
                    if direct_buy:
                        self.log.warning("Can't afford %s — mining for income", good)
                        await self._navigate_with_refuel(mining_target)
                        await self._ensure_orbit()

                # ── Arbitrage fill: use spare capacity for profitable goods ──
                if direct_buy and ctx and not ctx.done.is_set():
                    await self._fill_arbitrage(buy_wp, ctx.destination, good)

            except SpaceTradersError as e:
                self.log.warning("Purchase failed: %s", e)

    async def _fill_arbitrage(self, buy_wp: str, delivery_wp: str, skip_good: str) -> None:
        """Fill spare cargo with goods that have positive margin at the delivery waypoint."""
        try:
            ship_now = await self._get_ship()
            free = ship_now["cargo"]["capacity"] - ship_now["cargo"]["units"]
            if free < 1:
                return

            me = await agent_api.get_my_agent(self._client)
            credits_avail = max(0, me["credits"] - max(5_000, self._cfg.credit_reserve // 6))
            if credits_avail < 100:
                return

            # Get buy prices at current waypoint and sell prices at delivery
            await self._market.get_prices(buy_wp)   # populate cache
            sell_prices = await self._market.get_prices(delivery_wp)

            candidates = []
            for sym in list(sell_prices):
                if sym == skip_good:
                    continue
                buy_price = await self._market.get_buy_price(buy_wp, sym)
                if buy_price <= 0:
                    continue
                sell_price = sell_prices.get(sym, 0)
                if sell_price > buy_price:
                    candidates.append((sym, buy_price, sell_price - buy_price))

            if not candidates:
                return

            candidates.sort(key=lambda x: x[2], reverse=True)
            sym, buy_price, margin = candidates[0]

            affordable = credits_avail // buy_price
            to_buy = min(free, affordable)
            if to_buy < 1:
                return

            tx_limit = 10_000
            remaining = to_buy
            while remaining > 0:
                batch = min(remaining, tx_limit)
                try:
                    result = await fleet_api.purchase_cargo(self._client, self.ship_symbol, sym, batch)
                except SpaceTradersError as e:
                    if e.code == 4604:
                        tx_limit = max(1, batch // 2)
                        continue
                    self.log.warning("Arbitrage buy failed: %s", e)
                    return
                tx = result.get("transaction", {})
                bought = tx.get("units", batch)
                self.log.info(
                    "Arbitrage: bought %dx %s @ %d/u (margin +%d/u)",
                    bought, sym, buy_price, margin,
                )
                db.log_transaction(
                    buy_wp, self.ship_symbol, sym, "PURCHASE",
                    bought, buy_price, tx.get("totalPrice", 0),
                )
                remaining -= bought
                if bought < batch:
                    tx_limit = bought
        except Exception as e:
            self.log.warning("Arbitrage fill error: %s", e)

    async def _do_survey(self, good: str) -> dict | None:
        try:
            await self._nav.wait_cooldown(self.ship_symbol)
            result = await fleet_api.survey(self._client, self.ship_symbol)
            surveys = result.get("surveys", [])
            for sv in surveys:
                db.upsert_survey(sv)
            await self._surveys.add(surveys)
            best = await self._surveys.get_best(good)
            if best:
                count = sum(1 for d in best.get("deposits", []) if d["symbol"] == good)
                self.log.debug("Survey: %dx %s deposits (size: %s)", count, good, best.get("size"))
            return best or (surveys[0] if surveys else None)
        except SpaceTradersError as e:
            self.log.debug("Survey failed: %s — mining without survey", e)
            return None

    async def _repair(self) -> None:
        await self._navigate_to(self._cfg.shipyard_wp)
        await self._ensure_docked()
        try:
            cost_data = await fleet_api.get_repair_cost(self._client, self.ship_symbol)
            cost = cost_data.get("transaction", {}).get("totalCost", 0)
            me = await agent_api.get_my_agent(self._client)
            if me["credits"] - cost < self._cfg.credit_reserve:
                self.log.warning("Cannot afford repair (%d cr) — skipping", cost)
                return
            result = await fleet_api.repair(self._client, self.ship_symbol)
            tx = result.get("transaction", {})
            self.log.info("Repaired for %d cr", tx.get("totalPrice", tx.get("totalCost", 0)))
        except SpaceTradersError as e:
            self.log.warning("Repair failed: %s", e)
