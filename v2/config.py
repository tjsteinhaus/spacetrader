"""
config.py — Runtime configuration for the v2 bot.
Single source of truth for all tunables and auto-detected ship/waypoint values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from constants import (
    DEFAULT_CREDIT_RESERVE, DEFAULT_MIN_BUY_CREDITS, DEFAULT_REPAIR_THRESHOLD,
    DEFAULT_MIN_SELL_PRICE, DEFAULT_MARKET_CACHE_TTL, DEFAULT_DRY_EXTRACT_THRESHOLD,
    DEFAULT_CHEAP_BUY_THRESHOLD, DEFAULT_SELL_ROUTING_DIST_COST,
    DEFAULT_MIN_CONTRACT_PAYOUT, DEFAULT_MIN_FUEL_CAPACITY,
    ASTEROID_TRAIT_SCORES, ASTEROID_TYPES,
)
import db

if TYPE_CHECKING:
    from client import SpaceTradersClient


@dataclass
class Config:
    # Agent identity
    callsign: str = ""
    system: str = ""
    command_ship: str = ""
    fleet_manager_ship: str = ""
    faction_hq_wp: str = ""

    # Key waypoints (auto-detected or loaded from DB)
    asteroid: str = ""
    asteroid_base: str = ""
    shipyard_wp: str = ""
    shipyard_wps: list[str] = field(default_factory=list)

    # Credit thresholds
    credit_reserve: int = DEFAULT_CREDIT_RESERVE
    min_buy_credits: int = DEFAULT_MIN_BUY_CREDITS

    # Operational thresholds
    repair_threshold: float = DEFAULT_REPAIR_THRESHOLD
    min_sell_price: int = DEFAULT_MIN_SELL_PRICE
    market_cache_ttl: int = DEFAULT_MARKET_CACHE_TTL
    dry_extract_threshold: int = DEFAULT_DRY_EXTRACT_THRESHOLD
    cheap_buy_threshold: int = DEFAULT_CHEAP_BUY_THRESHOLD
    sell_routing_dist_cost: int = DEFAULT_SELL_ROUTING_DIST_COST
    min_contract_payout: int = DEFAULT_MIN_CONTRACT_PAYOUT
    min_fuel_capacity: int = DEFAULT_MIN_FUEL_CAPACITY

    # Feature flags
    auto_buy_ships: bool = True

    # Strategy file path
    strategy_file: Path = field(default_factory=lambda: Path(__file__).parent / "strategy.json")

    def auto_buy_enabled(self) -> bool:
        """Check DB for runtime override; fallback to dataclass field."""
        val = db.get_bot_setting("auto_buy_ships")
        if val:
            return val.lower() == "true"
        return self.auto_buy_ships

    def get_ship_targets(self) -> list[dict]:
        """Return ship purchase targets from DB, e.g. [{'type': 'SHIP_SURVEYOR', 'max': 1}]."""
        import json
        raw = db.get_bot_setting("ship_buy_list")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return []

    async def auto_configure(self, client: "SpaceTradersClient") -> None:
        """Detect callsign, system, asteroid, base, and shipyard from the live API.

        On the first run for a given callsign, scores all asteroids and persists
        the result to DB. On subsequent restarts, loads from DB instantly.
        """
        from api import agent as agent_api, universe as universe_api

        try:
            agent_data = await agent_api.get_my_agent(client)
            self.callsign = agent_data["symbol"]
            hq = agent_data.get("headquarters", "")
            parts = hq.split("-")
            self.system = f"{parts[0]}-{parts[1]}"
            self.command_ship = f"{self.callsign}-1"
            self.fleet_manager_ship = f"{self.callsign}-2"
            self.faction_hq_wp = hq
        except Exception as e:
            print(f"[auto_configure] agent query failed: {e} — keeping defaults")
            return

        db.init_db()

        # Load from DB if already configured for this callsign
        saved = db.load_agent_config(self.callsign)
        if saved.get("ASTEROID"):
            self.asteroid = saved["ASTEROID"]
            self.asteroid_base = saved.get("ASTEROID_BASE", self.asteroid_base or hq)
            self.shipyard_wp = saved.get("SHIPYARD_WP", self.shipyard_wp or hq)
            raw_wps = saved.get("SHIPYARD_WPS", "")
            self.shipyard_wps = raw_wps.split(",") if raw_wps else [self.shipyard_wp]
            self.faction_hq_wp = saved.get("FACTION_HQ_WP", hq)
            print(f"[config] Loaded from DB: ASTEROID={self.asteroid} BASE={self.asteroid_base}")
            return

        # First run — detect by scoring waypoints
        try:
            waypoints = await universe_api.get_waypoints(client, self.system)
        except Exception as e:
            print(f"[auto_configure] waypoint query failed: {e}")
            return

        coords: dict[str, tuple[int, int]] = {}
        traits_map: dict[str, set[str]] = {}
        shipyards: list[str] = []
        base_wps: list[str] = []
        market_wps: list[str] = []

        for wp in waypoints:
            sym = wp["symbol"]
            coords[sym] = (wp.get("x", 0), wp.get("y", 0))
            wp_traits = {t["symbol"] for t in wp.get("traits", [])}
            traits_map[sym] = wp_traits
            if wp["type"] == "ASTEROID_BASE":
                base_wps.append(sym)
            if "SHIPYARD" in wp_traits:
                shipyards.append(sym)
            if "MARKETPLACE" in wp_traits:
                market_wps.append(sym)

        base_candidates = base_wps if base_wps else market_wps

        best_score = -float("inf")
        best_asteroid = None
        best_base = None

        for wp in waypoints:
            if wp["type"] not in ASTEROID_TYPES:
                continue
            sym = wp["symbol"]
            traits = traits_map.get(sym, set())
            score = sum(ASTEROID_TRAIT_SCORES.get(t, 0) for t in traits)
            if score <= -9000:
                continue

            ax, ay = coords[sym]
            nearest_base, n_dist = None, float("inf")
            for bc in base_candidates:
                bx, by = coords.get(bc, (0, 0))
                d = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                if d < n_dist:
                    n_dist, nearest_base = d, bc

            if n_dist < 100:
                score += 20
            elif n_dist < 300:
                score += 10
            elif n_dist > 500:
                score -= 10

            if score > best_score:
                best_score = score
                best_asteroid = sym
                best_base = nearest_base

        if best_asteroid:
            self.asteroid = best_asteroid
            self.asteroid_base = best_base or hq
            print(f"[config] Auto-configured asteroid: {self.asteroid} (score={best_score:.0f}) base: {self.asteroid_base}")
        else:
            self.asteroid = self.asteroid or hq
            self.asteroid_base = self.asteroid_base or hq
            print(f"[config] No suitable asteroid found — using defaults")

        if shipyards:
            self.shipyard_wp = shipyards[0]
            self.shipyard_wps = shipyards
        else:
            self.shipyard_wp = self.shipyard_wp or hq
            self.shipyard_wps = [self.shipyard_wp]

        db.upsert_waypoints(waypoints)
        db.save_agent_config(self.callsign, {
            "ASTEROID":      self.asteroid,
            "ASTEROID_BASE": self.asteroid_base,
            "SHIPYARD_WP":   self.shipyard_wp,
            "SHIPYARD_WPS":  ",".join(self.shipyard_wps),
            "FACTION_HQ_WP": self.faction_hq_wp,
        })
        print(f"[config] Config saved to DB for {self.callsign}")
