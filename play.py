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
import uuid
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
import discord_notify as discord

load_dotenv()
console = Console(force_terminal=True)




# ── Per-ship color palette ───────────────────────────────────────────────────────
# Colors cycle by the numeric suffix of the ship symbol (e.g. TYLERMASTERY2-3 → index 2)
_SHIP_COLORS = [
    "cyan",          # ship -1
    "green",         # ship -2
    "magenta",       # ship -3
    "yellow",        # ship -4
    "blue",          # ship -5
    "bright_cyan",   # ship -6
    "bright_green",  # ship -7
    "bright_magenta",# ship -8
    "bright_yellow", # ship -9
    "bright_red",    # ship -10
]

def _ship_color(ship_symbol: str) -> str:
    """Return a consistent Rich color string for a ship symbol."""
    try:
        idx = int(ship_symbol.rsplit("-", 1)[-1]) - 1
        return _SHIP_COLORS[idx % len(_SHIP_COLORS)]
    except (ValueError, IndexError):
        return "white"

def _ship_group_tag(ship_symbol: str) -> str:
    """Return '(T1)', '(T2)', etc. if ship is in a group, else ''."""
    with _ship_groups_lock:
        groups = list(_ship_groups)
    for i, g in enumerate(groups, 1):
        if ship_symbol == g.get("hauler") or ship_symbol in g.get("workers", []):
            return f"(T{i})"
    return ""

def _ship_label(ship_symbol: str) -> str:
    """Return a concise display label for a ship, e.g. 'Siphoner-9 (T1)'."""
    suffix = ship_symbol.rsplit("-", 1)[-1]  # e.g. "4", "1C", "B"
    role = _ship_role_tag.get(ship_symbol, "")
    grp = _ship_group_tag(ship_symbol)
    if role:
        base = f"{role}-{suffix}"
        return f"{base} {grp}" if grp else base
    return ship_symbol  # fallback to full symbol before roles are assigned

def ship_log(ship_symbol: str, msg: str) -> None:
    """Log a message prefixed with the ship's color-coded role label."""
    color = _ship_color(ship_symbol)
    ts = datetime.now().strftime("%H:%M:%S")
    label = _ship_label(ship_symbol)
    console.print(f"[dim]{ts}[/dim] [{color}]{label}[/{color}] {msg}")
    db.write_log(f"{ts} {label} {msg}")

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
_scout_lock    = threading.Lock()                  # prevents two traders claiming the same scout target
_scout_claimed: set[str] = set()                   # waypoints currently being scouted by a trader
_scout_skip:   dict[str, float] = {}               # {waypoint: skip_until_ts} — markets that returned 0 goods

# Traders claim a (good, buy_wp) route slot so other traders skip to the next best option.
_route_lock    = threading.Lock()
_claimed_routes: dict[str, str] = {}  # {good: ship_symbol} — good currently being worked by a ship

# ── Shared survey pool (surveyor ships write, miners read) ────────────────────
_shared_surveys: list[dict] = []
_surveys_lock   = threading.Lock()

# ── Hauler and explorer ship registries ───────────────────────────────────────
_hauler_symbols:   list[str] = []   # symbols of ships running hauler_loop
_explorer_symbols: list[str] = []   # symbols of ships running explorer_loop
_siphoner_symbols: list[str] = []   # symbols of ships running siphon_loop
_trader_symbols:   list[str] = []   # symbols of ships running trader_loop

# ── Ship groups (hauler + workers operating as a unit) ────────────────────────
# Each entry: {"type": "siphon"|"miner", "hauler": "SYMBOL", "workers": ["SYM1", ...]}
# Workers skip self-delivery and signal the hauler instead.
_ship_groups: list[dict] = []
_ship_groups_lock = threading.Lock()

# ── Per-ship role tags (set when threads start, used by ship_log) ─────────────
# Value is a short label like "Siphoner", "Trader", "Surveyor", etc.
# Group context (e.g. "T1", "T2") is appended for siphon/miner group members.
_ship_role_tag: dict[str, str] = {}
_ship_no_sensor_logged: set[str] = set()  # suppress repeat "no sensor array" messages

# Per-worker event: worker sets this when its cargo is full, hauler clears it after transfer.
# Keys are worker ship symbols.
_group_worker_ready: dict[str, threading.Event] = {}


def _load_ship_groups() -> list[dict]:
    """Read ship_groups from DB. Returns [] if unset or invalid."""
    raw = db.get_bot_setting("ship_groups", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_ship_groups(groups: list[dict]) -> None:
    db.set_bot_setting("ship_groups", json.dumps(groups))


def auto_group_ships() -> None:
    """Automatically derive ship groups from mount types.

    Detects which ships have GAS_SIPHON or MINING_LASER mounts and groups them
    with hauler ships as their tender.  Only runs when:
      - 'auto_group_ships' DB setting is '1' (force-rebuild each run), OR
      - No groups currently exist in the DB.

    Round-robins workers across available haulers (~1 hauler per 3 workers).
    Saves result to DB as the 'ship_groups' key.
    """
    force    = db.get_bot_setting("auto_group_ships", "0") == "1"
    existing = _load_ship_groups()
    if existing and not force:
        # Rebuild if any siphon/miner workers are ungrouped
        _all_ships_chk = fleet_api.get_my_ships()
        _cur_siphoners = set(
            s["symbol"] for s in _all_ships_chk
            if has_siphon_mount(s) and s["symbol"] != FLEET_MANAGER_SHIP
        )
        _cur_miners_chk = set(
            s["symbol"] for s in _all_ships_chk
            if has_mining_mount(s) and s["symbol"] not in (FLEET_MANAGER_SHIP, COMMAND_SHIP)
        )
        _grouped_ws = set(w for g in existing for w in g.get("workers", []))
        if _cur_siphoners <= _grouped_ws and _cur_miners_chk <= _grouped_ws:
            return  # all workers already grouped
        log("[cyan]auto_group_ships: ungrouped workers found - rebuilding[/cyan]")

    all_ships = fleet_api.get_my_ships()

    siphon_workers = [
        s["symbol"] for s in all_ships
        if has_siphon_mount(s) and s["symbol"] != FLEET_MANAGER_SHIP
    ]
    miner_workers = [
        s["symbol"] for s in all_ships
        if has_mining_mount(s)
        and s["symbol"] not in (FLEET_MANAGER_SHIP, COMMAND_SHIP)
    ]
    worker_syms = set(siphon_workers + miner_workers)
    haulers = [
        s["symbol"] for s in all_ships
        if s["registration"]["role"] in ("HAULER", "TRANSPORT")
        and s["symbol"] != FLEET_MANAGER_SHIP
        and s["symbol"] not in worker_syms
    ]

    if not (siphon_workers or miner_workers) or not haulers:
        log("[dim]auto_group_ships: no workers or no haulers to group[/dim]")
        return

    groups: list[dict]   = []
    # Always reserve at least 1 hauler free for contract work / trading.
    # Use at most (len(haulers) - 1) haulers for groups, capped by team limits.
    max_group_haulers = max(0, len(haulers) - 1)
    remaining = list(haulers[:max_group_haulers]) if max_group_haulers > 0 else []

    # ── Siphon groups ─────────────────────────────────────────────────────────────
    if siphon_workers and remaining:
        n = max(1, (len(siphon_workers) + 2) // 3)  # 1 hauler per ~3 workers
        n = min(n, len(remaining), MAX_SIPHON_TEAMS)  # respect team cap
        s_haulers  = remaining[:n]
        remaining  = remaining[n:]
        for i, hauler in enumerate(s_haulers):
            my_workers = [w for j, w in enumerate(siphon_workers) if j % n == i]
            if my_workers:
                groups.append({
                    "name":    f"Siphon Team {i + 1}",
                    "type":    "siphon",
                    "hauler":  hauler,
                    "workers": my_workers,
                })

    # ── Miner groups ──────────────────────────────────────────────────────────────
    if miner_workers and remaining:
        n = max(1, (len(miner_workers) + 2) // 3)
        n = min(n, len(remaining), MAX_MINER_TEAMS)  # respect team cap
        m_haulers  = remaining[:n]
        for i, hauler in enumerate(m_haulers):
            my_workers = [w for j, w in enumerate(miner_workers) if j % n == i]
            if my_workers:
                groups.append({
                    "name":    f"Miner Team {i + 1}",
                    "type":    "miner",
                    "hauler":  hauler,
                    "workers": my_workers,
                })

    if groups:
        _save_ship_groups(groups)
        log(f"[cyan]auto_group_ships: created {len(groups)} group(s)[/cyan]")
        for g in groups:
            log(f"  {g['name']}: hauler={g['hauler']} workers={g['workers']}")


# Last-known shipyard listings, keyed by waypoint symbol.
# Populated after each fleet manager tour; used to skip tours when credits
# are nowhere near affording any eligible ship.
_shipyard_price_cache: dict[str, list[dict]] = {}

# When working a direct-buy contract, only one ship does buy/deliver.
# All other miners get this mine-only contract (fake good = "__SELL_ONLY__")
# so they mine and sell ore for income without competing for the good.
_mine_only_contract: dict | None = None

# Ships that have been explicitly assigned to BUY (not mine) the contract good.
# Set by work_contract when a miner is used as the fallback contract buyer.
_contract_buy_ships: set[str] = set()

# ── Active contract reference (set in work_contract, read by status table) ──────
_active_contract: dict | None = None

# Per-ship task destination — the *final* destination passed to navigate_with_refuel.
# Set at the top of each navigate_with_refuel call so the status table can display
# the full planned route even while the ship is stopped at a refuel waypoint.
_ship_task_dest: dict[str, str] = {}
_active_contract_lock = threading.Lock()

# ── Config ────────────────────────────────────────────────────────────────────
SYSTEM         = "X1-GK27"          # auto-set by auto_configure()
COMMAND_SHIP   = "MASTERY-1"        # auto-set by auto_configure()
ASTEROID       = "X1-GK27-CD5A"    # auto-set by auto_configure()
ASTEROID_BASE  = "X1-GK27-H48"     # auto-set by auto_configure()
SHIPYARD_WP    = "X1-GK27-H48"     # auto-set by auto_configure()
SHIPYARD_WPS   = ["X1-GK27-H48", "X1-GK27-A2", "X1-GK27-C37"]  # auto-set by auto_configure()
_FACTION_HQ_WP = "X1-GK27-A1"     # auto-set by auto_configure() — used for contract negotiation
CREDIT_RESERVE = 500_000           # Minimum credits to keep in reserve
MIN_BUY_CREDITS = 600_000          # Fleet manager starts buying once we clear this threshold
AUTO_BUY_SHIPS  = True             # Fleet manager will buy ships when credits allow
CHEAP_BUY_THRESHOLD    = 200        # cr/unit — buy even mineable goods if market price is this low
DRY_EXTRACT_THRESHOLD  = 5          # consecutive zero-hit extractions before forcing buy mode
SELL_ROUTING_DIST_COST = 20         # cr per distance unit deducted from remote-market revenue (fuel + time proxy)
MIN_CONTRACT_PAYOUT    = 30_000     # Skip contracts with onFulfilled < this and try to negotiate a better one
FLEET_MANAGER_SHIP = "MASTERY-2"   # auto-set by auto_configure()
MIN_FUEL_CAPACITY  = 0             # No blanket fuel check — no-drift gate handles small-tank ships
NO_DRIFT_DIST_MAX  = 70            # Max distance from fuel market for mining/siphon drones

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

# Ship purchase priority — higher score = buy first.
# -1 means never buy; dynamic gating enforced in ship_score().
SHIP_SCORES = {
    "SHIP_LIGHT_HAULER":   100,  # Seed new teams + traders — always first priority
    "SHIP_MINING_DRONE":    60,  # Cheap miner — fill miner team slots (drift-safe check)
    "SHIP_SIPHON_DRONE":    55,  # Gas siphoner — fill siphon team slots
    "SHIP_PROBE":           20,  # Scout/charter — buy when wealthy (>1M cr), explore all systems
    "SHIP_ORE_HOUND":       15,  # Miner — lower priority until groups prove out
    "SHIP_COMMAND_FRIGATE": 45,  # 2nd frigate for refinery scouting — bought at 1.5M cr
    "SHIP_SURVEYOR":        -1,  # Never buy
    "SHIP_HEAVY_FREIGHTER": -1,  # Never buy
    "SHIP_GAS_DRONE":       -1,  # Never buy
    "SHIP_LIGHT_SHUTTLE":   -1,  # Never buy
}

# ── Team composition targets (hardcoded) ────────────────────────────────────────────
PRODUCERS_PER_TEAM_TARGET = 5   # workers to fill one team before seeding a new one
HAULERS_PER_TEAM_TARGET   = 1   # haulers per active team (currently always 1)
MAX_TRADERS               = None  # auto: max(1, len(_known_markets) // 5) — set dynamically at runtime
PROBE_CREDIT_THRESHOLD    = 1_000_000  # don't buy probes until we have this many credits
MAX_PROBES                = None  # auto: len(_known_markets) — one probe per known market
MAX_SIPHON_TEAMS          = 2   # maximum concurrent siphon teams
MAX_MINER_TEAMS           = 2   # maximum concurrent miner teams

MIN_SELL_PRICE     = 30     # cr/unit — jettison anything below this instead of hauling to market
REPAIR_THRESHOLD   = 0.80   # Repair when any component drops below 80% condition

# ── Refining maps ─────────────────────────────────────────────────────────────
# Key = raw cargo good; value = `produce` argument for fleet.refine().
# Ships with MODULE_ORE_REFINERY_I or MODULE_MICRO_REFINERY_I can refine ores;
# ships with MODULE_GAS_PROCESSOR_I can refine HYDROCARBON → FUEL.
ORE_TO_REFINED: dict[str, str] = {
    "IRON_ORE":     "IRON",
    "COPPER_ORE":   "COPPER",
    "ALUMINUM_ORE": "ALUMINUM",
    "SILVER_ORE":   "SILVER",
    "GOLD_ORE":     "GOLD",
    "PLATINUM_ORE": "PLATINUM",
    "URANITE_ORE":  "URANITE",
    "MERITIUM_ORE": "MERITIUM",
}
GAS_TO_REFINED: dict[str, str] = {
    "HYDROCARBON": "FUEL",
}
REFINEABLE: dict[str, str] = {**ORE_TO_REFINED, **GAS_TO_REFINED}
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
    db.write_log(f"{ts} {msg}")


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
            ship_log(ship_symbol, f"[green]✓[/green] arrived at [bold]{nav['waypointSymbol']}[/bold]")
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
            flight_mode = nav.get("flightMode", "")
            mode_suffix = {
                "BURN":   " 🔥burn",
                "DRIFT":  " drift",
                "CRUISE": "",
            }.get(flight_mode, f" {flight_mode.lower()}" if flight_mode else "")
            ship_log(ship_symbol, f"[yellow]⏳ in transit (~{eta_str}{mode_suffix})...[/yellow]")
            last_log = now
        time.sleep(30 if secs > 120 else poll)


def wait_cooldown(ship_symbol: str) -> None:
    """Block until ship cooldown expires."""
    color = _ship_color(ship_symbol)
    while True:
        try:
            cd = fleet_api.get_ship_cooldown(ship_symbol)
            remaining = cd.get("remainingSeconds", 0) if isinstance(cd, dict) else 0
            if remaining <= 0:
                return
            # Only log at start and when ≤15s remain to avoid repeated countdown spam
            if remaining > 15:
                ship_log(ship_symbol, f"[yellow]🌡 cooldown {remaining}s[/yellow]")
            time.sleep(min(remaining, 10))
        except SpaceTradersError as e:
            if e.code == 204 or "no cooldown" in str(e).lower():
                return
            raise


def ensure_orbit(ship_symbol: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "DOCKED":
        fleet_api.orbit(ship_symbol)
        ship_log(ship_symbol, f"[cyan]↑ orbiting[/cyan]")
        time.sleep(0.5)  # let orbit state propagate before any immediate navigate call
    elif ship["nav"]["status"] == "IN_TRANSIT":
        wait_for_ship(ship_symbol)


def ensure_docked(ship_symbol: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "IN_ORBIT":
        fleet_api.dock(ship_symbol)
        ship_log(ship_symbol, f"[cyan]⚓ docked[/cyan]")
    elif ship["nav"]["status"] == "IN_TRANSIT":
        wait_for_ship(ship_symbol)
        fleet_api.dock(ship_symbol)
        ship_log(ship_symbol, f"[cyan]⚓ docked[/cyan]")


def navigate_to(ship_symbol: str, destination: str) -> None:
    ship = fleet_api.get_ship(ship_symbol)
    # If already in transit toward the destination (e.g., drifting there), just wait
    if ship["nav"]["status"] == "IN_TRANSIT":
        route_dest = ship["nav"].get("route", {}).get("destination", {}).get("symbol", "")
        if route_dest == destination:
            ship_log(ship_symbol, f"[dim]already en route to {destination} — waiting...[/dim]")
            wait_for_ship(ship_symbol)
            _fm = fleet_api.get_ship(ship_symbol)["nav"].get("flightMode", "CRUISE")
            if _fm != "CRUISE":
                fleet_api.patch_nav(ship_symbol, "CRUISE")
                ship_log(ship_symbol, f"[dim]restored CRUISE mode after in-transit wait[/dim]")
            return
    if ship["nav"]["waypointSymbol"] == destination:
        return  # already here — no log needed

    # ── Inter-system travel ───────────────────────────────────────────────────
    dest_system = _system_of(destination)
    curr_system = _system_of(ship["nav"]["waypointSymbol"])
    if dest_system != curr_system:
        if has_warp_drive(ship):
            # Warp drive: travel directly without needing a jump gate (takes time, uses fuel)
            ensure_orbit(ship_symbol)
            ship_log(ship_symbol, f"[blue]🌀 warping → {dest_system}[/blue]")
            fleet_api.warp(ship_symbol, dest_system)
            wait_for_ship(ship_symbol)
        else:
            # Any ship can use a jump gate — navigate to it, then jump (instantaneous)
            gate_wp = _find_jump_gate(curr_system)
            if not gate_wp:
                raise SpaceTradersError(0, f"No jump gate found in {curr_system}")
            if ship["nav"]["waypointSymbol"] != gate_wp:
                navigate_to(ship_symbol, gate_wp)  # intra-system leg to gate
            ensure_orbit(ship_symbol)
            ship_log(ship_symbol, f"[blue]⚡ jumping → {dest_system}[/blue]")
            fleet_api.jump(ship_symbol, dest_system)
            wait_cooldown(ship_symbol)  # jump is instant but triggers a cooldown
            # Navigate the final leg within the new system if needed
            _after = fleet_api.get_ship(ship_symbol)["nav"]["waypointSymbol"]
            if _after != destination:
                navigate_to(ship_symbol, destination)
        return

    # ── Intra-system travel ───────────────────────────────────────────────────
    ensure_orbit(ship_symbol)
    # Use BURN if ship has enough fuel for the 2x cost; otherwise CRUISE.
    cur_fuel     = ship["fuel"].get("current", 0)
    dist         = waypoint_distance(ship["nav"]["waypointSymbol"], destination)
    burn_cost    = max(2, round(dist) * 2)
    desired_mode = "BURN" if cur_fuel >= burn_cost else "CRUISE"
    current_mode = ship["nav"].get("flightMode", "CRUISE")
    if current_mode != desired_mode:
        fleet_api.patch_nav(ship_symbol, desired_mode)
        if desired_mode == "BURN":
            ship_log(ship_symbol, f"[dim]🔥 BURN mode (dist={dist:.0f}, fuel={cur_fuel})[/dim]")
        else:
            ship_log(ship_symbol, f"[dim]reset to CRUISE mode[/dim]")
    ship_log(ship_symbol, f"[blue]🚀 Navigating → {destination}[/blue]")
    try:
        fleet_api.navigate(ship_symbol, destination)
    except SpaceTradersError as e:
        if e.code == 4214:  # ship already in transit — wait and retry once
            ship_log(ship_symbol, f"[dim]already in transit, waiting to arrive before redirecting...[/dim]")
            wait_for_ship(ship_symbol)
            navigate_to(ship_symbol, destination)
            return
        if e.code == 4236:  # not in orbit — orbit API may not have propagated; wait briefly and retry
            ship_log(ship_symbol, f"[dim]4236: re-orbiting and retrying after short wait...[/dim]")
            fleet_api.orbit(ship_symbol)
            time.sleep(1)  # give server time to propagate orbit state
            # Use navigate_to (not raw API) so fuel checks still apply on the retry
            navigate_to(ship_symbol, destination)
            return
        if e.code == 4203:  # insufficient fuel — try local refuel first, then DRIFT
            ship_log(ship_symbol, f"[yellow]⚠ insufficient fuel for {destination} — attempting local refuel[/yellow]")
            _fuel_before = ship["fuel"].get("current", 0)
            try:
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)
            except SpaceTradersError:
                pass
            # Only retry navigate if refuel actually added fuel; refuel_if_needed
            # logs silently on failure (no exception), so we must check fuel level.
            _ship_after = fleet_api.get_ship(ship_symbol)
            if _ship_after["fuel"].get("current", 0) > _fuel_before:
                navigate_to(ship_symbol, destination)
                return
            ship_log(ship_symbol, f"[red]⚠ no fuel available locally, emergency DRIFT to {destination}[/red]")
            # Must orbit before navigating — ship was docked by ensure_docked above.
            ensure_orbit(ship_symbol)
            fleet_api.patch_nav(ship_symbol, "DRIFT")
            try:
                fleet_api.navigate(ship_symbol, destination)
            except SpaceTradersError as drift_e:
                if drift_e.code == 4214:
                    ship_log(ship_symbol, f"[dim]already drifting to {destination}[/dim]")
                else:
                    raise
            wait_for_ship(ship_symbol)
            fleet_api.patch_nav(ship_symbol, "CRUISE")
            ship_log(ship_symbol, f"[dim]restored CRUISE flight mode[/dim]")
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
    _ship_task_dest[ship_symbol] = destination
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

        # Fuel cost in CRUISE mode is max(1, round(distance))
        fuel_cost_to_dest = max(1, round(dist_to_dest))
        if cur_fuel >= fuel_cost_to_dest:
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
            if remaining >= dist_to_dest:
                continue  # no progress toward destination
            if remaining < best_remaining:
                best_remaining = remaining
                best_wp = wp

        if best_wp is None:
            # No fuel market can bridge the gap — try to cruise to any waypoint
            # (non-market included) that gets us geometrically closer before
            # drifting. CRUISE is much faster than DRIFT, so even one hop helps.
            best_cruise_wp: str | None = None
            best_cruise_remaining = dist_to_dest
            fuel_market_set = set(_good_exporters.get("FUEL", []))
            for wp, (wx, wy) in list(_wp_coords.items()):
                if wp == cur_wp:
                    continue
                hop_dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
                if hop_dist > cur_fuel:
                    continue  # can't reach in CRUISE with current fuel
                remaining = ((dx - wx) ** 2 + (dy - wy) ** 2) ** 0.5
                # Only use this waypoint as a cruise hop if it's a fuel market
                # OR the destination is reachable directly from here at full tank.
                # Hopping to a non-fuel waypoint that still can't reach the destination
                # just shifts where we drift from without saving any time.
                if wp not in fuel_market_set and remaining > fuel_cap:
                    continue
                if remaining < best_cruise_remaining:
                    best_cruise_remaining = remaining
                    best_cruise_wp = wp
            if best_cruise_wp:
                ship_log(ship_symbol, f"[dim]cruise hop → {best_cruise_wp} (closing drift gap to {destination})[/dim]")
                navigate_to(ship_symbol, best_cruise_wp)
                continue  # re-evaluate from new position with updated fuel
            ship_log(ship_symbol, f"[yellow]no reachable intermediate waypoint en route to {destination} — will drift[/yellow]")
            navigate_to(ship_symbol, destination)
            return

        ship_log(ship_symbol, f"[dim]refuel hop → {best_wp} (en route to {destination})[/dim]")
        navigate_to(ship_symbol, best_wp)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)
        ensure_orbit(ship_symbol)  # orbit after refueling so next navigate_to doesn't get 4236

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


def refine_cargo_for_sale(ship_symbol: str) -> None:
    """Refine any cargo goods where the refined output is more profitable, then sell refined.

    Requires the ship to have MODULE_ORE_REFINERY_I or MODULE_MICRO_REFINERY_I (for ores) or MODULE_GAS_PROCESSOR_I
    (for HYDROCARBON→FUEL).  Ship must be in orbit at the sell waypoint.
    """
    ship = fleet_api.get_ship(ship_symbol)
    has_mineral = has_mineral_processor(ship)
    has_gas     = has_gas_processor(ship)
    if not has_mineral and not has_gas:
        return

    inventory = ship["cargo"].get("inventory", [])
    if not inventory:
        return

    wp     = ship["nav"]["waypointSymbol"]
    prices = get_market_prices(wp)  # may be cached

    for item in inventory:
        raw     = item["symbol"]
        refined = REFINEABLE.get(raw)
        if not refined:
            continue
        if raw in ORE_TO_REFINED and not has_mineral:
            continue
        if raw in GAS_TO_REFINED and not has_gas:
            continue

        raw_price     = prices.get(raw, 0)
        refined_price = prices.get(refined, 0)
        # Refining is 100 raw → 10 refined (10% yield).
        # Only refine if: 10 × refined_price > 100 × raw_price  →  refined_price > 10 × raw_price
        if refined_price > 0 and refined_price <= raw_price * 10:
            ship_log(ship_symbol,
                f"[dim]Skip refine {raw}→{refined}: 10×{refined_price:,}={10*refined_price:,} <= 100×{raw_price:,}={100*raw_price:,} cr[/dim]")
            continue

        try:
            ensure_orbit(ship_symbol)
            wait_cooldown(ship_symbol)
            result   = fleet_api.refine(ship_symbol, refined)
            produced = result.get("produced", [])
            consumed = result.get("consumed", [])
            p_str    = ", ".join(f"{p.get('units')}x {p.get('tradeSymbol')}" for p in produced)
            c_str    = ", ".join(f"{c.get('units')}x {c.get('tradeSymbol')}" for c in consumed)
            ship_log(ship_symbol,
                f"[green]⚗️  Refined: {c_str} → {p_str}[/green]")
        except SpaceTradersError as e:
            ship_log(ship_symbol, f"[yellow]Refine {raw}→{refined} failed: {e}[/yellow]")


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


def refuel_from_cargo(ship_symbol: str) -> bool:
    """Use FUEL already in cargo to refuel (siphoners may produce FUEL via HYDROCARBON refining).
    Returns True if fuel was topped up from cargo."""
    ship = fleet_api.get_ship(ship_symbol)
    fuel = ship["fuel"]
    if fuel.get("capacity", 0) == 0:
        return False
    if fuel["current"] >= fuel["capacity"]:
        return False
    has_fuel_cargo = any(i["symbol"] == "FUEL" for i in ship["cargo"].get("inventory", []))
    if not has_fuel_cargo:
        return False
    try:
        ensure_docked(ship_symbol)
        result = fleet_api.refuel(ship_symbol, from_cargo=True)
        f = result.get("fuel", {})
        ship_log(ship_symbol, f"[green]⛽ Refueled from cargo to {f.get('current')}/{f.get('capacity')}[/green]")
        return True
    except SpaceTradersError:
        return False


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


def _short_wp(wp: str) -> str:
    """Return the trailing identifier of a waypoint symbol, e.g. 'B7' from 'X1-BX78-B7'."""
    return wp.rsplit("-", 1)[-1] if "-" in wp else wp


def _est_hop_secs(from_wp: str, to_wp: str, engine_speed: int) -> int:
    """Rough CRUISE-mode travel time in seconds between two waypoints."""
    try:
        dist = waypoint_distance(from_wp, to_wp)
        return max(15, round(15 + dist * 15 / max(engine_speed, 1)))
    except Exception:
        return 0


def _compute_route_hops(from_wp: str, destination: str, cur_fuel: int, fuel_cap: int) -> list[str]:
    """
    Simulate the greedy refuel-hop algorithm used by navigate_with_refuel, returning
    the ordered list of waypoints visited *after* from_wp (including destination).
    Uses only cached coordinates — never makes API calls.
    """
    route: list[str] = []
    wp = from_wp
    fuel = cur_fuel
    for _ in range(10):
        if wp == destination:
            break
        try:
            cx, cy = _get_coords(wp)
            dx, dy = _get_coords(destination)
        except Exception:
            route.append(destination)
            break
        dist_to_dest = ((dx - cx) ** 2 + (dy - cy) ** 2) ** 0.5
        if fuel > dist_to_dest:
            route.append(destination)
            break
        fuel_markets = _good_exporters.get("FUEL", [])
        markets = fuel_markets if fuel_markets else (_known_markets or [ASTEROID_BASE])
        best_wp: str | None = None
        best_remaining = dist_to_dest
        for m in markets:
            if m == wp:
                continue
            try:
                mx, my = _get_coords(m)
            except Exception:
                continue
            hop_dist = ((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5
            if hop_dist > fuel:
                continue
            remaining = ((dx - mx) ** 2 + (dy - my) ** 2) ** 0.5
            if remaining >= dist_to_dest:
                continue
            if remaining < best_remaining:
                best_remaining = remaining
                best_wp = m
        if best_wp is None:
            route.append(destination)
            break
        route.append(best_wp)
        wp = best_wp
        fuel = fuel_cap  # assume full refuel at each stop
    if not route or route[-1] != destination:
        route.append(destination)
    return route


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

# Tracks the asteroid where active miners are working — surveyors follow this.
_active_mining_wp: str = ""   # set by choose_mining_target; surveyor_loop reads it


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

    # First-trip drift penalty: if the ship can't cruise back to base after
    # the initial transit to this asteroid, it will be forced to drift home.
    # dist_from_ship fuel is burned getting here; base_dist more is needed to return.
    if ship_fuel_cap > 0 and (dist_from_ship + ast["base_dist"]) > ship_fuel_cap:
        score -= 60.0   # strong deterrent — pick a closer asteroid instead

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
    global _active_mining_wp, ASTEROID
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
                ship_log(ship_symbol, f"[cyan]sell-only → {_best_wp} (dist {_best_dist:.0f}, fuel cap {fuel_cap})[/cyan]")
            return _best_wp
        return ASTEROID  # no fuel tank (e.g. satellite) — use default

    _populate_asteroid_cache()

    if not _scored_asteroids:
        ship_log(ship_symbol, f"[yellow]asteroid cache empty — using default {ASTEROID}[/yellow]")
        return ASTEROID

    try:
        ship     = fleet_api.get_ship(ship_symbol)
        fuel     = ship["fuel"]
        ship_x, ship_y = _get_coords(ship["nav"]["waypointSymbol"])
        fuel_cap = fuel.get("capacity", 0)
    except SpaceTradersError as e:
        ship_log(ship_symbol, f"[yellow]choose_mining_target ship fetch failed ({e}) — using default[/yellow]")
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
    ship_log(ship_symbol, f"[cyan]mining target for [bold]{good}[/bold] (fuel cap={fuel_cap}) — top candidates:[/cyan]")
    for rank, (sc, ast) in enumerate(scored[:3], 1):
        match_str = "[green]✓ match[/green]" if deposit_traits & ast["traits"] else "no match"
        log(f"[dim]  #{rank} {ast['symbol']}  score={sc:.0f}  base_dist={ast['base_dist']:.0f}  {match_str}[/dim]")

    best = scored[0][1]["symbol"]
    ship_log(ship_symbol, f"[cyan]→ mining at [bold]{best}[/bold][/cyan]")
    _active_mining_wp = best  # surveyor follows the lead miner's chosen asteroid
    if best != ASTEROID:
        # Persist the better choice so future restarts start at the right asteroid
        try:
            agent = agent_api.get_my_agent()
            db.save_agent_config(agent["symbol"], {"ASTEROID": best})
            ASTEROID = best
            log(f"[cyan]Updated ASTEROID to {best} in DB[/cyan]")
        except Exception:
            pass
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
        _ship_role_tag[FLEET_MANAGER_SHIP] = "FleetMgr"
        log(f"[cyan]Agent: {callsign} | HQ: {hq} | System: {SYSTEM}[/cyan]")
    except SpaceTradersError as e:
        log(f"[yellow]auto_configure: agent query failed ({e}) — keeping defaults[/yellow]")
        return

    # ── Load from DB if this callsign was already configured ──────────────────
    db.init_db()   # ensure schema exists before reading config
    saved = db.load_agent_config(callsign)
    # Invalidate config from a previous reset if the saved waypoints belong to a different system
    if saved.get("ASTEROID") and not saved["ASTEROID"].startswith(SYSTEM + "-"):
        log(f"[yellow]Saved config is from a different system ({saved['ASTEROID']}) — discarding, re-detecting...[/yellow]")
        saved = {}
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
        # Also store buy prices and trade volume limits under prefixed keys
        buy = {f"_buy_{g['symbol']}": g["purchasePrice"] for g in data.get("tradeGoods", [])}
        vol = {f"_vol_{g['symbol']}": g["tradeVolume"] for g in data.get("tradeGoods", []) if g.get("tradeVolume")}
        # Only overwrite cache if we actually got price data (tradeGoods requires ship present).
        # Preserving last-known prices avoids zeroing out H51/H53 when called without a ship.
        if prices or buy:
            _market_cache[waypoint] = {**prices, **buy, **vol}
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
    ship_log(ship_symbol, f"[yellow]🔧 needs repair (worst condition: {worst:.0%})[/yellow]")
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
        ship_log(ship_symbol, f"[green]✓ repaired for {tx.get('totalPrice', tx.get('totalCost', 0)):,} cr[/green]")
    except SpaceTradersError as e:
        ship_log(ship_symbol, f"[red]Repair failed for {e}[/red]")


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
        ship_log(ship_symbol, f"[dim]in transit — deferring upgrade to next loop[/dim]")
        return
    tier = _best_mount_tier(ship)
    if tier >= len(MINING_MOUNT_TIERS) - 1:
        ship_log(ship_symbol, f"[dim]already has best mining mount ({MINING_MOUNT_TIERS[-1]})[/dim]")
        return
    target = MINING_MOUNT_TIERS[tier + 1]
    current = MINING_MOUNT_TIERS[tier] if tier >= 0 else "none"
    ship_log(ship_symbol, f"[bold]Upgrading {current} → {target}[/bold]")

    # 1. Find a market selling the mount item
    buy_wp = best_buy_waypoint(target)
    if not buy_wp:
        ship_log(ship_symbol, f"[yellow]no market found selling {target} — skipping upgrade[/yellow]")
        return

    # 2. Navigate ship to the market and purchase 1 mount
    navigate_with_refuel(ship_symbol, buy_wp)
    ensure_docked(ship_symbol)
    try:
        fleet_api.purchase_cargo(ship_symbol, target, 1)
        ship_log(ship_symbol, f"[dim]purchased {target} for upgrade[/dim]")
    except SpaceTradersError as e:
        ship_log(ship_symbol, f"[dim]could not purchase {target}: {e} — skipping upgrade[/dim]")
        return

    # 3. Navigate to shipyard and install
    navigate_with_refuel(ship_symbol, SHIPYARD_WP)
    ensure_docked(ship_symbol)
    try:
        result = fleet_api.install_mount(ship_symbol, target)
        ag = result.get("agent", {})
        ship_log(ship_symbol, f"[green]🔩 Installed {target} on {ship_symbol}! Credits: {ag.get('credits', 0):,}[/green]")
    except SpaceTradersError as e:
        ship_log(ship_symbol, f"[dim]Mount upgrade not available ({target} on {ship_symbol}): {e}[/dim]")


def step_scrap_pending() -> None:
    """Scrap any ships queued via the dashboard 'pending_scrap' DB setting.

    The dashboard writes a JSON list of ship symbols to the 'pending_scrap'
    bot_setting key.  This function navigates each to a shipyard, scraps it,
    and removes it from the list.  The command ship and fleet manager are
    never scrapped here.
    """
    raw = db.get_bot_setting("pending_scrap", "[]")
    try:
        queue: list[str] = json.loads(raw)
    except Exception:
        queue = []
    if not queue:
        return

    protected = {COMMAND_SHIP, FLEET_MANAGER_SHIP}
    scrapped: list[str] = []
    remaining: list[str] = list(queue)

    for sym in queue:
        if sym in protected:
            log(f"[yellow]Scrap queue: {sym} is protected (command/fleet-manager) — skipping[/yellow]")
            remaining.remove(sym)
            continue
        log(f"[bold yellow]🗑  Scrapping {sym} (queued via dashboard)[/bold yellow]")
        try:
            # Navigate to nearest shipyard for scrapping
            navigate_with_refuel(sym, SHIPYARD_WP)
            ensure_docked(sym)
            result = fleet_api.scrap(sym)
            ag = result.get("agent", {})
            tx = result.get("transaction", {})
            _scrap_val = tx.get('totalPrice', 0)
            log(
                f"[green]✅ Scrapped {sym} — received {_scrap_val:,} cr "
                f"| Credits: {ag.get('credits', 0):,}[/green]"
            )
            discord.send_scrap(sym, _scrap_val)
            scrapped.append(sym)
            remaining.remove(sym)
        except SpaceTradersError as e:
            log(f"[red]Scrap {sym} failed: {e}[/red]")
        finally:
            # Always persist the remaining queue so a crash doesn't re-queue
            db.set_bot_setting("pending_scrap", json.dumps(remaining))

    if scrapped:
        log(f"[dim]Scrapped {len(scrapped)} ship(s): {', '.join(scrapped)}[/dim]")


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


def has_siphon_mount(ship: dict) -> bool:
    """Return True if this ship has a gas siphon mount."""
    return any("GAS_SIPHON" in m.get("symbol", "") for m in ship.get("mounts", []))


def has_mineral_processor(ship: dict) -> bool:
    """Return True if this ship has an ore refinery module (ORE_REFINERY or MICRO_REFINERY)."""
    return any(
        "ORE_REFINERY" in m.get("symbol", "") or "MICRO_REFINERY" in m.get("symbol", "")
        for m in ship.get("modules", [])
    )


def has_gas_processor(ship: dict) -> bool:
    """Return True if this ship has a gas processor module."""
    return any("GAS_PROCESSOR" in m.get("symbol", "") for m in ship.get("modules", []))


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
            ship_log(ship_symbol, f"[red]💥 surveyor thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _surveyor_loop_inner(
    ship_symbol: str,
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    good = contract["terms"]["deliver"][0]["tradeSymbol"]
    _ship_role_tag[ship_symbol] = "Surveyor"
    ship_log(ship_symbol, f"[magenta]🔭 surveyor thread started[/magenta]")

    # Wait briefly for the lead miner to call choose_mining_target (race condition:
    # both threads start simultaneously; give the miner up to 30s to set the target).
    _wait_start = time.time()
    while not _active_mining_wp and time.time() - _wait_start < 30:
        time.sleep(1)
    survey_target = _active_mining_wp or ASTEROID

    # Preflight: get to ASTEROID_BASE for fuel, then to survey_target
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0  = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    if _fuel_pct0 < 0.90 and _wp0 != survey_target:
        _nearest = ASTEROID_BASE if _wp0 == ASTEROID_BASE else nearest_refuel_point(_wp0)
        ship_log(ship_symbol, f"[dim]surveyor preflight refuel at {_nearest}[/dim]")
        if _wp0 != _nearest:
            navigate_to(ship_symbol, _nearest)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

    # Validate the surveyor can physically reach survey_target on one cruise tank.
    # Small-tank ships (80 fuel) can't cruise to asteroids that have no fuel market
    # within range — they'd drift for hours.  Fall back to ASTEROID if unreachable.
    _sv_info = fleet_api.get_ship(ship_symbol)
    _sv_fuel_cap = _sv_info["fuel"].get("capacity", 0)
    if _sv_fuel_cap > 0 and survey_target != ASTEROID:
        _fuel_markets = _good_exporters.get("FUEL", [])
        _can_reach = any(
            waypoint_distance(survey_target, fm) <= _sv_fuel_cap * 0.90
            for fm in _fuel_markets
        )
        if not _can_reach:
            ship_log(ship_symbol,
                f"[yellow]🔭 surveyor: {survey_target} has no fuel market within "
                f"{_sv_fuel_cap} units — falling back to {ASTEROID}[/yellow]")
            survey_target = ASTEROID

    ship_log(ship_symbol, f"[magenta]🔭 surveyor heading to: {survey_target}[/magenta]")
    navigate_with_refuel(ship_symbol, survey_target)
    ensure_orbit(ship_symbol)

    while not stop_event.is_set() and not contract_done.is_set():
        # Re-check in case miners switched asteroids
        survey_target = _active_mining_wp or ASTEROID

        # Proactive fuel check: only refuel if NOT already at the asteroid.
        # Surveying costs no fuel, so let small ships survey before drifting back.
        _sv_ship = fleet_api.get_ship(ship_symbol)
        _sv_fuel = _sv_ship["fuel"]
        _sv_wp   = _sv_ship["nav"].get("waypointSymbol")
        _sv_at_asteroid = _sv_wp == survey_target
        if not _sv_at_asteroid and _sv_fuel.get("capacity", 0) > 0 and _sv_fuel["current"] / _sv_fuel["capacity"] < 0.50:
            ship_log(ship_symbol, f"[yellow]⛽ surveyor: fuel low, topping up[/yellow]")
            navigate_with_refuel(ship_symbol, ASTEROID_BASE)
            ensure_docked(ship_symbol)
            refuel_if_needed(ship_symbol, threshold=100_000)
            navigate_with_refuel(ship_symbol, survey_target)
            ensure_orbit(ship_symbol)
        elif not _sv_at_asteroid:
            # Miners may have moved — reposition
            ship_log(ship_symbol, f"[magenta]🔭 surveyor repositioning to {survey_target}[/magenta]")
            navigate_with_refuel(ship_symbol, survey_target)
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
            ship_log(ship_symbol, f"[yellow]surveyor error: {e}[/yellow]")
            if not stop_event.is_set():
                time.sleep(10)

    ship_log(ship_symbol, f"[dim]surveyor thread done[/dim]")


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
            ship_log(ship_symbol, f"[red]💥 hauler thread crashed: {e} — restarting in 30s[/red]")
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

    _ship_role_tag[ship_symbol] = "Hauler"
    ship_log(ship_symbol, f"[blue]🚛 hauler thread started | contract good: {good}[/blue]")

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
            ship_log(ship_symbol, f"[dim]🚛 waiting at asteroid ({_h_units}/{_h_capacity} cargo)[/dim]")
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
                                db.record_delivery(cid, good, ship_symbol, _to_deliver, _f, _req)
                                ship_log(ship_symbol, f"[green]✓ hauler: {_f}/{_req} {good} delivered[/green]")
                                if _f >= _req:
                                    with _fulfill_lock:
                                        if not contract_done.is_set():
                                            _res = contracts_api.fulfill_contract(cid)
                                            _ag  = _res.get("agent", {})
                                            _earned = _ag.get("credits", 0)
                                            db.record_credits(_earned)
                                            contract["fulfilled"] = True
                                            db.upsert_contract(contract, fulfilled_now=True)  # record real fulfillment timestamp
                                            log(f"[green bold]🏆 Contract fulfilled! Credits: {_earned:,}[/green bold]")
                                            discord.send_contract_finish(
                                                contract,
                                                contract["terms"]["payment"].get("onFulfilled", 0),
                                            )
                                            contract_done.set()
                                    return
                        break
            except SpaceTradersError as e:
                ship_log(ship_symbol, f"[yellow]hauler delivery error: {e}[/yellow]")

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

    ship_log(ship_symbol, f"[dim]hauler thread done[/dim]")


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
            ship_log(ship_symbol, f"[red]💥 miner thread crashed: {e} — restarting in 30s[/red]")
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

    _ship_role_tag[ship_symbol] = "Miner"
    ship_log(ship_symbol, f"[cyan]⚓ thread started | mining: {good} → {delivery_wp}[/cyan]")

    # Safe startup: if not near the asteroid or fuel < 50%, refuel at ASTEROID_BASE first
    _s0 = fleet_api.get_ship(ship_symbol)
    _wp0 = _s0["nav"]["waypointSymbol"]
    _f0 = _s0["fuel"]
    _fuel_pct0 = _f0["current"] / max(_f0["capacity"], 1) if _f0.get("capacity", 0) > 0 else 1.0
    _small_tank = _f0.get("capacity", 0) <= 80
    # Small-tank ships (≤80 cap) must always start fully fuelled — they can't reach
    # repair yards or delivery waypoints from the asteroid on a partial tank.
    _needs_preflight_refuel = _fuel_pct0 < 0.90 or (_small_tank and _fuel_pct0 < 1.0)
    if _needs_preflight_refuel and _wp0 != mining_target:
        ship_log(ship_symbol, f"[dim]preflight refuel at {ASTEROID_BASE} (wp={_wp0}, fuel={_f0['current']}/{_f0['capacity']})[/dim]")
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
                        ship_log(ship_symbol, f"[cyan]preflight shortcut — has {_have_preflight}x {good}, need {_remaining_pre} — delivering now[/cyan]")
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
                                db.record_delivery(cid, good, ship_symbol, _deliver_amt, f, req)
                                ship_log(ship_symbol, f"[green]✓ {f}/{req} {good} delivered (preflight)[/green]")
                                if f >= req:
                                    with _fulfill_lock:
                                        if not contract_done.is_set():
                                            res = contracts_api.fulfill_contract(cid)
                                            ag = res.get("agent", {})
                                            db.record_credits(ag.get("credits", 0))
                                            contract["fulfilled"] = True
                                            db.upsert_contract(contract, fulfilled_now=True)  # record real fulfillment timestamp
                                            log(f"[green bold]🏆 Contract fulfilled! Credits: {ag.get('credits', 0):,}[/green bold]")
                                            contract_done.set()
                                    return
                    break
        except SpaceTradersError as e:
            ship_log(ship_symbol, f"[dim]preflight delivery check failed: {e}[/dim]")

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
        ship_log(ship_symbol, f"[yellow]{good} requires {sorted(_required_traits)} — not present in any scanned asteroid, skipping straight to buy[/yellow]")

    # Also force buy if this ship was explicitly assigned as the contract buyer
    # (e.g. solo miner used as fallback — buying is faster than mining alone).
    _forced_buyer = ship_symbol in _contract_buy_ships

    # Ships with no mining mount (e.g. haulers assigned to contract delivery)
    # must never attempt extraction — treat them as forced buyers unconditionally.
    _ship_check = fleet_api.get_ship(ship_symbol)
    if not has_mining_mount(_ship_check):
        _forced_buyer = True

    _direct_buy = bool(
        _buy_wp_check
        and (
            not _is_mineable
            or _no_deposit
            or (_buy_price_check > 0 and _buy_price_check <= CHEAP_BUY_THRESHOLD)
            or _forced_buyer
        )
    )
    if _direct_buy:
        _reason = (
            "forced buyer — solo miner buys instead of mines" if _forced_buyer and _is_mineable and not _no_deposit
            else "no deposit in system" if _no_deposit
            else "cheap ore" if _is_mineable
            else "non-mineable good"
        )
        ship_log(ship_symbol, f"[cyan]{good} will be purchased ({_reason} @ {_buy_price_check:,} cr/u)[/cyan]")
        # Non-mineable / no-deposit goods skip the asteroid entirely and go straight to market.
        # Forced buyers (solo miner assigned to buy) also skip the asteroid.
        # Cheap ores still visit the asteroid base (serves as a refuel waypoint en route).
        if (_no_deposit or not _is_mineable or _forced_buyer) and _buy_wp_check:
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
        _fuel_cap = _fuel.get("capacity", 0)
        _small_tank_loop = _fuel_cap <= 80
        # Small-tank ships use 60% threshold; normal ships use 40%.
        # This ensures 80-cap ships always have enough fuel to reach A2 (64+ units).
        _low_fuel_threshold = 0.60 if _small_tank_loop else 0.40
        if not _at_asteroid and not _at_buy_wp and _fuel_cap > 0 and _fuel["current"] / _fuel_cap < _low_fuel_threshold:
            ship_log(ship_symbol, f"[yellow]⛽ fuel low ({_fuel['current']}/{_fuel['capacity']}), topping up[/yellow]")
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
            ship_log(ship_symbol, f"[yellow]🔧 condition {_cond:.0%} below threshold — diverting to repair[/yellow]")
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
                        ship_log(ship_symbol, f"[cyan]📦 transferred {_transferred}u → {_avail_hauler}[/cyan]")
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
                            db.record_delivery(cid, good, ship_symbol, have, f, req)
                            ship_log(ship_symbol, f"[green]✓ {f}/{req} {good} delivered[/green]")
                            if f >= req:
                                with _fulfill_lock:
                                    if not contract_done.is_set():
                                        res = contracts_api.fulfill_contract(cid)
                                        ag = res.get("agent", {})
                                        db.record_credits(ag.get("credits", 0))
                                        contract["fulfilled"] = True
                                        db.upsert_contract(contract, fulfilled_now=True)  # record real fulfillment timestamp
                                        log(f"[green bold]🏆 Contract fulfilled! Credits: {ag.get('credits', 0):,}[/green bold]")
                                        contract_done.set()
                                return
                except SpaceTradersError as e:
                    if contract_done.is_set():
                        return
                    ship_log(ship_symbol, f"[yellow]delivery error: {e}[/yellow]")

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
                    ship_log(ship_symbol, f"[yellow]{_dry_extractions} consecutive dry extractions — escalating to buy mode early[/yellow]")
                    _empty_loads = 3
                    _dry_extractions = 0
                _empty_loads += 1
                if _direct_buy:
                    # Sell any junk cargo before heading to the buy market so all
                    # cargo slots are free for the contract good.
                    _ship_check = fleet_api.get_ship(ship_symbol)
                    _cargo_items = _ship_check.get("cargo", {}).get("inventory", [])
                    _has_junk = any(i["symbol"] != good for i in _cargo_items if i.get("units", 0) > 0)
                    if _has_junk:
                        sell_junk(ship_symbol, good)
                        navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                        ensure_docked(ship_symbol)
                        refuel_if_needed(ship_symbol, threshold=100_000)
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
                        ship_log(ship_symbol, f"[cyan]{_empty_loads} empty loads — switching to buy {good} from {_buy_wp}[/cyan]")
                        navigate_with_refuel(ship_symbol, _buy_wp)
                        ensure_docked(ship_symbol)
                        _market_cache_ts.pop(_buy_wp, None)  # force fresh query while docked
                        get_market_prices(_buy_wp)  # populate cache (needs ship present)
                        # Log and store full market snapshot
                        _market_raw = universe_api.get_market(SYSTEM, _buy_wp)
                        _trade_goods = _market_raw.get("tradeGoods", [])
                        db.log_market_visit(_buy_wp, ship_symbol, _trade_goods)
                        if _trade_goods:
                            _good_lines = "  ".join(
                                f"[cyan]{g['symbol']}[/cyan] buy=[green]{g.get('purchasePrice',0):,}[/green] sell=[yellow]{g.get('sellPrice',0):,}[/yellow] vol={g.get('tradeVolume','?')} supply={g.get('supply','?')}"
                                for g in sorted(_trade_goods, key=lambda x: x.get("symbol",""))
                            )
                            ship_log(ship_symbol, f"[dim]{_buy_wp} market ({len(_trade_goods)} goods):[/dim]")
                            for _g in sorted(_trade_goods, key=lambda x: x.get("symbol", "")):
                                ship_log(ship_symbol,
                                    f"  [cyan]{_g['symbol']:22s}[/cyan] "
                                    f"buy=[green]{_g.get('purchasePrice',0):>6,}[/green] "
                                    f"sell=[yellow]{_g.get('sellPrice',0):>6,}[/yellow] "
                                    f"vol={_g.get('tradeVolume','?'):>4}  supply={_g.get('supply','?')}"
                                )
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
                            # Respect per-transaction trade volume limit from market data.
                            _trade_volume = _market_cache.get(_buy_wp, {}).get(f"_vol_{good}", 10000)
                            if _to_buy > 0:
                                _bought_total = 0
                                _buy_failed = False
                                _remaining_to_buy = _to_buy
                                _batch_limit = _trade_volume  # shrinks on 4604 error
                                while _remaining_to_buy > 0 and not _buy_failed:
                                    _this_batch = min(_remaining_to_buy, _batch_limit)
                                    try:
                                        result = fleet_api.purchase_cargo(ship_symbol, good, _this_batch)
                                        ag = result.get("agent", {})
                                        tx = result.get("transaction", {})
                                        _bought_total += _this_batch
                                        _remaining_to_buy -= _this_batch
                                        ship_log(ship_symbol, f"[green]🛒 bought {_this_batch}x {good} @ {_buy_price:,} cr/u | Credits: {ag.get('credits',0):,}[/green]")
                                        db.log_transaction(_buy_wp, ship_symbol, good, "PURCHASE",
                                                           _this_batch, _buy_price, tx.get("totalPrice", 0))
                                    except SpaceTradersError as e:
                                        if e.code == 4604 and _batch_limit > 1:
                                            # Transaction limit hit — halve the batch and retry
                                            _batch_limit = max(1, _batch_limit // 2)
                                            ship_log(ship_symbol, f"[dim]buy batch too large — retrying at {_batch_limit}u/tx[/dim]")
                                        else:
                                            ship_log(ship_symbol, f"[yellow]purchase failed: {e}[/yellow]")
                                            _buy_failed = True
                                if _bought_total > 0:
                                    _empty_loads = 3  # stay in buy-mode; next junk load re-triggers buy
                                    continue  # cargo now has the good — deliver path fires next iteration
                            else:
                                _affordable_str = f"{_affordable}u affordable" if _buy_price > 0 else "price unknown"
                                ship_log(ship_symbol, f"[yellow]can't buy {good} — {_affordable_str} (credits: {_me['credits']:,}, reserve: {_buy_reserve:,})[/yellow]")
                                if _direct_buy:
                                    # Credits too low to buy even 1 unit — fall back to mining
                                    # for income so we don't tight-loop or stall the contract.
                                    ship_log(ship_symbol, f"[yellow]mining for income to fund direct-buy contract[/yellow]")
                                    _empty_loads = 0  # exit buy mode; mine 3 loads then retry
                                    navigate_with_refuel(ship_symbol, mining_target)
                                    ensure_orbit(ship_symbol)
                                    active_survey = (_get_shared_survey(good) if _use_shared_surveys else None) or try_survey(ship_symbol, good)
                        else:
                            ship_log(ship_symbol, f"[yellow]{good} not found in {_buy_wp} tradeGoods — blacklisting for 20 min[/yellow]")
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
                            ship_log(ship_symbol, f"[red bold]{good} cannot be mined or purchased after {_empty_loads} loads — contract unworkable, exiting[/red bold]")
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
                        ship_log(ship_symbol, f"[dim]Survey expired, trying shared pool[/dim]")
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
                ship_log(ship_symbol, f"  [yellow]⚠ {ev.get('name')}: {ev.get('description', '')}[/yellow]")

        except SpaceTradersError as e:
            if e.code == 4228:  # cargo full — handled at top of loop
                pass
            elif e.code == 4236:  # not in orbit — dock/sell left ship docked; re-orbit and retry
                ship_log(ship_symbol, f"[yellow]extract: not in orbit, re-orbiting...[/yellow]")
                ensure_orbit(ship_symbol)
            elif e.code == 4205:  # wrong waypoint type — navigate back to asteroid
                ship_log(ship_symbol, f"[yellow]extract: wrong location, navigating to asteroid...[/yellow]")
                navigate_with_refuel(ship_symbol, mining_target)
                ensure_orbit(ship_symbol)
            elif e.code == 4243:  # no mining laser — this ship can't mine, exit permanently
                ship_log(ship_symbol, f"[red]⛔ {ship_symbol} has no mining laser — exiting miner loop (wrong ship type)[/red]")
                return
            else:
                ship_log(ship_symbol, f"[red]extract error: {e}[/red]")
                time.sleep(5)

    ship_log(ship_symbol, f"[dim]miner thread done[/dim]")


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
    _ship_role_tag[ship_symbol] = "Siphoner"
    ship_log(ship_symbol, f"[blue]🌀 siphon thread started[/blue]")
    while not stop_event.is_set():
        try:
            _siphon_loop_inner(ship_symbol, stop_event)
        except Exception as e:
            import traceback
            ship_log(ship_symbol, f"[red]💥 siphon thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _get_my_group(ship_symbol: str) -> dict | None:
    """Return the group dict this ship belongs to (as worker), or None."""
    with _ship_groups_lock:
        for g in _ship_groups:
            if ship_symbol in g.get("workers", []):
                return g
    return None


def _siphon_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    gas_giants = _find_gas_giants()
    if not gas_giants:
        ship_log(ship_symbol, f"[yellow]no gas giants in {SYSTEM} — siphon loop idle (retry in 10min)[/yellow]")
        stop_event.wait(600)
        return

    # Pick closest gas giant by coordinate distance from ASTEROID_BASE
    target_gg = min(
        gas_giants,
        key=lambda wp: waypoint_distance(wp, ASTEROID_BASE),
    )
    ship_log(ship_symbol, f"[blue]🌀 targeting gas giant {target_gg}[/blue]")
    # Prefuel at current waypoint if it has a market, so we can make hops
    _pre_ship = fleet_api.get_ship(ship_symbol)
    _pre_wp   = _pre_ship["nav"]["waypointSymbol"]
    _pre_fuel = _pre_ship["fuel"]
    if _pre_fuel.get("capacity", 0) > 0 and _pre_fuel.get("current", 0) < _pre_fuel["capacity"]:
        if _pre_wp in (_good_exporters.get("FUEL", []) + _good_buyers.get("FUEL", [])):
            try:
                ensure_docked(ship_symbol)
                refuel_if_needed(ship_symbol, threshold=100_000)
                ensure_orbit(ship_symbol)
                ship_log(ship_symbol, f"[dim]preflight refuel at {_pre_wp}[/dim]")
            except SpaceTradersError:
                pass
    navigate_with_refuel(ship_symbol, target_gg)
    ensure_orbit(ship_symbol)

    # Check if this worker is part of a group — if so, stay put and signal the hauler.
    in_group = ship_symbol in _group_worker_ready

    while not stop_event.is_set():
        wait_cooldown(ship_symbol)
        ship_data = fleet_api.get_ship(ship_symbol)
        cargo     = ship_data["cargo"]

        # Anti-clog: jettison unsellable goods before the hold fills so we
        # don't waste a hauler pickup slot on worthless items.
        if cargo["units"] > 0:
            for item in ship_data["cargo"].get("inventory", []):
                sym = item["symbol"]
                best_price = max(
                    (get_market_prices(wp).get(sym, 0) for wp in (_known_markets or [ASTEROID_BASE])),
                    default=0,
                )
                has_buyer = any(wp in (_known_markets or []) for wp in _good_buyers.get(sym, []))
                if best_price < MIN_SELL_PRICE and not has_buyer:
                    try:
                        fleet_api.jettison(ship_symbol, sym, item["units"])
                        ship_log(ship_symbol,
                            f"[dim]🗑️  Jettisoned {item['units']}x {sym} ({best_price} cr/u)[/dim]")
                    except SpaceTradersError:
                        pass
            # Re-fetch after potential jettisons
            cargo = fleet_api.get_ship(ship_symbol)["cargo"]

        if cargo["units"] >= cargo["capacity"]:
            if in_group:
                # Signal hauler and wait for it to come collect
                _group_worker_ready[ship_symbol].set()
                ship_log(ship_symbol, f"[blue]🌀 cargo full — waiting for hauler pickup[/blue]")
                while not stop_event.is_set() and _group_worker_ready[ship_symbol].is_set():
                    stop_event.wait(10)
                # After pickup, re-check position (hauler may have moved us/cargo is clear)
                continue
            else:
                # Solo mode — try fromCargo refuel first, then sell everything and return
                refuel_from_cargo(ship_symbol)
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
            ship_log(ship_symbol, f"[yellow]siphon error: {e}[/yellow]")
            stop_event.wait(15)


def _sell_siphon_goods(ship_symbol: str) -> None:
    """Sell all siphoned cargo at the best available markets."""
    ship_data = fleet_api.get_ship(ship_symbol)
    inventory = ship_data["cargo"].get("inventory", [])
    if not inventory:
        return
    # sell_junk handles routing and picking the best market for each good
    sell_junk(ship_symbol, "__NONE__")


# ── Group hauler loops ────────────────────────────────────────────────────────

def siphon_hauler_loop(
    ship_symbol: str,
    workers: list[str],
    stop_event: threading.Event,
) -> None:
    """Tender hauler: parks at gas giant, collects cargo from siphon drones, sells it."""
    _ship_role_tag[ship_symbol] = "SiphHauler"
    ship_log(ship_symbol, f"[cyan]🚢 siphon-hauler thread started (workers: {[s.rsplit('-',1)[-1] for s in workers]})[/cyan]")
    while not stop_event.is_set():
        try:
            _siphon_hauler_inner(ship_symbol, workers, stop_event)
        except Exception as e:
            import traceback
            ship_log(ship_symbol, f"[red]💥 siphon-hauler crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _siphon_hauler_inner(
    ship_symbol: str,
    workers: list[str],
    stop_event: threading.Event,
) -> None:
    gas_giants = _find_gas_giants()
    if not gas_giants:
        ship_log(ship_symbol, f"[yellow]no gas giants — siphon-hauler idle (retry 10min)[/yellow]")
        stop_event.wait(600)
        return

    target_gg = min(gas_giants, key=lambda wp: waypoint_distance(wp, ASTEROID_BASE))
    ship_log(ship_symbol, f"[cyan]🚢 hauler heading to gas giant {target_gg}[/cyan]")
    navigate_with_refuel(ship_symbol, target_gg)

    while not stop_event.is_set():
        hauler_data = fleet_api.get_ship(ship_symbol)
        hauler_cargo = hauler_data["cargo"]
        hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

        # Collect from any worker that has signalled it's full
        collected = False
        for worker in workers:
            if stop_event.is_set():
                break
            evt = _group_worker_ready.get(worker)
            if evt is None or not evt.is_set():
                continue

            # Worker is ready — get to the same location and transfer
            worker_data = fleet_api.get_ship(worker)
            worker_wp   = worker_data["nav"]["waypointSymbol"]
            worker_cargo = worker_data["cargo"]
            if worker_cargo["units"] == 0:
                evt.clear()
                continue

            if hauler_data["nav"]["waypointSymbol"] != worker_wp:
                navigate_with_refuel(ship_symbol, worker_wp)
                hauler_data = fleet_api.get_ship(ship_symbol)
                hauler_cargo = hauler_data["cargo"]
                hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

            if hauler_space == 0:
                # Hauler is full — sell before collecting more
                break

            ensure_orbit(ship_symbol)
            # Transfer each good individually (API requires per-good calls)
            inventory = worker_data["cargo"].get("inventory", [])
            for item in inventory:
                xfr_units = min(item["units"], hauler_space)
                if xfr_units <= 0:
                    continue
                try:
                    fleet_api.transfer_cargo(worker, item["symbol"], xfr_units, ship_symbol)
                    ship_log(ship_symbol,
                        f"[cyan]📦 transferred {xfr_units}x {item['symbol']} from {worker}[/cyan]")
                    hauler_space -= xfr_units
                    collected = True
                except SpaceTradersError as e:
                    ship_log(ship_symbol, f"[yellow]transfer error from {worker}: {e}[/yellow]")
                if hauler_space <= 0:
                    break

            # Clear ready flag so worker resumes siphoning
            evt.clear()

        # If hauler is full, or no worker is ready and hauler has cargo, go sell
        hauler_data  = fleet_api.get_ship(ship_symbol)
        hauler_cargo = hauler_data["cargo"]
        if hauler_cargo["units"] > 0:
            hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]
            workers_have_cargo = any(
                fleet_api.get_ship(w)["cargo"]["units"] > 0 for w in workers
            )
            if hauler_space == 0 or (not workers_have_cargo and not collected):
                ship_log(ship_symbol,
                    f"[cyan]🚢 hauler cargo {hauler_cargo['units']}/{hauler_cargo['capacity']} — heading to sell[/cyan]")
                # Refuel from cargo (HYDROCARBON→FUEL if available) before the run home
                refuel_from_cargo(ship_symbol)
                # Refine HYDROCARBON→FUEL and ores at the sell waypoint before selling
                navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                refine_cargo_for_sale(ship_symbol)
                _sell_siphon_goods(ship_symbol)
                # Return to gas giant for next round
                navigate_with_refuel(ship_symbol, target_gg)
                continue

        if not collected:
            stop_event.wait(15)


def miner_hauler_loop(
    ship_symbol: str,
    workers: list[str],
    stop_event: threading.Event,
) -> None:
    """Tender hauler: parks at asteroid, collects ore from mining drones, sells it."""
    _ship_role_tag[ship_symbol] = "MineHauler"
    ship_log(ship_symbol, f"[cyan]🚢 miner-hauler thread started (workers: {[s.rsplit('-',1)[-1] for s in workers]})[/cyan]")
    while not stop_event.is_set():
        try:
            _miner_hauler_inner(ship_symbol, workers, stop_event)
        except Exception as e:
            import traceback
            ship_log(ship_symbol, f"[red]💥 miner-hauler crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _miner_hauler_inner(
    ship_symbol: str,
    workers: list[str],
    stop_event: threading.Event,
) -> None:
    navigate_with_refuel(ship_symbol, ASTEROID)
    ensure_orbit(ship_symbol)
    ship_log(ship_symbol, f"[cyan]🚢 miner-hauler at asteroid {ASTEROID}[/cyan]")

    while not stop_event.is_set():
        hauler_data  = fleet_api.get_ship(ship_symbol)
        hauler_cargo = hauler_data["cargo"]
        hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

        collected = False
        for worker in workers:
            if stop_event.is_set():
                break
            evt = _group_worker_ready.get(worker)
            if evt is None or not evt.is_set():
                continue

            worker_data  = fleet_api.get_ship(worker)
            worker_cargo = worker_data["cargo"]
            if worker_cargo["units"] == 0:
                evt.clear()
                continue

            worker_wp = worker_data["nav"]["waypointSymbol"]
            if hauler_data["nav"]["waypointSymbol"] != worker_wp:
                navigate_with_refuel(ship_symbol, worker_wp)
                hauler_data  = fleet_api.get_ship(ship_symbol)
                hauler_cargo = hauler_data["cargo"]
                hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]

            if hauler_space == 0:
                break

            ensure_orbit(ship_symbol)
            for item in worker_data["cargo"].get("inventory", []):
                xfr_units = min(item["units"], hauler_space)
                if xfr_units <= 0:
                    continue
                try:
                    fleet_api.transfer_cargo(worker, item["symbol"], xfr_units, ship_symbol)
                    ship_log(ship_symbol,
                        f"[cyan]📦 transferred {xfr_units}x {item['symbol']} from {worker}[/cyan]")
                    hauler_space -= xfr_units
                    collected = True
                except SpaceTradersError as e:
                    ship_log(ship_symbol, f"[yellow]transfer error from {worker}: {e}[/yellow]")
                if hauler_space <= 0:
                    break
            evt.clear()

        hauler_data  = fleet_api.get_ship(ship_symbol)
        hauler_cargo = hauler_data["cargo"]
        if hauler_cargo["units"] > 0:
            hauler_space = hauler_cargo["capacity"] - hauler_cargo["units"]
            workers_have_cargo = any(
                fleet_api.get_ship(w)["cargo"]["units"] > 0 for w in workers
            )
            if hauler_space == 0 or (not workers_have_cargo and not collected):
                ship_log(ship_symbol,
                    f"[cyan]🚢 hauler cargo {hauler_cargo['units']}/{hauler_cargo['capacity']} — heading to sell[/cyan]")
                # Refine ores before selling
                navigate_with_refuel(ship_symbol, ASTEROID_BASE)
                refine_cargo_for_sale(ship_symbol)
                sell_junk(ship_symbol, "__NONE__")
                navigate_with_refuel(ship_symbol, ASTEROID)
                ensure_orbit(ship_symbol)
                continue

        if not collected:
            stop_event.wait(15)


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
    _ship_role_tag[ship_symbol] = "Trader"
    ship_log(ship_symbol, f"[magenta]💹 trader thread started[/magenta]")
    while not stop_event.is_set():
        try:
            _trader_loop_inner(ship_symbol, stop_event)
        except Exception as e:
            import traceback
            ship_log(ship_symbol, f"[red]💥 trader thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            # Release any route claim this ship held so other traders can use it
            with _route_lock:
                leaked = [g for g, s in list(_claimed_routes.items()) if s == ship_symbol]
                for g in leaked:
                    _claimed_routes.pop(g, None)
                    ship_log(ship_symbol, f"[dim]released leaked route claim for {g}[/dim]")
            stop_event.wait(30)


def _trader_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        opps = db.get_arbitrage_opportunities(SYSTEM, min_margin=TRADER_MIN_MARGIN)

        # Filter: positive buy price only (absolute margin already enforced by DB query)
        viable = [
            o for o in opps
            if o["buy_price"] > 0
        ]

        # Skip the active contract good — let the hauler handle it uncontested
        with _active_contract_lock:
            _deliver = (_active_contract or {}).get("terms", {}).get("deliver", [])
            _contract_good = _deliver[0]["tradeSymbol"] if _deliver else None
        if _contract_good:
            viable = [o for o in viable if o["trade_symbol"] != _contract_good]

        if not viable:
            # ── Idle scout: visit unscanned/stale markets instead of sitting idle ──
            _scout_target: str | None = None
            _scout_listing_count = 0
            with db._conn() as _scout_con:
                _scout_rows = _scout_con.execute(
                    """
                    SELECT ml.waypoint_symbol,
                           COUNT(*) AS listing_count,
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
                    LIMIT  5
                    """,
                    (f"{SYSTEM}-%", time.time() - MARKET_SCAN_STALENESS),
                ).fetchall()
            with _scout_lock:
                _now = time.time()
                for _scout_row in _scout_rows:
                    _wp = _scout_row[0]
                    if _wp not in _scout_claimed and _now >= _scout_skip.get(_wp, 0):
                        _scout_target = _wp
                        _scout_listing_count = _scout_row[1]
                        _scout_claimed.add(_scout_target)
                        break

            if _scout_target:
                ship_log(ship_symbol, f"[dim]🔍 no arbitrage — scouting {_scout_target} ({_scout_listing_count} listings)[/dim]")
                try:
                    navigate_with_refuel(ship_symbol, _scout_target)
                    ensure_docked(ship_symbol)
                    _market_cache_ts.pop(_scout_target, None)  # force fresh API call with ship present
                    _scout_prices = get_market_prices(_scout_target)
                    _scout_goods  = len([k for k in _scout_prices if not k.startswith("_")])
                    ship_log(ship_symbol, f"[dim]🔍 {_scout_target}: {_scout_goods} goods priced[/dim]")
                    if _scout_goods == 0:
                        # Market returned no live trade data — skip for 2h to avoid spin loop
                        with _scout_lock:
                            _scout_skip[_scout_target] = time.time() + MARKET_SCAN_STALENESS
                finally:
                    with _scout_lock:
                        _scout_claimed.discard(_scout_target)
                continue  # re-evaluate arbitrage immediately after scanning

            ship_log(ship_symbol, f"[dim]no profitable arbitrage found — waiting 5min[/dim]")
            stop_event.wait(300)
            continue

        best = viable[0]  # already sorted by margin DESC
        good  = best["trade_symbol"]
        buy_wp  = best["buy_at"]
        sell_wp = best["sell_at"]

        # ── Route diversification: skip goods already claimed by another trader ──
        # Each trader picks the best *unclaimed* route so they don't pile onto
        # the same market and saturate it with identical buys.
        _route_claimed = False
        with _route_lock:
            for _cand in viable:
                if _cand["trade_symbol"] not in _claimed_routes:
                    best    = _cand
                    good    = _cand["trade_symbol"]
                    buy_wp  = _cand["buy_at"]
                    sell_wp = _cand["sell_at"]
                    _claimed_routes[good] = ship_symbol
                    _route_claimed = True
                    break
            else:
                # All routes are claimed — fall back to the top option
                best    = viable[0]
                good    = best["trade_symbol"]
                buy_wp  = best["buy_at"]
                sell_wp = best["sell_at"]

        def _release_route() -> None:
            if _route_claimed:
                with _route_lock:
                    _claimed_routes.pop(good, None)

        _trip_id = uuid.uuid4().hex[:8]  # groups BUY + SELL for this run

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
            _pre_cost_basis = _pre_opps[0]["buy_price"] if _pre_opps else 0
        else:
            _pre_cost_basis = 0
        _pre_have = sum(i["units"] for i in _pre_inv if i["symbol"] == good) if _pre_inv else 0
        if _pre_have > 0:
            ship_log(ship_symbol, f"[dim]already carrying {_pre_have}x {good} — skipping buy, going to sell @ {sell_wp}[/dim]")
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
                        else:
                            # Stop selling if price crashes below cost basis (or 50% of first price if cost unknown)
                            _pre_floor = _pre_cost_basis if _pre_cost_basis > 0 else int(_sell_first_price * 0.50)
                            if _sell_price < _pre_floor:
                                ship_log(ship_symbol,
                                    f"[red]🚨 {good} price crashed below cost: {_sell_price:,}/u < floor {_pre_floor:,}/u — stopping sell[/red]")
                                _sell_failed = True
                                break
                        _sell_total_rev += tx.get("totalPrice", 0)
                        _sell_remaining -= _this_sell
                        if _sell_remaining > 0:
                            ship_log(ship_symbol, f"[magenta]💰 sold {_this_sell}x {good} @ {_sell_price:,}/u | Credits: {ag.get('credits', 0):,}[/magenta]")
                        db.log_transaction(sell_wp, ship_symbol, good, "SELL", _this_sell, _sell_price, tx.get("totalPrice", 0), trip_id=_trip_id)
                    except SpaceTradersError as e:
                        import re as _re
                        _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                        if _lm and _sell_batch_size > 1:
                            _sell_batch_size = int(_lm.group(1))
                            ship_log(ship_symbol, f"[dim]adjusting sell batch to {_sell_batch_size}/tx[/dim]")
                            continue
                        ship_log(ship_symbol, f"[yellow]sell {good} failed: {e}[/yellow]")
                        _sell_failed = True
                        sell_junk(ship_symbol, "__NONE__")
                        break
                if not _sell_failed and _sell_total_rev > 0:
                    ship_log(ship_symbol, f"[magenta]💰 sold {have}x {good} @ {_sell_price:,}/u = {_sell_total_rev:,} cr[/magenta]")
            _release_route()
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
            _release_route()
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
            ship_log(ship_symbol, f"[yellow]{good} not available at {buy_wp} — re-scanning[/yellow]")
            _release_route()
            continue

        # Re-score opportunities with the freshly updated DB prices
        _live_margin = best["sell_price"] - live_buy
        _live_roi    = _live_margin / live_buy if live_buy > 0 else 0
        if _live_margin < TRADER_MIN_MARGIN:
            log(
                f"[yellow]{ship_symbol}: {good} margin shrank to {_live_margin:,}/u "
                f"({_live_roi:.0%} ROI) at live price {live_buy:,} — re-scanning[/yellow]"
            )
            _release_route()
            continue
        # Check if a different route is now clearly better after cache refresh
        # Must apply the same contract-good exclusion as the main `viable` filter,
        # otherwise the contract good (e.g. SHIP_PLATING) always appears as #1 here
        # even though it's excluded from `viable`, causing an infinite re-route loop.
        _fresh_opps = [
            o for o in db.get_arbitrage_opportunities(SYSTEM, min_margin=TRADER_MIN_MARGIN)
            if o["buy_price"] > 0
            and (_contract_good is None or o["trade_symbol"] != _contract_good)
        ]
        if _fresh_opps and _fresh_opps[0]["trade_symbol"] != good:
            _alt = _fresh_opps[0]
            if _alt["margin"] > _live_margin * 1.25:  # only switch if 25% better
                log(
                    f"[cyan]{ship_symbol}: better route after cache refresh — "
                    f"{_alt['trade_symbol']} +{_alt['margin']:,}/u > "
                    f"{good} +{_live_margin:,}/u — re-routing[/cyan]"
                )
                _release_route()
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
            ship_log(ship_symbol, f"[yellow]can't afford even 1u at live price {live_buy:,}[/yellow]")
            _release_route()
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
                                   _this_batch, live_buy, tx.get("totalPrice", 0), trip_id=_trip_id)
            except SpaceTradersError as e:
                err_str = str(e)
                # Detect per-transaction limit error and retry with the limit as batch size
                import re as _re
                _limit_match = _re.search(r"limit of (\d+) units per transaction", err_str)
                if _limit_match and _batch_size > 1:
                    _batch_size = int(_limit_match.group(1))
                    ship_log(ship_symbol, f"[dim]adjusting batch size to {_batch_size}/tx for {good}[/dim]")
                    continue  # retry with smaller batch
                ship_log(ship_symbol, f"[yellow]purchase failed: {e}[/yellow]")
                _buy_failed = True
                break
        if _buy_failed and _bought_total == 0:
            _release_route()
            continue

        if _bought_total > 0:
            discord.send_trade_start(
                ship_symbol, good, buy_wp, sell_wp,
                _bought_total, live_buy,
                _bought_total * (best["sell_price"] - live_buy),
            )

        # ── Sell ─────────────────────────────────────────────────────────────
        navigate_with_refuel(ship_symbol, sell_wp)
        ensure_docked(ship_symbol)
        refuel_if_needed(ship_symbol, threshold=100_000)

        # Pre-sell price check: refuse to sell below cost, poll every 90s, and
        # actively search for an alternative sell market if this one is depressed.
        def _refresh_sell_price(wp: str) -> int:
            _market_cache_ts.pop(wp, None)
            get_market_prices(wp)
            return _market_cache.get(wp, {}).get(good, 0)

        _live_sell_price = _refresh_sell_price(sell_wp)
        _presell_margin  = (_live_sell_price - live_buy) if _live_sell_price > 0 else 1  # unknown = optimistic

        if _live_sell_price > 0 and _presell_margin < 0:
            # Price is below cost — poll for recovery, try alternate market after 3 min
            ship_log(ship_symbol,
                f"[red]🚨 sell price BELOW COST: {good} @ {_live_sell_price:,}/u "
                f"(paid {live_buy:,}/u, loss {_presell_margin:+,}/u) — polling for recovery[/red]")
            _recovery_polls = 0
            while _recovery_polls < 20 and not stop_event.is_set():
                stop_event.wait(90)
                _live_sell_price = _refresh_sell_price(sell_wp)
                _presell_margin  = (_live_sell_price - live_buy) if _live_sell_price > 0 else 1
                if _live_sell_price <= 0 or _presell_margin >= 0:
                    ship_log(ship_symbol, f"[dim]sell price recovered: {good} @ {_live_sell_price:,}/u[/dim]")
                    break
                _recovery_polls += 1
                ship_log(ship_symbol,
                    f"[yellow]⏳ still below cost ({_recovery_polls}/20): {good} @ {_live_sell_price:,}/u "
                    f"vs paid {live_buy:,}/u[/yellow]")
                # After ~3min, look for a better sell market
                if _recovery_polls == 2:
                    _alt_wp, _alt_price = best_sell_waypoint(good)
                    if _alt_wp and _alt_wp != sell_wp and (_alt_price - live_buy) >= 0:
                        ship_log(ship_symbol,
                            f"[cyan]↩ switching sell market: {_alt_wp} @ {_alt_price:,}/u "
                            f"(margin {_alt_price - live_buy:+,}/u)[/cyan]")
                        sell_wp = _alt_wp
                        navigate_with_refuel(ship_symbol, sell_wp)
                        ensure_docked(ship_symbol)
                        refuel_if_needed(ship_symbol, threshold=100_000)
                        _live_sell_price = _refresh_sell_price(sell_wp)
                        _presell_margin  = (_live_sell_price - live_buy) if _live_sell_price > 0 else 1
                        break  # commit to this market
            if _live_sell_price > 0 and _presell_margin < 0:
                ship_log(ship_symbol,
                    f"[red]⚠ no profitable market found after recovery attempts — "
                    f"selling {good} @ {_live_sell_price:,}/u (loss {_presell_margin:+,}/u)[/red]")
        elif _live_sell_price > 0 and _presell_margin < TRADER_MIN_MARGIN:
            # Above cost but below target margin — short wait then proceed
            ship_log(ship_symbol,
                f"[yellow]⏳ margin thin: {good} @ {_live_sell_price:,}/u "
                f"(margin {_presell_margin:+,}/u, target {TRADER_MIN_MARGIN:,}) — waiting 2min[/yellow]")
            stop_event.wait(120)
            _live_sell_price = _refresh_sell_price(sell_wp)

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
                    # Stop selling if price drops below what we paid (selling at a loss)
                    elif _sell_price < live_buy:
                        ship_log(ship_symbol,
                            f"[red]🚨 {good} price crashed below cost: {_sell_price:,}/u < paid {live_buy:,}/u — stopping sell[/red]")
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
                                       _this_sell, _sell_price, tx.get("totalPrice", 0), trip_id=_trip_id)
                except SpaceTradersError as e:
                    err_str = str(e)
                    import re as _re
                    _limit_match = _re.search(r"limit of (\d+) units per transaction", err_str)
                    if _limit_match and _sell_batch_size > 1:
                        _sell_batch_size = int(_limit_match.group(1))
                        ship_log(ship_symbol, f"[dim]adjusting sell batch size to {_sell_batch_size}/tx for {good}[/dim]")
                        continue
                    ship_log(ship_symbol, f"[yellow]sell {good} failed: {e}[/yellow]")
                    _sell_failed = True
                    sell_junk(ship_symbol, "__NONE__")
                    break
            if not _sell_failed and _sell_total_rev > 0:
                actual_profit = _sell_total_rev - (live_buy * have)
                log(
                    f"[magenta]💰 {ship_symbol}: sold {have}x {good} @ {_sell_price:,}/u "
                    f"= {_sell_total_rev:,} cr (profit: +{actual_profit:,} cr)[/magenta]"
                )
                db.log_trade_trip(
                    _trip_id, ship_symbol, good, buy_wp, sell_wp,
                    have, live_buy * have, _sell_total_rev,
                )
                discord.send_trade_finish(
                    ship_symbol, good, buy_wp, sell_wp,
                    have, live_buy * have, _sell_total_rev, actual_profit,
                )

        # Route is complete — release the claim so other traders can use this good again
        _release_route()

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
                    _bh_trip_id = uuid.uuid4().hex[:8]  # separate trip_id for backhaul leg
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
                            ship_log(ship_symbol, f"[magenta]🛒 bought {_bh_this}x {_bh['trade_symbol']} @ {_bh['buy_price']:,}/u | Credits: {ag.get('credits', 0):,}[/magenta]")
                            db.log_transaction(sell_wp, ship_symbol, _bh["trade_symbol"], "PURCHASE", _bh_this, _bh["buy_price"], tx.get("totalPrice", 0), trip_id=_bh_trip_id)
                        except SpaceTradersError as e:
                            import re as _re
                            _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                            if _lm and _bh_batch > 1:
                                _bh_batch = int(_lm.group(1))
                                continue
                            ship_log(ship_symbol, f"[yellow]backhaul buy failed: {e}[/yellow]")
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
                                        ship_log(ship_symbol, f"[magenta]💰 sold {_bh_st}x {_bh['trade_symbol']} @ {_bh_sp:,}/u[/magenta]")
                                    db.log_transaction(_bh["sell_at"], ship_symbol, _bh["trade_symbol"], "SELL", _bh_st, _bh_sp, tx.get("totalPrice", 0), trip_id=_bh_trip_id)
                                except SpaceTradersError as e:
                                    import re as _re
                                    _lm = _re.search(r"limit of (\d+) units per transaction", str(e))
                                    if _lm and _bh_sb > 1:
                                        _bh_sb = int(_lm.group(1))
                                        continue
                                    ship_log(ship_symbol, f"[yellow]backhaul sell failed: {e}[/yellow]")
                                    sell_junk(ship_symbol, "__NONE__")
                                    break
                            if _bh_rev > 0:
                                _bh_profit = _bh_rev - _bh['buy_price'] * _bh_have
                                log(
                                    f"[cyan]💰 {ship_symbol}: backhaul sold {_bh_have}x {_bh['trade_symbol']} "
                                    f"@ {_bh_sp:,}/u = {_bh_rev:,} cr "
                                    f"(profit: +{_bh_profit:,} cr)[/cyan]"
                                )
                                db.log_trade_trip(
                                    _bh_trip_id, ship_symbol, _bh["trade_symbol"],
                                    sell_wp, _bh["sell_at"],
                                    _bh_have,
                                    _bh["buy_price"] * _bh_have, _bh_rev,
                                    trip_type="backhaul",
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
            ship_log(ship_symbol, f"[red]💥 explorer thread crashed: {e} — restarting in 30s[/red]")
            log(f"[dim]{traceback.format_exc()}[/dim]")
            stop_event.wait(30)


def _probe_market_patrol(ship_symbol: str, stop_event: threading.Event) -> None:
    """Park at one assigned market and keep refreshing prices. Used for probes with no sensor array."""
    ship_log(ship_symbol, f"[dim]🌌 switching to market-patrol mode[/dim]")

    # Pick a permanent home market based on probe index so they spread evenly
    markets = list(_known_markets or [ASTEROID_BASE])
    n_probes = max(len(_explorer_symbols), 1)
    idx = _explorer_symbols.index(ship_symbol) if ship_symbol in _explorer_symbols else 0
    offset = (idx * max(1, len(markets) // n_probes)) % len(markets)
    home_market = markets[offset]

    # Navigate once, then stay docked and keep refreshing
    try:
        navigate_to(ship_symbol, home_market)
        ensure_docked(ship_symbol)
        ship_log(ship_symbol, f"[dim]🌌 parked at {home_market} — refreshing prices[/dim]")
    except Exception as e:
        ship_log(ship_symbol, f"[yellow]🌌 could not reach {home_market}: {e}[/yellow]")

    while not stop_event.is_set():
        try:
            _market_cache_ts.pop(home_market, None)
            get_market_prices(home_market)
        except Exception:
            pass
        stop_event.wait(MARKET_CACHE_TTL)


def _explorer_loop_inner(ship_symbol: str, stop_event: threading.Event) -> None:
    _ship_role_tag[ship_symbol] = "Explorer"
    ship_log(ship_symbol, f"[blue]🌌 explorer thread started[/blue]")

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
                if e.code == 4215:  # no sensor array — this is a probe, switch to patrol mode
                    _ship_no_sensor_logged.add(ship_symbol)
                    _probe_market_patrol(ship_symbol, stop_event)
                    return  # patrol loop runs until stop_event; then this thread exits cleanly
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

    ship_log(ship_symbol, f"[dim]explorer thread done[/dim]")


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
    # Count ALL hauler-role ships in fleet, not just _hauler_symbols (which excludes traders).
    current_haulers   = len([
        s for s in current_ships
        if s["registration"]["role"] in ("HAULER", "TRANSPORT")
        and s["symbol"] != FLEET_MANAGER_SHIP
    ])

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
        _auto_max_traders = max(1, len(_known_markets) // 5) if _known_markets else 3
        _hauler_cap = MAX_SIPHON_TEAMS + MAX_MINER_TEAMS + _auto_max_traders
        if stype in ("SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER") and current_haulers >= _hauler_cap:
            return False
        # SHIP_COMMAND_FRIGATE: ship_score() enforces the 1.5M credit gate; no static block here
        return True

    # Short-circuit: if every fleet cap is already met, no point touring.
    _KNOWN_SHIP_TYPES = [
        "SHIP_ORE_HOUND", "SHIP_MINING_DRONE", "SHIP_COMMAND_FRIGATE",
        "SHIP_SURVEYOR", "SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER",
        "SHIP_SIPHON_DRONE", "SHIP_PROBE",
    ]
    _eligible_types = set(t for t in _KNOWN_SHIP_TYPES if _should_buy(t))
    if not _eligible_types:
        log("[dim]Fleet manager: all fleet caps met — skipping shipyard tour[/dim]")
        return

    # Short-circuit: use last-known prices to skip the tour when we can't
    # afford the cheapest eligible ship yet.
    if _shipyard_price_cache:
        _min_price = min(
            (s["purchasePrice"] for ships in _shipyard_price_cache.values()
             for s in ships if s.get("type") in _eligible_types),
            default=None,
        )
        if _min_price is not None and credits - _min_price < CREDIT_RESERVE:
            log(
                f"[dim]Fleet manager: cheapest eligible ship ~{_min_price:,} cr, "
                f"only {credits:,} cr available — skipping tour[/dim]"
            )
            return

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
        _shipyard_price_cache[yd_wp] = shipyard.get("ships", [])  # refresh cache
        for s in shipyard.get("ships", []):
            stype = s.get("type", "")
            if not _should_buy(stype):
                continue
            fuel_cap = s.get("frame", {}).get("fuelCapacity", 9999)
            if fuel_cap < MIN_FUEL_CAPACITY:
                log(f"[yellow]Fleet manager: skipping {stype} at {yd_wp} — fuel tank too small ({fuel_cap} < {MIN_FUEL_CAPACITY})[/yellow]")
                continue
            all_candidates.append((
                ship_score(stype, current_miners, current_surveyors, current_haulers),
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
            current_miners = len(get_mining_ships())  # refresh after purchase
            current_haulers = len([
                s for s in fleet_api.get_my_ships()
                if s["registration"]["role"] in ("HAULER", "TRANSPORT")
                and s["symbol"] != FLEET_MANAGER_SHIP
            ])

            # Immediately launch the correct loop for the new ship
            if new_symbol and not contract_done.is_set():
                new_ship_data = fleet_api.get_ship(new_symbol)
                _new_role = new_ship_data["registration"]["role"]
                if ship_type in ("SHIP_SIPHON_DRONE", "SHIP_GAS_DRONE"):
                    _siphoner_symbols.append(new_symbol)
                    loop_target  = siphon_loop
                    thread_label = "siphoner"
                elif ship_type == "SHIP_PROBE" or new_ship_data["frame"].get("symbol") == "FRAME_PROBE":
                    _explorer_symbols.append(new_symbol)
                    loop_target  = explorer_loop
                    thread_label = "explorer"
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


MARKET_SCAN_STALENESS = 7_200   # rescan a market if data is older than 2h


def _bg_scan_next_market() -> None:
    """Visit the highest-priority unscanned or stale market and refresh its prices.

    Priority: markets with no data at all first, then by listing count (more
    goods → more arbitrage candidates), then by age of existing data.
    """
    with db._conn() as con:
        rows = con.execute(
            """
            SELECT ml.waypoint_symbol,
                   COUNT(*)                        AS listing_count,
                   COALESCE(MAX(mp.last_updated), 0) AS newest_price
            FROM   market_listings ml
            LEFT JOIN market_prices mp
                   ON ml.waypoint_symbol = mp.waypoint_symbol
            WHERE  ml.waypoint_symbol LIKE ?
            GROUP  BY ml.waypoint_symbol
            HAVING newest_price < ?
            ORDER  BY (newest_price = 0) DESC,   -- never-visited first
                      listing_count DESC,         -- then most goods
                      newest_price ASC            -- then oldest data
            LIMIT  1
            """,
            (f"{SYSTEM}-%", time.time() - MARKET_SCAN_STALENESS),
        ).fetchall()

    if not rows:
        return  # all markets are fresh

    # Skip wandering for market scans when we can't afford any ship — stay parked.
    if _shipyard_price_cache:
        current_ships = fleet_api.get_my_ships()
        current_miners    = len([s for s in current_ships if has_mining_mount(s)])
        current_surveyors = len([s for s in current_ships if has_survey_mount(s) and not has_mining_mount(s)])
        current_haulers   = len([
            s for s in current_ships
            if s["registration"]["role"] in ("HAULER", "TRANSPORT")
            and s["symbol"] != FLEET_MANAGER_SHIP
        ])
        def _scan_should_buy(stype: str) -> bool:
            score = ship_score(stype, current_miners, current_surveyors, current_haulers)
            return score >= 0
        _KNOWN_SHIP_TYPES = [
            "SHIP_ORE_HOUND", "SHIP_MINING_DRONE", "SHIP_COMMAND_FRIGATE",
            "SHIP_SURVEYOR", "SHIP_LIGHT_HAULER", "SHIP_HEAVY_FREIGHTER",
            "SHIP_SIPHON_DRONE", "SHIP_PROBE",
        ]
        _eligible = set(t for t in _KNOWN_SHIP_TYPES if _scan_should_buy(t))
        if _eligible:
            me = agent_api.get_my_agent()
            credits = me.get("credits", 0)
            _min_price = min(
                (s["purchasePrice"] for ships in _shipyard_price_cache.values()
                 for s in ships if s.get("type") in _eligible),
                default=None,
            )
            if _min_price is not None and credits - _min_price < CREDIT_RESERVE:
                log("[dim]Fleet manager: can't afford next ship — skipping market scan[/dim]")
                return

    target_wp, listing_count, _ = rows[0]
    log(f"[dim]🔍 Fleet manager scanning market {target_wp} ({listing_count} listings)…[/dim]")
    try:
        navigate_with_refuel(FLEET_MANAGER_SHIP, target_wp)
        ensure_docked(FLEET_MANAGER_SHIP)
        prices = get_market_prices(target_wp)
        goods_count = len([k for k in prices if not k.startswith("_")])
        if goods_count:
            log(f"[dim]🔍 {target_wp}: {goods_count} goods priced[/dim]")
        else:
            log(f"[dim]🔍 {target_wp}: no live prices (no ship required? listing only)[/dim]")
    except SpaceTradersError as e:
        log(f"[yellow]Fleet manager: market scan {target_wp} failed: {e}[/yellow]")


def fleet_manager_loop(
    contract: dict,
    contract_done: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Daemon thread: every 2 min, buy affordable ships, negotiate contracts,
    and scan unvisited/stale markets to keep arbitrage data fresh.
    """
    _ship_role_tag[FLEET_MANAGER_SHIP] = "FleetMgr"
    log("[dim]⚙  Fleet manager thread started[/dim]")
    CHECK_INTERVAL = 120  # seconds

    while not stop_event.wait(CHECK_INTERVAL):
        if not _manager_lock.acquire(blocking=False):
            continue  # another management op in progress — skip this tick
        try:
            _bg_negotiate_contract()
            if not stop_event.is_set():
                _bg_buy_and_launch(contract, contract_done, stop_event)
            if not stop_event.is_set():
                _bg_scan_next_market()
        except Exception as e:
            log(f"[yellow]Fleet manager error: {e}[/yellow]")
        finally:
            _manager_lock.release()

    log("[dim]⚙  Fleet manager thread stopped[/dim]")


# ── Status table ─────────────────────────────────────────────────────────────

def _print_status_table(contract: dict | None = None) -> None:
    """Print a snapshot table: all ships + contract progress + credits."""
    try:
        ships   = fleet_api.get_my_ships()
        me      = agent_api.get_my_agent()
        credits = me.get("credits", 0)
    except Exception:
        return

    # ── Ships table ───────────────────────────────────────────────────────────
    t = Table(
        title=f"[bold]Fleet Status[/bold]  [green]{credits:,} cr[/green]  [dim]{datetime.now().strftime('%H:%M:%S')}[/dim]",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    t.add_column("Ship", style="bold")
    t.add_column("Role")
    t.add_column("Status")
    t.add_column("Route")
    t.add_column("Fuel", justify="right")
    t.add_column("Cargo", justify="right")
    t.add_column("ETA")

    # ── Sort ships: groups together (hauler first, then its workers), then ungrouped ──
    def _ship_sort_key(s: dict) -> tuple:
        sym = s["symbol"]
        for gi, grp in enumerate(_ship_groups):
            if grp.get("hauler") == sym:
                return (gi, 0, sym)
            if sym in grp.get("workers", []):
                wi = grp["workers"].index(sym)
                return (gi, 1 + wi, sym)
        # Ungrouped: fleet-mgr first, then by role label order, then symbol
        if sym == FLEET_MANAGER_SHIP:
            return (len(_ship_groups), 0, sym)
        return (len(_ship_groups), 1, sym)

    sorted_ships = sorted(ships, key=_ship_sort_key)
    _prev_group_idx: int | None = None

    for s in sorted_ships:
        sym     = s["symbol"]

        # ── Group header divider ──────────────────────────────────────────────
        _cur_group_idx: int | None = None
        for gi, grp in enumerate(_ship_groups):
            if grp.get("hauler") == sym or sym in grp.get("workers", []):
                _cur_group_idx = gi
                break
        if _cur_group_idx != _prev_group_idx:
            if _prev_group_idx is not None:
                t.add_section()
            if _cur_group_idx is not None:
                grp_obj = _ship_groups[_cur_group_idx]
                grp_icon = "⛽" if grp_obj.get("type") == "siphon" else "⛏"
                t.add_row(
                    f"[bold dim]{grp_icon} {grp_obj['name']}[/bold dim]",
                    "", "", "", "", "", "",
                )
            _prev_group_idx = _cur_group_idx

        color   = _ship_color(sym)
        role    = s["registration"]["role"]
        nav     = s["nav"]
        status  = nav["status"]
        wp      = nav["waypointSymbol"]
        fuel    = s["fuel"]
        cargo   = s["cargo"]

        fuel_str  = f"{fuel['current']}/{fuel['capacity']}" if fuel.get("capacity") else "—"
        cargo_str = f"{cargo['units']}/{cargo['capacity']}"

        # Determine role label
        if sym in _trader_symbols:
            role_label = "trader"
        elif sym in _siphoner_symbols:
            role_label = "siphoner"
        elif sym in _hauler_symbols:
            role_label = "hauler"
        elif sym in _explorer_symbols:
            role_label = "explorer"
        elif sym == FLEET_MANAGER_SHIP:
            role_label = "fleet-mgr"
        elif any(g.get("hauler") == sym for g in _ship_groups):
            grp = next(g for g in _ship_groups if g.get("hauler") == sym)
            role_label = f"{grp.get('type','?')}-hauler"
        elif sym in _group_worker_ready:
            grp = next((g for g in _ship_groups if sym in g.get("workers", [])), None)
            role_label = f"{grp.get('type','?')}-worker" if grp else "worker"
        elif has_survey_mount(s) and not has_mining_mount(s):
            role_label = "surveyor"
        elif has_mining_mount(s):
            role_label = "miner"
        else:
            role_label = role.lower()

        # Status icon
        status_icon = {
            "IN_TRANSIT": "[yellow]✈ transit[/yellow]",
            "IN_ORBIT":   "[cyan]↑ orbit[/cyan]",
            "DOCKED":     "[green]⚓ docked[/green]",
        }.get(status, status)

        # ETA for ships in transit
        eta_str = ""
        if status == "IN_TRANSIT":
            arrival = nav.get("route", {}).get("arrival", "")
            if arrival:
                try:
                    dt   = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                    secs = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
                    if secs >= 3600:
                        eta_str = f"{secs//3600}h{(secs%3600)//60}m"
                    elif secs >= 60:
                        eta_str = f"{secs//60}m{secs%60}s"
                    else:
                        eta_str = f"{secs}s"
                    flight_mode = nav.get("flightMode", "")
                    if flight_mode == "DRIFT":
                        eta_str = f"[dim]{eta_str} drift[/dim]"
                    elif flight_mode == "BURN":
                        eta_str = f"[yellow]{eta_str} 🔥burn[/yellow]"
                    elif flight_mode == "CRUISE":
                        eta_str = f"{eta_str} cruise"
                except Exception:
                    pass

        # From waypoint (only meaningful in transit)
        from_str = ""
        if status == "IN_TRANSIT":
            from_str = nav.get("route", {}).get("departure", {}).get("symbol", "")

        # ── Route string ─────────────────────────────────────────────────────
        engine_speed = s.get("engine", {}).get("speed", 30)
        task_dest = _ship_task_dest.get(sym, "")
        wp_short = _short_wp(wp)

        if status == "IN_TRANSIT":
            departure = nav.get("route", {}).get("departure", {}).get("symbol", "")
            dep_short = _short_wp(departure) if departure else ""
            # Compute remaining hops from the current transit destination onward.
            future_hops: list[str] = []
            if task_dest and task_dest != wp:
                fuel_cap = max(fuel.get("capacity", 1), 1)
                try:
                    future_hops = _compute_route_hops(wp, task_dest, fuel_cap, fuel_cap)
                except Exception:
                    future_hops = [task_dest]
            parts: list[str] = []
            if dep_short:
                parts.append(f"[dim]{dep_short}[/dim]")
            # Current hop destination (yellow + plane icon)
            parts.append(f"[bold yellow]▶{wp_short}[/bold yellow]")
            # Upcoming hops
            prev_wp = wp
            for hop in future_hops:
                hop_short = _short_wp(hop)
                secs = _est_hop_secs(prev_wp, hop, engine_speed)
                if secs > 0:
                    if secs >= 3600:
                        t_str = f"{secs//3600}h{(secs%3600)//60}m"
                    elif secs >= 60:
                        t_str = f"{secs//60}m{secs%60}s"
                    else:
                        t_str = f"{secs}s"
                    time_tag = f"[dim](~{t_str})[/dim]"
                else:
                    time_tag = ""
                if hop == future_hops[-1]:
                    parts.append(f"[green]{hop_short}{time_tag}[/green]")
                else:
                    parts.append(f"[dim]{hop_short}{time_tag}[/dim]")
                prev_wp = hop
            route_str = " → ".join(parts)
        else:
            # Docked or orbiting
            if task_dest and task_dest != wp:
                cur_fuel_val = fuel.get("current", 0)
                fuel_cap = max(fuel.get("capacity", 1), 1)
                try:
                    hops = _compute_route_hops(wp, task_dest, cur_fuel_val, fuel_cap)
                except Exception:
                    hops = [task_dest]
                parts = [f"[bold cyan]{wp_short}[/bold cyan]"]
                prev_wp = wp
                for hop in hops:
                    hop_short = _short_wp(hop)
                    secs = _est_hop_secs(prev_wp, hop, engine_speed)
                    if secs > 0:
                        if secs >= 3600:
                            t_str = f"{secs//3600}h{(secs%3600)//60}m"
                        elif secs >= 60:
                            t_str = f"{secs//60}m{secs%60}s"
                        else:
                            t_str = f"{secs}s"
                        time_tag = f"[dim](~{t_str})[/dim]"
                    else:
                        time_tag = ""
                    if hop == hops[-1]:
                        parts.append(f"[green]{hop_short}{time_tag}[/green]")
                    else:
                        parts.append(f"[dim]{hop_short}{time_tag}[/dim]")
                    prev_wp = hop
                route_str = " → ".join(parts)
            else:
                route_str = f"[bold cyan]{wp_short}[/bold cyan]"

        # Friendly display name: emoji + role description + ship number suffix
        _num_suffix = sym.split("-")[-1] if "-" in sym else sym
        _friendly: dict[str, str] = {
            "fleet-mgr":    f"⚙ Fleet Mgr",
            "siphon-hauler": f"⛽ Siphon Hauler",
            "siphon-worker": f"🌀 Siphoner",
            "siphoner":      f"🌀 Siphoner",
            "miner-hauler":  f"⛏ Mine Hauler",
            "miner-worker":  f"⛏ Miner",
            "miner":         f"⛏ Miner",
            "surveyor":      f"🔭 Surveyor",
            "trader":        f"💹 Trader",
            "hauler":        f"🚢 Hauler",
            "explorer":      f"🌌 Explorer",
            "command":       f"👑 Command",
        }
        display_name = _friendly.get(role_label, role_label.title())
        display_name = f"{display_name} [{_num_suffix}]"

        t.add_row(
            f"[{color}]{display_name}[/{color}]",
            role_label,
            status_icon,
            route_str,
            fuel_str,
            cargo_str,
            eta_str,
        )

        # Persist cargo snapshot to DB for historical tracking
        goods = [{"symbol": i["symbol"], "units": i["units"]} for i in cargo.get("inventory", [])]
        db.record_ship_cargo(sym, wp, status, cargo["units"], cargo["capacity"], goods)

    console.print(t)

    # ── Contract progress bar ─────────────────────────────────────────────────
    if contract:
        for d in contract["terms"].get("deliver", []):
            good    = d["tradeSymbol"]
            dest    = d["destinationSymbol"]
            done    = d["unitsFulfilled"]
            needed  = d["unitsRequired"]
            pct     = done / max(needed, 1)
            bar_len = 30
            filled  = int(bar_len * pct)
            bar     = "█" * filled + "░" * (bar_len - filled)
            color   = "green" if pct >= 1.0 else "yellow" if pct >= 0.5 else "cyan"
            console.print(
                f"  Contract: [{color}]{bar}[/{color}] "
                f"[bold]{done}/{needed}[/bold] {good} → {dest} "
                f"([green]{pct*100:.0f}%[/green])"
            )


# ── Contract orchestration (concurrent) ──────────────────────────────────────

def work_contract(contract: dict) -> None:
    """Accept (if needed) then run all miners in parallel until contract is fulfilled."""
    global _active_contract, _ship_groups
    _contract_buy_ships.clear()  # reset per-contract buy assignments
    with _active_contract_lock:
        _active_contract = contract
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
        db.upsert_contract(contract, accepted_now=True)  # record real acceptance timestamp
    discord.send_contract_start(contract)

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
    probes = [
        s["symbol"] for s in all_fleet
        if s["frame"].get("symbol", "") == "FRAME_PROBE"
        and s["symbol"] != FLEET_MANAGER_SHIP
    ]
    # Optionally run the command ship as a hauler if the user set command_ship_role=hauler
    if db.get_bot_setting("command_ship_role", "idle") == "hauler" and FLEET_MANAGER_SHIP not in haulers:
        haulers.append(FLEET_MANAGER_SHIP)
    miners = miners or [COMMAND_SHIP]

    # ── Pre-compute groups early so group haulers are excluded from contract buyer pool ──
    auto_group_ships()
    with _ship_groups_lock:
        _ship_groups = _load_ship_groups()
    _all_symbols_pre = set(s["symbol"] for s in all_fleet)
    _pre_grouped_haulers: set[str] = set()
    _pre_grouped_workers: set[str] = set()
    for grp in _ship_groups:
        h = grp.get("hauler", "")
        ws = [w for w in grp.get("workers", []) if w in _all_symbols_pre]
        if h in _all_symbols_pre and ws:
            _pre_grouped_haulers.add(h)
            _pre_grouped_workers.update(ws)
    # Exclude group-assigned haulers from the contract buyer / trader pools
    haulers_free = [h for h in haulers if h not in _pre_grouped_haulers and h not in _pre_grouped_workers]

    # ── Direct-buy contract: best ship buys/delivers; miners focus on income ────
    global _mine_only_contract
    _contract_good = contract["terms"]["deliver"][0]["tradeSymbol"] if contract["terms"].get("deliver") else ""
    # The contract_buyer is the ship that will run buy/deliver.
    # Prefer a hauler (bigger fuel tank, possible jump drive) over a miner.
    # Falls back to miners[0] if no hauler is available.
    _contract_buyer: str | None = None
    _haulers_for_buy: list[str] = []   # haulers running miner_loop (buy/deliver)
    _haulers_for_trade: list[str] = [] # remaining haulers running trader_loop

    if _contract_good and best_buy_waypoint(_contract_good):
        # Build a fake contract so every non-buyer ship mines/sells for income.
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
        # Always use the command ship (miner) for contract buying so all Light Haulers can trade.
        miners_free = [m for m in miners if m not in _pre_grouped_workers]
        if miners_free:
            _contract_buyer_miner = miners_free[0]
            _contract_buyer = _contract_buyer_miner
            _haulers_for_buy = []
            _haulers_for_trade = haulers_free   # ALL free haulers do arbitrage
            _contract_buy_ships.add(_contract_buyer_miner)
            log(f"[cyan]Direct-buy contract: {_contract_buyer_miner} handles buy/deliver; "
                f"{len(haulers_free)} hauler(s) do arbitrage[/cyan]")
        elif haulers_free:
            # No free miners (all grouped) — fall back to first hauler for contract
            _contract_buyer = haulers_free[0]
            _haulers_for_buy = [haulers_free[0]]
            _haulers_for_trade = haulers_free[1:]
            log(f"[cyan]Direct-buy contract: hauler {haulers_free[0]} handles buy/deliver "
                f"(no free miners — all grouped)[/cyan]")
        else:
            # No free miners and no free haulers — fall back to mining normally.
            _mine_only_contract = None
            log(f"[yellow]Direct-buy contract: all miners grouped and no free hauler — falling back to mine contract[/yellow]")
    else:
        _mine_only_contract = None  # all miners work the real contract

    log(f"[bold]Launching {len(miners)} miner thread(s): {miners}[/bold]")
    if surveyors:
        log(f"[magenta]Launching {len(surveyors)} surveyor thread(s): {surveyors}[/magenta]")
    if haulers:
        if _mine_only_contract is not None:
            if _haulers_for_buy:
                log(f"[blue]Launching {len(_haulers_for_buy)} hauler(s) as contract buyer(s): {_haulers_for_buy}[/blue]")
            if _haulers_for_trade:
                _trader_symbols.extend(_haulers_for_trade)
                log(f"[blue]Launching {len(_haulers_for_trade)} hauler(s) as trader(s) (direct-buy contract): {_haulers_for_trade}[/blue]")
        else:
            # Mining contract: with 2+ free haulers, first is dedicated contract hauler, rest trade.
            # With only 1 free hauler, arbitrage earns more — send it to trader_loop instead.
            _hauler_symbols.clear()
            if len(haulers_free) >= 2:
                _hauler_symbols.extend(haulers_free[:1])
                _trader_symbols.extend(haulers_free[1:])
                log(f"[blue]Launching hauler {haulers_free[0]} as contract hauler"
                    + (f"; {haulers_free[1:]} as trader(s)" if haulers_free[1:] else "") + "[/blue]")
            elif haulers_free:
                # Single free hauler: trade for income; miners self-deliver contract ore
                _trader_symbols.extend(haulers_free)
                log(f"[blue]Single hauler {haulers_free[0]} → arbitrage (miners self-deliver for contract)[/blue]")
    if siphoners:
        _siphoner_symbols.clear()
        _siphoner_symbols.extend(siphoners)
        log(f"[blue]Launching {len(siphoners)} siphoner thread(s): {siphoners}[/blue]")
    if probes:
        new_probes = [p for p in probes if p not in _explorer_symbols]
        _explorer_symbols.extend(new_probes)
        if new_probes:
            log(f"[blue]Launching {len(new_probes)} probe explorer thread(s): {new_probes}[/blue]")

    # ── Load ship groups and remove group-assigned ships from normal pools ────
    # Groups were already loaded above (pre-grouping); just clear worker events.
    # _ship_groups is already set from the pre-grouping step above.
    _group_worker_ready.clear()

    # Validate groups: remove ships not in the current fleet
    _all_symbols = set(s["symbol"] for s in all_fleet)
    _active_groups: list[dict] = []
    for grp in _ship_groups:
        h = grp.get("hauler", "")
        ws = [w for w in grp.get("workers", []) if w in _all_symbols]
        if h in _all_symbols and ws:
            _active_groups.append({"type": grp.get("type", "siphon"), "hauler": h, "workers": ws})

    # Initialize per-worker ready events; pull these ships out of normal loops
    _grouped_haulers: set[str] = set()
    _grouped_workers: set[str] = set()
    for grp in _active_groups:
        _grouped_haulers.add(grp["hauler"])
        for w in grp["workers"]:
            _group_worker_ready[w] = threading.Event()
            _grouped_workers.add(w)
        log(f"[cyan]Group ({grp['type']}): hauler={grp['hauler']} workers={grp['workers']}[/cyan]")

    # Remove group ships from normal pools so they aren't double-assigned
    miners    = [s for s in miners    if s not in _grouped_workers and s not in _grouped_haulers]
    haulers   = [s for s in haulers   if s not in _grouped_workers and s not in _grouped_haulers]
    siphoners = [s for s in siphoners if s not in _grouped_workers and s not in _grouped_haulers]

    stop_event = threading.Event()
    # Miners: if there's a hauler buyer, ALL miners get mine_only; otherwise first miner gets real contract.
    _all_miners_mine_only = _mine_only_contract is not None and bool(_haulers_for_buy)
    threads = [
        threading.Thread(
            target=miner_loop,
            args=(miner,
                  _mine_only_contract if (_all_miners_mine_only or (i > 0 and _mine_only_contract is not None)) else contract,
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
    # Haulers: buy/deliver haulers run miner_loop (direct-buy path), rest are traders/haulers.
    threads += [
        threading.Thread(
            target=miner_loop,
            args=(hauler, contract, contract_done, stop_event),
            daemon=True,
            name=f"hauler-{hauler}",
        )
        for hauler in _haulers_for_buy
    ]
    if _mine_only_contract is not None:
        # Direct-buy: remaining haulers are traders
        _mining_hauler_traders = _haulers_for_trade
        _mining_hauler_miningc = []
    else:
        # Mining contract: haulers[0] delivers ore, rest run arbitrage
        _mining_hauler_miningc = haulers[:1]
        _mining_hauler_traders = haulers[1:]
    threads += [
        threading.Thread(
            target=hauler_loop,
            args=(hauler, contract, contract_done, stop_event),
            daemon=True,
            name=f"hauler-{hauler}",
        )
        for hauler in _mining_hauler_miningc
    ]
    threads += [
        threading.Thread(
            target=trader_loop,
            args=(hauler, contract, contract_done, stop_event),
            daemon=True,
            name=f"hauler-{hauler}",
        )
        for hauler in _mining_hauler_traders
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
    # Group hauler threads
    for grp in _active_groups:
        _grp_type    = grp["type"]
        _grp_hauler  = grp["hauler"]
        _grp_workers = grp["workers"]
        if _grp_type == "siphon":
            threads.append(threading.Thread(
                target=siphon_hauler_loop,
                args=(_grp_hauler, _grp_workers, stop_event),
                daemon=True,
                name=f"siphon-hauler-{_grp_hauler}",
            ))
            # Workers run normal siphon_loop (their inner will detect group membership)
            threads += [
                threading.Thread(
                    target=siphon_loop,
                    args=(w, contract, contract_done, stop_event),
                    daemon=True,
                    name=f"siphoner-{w}",
                )
                for w in _grp_workers
                if w not in [t.name.split("-", 1)[-1] for t in threads]
            ]
        elif _grp_type == "miner":
            threads.append(threading.Thread(
                target=miner_hauler_loop,
                args=(_grp_hauler, _grp_workers, stop_event),
                daemon=True,
                name=f"miner-hauler-{_grp_hauler}",
            ))
            # Workers run miner_loop in sell-only mode (they signal hauler instead of self-delivering)
            threads += [
                threading.Thread(
                    target=miner_loop,
                    args=(w, _mine_only_contract or contract, contract_done, stop_event),
                    daemon=True,
                    name=f"miner-{w}",
                )
                for w in _grp_workers
                if w not in [t.name.split("-", 1)[-1] for t in threads]
            ]
    # Explorer threads for probes (including those that existed before this run)
    threads += [
        threading.Thread(
            target=explorer_loop,
            args=(probe, contract, contract_done, stop_event),
            daemon=True,
            name=f"explorer-{probe}",
        )
        for probe in _explorer_symbols
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

    _last_status_table   = time.monotonic()
    _last_discord_status  = time.monotonic()
    _last_progress_units  = sum(
        d.get("unitsFulfilled", 0) for d in contract.get("terms", {}).get("deliver", [])
    )
    _last_progress_ts     = time.monotonic()  # separate clock for stuck detection
    _stuck_warned         = False
    _DISCORD_STATUS_SECS  = int(db.get_bot_setting("discord_status_interval", "300"))  # default 5 min
    _STUCK_SECS           = 1800  # warn if no delivery progress for 30 min
    while not contract_done.is_set():
        worker_threads = [t for t in threads if "miner" in t.name or "hauler" in t.name]
        if worker_threads and all(not t.is_alive() for t in worker_threads):
            log("[yellow]All worker threads exited without fulfilling contract[/yellow]")
            break
        contract_done.wait(timeout=15)
        _now_mono = time.monotonic()
        if _now_mono - _last_status_table >= 60:
            _print_status_table(contract)
            _last_status_table = _now_mono
        if _now_mono - _last_discord_status >= _DISCORD_STATUS_SECS:
            try:
                _d_ships = fleet_api.get_my_ships()
                _d_agent = agent_api.get_my_agent()
                from dashboard2 import _calc_cph  # reuse CPH helper
                _cph, _ = _calc_cph()
            except Exception:
                _d_ships, _d_agent, _cph = [], {}, 0
            discord.send_status(_d_ships, _d_agent, _cph,
                                interval_min=_DISCORD_STATUS_SECS // 60)
            _last_discord_status = _now_mono
        # Stuck detection: no delivery progress in 30 min
        try:
            _fresh_c = contracts_api.get_contract(contract["id"])
            _cur_units = sum(
                d.get("unitsFulfilled", 0)
                for d in _fresh_c.get("terms", {}).get("deliver", [])
            )
        except Exception:
            _cur_units = _last_progress_units
        if _cur_units > _last_progress_units:
            _last_progress_units = _cur_units
            _last_progress_ts    = _now_mono  # reset the dedicated stuck clock
            _stuck_warned = False
        elif not _stuck_warned and _now_mono - _last_progress_ts >= _STUCK_SECS:
            good_stuck = _contract_good or "?"
            discord.send_stuck(
                f"No delivery progress on **{good_stuck}** for 30+ min. "
                f"Delivered so far: {_cur_units}"
            )
            _stuck_warned = True
    stop_event.set()       # tell remaining miners and fleet manager to wind down
    for t in threads:
        t.join(timeout=120)
    mgr_thread.join(timeout=30)

    log("[green bold]All miners done.[/green bold]")


# ── Ship buying ───────────────────────────────────────────────────────────────

def _is_mining_drone_safe() -> bool:
    """True if ASTEROID is within NO_DRIFT_DIST_MAX units of a fuel market."""
    fuel_markets = _good_exporters.get("FUEL", [])
    if not fuel_markets:
        return False
    nearest = min(fuel_markets, key=lambda wp: waypoint_distance(ASTEROID, wp))
    return waypoint_distance(ASTEROID, nearest) <= NO_DRIFT_DIST_MAX


def _is_siphon_reachable() -> bool:
    """True if any gas giant in the system is within NO_DRIFT_DIST_MAX units of a fuel market."""
    gas_giants = _find_gas_giants()
    if not gas_giants:
        return False
    fuel_markets = _good_exporters.get("FUEL", [])
    if not fuel_markets:
        return False
    for gg in gas_giants:
        nearest = min(fuel_markets, key=lambda wp: waypoint_distance(gg, wp))
        if waypoint_distance(gg, nearest) <= NO_DRIFT_DIST_MAX:
            return True
    return False


def ship_score(
    ship_type: str,
    current_miner_count: int,
    current_surveyor_count: int = 0,
    current_hauler_count: int = 0,
    current_probe_count: int = -1,  # -1 means look it up (only safe outside buy loops)
) -> int:
    """
    Score a ship type for purchase priority.  Higher = more desirable to buy.
    -1 means skip entirely.

    Uses fill-before-expand team policy:
    - Each team needs PRODUCERS_PER_TEAM_TARGET (5) workers + HAULERS_PER_TEAM_TARGET (1) hauler.
    - Fill existing teams to capacity before starting a new team.
    - A new team is seeded by buying a hauler; workers follow.
    - Caps at MAX_SIPHON_TEAMS / MAX_MINER_TEAMS total teams.
    """
    base = SHIP_SCORES.get(ship_type, -1)
    if base < 0:
        return -1

    if ship_type == "SHIP_SURVEYOR":
        if current_surveyor_count >= 1:
            return -1
        return base

    # Snapshot group state once for all team-aware decisions
    with _ship_groups_lock:
        _s_groups = [g for g in _ship_groups if g.get("type") == "siphon"]
        _m_groups = [g for g in _ship_groups if g.get("type") == "miner"]
    n_siphon_teams   = len(_s_groups)
    n_siphon_workers = sum(len(g.get("workers", [])) for g in _s_groups)
    n_miner_teams    = len(_m_groups)
    n_miner_workers  = sum(len(g.get("workers", [])) for g in _m_groups)

    if ship_type in ("SHIP_LIGHT_HAULER", "SHIP_LIGHT_SHUTTLE", "SHIP_HEAVY_FREIGHTER"):
        # Buy a hauler to seed a new team when:
        #   (a) no teams of that type exist yet, OR
        #   (b) all existing teams of that type are full AND we're under the team cap.
        siphon_ok = _is_siphon_reachable()
        s_all_full = n_siphon_teams > 0 and all(
            len(g.get("workers", [])) >= PRODUCERS_PER_TEAM_TARGET for g in _s_groups
        )
        need_siphon_hauler = siphon_ok and (
            n_siphon_teams == 0 or (s_all_full and n_siphon_teams < MAX_SIPHON_TEAMS)
        )
        m_all_full = n_miner_teams > 0 and all(
            len(g.get("workers", [])) >= PRODUCERS_PER_TEAM_TARGET for g in _m_groups
        )
        need_miner_hauler = (
            n_miner_teams == 0 or (m_all_full and n_miner_teams < MAX_MINER_TEAMS)
        )
        # Also buy haulers for trading until we reach MAX_TRADERS free haulers
        with _ship_groups_lock:
            _grouped_h = {g.get("hauler") for g in _ship_groups}
        all_haulers = fleet_api.get_my_ships()
        free_haulers = sum(
            1 for s in all_haulers
            if s["registration"]["role"] in ("HAULER", "TRANSPORT")
            and s["symbol"] not in _grouped_h
            and s["symbol"] != FLEET_MANAGER_SHIP
        )
        _auto_max_traders = max(1, len(_known_markets) // 5) if _known_markets else 3
        need_trader = free_haulers < _auto_max_traders
        if not need_siphon_hauler and not need_miner_hauler and not need_trader:
            log("[dim]Fleet manager: hauler skipped — all teams full, at cap, and traders at max[/dim]")
            return -1
        return base

    if ship_type == "SHIP_PROBE":
        # Only buy probes when comfortably wealthy and under the probe cap
        try:
            me = agent_api.get_my_agent()
            credits = me.get("credits", 0)
        except Exception:
            return -1
        if credits < PROBE_CREDIT_THRESHOLD:
            return -1
        if current_probe_count < 0:
            # Fallback: look up live (only used outside buy_ships loops)
            try:
                all_ships = fleet_api.get_my_ships()
                current_probe_count = sum(1 for s in all_ships if s["frame"].get("symbol") == "FRAME_PROBE")
            except Exception:
                return -1
        _auto_max_probes = max(len(_known_markets), 1) if _known_markets else 10
        if current_probe_count >= _auto_max_probes:
            return -1
        return base

    if ship_type in ("SHIP_ORE_HOUND", "SHIP_MINING_DRONE"):
        if ship_type == "SHIP_MINING_DRONE" and not _is_mining_drone_safe():
            log("[dim]Fleet manager: MINING_DRONE skipped — asteroid too far from fuel market[/dim]")
            return -1
        if current_hauler_count == 0:
            return -1  # need at least 1 hauler to form a team
        # Effective teams: use actual groups if they exist, else 1 team per hauler up to cap
        eff_teams    = n_miner_teams if n_miner_teams > 0 else min(current_hauler_count, MAX_MINER_TEAMS)
        target_total = min(eff_teams, MAX_MINER_TEAMS) * PRODUCERS_PER_TEAM_TARGET
        if current_miner_count >= target_total:
            return -1  # all miner teams full
        return base

    if ship_type in ("SHIP_SIPHON_DRONE", "SHIP_GAS_DRONE"):
        if not _is_siphon_reachable():
            log("[dim]Fleet manager: SIPHON_DRONE skipped — gas giant too far from fuel market[/dim]")
            return -1
        if current_hauler_count == 0:
            return -1  # need at least 1 hauler to form a team
        eff_teams    = n_siphon_teams if n_siphon_teams > 0 else min(current_hauler_count, MAX_SIPHON_TEAMS)
        target_total = min(eff_teams, MAX_SIPHON_TEAMS) * PRODUCERS_PER_TEAM_TARGET
        if len(_siphoner_symbols) >= target_total:
            return -1  # all siphon teams full
        return base

    if ship_type == "SHIP_COMMAND_FRIGATE":
        # Buy a 2nd frigate (with jump drive) for refinery scouting once we can afford it.
        # Stop at 1 — the _should_buy gate blocks this once _explorer_symbols is populated.
        FRIGATE_CREDIT_THRESHOLD = 1_500_000
        try:
            me = agent_api.get_my_agent()
            credits = me.get("credits", 0)
        except Exception:
            return -1
        if credits < FRIGATE_CREDIT_THRESHOLD:
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

    current_ships     = fleet_api.get_my_ships()
    current_miners    = len(get_mining_ships())
    current_surveyors = len([s for s in current_ships if has_survey_mount(s) and not has_mining_mount(s)])
    current_haulers   = len([s for s in current_ships
                             if s["registration"]["role"] in ("HAULER", "TRANSPORT")
                             and s["symbol"] != FLEET_MANAGER_SHIP])
    current_probes    = sum(1 for s in current_ships if s["frame"].get("symbol") == "FRAME_PROBE")
    purchases = 0

    def _eligible(stype: str) -> bool:
        if ship_score(stype, current_miners, current_surveyors, current_haulers, current_probes) < 0:
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
            sc = ship_score(s.get("type", ""), current_miners, current_surveyors, current_haulers, current_probes)
            price = s.get("purchasePrice", 0)
            can_afford = "[green]✓[/green]" if credits - price >= CREDIT_RESERVE else "[red]✗[/red]"
            priority = str(sc) if sc >= 0 else "[dim]skip[/dim]"
            t.add_row(s.get("type", "?"), f"{can_afford} {price:,} cr", s.get("supply", "?"), priority)
        console.print(t)

        # Buy greedily in priority order, skipping anything we can't afford
        buyable = sorted(
            [s for s in ships_for_sale if _eligible(s.get("type", ""))],
            key=lambda s: ship_score(s.get("type", ""), current_miners, current_surveyors, current_haulers, current_probes),
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
        navigate_to(FLEET_MANAGER_SHIP, _FACTION_HQ_WP)
        ensure_docked(FLEET_MANAGER_SHIP)
        result = fleet_api.negotiate_contract(FLEET_MANAGER_SHIP)
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
        db.record_credits(me["credits"])
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

        # Post-contract: expand → maintain → upgrade → scrap queued ships
        # (contract negotiation is handled by get_next_contract() at the start
        #  of the next loop so the MIN_CONTRACT_PAYOUT filter can apply cleanly)
        buy_ships()
        step_maintain_fleet()
        step_upgrade_fleet()
        step_scrap_pending()

        step_show_status()
        log("[green]Loop complete — starting next contract.[/green]")
        time.sleep(2)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        console.print("\n[dim]Automation stopped.[/dim]")
        discord.send_shutdown("KeyboardInterrupt")
    except SpaceTradersError as e:
        console.print(f"\n[bold red]Fatal error: {e}[/bold red]")
        if "reset" in str(e).lower() or e.code in (4100, 4101, 4109):
            discord.send_server_reset()
        else:
            discord.send_shutdown(str(e))
        raise

