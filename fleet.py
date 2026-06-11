"""Fleet / ship endpoints."""
import client


def get_my_ships() -> list:
    return client.get_all_pages("/my/ships")


def get_ship(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}")


def get_ship_nav(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/nav")


def get_ship_cargo(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/cargo")


def get_ship_cooldown(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/cooldown")


# --- Navigation ---

def orbit(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/orbit")


def dock(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/dock")


def navigate(symbol: str, waypoint_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/navigate", {"waypointSymbol": waypoint_symbol})


def jump(symbol: str, system_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/jump", {"systemSymbol": system_symbol})


def warp(symbol: str, system_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/warp", {"systemSymbol": system_symbol})


def patch_nav(symbol: str, flight_mode: str) -> dict:
    return client.patch(f"/my/ships/{symbol}/nav", {"flightMode": flight_mode})


# --- Fuel & Cargo ---

def refuel(symbol: str, units: int | None = None) -> dict:
    body = {}
    if units is not None:
        body["units"] = units
    return client.post(f"/my/ships/{symbol}/refuel", body)


def purchase_cargo(symbol: str, good: str, units: int) -> dict:
    return client.post(f"/my/ships/{symbol}/purchase", {"symbol": good, "units": units})


def sell_cargo(symbol: str, good: str, units: int) -> dict:
    return client.post(f"/my/ships/{symbol}/sell", {"symbol": good, "units": units})


def jettison(symbol: str, good: str, units: int) -> dict:
    return client.post(f"/my/ships/{symbol}/jettison", {"symbol": good, "units": units})


def transfer_cargo(symbol: str, trade_symbol: str, units: int, target_ship: str) -> dict:
    return client.post(
        f"/my/ships/{symbol}/transfer",
        {"tradeSymbol": trade_symbol, "units": units, "shipSymbol": target_ship},
    )


# --- Mining / Extraction ---

def extract(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/extract")


def extract_with_survey(symbol: str, survey: dict) -> dict:
    return client.post(f"/my/ships/{symbol}/extract/survey", survey)


def siphon(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/siphon")


def survey(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/survey")


# --- Scanning ---

def scan_systems(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/scan/systems")


def scan_waypoints(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/scan/waypoints")


def scan_ships(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/scan/ships")


# --- Ship upgrades / modifications ---

def get_mounts(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/mounts")


def install_mount(symbol: str, mount_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/mounts/install", {"symbol": mount_symbol})


def remove_mount(symbol: str, mount_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/mounts/remove", {"symbol": mount_symbol})


def get_modules(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/modules")


def install_module(symbol: str, module_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/modules/install", {"symbol": module_symbol})


def remove_module(symbol: str, module_symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/modules/remove", {"symbol": module_symbol})


# --- Chart, Repair, Scrap ---

def chart(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/chart")


def repair(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/repair")


def get_repair_cost(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/repair")


def scrap(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/scrap")


def get_scrap_value(symbol: str) -> dict:
    return client.get(f"/my/ships/{symbol}/scrap")


# --- Refine ---

def refine(symbol: str, produce: str) -> dict:
    return client.post(f"/my/ships/{symbol}/refine", {"produce": produce})


# --- Purchase ship ---

def purchase_ship(ship_type: str, waypoint_symbol: str) -> dict:
    return client.post("/my/ships", {"shipType": ship_type, "waypointSymbol": waypoint_symbol})


# --- Negotiate contract ---

def negotiate_contract(symbol: str) -> dict:
    return client.post(f"/my/ships/{symbol}/negotiate/contract")
