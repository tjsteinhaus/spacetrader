"""api/fleet.py — All ship/fleet endpoints."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from client import SpaceTradersClient


# --- Ship info ---

async def get_my_ships(client: "SpaceTradersClient") -> list[dict]:
    return await client.get_all_pages("/my/ships")


async def get_ship(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}")


async def get_ship_nav(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}/nav")


async def get_ship_cargo(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}/cargo")


async def get_ship_cooldown(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}/cooldown")


# --- Navigation ---

async def orbit(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/orbit")


async def dock(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/dock")


async def navigate(client: "SpaceTradersClient", symbol: str, waypoint_symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/navigate", {"waypointSymbol": waypoint_symbol})


async def jump(client: "SpaceTradersClient", symbol: str, system_symbol: str) -> dict:
    """Jump to a system via jump gate. API expects systemSymbol in body."""
    return await client.post(f"/my/ships/{symbol}/jump", {"systemSymbol": system_symbol})


async def warp(client: "SpaceTradersClient", symbol: str, waypoint_symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/warp", {"waypointSymbol": waypoint_symbol})


async def patch_nav(client: "SpaceTradersClient", symbol: str, flight_mode: str) -> dict:
    return await client.patch(f"/my/ships/{symbol}/nav", {"flightMode": flight_mode})


# --- Fuel & Cargo ---

async def refuel(client: "SpaceTradersClient", symbol: str, units: int | None = None) -> dict:
    body: dict[str, Any] = {}
    if units is not None:
        body["units"] = units
    return await client.post(f"/my/ships/{symbol}/refuel", body)


async def purchase_cargo(client: "SpaceTradersClient", symbol: str, good: str, units: int) -> dict:
    return await client.post(f"/my/ships/{symbol}/purchase", {"symbol": good, "units": units})


async def sell_cargo(client: "SpaceTradersClient", symbol: str, good: str, units: int) -> dict:
    return await client.post(f"/my/ships/{symbol}/sell", {"symbol": good, "units": units})


async def jettison(client: "SpaceTradersClient", symbol: str, good: str, units: int) -> dict:
    return await client.post(f"/my/ships/{symbol}/jettison", {"symbol": good, "units": units})


async def transfer_cargo(
    client: "SpaceTradersClient",
    symbol: str,
    trade_symbol: str,
    units: int,
    target_ship: str,
) -> dict:
    return await client.post(
        f"/my/ships/{symbol}/transfer",
        {"tradeSymbol": trade_symbol, "units": units, "shipSymbol": target_ship},
    )


# --- Mining / Extraction ---

async def extract(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/extract")


async def extract_with_survey(client: "SpaceTradersClient", symbol: str, survey: dict) -> dict:
    return await client.post(f"/my/ships/{symbol}/extract/survey", survey)


async def refine(client: "SpaceTradersClient", symbol: str, produce: str) -> dict:
    """Refine raw resources into processed goods using an onboard refinery/processor."""
    return await client.post(f"/my/ships/{symbol}/refine", {"produce": produce})


async def siphon(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/siphon")


async def survey(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/survey")


# --- Scanning ---

async def scan_systems(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/scan/systems")


async def scan_waypoints(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/scan/waypoints")


async def scan_ships(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/scan/ships")


# --- Maintenance ---

async def get_repair_cost(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}/repair")


async def repair(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/repair")


async def scrap(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/scrap")


# --- Mounts ---

async def get_mounts(client: "SpaceTradersClient", symbol: str) -> dict:
    return await client.get(f"/my/ships/{symbol}/mounts")


async def install_mount(client: "SpaceTradersClient", symbol: str, mount_symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/mounts/install", {"symbol": mount_symbol})


async def remove_mount(client: "SpaceTradersClient", symbol: str, mount_symbol: str) -> dict:
    return await client.post(f"/my/ships/{symbol}/mounts/remove", {"symbol": mount_symbol})


# --- Shipyard purchase ---

async def purchase_ship(client: "SpaceTradersClient", ship_type: str, waypoint_symbol: str) -> dict:
    return await client.post("/my/ships", {"shipType": ship_type, "waypointSymbol": waypoint_symbol})
