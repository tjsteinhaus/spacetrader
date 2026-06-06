"""Systems, waypoints, markets, shipyards endpoints."""
import client


# --- Systems ---

def get_systems(page: int = 1, limit: int = 20) -> dict:
    return client.get("/systems", params={"page": page, "limit": limit})


def get_system(symbol: str) -> dict:
    return client.get(f"/systems/{symbol}")


def get_waypoints(system_symbol: str, waypoint_type: str | None = None) -> list:
    # client.get() strips the response envelope and returns the data array directly,
    # so we can't use meta.total. Instead, stop when a page returns fewer than the limit.
    params: dict = {"limit": 20}
    if waypoint_type:
        params["type"] = waypoint_type
    results = []
    page = 1
    while True:
        params["page"] = page
        batch = client.get(f"/systems/{system_symbol}/waypoints", params=params)
        items = batch if isinstance(batch, list) else batch.get("data", [])
        if not items:
            break
        results.extend(items)
        if len(items) < 20:  # last page
            break
        page += 1
    return results


def get_waypoint(system_symbol: str, waypoint_symbol: str) -> dict:
    return client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}")


def get_jump_gate(system_symbol: str, waypoint_symbol: str) -> dict:
    return client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/jump-gate")


def get_construction(system_symbol: str, waypoint_symbol: str) -> dict:
    return client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/construction")


def supply_construction(system_symbol: str, waypoint_symbol: str, ship_symbol: str, trade_symbol: str, units: int) -> dict:
    return client.post(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/construction/supply",
        {"shipSymbol": ship_symbol, "tradeSymbol": trade_symbol, "units": units},
    )


# --- Markets ---

def get_market(system_symbol: str, waypoint_symbol: str) -> dict:
    return client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/market")


# --- Shipyards ---

def get_shipyard(system_symbol: str, waypoint_symbol: str) -> dict:
    return client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/shipyard")


# --- Factions ---

def get_factions() -> list:
    return client.get_all_pages("/factions")


def get_faction(symbol: str) -> dict:
    return client.get(f"/factions/{symbol}")


def get_my_factions() -> dict:
    return client.get("/my/factions")


# --- Server status ---

def get_status() -> dict:
    return client.get("/")


def get_supply_chain() -> dict:
    return client.get("/market/supply-chain")
