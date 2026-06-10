"""
sync_api.py — Thin synchronous wrappers around v2's async API modules.
Intended for use in Textual @work(thread=True) workers only.
Each function creates its own event loop (safe inside a thread).
"""
from __future__ import annotations

import asyncio
from typing import Any

from client import SpaceTradersClient


def _run(coro):
    """Run an async coroutine synchronously (safe in worker threads)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _with_client(coro_fn):
    async with SpaceTradersClient() as c:
        return await coro_fn(c)


# ── Agent ────────────────────────────────────────────────────────────────────

def get_my_agent() -> dict:
    from api import agent as _a
    return _run(_with_client(_a.get_my_agent))


# ── Fleet ────────────────────────────────────────────────────────────────────

def get_my_ships() -> list:
    async def _f(c):
        from api import fleet as _fl
        return await _fl.get_my_ships(c)
    return _run(_with_client(_f))


def get_ship(symbol: str) -> dict:
    async def _f(c):
        from api import fleet as _fl
        return await _fl.get_ship(c, symbol)
    return _run(_with_client(_f))


def get_contracts() -> list:
    async def _f(c):
        from api import contracts as _co
        return await _co.get_contracts(c)
    return _run(_with_client(_f))


def get_active_contracts() -> list:
    """Return only accepted, unfulfilled contracts."""
    contracts = get_contracts()
    return [c for c in contracts if c.get("accepted") and not c.get("fulfilled")]


# ── Universe ─────────────────────────────────────────────────────────────────

def get_waypoints(system: str, waypoint_type: str | None = None) -> list:
    async def _f(c):
        from api import universe as _u
        return await _u.get_waypoints(c, system, waypoint_type)
    return _run(_with_client(_f))


def get_market(system: str, waypoint: str) -> dict:
    async def _f(c):
        from api import universe as _u
        return await _u.get_market(c, system, waypoint)
    return _run(_with_client(_f))


def get_shipyard(system: str, waypoint: str) -> dict:
    async def _f(c):
        from api import universe as _u
        return await _u.get_shipyard(c, system, waypoint)
    return _run(_with_client(_f))
