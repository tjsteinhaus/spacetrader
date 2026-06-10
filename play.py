#!/usr/bin/env python3
"""
SpaceTraders Automation Script
Loops indefinitely: complete contracts → buy ships → repeat.
Run with: python3 play.py
"""
from __future__ import annotations
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

import agent as agent_api
import fleet as fleet_api
import contracts as contracts_api
import universe as universe_api
from client import SpaceTradersError
import db

load_dotenv()
console = Console(force_terminal=True)




# ── Market price cache (populated at runtime) ─────────────────────────────────
_market_cache: dict[str, dict[str, int]] = {}   # {wp: {good: sell_price}}
_market_cache_ts: dict[str, float] = {}          # {wp: unix_timestamp}
_known_markets: list[str] = []                   # grows after discover_markets()
_good_exporters: dict[str, list[str]] = {}       # {good: [waypoints that sell it]}
_good_buyers:    dict[str, list[str]] = {}       # {good: [waypoints that import/buy it]}
_buy_source_blacklist: dict[str, float] = {}     # {waypoint: expiry_timestamp} — temp skip on empty tradeGoods
_contract_retry_after: dict[str, float] = {}     # {contract_id: earliest_retry_time} — skip unworkable contracts

_fulfill_lock = threading.Lock()                  # prevents double-fulfillment
_manager_lock = threading.Lock()                  # serialises background fleet-management ops
_last_negotiation: float = 0.0                    # unix timestamp of last proactive contract negotiation

# ── Shared survey pool (surveyor ships write, miners read) ────────────────────
_shared_surveys: list[dict] = []
_surveys_lock   = threading.Lock()

# ── Hauler and explorer ship registries ───────────────────────────────────────
_hauler_symbols:   list[str] = []   # symbols of ships running hauler_loop
_explorer_symbols: list[str] = []   # symbols of ships running explorer_loop
_siphoner_symbols: list[str] = []   # symbols of ships running siphon_loop
_trader_symbols:   list[str] = []   # symbols of ships running trader_loop

# When working a direct-buy contract, only one ship does buy/deliver.
# All other miners get this mine-only contract (fake good = "__SELL_ONLY__")
# so they mine and sell ore for income without competing for the good.
_mine_only_contract: dict | None = None

# ── Config ────────────────────────────────────────────────────────────────────
SYSTEM         = "X1-GK27"          # auto-set by auto_configure()
COMMAND_SHIP   = "MASTERY-1"        # auto-set by auto_configure()
ASTEROID       = "X1-GK27-CD5A"    # auto-set by auto_configure()
ASTEROID_BASE  = "X1-GK27-H48"     # auto-set by auto_configure()
SHIPYARD_WP    = "X1-GK27-H48"     # auto-set by auto_configure()
SHIPYARD_WPS   = ["X1-GK27-H48", "X1-GK27-A2", "X1-GK27-C37"]  # auto-set by auto_configure()
_FACTION_HQ_WP = "X1-GK27-A1"     # auto-set by auto_configure() — used for contract negotiation
CREDIT_RESERVE = 50_000            # Minimum credits to keep in reserve
MIN_BUY_CREDITS = 100_000          # Fleet manager starts buying once we clear this threshold
AUTO_BUY_SHIPS  = True             # Fleet manager will buy ships when credits allow
CHEAP_BUY_THRESHOLD    = 200        # cr/unit — buy even mineable goods if market price is this low
DRY_EXTRACT_THRESHOLD  = 5          # consecutive zero-hit extractions before forcing buy mode
SELL_ROUTING_DIST_COST = 20         # cr per distance unit deducted from remote-market revenue (fuel + time proxy)
MIN_CONTRACT_PAYOUT    = 30_000     # Skip contracts with onFulfilled < this and try to negotiate a better one
FLEET_MANAGER_SHIP = "MASTERY-2"   # auto-set by auto_configure()
MIN_FUEL_CAPACITY  = 150           # Skip ships whose fuel tank can't cover the asteroid↔base route

# Goods extractable from asteroids — always mine these rather than purchase,
# unless the market price is at or below CHEAP_BUY_THRESHOLD (trivial cost).
MINEABLE_GOODS: frozenset[str] = frozenset({
    "ALUMINUM_ORE", "IRON_ORE", "COPPER_ORE", "SILVER_ORE", "GOLD_ORE",
    "PLATINUM_ORE", "URANITE_ORE", "MERITIUM_ORE",
    "SILICON_CRYSTALS", "QUARTZ_SAND", "PRECIOUS_STONES", "DIAMONDS",
    "AMMONIA_ICE", "ICE_WATER", "LIQUID_HYDROGEN", "LIQUID_NITROGEN",
    "HYDROCARBON",
})

# Quality score for each asteroid deposit trait — used when scoring & ranking asteroids.
_ASTEROID_TRAIT_SCORES: dict[str, int] = {
    "STRIPPED":                -9999,
    "PRECIOUS_METAL_DEPOSITS":    50,
    "RARE_METAL_DEPOSITS":        40,
    "COMMON_METAL_DEPOSITS":      20,
    "MINERAL_DEPOSITS":           10,
    "DEEP_CRATERS":               15,
    "HOLLOWED_INTERIOR":           5,
    "EXPLOSIVE_GASES":            -5,
    "UNSTABLE_COMPOSITION":       -5,
    "RADIOACTIVE":               -10,
    "DEBRIS_CLUSTER":             -5,
}

# Maps each mineable good to the asteroid deposit trait(s) that signal it's present.
# Used by choose_mining_target() to prioritise asteroids that actually yield the contract good.
GOOD_TO_DEPOSIT_TRAITS: dict[str, frozenset[str]] = {
    "IRON_ORE":         frozenset({"COMMON_METAL_DEPOSITS"}),
    "COPPER_ORE":       frozenset({"COMMON_METAL_DEPOSITS"}),
    "ALUMINUM_ORE":     frozenset({"COMMON_METAL_DEPOSITS"}),
    "SILVER_ORE":       frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "GOLD_ORE":         frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "PRECIOUS_STONES":  frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "DIAMONDS":         frozenset({"PRECIOUS_METAL_DEPOSITS", "RARE_METAL_DEPOSITS"}),
    "PLATINUM_ORE":     frozenset({"RARE_METAL_DEPOSITS"}),
    "URANITE_ORE":      frozenset({"RARE_METAL_DEPOSITS"}),
    "MERITIUM_ORE":     frozenset({"RARE_METAL_DEPOSITS", "PRECIOUS_METAL_DEPOSITS"}),
    "SILICON_CRYSTALS": frozenset({"MINERAL_DEPOSITS"}),
    "QUARTZ_SAND":      frozenset({"MINERAL_DEPOSITS"}),
    "AMMONIA_ICE":      frozenset({"MINERAL_DEPOSITS"}),
    "ICE_WATER":        frozenset({"MINERAL_DEPOSITS"}),
    "LIQUID_HYDROGEN":  frozenset({"EXPLOSIVE_GASES"}),
    "LIQUID_NITROGEN":  frozenset({"EXPLOSIVE_GASES"}),
    "HYDROCARBON":      frozenset({"EXPLOSIVE_GASES"}),
}

# Ship purchase priority for mining contracts (higher score = buy first)
# -1 means never buy; caps enforced in _bg_buy_and_launch
# Rule: miners first → surveyors → haulers only once we have 2+ miners
SHIP_SCORES = {
    "SHIP_ORE_HOUND":       100,  # Best miner: powerful mounts + large cargo
    "SHIP_MINING_DRONE":    95,   # Cheap early miners — priority until we have 4
    "SHIP_SURVEYOR":        85,   # Improves yields — buy after first extra miner
    "SHIP_LIGHT_HAULER":    70,   # Hauler/trader — useful once we have 2+ miners
    "SHIP_HEAVY_FREIGHTER": 65,   # Hauler alt — larger cargo if available
    "SHIP_COMMAND_FRIGATE": 50,   # Explorer (4+ miners) — has MODULE_JUMP_DRIVE_I
    "SHIP_SIPHON_DRONE":    60,   # Gas siphoner — passive income from gas giants
    "SHIP_GAS_DRONE":       55,   # Gas collector alt
    "SHIP_LIGHT_SHUTTLE":   -1,   # Never buy — tiny cargo, wrong role
    "SHIP_PROBE":           -1,   # Never buy
}

MIN_SELL_PRICE     = 30     # cr/unit — jettison anything below this instead of hauling to market
REPAIR_THRESHOLD   = 0.80   # Repair when any component drops below 80% condition
MARKET_CACHE_TTL   = 600    # Seconds to keep market price data fresh
MINING_MOUNT_TIERS = [      # Mining laser upgrades, weakest → strongest
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]

# ── Runtime settings helpers (read from DB so dashboard can control them) ────

def _auto_buy_enabled() -> bool:
    """Check DB for auto_buy_ships setting; falls back to AUTO_BUY_SHIPS constant."""
    val = db.get_bot_setting("auto_buy_ships")
    if val:
        return val.lower() == "true"
    return AUTO_BUY_SHIPS


def _get_ship_targets() -> list[dict]:
    """Return ship purchase targets from DB, e.g. [{"type": "SHIP_SURVEYOR", "max": 1}, ...].
    Empty list = no DB config yet, callers should fall back to hardcoded caps.
    """
    raw = db.get_bot_setting("ship_buy_list")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim]{ts}[/dim] {msg}")


def wait_for_ship(ship_symbol: str, poll: int = 5) -> None:
    """Block until ship is no longer IN_TRANSIT.

    Uses adaptive polling: short transits poll every `poll` seconds;
    long drifts (>120s remaining) poll every 30s and log once per minute
    to avoid flooding the console.
    """
    last_log = 0.0
    while True:
        ship = fleet_api.get_ship(ship_symbol)
        nav = ship["nav"]
        if nav["status"] != "IN_TRANSIT":
            log(f"[green]✓[/green] {ship_symbol} arrived at [bold]{nav['waypointSymbol']}[/bold]")
            return
        arrival = nav["route"].get("arrival", "")
        secs = 0
        if arrival:
            try:
                dt = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                secs = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
        now = time.monotonic()
        log_every = 60 if secs > 120 else poll
        if now - last_log >= log_every:
            if secs >= 3600:
                eta_str = f"{secs // 3600}h {(secs % 3600) // 60}m"
            elif secs >= 60:
                eta_str = f"{secs // 60}m {secs % 60}s"
            else:
                eta_str = f"{secs}s"
            log(f"[yellow]⏳ {ship_symbol} in transit (~{eta_str})...[/yellow]")
            last_log = now
        time.sleep(30 if secs > 120 else poll)


def wait_cooldown(ship_symbol: str) -> None:
    """Block until ship cooldown expires."""
    while True:
        try:
            cd = fleet_api.get_ship_cooldown(ship_symbol)
            remaining = cd.get("remainingSeconds", 0) if isinstance(cd, dict) else 0
            if remaining <= 0:
                return
            log(f"[yellow]🌡 Cooldown: {remaining}s[/yellow]")
            time.sleep(min(remaining, 10))
        except SpaceTradersError as e:
            if e.code == 204 or "no cooldown" in str(e).lower():
                return
            raise


def ensure_orbit(ship_symbol: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "DOCKED":
        fleet_api.orbit(ship_symbol)
        log(f"[cyan]↑ {ship_symbol} orbiting[/cyan]")
    elif ship["nav"]["status"] == "IN_TRANSIT":
        wait_for_ship(ship_symbol)


def ensure_docked(ship_symbol: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "IN_ORBIT":
        fleet_api.dock(ship_symbol)
        log(f"[cyan]⚓ {ship_symbol} docked[/cyan]")
    elif ship["nav"]["status"] == "IN_TRANSIT":
        wait_for_ship(ship_symbol)
        fleet_api.dock(ship_symbol)
        log(f"[cyan]⚓ {ship_symbol} docked[/cyan]")


def navigate_to(ship_symbol: str, destination: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    # If already in transit toward the destination (e.g., drifting there), just wait
    if ship["nav"]["status"] == "IN_TRANSIT":
        route_dest = ship["nav"].get("route", {}).get("destination", {}).get("symbol", "")
        if route_dest == destination:
            log(f"[dim]{ship_symbol}: already en route to {destination} — waiting...[/dim]")
            wait_for_ship(ship_symbol)
            _fm = fleet_api.get_ship(ship_symbol)["nav"].get("flightMode", "CRUISE")
            if _fm != "CRUISE":
                fleet_api.patch_nav(ship_symbol, "CRUISE")
                log(f"[dim]{ship_symbol}: restored CRUISE mode after in-transit wait[/dim]")
            return
    if ship["nav"]["waypointSymbol"] == destination:
        log(f"[dim]{ship_symbol} already at {destination}[/dim]")
        return

    # ── Inter-system travel ───────────────────────────────────────────────────
    dest_system = _system_of(destination)
    curr_system = _system_of(ship["nav"]["waypointSymbol"])
    if dest_system != curr_system:
        if has_jump_drive(ship):
            # Navigate to local jump gate, then jump to destination system
            gate_wp = _find_jump_gate(curr_system)
            if not gate_wp:
                raise SpaceTradersError(0, f"No jump gate found in {curr_system}")
            if ship["nav"]["waypointSymbol"] != gate_wp:
                navigate_to(ship_symbol, gate_wp)  # intra-system leg to gate
            ensure_orbit(ship_symbol)
            log(f"[blue]⚡ {ship_symbol}: jumping → {dest_system}[/blue]")
            fleet_api.jump(ship_symbol, destination)
            wait_for_ship(ship_symbol)
            # If the jump landed us at a gate rather than the exact destination, navigate the rest
            _after = fleet_api.get_ship(ship_symbol)["nav"]["waypointSymbol"]
            if _after != destination:
                navigate_to(ship_symbol, destination)
        elif has_warp_drive(ship):
            ensure_orbit(ship_symbol)
            log(f"[blue]🌀 {ship_symbol}: warping → {destination}[/blue]")
            fleet_api.warp(ship_symbol, destination)
            wait_for_ship(ship_symbol)
        else:
            raise SpaceTradersError(
                0,
                f"{ship_symbol} has no jump/warp drive — cannot travel from {curr_system} to {dest_system}",
            )
        return

    # ── Intra-system travel ───────────────────────────────────────────────────
    ensure_orbit(ship_symbol)
    # Always navigate in CRUISE mode; reset if ship was left in DRIFT
    if ship["nav"].get("flightMode", "CRUISE") != "CRUISE":
        fleet_api.patch_nav(ship_symbol, "CRUISE")
        log(f"[dim]{ship_symbol}: reset to CRUISE mode[/dim]")
    log(f"[blue]🚀 Navigating {ship_symbol} → {destination}[/blue]")
    try:
        fleet_api.navigate(ship_symbol, destination)
    except SpaceTradersError as e:
        if e.code == 4214:  # ship already in transit — wait and retry once
            log(f"[dim]{ship_symbol}: already in transit, waiting to arrive before redirecting...[/dim]")
            wait_for_ship(ship_symbol)
            navigate_to(ship_symbol, destination)
            return
        if e.code == 4203:  # insufficient fuel — try local refuel first, then DRIFT
            log(f"[yellow]⚠ {ship_symbol}: insufficient fuel for {destination} — attempting local refuel[/yellow]")
            try:
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)
                fleet_api.orbit(ship_symbol)
                fleet_api.navigate(ship_symbol, destination)
                wait_for_ship(ship_symbol)
                return
            except SpaceTradersError:
                pass
            log(f"[red]⚠ {ship_symbol}: no fuel available locally, emergency DRIFT to {destination}[/red]")
            fleet_api.patch_nav(ship_symbol, "DRIFT")
            try:
                fleet_api.navigate(ship_symbol, destination)
            except SpaceTradersError as drift_e:
                if drift_e.code == 4214:
                    log(f"[dim]{ship_symbol}: already drifting to {destination}[/dim]")
                else:
                    raise
            wait_for_ship(ship_symbol)
            fleet_api.patch_nav(ship_symbol, "CRUISE")
            log(f"[dim]{ship_symbol}: restored CRUISE flight mode[/dim]")
            return
        raise
    wait_for_ship(ship_symbol)


def navigate_with_refuel(ship_symbol: str, destination: str) -> None:
    """
    Navigate to destination, making intermediate refuel stops when the ship
    doesn't have enough fuel to reach it in one CRUISE hop.

    At each step, finds the best reachable market (within fuel capacity) that
    makes progress toward the destination. Falls back to a direct navigate_to
    (which will drift if needed) if no intermediate market can be found.
    """
    for _hop in range(10):
        ship = fleet_api.get_ship(ship_symbol)
        cur_wp = ship["nav"]["waypointSymbol"]
        if cur_wp == destination:
            return

        fuel = ship["fuel"]
        cur_fuel = fuel.get("current", 0)
        fuel_cap = max(fuel.get("capacity", 1), 1)

        cx, cy = _get_coords(cur_wp)
        dx, dy = _get_coords(destination)
        dist_to_dest = ((dx - cx) ** 2 + (dy - cy) ** 2) ** 0.5

        if cur_fuel > dist_to_dest:
            navigate_to(ship_symbol, destination)
            return

        # Find the best reachable market: within fuel_cap of current position
        # and closer to the destination than we currently are.
        # Only consider markets that actually sell FUEL — routing through a
        # fuel-less market (e.g. J58) strands ships and forces a long drift.
        fuel_markets = _good_exporters.get("FUEL", [])
        markets = fuel_markets if fuel_markets else (_known_markets or [ASTEROID_BASE])
        best_wp: str | None = None
        best_remaining = dist_to_dest  # must beat our current distance

        for wp in markets:
            if wp == cur_wp:
                continue
            wx, wy = _get_coords(wp)
            hop_dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
            if hop_dist > cur_fuel:
                continue  # can't reach this hop with current fuel
            remaining = ((dx - wx) ** 2 + (dy - wy) ** 2) ** 0.5
            if remaining > fuel_cap:
                continue  # even at full tank from this hop, destination is out of range — dead end
            if remaining < best_remaining:
                best_remaining = remaining
                best_wp = wp

        if best_wp is None:
            log(f"[yellow]{ship_symbol}: no reachable intermediate market en route to {destination} — will drift[/yellow]")
            navigate_to(ship_symbol, destination)
            return

        log(f"[dim]{ship_symbol}: refuel hop → {best_wp} (en route to {destination})[/dim]")
        navigate_to(ship_symbol, best_wp)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

    # Reached hop limit — attempt direct (may drift)
    navigate_to(ship_symbol, destination)


def good_in_cargo(ship_symbol: str, good: str) -> int:
    ship = fleet_api.get_ship(ship_symbol)
    for item in ship["cargo"].get("inventory", []):
        if item["symbol"] == good:
            return item["units"]
    return 0


def cargo_space(ship_symbol: str) -> int:
    ship = fleet_api.get_ship(ship_symbol)
    c = ship["cargo"]
    return c["capacity"] - c["units"]


def sell_junk(ship_symbol: str, keep_good: str | None = None) -> None:
    """
    Sell everything except keep_good.
    Items with best sell price < MIN_SELL_PRICE are jettisoned immediately at the asteroid.
    Routes to the best-paying market when it pays >20% more than ASTEROID_BASE.
    """
    ship = fleet_api.get_ship(ship_symbol)
    inventory = [i for i in ship["cargo"].get("inventory", []) if i["symbol"] != keep_good]
    if not inventory:
        return

    # Jettison items too cheap to be worth hauling to market
    worth_selling = []
    for item in inventory:
        sym = item["symbol"]
        best_price = max(
            (get_market_prices(wp).get(sym, 0) for wp in (_known_markets or [ASTEROID_BASE])),
            default=0,
        )
        # Don't jettison if a known importer exists — route there to sell even without a cached price
        if best_price < MIN_SELL_PRICE and any(
            wp in (_known_markets or []) for wp in _good_buyers.get(sym, [])
        ):
            best_price = MIN_SELL_PRICE
        if best_price < MIN_SELL_PRICE:
            try:
                fleet_api.jettison(ship_symbol, sym, item["units"])
                log(f"[dim]Jettisoned {item['units']}x {sym} ({best_price} cr/u < threshold)[/dim]")
            except SpaceTradersError:
                worth_selling.append(item)  # keep if jettison fails
        else:
            worth_selling.append(item)

    if not worth_selling:
        return

    target_wp = best_sell_market_for_cargo(worth_selling)
    current_wp = ship["nav"]["waypointSymbol"]
    if current_wp != target_wp or ship["nav"]["status"] != "DOCKED":
        navigate_with_refuel(ship_symbol, target_wp)
        ensure_docked(ship_symbol)

    # Re-fetch cargo after possible travel
    ship = fleet_api.get_ship(ship_symbol)
    _sell_wp = ship["nav"]["waypointSymbol"]
    for item in ship["cargo"].get("inventory", []):
        if keep_good and item["symbol"] == keep_good:
            continue
        try:
            result = fleet_api.sell_cargo(ship_symbol, item["symbol"], item["units"])
            tx = result.get("transaction", {})
            ppu = tx.get("pricePerUnit", 0)
            log(f"[green]💰 Sold {tx.get('units')}x {tx.get('tradeSymbol')} @ {ppu:,}/u = {tx.get('totalPrice', 0):,} cr[/green]")
            db.log_transaction(_sell_wp, ship_symbol, item["symbol"], "SELL",
                               tx.get("units", 0), ppu, tx.get("totalPrice", 0))
        except SpaceTradersError:
            try:
                fleet_api.jettison(ship_symbol, item["symbol"], item["units"])
                log(f"[dim]Jettisoned {item['units']}x {item['symbol']}[/dim]")
            except SpaceTradersError:
                pass
    # Refuel at the sell market before returning — no-op if market has no fuel
    refuel_if_needed(ship_symbol, threshold=100_000)


def refuel_if_needed(ship_symbol: str, threshold: int = 200) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    fuel = ship["fuel"]
    if fuel["capacity"] > 0 and fuel["current"] < min(threshold, fuel["capacity"]):
        try:
            result = fleet_api.refuel(ship_symbol)
            f = result.get("fuel", {})
            log(f"[green]⛽ Refueled to {f.get('current')}/{f.get('capacity')}[/green]")
        except SpaceTradersError as e:
            log(f"[yellow]Could not refuel: {e}[/yellow]")


# Coordinate cache to avoid repeated API calls
_wp_coords: dict[str, tuple[int, int]] = {}
_jump_gate_cache: dict[str, str] = {}  # system -> jump gate waypoint symbol


def _system_of(waypoint: str) -> str:
    """Extract the system symbol from a waypoint symbol (e.g. 'X1-HU91-B8' -> 'X1-HU91')."""
    parts = waypoint.split("-")
    return f"{parts[0]}-{parts[1]}"


def _get_coords(waypoint: str) -> tuple[int, int]:
    """Return (x, y) coordinates for a waypoint, cached."""
    if waypoint not in _wp_coords:
        try:
            data = universe_api.get_waypoint(_system_of(waypoint), waypoint)
            _wp_coords[waypoint] = (data.get("x", 0), data.get("y", 0))
        except SpaceTradersError:
            _wp_coords[waypoint] = (0, 0)
    return _wp_coords[waypoint]


def waypoint_distance(wp_a: str, wp_b: str) -> float:
    """Return Euclidean distance between two waypoints."""
    ax, ay = _get_coords(wp_a)
    bx, by = _get_coords(wp_b)
    return ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5


def _find_jump_gate(system: str) -> str | None:
    """Return the waypoint symbol of the jump gate in system, or None (cached)."""
    if system not in _jump_gate_cache:
        gates = universe_api.get_waypoints(system, "JUMP_GATE")
        if gates:
            _jump_gate_cache[system] = gates[0]["symbol"]
        else:
            return None
    return _jump_gate_cache.get(system)


def nearest_refuel_point(from_wp: str) -> str:
    """Return the closest fuel-selling market waypoint to from_wp."""
    fuel_markets = _good_exporters.get("FUEL", [])
    candidates = fuel_markets if fuel_markets else (_known_markets or [ASTEROID_BASE])
    fx, fy = _get_coords(from_wp)
    best_wp, best_dist = ASTEROID_BASE, float("inf")
    for wp in candidates:
        wx, wy = _get_coords(wp)
        dist = ((wx - fx) ** 2 + (wy - fy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist, best_wp = dist, wp
    if best_wp != ASTEROID_BASE:
        log(f"[dim]Nearest fuel market to {from_wp}: {best_wp} (dist={best_dist:.0f})[/dim]")
    return best_wp


# ── Asteroid scoring & mining target selection ────────────────────────────────

# Populated lazily on first contract; read-only after that (safe under threading).
_scored_asteroids: list[dict] = []                   # [{symbol, x, y, traits, trait_score, nearest_base, base_dist}]
_asteroid_traits:  dict[str, frozenset[str]] = {}    # {symbol -> frozenset of trait symbols}


def _populate_asteroid_cache() -> None:
    """Build _scored_asteroids and _asteroid_traits from DB (or API fallback).
    Idempotent — no-op if already populated.
    """
    global _scored_asteroids, _asteroid_traits
    if _scored_asteroids:
        return

    _ASTEROID_TYPES_LOCAL = {"ASTEROID", "ASTEROID_FIELD", "ENGINEERED_ASTEROID"}

    waypoints = db.get_all_waypoints(SYSTEM)
    if not waypoints:
        try:
            waypoints = universe_api.get_waypoints(SYSTEM)
        except SpaceTradersError as e:
            log(f"[yellow]_populate_asteroid_cache: waypoint fetch failed ({e})[/yellow]")
            return

    coords:          dict[str, tuple[int, int]] = {}
    traits_map:      dict[str, frozenset[str]]  = {}
    base_candidates: list[str]                  = []
    market_wps:      list[str]                  = []

    for wp in waypoints:
        sym = wp["symbol"]
        coords[sym] = (wp.get("x", 0), wp.get("y", 0))
        wp_traits = frozenset(t["symbol"] for t in wp.get("traits", []))
        traits_map[sym] = wp_traits
        if wp["type"] == "ASTEROID_BASE":
            base_candidates.append(sym)
        if "MARKETPLACE" in wp_traits:
            market_wps.append(sym)

    if not base_candidates:
        base_candidates = market_wps or [ASTEROID_BASE]

    results: list[dict] = []
    for wp in waypoints:
        if wp["type"] not in _ASTEROID_TYPES_LOCAL:
            continue
        sym    = wp["symbol"]
        traits = traits_map.get(sym, frozenset())
        score  = sum(_ASTEROID_TRAIT_SCORES.get(t, 0) for t in traits)
        if score <= -9000:
            continue  # STRIPPED

        ax, ay               = coords[sym]
        nearest_base, n_dist = ASTEROID_BASE, float("inf")
        for bc in base_candidates:
            bx, by = coords.get(bc, (0, 0))
            d = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if d < n_dist:
                n_dist, nearest_base = d, bc

        results.append({
            "symbol":       sym,
            "x":            ax,
            "y":            ay,
            "traits":       traits,
            "trait_score":  score,
            "nearest_base": nearest_base,
            "base_dist":    n_dist,
        })
        _wp_coords[sym] = (ax, ay)  # seed coordinate cache

    _scored_asteroids[:] = results
    _asteroid_traits.update({r["symbol"]: r["traits"] for r in results})
    log(f"[dim]Asteroid cache: {len(_scored_asteroids)} candidates loaded[/dim]")


def score_asteroid_for_miner(
    ast: dict,
    contract_good: str,
    ship_x: int,
    ship_y: int,
    ship_fuel_cap: int,
    delivery_wp: str,
) -> float:
    """Score a single asteroid for a specific miner + contract.

    Factors (all additive):
    - Base trait quality   (_ASTEROID_TRAIT_SCORES sum)
    - +80  resource match: asteroid has the deposit trait for contract_good
    - Fuel efficiency:     round-trip to nearest base vs ship fuel capacity
    - Distance from ship:  initial travel cost to reach the asteroid
    - Delivery proximity:  minor bonus/penalty based on asteroid → delivery distance
    """
    score = float(ast["trait_score"])

    # Resource match — highest-weight factor
    deposit_traits = GOOD_TO_DEPOSIT_TRAITS.get(contract_good, frozenset())
    if deposit_traits & ast["traits"]:
        score += 80.0

    # Fuel efficiency: round-trip asteroid ↔ nearest refuel base
    round_trip = ast["base_dist"] * 2.0
    if ship_fuel_cap > 0:
        ratio = round_trip / ship_fuel_cap
        if ratio <= 1.0:
            score += 20.0   # fits in one tank — very efficient
        elif ratio <= 2.0:
            score -= 10.0   # one refuel stop each way
        elif ratio <= 4.0:
            score -= 35.0   # two+ refuel stops — costly for small ships
        else:
            score -= 80.0   # extremely far; major inefficiency

    # Distance from ship's current position (initial travel cost)
    dist_from_ship = ((ast["x"] - ship_x) ** 2 + (ast["y"] - ship_y) ** 2) ** 0.5
    if dist_from_ship < 50:
        score += 15.0
    elif dist_from_ship > 200:
        score -= 10.0

    # Delivery proximity (minor — asteroid closer to delivery WP = shorter trips)
    dx, dy = _get_coords(delivery_wp)
    dist_to_delivery = ((ast["x"] - dx) ** 2 + (ast["y"] - dy) ** 2) ** 0.5
    if dist_to_delivery < 100:
        score += 10.0
    elif dist_to_delivery > 500:
        score -= 10.0

    return score


def choose_mining_target(ship_symbol: str, contract: dict) -> str:
    """Return the best asteroid waypoint for this miner + contract.

    Scores all known asteroids by resource trait match, fuel round-trip efficiency,
    current ship position, and delivery proximity.  Logs the top 3 candidates so
    the reasoning is visible in the console.

    Called at miner thread startup — naturally re-evaluated on every new contract.
    Falls back to the global ASTEROID if the scored cache is empty.
    """
    d           = contract["terms"]["deliver"][0]
    good        = d["tradeSymbol"]
    delivery_wp = d["destinationSymbol"]

    if good not in MINEABLE_GOODS:
        # For sell-only miners: pick the closest asteroid the ship can safely
        # round-trip to on a single tank.  Small ships (e.g. 80-fuel EXCAVATOR)
        # can't reach B46 (dist 44) and return — find the nearest asteroid where
        # round_trip_cost = dist * 2 * 1.15 <= fuel_cap.
        try:
            fuel_cap = fleet_api.get_ship(ship_symbol)["fuel"].get("capacity", 0)
        except SpaceTradersError:
            return ASTEROID
        if fuel_cap > 0:
            _populate_asteroid_cache()
            _best_wp   = ASTEROID
            _best_dist = float("inf")
            for _ast in _scored_asteroids:
                _d = _ast.get("base_dist", float("inf"))
                if fuel_cap >= _d * 2 * 1.15 and _d < _best_dist:
                    _best_dist = _d
                    _best_wp   = _ast["symbol"]
            if _best_wp != ASTEROID:
                log(f"[cyan]{ship_symbol}: sell-only → {_best_wp} (dist {_best_dist:.0f}, fuel cap {fuel_cap})[/cyan]")
            return _best_wp
        return ASTEROID  # no fuel tank (e.g. satellite) — use default

    _populate_asteroid_cache()

    if not _scored_asteroids:
        log(f"[yellow]{ship_symbol}: asteroid cache empty — using default {ASTEROID}[/yellow]")
        return ASTEROID

    try:
        ship     = fleet_api.get_ship(ship_symbol)
        fuel     = ship["fuel"]
        ship_x, ship_y = _get_coords(ship["nav"]["waypointSymbol"])
        fuel_cap = fuel.get("capacity", 0)
    except SpaceTradersError as e:
        log(f"[yellow]{ship_symbol}: choose_mining_target ship fetch failed ({e}) — using default[/yellow]")
        return ASTEROID

    scored = sorted(
        (
            (score_asteroid_for_miner(ast, good, ship_x, ship_y, fuel_cap, delivery_wp), ast)
            for ast in _scored_asteroids
        ),
        key=lambda t: t[0],
        reverse=True,
    )

    deposit_traits = GOOD_TO_DEPOSIT_TRAITS.get(good, frozenset())
    log(f"[cyan]{ship_symbol}: mining target for [bold]{good}[/bold] (fuel cap={fuel_cap}) — top candidates:[/cyan]")
    for rank, (sc, ast) in enumerate(scored[:3], 1):
        match_str = "[green]✓ match[/green]" if deposit_traits & ast["traits"] else "no match"
        log(f"[dim]  #{rank} {ast['symbol']}  score={sc:.0f}  base_dist={ast['base_dist']:.0f}  {match_str}[/dim]")

    best = scored[0][1]["symbol"]
    log(f"[cyan]{ship_symbol}: → mining at [bold]{best}[/bold][/cyan]")
    return best


# ── Auto-configuration ────────────────────────────────────────────────────────

def auto_configure() -> None:
    """Detect SYSTEM, COMMAND_SHIP, ASTEROID, ASTEROID_BASE, SHIPYARD_WP from the API.

    On first run for a given agent callsign: scores all asteroids (avoids STRIPPED,
    prefers DEEP_CRATERS / precious/rare metal deposits near an ASTEROID_BASE market)
    and persists the result to the DB.

    On subsequent restarts with the same callsign: loads saved values from DB instantly
    (no extra API calls).  Delete the agent_config rows or use a new callsign to force
    re-detection.
    """
    global SYSTEM, COMMAND_SHIP, FLEET_MANAGER_SHIP
    global ASTEROID, ASTEROID_BASE, SHIPYARD_WP, SHIPYARD_WPS, _FACTION_HQ_WP

    try:
        agent    = agent_api.get_my_agent()
        callsign = agent["symbol"]
        hq       = agent["headquarters"]           # e.g. "X1-GK27-H48"
        parts    = hq.split("-")
        SYSTEM             = f"{parts[0]}-{parts[1]}"
        COMMAND_SHIP       = f"{callsign}-1"
        FLEET_MANAGER_SHIP = f"{callsign}-2"
        log(f"[cyan]Agent: {callsign} | HQ: {hq} | System: {SYSTEM}[/cyan]")
    except SpaceTradersError as e:
        log(f"[yellow]auto_configure: agent query failed ({e}) — keeping defaults[/yellow]")
        return

    # ── Load from DB if this callsign was already configured ──────────────────
    db.init_db()   # ensure schema exists before reading config
    saved = db.load_agent_config(callsign)
    if saved.get("ASTEROID"):
        ASTEROID       = saved["ASTEROID"]
        ASTEROID_BASE  = saved.get("ASTEROID_BASE", ASTEROID_BASE)
        SHIPYARD_WP    = saved.get("SHIPYARD_WP",   SHIPYARD_WP)
        SHIPYARD_WPS   = saved.get("SHIPYARD_WPS",  ",".join(SHIPYARD_WPS)).split(",")
        _FACTION_HQ_WP = saved.get("FACTION_HQ_WP", hq)
        log(f"[cyan]Loaded config from DB: ASTEROID={ASTEROID} | BASE={ASTEROID_BASE}[/cyan]")
        log(f"[cyan]Shipyards: {SHIPYARD_WPS} | HQ: {_FACTION_HQ_WP}[/cyan]")
        return

    # ── First run for this callsign — detect and score ────────────────────────
    try:
        waypoints = universe_api.get_waypoints(SYSTEM)
    except SpaceTradersError as e:
        log(f"[yellow]auto_configure: waypoint query failed ({e}) — keeping defaults[/yellow]")
        return

    ASTEROID_TYPES = {"ASTEROID", "ASTEROID_FIELD", "ENGINEERED_ASTEROID"}
    TRAIT_SCORES: dict[str, int] = {
        "STRIPPED":               -9999,
        "PRECIOUS_METAL_DEPOSITS":   50,
        "RARE_METAL_DEPOSITS":       40,
        "COMMON_METAL_DEPOSITS":     20,
        "MINERAL_DEPOSITS":          10,
        "DEEP_CRATERS":              15,
        "HOLLOWED_INTERIOR":          5,
        "EXPLOSIVE_GASES":           -5,
        "UNSTABLE_COMPOSITION":      -5,
        "RADIOACTIVE":              -10,
        "DEBRIS_CLUSTER":            -5,
    }

    coords:      dict[str, tuple[int, int]] = {}
    traits_map:  dict[str, set[str]]        = {}
    shipyards:   list[str]                  = []
    base_wps:    list[str]                  = []   # ASTEROID_BASE type — purpose-built support
    market_wps:  list[str]                  = []   # any marketplace (fallback base)

    for wp in waypoints:
        sym          = wp["symbol"]
        coords[sym]  = (wp["x"], wp["y"])
        wp_traits    = {t["symbol"] for t in wp.get("traits", [])}
        traits_map[sym] = wp_traits
        if wp["type"] == "ASTEROID_BASE":
            base_wps.append(sym)
        if "SHIPYARD" in wp_traits:
            shipyards.append(sym)
        if "MARKETPLACE" in wp_traits:
            market_wps.append(sym)

    # Prefer ASTEROID_BASE type as the mining support station; fall back to any market
    base_candidates = base_wps if base_wps else market_wps

    best_score    = -float("inf")
    best_asteroid = None
    best_base     = None

    for wp in waypoints:
        if wp["type"] not in ASTEROID_TYPES:
            continue
        sym    = wp["symbol"]
        traits = traits_map.get(sym, set())
        score  = sum(TRAIT_SCORES.get(t, 0) for t in traits)
        if score <= -9000:          # STRIPPED — hard skip
            continue

        ax, ay               = coords[sym]
        nearest_base, n_dist = None, float("inf")
        for bc in base_candidates:
            bx, by = coords[bc]
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
            best_score    = score
            best_asteroid = sym
            best_base     = nearest_base

    if best_asteroid:
        ASTEROID      = best_asteroid
        ASTEROID_BASE = best_base or hq
        log(f"[cyan]Auto-configured asteroid: {ASTEROID} (score={best_score:.0f}) | base: {ASTEROID_BASE}[/cyan]")
    else:
        log(f"[yellow]No suitable asteroid found — keeping defaults ({ASTEROID})[/yellow]")

    if shipyards:
        SHIPYARD_WP  = shipyards[0]
        SHIPYARD_WPS = shipyards
        log(f"[cyan]Shipyards: {SHIPYARD_WPS}[/cyan]")

    _FACTION_HQ_WP = hq
    log(f"[cyan]Faction HQ waypoint: {_FACTION_HQ_WP}[/cyan]")

    # ── Persist so future restarts skip re-detection ──────────────────────────
    db.save_agent_config(callsign, {
        "ASTEROID":       ASTEROID,
        "ASTEROID_BASE":  ASTEROID_BASE,
        "SHIPYARD_WP":    SHIPYARD_WP,
        "SHIPYARD_WPS":   ",".join(SHIPYARD_WPS),
        "FACTION_HQ_WP":  _FACTION_HQ_WP,
    })
    log(f"[dim]Config saved to DB for {callsign}[/dim]")


# ── Market intelligence ───────────────────────────────────────────────────────

def discover_markets() -> list[str]:
    """Scan all waypoints in the system for MARKETPLACE trait. Updates _known_markets."""
    global _known_markets
    log("[dim]Scanning system for markets...[/dim]")
    try:
        waypoints = universe_api.get_waypoints(SYSTEM)
        # Pre-populate coordinate cache so _get_coords never falls back to (0,0)
        for wp in waypoints:
            _wp_coords[wp["symbol"]] = (wp["x"], wp["y"])

        found = [
            wp["symbol"] for wp in waypoints
            if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
        ]
        if found:
            _known_markets = found
            log(f"[dim]Found {len(found)} market(s): {', '.join(found)}[/dim]")
            db.upsert_waypoints(waypoints)
        elif not _known_markets:
            _known_markets = [ASTEROID_BASE]
    except SpaceTradersError as e:
        log(f"[dim]Market scan failed: {e} — using defaults[/dim]")
        if not _known_markets:
            _known_markets = [ASTEROID_BASE]
    return _known_markets


def scan_good_sources() -> None:
    """Scan all known markets for exports/exchange/imports to populate _good_exporters and _good_buyers.
    Uses the public market endpoint (no ship required) to get imports/exports lists."""
    global _good_exporters, _good_buyers
    _good_exporters = {}
    _good_buyers = {}
    for wp in (_known_markets or [ASTEROID_BASE]):
        try:
            data = universe_api.get_market(SYSTEM, wp)
            db.upsert_market_listings(wp, data)
            for category in ("exports", "exchange"):
                for g in data.get(category, []):
                    sym = g.get("symbol", "")
                    if sym:
                        _good_exporters.setdefault(sym, []).append(wp)
            for g in data.get("imports", []):
                sym = g.get("symbol", "")
                if sym:
                    _good_buyers.setdefault(sym, []).append(wp)
            # Exchange markets buy AND sell — include them as buyers too
            for g in data.get("exchange", []):
                sym = g.get("symbol", "")
                if sym:
                    _good_buyers.setdefault(sym, []).append(wp)
            # Seed the market-price timestamp so the first sell_junk doesn't re-fetch all 29
            # markets immediately after startup. Prices (tradeGoods) require a ship on site.
            if wp not in _market_cache_ts:
                _market_cache_ts[wp] = time.time()
        except SpaceTradersError:
            pass
        time.sleep(0.4)  # throttle to avoid 429 bursts
    if _good_exporters:
        log(f"[dim]Good sources: {len(_good_exporters)} goods indexed, {len(_good_buyers)} buyable goods across {len(_known_markets)} markets[/dim]")


def get_market_prices(waypoint: str) -> dict[str, int]:
    """Return {trade_symbol: sell_price} for a waypoint. Results are cached."""
    now = time.time()
    if _market_cache_ts.get(waypoint, 0) + MARKET_CACHE_TTL > now:
        # Return cached data (may be {} if visited without a ship — still a valid cache hit)
        return _market_cache.get(waypoint, {})
    try:
        data = universe_api.get_market(SYSTEM, waypoint)
        prices = {g["symbol"]: g["sellPrice"] for g in data.get("tradeGoods", [])}
        # Also store buy prices under a buy_ prefix for purchasing decisions
        buy = {f"_buy_{g['symbol']}": g["purchasePrice"] for g in data.get("tradeGoods", [])}
        # Only overwrite cache if we actually got price data (tradeGoods requires ship present).
        # Preserving last-known prices avoids zeroing out H51/H53 when called without a ship.
        if prices or buy:
            _market_cache[waypoint] = {**prices, **buy}
            db.upsert_market_prices(waypoint, data.get("tradeGoods", []))
        _market_cache_ts[waypoint] = now
        return _market_cache.get(waypoint, {})
    except SpaceTradersError:
        return {k: v for k, v in _market_cache.get(waypoint, {}).items() if not k.startswith("_buy_")}


def best_sell_waypoint(good: str) -> tuple[str, int]:
    """Return (waypoint, sell_price) for the market paying the most for `good`.

    TODO (item 5): Adjust for distance — net revenue = sell_price * units - round_trip_distance
    * SELL_ROUTING_DIST_COST.  Currently pure price comparison; a market 400 units away
    paying 50 cr/unit more than base may still be worse after travel cost.
    """
    best_wp, best_price = ASTEROID_BASE, 0
    for wp in (_known_markets or [ASTEROID_BASE]):
        price = get_market_prices(wp).get(good, 0)
        if price > best_price:
            best_price, best_wp = price, wp
    return best_wp, best_price


def best_buy_waypoint(good: str) -> str:
    """Return the waypoint that exports/exchanges `good` (i.e. sells it to us).
    Prefers markets with a known price; falls back to any exporter in _good_exporters.
    Skips waypoints in _buy_source_blacklist until their cooldown expires.

    TODO (item 5): Weight by net cost = buy_price + round_trip_distance * SELL_ROUTING_DIST_COST.
    Cheapest source isn't always best when a slightly pricier nearby market saves significant travel.
    """
    now = time.time()
    available = [wp for wp in _good_exporters.get(good, [])
                 if _buy_source_blacklist.get(wp, 0) <= now]
    # First pass: prefer markets with a live price already in cache
    best_wp, best_price = "", 0
    for wp in available:
        price = _market_cache.get(wp, {}).get(f"_buy_{good}", 0)
        if price > 0 and (best_price == 0 or price < best_price):
            best_price, best_wp = price, wp
    if best_wp:
        return best_wp
    # Fallback: return any non-blacklisted exporter (price unknown, will be revealed on arrival)
    return available[0] if available else ""


def best_sell_market_for_cargo(inventory: list[dict]) -> str:
    """
    Given a list of cargo items, return the waypoint with the highest aggregate
    sell revenue. Prefers the base cluster (markets co-located with ASTEROID_BASE,
    e.g. H51/H52/H53) so we don't waste travel time for marginal gains.

    Only routes to a remote market if it offers both a >20% relative premium AND
    at least 500 cr absolute gain over the best cluster market.
    """
    # Narrow candidates to markets that are plausible buyers for our goods.
    # Using all 29 markets is wasteful when only 2-3 actually buy ores.
    our_syms = {item["symbol"] for item in inventory}
    candidate_markets: set[str] = {ASTEROID_BASE}
    for sym in our_syms:
        candidate_markets.update(_good_buyers.get(sym, []))
    # Also include any market where we already have live price data for our goods
    for wp, prices in _market_cache.items():
        if any(sym in prices for sym in our_syms):
            candidate_markets.add(wp)

    market_values: dict[str, int] = {}
    for item in inventory:
        for wp in candidate_markets:
            price = get_market_prices(wp).get(item["symbol"], 0)
            # If no cached price, treat known importers as viable (real price on arrival)
            if price == 0 and wp in _good_buyers.get(item["symbol"], []):
                price = MIN_SELL_PRICE
            market_values[wp] = market_values.get(wp, 0) + price * item["units"]

    if not market_values:
        return ASTEROID_BASE

    # Identify the "base cluster": markets co-located with ASTEROID_BASE (≤5 units away).
    # These are free to visit since we refuel there anyway (e.g. H51 and H53 at H52's coords).
    bx, by = _get_coords(ASTEROID_BASE)
    cluster_val = 0
    cluster_best = ASTEROID_BASE
    for wp, val in market_values.items():
        wx, wy = _get_coords(wp)
        if ((wx - bx) ** 2 + (wy - by) ** 2) ** 0.5 < 5:
            if val > cluster_val:
                cluster_val, cluster_best = val, wp

    # Score every candidate by net value = raw revenue minus round-trip travel cost.
    # Cluster markets (≤5 units from ASTEROID_BASE) get zero penalty — we're going
    # there to refuel anyway.  Remote markets must sell FUEL and must beat the cluster
    # on net value to justify the extra trip.
    fuel_markets = set(_good_exporters.get("FUEL", []))
    best_net_wp, best_net_val = cluster_best, cluster_val
    for wp, raw_val in market_values.items():
        wx, wy = _get_coords(wp)
        dist = ((wx - bx) ** 2 + (wy - by) ** 2) ** 0.5
        if dist < 5:
            continue  # already captured in cluster_val/cluster_best above
        if wp not in fuel_markets:
            continue  # can't safely refuel for return trip — skip
        net_val = raw_val - dist * 2 * SELL_ROUTING_DIST_COST  # round-trip penalty
        if net_val > best_net_val:
            best_net_val = net_val
            best_net_wp  = wp

    if best_net_wp != cluster_best:
        travel_penalty = market_values[best_net_wp] - best_net_val
        log(
            f"[dim]Sell routing: {best_net_wp} net {best_net_val:,} cr "
            f"(raw {market_values[best_net_wp]:,} − {travel_penalty:,} travel cost) "
            f"vs cluster {cluster_val:,} cr[/dim]"
        )
        return best_net_wp

    return cluster_best


def show_market_table(waypoint: str) -> None:
    """Print a price table for a market waypoint."""
    prices = {k: v for k, v in get_market_prices(waypoint).items() if not k.startswith("_buy_")}
    if not prices:
        return
    t = Table(title=f"Market — {waypoint}", box=box.SIMPLE_HEAVY)
    t.add_column("Good")
    t.add_column("Sell", justify="right")
    for good, sell_price in sorted(prices.items(), key=lambda x: -x[1]):
        t.add_row(good, f"{sell_price:,} cr")
    console.print(t)


# ── Maintenance ───────────────────────────────────────────────────────────────

def _condition(component: dict) -> float:
    """Normalize component condition to 0.0–1.0 regardless of API representation."""
    val = component.get("condition", 1.0)
    return float(val) / 100.0 if val > 1.0 else float(val)


def needs_repair(ship: dict) -> bool:
    """Return True if any ship component is below REPAIR_THRESHOLD."""
    return any(_condition(ship.get(c, {})) < REPAIR_THRESHOLD for c in ("frame", "engine", "reactor"))


def repair_ship(ship_symbol: str) -> None:
    """Navigate to the shipyard and repair the ship, if affordable."""
    ship = fleet_api.get_ship(ship_symbol)
    if not needs_repair(ship):
        return
    worst = min(_condition(ship.get(c, {})) for c in ("frame", "engine", "reactor"))
    log(f"[yellow]🔧 {ship_symbol} needs repair (worst condition: {worst:.0%})[/yellow]")
    navigate_to(ship_symbol, SHIPYARD_WP)
    ensure_docked(ship_symbol)
    try:
        cost_data = fleet_api.get_repair_cost(ship_symbol)
        cost = cost_data.get("transaction", {}).get("totalCost", 0)
        me = agent_api.get_my_agent()
        if me["credits"] - cost < CREDIT_RESERVE:
            log(f"[red]Cannot afford repair ({cost:,} cr) — would breach reserve[/red]")
            return
        result = fleet_api.repair(ship_symbol)
        tx = result.get("transaction", {})
        log(f"[green]✓ {ship_symbol} repaired for {tx.get('totalPrice', tx.get('totalCost', 0)):,} cr[/green]")
    except SpaceTradersError as e:
        log(f"[red]Repair failed for {ship_symbol}: {e}[/red]")


def step_maintain_fleet() -> None:
    """Check all ships for condition and repair as needed."""
    log("[bold]Maintenance check[/bold]")
    ships = fleet_api.get_my_ships()
    repaired = 0
    for ship in ships:
        if needs_repair(ship):
            repair_ship(ship["symbol"])
            repaired += 1
    if repaired == 0:
        log("[dim]All ships in good condition ✓[/dim]")


# ── Upgrades ─────────────────────────────────────────────────────────────────

def _best_mount_tier(ship: dict) -> int:
    """Return 0-based index into MINING_MOUNT_TIERS for the best mount this ship has (-1 if none)."""
    symbols = {m.get("symbol") for m in ship.get("mounts", [])}
    for i in range(len(MINING_MOUNT_TIERS) - 1, -1, -1):
        if MINING_MOUNT_TIERS[i] in symbols:
            return i
    return -1


def upgrade_mining_mounts(ship_symbol: str) -> None:
    """
    Attempt to install the next-tier mining laser on a mining ship.
    1. Find a market that sells the target mount item.
    2. Navigate the ship there and purchase 1 unit into cargo.
    3. Navigate to SHIPYARD_WP and install the mount.
    """
    ship = fleet_api.get_ship(ship_symbol)
    if not has_mining_mount(ship):
        return
    # Skip if the ship is mid-flight; don't block the main loop waiting for a long transit
    if ship.get("nav", {}).get("status") == "IN_TRANSIT":
        log(f"[dim]{ship_symbol}: in transit — deferring upgrade to next loop[/dim]")
        return
    if not has_mining_mount(ship):
        return
    tier = _best_mount_tier(ship)
    if tier >= len(MINING_MOUNT_TIERS) - 1:
        log(f"[dim]{ship_symbol} already has best mining mount ({MINING_MOUNT_TIERS[-1]})[/dim]")
        return
    target = MINING_MOUNT_TIERS[tier + 1]
    current = MINING_MOUNT_TIERS[tier] if tier >= 0 else "none"
    log(f"[bold]Upgrading {ship_symbol}: {current} → {target}[/bold]")

    # 1. Find a market selling the mount item
    buy_wp = best_buy_waypoint(target)
    if not buy_wp:
        log(f"[yellow]{ship_symbol}: no market found selling {target} — skipping upgrade[/yellow]")
        return

    # 2. Navigate ship to the market and purchase 1 mount
    navigate_with_refuel(ship_symbol, buy_wp)
    ensure_docked(ship_symbol)
    try:
        fleet_api.purchase_cargo(ship_symbol, target, 1)
        log(f"[dim]{ship_symbol}: purchased {target} for upgrade[/dim]")
    except SpaceTradersError as e:
        log(f"[dim]{ship_symbol}: could not purchase {target}: {e} — skipping upgrade[/dim]")
        return

    # 3. Navigate to shipyard and install
    navigate_with_refuel(ship_symbol, SHIPYARD_WP)
    ensure_docked(ship_symbol)
    try:
        result = fleet_api.install_mount(ship_symbol, target)
        ag = result.get("agent", {})
        log(f"[green]🔩 Installed {target} on {ship_symbol}! Credits: {ag.get('credits', 0):,}[/green]")
    except SpaceTradersError as e:
        log(f"[dim]Mount upgrade not available ({target} on {ship_symbol}): {e}[/dim]")


def step_upgrade_fleet() -> None:
    """Upgrade all mining ships to max tier, looping until no more upgrades are possible."""
    log("[bold]Checking upgrade opportunities[/bold]")
    miners = get_mining_ships()
    if not miners:
        return
    for miner in miners:
        # Keep upgrading until max tier or no market found
        while True:
            ship = fleet_api.get_ship(miner)
            tier = _best_mount_tier(ship)
            if tier >= len(MINING_MOUNT_TIERS) - 1:
                break
            prev_tier = tier
            upgrade_mining_mounts(miner)
            # If tier didn't advance (e.g. market not found), stop to avoid infinite loop
            ship = fleet_api.get_ship(miner)
            if _best_mount_tier(ship) == prev_tier:
                break


def has_mining_mount(ship: dict) -> bool:
    """Return True if this ship has a mining laser mount."""
    return any("MINING" in m.get("symbol", "") for m in ship.get("mounts", []))


def has_survey_mount(ship: dict) -> bool:
    """Return True if this ship has a surveyor mount."""
    return any("SURVEYOR" in m.get("symbol", "") for m in ship.get("mounts", []))


def has_jump_drive(ship: dict) -> bool:
    """Return True if this ship has a jump drive module."""
    return any("JUMP_DRIVE" in m.get("symbol", "") for m in ship.get("modules", []))


def has_warp_drive(ship: dict) -> bool:
    """Return True if this ship has a warp drive module."""
    return any("WARP_DRIVE" in m.get("symbol", "") for m in ship.get("modules", []))


def get_mining_ships() -> list[str]:
    """Return symbols of all ships with mining mounts."""
    return [s["symbol"] for s in fleet_api.get_my_ships() if has_mining_mount(s)]


def try_survey(ship_symbol: str, good: str | None = None) -> dict | None:
    """Survey the current asteroid. Returns best survey for `good`, or any survey."""
    log(f"📡 Surveying asteroid for better yields...")
    try:
        wait_cooldown(ship_symbol)
        result = fleet_api.survey(ship_symbol)
        surveys = result.get("surveys", [])
        if good:
            focused = [s for s in surveys if any(d["symbol"] == good for d in s.get("deposits", []))]
            if focused:
                best = max(focused, key=lambda s: sum(1 for d in s["deposits"] if d["symbol"] == good))
                count = sum(1 for d in best["deposits"] if d["symbol"] == good)
                log(f"[green]Found survey with {count}x {good} deposits (size: {best['size']})[/green]")
                return best
        if surveys:
            log("[dim]No focused survey found, using generic survey[/dim]")
            return surveys[0]
        return None
    except SpaceTradersError as e:
        log(f"[dim]Survey failed: {e}, mining without survey[/dim]")
        return None


# ── Shared survey helpers ─────────────────────────────────────────────────────

def _prune_surveys() -> None:
    """Remove expired surveys from the shared pool. Caller must hold _surveys_lock."""
    now = datetime.now(timezone.utc)
    _shared_surveys[:] = [
        s for s in _shared_surveys
        if datetime.fromisoformat(s.get("expiration", "").replace("Z", "+00:00")) > now
    ]


def _add_shared_surveys(surveys: list[dict]) -> None:
    """Add surveys to the shared pool and prune expired ones."""
    with _surveys_lock:
        _shared_surveys.extend(surveys)
        _prune_surveys()


def _get_shared_survey(good: str) -> dict | None:
    """Return the best valid shared survey for `good` (surveys are reusable — not consumed)."""
    with _surveys_lock:
        _prune_surveys()
        focused = [
            s for s in _shared_surveys
            if any(d["symbol"] == good for d in s.get("deposits", []))
        ]
        if not focused:
            return None
        return max(focused, key=lambda s: sum(1 for d in s["deposits"] if d["symbol"] == good))


def _get_available_hauler(at_waypoint: str) -> str | None:
    """
    Return the symbol of a hauler currently orbiting at_waypoint with cargo
    space available, or None if no hauler is ready to accept transfers.
    """
    for symbol in _hauler_symbols:
        try:
            ship  = fleet_api.get_ship(symbol)
            nav   = ship["nav"]
            cargo = ship["cargo"]
            if (
                nav["waypointSymbol"] == at_waypoint
                and nav["status"] != "IN_TRANSIT"
                and cargo["capacity"] - cargo["units"] >= 10
            ):
                return symbol
        except SpaceTradersError:
            pass
    return None


# ── Dedicated surveyor loop ───────────────────────────────────────────────────

def surveyor_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """
    Dedicated surveyor thread. Navigates to B8, then continuously surveys the
    asteroid and publishes results to the shared pool for miners to consume.

    TODO (item 4): Target the asteroid that active miners are assigned to (the result
    of choose_mining_target for the lead miner) rather than always using the global
    ASTEROID.  Surveys are asteroid-specific — surveying the wrong asteroid wastes the
    surveyor when miners have been routed to a different target.  If miners are split,
    follow the asteroid with the most miners assigned to it.
    """
    while not contract_done.is_set() and not stop_event.is_set():
        try:
            _surveyor_loop_inner(ship_symbol, contract, contract_done, stop_event)
            return  # clean exit
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} surveyor thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _surveyor_loop_inner(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    good = contract["terms"]["deliver"][0]["tradeSymbol"]
    log(f"[magenta]🔭 {ship_symbol} surveyor thread started[/magenta]")

    # Preflight: get to ASTEROID_BASE for fuel, then to ASTEROID
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0  = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    if _fuel_pct0 < 0.90 and _wp0 != ASTEROID:
        _nearest = ASTEROID_BASE if _wp0 == ASTEROID_BASE else nearest_refuel_point(_wp0)
        log(f"[dim]{ship_symbol}: surveyor preflight refuel at {_nearest}[/dim]")
        if _wp0 != _nearest:
            navigate_to(ship_symbol, _nearest)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

    navigate_with_refuel(ship_symbol, ASTEROID)
    ensure_orbit(ship_symbol)

    while not stop_event.is_set() and not contract_done.is_set():
        # Proactive fuel check: only refuel if NOT already at the asteroid.
        # Surveying costs no fuel, so let small ships survey before drifting back.
        _sv_ship = fleet_api.get_ship(ship_symbol)
        _sv_fuel = _sv_ship["fuel"]
        _sv_at_asteroid = _sv_ship["nav"].get("waypointSymbol") == ASTEROID
        if not _sv_at_asteroid and _sv_fuel.get("capacity", 0) > 0 and _sv_fuel["current"] / _sv_fuel["capacity"] < 0.50:
            log(f"[yellow]⛽ {ship_symbol} surveyor: fuel low, topping up[/yellow]")
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
            ensure_docked(ship_symbol)
            refuel_if_needed(ship_symbol, threshold=100_000)
            navigate_with_refuel(ship_symbol, ASTEROID)
            ensure_orbit(ship_symbol)

        if stop_event.is_set() or contract_done.is_set():
            break

        try:
            wait_cooldown(ship_symbol)
            if stop_event.is_set() or contract_done.is_set():
                break

            result  = fleet_api.survey(ship_symbol)
            surveys = result.get("surveys", [])
            if surveys:
                for _sv in surveys:
                    db.upsert_survey(_sv)
                _add_shared_surveys(surveys)
                focused = [s for s in surveys if any(d["symbol"] == good for d in s.get("deposits", []))]
                total   = sum(sum(1 for d in s["deposits"] if d["symbol"] == good) for s in focused)
                with _surveys_lock:
                    pool_size = len(_shared_surveys)
                log(
                    f"[magenta]🔭 {ship_symbol}: {len(surveys)} survey(s) added "
                    f"({total}x {good} hits) | pool: {pool_size}[/magenta]"
                )
        except SpaceTradersError as e:
            log(f"[yellow]{ship_symbol} surveyor error: {e}[/yellow]")
            if not stop_event.is_set():
                time.sleep(10)

    log(f"[dim]{ship_symbol} surveyor thread done[/dim]")


# ── Dedicated hauler loop ─────────────────────────────────────────────────────

# Fraction of cargo capacity that triggers a hauler departure run
HAULER_DEPART_FRACTION = 0.50
# Depart if cargo > 0 and no new transfers have arrived for this many seconds
HAULER_MAX_WAIT_SECS   = 300


def hauler_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """
    Dedicated hauler thread. Parks at ASTEROID, waits for stationary miners to
    transfer their cargo, then shuttles to the delivery WP and sell markets and
    returns. Miners never leave the asteroid while a hauler is active.
    """
    while not contract_done.is_set() and not stop_event.is_set():
        try:
            _hauler_loop_inner(ship_symbol, contract, contract_done, stop_event)
            return  # clean exit
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} hauler thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _hauler_loop_inner(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    cid         = contract["id"]
    d           = contract["terms"]["deliver"][0]
    good        = d["tradeSymbol"]
    delivery_wp = d["destinationSymbol"]

    log(f"[blue]🚛 {ship_symbol} hauler thread started | contract good: {good}[/blue]")

    # Preflight: fuel up at base then head to asteroid
    navigate_with_refuel(ship_symbol, ASTEROID_BASE)
    ensure_docked(ship_symbol)
    refuel_if_needed(ship_symbol, threshold=100_000)
    navigate_with_refuel(ship_symbol, ASTEROID)
    ensure_orbit(ship_symbol)

    last_cargo_time = time.monotonic()  # reset each time we return from a run
    last_log_units  = -1                # suppress repeated "waiting" lines

    while not stop_event.is_set() and not contract_done.is_set():
        # ── Ensure we are orbiting at the asteroid ────────────────────────────
        _h = fleet_api.get_ship(ship_symbol)
        _h_nav = _h["nav"]
        if _h_nav["status"] == "IN_TRANSIT":
            wait_for_ship(ship_symbol)
            _h     = fleet_api.get_ship(ship_symbol)
            _h_nav = _h["nav"]
        if _h_nav["waypointSymbol"] != ASTEROID:
            navigate_with_refuel(ship_symbol, ASTEROID)
            ensure_orbit(ship_symbol)
            last_cargo_time = time.monotonic()
            continue

        _h_cargo    = _h["cargo"]
        _h_units    = _h_cargo["units"]
        _h_capacity = _h_cargo["capacity"]

        if _h_units > 0:
            last_cargo_time = time.monotonic()

        # Log only when cargo count changes (avoids console spam)
        if _h_units != last_log_units:
            log(f"[dim]🚛 {ship_symbol}: waiting at asteroid ({_h_units}/{_h_capacity} cargo)[/dim]")
            last_log_units = _h_units

        # ── Departure decision ────────────────────────────────────────────────
        _have_good = sum(
            i["units"] for i in _h_cargo.get("inventory", []) if i["symbol"] == good
        )
        _wait_secs = time.monotonic() - last_cargo_time
        _should_depart = (
            _h_units >= _h_capacity * HAULER_DEPART_FRACTION
            or (_have_good > 0 and _have_good >= min(30, _h_capacity // 4))
            or (_h_units > 0 and _wait_secs >= HAULER_MAX_WAIT_SECS)
        )

        if not _should_depart:
            stop_event.wait(15)
            continue

        log(
            f"[blue]🚛 {ship_symbol}: departing — "
            f"{_h_units}/{_h_capacity} cargo | {_have_good}x {good}[/blue]"
        )

        # ── Fuel up at base, then deliver and sell ────────────────────────────
        navigate_with_refuel(ship_symbol, ASTEROID_BASE)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

        # Deliver contract good if we have it
        if _have_good > 0 and not contract_done.is_set():
            try:
                _fc = contracts_api.get_contract(cid)
                for _dt in _fc.get("terms", {}).get("deliver", []):
                    if _dt["tradeSymbol"] == good:
                        _remaining = _dt["unitsRequired"] - _dt["unitsFulfilled"]
                        if _remaining <= 0:
                            contract_done.set()
                            return
                        _to_deliver = min(_have_good, _remaining)
                        navigate_with_refuel(ship_symbol, delivery_wp)
                        ensure_docked(ship_symbol)
                        result = contracts_api.deliver_contract(cid, ship_symbol, good, _to_deliver)
                        _c = result.get("contract", {})
                        for _dt2 in _c.get("terms", {}).get("deliver", []):
                            if _dt2["tradeSymbol"] == good:
                                _f   = _dt2["unitsFulfilled"]
                                _req = _dt2["unitsRequired"]
                                log(f"[green]✓ {ship_symbol} hauler: {_f}/{_req} {good} delivered[/green]")
                                if _f >= _req:
                                    with _fulfill_lock:
                                        if not contract_done.is_set():
                                            _res = contracts_api.fulfill_contract(cid)
                                            _ag  = _res.get("agent", {})
                                            log(f"[green bold]🏆 Contract fulfilled! Credits: {_ag.get('credits', 0):,}[/green bold]")
                                            contract_done.set()
                                    return
                        break
            except SpaceTradersError as e:
                log(f"[yellow]{ship_symbol} hauler delivery error: {e}[/yellow]")

        if contract_done.is_set():
            return

        # Sell all junk (routes to best market automatically)
        sell_junk(ship_symbol, good)

        # Return to base to refuel, then back to asteroid
        navigate_with_refuel(ship_symbol, ASTEROID_BASE)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)
        navigate_with_refuel(ship_symbol, ASTEROID)
        ensure_orbit(ship_symbol)
        last_cargo_time = time.monotonic()
        last_log_units  = -1   # reset so first "waiting" line re-appears

    log(f"[dim]{ship_symbol} hauler thread done[/dim]")


def miner_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """
    Per-ship mining thread. Mines at ASTEROID, delivers contract good,
    sells junk at best market. Exits when contract_done or stop_event is set.
    """
    while not contract_done.is_set() and not stop_event.is_set():
        try:
            _miner_loop_inner(ship_symbol, contract, contract_done, stop_event)
            return  # clean exit
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} miner thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _miner_loop_inner(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    cid = contract["id"]
    d = contract["terms"]["deliver"][0]  # first delivery term
    good = d["tradeSymbol"]
    delivery_wp = d["destinationSymbol"]

    # Choose the best asteroid for this miner + contract (re-evaluated fresh per contract).
    mining_target       = choose_mining_target(ship_symbol, contract)
    # Shared surveys are generated at the default ASTEROID; skip them if we're mining elsewhere.
    _use_shared_surveys = (mining_target == ASTEROID)

    log(f"[cyan]⚓ {ship_symbol} thread started | mining: {good} → {delivery_wp}[/cyan]")

    # Safe startup: if not near the asteroid or fuel < 50%, refuel at ASTEROID_BASE first
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0 = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    if _fuel_pct0 < 0.90 and _wp0 != mining_target:
        log(f"[dim]{ship_symbol}: preflight refuel at {ASTEROID_BASE} (wp={_wp0}, fuel={_f0['current']}/{_f0['capacity']})[/dim]")
        if _wp0 != ASTEROID_BASE:
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max

    # Preflight delivery shortcut: if we already have enough contract goods, deliver now
    _s0_cargo = _s0["cargo"]
    _have_preflight = sum(i["units"] for i in _s0_cargo.get("inventory", []) if i["symbol"] == good)
    if _have_preflight > 0 and not contract_done.is_set():
        try:
            _fc_pre = contracts_api.get_contract(cid)
            for _dt_pre in _fc_pre.get("terms", {}).get("deliver", []):
                if _dt_pre["tradeSymbol"] == good:
                    _remaining_pre = _dt_pre["unitsRequired"] - _dt_pre["unitsFulfilled"]
                    if _have_preflight >= _remaining_pre > 0:
                        log(f"[cyan]{ship_symbol}: preflight shortcut — has {_have_preflight}x {good}, need {_remaining_pre} — delivering now[/cyan]")
                        navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                        if contract_done.is_set():
                            return  # contract was fulfilled while we navigated; bail out cleanly
                        ensure_docked(ship_symbol)
                        refuel_if_needed(ship_symbol, threshold=100_000)
                        navigate_with_refuel(ship_symbol, delivery_wp)
                        ensure_docked(ship_symbol)
                        _deliver_amt = min(_have_preflight, _remaining_pre)
                        result = contracts_api.deliver_contract(cid, ship_symbol, good, _deliver_amt)
                        c = result.get("contract", {})
                        for dt in c.get("terms", {}).get("deliver", []):
                            if dt["tradeSymbol"] == good:
                                f = dt["unitsFulfilled"]
                                req = dt["unitsRequired"]
                                log(f"[green]✓ {ship_symbol}: {f}/{req} {good} delivered (preflight)[/green]")
                                if f >= req:
                                    with _fulfill_lock:
                                        if not contract_done.is_set():
                                            res = contracts_api.fulfill_contract(cid)
                                            ag = res.get("agent", {})
                                            log(f"[green bold]🏆 Contract fulfilled! Credits: {ag.get('credits', 0):,}[/green bold]")
                                            contract_done.set()
                                    return
                    break
        except SpaceTradersError as e:
            log(f"[dim]{ship_symbol}: preflight delivery check failed: {e}[/dim]")

    # Decide whether to mine or buy:
    # - Manufactured goods (not in MINEABLE_GOODS) must be purchased.
    # - Mineable goods in MINEABLE_GOODS are mined UNLESS:
    #     (a) market price is trivially cheap (≤ CHEAP_BUY_THRESHOLD), OR
    #     (b) db.can_be_mined() confirms no asteroid in the system has the
    #         required deposit trait — so it physically cannot drop here.
    _buy_wp_check = best_buy_waypoint(good)
    _buy_price_check = _market_cache.get(_buy_wp_check or "", {}).get(f"_buy_{good}", 0) if _buy_wp_check else 0
    _is_mineable = good in MINEABLE_GOODS

    # Check whether any scanned asteroid in this system can yield this good.
    _no_deposit = bool(
        _is_mineable
        and not db.can_be_mined(good, SYSTEM)  # [] means no matching deposit trait found
    )
    if _no_deposit:
        _required_traits = GOOD_TO_DEPOSIT_TRAITS.get(good, frozenset())
        log(f"[yellow]{ship_symbol}: {good} requires {sorted(_required_traits)} — not present in any scanned asteroid, skipping straight to buy[/yellow]")

    _direct_buy = bool(
        _buy_wp_check
        and (
            not _is_mineable
            or _no_deposit
            or (_buy_price_check > 0 and _buy_price_check <= CHEAP_BUY_THRESHOLD)
        )
    )
    if _direct_buy:
        _reason = (
            "no deposit in system" if _no_deposit
            else "cheap ore" if _is_mineable
            else "non-mineable good"
        )
        log(f"[cyan]{ship_symbol}: {good} will be purchased ({_reason} @ {_buy_price_check:,} cr/u)[/cyan]")
        # Non-mineable goods and no-deposit goods skip the asteroid entirely.
        # Cheap ores still visit the asteroid base (serves as a refuel waypoint en route).
        if (_no_deposit or not _is_mineable) and _buy_wp_check:
            navigate_with_refuel(ship_symbol, _buy_wp_check)
        else:
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
        active_survey = None
        _empty_loads = 3  # trigger buy on first loop iteration
    else:
        navigate_with_refuel(ship_symbol, mining_target)
        ensure_orbit(ship_symbol)
        active_survey = (_get_shared_survey(good) if _use_shared_surveys else None) or try_survey(ship_symbol, good)
        _empty_loads = 0  # consecutive full cargo loads with 0 contract good
        _dry_extractions = 0  # consecutive extractions yielding 0 of the contract good

    while not stop_event.is_set() and not contract_done.is_set():
        # Refresh active_survey from shared pool if we don't have one
        if active_survey is None:
            active_survey = _get_shared_survey(good) if _use_shared_surveys else None

        # ── Proactive fuel + condition check (one API call) ───────────────────
        _loop_ship = fleet_api.get_ship(ship_symbol)
        _fuel = _loop_ship["fuel"]
        _at_asteroid = _loop_ship["nav"].get("waypointSymbol") == mining_target
        _at_buy_wp   = _direct_buy and _loop_ship["nav"].get("waypointSymbol") == _buy_wp_check
        # Only refuel if NOT already at the asteroid — mining costs no fuel, so
        # let small ships (80-cap) mine a full load before drifting back to B7.
        # Skip the refuel detour if we're already AT the buy waypoint in direct-buy mode:
        # the delivery branch already refuels at ASTEROID_BASE before the delivery run.
        if not _at_asteroid and not _at_buy_wp and _fuel.get("capacity", 0) > 0 and _fuel["current"] / _fuel["capacity"] < 0.40:
            log(f"[yellow]⛽ {ship_symbol}: fuel low ({_fuel['current']}/{_fuel['capacity']}), topping up[/yellow]")
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
            ensure_docked(ship_symbol)
            refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max
            # In direct-buy mode (or if we already have cargo) let the main
            # loop decide where to go next — don't detour back to the asteroid.
            if not _direct_buy:
                navigate_with_refuel(ship_symbol, mining_target)
                ensure_orbit(ship_symbol)
                active_survey = (_get_shared_survey(good) if _use_shared_surveys else None) or try_survey(ship_symbol, good)

        # ── Proactive repair check: detour to shipyard if condition degraded ──
        if needs_repair(_loop_ship):
            _cond = min(_condition(_loop_ship.get(c, {})) for c in ("frame", "engine", "reactor"))
            log(f"[yellow]🔧 {ship_symbol}: condition {_cond:.0%} below threshold — diverting to repair[/yellow]")
            repair_ship(ship_symbol)
            navigate_with_refuel(ship_symbol, mining_target)
            ensure_orbit(ship_symbol)
            active_survey = (_get_shared_survey(good) if _use_shared_surveys else None) or try_survey(ship_symbol, good)

        # ── Cargo almost full OR have contract good while not at asteroid ─────
        _loop_cargo = _loop_ship["cargo"]
        _loop_space = _loop_cargo["capacity"] - _loop_cargo["units"]
        _have_cached = sum(
            i["units"] for i in _loop_cargo.get("inventory", [])
            if i["symbol"] == good
        )
        # Skip mining when good is purchasable (cargo may have junk; sell_junk handles it)
        _skip_to_buy = (
            _direct_buy and _empty_loads >= 3
            and not contract_done.is_set()
        )
        # Force buy mode early if many consecutive extractions yielded nothing
        _force_buy = (
            not _direct_buy
            and _dry_extractions >= DRY_EXTRACT_THRESHOLD
            and bool(best_buy_waypoint(good))
            and not contract_done.is_set()
        )
        if _loop_space < 5 or (_have_cached > 0 and not _at_asteroid) or _skip_to_buy or _force_buy:
            # ── Stationary mode: offload everything to hauler when possible ───
            if _loop_space < 5 and _at_asteroid and _hauler_symbols:
                _avail_hauler = _get_available_hauler(mining_target)
                if _avail_hauler:
                    _transferred = 0
                    for _item in list(_loop_cargo.get("inventory", [])):
                        try:
                            fleet_api.transfer_cargo(
                                ship_symbol, _item["symbol"], _item["units"], _avail_hauler
                            )
                            _transferred += _item["units"]
                        except SpaceTradersError as _te:
                            log(
                                f"[yellow]{ship_symbol}: transfer → {_avail_hauler} "
                                f"failed ({_item['symbol']}): {_te}[/yellow]"
                            )
                    if _transferred > 0:
                        log(f"[cyan]📦 {ship_symbol}: transferred {_transferred}u → {_avail_hauler}[/cyan]")
                    continue  # back to mining whether transfer succeeded or not

            have = _have_cached

            if have > 0 and not contract_done.is_set():
                if _empty_loads < 3:
                    _empty_loads = 0  # reset only when good was mined, not bought
                _dry_extractions = 0  # good found — reset dry streak
                # Cap delivery to remaining needed — API rejects over-delivery with 4508
                try:
                    _fc = contracts_api.get_contract(cid)
                    for _dt in _fc.get("terms", {}).get("deliver", []):
                        if _dt["tradeSymbol"] == good:
                            _remaining = _dt["unitsRequired"] - _dt["unitsFulfilled"]
                            if _remaining <= 0:
                                contract_done.set()
                                return
                            have = min(have, _remaining)
                            break
                except SpaceTradersError:
                    pass  # use original have if fetch fails
                # Refuel at ASTEROID_BASE before the long delivery trip to maximize fuel
                navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)
                navigate_with_refuel(ship_symbol, delivery_wp)
                ensure_docked(ship_symbol)
                try:
                    result = contracts_api.deliver_contract(cid, ship_symbol, good, have)
                    c = result.get("contract", {})
                    for dt in c.get("terms", {}).get("deliver", []):
                        if dt["tradeSymbol"] == good:
                            f = dt["unitsFulfilled"]
                            req = dt["unitsRequired"]
                            log(f"[green]✓ {ship_symbol}: {f}/{req} {good} delivered[/green]")
                            if f >= req:
                                with _fulfill_lock:
                                    if not contract_done.is_set():
                                        res = contracts_api.fulfill_contract(cid)
                                        ag = res.get("agent", {})
                                        log(f"[green bold]🏆 Contract fulfilled! Credits: {ag.get('credits', 0):,}[/green bold]")
                                        contract_done.set()
                                return
                except SpaceTradersError as e:
                    if contract_done.is_set():
                        return
                    log(f"[yellow]{ship_symbol} delivery error: {e}[/yellow]")

                if not contract_done.is_set():
                    # Multi-hop back to asteroid (delivery WP may be far from ASTEROID_BASE)
                    _nearest = nearest_refuel_point(delivery_wp)
                    navigate_with_refuel(ship_symbol, _nearest)
                    ensure_docked(ship_symbol)
                    refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max
                    if not _direct_buy:
                        navigate_with_refuel(ship_symbol, mining_target)
                        ensure_orbit(ship_symbol)
            else:
                # No contract good in cargo — junk run, then optionally buy the good
                if _force_buy and _empty_loads < 3:
                    log(f"[yellow]{ship_symbol}: {_dry_extractions} consecutive dry extractions — escalating to buy mode early[/yellow]")
                    _empty_loads = 3
                    _dry_extractions = 0
                _empty_loads += 1
                if _direct_buy:
                    # Already in buy mode — skip the asteroid-base junk run entirely.
                    # Cargo is empty (just delivered), so there's nothing to sell.
                    # Go straight to the buy market.
                    pass
                else:
                    navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                    ensure_docked(ship_symbol)
                    refuel_if_needed(ship_symbol, threshold=100_000)  # always fill to max
                    sell_junk(ship_symbol, good)
                    # If sell_junk routed to a remote market, refuel at base before returning to asteroid
                    navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                    ensure_docked(ship_symbol)
                    refuel_if_needed(ship_symbol, threshold=100_000)

                if _empty_loads >= 3 and not contract_done.is_set():
                    _buy_wp = best_buy_waypoint(good)
                    if _buy_wp:
                        log(f"[cyan]{ship_symbol}: {_empty_loads} empty loads — switching to buy {good} from {_buy_wp}[/cyan]")
                        navigate_with_refuel(ship_symbol, _buy_wp)
                        ensure_docked(ship_symbol)
                        _market_cache_ts.pop(_buy_wp, None)  # force fresh query while docked
                        get_market_prices(_buy_wp)  # populate cache (needs ship present)
                        _buy_price = _market_cache.get(_buy_wp, {}).get(f"_buy_{good}", 0)
                        if _buy_price > 0:
                            _me = agent_api.get_my_agent()
                            _ship_now = fleet_api.get_ship(ship_symbol)
                            _free = _ship_now["cargo"]["capacity"] - _ship_now["cargo"]["units"]
                            # Check remaining need first so we can relax reserve for the last few units
                            _still_need = 9999
                            try:
                                _fc2 = contracts_api.get_contract(cid)
                                for _dt2 in _fc2.get("terms", {}).get("deliver", []):
                                    if _dt2["tradeSymbol"] == good:
                                        _still_need = _dt2["unitsRequired"] - _dt2["unitsFulfilled"]
                                        break
                            except SpaceTradersError:
                                pass
                            # In direct-buy mode, use a lower reserve — no passive income from mining
                            # so we need to spend more aggressively to make progress.
                            _buy_reserve = (
                                max(5_000, CREDIT_RESERVE // 6) if _direct_buy
                                else CREDIT_RESERVE if _still_need > 5
                                else max(5_000, CREDIT_RESERVE // 4)
                            )
                            _affordable = max(0, (_me["credits"] - _buy_reserve) // _buy_price)
                            _to_buy = min(_free, _affordable, max(0, _still_need))
                            if _to_buy > 0:
                                try:
                                    result = fleet_api.purchase_cargo(ship_symbol, good, _to_buy)
                                    ag = result.get("agent", {})
                                    tx = result.get("transaction", {})
                                    log(f"[green]🛒 {ship_symbol}: bought {_to_buy}x {good} @ {_buy_price:,} cr/u | total {tx.get('totalPrice',0):,} cr | Credits: {ag.get('credits',0):,}[/green]")
                                    db.log_transaction(_buy_wp, ship_symbol, good, "PURCHASE",
                                                       _to_buy, _buy_price, tx.get("totalPrice", 0))
                                    _empty_loads = 3  # stay in buy-mode; next junk load re-triggers buy
                                    continue  # cargo now has the good — deliver path fires next iteration
                                except SpaceTradersError as e:
                                    log(f"[yellow]{ship_symbol}: purchase failed: {e}[/yellow]")
                            else:
                                _affordable_str = f"{_affordable}u affordable" if _buy_price > 0 else "price unknown"
                                log(f"[yellow]{ship_symbol}: can't buy {good} — {_affordable_str} (credits: {_me['credits']:,}, reserve: {_buy_reserve:,})[/yellow]")
                                if _direct_buy:
                                    # Credits too low to buy even 1 unit — fall back to mining
                                    # for income so we don't tight-loop or stall the contract.
                                    log(f"[yellow]{ship_symbol}: mining for income to fund direct-buy contract[/yellow]")
                                    _empty_loads = 0  # exit buy mode; mine 3 loads then retry
                                    navigate_with_refuel(ship_symbol, mining_target)
                                    ensure_orbit(ship_symbol)
                                    active_survey = (_get_shared_survey(good) if _use_shared_surveys else None) or try_survey(ship_symbol, good)
                        else:
                            log(f"[yellow]{ship_symbol}: {good} not found in {_buy_wp} tradeGoods — blacklisting for 20 min[/yellow]")
                            _buy_source_blacklist[_buy_wp] = time.time() + 1200  # 20-minute cooldown
                    else:
                        # No market exports this good at all
                        if _empty_loads >= 5:
                            if good == "__SELL_ONLY__":
                                # Sell-only ships mine byproducts forever — reset and continue
                                _empty_loads = 0
                                navigate_with_refuel(ship_symbol, mining_target)
                                ensure_orbit(ship_symbol)
                                continue
                            log(f"[red bold]{ship_symbol}: {good} cannot be mined or purchased after {_empty_loads} loads — contract unworkable, exiting[/red bold]")
                            return

                if not _direct_buy:
                    navigate_with_refuel(ship_symbol, mining_target)
                    ensure_orbit(ship_symbol)
            continue

        if contract_done.is_set():
            break

        # ── Wait for cooldown ─────────────────────────────────────────────────
        wait_cooldown(ship_symbol)

        if contract_done.is_set():
            break

        # ── Extract ───────────────────────────────────────────────────────────
        try:
            if active_survey:
                try:
                    result = fleet_api.extract_with_survey(ship_symbol, active_survey)
                except SpaceTradersError as e:
                    if e.code in (4224, 4000) or "survey" in str(e).lower():
                        log(f"[dim]{ship_symbol}: Survey expired, trying shared pool[/dim]")
                        active_survey = _get_shared_survey(good)
                        if active_survey:
                            result = fleet_api.extract_with_survey(ship_symbol, active_survey)
                        else:
                            result = fleet_api.extract(ship_symbol)
                    else:
                        raise
            else:
                result = fleet_api.extract(ship_symbol)

            yld   = result.get("extraction", {}).get("yield", {})
            cargo = result.get("cargo", {})
            cd    = result.get("cooldown", {}).get("remainingSeconds", 0)
            have  = good_in_cargo(ship_symbol, good)
            log(
                f"  [cyan]{ship_symbol}[/cyan]: "
                f"{yld.get('units')}x {yld.get('symbol')} | "
                f"{good}: {have} | "
                f"Cargo: {cargo.get('units')}/{cargo.get('capacity')} | "
                f"CD: {cd}s"
            )
            if yld.get("symbol") == good:
                _dry_extractions = 0
            else:
                _dry_extractions += 1
            if yld.get("symbol"):
                db.log_extraction(
                    mining_target, ship_symbol,
                    active_survey.get("signature") if active_survey else None,
                    yld["symbol"], yld.get("units", 0),
                )
            for ev in result.get("events", []):
                log(f"  [yellow]⚠ {ship_symbol}: {ev.get('name')}: {ev.get('description', '')}[/yellow]")

        except SpaceTradersError as e:
            if e.code == 4228:  # cargo full — handled at top of loop
                pass
            else:
                log(f"[red]{ship_symbol} extract error: {e}[/red]")
                time.sleep(5)

    log(f"[dim]{ship_symbol} miner thread done[/dim]")


# ── Siphon loop (gas giants) ──────────────────────────────────────────────────

def _find_gas_giants() -> list[str]:
    """Return waypoint symbols of gas giants in the current system from the DB."""
    with db._conn() as con:
        rows = con.execute(
            "SELECT symbol FROM waypoints WHERE system_symbol = ? AND type = 'GAS_GIANT' ORDER BY symbol",
            (SYSTEM,),
        ).fetchall()
    return [r[0] for r in rows]


def siphon_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Siphon gas from gas giants and sell at the best nearby market."""
    log(f"[blue]🌀 {ship_symbol} siphon thread started[/blue]")
    while not stop_event.is_set():
        try:
            _siphon_loop_inner(ship_symbol, stop_event)
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} siphon thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _siphon_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    gas_giants = _find_gas_giants()
    if not gas_giants:
        log(f"[yellow]{ship_symbol}: no gas giants in {SYSTEM} — siphon loop idle (retry in 10min)[/yellow]")
        stop_event.wait(600)
        return

    # Pick closest gas giant by coordinate distance from ASTEROID_BASE
    target_gg = min(
        gas_giants,
        key=lambda wp: waypoint_distance(wp, ASTEROID_BASE),
    )
    log(f"[blue]🌀 {ship_symbol}: targeting gas giant {target_gg}[/blue]")
    navigate_with_refuel(ship_symbol, target_gg)
    ensure_orbit(ship_symbol)

    while not stop_event.is_set():
        wait_cooldown(ship_symbol)
        ship_data = fleet_api.get_ship(ship_symbol)
        cargo     = ship_data["cargo"]
        if cargo["units"] >= cargo["capacity"]:
            # Cargo full — sell everything then return to gas giant
            _sell_siphon_goods(ship_symbol)
            navigate_with_refuel(ship_symbol, target_gg)
            ensure_orbit(ship_symbol)
            continue

        try:
            result = fleet_api.siphon(ship_symbol)
            yld    = result.get("siphon", {}).get("yield", {})
            sym    = yld.get("symbol", "?")
            amt    = yld.get("units", 0)
            c_now  = result.get("cargo", {})
            cd     = result.get("cooldown", {}).get("remainingSeconds", 0)
            log(
                f"[blue]🌀 {ship_symbol}: {amt}x {sym} | "
                f"Cargo: {c_now.get('units',0)}/{c_now.get('capacity',1)} | CD: {cd}s[/blue]"
            )
            if cd > 0:
                stop_event.wait(cd)
        except SpaceTradersError as e:
            log(f"[yellow]{ship_symbol} siphon error: {e}[/yellow]")
            stop_event.wait(15)


def _sell_siphon_goods(ship_symbol: str) -> None:
    """Sell all siphoned cargo at the best available markets."""
    ship_data = fleet_api.get_ship(ship_symbol)
    inventory = ship_data["cargo"].get("inventory", [])
    if not inventory:
        return
    # sell_junk handles routing and picking the best market for each good
    sell_junk(ship_symbol, "__NONE__")


# ── Trader loop (arbitrage) ───────────────────────────────────────────────────

TRADER_MIN_MARGIN     = 150    # cr/unit — skip opportunities below this spread
TRADER_MIN_ROI        = 0.10   # 10% minimum return on investment per trip
TRADER_CREDIT_RESERVE = 150_000  # keep at least this much when spending on cargo


def trader_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Buy low / sell high using market arbitrage within the system."""
    log(f"[magenta]💹 {ship_symbol} trader thread started[/magenta]")
    while not stop_event.is_set():
        try:
            _trader_loop_inner(ship_symbol, stop_event)
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} trader thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _trader_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        opps = db.get_arbitrage_opportunities(SYSTEM, min_margin=TRADER_MIN_MARGIN)

        # Filter to opportunities with sufficient ROI
        viable = [
            o for o in opps
            if o["buy_price"] > 0
            and (o["margin"] / o["buy_price"]) >= TRADER_MIN_ROI
        ]

        if not viable:
            log(f"[dim]{ship_symbol}: no profitable arbitrage found — waiting 5min[/dim]")
            stop_event.wait(300)
            continue

        best = viable[0]  # already sorted by margin DESC
        good  = best["trade_symbol"]
        buy_wp  = best["buy_at"]
        sell_wp = best["sell_at"]

        # ── Pre-check: already have cargo → skip straight to sell ─────────────
        # Must happen BEFORE the credits check so a low-credit state doesn't
        # block selling cargo we already hold.
        # Check for ANY cargo, not just the current best opp — the best opp may
        # have changed since we bought (market absorption pushes buy price up).
        _pre_ship = fleet_api.get_ship(ship_symbol)
        _pre_inv  = [i for i in _pre_ship["cargo"].get("inventory", []) if i["units"] > 0]
        if _pre_inv:
            # Find the best sell destination for what we're actually carrying
            _pre_item   = _pre_inv[0]
            good        = _pre_item["symbol"]
            _pre_have   = _pre_item["units"]
            # Look for a sell opportunity for this good; fall back to best_sell_waypoint
            _pre_opps   = [o for o in db.get_arbitrage_opportunities(SYSTEM, min_margin=0)
                           if o["trade_symbol"] == good and o["sell_price"] > 0]
            sell_wp     = _pre_opps[0]["sell_at"] if _pre_opps else best_sell_waypoint(good)[0]
        _pre_have = sum(i["units"] for i in _pre_inv if i["symbol"] == good) if _pre_inv else 0
        if _pre_have > 0:
            log(f"[dim]{ship_symbol}: already carrying {_pre_have}x {good} — skipping buy, going to sell @ {sell_wp}[/dim]")
            navigate_with_refuel(ship_symbol, sell_wp)
            ensure_docked(ship_symbol)
            refuel_if_needed(ship_symbol, threshold=100_000)
            ship_data3 = fleet_api.get_ship(ship_symbol)
            have = sum(i["units"] for i in ship_data3["cargo"].get("inventory", []) if i["symbol"] == good)
            if have > 0:
                _sell_batch_size  = have
                _sell_remaining   = have
                _sell_total_rev   = 0
                _sell_price       = 0
                _sell_first_price = 0  # price of first successful batch — used as floor
                _sell_failed      = False
                while _sell_remaining > 0:
                    _this_sell = min(_sell_batch_size, _sell_remaining)
                    try:
                        result = fleet_api.sell_cargo(ship_symbol, good, _this_sell)
                        ag  = result.get("agent", {})
                        tx  = result.get("transaction", {})
                        _sell_price     = tx.get("pricePerUnit", 0)
                        if _sell_first_price == 0:
                            _sell_first_price = _sell_price
                        # Stop selling if market has crashed to <10% of opening price
                        elif _sell_price < _sell_first_price * 0.10:
                            log(f"[yellow]{ship_symbol}: {good} price crashed {_sell_price:,}/u vs opening {_sell_first_price:,}/u — stopping sell, market saturated[/yellow]")
                            _sell_failed = True
                            break
                        _sell_total_rev += tx.get("totalPrice", 0)
                        _sell_remaining -= _this_sell
                        if _sell_remaining > 0:
                            log(f"[magenta]💰 {ship_symbol}: sold {_this_sell}x {good} @ {_sell_price:,}/u | Credits: {ag.get('credits', 0):,}[/magenta]")
                        db.log_transaction(sell_wp, ship_symbol, good, "SELL", _this_sell, _sell_price, tx.get("totalPrice", 0))
                    except SpaceTradersError as e:
                        import re as _re
                        _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                        if _lm and _sell_batch_size > 1:
                            _sell_batch_size = int(_lm.group(1))
                            log(f"[dim]{ship_symbol}: adjusting sell batch to {_sell_batch_size}/tx[/dim]")
                            continue
                        log(f"[yellow]{ship_symbol}: sell {good} failed: {e}[/yellow]")
                        _sell_failed = True
                        sell_junk(ship_symbol, "__NONE__")
                        break
                if not _sell_failed and _sell_total_rev > 0:
                    log(f"[magenta]💰 {ship_symbol}: sold {have}x {good} @ {_sell_price:,}/u = {_sell_total_rev:,} cr[/magenta]")
            continue

        # ── Credits / affordability check (after pre-check so we can sell existing cargo even when broke) ──
        me       = agent_api.get_my_agent()
        credits  = me["credits"]
        ship_data = fleet_api.get_ship(ship_symbol)
        capacity  = ship_data["cargo"]["capacity"]
        affordable = max(0, (credits - TRADER_CREDIT_RESERVE) // best["buy_price"])
        to_buy = min(capacity, affordable)

        if to_buy < 5:
            log(
                f"[yellow]{ship_symbol}: not enough credits for trade "
                f"(need {TRADER_CREDIT_RESERVE + best['buy_price'] * 5:,}, have {credits:,}) "
                f"— waiting 2min[/yellow]"
            )
            stop_event.wait(120)
            continue

        est_profit = to_buy * best["margin"]
        log(
            f"[magenta]💹 {ship_symbol}: {good} {buy_wp}→{sell_wp} "
            f"buy {best['buy_price']:,}/u sell {best['sell_price']:,}/u "
            f"(+{best['margin']:,}/u) × {to_buy}u = est +{est_profit:,} cr[/magenta]"
        )

        # ── Buy ──────────────────────────────────────────────────────────────
        navigate_with_refuel(ship_symbol, buy_wp)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

        # Refresh live price (needs docked ship)
        _market_cache_ts.pop(buy_wp, None)
        get_market_prices(buy_wp)
        live_buy = _market_cache.get(buy_wp, {}).get(f"_buy_{good}", 0)

        if live_buy <= 0:
            log(f"[yellow]{ship_symbol}: {good} not available at {buy_wp} — re-scanning[/yellow]")
            continue

        # Re-score opportunities with the freshly updated DB prices
        _live_margin = best["sell_price"] - live_buy
        _live_roi    = _live_margin / live_buy if live_buy > 0 else 0
        if _live_margin < TRADER_MIN_MARGIN or _live_roi < TRADER_MIN_ROI:
            log(
                f"[yellow]{ship_symbol}: {good} margin shrank to {_live_margin:,}/u "
                f"({_live_roi:.0%} ROI) at live price {live_buy:,} — re-scanning[/yellow]"
            )
            continue
        # Check if a different route is now clearly better after cache refresh
        _fresh_opps = [
            o for o in db.get_arbitrage_opportunities(SYSTEM, min_margin=TRADER_MIN_MARGIN)
            if o["buy_price"] > 0 and (o["margin"] / o["buy_price"]) >= TRADER_MIN_ROI
        ]
        if _fresh_opps and _fresh_opps[0]["trade_symbol"] != good:
            _alt = _fresh_opps[0]
            if _alt["margin"] > _live_margin * 1.25:  # only switch if 25% better
                log(
                    f"[cyan]{ship_symbol}: better route after cache refresh — "
                    f"{_alt['trade_symbol']} +{_alt['margin']:,}/u > "
                    f"{good} +{_live_margin:,}/u — re-routing[/cyan]"
                )
                continue
        if live_buy != best["buy_price"]:
            log(
                f"[dim]{ship_symbol}: {good} live buy {live_buy:,}/u "
                f"(was {best['buy_price']:,} cached), margin {_live_margin:,}/u[/dim]"
            )

        me2        = agent_api.get_my_agent()
        ship_data2 = fleet_api.get_ship(ship_symbol)
        free_cargo  = ship_data2["cargo"]["capacity"] - ship_data2["cargo"]["units"]
        affordable2 = max(0, (me2["credits"] - TRADER_CREDIT_RESERVE) // live_buy)
        to_buy2     = min(free_cargo, affordable2)

        if to_buy2 < 1:
            log(f"[yellow]{ship_symbol}: can't afford even 1u at live price {live_buy:,}[/yellow]")
            continue

        # Buy in batches to respect per-transaction limits (some goods cap at 6/tx)
        _bought_total = 0
        _buy_failed = False
        _batch_size = to_buy2
        _remaining_to_buy = to_buy2
        while _remaining_to_buy > 0:
            _this_batch = min(_batch_size, _remaining_to_buy)
            try:
                result = fleet_api.purchase_cargo(ship_symbol, good, _this_batch)
                ag = result.get("agent", {})
                tx = result.get("transaction", {})
                _bought_total += _this_batch
                _remaining_to_buy -= _this_batch
                log(
                    f"[magenta]🛒 {ship_symbol}: bought {_this_batch}x {good} @ {live_buy:,}/u "
                    f"| Credits: {ag.get('credits', 0):,}[/magenta]"
                )
                db.log_transaction(buy_wp, ship_symbol, good, "PURCHASE",
                                   _this_batch, live_buy, tx.get("totalPrice", 0))
            except SpaceTradersError as e:
                err_str = str(e)
                # Detect per-transaction limit error and retry with the limit as batch size
                import re as _re
                _limit_match = _re.search(r"limit of (\d+) units per transaction", err_str)
                if _limit_match and _batch_size > 1:
                    _batch_size = int(_limit_match.group(1))
                    log(f"[dim]{ship_symbol}: adjusting batch size to {_batch_size}/tx for {good}[/dim]")
                    continue  # retry with smaller batch
                log(f"[yellow]{ship_symbol}: purchase failed: {e}[/yellow]")
                _buy_failed = True
                break
        if _buy_failed and _bought_total == 0:
            continue

        # ── Sell ─────────────────────────────────────────────────────────────
        navigate_with_refuel(ship_symbol, sell_wp)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

        ship_data3 = fleet_api.get_ship(ship_symbol)
        have = sum(
            i["units"] for i in ship_data3["cargo"].get("inventory", [])
            if i["symbol"] == good
        )
        if have > 0:
            _sell_batch_size  = have
            _sell_remaining   = have
            _sell_total_rev   = 0
            _sell_price       = 0
            _sell_first_price = 0  # price of first successful batch — used as floor
            _sell_failed      = False
            while _sell_remaining > 0:
                _this_sell = min(_sell_batch_size, _sell_remaining)
                try:
                    result = fleet_api.sell_cargo(ship_symbol, good, _this_sell)
                    ag  = result.get("agent", {})
                    tx  = result.get("transaction", {})
                    _sell_price     = tx.get("pricePerUnit", 0)
                    if _sell_first_price == 0:
                        _sell_first_price = _sell_price
                    # Stop selling if market has crashed to <10% of opening price
                    elif _sell_price < _sell_first_price * 0.10:
                        log(f"[yellow]{ship_symbol}: {good} price crashed {_sell_price:,}/u vs opening {_sell_first_price:,}/u — stopping sell, market saturated[/yellow]")
                        _sell_failed = True
                        break
                    _sell_total_rev += tx.get("totalPrice", 0)
                    _sell_remaining -= _this_sell
                    if _sell_remaining > 0:
                        log(
                            f"[magenta]💰 {ship_symbol}: sold {_this_sell}x {good} @ {_sell_price:,}/u "
                            f"| Credits: {ag.get('credits', 0):,}[/magenta]"
                        )
                    db.log_transaction(sell_wp, ship_symbol, good, "SELL",
                                       _this_sell, _sell_price, tx.get("totalPrice", 0))
                except SpaceTradersError as e:
                    err_str = str(e)
                    import re as _re
                    _limit_match = _re.search(r"limit of (\d+) units per transaction", err_str)
                    if _limit_match and _sell_batch_size > 1:
                        _sell_batch_size = int(_limit_match.group(1))
                        log(f"[dim]{ship_symbol}: adjusting sell batch size to {_sell_batch_size}/tx for {good}[/dim]")
                        continue
                    log(f"[yellow]{ship_symbol}: sell {good} failed: {e}[/yellow]")
                    _sell_failed = True
                    sell_junk(ship_symbol, "__NONE__")
                    break
            if not _sell_failed and _sell_total_rev > 0:
                actual_profit = _sell_total_rev - (live_buy * have)
                log(
                    f"[magenta]💰 {ship_symbol}: sold {have}x {good} @ {_sell_price:,}/u "
                    f"= {_sell_total_rev:,} cr (profit: +{actual_profit:,} cr)[/magenta]"
                )

        # ── Backhaul: buy at current location before dead-leg return ─────────
        if not _sell_failed:
            _market_cache_ts.pop(sell_wp, None)
            get_market_prices(sell_wp)
            _bh_opps = [
                o for o in db.get_arbitrage_opportunities(SYSTEM, min_margin=TRADER_MIN_MARGIN)
                if o["buy_at"] == sell_wp
                and o["buy_price"] > 0
                and (o["margin"] / o["buy_price"]) >= TRADER_MIN_ROI
            ]
            if _bh_opps:
                _bh       = _bh_opps[0]
                _bh_me    = agent_api.get_my_agent()
                _bh_sd    = fleet_api.get_ship(ship_symbol)
                _bh_free  = _bh_sd["cargo"]["capacity"] - _bh_sd["cargo"]["units"]
                _bh_aff   = max(0, (_bh_me["credits"] - TRADER_CREDIT_RESERVE) // _bh["buy_price"])
                _bh_qty   = min(_bh_free, _bh_aff)
                if _bh_qty >= 5:
                    log(
                        f"[cyan]🔄 {ship_symbol}: backhaul {_bh['trade_symbol']} "
                        f"{sell_wp}→{_bh['sell_at']} "
                        f"buy {_bh['buy_price']:,}/u sell {_bh['sell_price']:,}/u "
                        f"× {_bh_qty}u = est +{_bh_qty * _bh['margin']:,} cr[/cyan]"
                    )
                    _bh_batch = _bh_qty
                    _bh_bought = 0
                    _bh_remaining = _bh_qty
                    _bh_buy_failed = False
                    while _bh_remaining > 0:
                        _bh_this = min(_bh_batch, _bh_remaining)
                        try:
                            result = fleet_api.purchase_cargo(ship_symbol, _bh["trade_symbol"], _bh_this)
                            ag = result.get("agent", {})
                            tx = result.get("transaction", {})
                            _bh_bought += _bh_this
                            _bh_remaining -= _bh_this
                            log(f"[magenta]🛒 {ship_symbol}: bought {_bh_this}x {_bh['trade_symbol']} @ {_bh['buy_price']:,}/u | Credits: {ag.get('credits', 0):,}[/magenta]")
                            db.log_transaction(sell_wp, ship_symbol, _bh["trade_symbol"], "PURCHASE", _bh_this, _bh["buy_price"], tx.get("totalPrice", 0))
                        except SpaceTradersError as e:
                            import re as _re
                            _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                            if _lm and _bh_batch > 1:
                                _bh_batch = int(_lm.group(1))
                                continue
                            log(f"[yellow]{ship_symbol}: backhaul buy failed: {e}[/yellow]")
                            _bh_buy_failed = True
                            break
                    if not _bh_buy_failed and _bh_bought > 0:
                        navigate_with_refuel(ship_symbol, _bh["sell_at"])
                        ensure_docked(ship_symbol)
                        refuel_if_needed(ship_symbol, threshold=100_000)
                        _bh_inv   = fleet_api.get_ship(ship_symbol)["cargo"].get("inventory", [])
                        _bh_have  = sum(i["units"] for i in _bh_inv if i["symbol"] == _bh["trade_symbol"])
                        if _bh_have > 0:
                            _bh_sb = _bh_have
                            _bh_sr = _bh_have
                            _bh_rev = 0
                            _bh_sp = 0
                            while _bh_sr > 0:
                                _bh_st = min(_bh_sb, _bh_sr)
                                try:
                                    result = fleet_api.sell_cargo(ship_symbol, _bh["trade_symbol"], _bh_st)
                                    tx = result.get("transaction", {})
                                    _bh_sp = tx.get("pricePerUnit", 0)
                                    _bh_rev += tx.get("totalPrice", 0)
                                    _bh_sr -= _bh_st
                                    if _bh_sr > 0:
                                        log(f"[magenta]💰 {ship_symbol}: sold {_bh_st}x {_bh['trade_symbol']} @ {_bh_sp:,}/u[/magenta]")
                                    db.log_transaction(_bh["sell_at"], ship_symbol, _bh["trade_symbol"], "SELL", _bh_st, _bh_sp, tx.get("totalPrice", 0))
                                except SpaceTradersError as e:
                                    import re as _re
                                    _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                                    if _lm and _bh_sb > 1:
                                        _bh_sb = int(_lm.group(1))
                                        continue
                                    log(f"[yellow]{ship_symbol}: backhaul sell failed: {e}[/yellow]")
                                    sell_junk(ship_symbol, "__NONE__")
                                    break
                            if _bh_rev > 0:
                                log(
                                    f"[cyan]💰 {ship_symbol}: backhaul sold {_bh_have}x {_bh['trade_symbol']} "
                                    f"@ {_bh_sp:,}/u = {_bh_rev:,} cr "
                                    f"(profit: +{_bh_rev - _bh['buy_price'] * _bh_have:,} cr)[/cyan]"
                                )


# ── Dedicated explorer loop ───────────────────────────────────────────────────

# Seconds to rest between full system sweeps
EXPLORER_REST_SECS   = 600
# Log when a remote market pays this much more than the home system for any ore
EXPLORER_PRICE_BOOST = 1.30


def explorer_loop(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """
    Explorer thread. Jumps to nearby systems, scans markets, and logs any
    meaningful price-arbitrage opportunities or contract leads back to the
    console. Updates the shared market cache so the hauler can route to better
    sell markets when they exist.
    """
    while not stop_event.is_set():
        try:
            _explorer_loop_inner(ship_symbol, stop_event)
            return  # clean exit
        except Exception as e:
            import traceback
            log(f"[red]💥 {ship_symbol} explorer thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _explorer_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    log(f"[blue]🌌 {ship_symbol} explorer thread started[/blue]")

    # Make sure explorer is in the home system before starting sweeps
    _s = fleet_api.get_ship(ship_symbol)
    if _system_of(_s["nav"]["waypointSymbol"]) != SYSTEM:
        navigate_to(ship_symbol, ASTEROID_BASE)

    _visited: set[str] = set()

    while not stop_event.is_set():
        try:
            gate_wp = _find_jump_gate(SYSTEM)
            if not gate_wp:
                log(f"[yellow]Explorer: no jump gate in {SYSTEM} — sleeping[/yellow]")
                stop_event.wait(EXPLORER_REST_SECS)
                continue

            navigate_to(ship_symbol, gate_wp)
            ensure_orbit(ship_symbol)

            # Scan for nearby systems
            wait_cooldown(ship_symbol)
            try:
                scan_result = fleet_api.scan_systems(ship_symbol)
            except SpaceTradersError as e:
                log(f"[yellow]Explorer: system scan failed: {e}[/yellow]")
                stop_event.wait(EXPLORER_REST_SECS)
                continue

            nearby  = sorted(scan_result.get("systems", []), key=lambda s: s.get("distance", 9999))
            targets = [s for s in nearby if s["symbol"] not in _visited][:5]

            if not targets:
                log(f"[dim]Explorer: all nearby systems visited — resetting, resting {EXPLORER_REST_SECS // 60} min[/dim]")
                _visited.clear()
                stop_event.wait(EXPLORER_REST_SECS)
                continue

            for sys_info in targets:
                if stop_event.is_set():
                    break
                _sys  = sys_info["symbol"]
                _dist = sys_info.get("distance", "?")

                # Find the jump gate in the target system from scan waypoint data
                _target_gate = next(
                    (wp["symbol"] for wp in sys_info.get("waypoints", []) if wp.get("type") == "JUMP_GATE"),
                    None,
                )
                if not _target_gate:
                    log(f"[dim]Explorer: {_sys} has no jump gate — skipping[/dim]")
                    _visited.add(_sys)
                    continue

                log(f"[blue]🌌 Explorer: jumping to {_sys} ({_dist} lu)[/blue]")
                try:
                    ensure_orbit(ship_symbol)
                    fleet_api.jump(ship_symbol, _target_gate)
                    wait_for_ship(ship_symbol)
                    _visited.add(_sys)
                except SpaceTradersError as e:
                    log(f"[yellow]Explorer: jump to {_sys} failed: {e}[/yellow]")
                    continue

                # Scan waypoints in the new system
                try:
                    wait_cooldown(ship_symbol)
                    wp_result = fleet_api.scan_waypoints(ship_symbol)
                    wps       = wp_result.get("waypoints", [])
                    markets   = [
                        w for w in wps
                        if any(t.get("symbol") == "MARKETPLACE" for t in w.get("traits", []))
                    ]
                    log(f"[blue]🌌 {_sys}: {len(wps)} waypoints, {len(markets)} market(s)[/blue]")

                    # Check up to 3 markets for price arbitrage
                    for mwp in markets[:3]:
                        mwp_sym = mwp["symbol"]
                        try:
                            data = universe_api.get_market(_sys, mwp_sym)
                            for g in data.get("tradeGoods", []):
                                gsym      = g.get("symbol", "")
                                sell      = g.get("sellPrice", 0)
                                home_best = max(
                                    (get_market_prices(wp).get(gsym, 0) for wp in (_known_markets or [ASTEROID_BASE])),
                                    default=0,
                                )
                                if home_best > 0 and sell >= home_best * EXPLORER_PRICE_BOOST:
                                    log(
                                        f"[green bold]💹 {gsym}: {sell:,}/u at {mwp_sym} "
                                        f"vs {home_best:,}/u home "
                                        f"(+{(sell / home_best - 1) * 100:.0f}%)[/green bold]"
                                    )
                        except SpaceTradersError:
                            pass
                except SpaceTradersError as e:
                    log(f"[yellow]Explorer: waypoint scan failed in {_sys}: {e}[/yellow]")

                # Jump back to home system
                _home_gate_in_sys = _find_jump_gate(_sys)
                if _home_gate_in_sys and _home_gate_in_sys != fleet_api.get_ship(ship_symbol)["nav"]["waypointSymbol"]:
                    navigate_to(ship_symbol, _home_gate_in_sys)
                try:
                    ensure_orbit(ship_symbol)
                    fleet_api.jump(ship_symbol, gate_wp)
                    wait_for_ship(ship_symbol)
                except SpaceTradersError as e:
                    log(f"[yellow]Explorer: return jump failed: {e} — using navigate_to fallback[/yellow]")
                    navigate_to(ship_symbol, gate_wp)

            log(f"[dim]Explorer: sweep complete, resting {EXPLORER_REST_SECS // 60} min[/dim]")
            stop_event.wait(EXPLORER_REST_SECS)

        except Exception as e:
            import traceback
            log(f"[yellow]Explorer: unexpected error: {e}[/yellow]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(60)

    log(f"[dim]{ship_symbol} explorer thread done[/dim]")


# ── Background fleet manager ─────────────────────────────────────────────────

def _bg_buy_and_launch(
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Buy any affordable ships and immediately launch a miner thread for each.
    Checks ALL shipyards in SHIPYARD_WPS so mining drones / surveyors at
    non-primary yards are discovered and prioritised over haulers / shuttles.
    """
    me = agent_api.get_my_agent()
    credits = me.get("credits", 0)
    if credits < MIN_BUY_CREDITS:
        return

    if not _auto_buy_enabled():
        log("[dim]Fleet manager: auto-purchase disabled in settings — skipping[/dim]")
        return

    try:
        mgr = fleet_api.get_ship(FLEET_MANAGER_SHIP)
    except SpaceTradersError:
        log(f"[yellow]Fleet manager: can't reach {FLEET_MANAGER_SHIP}[/yellow]")
        return

    current_ships     = fleet_api.get_my_ships()
    current_miners    = len([s for s in current_ships if has_mining_mount(s)])
    current_surveyors = len([s for s in current_ships if has_survey_mount(s) and not has_mining_mount(s)])
    current_haulers   = len(_hauler_symbols)

    def _should_buy(stype: str) -> bool:
        if ship_score(stype, current_miners, current_surveyors, current_haulers) < 0:
            return False
        _role_count = {
            "SHIP_ORE_HOUND":       current_miners,
            "SHIP_MINING_DRONE":    current_miners,
            "SHIP_COMMAND_FRIGATE": current_miners,
            "SHIP_SURVEYOR":        current_surveyors,
            "SHIP_LIGHT_HAULER":    current_haulers,
            "SHIP_HEAVY_FREIGHTER": current_haulers,
        }
        targets = _get_ship_targets()
        if targets:
            entry = next((t for t in targets if t["type"] == stype), None)
            if entry is None:
                return False  # not in the configured buy list
            return _role_count.get(stype, 0) < entry["max"]
        # Fallback to hardcoded caps
        if stype in ("SHIP_ORE_HOUND", "SHIP_MINING_DRONE") and current_miners >= 8:
            return False
        if stype == "SHIP_SURVEYOR" and current_surveyors >= 2:
            return False
        if stype in ("SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER") and current_haulers >= 3:
            return False
        if stype == "SHIP_COMMAND_FRIGATE" and _explorer_symbols:
            return False
        return True

    # Collect candidates from ALL shipyards so miners/surveyors at secondary
    # yards aren't missed in favour of shuttles at the primary yard.
    all_candidates: list[tuple[int, int, str, str]] = []  # (score, price, type, yard_wp)
    for yd_wp in SHIPYARD_WPS:
        if contract_done.is_set() or stop_event.is_set():
            break
        if mgr["nav"]["status"] == "IN_TRANSIT" or mgr["nav"]["waypointSymbol"] != yd_wp:
            navigate_to(FLEET_MANAGER_SHIP, yd_wp)
            mgr = fleet_api.get_ship(FLEET_MANAGER_SHIP)
        ensure_docked(FLEET_MANAGER_SHIP)

        shipyard = universe_api.get_shipyard(SYSTEM, yd_wp)
        for s in shipyard.get("ships", []):
            stype = s.get("type", "")
            if not _should_buy(stype):
                continue
            fuel_cap = s.get("frame", {}).get("fuelCapacity", 9999)
            if fuel_cap < MIN_FUEL_CAPACITY:
                log(f"[yellow]Fleet manager: skipping {stype} at {yd_wp} — fuel tank too small ({fuel_cap} < {MIN_FUEL_CAPACITY})[/yellow]")
                continue
            all_candidates.append((
                ship_score(stype, current_miners),
                s["purchasePrice"],
                stype,
                yd_wp,
            ))

    if not all_candidates:
        log("[dim]Fleet manager: no suitable ships at any shipyard[/dim]")
        return

    # Buy highest-scored affordable ships across all yards
    all_candidates.sort(key=lambda x: x[0], reverse=True)
    for _score, price, ship_type, yd_wp in all_candidates:
        if contract_done.is_set() or stop_event.is_set():
            break

        me = agent_api.get_my_agent()
        if me.get("credits", 0) - price < CREDIT_RESERVE:
            log(f"[yellow]Fleet manager: can't afford {ship_type} ({price:,} cr)[/yellow]")
            continue

        # Navigate to the correct shipyard for this purchase
        mgr = fleet_api.get_ship(FLEET_MANAGER_SHIP)
        if mgr["nav"]["waypointSymbol"] != yd_wp:
            navigate_to(FLEET_MANAGER_SHIP, yd_wp)
        ensure_docked(FLEET_MANAGER_SHIP)

        try:
            result     = fleet_api.purchase_ship(ship_type, yd_wp)
            new_ship   = result.get("ship", {})
            tx         = result.get("transaction", {})
            ag         = result.get("agent", {})
            new_symbol = new_ship.get("symbol", "")
            log(f"[green bold]🚀 [BG] Bought {new_symbol} ({ship_type}) for {tx.get('price', 0):,} cr[/green bold]")
            log(f"Credits remaining: [green]{ag.get('credits', 0):,}[/green]")

            # Immediately launch the correct loop for the new ship
            if new_symbol and not contract_done.is_set():
                new_ship_data = fleet_api.get_ship(new_symbol)
                _new_role = new_ship_data["registration"]["role"]
                if ship_type in ("SHIP_SIPHON_DRONE", "SHIP_GAS_DRONE"):
                    _siphoner_symbols.append(new_symbol)
                    loop_target  = siphon_loop
                    thread_label = "siphoner"
                elif has_survey_mount(new_ship_data) and not has_mining_mount(new_ship_data):
                    loop_target  = surveyor_loop
                    thread_label = "surveyor"
                elif _new_role in ("HAULER", "TRANSPORT") or ship_type in ("SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER", "SHIP_LIGHT_SHUTTLE"):
                    # Haulers run trader_loop (arbitrage) between contract deliveries
                    _trader_symbols.append(new_symbol)
                    loop_target  = trader_loop
                    thread_label = "trader"
                elif ship_type == "SHIP_COMMAND_FRIGATE" and current_miners >= 4 and not _explorer_symbols:
                    _explorer_symbols.append(new_symbol)
                    loop_target  = explorer_loop
                    thread_label = "explorer"
                else:
                    loop_target  = miner_loop
                    thread_label = "miner"
                    current_miners += 1  # keep score in sync
                # New miners get the mine-only contract when in direct-buy mode
                _loop_contract = (
                    _mine_only_contract
                    if (_mine_only_contract is not None and loop_target == miner_loop)
                    else contract
                )
                t = threading.Thread(
                    target=loop_target,
                    args=(new_symbol, _loop_contract, contract_done, stop_event),
                    daemon=True,
                    name=f"{thread_label}-{new_symbol}",
                )
                t.start()
                log(f"[cyan]⚓ Fleet manager launched {thread_label} thread: {new_symbol}[/cyan]")
        except SpaceTradersError as e:
            log(f"[yellow]Fleet manager: purchase failed ({ship_type}): {e}[/yellow]")


def _bg_negotiate_contract() -> None:
    """
    Proactively negotiate a new contract using FLEET_MANAGER_SHIP so one is
    ready the moment the current contract completes. Only runs at most once per
    10 minutes and only when fewer than 2 unfulfilled contracts already exist.
    The ship navigates to the faction HQ (X1-GK27-A1), negotiates in orbit, then
    the next fleet-manager cycle will re-route it back to the shipyard as needed.
    """
    global _last_negotiation
    if time.time() - _last_negotiation < 600:  # at most once per 10 min
        return

    # Skip if we already have a queued contract waiting
    try:
        cs = contracts_api.get_contracts()
        if len([c for c in cs if not c.get("fulfilled")]) >= 2:
            _last_negotiation = time.time()
            return
    except SpaceTradersError:
        return

    try:
        navigate_to(FLEET_MANAGER_SHIP, _FACTION_HQ_WP)
        ensure_docked(FLEET_MANAGER_SHIP)
        result = fleet_api.negotiate_contract(FLEET_MANAGER_SHIP)
        new_c  = result.get("contract", {})
        payout = new_c.get("terms", {}).get("payment", {}).get("onFulfilled", 0)
        log(f"[green]📋 Pre-negotiated contract: {new_c.get('id')} (+{payout:,} cr on fulfill)[/green]")
    except SpaceTradersError as e:
        log(f"[dim]Fleet manager: contract negotiation skipped: {e}[/dim]")
    finally:
        _last_negotiation = time.time()


def fleet_manager_loop(
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Daemon thread: every 2 min, buy affordable ships and spin up miners immediately.
    Between ship-buying checks also proactively negotiates the next contract so
    it is ready the moment the current one completes.
    """
    log("[dim]⚙  Fleet manager thread started[/dim]")
    CHECK_INTERVAL = 120  # seconds

    while not stop_event.wait(CHECK_INTERVAL):
        if contract_done.is_set() or stop_event.is_set():
            break
        if not _manager_lock.acquire(blocking=False):
            continue  # another management op in progress — skip this tick
        try:
            _bg_buy_and_launch(contract, contract_done, stop_event)
            if not contract_done.is_set() and not stop_event.is_set():
                _bg_negotiate_contract()
        except Exception as e:
            log(f"[yellow]Fleet manager error: {e}[/yellow]")
        finally:
            _manager_lock.release()

    log("[dim]⚙  Fleet manager thread stopped[/dim]")


# ── Contract orchestration (concurrent) ──────────────────────────────────────

def work_contract(contract: dict) -> None:
    """Accept (if needed) then run all miners in parallel until contract is fulfilled."""
    cid     = contract["id"]
    payment = contract["terms"]["payment"]

    console.print(Panel(
        f"[bold]Contract:[/bold] {cid}\n"
        f"[bold]Type:[/bold] {contract['type']}\n"
        f"[bold]Payment:[/bold] {payment['onAccepted']:,} + {payment['onFulfilled']:,} cr\n"
        f"[bold]Accepted:[/bold] {contract.get('accepted')}",
        title="Working Contract",
        border_style="yellow",
    ))
    for d in contract["terms"].get("deliver", []):
        log(f"  Deliver: {d['unitsRequired']}x {d['tradeSymbol']} → {d['destinationSymbol']} ({d['unitsFulfilled']} done)")

    if not contract.get("accepted"):
        log("[bold]Accepting contract[/bold]")
        result = contracts_api.accept_contract(cid)
        ag = result.get("agent", {})
        log(f"[green]✓ Accepted! Credits: {ag.get('credits', 0):,}[/green]")
        contract["accepted"] = True

    # Check if already complete (restart edge case)
    contract_done = threading.Event()
    for d in contract["terms"].get("deliver", []):
        if d["unitsFulfilled"] >= d["unitsRequired"]:
            contract_done.set()
            break

    if contract_done.is_set():
        log("[dim]Contract already fulfilled — skipping[/dim]")
        return

    all_fleet = fleet_api.get_my_ships()
    miners    = [s["symbol"] for s in all_fleet if has_mining_mount(s)]
    surveyors = [
        s["symbol"] for s in all_fleet
        if has_survey_mount(s) and not has_mining_mount(s) and s["symbol"] != FLEET_MANAGER_SHIP
    ]
    haulers = [
        s["symbol"] for s in all_fleet
        if s["registration"]["role"] in ("HAULER", "TRANSPORT") and s["symbol"] != FLEET_MANAGER_SHIP
    ]
    siphoners = [
        s["symbol"] for s in all_fleet
        if s["frame"].get("symbol", "") in ("FRAME_DRONE",)
        and any(m.get("symbol", "").startswith("MOUNT_GAS_SIPHON") for m in s.get("mounts", []))
        and s["symbol"] not in miners
        and s["symbol"] != FLEET_MANAGER_SHIP
    ]
    # Optionally run the command ship as a hauler if the user set command_ship_role=hauler
    if db.get_bot_setting("command_ship_role", "idle") == "hauler" and FLEET_MANAGER_SHIP not in haulers:
        haulers.append(FLEET_MANAGER_SHIP)
    miners = miners or [COMMAND_SHIP]

    # ── Direct-buy contract: only one ship buys/delivers; others mine for income ─
    global _mine_only_contract
    _contract_good = contract["terms"]["deliver"][0]["tradeSymbol"] if contract["terms"].get("deliver") else ""
    if _contract_good and best_buy_waypoint(_contract_good):
        # Build a fake contract that will never match any cargo so miners just
        # mine and sell everything as junk for credits.
        _mine_only_contract = {
            "id": contract["id"],
            "type": contract["type"],
            "accepted": True,
            "fulfilled": False,
            "terms": {
                "deliver": [{"tradeSymbol": "__SELL_ONLY__", "destinationSymbol": ASTEROID_BASE,
                             "unitsRequired": 99999, "unitsFulfilled": 0}],
                "payment": contract["terms"]["payment"],
            },
        }
        log(f"[cyan]Direct-buy contract: {miners[0]} handles buy/deliver; "
            f"{len(miners)-1} miner(s) mine for income[/cyan]")
    else:
        _mine_only_contract = None  # all miners work the real contract

    log(f"[bold]Launching {len(miners)} miner thread(s): {miners}[/bold]")
    if surveyors:
        log(f"[magenta]Launching {len(surveyors)} surveyor thread(s): {surveyors}[/magenta]")
    if haulers:
        if _mine_only_contract is not None:
            # Direct-buy contract — haulers have nothing to pick up from miners;
            # run them as arbitrage traders instead.
            _trader_symbols.extend(haulers)
            log(f"[blue]Launching {len(haulers)} hauler(s) as trader(s) (direct-buy contract): {haulers}[/blue]")
        else:
            _hauler_symbols.clear()
            _hauler_symbols.extend(haulers)
            log(f"[blue]Launching {len(haulers)} hauler thread(s): {haulers}[/blue]")
    if siphoners:
        _siphoner_symbols.clear()
        _siphoner_symbols.extend(siphoners)
        log(f"[blue]Launching {len(siphoners)} siphoner thread(s): {siphoners}[/blue]")

    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=miner_loop,
            # First miner works the real contract; extras mine for income
            args=(miner, contract if (i == 0 or _mine_only_contract is None) else _mine_only_contract,
                  contract_done, stop_event),
            daemon=True,
            name=f"miner-{miner}",
        )
        for i, miner in enumerate(miners)
    ]
    threads += [
        threading.Thread(
            target=surveyor_loop,
            args=(surveyor, contract, contract_done, stop_event),
            daemon=True,
            name=f"surveyor-{surveyor}",
        )
        for surveyor in surveyors
    ]
    threads += [
        threading.Thread(
            target=trader_loop if _mine_only_contract is not None else hauler_loop,
            args=(hauler, contract, contract_done, stop_event),
            daemon=True,
            name=f"hauler-{hauler}",
        )
        for hauler in haulers
    ]
    threads += [
        threading.Thread(
            target=siphon_loop,
            args=(siphoner, contract, contract_done, stop_event),
            daemon=True,
            name=f"siphoner-{siphoner}",
        )
        for siphoner in siphoners
    ]
    for t in threads:
        t.start()

    # Background fleet manager: buys ships and spins up new miners mid-contract
    mgr_thread = threading.Thread(
        target=fleet_manager_loop,
        args=(contract, contract_done, stop_event),
        daemon=True,
        name="fleet-manager",
    )
    mgr_thread.start()

    while not contract_done.is_set():
        worker_threads = [t for t in threads if "miner" in t.name or "hauler" in t.name]
        if worker_threads and all(not t.is_alive() for t in worker_threads):
            log("[yellow]All worker threads exited without fulfilling contract[/yellow]")
            break
        contract_done.wait(timeout=15)
    stop_event.set()       # tell remaining miners and fleet manager to wind down
    for t in threads:
        t.join(timeout=120)
    mgr_thread.join(timeout=30)

    log("[green bold]All miners done.[/green bold]")


# ── Ship buying ───────────────────────────────────────────────────────────────

def ship_score(
    ship_type: str,
    current_miner_count: int,
    current_surveyor_count: int = 0,
    current_hauler_count: int = 0,
) -> int:
    """
    Score a ship type for purchase priority.  Higher = more desirable to buy.
    -1 means skip entirely.

    Dynamic adjustments based on current fleet composition:
    - Haulers: blocked until 2+ miners; priority rises with miner count so that
      once 3+ miners are wasting time on delivery runs a hauler jumps above extra miners.
    - Surveyors: blocked until 2+ miners (need miners to share the survey pool);
      capped at 1 surveyor.
    - Miners: slight score reduction above 5 (diminishing returns).
    """
    base = SHIP_SCORES.get(ship_type, 30)  # unknown types get a mediocre score
    if base < 0:
        return -1

    _hauler_types = ("SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER", "SHIP_LIGHT_SHUTTLE")

    if ship_type in _hauler_types:
        if current_miner_count < 2 or current_hauler_count >= 3:
            return -1
        # With 3+ miners each delivery run wastes mining time — hauler frees that up.
        # Boost score above extra miners once we reach that threshold.
        if current_miner_count >= 3:
            return base + (current_miner_count - 2) * 15
        return base

    if ship_type == "SHIP_SURVEYOR":
        if current_miner_count < 2 or current_surveyor_count >= 2:
            return -1  # not enough miners to share surveys / already have two
        return base

    if ship_type in ("SHIP_ORE_HOUND", "SHIP_MINING_DRONE"):
        if current_miner_count >= 8:
            return max(base - 30, 10)  # diminishing returns above 8 miners
        return base

    # Cap siphon/gas drones at 3 total; require at least 2 miners first
    if ship_type in ("SHIP_SIPHON_DRONE", "SHIP_GAS_DRONE"):
        if current_miner_count < 2:
            return -1
        if len(_siphoner_symbols) >= 3:
            return -1
        return base

    return base


def buy_ships() -> int:
    """
    Navigate command ship to each known shipyard, show available ships, and buy
    as many useful ships as credits allow (keeping CREDIT_RESERVE).
    Only runs when credits >= MIN_BUY_CREDITS and AUTO_BUY_SHIPS=True.
    Returns the number of ships purchased.
    """
    me = agent_api.get_my_agent()
    credits = me.get("credits", 0)
    if credits < MIN_BUY_CREDITS:
        log(f"[yellow]Skipping fleet expansion — saving up ({credits:,} / {MIN_BUY_CREDITS:,} cr)[/yellow]")
        return 0

    log("[bold]Building fleet[/bold]")
    if not _auto_buy_enabled():
        log("[yellow]Fleet purchasing disabled in settings — skipping[/yellow]")
        return 0
    log(f"Credits available: [green]{credits:,}[/green] | Reserve: {CREDIT_RESERVE:,}")

    current_miners    = len(get_mining_ships())
    current_ships     = fleet_api.get_my_ships()
    current_surveyors = len([s for s in current_ships if has_survey_mount(s) and not has_mining_mount(s)])
    current_haulers   = len([s for s in current_ships
                             if s["registration"]["role"] in ("HAULER", "TRANSPORT")
                             and s["symbol"] != FLEET_MANAGER_SHIP])
    purchases = 0

    def _eligible(stype: str) -> bool:
        if ship_score(stype, current_miners, current_surveyors, current_haulers) < 0:
            return False
        _role_count = {
            "SHIP_ORE_HOUND":       current_miners,
            "SHIP_MINING_DRONE":    current_miners,
            "SHIP_COMMAND_FRIGATE": current_miners,
            "SHIP_SURVEYOR":        current_surveyors,
            "SHIP_LIGHT_HAULER":    current_haulers,
            "SHIP_HEAVY_FREIGHTER": current_haulers,
        }
        targets = _get_ship_targets()
        if targets:
            entry = next((t for t in targets if t["type"] == stype), None)
            if entry is None:
                return False  # not in the configured buy list
            return _role_count.get(stype, 0) < entry["max"]
        # Fallback to hardcoded caps
        if stype in ("SHIP_ORE_HOUND", "SHIP_MINING_DRONE") and current_miners >= 8:
            return False
        if stype == "SHIP_SURVEYOR" and current_surveyors >= 2:
            return False
        if stype in ("SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER", "SHIP_LIGHT_SHUTTLE") and current_haulers >= 4:
            return False
        if stype == "SHIP_COMMAND_FRIGATE" and _explorer_symbols:
            return False
        return True

    for shipyard_wp in SHIPYARD_WPS:
        navigate_to(FLEET_MANAGER_SHIP, shipyard_wp)
        ensure_docked(FLEET_MANAGER_SHIP)

        shipyard = universe_api.get_shipyard(SYSTEM, shipyard_wp)
        ships_for_sale = shipyard.get("ships", [])

        if not ships_for_sale:
            log(f"[yellow]No ship listings at {shipyard_wp} (need a ship physically present)[/yellow]")
            continue

        me = agent_api.get_my_agent()
        credits = me.get("credits", 0)

        # Display what's available
        t = Table(title=f"Shipyard — {shipyard_wp}", box=box.SIMPLE_HEAVY)
        t.add_column("Type", style="bold")
        t.add_column("Price", justify="right")
        t.add_column("Supply")
        t.add_column("Priority", justify="right")
        for s in ships_for_sale:
            sc = ship_score(s.get("type", ""), current_miners, current_surveyors, current_haulers)
            price = s.get("purchasePrice", 0)
            can_afford = "[green]✓[/green]" if credits - price >= CREDIT_RESERVE else "[red]✗[/red]"
            priority = str(sc) if sc >= 0 else "[dim]skip[/dim]"
            t.add_row(s.get("type", "?"), f"{can_afford} {price:,} cr", s.get("supply", "?"), priority)
        console.print(t)

        # Buy greedily in priority order, skipping anything we can't afford
        buyable = sorted(
            [s for s in ships_for_sale if _eligible(s.get("type", ""))],
            key=lambda s: ship_score(s.get("type", ""), current_miners, current_surveyors, current_haulers),
            reverse=True,
        )

        for ship_info in buyable:
            ship_type = ship_info["type"]
            price     = ship_info["purchasePrice"]

            me = agent_api.get_my_agent()
            credits = me.get("credits", 0)

            if credits - price < CREDIT_RESERVE:
                log(f"[yellow]Skipping {ship_type} ({price:,} cr) — would breach {CREDIT_RESERVE:,} reserve[/yellow]")
                continue

            try:
                result   = fleet_api.purchase_ship(ship_type, shipyard_wp)
                new_ship = result.get("ship", {})
                tx       = result.get("transaction", {})
                ag       = result.get("agent", {})
                log(f"[green bold]🚀 Bought {new_ship.get('symbol')} ({ship_type}) for {tx.get('price', 0):,} cr![/green bold]")
                log(f"Credits remaining: [green]{ag.get('credits', 0):,}[/green]")
                current_miners = len(get_mining_ships())  # refresh after purchase
                purchases += 1
            except SpaceTradersError as e:
                log(f"[red]Purchase failed for {ship_type}: {e}[/red]")

    if purchases == 0:
        log("[yellow]No ships purchased this round[/yellow]")
    else:
        fleet_size = len(fleet_api.get_my_ships())
        log(f"[green bold]Purchased {purchases} ship(s)! Fleet now: {fleet_size} ships[/green bold]")

    return purchases


# ── Contract sourcing ─────────────────────────────────────────────────────────

def _contract_good(c: dict) -> str:
    """Return the first delivery good symbol for a contract, or ''."""
    return (c.get("terms", {}).get("deliver") or [{}])[0].get("tradeSymbol", "")


def _contract_payout(c: dict) -> int:
    """Return the onFulfilled payment for a contract (0 if unknown)."""
    return c.get("terms", {}).get("payment", {}).get("onFulfilled", 0)


def _is_buyable_contract(c: dict) -> bool:
    """True if the contract requires purchasing a manufactured good (not mineable).

    Mineable goods (ores, ice, crystals) are NOT considered "buyable" even if
    a market exports them — we mine those. Exception: if the market price is at
    or below CHEAP_BUY_THRESHOLD the bot will buy them anyway (trivial cost).
    """
    good = _contract_good(c)
    if not good:
        return False
    if good in MINEABLE_GOODS:
        # Only flag as "buyable" if market price is trivially cheap
        buy_wp = best_buy_waypoint(good)
        if not buy_wp:
            return False
        price = _market_cache.get(buy_wp, {}).get(f"_buy_{good}", 0)
        return price > 0 and price <= CHEAP_BUY_THRESHOLD
    # Non-mineable manufactured good with a buy source → must be purchased
    return bool(best_buy_waypoint(good))


def _contract_score(c: dict) -> float:
    """Weighted efficiency score for choosing between contracts.

    Factors (all affect a base payout-per-unit-remaining value):
    - Payout per remaining unit  — favours small high-value contracts over large cheap ones
    - +20% resource match bonus  — any scored asteroid has the right deposit trait
    - -15% non-mineable penalty  — purchased goods carry overhead vs free mining
    - Delivery distance penalty  — far delivery WP costs more fuel/time per cycle

    Used only when selecting UNACCEPTED contracts; accepted contracts are always resumed.
    """
    payout = _contract_payout(c)
    d = (c.get("terms", {}).get("deliver") or [{}])[0]
    good            = d.get("tradeSymbol", "")
    units_remaining = max(d.get("unitsRequired", 1) - d.get("unitsFulfilled", 0), 1)
    delivery_wp     = d.get("destinationSymbol", ASTEROID_BASE)

    payout_per_unit = float(payout) / units_remaining

    # Resource match bonus: any known asteroid has the deposit trait for this good
    _populate_asteroid_cache()
    deposit_traits = GOOD_TO_DEPOSIT_TRAITS.get(good, frozenset())
    has_match = bool(deposit_traits) and any(
        deposit_traits & ast["traits"] for ast in _scored_asteroids
    )
    if has_match:
        payout_per_unit *= 1.20

    # Non-mineable goods must be purchased — lower effective margin
    if good not in MINEABLE_GOODS:
        payout_per_unit *= 0.85

    # Delivery distance penalty: far WP = more fuel + time per run
    try:
        ax, ay = _get_coords(ASTEROID)
        dx, dy = _get_coords(delivery_wp)
        dist_delivery = ((ax - dx) ** 2 + (ay - dy) ** 2) ** 0.5
        payout_per_unit -= dist_delivery * 0.3
    except Exception:
        pass

    return payout_per_unit


def _log_contract_scores(contracts: list[dict]) -> None:
    """Log a scoring breakdown table for a list of unaccepted contracts."""
    if len(contracts) < 2:
        return  # nothing to compare
    rows = []
    for c in contracts:
        d = (c.get("terms", {}).get("deliver") or [{}])[0]
        good     = d.get("tradeSymbol", "?")
        req      = d.get("unitsRequired", 0)
        done     = d.get("unitsFulfilled", 0)
        payout   = _contract_payout(c)
        score    = _contract_score(c)
        deposit_traits = GOOD_TO_DEPOSIT_TRAITS.get(good, frozenset())
        match_str = "[green]✓[/green]" if bool(deposit_traits) and any(
            deposit_traits & ast["traits"] for ast in _scored_asteroids
        ) else "–"
        rows.append((score, good, req, done, payout, match_str))
    rows.sort(reverse=True)
    log("[dim]Contract scoring (best efficiency first):[/dim]")
    for score, good, req, done, payout, match in rows:
        log(f"[dim]  {good} ×{req} ({done} done) | {payout:,} cr | score={score:.0f} | deposit={match}[/dim]")


def get_next_contract() -> dict | None:
    """Return an unfulfilled contract, prioritising by weighted efficiency score.

    Priority order:
      1. Already-accepted contracts — committed, must finish them.
      2. Unaccepted high-value contracts (onFulfilled >= MIN_CONTRACT_PAYOUT),
         ranked by _contract_score (payout/unit, resource match, delivery distance).
      3. Negotiate a fresh contract hoping for a high-value one.
      4. Fall back to the best-scoring unaccepted contract available.
         If still nothing, accept whatever was negotiated.
    """
    cs = contracts_api.get_contracts()
    for _c in cs:
        db.upsert_contract(_c)
    now = time.time()
    pending = [
        c for c in cs
        if not c.get("fulfilled")
        and c.get("type") == "PROCUREMENT"
        and now >= _contract_retry_after.get(c.get("id", ""), 0)
    ]

    # 1. Already-accepted: we're committed — pick the best of those.
    accepted = [c for c in pending if c.get("accepted")]
    if accepted:
        chosen = max(accepted, key=_contract_payout)
        if _contract_payout(chosen) < MIN_CONTRACT_PAYOUT:
            log(f"[yellow]⚠ Resuming accepted contract {chosen['id']} (payout {_contract_payout(chosen):,} cr) — no choice[/yellow]")
        return chosen

    # 2. Unaccepted high-value contracts available right now.
    unaccepted = [c for c in pending if not c.get("accepted")]
    good_unaccepted = [c for c in unaccepted if _contract_payout(c) >= MIN_CONTRACT_PAYOUT]
    if good_unaccepted:
        _log_contract_scores(good_unaccepted)
        chosen = max(good_unaccepted, key=_contract_score)
        payout = _contract_payout(chosen)
        good   = _contract_good(chosen)
        score  = _contract_score(chosen)
        log(f"[green]Contract selected: {good} (+{payout:,} cr | score={score:.0f})[/green]")
        return chosen

    # 3. No high-value contracts on hand — try negotiating a fresh one.
    if unaccepted:
        best_available = max(unaccepted, key=_contract_score)
        log(f"[yellow]Best available contract only pays {_contract_payout(best_available):,} cr — trying to negotiate a better one...[/yellow]")
    else:
        best_available = None
        log("[yellow]No pending contracts — negotiating a new one...[/yellow]")

    try:
        navigate_to(COMMAND_SHIP, _FACTION_HQ_WP)
        ensure_docked(COMMAND_SHIP)
        result = fleet_api.negotiate_contract(COMMAND_SHIP)
        new_contract = result.get("contract", {})
        payout = _contract_payout(new_contract)
        good = _contract_good(new_contract)
        if payout >= MIN_CONTRACT_PAYOUT:
            log(f"[green]📋 Great contract negotiated: {good} (+{payout:,} cr on fulfill)[/green]")
        else:
            log(f"[yellow]📋 Negotiated contract: {good} (+{payout:,} cr on fulfill)[/yellow]")
        return new_contract
    except SpaceTradersError as e:
        log(f"[red]Could not negotiate contract: {e}[/red]")
        # If the error is "already has an active contract", find it regardless of type
        if "4511" in str(e) or "already has an active contract" in str(e).lower():
            _all = contracts_api.get_contracts()
            for _c in _all:
                db.upsert_contract(_c)
            _any_accepted = [
                c for c in _all
                if c.get("accepted") and not c.get("fulfilled")
            ]
            if _any_accepted:
                _found = max(_any_accepted, key=_contract_payout)
                log(f"[yellow]Resuming existing active contract: {_contract_good(_found)} type={_found.get('type')} (+{_contract_payout(_found):,} cr)[/yellow]")
                return _found

    # 4. Fall back to best unaccepted contract we already have.
    if best_available:
        payout = _contract_payout(best_available)
        log(f"[yellow]Falling back to best available contract: {_contract_good(best_available)} (+{payout:,} cr)[/yellow]")
        return best_available
    return None


# ── Status display ────────────────────────────────────────────────────────────

def step_show_status() -> None:
    me    = agent_api.get_my_agent()
    ships = fleet_api.get_my_ships()

    t = Table(title="Fleet Status", box=box.SIMPLE_HEAVY)
    t.add_column("Symbol", style="bold")
    t.add_column("Role")
    t.add_column("Mining", justify="center")
    t.add_column("Status")
    t.add_column("Location")
    t.add_column("Fuel", justify="right")
    t.add_column("Cargo", justify="right")
    t.add_column("Condition", justify="right")
    t.add_column("Crew / Morale", justify="right")
    for s in ships:
        nav   = s["nav"]
        fuel  = s["fuel"]
        cargo = s["cargo"]
        crew  = s.get("crew", {})
        mining = "[green]✓[/green]" if has_mining_mount(s) else "[dim]–[/dim]"

        # Worst condition across frame / engine / reactor
        cond = min(_condition(s.get(c, {})) for c in ("frame", "engine", "reactor"))
        if cond < REPAIR_THRESHOLD:
            cond_str = f"[red]{cond:.0%}[/red]"
        elif cond < 0.95:
            cond_str = f"[yellow]{cond:.0%}[/yellow]"
        else:
            cond_str = f"[green]{cond:.0%}[/green]"

        # Crew morale (0–100 in SpaceTraders API)
        morale   = crew.get("morale", 100)
        current  = crew.get("current", 0)
        required = crew.get("required", 0)
        wages    = crew.get("wages", 0)
        if current < required:
            crew_str = f"[red]{current}/{required} UNDER[/red]"
        elif morale < 30:
            crew_str = f"[red]{morale}/100 😠[/red]"
        elif morale < 60:
            crew_str = f"[yellow]{morale}/100 😐[/yellow]"
        else:
            crew_str = f"[green]{morale}/100 😊[/green]"
        if wages:
            crew_str += f" ({wages}cr/h)"

        t.add_row(
            s["symbol"],
            s["registration"]["role"],
            mining,
            nav["status"],
            nav["waypointSymbol"],
            f"{fuel.get('current', 0)}/{fuel.get('capacity', 0)}",
            f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}",
            cond_str,
            crew_str,
        )
    console.print(t)
    log(f"Agent: [bold]{me['symbol']}[/bold] | Credits: [green]{me['credits']:,}[/green] | Ships: {me['shipCount']}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    console.print(Panel(
        "[bold cyan]SpaceTraders Automation[/bold cyan]\n"
        "Goal: Complete contracts → buy ships → upgrade → repeat indefinitely",
        border_style="cyan",
    ))

    # Auto-configure system/ship/asteroid constants (also calls db.init_db() internally)
    auto_configure()

    # Warm-start caches from previous run
    _km, _ge, _gb, _mc, _mts = db.load_market_caches(SYSTEM, MARKET_CACHE_TTL)
    if _km:
        _known_markets[:] = _km
        _good_exporters.update(_ge)
        _good_buyers.update(_gb)
        _market_cache.update(_mc)
        _market_cache_ts.update(_mts)
        log(f"[dim]Warm-loaded {len(_km)} markets, {len(_ge)} goods from DB[/dim]")
    with _surveys_lock:
        _loaded_surveys = db.load_active_surveys()
        if _loaded_surveys:
            _shared_surveys.extend(_loaded_surveys)
            log(f"[dim]Warm-loaded {len(_loaded_surveys)} active survey(s) from DB[/dim]")

    # Discover all markets in the system on startup
    discover_markets()
    scan_good_sources()

    loop = 0
    while True:
        loop += 1
        me = agent_api.get_my_agent()
        log(f"[bold]─── Loop {loop} | Credits: {me['credits']:,} | Ships: {me['shipCount']} ───[/bold]")

        contract = get_next_contract()
        if not contract:
            log("[red]No contract available. Retrying in 60s...[/red]")
            time.sleep(60)
            continue

        work_contract(contract)

        # If miners gave up (contract unworkable), add cooldown before retry
        try:
            _fresh = contracts_api.get_contract(contract["id"])
            if not _fresh.get("fulfilled"):
                _delivered = sum(d.get("unitsFulfilled", 0) for d in _fresh.get("terms", {}).get("deliver", []))
                _good = _contract_good(contract)
                _unbuyable = not bool(best_buy_waypoint(_good)) if _good else True
                if _delivered == 0 and _unbuyable:
                    _contract_retry_after[contract["id"]] = time.time() + 3600  # 1-hour backoff
                    log("[yellow]Contract impossible (good unbuyable, 0 delivered) — backing off 1h[/yellow]")
                else:
                    _contract_retry_after[contract["id"]] = time.time() + 900  # 15-min cooldown
                    log("[yellow]Contract unfulfilled after workers exited — will retry in 15 min[/yellow]")
        except SpaceTradersError:
            pass

        # Post-contract: expand → maintain → upgrade
        # (contract negotiation is handled by get_next_contract() at the start
        #  of the next loop so the MIN_CONTRACT_PAYOUT filter can apply cleanly)
        buy_ships()
        step_maintain_fleet()
        step_upgrade_fleet()

        step_show_status()
        log("[green]Loop complete — starting next contract.[/green]")
        time.sleep(2)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        console.print("\n[dim]Automation stopped.[/dim]")
    except SpaceTradersError as e:
        console.print(f"\n[bold red]Fatal error: {e}[/bold red]")
        raise

