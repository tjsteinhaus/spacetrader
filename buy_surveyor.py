#!/usr/bin/env python3
"""One-off script: buy a SHIP_SURVEYOR at H52 and navigate it to FD5D.
Run ONLY while the main daemon is stopped."""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

import fleet as fleet_api
import universe as universe_api
import agent as agent_api
from client import SpaceTradersError

SYSTEM = "X1-HU91"
COMMAND_SHIP = "TYLERMASTERY-1"
H52 = "X1-HU91-H52"
FD5D = "X1-HU91-FD5D"


def wait_for_ship(ship_symbol):
    while True:
        ship = fleet_api.get_ship(ship_symbol)
        nav = ship["nav"]
        if nav["status"] != "IN_TRANSIT":
            print(f"  {ship_symbol} arrived at {nav['waypointSymbol']}")
            return
        arrival = nav["route"].get("arrival", "")
        secs = 0
        if arrival:
            try:
                dt = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                secs = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
        print(f"  {ship_symbol} in transit (~{secs}s)...")
        time.sleep(min(secs, 10) if secs > 0 else 5)


def navigate_to(ship_symbol, destination):
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "IN_TRANSIT":
        # If in transit toward destination, just wait
        if ship["nav"]["waypointSymbol"] == destination:
            print(f"  {ship_symbol} already en route to {destination}, waiting...")
            wait_for_ship(ship_symbol)
            return
        wait_for_ship(ship_symbol)
        ship = fleet_api.get_ship(ship_symbol)
        if ship["nav"]["waypointSymbol"] == destination:
            return
    if ship["nav"]["status"] == "DOCKED":
        # Check actual current location (not route destination)
        if ship["nav"]["waypointSymbol"] == destination:
            print(f"  {ship_symbol} already at {destination}")
            return
        fleet_api.orbit(ship_symbol)
    print(f"  Navigating {ship_symbol} → {destination}...")
    try:
        fleet_api.navigate(ship_symbol, destination)
    except SpaceTradersError as e:
        if e.code == 4203:
            print(f"  Insufficient fuel — refueling at current location first")
            fleet_api.dock(ship_symbol)
            fleet_api.refuel(ship_symbol)
            fleet_api.orbit(ship_symbol)
            fleet_api.navigate(ship_symbol, destination)
        else:
            raise
    wait_for_ship(ship_symbol)


def ensure_docked(ship_symbol):
    ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] == "IN_TRANSIT":
        wait_for_ship(ship_symbol)
        ship = fleet_api.get_ship(ship_symbol)
    if ship["nav"]["status"] != "DOCKED":
        fleet_api.dock(ship_symbol)
        print(f"  {ship_symbol} docked")


me = agent_api.get_my_agent()
print(f"Agent: {me['symbol']} | Credits: {me['credits']:,}")

# Step 1: Get TM-1 to H52
print(f"\n[1] Navigating {COMMAND_SHIP} to H52...")
navigate_to(COMMAND_SHIP, H52)
ensure_docked(COMMAND_SHIP)

# Refuel TM-1 while we're here
try:
    r = fleet_api.refuel(COMMAND_SHIP)
    f = r.get("fuel", {})
    print(f"  Refueled TM-1: {f.get('current')}/{f.get('capacity')}")
except SpaceTradersError as e:
    print(f"  Refuel skipped: {e}")

# Step 2: Check shipyard
print(f"\n[2] Querying H52 shipyard...")
yard = universe_api.get_shipyard(SYSTEM, H52)
ships = yard.get("ships", [])
if not ships:
    print("ERROR: No ship listings at H52 — need a ship physically docked there.")
    print("Hint: TM-1 should be docked there now. Try again.")
    sys.exit(1)

for s in ships:
    print(f"  {s['type']:30s}  {s['purchasePrice']:>8,} cr  supply={s.get('supply','?')}")

# Step 3: Buy SHIP_SURVEYOR
surveyor_info = next((s for s in ships if s["type"] == "SHIP_SURVEYOR"), None)
if not surveyor_info:
    print("ERROR: SHIP_SURVEYOR not listed at H52!")
    sys.exit(1)

price = surveyor_info["purchasePrice"]
me = agent_api.get_my_agent()
print(f"\n[3] Buying SHIP_SURVEYOR for {price:,} cr (have {me['credits']:,})...")
if me["credits"] - price < 30_000:
    print(f"ERROR: Would breach 30k credit reserve!")
    sys.exit(1)

result = fleet_api.purchase_ship("SHIP_SURVEYOR", H52)
new_ship = result.get("ship", {})
ag = result.get("agent", {})
new_symbol = new_ship.get("symbol", "")
print(f"  Bought {new_symbol}! Credits remaining: {ag.get('credits', 0):,}")

# Verify fuel capacity
fuel = new_ship.get("fuel", {})
fuel_cap = fuel.get("capacity", 0)
fuel_cur = fuel.get("current", 0)
print(f"  Fuel: {fuel_cur}/{fuel_cap}")
if fuel_cap < 38:
    print(f"WARNING: Fuel capacity {fuel_cap} may be too low to reach FD5D (dist ~38)!")

# Step 4: Navigate new surveyor to FD5D
print(f"\n[4] Navigating {new_symbol} to FD5D...")
navigate_to(new_symbol, FD5D)

# Leave it in orbit at FD5D
ship = fleet_api.get_ship(new_symbol)
if ship["nav"]["status"] == "DOCKED":
    fleet_api.orbit(new_symbol)
    print(f"  {new_symbol} now orbiting FD5D")

print(f"\nDone! {new_symbol} is at FD5D, ready to survey.")
print("Restart the daemon — it will detect the new surveyor and launch a thread for it.")
