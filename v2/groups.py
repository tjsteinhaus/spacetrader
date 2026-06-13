"""
groups.py — Ship group registry for siphon/miner team coordination.

A group is a dict: {"name": str, "type": "siphon"|"miner", "hauler": str, "workers": [str]}

Workers (siphon drones / mining drones) set an asyncio.Event when their cargo is full.
The hauler role awaits those events, transfers the cargo, then clears the event so the
worker can resume.

All asyncio.Event objects are created lazily inside the running event loop — never at
module import time — so this module is safe to import before asyncio.run() is called.
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level event registry (per worker ship symbol)
# ---------------------------------------------------------------------------

# Populated by init_worker_events() before group tasks are launched.
_worker_events: dict[str, asyncio.Event] = {}


def init_worker_events(worker_symbols: list[str]) -> None:
    """(Re)create asyncio.Events for each worker — must be called inside the event loop."""
    global _worker_events
    _worker_events = {sym: asyncio.Event() for sym in worker_symbols}


def get_worker_event(worker_sym: str) -> asyncio.Event | None:
    """Return the asyncio.Event for a worker, or None if not in any group."""
    return _worker_events.get(worker_sym)


def is_grouped_worker(worker_sym: str) -> bool:
    return worker_sym in _worker_events


def clear_all_events() -> None:
    """Reset all worker events — called at the start of each contract cycle."""
    for evt in _worker_events.values():
        evt.clear()
    _worker_events.clear()


# ---------------------------------------------------------------------------
# Persistence (stored in DB bot_settings as JSON)
# ---------------------------------------------------------------------------

def load_groups() -> list[dict]:
    """Read ship groups from DB. Returns [] if unset or invalid."""
    import db
    raw = db.get_bot_setting("ship_groups", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_groups(groups: list[dict]) -> None:
    """Persist ship groups to DB bot_settings."""
    import db
    db.set_bot_setting("ship_groups", json.dumps(groups))


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

async def auto_group_ships(client, config) -> list[dict]:
    """Build ship groups from mount types.

    Runs when:
      - 'auto_group_ships' DB setting is '1', OR
      - No groups currently exist in the DB.

    Assigns 1 hauler per ~3 workers. Siphon drones group separately from
    mining drones. Saves result to DB and returns the new groups list.
    """
    import db
    from api import fleet as fleet_api

    force    = db.get_bot_setting("auto_group_ships", "0") == "1"
    existing = load_groups()
    if existing and not force:
        return existing  # manual groups exist — don't override

    all_ships = await fleet_api.get_my_ships(client)

    def _has_siphon(s: dict) -> bool:
        return any(
            "SIPHON" in m.get("symbol", "") or "GAS" in m.get("symbol", "")
            for m in s.get("mounts", [])
        )

    def _has_mining(s: dict) -> bool:
        return any("MINING" in m.get("symbol", "") for m in s.get("mounts", []))

    def _is_hauler(s: dict) -> bool:
        return s.get("registration", {}).get("role", "") in ("HAULER", "TRANSPORT")

    siphon_workers = [
        s["symbol"] for s in all_ships
        if _has_siphon(s) and s["symbol"] != config.fleet_manager_ship
    ]
    miner_workers = [
        s["symbol"] for s in all_ships
        if _has_mining(s)
        and s["symbol"] not in (config.fleet_manager_ship, config.command_ship)
    ]
    worker_set = set(siphon_workers + miner_workers)
    haulers = [
        s["symbol"] for s in all_ships
        if _is_hauler(s)
        and s["symbol"] != config.fleet_manager_ship
        and s["symbol"] not in worker_set
    ]

    if not (siphon_workers or miner_workers) or not haulers:
        log.info("auto_group_ships: no workers or no haulers to group")
        return existing

    groups: list[dict] = []
    remaining = list(haulers)

    # ── Siphon groups ──────────────────────────────────────────────────────────
    if siphon_workers and remaining:
        n = max(1, (len(siphon_workers) + 2) // 3)  # ~1 hauler per 3 workers
        n = min(n, len(remaining))
        s_haulers = remaining[:n]
        remaining = remaining[n:]
        for i, hauler in enumerate(s_haulers):
            my_workers = [w for j, w in enumerate(siphon_workers) if j % n == i]
            if my_workers:
                groups.append({
                    "name":    f"Siphon Team {i + 1}",
                    "type":    "siphon",
                    "hauler":  hauler,
                    "workers": my_workers,
                })

    # ── Miner groups ────────────────────────────────────────────────────────────
    if miner_workers and remaining:
        n = max(1, (len(miner_workers) + 2) // 3)
        n = min(n, len(remaining))
        m_haulers = remaining[:n]
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
        save_groups(groups)
        log.info("auto_group_ships: created %d group(s)", len(groups))
        for g in groups:
            log.info("  %s: hauler=%s workers=%s", g["name"], g["hauler"], g["workers"])

    return groups


def validate_groups(groups: list[dict], fleet_symbols: set[str]) -> list[dict]:
    """Remove groups whose hauler or all workers are no longer in the fleet."""
    active = []
    for g in groups:
        hauler = g.get("hauler", "")
        workers = [w for w in g.get("workers", []) if w in fleet_symbols]
        if hauler in fleet_symbols and workers:
            active.append({**g, "workers": workers})
    return active
