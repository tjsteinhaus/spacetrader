"""api/universe.py — Systems, waypoints, markets, shipyards, factions endpoints."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from client import SpaceTradersClient


# --- Systems ---

async def get_systems(client: "SpaceTradersClient", page: int = 1, limit: int = 20) -> dict:
    return await client.get("/systems", params={"page": page, "limit": limit})


async def get_system(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/systems/{symbol}")


async def get_waypoints(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_type: str | None = None,
) -> list[dict]:
    """Fetch all waypoints in a system, handling pagination automatically."""
    params: dict[str, Any] = {"limit": 20}
    if waypoint_type:
        params["type"] = waypoint_type
    results: list[dict] = []
    page = 1
    while True:
        params["page"] = page
        batch = await client.get(f"/systems/{system_symbol}/waypoints", params=params)
        items = batch if isinstance(batch, list) else batch.get("data", [])
        if not items:
            break
        results.extend(items)
        if len(items) < 20:
            break
        page += 1
    return results


async def get_waypoint(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
) -> dict:
    return await client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}")


async def get_jump_gate(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
) -> dict:
    return await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/jump-gate"
    )


async def get_construction(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
) -> dict:
    return await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/construction"
    )


async def supply_construction(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
    ship_symbol: str,
    trade_symbol: str,
    units: int,
) -> dict:
    return await client.post(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/construction/supply",
        {"shipSymbol": ship_symbol, "tradeSymbol": trade_symbol, "units": units},
    )


# --- Markets ---

async def get_market(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
) -> dict:
    return await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/market"
    )


# --- Shipyards ---

async def get_shipyard(
    client: "SpaceTradersClient",
    system_symbol: str,
    waypoint_symbol: str,
) -> dict:
    return await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/shipyard"
    )


# --- Factions ---

async def get_factions(client: "SpaceTradersClient") -> list[dict]:
    return await client.get_all_pages("/factions")


async def get_faction(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/factions/{symbol}")


async def get_my_factions(client: "SpaceTradersClient") -> dict:
    return await client.get("/my/factions")


# --- Server ---

async def get_status(client: "SpaceTradersClient") -> dict:
    return await client.get("/")


async def get_supply_chain(client: "SpaceTradersClient") -> dict:
    return await client.get("/market/supply-chain")
