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

load_dotenv()
console = Console()

# ── Strategy file (shared with MCP server) ───────────────────────────────────
STRATEGY_FILE = Path(__file__).parent / "strategy.json"


def load_strategy() -> dict:
    """Read strategy.json or return defaults. Always safe to call."""
    try:
        return json.loads(STRATEGY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"mode": "contract_grind", "notes": "", "target_contract_id": None}


# ── Market price cache (populated at runtime) ─────────────────────────────────
_market_cache: dict[str, dict[str, int]] = {}   # {wp: {good: sell_price}}
_market_cache_ts: dict[str, float] = {}          # {wp: unix_timestamp}
_known_markets: list[str] = []                   # grows after discover_markets()

_fulfill_lock = threading.Lock()                  # prevents double-fulfillment
_manager_lock = threading.Lock()                  # serialises background fleet-management ops
_last_negotiation: float = 0.0                    # unix timestamp of last proactive contract negotiation

# ── Shared survey pool (surveyor ships write, miners read) ────────────────────
_shared_surveys: list[dict] = []
_surveys_lock   = threading.Lock()

# ── Config ────────────────────────────────────────────────────────────────────
SYSTEM         = "X1-HU91"
COMMAND_SHIP   = "TYLERMASTERY-1"
ASTEROID       = "X1-HU91-FD5D"    # Engineered asteroid, center cluster — COMMON_METAL_DEPOSITS + FUEL
ASTEROID_BASE  = "X1-HU91-H52"     # Moon at center cluster — MARKETPLACE + SHIPYARD (38 units from FD5D)
SHIPYARD_WP    = "X1-HU91-H52"     # Primary shipyard (Moon) — used for repairs/upgrades
SHIPYARD_WPS   = ["X1-HU91-H52", "X1-HU91-A2"]  # All shipyards checked when buying ships
CREDIT_RESERVE = 30_000            # Minimum credits to keep in reserve
MIN_BUY_CREDITS = 120_000          # Don't buy ships until we can afford the light shuttle
FLEET_MANAGER_SHIP = "TYLERMASTERY-2"  # Idle non-miner ship kept at SHIPYARD_WP for background ops
MIN_FUEL_CAPACITY  = 200           # Skip ships whose fuel tank can't cover B7↔B8↔H51 routes

# Ship purchase priority for mining contracts (higher score = buy first)
# -1 means never buy
SHIP_SCORES = {
    "SHIP_ORE_HOUND":       100,  # Best miner: powerful mounts + large cargo
    "SHIP_MINING_DRONE":    90,   # Decent miner — only buy if fuel tank >= MIN_FUEL_CAPACITY
    "SHIP_SURVEYOR":        75,   # No mining laser but fills survey pool — worth buying
    "SHIP_HEAVY_FREIGHTER": 65,   # Good once fleet is large
    "SHIP_LIGHT_HAULER":    60,   # Useful once we have 2+ miners
    "SHIP_LIGHT_SHUTTLE":   55,   # Dedicated delivery runner (A2 shipyard, 86k cr)
    "SHIP_COMMAND_FRIGATE": 50,   # Versatile but expensive
    "SHIP_PROBE":           -1,   # Useless for contracts – never buy
}

MIN_SELL_PRICE     = 30     # cr/unit — jettison anything below this instead of hauling to market
REPAIR_THRESHOLD   = 0.80   # Repair when any component drops below 80% condition
MARKET_CACHE_TTL   = 600    # Seconds to keep market price data fresh
MINING_MOUNT_TIERS = [      # Mining laser upgrades, weakest → strongest
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]

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
    if ship["nav"]["waypointSymbol"] == destination:
        if ship["nav"]["status"] == "IN_TRANSIT":
            # Already en route (e.g., drifting from a previous rescue) — wait for arrival
            log(f"[dim]{ship_symbol}: already en route to {destination}, waiting...[/dim]")
            wait_for_ship(ship_symbol)
            # Restore CRUISE mode in case ship was left in DRIFT
            _fm = fleet_api.get_ship(ship_symbol)["nav"].get("flightMode", "CRUISE")
            if _fm != "CRUISE":
                fleet_api.patch_nav(ship_symbol, "CRUISE")
                log(f"[dim]{ship_symbol}: restored CRUISE mode after drift arrival[/dim]")
        else:
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
        if e.code == 4203:  # insufficient fuel — emergency DRIFT to destination
            log(f"[red]⚠ {ship_symbol}: insufficient fuel, emergency DRIFT to {destination}[/red]")
            fleet_api.patch_nav(ship_symbol, "DRIFT")
            fleet_api.navigate(ship_symbol, destination)
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

        if cur_fuel >= dist_to_dest:
            navigate_to(ship_symbol, destination)
            return

        # Find the best reachable market: within fuel_cap of current position
        # and closer to the destination than we currently are.
        markets = _known_markets or [ASTEROID_BASE]
        best_wp: str | None = None
        best_remaining = dist_to_dest  # must beat our current distance

        for wp in markets:
            if wp == cur_wp:
                continue
            wx, wy = _get_coords(wp)
            hop_dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
            if hop_dist > fuel_cap:
                continue  # unreachable even at full tank
            remaining = ((dx - wx) ** 2 + (dy - wy) ** 2) ** 0.5
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
        best_price = max(
            (get_market_prices(wp).get(item["symbol"], 0) for wp in (_known_markets or [ASTEROID_BASE])),
            default=0,
        )
        if best_price < MIN_SELL_PRICE:
            try:
                fleet_api.jettison(ship_symbol, item["symbol"], item["units"])
                log(f"[dim]Jettisoned {item['units']}x {item['symbol']} ({best_price} cr/u < threshold)[/dim]")
            except SpaceTradersError:
                worth_selling.append(item)  # keep if jettison fails
        else:
            worth_selling.append(item)

    if not worth_selling:
        return

    target_wp = best_sell_market_for_cargo(worth_selling)
    current_wp = ship["nav"]["waypointSymbol"]
    if current_wp != target_wp or ship["nav"]["status"] != "DOCKED":
        navigate_to(ship_symbol, target_wp)
        ensure_docked(ship_symbol)

    # Re-fetch cargo after possible travel
    ship = fleet_api.get_ship(ship_symbol)
    for item in ship["cargo"].get("inventory", []):
        if keep_good and item["symbol"] == keep_good:
            continue
        try:
            result = fleet_api.sell_cargo(ship_symbol, item["symbol"], item["units"])
            tx = result.get("transaction", {})
            ppu = tx.get("pricePerUnit", 0)
            log(f"[green]💰 Sold {tx.get('units')}x {tx.get('tradeSymbol')} @ {ppu:,}/u = {tx.get('totalPrice', 0):,} cr[/green]")
        except SpaceTradersError:
            try:
                fleet_api.jettison(ship_symbol, item["symbol"], item["units"])
                log(f"[dim]Jettisoned {item['units']}x {item['symbol']}[/dim]")
            except SpaceTradersError:
                pass


def refuel_if_needed(ship_symbol: str, threshold: int = 200) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    fuel = ship["fuel"]
    if fuel["capacity"] > 0 and fuel["current"] < threshold:
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
    """Return the closest known market waypoint to from_wp using coordinate distance."""
    markets = _known_markets or [ASTEROID_BASE]
    fx, fy = _get_coords(from_wp)
    best_wp, best_dist = ASTEROID_BASE, float("inf")
    for wp in markets:
        wx, wy = _get_coords(wp)
        dist = ((wx - fx) ** 2 + (wy - fy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist, best_wp = dist, wp
    if best_wp != ASTEROID_BASE:
        log(f"[dim]Nearest market to {from_wp}: {best_wp} (dist={best_dist:.0f})[/dim]")
    return best_wp


# ── Market intelligence ───────────────────────────────────────────────────────

def discover_markets() -> list[str]:
    """Scan all waypoints in the system for MARKETPLACE trait. Updates _known_markets."""
    global _known_markets
    log("[dim]Scanning system for markets...[/dim]")
    try:
        waypoints = universe_api.get_waypoints(SYSTEM)
        found = [
            wp["symbol"] for wp in waypoints
            if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
        ]
        if found:
            _known_markets = found
            log(f"[dim]Found {len(found)} market(s): {', '.join(found)}[/dim]")
        elif not _known_markets:
            _known_markets = [ASTEROID_BASE]
    except SpaceTradersError as e:
        log(f"[dim]Market scan failed: {e} — using defaults[/dim]")
        if not _known_markets:
            _known_markets = [ASTEROID_BASE]
    return _known_markets


def get_market_prices(waypoint: str) -> dict[str, int]:
    """Return {trade_symbol: sell_price} for a waypoint. Results are cached."""
    now = time.time()
    if waypoint in _market_cache and now - _market_cache_ts.get(waypoint, 0) < MARKET_CACHE_TTL:
        return _market_cache[waypoint]
    try:
        data = universe_api.get_market(SYSTEM, waypoint)
        prices = {g["symbol"]: g["sellPrice"] for g in data.get("tradeGoods", [])}
        # Also store buy prices under a buy_ prefix for purchasing decisions
        buy = {f"_buy_{g['symbol']}": g["purchasePrice"] for g in data.get("tradeGoods", [])}
        _market_cache[waypoint] = {**prices, **buy}
        _market_cache_ts[waypoint] = now
        return prices
    except SpaceTradersError:
        return {k: v for k, v in _market_cache.get(waypoint, {}).items() if not k.startswith("_buy_")}


def best_sell_waypoint(good: str) -> tuple[str, int]:
    """Return (waypoint, sell_price) for the market paying the most for `good`."""
    best_wp, best_price = ASTEROID_BASE, 0
    for wp in (_known_markets or [ASTEROID_BASE]):
        price = get_market_prices(wp).get(good, 0)
        if price > best_price:
            best_price, best_wp = price, wp
    return best_wp, best_price


def best_sell_market_for_cargo(inventory: list[dict]) -> str:
    """
    Given a list of cargo items, return the waypoint with the highest aggregate
    sell revenue. Falls back to ASTEROID_BASE if no price data is available.
    """
    market_values: dict[str, int] = {}
    for item in inventory:
        for wp in (_known_markets or [ASTEROID_BASE]):
            price = get_market_prices(wp).get(item["symbol"], 0)
            market_values[wp] = market_values.get(wp, 0) + price * item["units"]

    if not market_values:
        return ASTEROID_BASE

    base_val = market_values.get(ASTEROID_BASE, 0)
    best_market, best_val = ASTEROID_BASE, base_val
    for wp, val in market_values.items():
        if val > best_val:
            best_val, best_market = val, wp

    # Only reroute if another market pays meaningfully more (>20% premium)
    if best_market != ASTEROID_BASE and best_val > base_val * 1.20:
        log(f"[dim]Routing to {best_market} (est. {best_val:,} cr vs {base_val:,} cr at base)[/dim]")
        return best_market
    return ASTEROID_BASE


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
    Ship must be docked at a shipyard; credits are deducted automatically.
    """
    ship = fleet_api.get_ship(ship_symbol)
    if not has_mining_mount(ship):
        return
    tier = _best_mount_tier(ship)
    if tier >= len(MINING_MOUNT_TIERS) - 1:
        log(f"[dim]{ship_symbol} already has best mining mount ({MINING_MOUNT_TIERS[-1]})[/dim]")
        return
    target = MINING_MOUNT_TIERS[tier + 1]
    current = MINING_MOUNT_TIERS[tier] if tier >= 0 else "none"
    log(f"[bold]Upgrading {ship_symbol}: {current} → {target}[/bold]")
    navigate_to(ship_symbol, SHIPYARD_WP)
    ensure_docked(ship_symbol)
    try:
        result = fleet_api.install_mount(ship_symbol, target)
        ag = result.get("agent", {})
        log(f"[green]🔩 Installed {target} on {ship_symbol}! Credits: {ag.get('credits', 0):,}[/green]")
    except SpaceTradersError as e:
        log(f"[dim]Mount upgrade not available ({target} on {ship_symbol}): {e}[/dim]")


def step_upgrade_fleet() -> None:
    """Attempt mount upgrades on all mining ships."""
    log("[bold]Checking upgrade opportunities[/bold]")
    miners = get_mining_ships()
    if not miners:
        return
    for miner in miners:
        upgrade_mining_mounts(miner)


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
    """
    good = contract["terms"]["deliver"][0]["tradeSymbol"]
    log(f"[magenta]🔭 {ship_symbol} surveyor thread started[/magenta]")

    # Preflight: get to ASTEROID_BASE for fuel, then to ASTEROID
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0  = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    if _wp0 not in (ASTEROID, ASTEROID_BASE) and _fuel_pct0 < 0.50:
        _nearest = nearest_refuel_point(_wp0)
        log(f"[dim]{ship_symbol}: surveyor preflight refuel at {_nearest}[/dim]")
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
    cid = contract["id"]
    d = contract["terms"]["deliver"][0]  # first delivery term
    good = d["tradeSymbol"]
    delivery_wp = d["destinationSymbol"]

    log(f"[cyan]⚓ {ship_symbol} thread started | mining: {good} → {delivery_wp}[/cyan]")

    # Safe startup: if not near the asteroid or fuel < 50%, refuel at ASTEROID_BASE first
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0 = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    if _wp0 not in (ASTEROID, ASTEROID_BASE) and _fuel_pct0 < 0.50:
        log(f"[dim]{ship_symbol}: preflight refuel at {ASTEROID_BASE} (wp={_wp0}, fuel={_f0['current']}/{_f0['capacity']})[/dim]")
        navigate_with_refuel(ship_symbol, ASTEROID_BASE)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max

    navigate_with_refuel(ship_symbol, ASTEROID)
    ensure_orbit(ship_symbol)
    active_survey = _get_shared_survey(good) or try_survey(ship_symbol, good)

    while not stop_event.is_set() and not contract_done.is_set():
        # Refresh active_survey from shared pool if we don't have one
        if active_survey is None:
            active_survey = _get_shared_survey(good)

        # ── Proactive fuel + condition check (one API call) ───────────────────
        _loop_ship = fleet_api.get_ship(ship_symbol)
        _fuel = _loop_ship["fuel"]
        _at_asteroid = _loop_ship["nav"].get("waypointSymbol") == ASTEROID
        # Only refuel if NOT already at the asteroid — mining costs no fuel, so
        # let small ships (80-cap) mine a full load before drifting back to B7.
        if not _at_asteroid and _fuel.get("capacity", 0) > 0 and _fuel["current"] / _fuel["capacity"] < 0.40:
            log(f"[yellow]⛽ {ship_symbol}: fuel low ({_fuel['current']}/{_fuel['capacity']}), topping up[/yellow]")
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
            ensure_docked(ship_symbol)
            refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max
            navigate_with_refuel(ship_symbol, ASTEROID)
            ensure_orbit(ship_symbol)
            active_survey = _get_shared_survey(good) or try_survey(ship_symbol, good)

        # ── Proactive repair check: detour to shipyard if condition degraded ──
        if needs_repair(_loop_ship):
            _cond = min(_condition(_loop_ship.get(c, {})) for c in ("frame", "engine", "reactor"))
            log(f"[yellow]🔧 {ship_symbol}: condition {_cond:.0%} below threshold — diverting to repair[/yellow]")
            repair_ship(ship_symbol)
            navigate_with_refuel(ship_symbol, ASTEROID)
            ensure_orbit(ship_symbol)
            active_survey = _get_shared_survey(good) or try_survey(ship_symbol, good)

        # ── Cargo almost full: deliver or dump ────────────────────────────────
        if cargo_space(ship_symbol) < 5:
            have = good_in_cargo(ship_symbol, good)

            if have > 0 and not contract_done.is_set():
                # Refuel at ASTEROID_BASE before the long delivery trip to maximize fuel
                navigate_to(ship_symbol, ASTEROID_BASE)
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)
                navigate_to(ship_symbol, delivery_wp)
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
                    # Drift to nearest market (may be closer than ASTEROID_BASE)
                    _nearest = nearest_refuel_point(delivery_wp)
                    navigate_to(ship_symbol, _nearest)
                    ensure_docked(ship_symbol)
                    refuel_if_needed(ship_symbol, threshold=100_000)  # fill to max
                    navigate_to(ship_symbol, ASTEROID)
                    ensure_orbit(ship_symbol)
            else:
                # No contract good — dump junk and return
                navigate_to(ship_symbol, ASTEROID_BASE)
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)  # always fill to max
                sell_junk(ship_symbol, good)
                navigate_to(ship_symbol, ASTEROID)
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
            for ev in result.get("events", []):
                log(f"  [yellow]⚠ {ship_symbol}: {ev.get('name')}: {ev.get('description', '')}[/yellow]")

        except SpaceTradersError as e:
            if e.code == 4228:  # cargo full — handled at top of loop
                pass
            else:
                log(f"[red]{ship_symbol} extract error: {e}[/red]")
                time.sleep(5)

    log(f"[dim]{ship_symbol} miner thread done[/dim]")


# ── Background fleet manager ─────────────────────────────────────────────────

def _bg_buy_and_launch(
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Buy any affordable ships and immediately launch a miner thread for each."""
    me = agent_api.get_my_agent()
    credits = me.get("credits", 0)
    if credits < MIN_BUY_CREDITS:  # wait until we can afford the light shuttle
        return

    # Ensure fleet manager ship is docked at the shipyard for price data
    try:
        mgr = fleet_api.get_ship(FLEET_MANAGER_SHIP)
    except SpaceTradersError:
        log(f"[yellow]Fleet manager: can't reach {FLEET_MANAGER_SHIP}[/yellow]")
        return

    if mgr["nav"]["status"] == "IN_TRANSIT" or mgr["nav"]["waypointSymbol"] != SHIPYARD_WP:
        navigate_to(FLEET_MANAGER_SHIP, SHIPYARD_WP)
    ensure_docked(FLEET_MANAGER_SHIP)

    shipyard = universe_api.get_shipyard(SYSTEM, SHIPYARD_WP)
    ships_for_sale = shipyard.get("ships", [])
    if not ships_for_sale:
        log("[yellow]Fleet manager: no price data at shipyard[/yellow]")
        return

    current_miners = len(get_mining_ships())
    buyable = sorted(
        [s for s in ships_for_sale if ship_score(s.get("type", ""), current_miners) >= 0],
        key=lambda s: ship_score(s.get("type", ""), current_miners),
        reverse=True,
    )

    for ship_info in buyable:
        if contract_done.is_set() or stop_event.is_set():
            break
        ship_type = ship_info["type"]
        price     = ship_info["purchasePrice"]

        # Skip ships with fuel tanks too small to navigate the mining route
        fuel_cap = ship_info.get("frame", {}).get("fuelCapacity", 9999)
        if fuel_cap < MIN_FUEL_CAPACITY:
            log(f"[yellow]Fleet manager: skipping {ship_type} — fuel tank too small ({fuel_cap} < {MIN_FUEL_CAPACITY})[/yellow]")
            continue

        me = agent_api.get_my_agent()
        if me.get("credits", 0) - price < CREDIT_RESERVE:
            log(f"[yellow]Fleet manager: can't afford {ship_type} ({price:,} cr)[/yellow]")
            continue

        try:
            result     = fleet_api.purchase_ship(ship_type, SHIPYARD_WP)
            new_ship   = result.get("ship", {})
            tx         = result.get("transaction", {})
            ag         = result.get("agent", {})
            new_symbol = new_ship.get("symbol", "")
            log(f"[green bold]🚀 [BG] Bought {new_symbol} ({ship_type}) for {tx.get('price', 0):,} cr[/green bold]")
            log(f"Credits remaining: [green]{ag.get('credits', 0):,}[/green]")

            # Immediately launch the correct loop for the new ship
            if new_symbol and not contract_done.is_set():
                new_ship_data = fleet_api.get_ship(new_symbol)
                if has_survey_mount(new_ship_data) and not has_mining_mount(new_ship_data):
                    loop_target = surveyor_loop
                    thread_label = "surveyor"
                else:
                    loop_target = miner_loop
                    thread_label = "miner"
                    current_miners += 1  # keep score in sync
                t = threading.Thread(
                    target=loop_target,
                    args=(new_symbol, contract, contract_done, stop_event),
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
    The ship navigates to the faction HQ (X1-HU91-A1), negotiates in orbit, then
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
        navigate_to(FLEET_MANAGER_SHIP, "X1-HU91-A1")
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
    miners = miners or [COMMAND_SHIP]
    log(f"[bold]Launching {len(miners)} miner thread(s): {miners}[/bold]")
    if surveyors:
        log(f"[magenta]Launching {len(surveyors)} surveyor thread(s): {surveyors}[/magenta]")

    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=miner_loop,
            args=(miner, contract, contract_done, stop_event),
            daemon=True,
            name=f"miner-{miner}",
        )
        for miner in miners
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

    contract_done.wait()   # block until any miner signals fulfillment
    stop_event.set()       # tell remaining miners and fleet manager to wind down
    for t in threads:
        t.join(timeout=120)
    mgr_thread.join(timeout=30)

    log("[green bold]All miners done.[/green bold]")


# ── Ship buying ───────────────────────────────────────────────────────────────

def ship_score(ship_type: str, current_miner_count: int) -> int:
    """
    Score a ship type. Higher = more desirable to buy.
    -1 means skip entirely.
    Haulers get a boost once we already have 2+ miners.
    """
    base = SHIP_SCORES.get(ship_type, 30)  # unknown types get a mediocre score
    if base < 0:
        return -1
    # Haulers aren't very useful until we have multiple miners
    if ship_type == "SHIP_LIGHT_HAULER" and current_miner_count < 2:
        return 20
    return base


def buy_ships() -> int:
    """
    Navigate command ship to each known shipyard, show available ships, and buy
    as many useful ships as credits allow (keeping CREDIT_RESERVE).
    Only runs when credits >= MIN_BUY_CREDITS.
    Returns the number of ships purchased.
    """
    strategy = load_strategy()
    mode = strategy.get("mode", "contract_grind")
    if mode in ("idle", "upgrade_first"):
        log(f"[yellow]Skipping fleet expansion (strategy mode: {mode})[/yellow]")
        return 0

    me = agent_api.get_my_agent()
    credits = me.get("credits", 0)
    if credits < MIN_BUY_CREDITS:
        log(f"[yellow]Skipping fleet expansion — saving for light shuttle ({credits:,} / {MIN_BUY_CREDITS:,} cr)[/yellow]")
        return 0

    log("[bold]Building fleet[/bold]")
    log(f"Credits available: [green]{credits:,}[/green] | Reserve: {CREDIT_RESERVE:,}")

    current_miners = len(get_mining_ships())
    purchases = 0

    for shipyard_wp in SHIPYARD_WPS:
        navigate_to(COMMAND_SHIP, shipyard_wp)
        ensure_docked(COMMAND_SHIP)

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
            sc = ship_score(s.get("type", ""), current_miners)
            price = s.get("purchasePrice", 0)
            can_afford = "[green]✓[/green]" if credits - price >= CREDIT_RESERVE else "[red]✗[/red]"
            priority = str(sc) if sc >= 0 else "[dim]skip[/dim]"
            t.add_row(s.get("type", "?"), f"{can_afford} {price:,} cr", s.get("supply", "?"), priority)
        console.print(t)

        # Buy greedily in priority order, skipping anything we can't afford
        buyable = sorted(
            [s for s in ships_for_sale if ship_score(s.get("type", ""), current_miners) >= 0],
            key=lambda s: ship_score(s.get("type", ""), current_miners),
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

def get_next_contract() -> dict | None:
    """Return an unfulfilled contract, or negotiate a new one if none exist."""
    cs = contracts_api.get_contracts()
    pending = [c for c in cs if not c.get("fulfilled")]
    if pending:
        strategy = load_strategy()
        target = strategy.get("target_contract_id")
        if target:
            for c in pending:
                if c.get("id") == target:
                    return c
        return pending[0]

    log("[yellow]No pending contracts — negotiating a new one...[/yellow]")
    try:
        # Need to be docked at a faction waypoint to negotiate
        navigate_to(COMMAND_SHIP, "X1-HU91-A1")
        ensure_docked(COMMAND_SHIP)
        result = fleet_api.negotiate_contract(COMMAND_SHIP)
        new_contract = result.get("contract", {})
        log(f"[green]New contract negotiated: {new_contract.get('id')}[/green]")
        return new_contract
    except SpaceTradersError as e:
        log(f"[red]Could not negotiate contract: {e}[/red]")
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

    # Discover all markets in the system on startup
    discover_markets()

    loop = 0
    while True:
        loop += 1
        me = agent_api.get_my_agent()
        log(f"[bold]─── Loop {loop} | Credits: {me['credits']:,} | Ships: {me['shipCount']} ───[/bold]")

        strategy = load_strategy()
        mode = strategy.get("mode", "contract_grind")
        if mode == "idle":
            notes = strategy.get("notes", "")
            log(f"[yellow]Strategy: IDLE — pausing for 60s. Notes: {notes}[/yellow]")
            time.sleep(60)
            continue
        if strategy.get("notes"):
            log(f"[dim]Strategy note: {strategy['notes']}[/dim]")

        contract = get_next_contract()
        if not contract:
            log("[red]No contract available. Retrying in 60s...[/red]")
            time.sleep(60)
            continue

        work_contract(contract)

        # Post-contract: expand → maintain → upgrade
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

