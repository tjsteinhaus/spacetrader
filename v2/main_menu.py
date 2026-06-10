#!/usr/bin/env python3
"""
SpaceTraders CLI — Main entry point.

Usage:
    python main.py

You need Python 3.10+ and the packages in requirements.txt.
On first run, register a new agent or paste an existing token when prompted.
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

import sync_api as agent_api
import sync_api as contracts_api
import display
import sync_api as fleet_api
import sync_api as universe_api
from client import SpaceTradersError, save_token

load_dotenv()
console = Console()

BANNER = r"""
  ____                  _____              _
 / ___| _ __   __ _  __|_   _| __ __ _  __| | ___ _ __ ___
 \___ \| '_ \ / _` |/ __|| || '__/ _` |/ _` |/ _ \ '__/ __|
  ___) | |_) | (_| | (__ | || | | (_| | (_| |  __/ |  \__ \
 |____/| .__/ \__,_|\___||_||_|  \__,_|\__,_|\___|_|  |___/
       |_|
"""


def _pause() -> None:
    Prompt.ask("\n[dim]Press Enter to continue[/dim]", default="")


def _pick(prompt: str, options: list[str]) -> str:
    """Show a numbered list and return the chosen item."""
    for i, opt in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]. {opt}")
    while True:
        raw = Prompt.ask(prompt)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        console.print("[red]Invalid choice.[/red]")


def _pick_ship(ships: list[dict]) -> dict | None:
    if not ships:
        display.error("You have no ships.")
        return None
    symbols = [s["symbol"] for s in ships]
    display.show_ships_list(ships)
    sym = _pick("Select a ship", symbols)
    return next((s for s in ships if s["symbol"] == sym), None)


def _pick_ship_symbol(ships: list[dict]) -> str | None:
    ship = _pick_ship(ships)
    return ship["symbol"] if ship else None


# ──────────────────────────────────────────────────────────────────────────────
#  Auth flow
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_authenticated() -> None:
    token = os.getenv("SPACETRADERS_TOKEN")
    if token:
        return

    console.print(Panel(
        "No token found. Choose an option:\n"
        "  [cyan]1[/cyan]. Register a new agent (free)\n"
        "  [cyan]2[/cyan]. Paste an existing token",
        title="Authentication Required",
        border_style="yellow",
    ))

    choice = Prompt.ask("Choice", choices=["1", "2"])

    if choice == "2":
        token = Prompt.ask("Paste your Bearer token").strip()
        # Verify it works
        try:
            me = agent_api.get_my_agent()
            save_token(token, me.get("symbol", ""))
            display.success(f"Logged in as [bold]{me.get('symbol')}[/bold]")
        except SpaceTradersError as e:
            display.error(f"Token invalid: {e}")
            sys.exit(1)
    else:
        _register_flow()


def _register_flow() -> None:
    console.print("\n[bold]Register a new agent[/bold]")
    console.print("Your callsign must be 3–14 alphanumeric characters.")

    factions_raw = universe_api.get_factions()
    recruiting = [f["symbol"] for f in factions_raw if f.get("isRecruiting")]
    console.print(f"Recruiting factions: {', '.join(recruiting)}")

    symbol = ""
    while not symbol:
        symbol = Prompt.ask("Callsign").strip().upper()
        if not (3 <= len(symbol) <= 14 and symbol.replace("-", "").replace("_", "").isalnum()):
            display.error("Invalid callsign. Use 3–14 alphanumeric characters.")
            symbol = ""

    faction = Prompt.ask("Starting faction", default="COSMIC").upper()

    try:
        result = agent_api.register(symbol, faction)
        token = result.get("token", "")
        ag = result.get("agent", {})
        save_token(token, ag.get("symbol", symbol))

        display.success(f"Registered as [bold]{ag.get('symbol')}[/bold]!")
        display.show_agent(ag)

        console.print("\n[bold yellow]⚠  Save your token — store it somewhere safe:[/bold yellow]")
        console.print(f"[dim]{token}[/dim]\n")

        contract = result.get("contract", {})
        if contract:
            display.info("You have a starting contract:")
            display.show_contract_detail(contract)

        ships = result.get("ships", [])
        if ships:
            display.info(f"Starting ships: {', '.join(s['symbol'] for s in ships)}")

    except SpaceTradersError as e:
        display.error(str(e))
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  Sub-menus
# ──────────────────────────────────────────────────────────────────────────────

def menu_fleet() -> None:
    while True:
        display.header("Fleet Management")
        options = [
            "List all ships",
            "Ship detail",
            "Orbit ship",
            "Dock ship",
            "Navigate ship",
            "Jump ship",
            "Set flight mode",
            "Refuel ship",
            "Extract resources",
            "Siphon resources",
            "Survey waypoint",
            "Sell cargo",
            "Purchase cargo",
            "Jettison cargo",
            "Transfer cargo",
            "Refine cargo",
            "Chart waypoint",
            "Repair ship",
            "Scrap ship info",
            "Buy new ship",
            "Back",
        ]
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i:2}[/cyan]. {opt}")
        raw = Prompt.ask("Choice")
        if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
            display.error("Invalid choice.")
            continue
        choice = options[int(raw) - 1]

        try:
            if choice == "Back":
                return

            ships = fleet_api.get_my_ships()

            if choice == "List all ships":
                display.show_ships_list(ships)
                _pause()

            elif choice == "Ship detail":
                sym = _pick_ship_symbol(ships)
                if sym:
                    ship = fleet_api.get_ship(sym)
                    display.show_ship_detail(ship)
                    _pause()

            elif choice == "Orbit ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.orbit(sym)
                    status = result.get("nav", {}).get("status", "?")
                    display.success(f"{sym} is now {status}.")

            elif choice == "Dock ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.dock(sym)
                    status = result.get("nav", {}).get("status", "?")
                    display.success(f"{sym} is now {status}.")

            elif choice == "Navigate ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    dest = Prompt.ask("Destination waypoint symbol").strip().upper()
                    result = fleet_api.navigate(sym, dest)
                    display.show_navigate_result(result)
                    _pause()

            elif choice == "Jump ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    dest = Prompt.ask("Target connected waypoint symbol").strip().upper()
                    result = fleet_api.jump(sym, dest)
                    display.show_navigate_result(result)
                    _pause()

            elif choice == "Set flight mode":
                sym = _pick_ship_symbol(ships)
                if sym:
                    mode = _pick("Flight mode", ["DRIFT", "STEALTH", "CRUISE", "BURN"])
                    result = fleet_api.patch_nav(sym, mode)
                    display.success(f"Flight mode set to {mode}.")

            elif choice == "Refuel ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    amt_str = Prompt.ask("Units to refuel (blank = full tank)", default="")
                    units = int(amt_str) if amt_str.strip().isdigit() else None
                    result = fleet_api.refuel(sym, units)
                    fuel = result.get("fuel", {})
                    display.success(f"Refueled. Fuel: {fuel.get('current')}/{fuel.get('capacity')}")
                    tx = result.get("transaction", {})
                    if tx:
                        display.info(f"Cost: {tx.get('totalPrice', 0):,} cr")

            elif choice == "Extract resources":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.extract(sym)
                    display.show_extraction(result)
                    _pause()

            elif choice == "Siphon resources":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.siphon(sym)
                    display.show_extraction(result)
                    _pause()

            elif choice == "Survey waypoint":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.survey(sym)
                    display.show_survey_result(result)
                    _pause()

            elif choice == "Sell cargo":
                sym = _pick_ship_symbol(ships)
                if sym:
                    ship_detail = fleet_api.get_ship(sym)
                    inventory = ship_detail.get("cargo", {}).get("inventory", [])
                    if not inventory:
                        display.info("Cargo hold is empty.")
                    else:
                        display.info("Cargo:")
                        for item in inventory:
                            console.print(f"  {item['symbol']}: {item['units']} units")
                        good = Prompt.ask("Good symbol to sell").strip().upper()
                        units = IntPrompt.ask("Units to sell")
                        result = fleet_api.sell_cargo(sym, good, units)
                        tx = result.get("transaction", {})
                        display.success(
                            f"Sold {tx.get('units')}x {tx.get('tradeSymbol')} "
                            f"for {tx.get('totalPrice', 0):,} cr "
                            f"({tx.get('pricePerUnit', 0):,}/unit)"
                        )
                        ag = result.get("agent", {})
                        display.info(f"Credits: {ag.get('credits', 0):,}")

            elif choice == "Purchase cargo":
                sym = _pick_ship_symbol(ships)
                if sym:
                    good = Prompt.ask("Good symbol to purchase").strip().upper()
                    units = IntPrompt.ask("Units to purchase")
                    result = fleet_api.purchase_cargo(sym, good, units)
                    tx = result.get("transaction", {})
                    display.success(
                        f"Purchased {tx.get('units')}x {tx.get('tradeSymbol')} "
                        f"for {tx.get('totalPrice', 0):,} cr"
                    )
                    ag = result.get("agent", {})
                    display.info(f"Credits: {ag.get('credits', 0):,}")

            elif choice == "Jettison cargo":
                sym = _pick_ship_symbol(ships)
                if sym:
                    good = Prompt.ask("Good symbol to jettison").strip().upper()
                    units = IntPrompt.ask("Units to jettison")
                    if Confirm.ask(f"Jettison {units}x {good}? This cannot be undone.", default=False):
                        result = fleet_api.jettison(sym, good, units)
                        cargo = result.get("cargo", {})
                        display.success(f"Jettisoned. Cargo: {cargo.get('units')}/{cargo.get('capacity')}")

            elif choice == "Transfer cargo":
                sym = _pick_ship_symbol(ships)
                if sym:
                    good = Prompt.ask("Good symbol to transfer").strip().upper()
                    units = IntPrompt.ask("Units to transfer")
                    target = Prompt.ask("Target ship symbol").strip().upper()
                    result = fleet_api.transfer_cargo(sym, good, units, target)
                    display.success("Cargo transferred.")

            elif choice == "Refine cargo":
                sym = _pick_ship_symbol(ships)
                if sym:
                    produce = _pick("Produce", ["IRON", "COPPER", "SILVER", "GOLD", "ALUMINUM", "PLATINUM", "URANITE", "MERITIUM", "FUEL"])
                    result = fleet_api.refine(sym, produce)
                    display.success(f"Refined {produce}.")
                    for item in result.get("produced", []):
                        console.print(f"  [green]+{item['units']}x {item['tradeSymbol']}[/green]")

            elif choice == "Chart waypoint":
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.chart(sym)
                    wp = result.get("waypoint", {})
                    display.success(f"Charted {wp.get('symbol', '?')} ({wp.get('type', '?')}).")
                    tx = result.get("transaction", {})
                    if tx:
                        display.info(f"Reward: {tx.get('totalPrice', 0):,} cr")

            elif choice == "Repair ship":
                sym = _pick_ship_symbol(ships)
                if sym:
                    cost = fleet_api.get_repair_cost(sym)
                    tx = cost.get("transaction", {})
                    price = tx.get("totalPrice", 0)
                    if Confirm.ask(f"Repair {sym} for {price:,} cr?"):
                        result = fleet_api.repair(sym)
                        display.success("Ship repaired.")

            elif choice == "Scrap ship info":
                sym = _pick_ship_symbol(ships)
                if sym:
                    value = fleet_api.get_scrap_value(sym)
                    tx = value.get("transaction", {})
                    display.info(f"Scrap value of {sym}: {tx.get('totalPrice', 0):,} cr")
                    if Confirm.ask("Scrap this ship? This cannot be undone.", default=False):
                        result = fleet_api.scrap(sym)
                        display.success("Ship scrapped.")

            elif choice == "Buy new ship":
                wp = Prompt.ask("Shipyard waypoint symbol").strip().upper()
                system = "-".join(wp.split("-")[:2])
                shipyard = universe_api.get_shipyard(system, wp)
                display.show_shipyard(shipyard)
                ship_type = Prompt.ask("Ship type to buy (or blank to cancel)", default="").strip().upper()
                if ship_type:
                    result = fleet_api.purchase_ship(ship_type, wp)
                    ship = result.get("ship", {})
                    display.success(f"Purchased {ship.get('symbol')}!")
                    tx = result.get("transaction", {})
                    display.info(f"Cost: {tx.get('price', 0):,} cr")

        except SpaceTradersError as e:
            display.error(str(e))
            _pause()


def menu_contracts() -> None:
    while True:
        display.header("Contracts")
        options = ["List contracts", "Contract detail", "Accept contract", "Deliver cargo", "Fulfill contract", "Negotiate new contract", "Back"]
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/cyan]. {opt}")
        raw = Prompt.ask("Choice")
        if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
            display.error("Invalid choice.")
            continue
        choice = options[int(raw) - 1]

        try:
            if choice == "Back":
                return

            if choice == "List contracts":
                cs = contracts_api.get_contracts()
                display.show_contracts(cs)
                _pause()

            elif choice == "Contract detail":
                cs = contracts_api.get_contracts()
                display.show_contracts(cs)
                cid = Prompt.ask("Contract ID").strip()
                c = contracts_api.get_contract(cid)
                display.show_contract_detail(c)
                _pause()

            elif choice == "Accept contract":
                cs = contracts_api.get_contracts()
                display.show_contracts(cs)
                cid = Prompt.ask("Contract ID to accept").strip()
                result = contracts_api.accept_contract(cid)
                display.success("Contract accepted!")
                ag = result.get("agent", {})
                display.info(f"Credits: {ag.get('credits', 0):,}")
                display.show_contract_detail(result.get("contract", {}))

            elif choice == "Deliver cargo":
                cs = contracts_api.get_contracts()
                display.show_contracts(cs)
                cid = Prompt.ask("Contract ID").strip()
                ships = fleet_api.get_my_ships()
                sym = _pick_ship_symbol(ships)
                if sym:
                    trade = Prompt.ask("Trade symbol to deliver").strip().upper()
                    units = IntPrompt.ask("Units to deliver")
                    result = contracts_api.deliver_contract(cid, sym, trade, units)
                    display.success("Cargo delivered!")
                    display.show_contract_detail(result.get("contract", {}))

            elif choice == "Fulfill contract":
                cs = contracts_api.get_contracts()
                display.show_contracts(cs)
                cid = Prompt.ask("Contract ID to fulfill").strip()
                result = contracts_api.fulfill_contract(cid)
                display.success("Contract fulfilled!")
                ag = result.get("agent", {})
                display.info(f"Credits: {ag.get('credits', 0):,}")

            elif choice == "Negotiate new contract":
                ships = fleet_api.get_my_ships()
                sym = _pick_ship_symbol(ships)
                if sym:
                    result = fleet_api.negotiate_contract(sym)
                    display.success("New contract negotiated!")
                    display.show_contract_detail(result.get("contract", {}))

        except SpaceTradersError as e:
            display.error(str(e))
            _pause()


def menu_universe() -> None:
    while True:
        display.header("Universe / Systems")
        options = [
            "View waypoints in a system",
            "View waypoint detail",
            "View market",
            "View shipyard",
            "View jump gate",
            "List factions",
            "Back",
        ]
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/cyan]. {opt}")
        raw = Prompt.ask("Choice")
        if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
            display.error("Invalid choice.")
            continue
        choice = options[int(raw) - 1]

        try:
            if choice == "Back":
                return

            if choice == "View waypoints in a system":
                system = Prompt.ask("System symbol").strip().upper()
                wp_type = Prompt.ask("Filter by type (blank for all)", default="").strip().upper() or None
                wps = universe_api.get_waypoints(system, wp_type)
                display.show_waypoints(wps, f"Waypoints in {system}")
                _pause()

            elif choice == "View waypoint detail":
                wp = Prompt.ask("Waypoint symbol").strip().upper()
                system = "-".join(wp.split("-")[:2])
                data = universe_api.get_waypoint(system, wp)
                display.show_waypoints([data], f"Waypoint {wp}")
                _pause()

            elif choice == "View market":
                wp = Prompt.ask("Market waypoint symbol").strip().upper()
                system = "-".join(wp.split("-")[:2])
                data = universe_api.get_market(system, wp)
                display.show_market(data)
                _pause()

            elif choice == "View shipyard":
                wp = Prompt.ask("Shipyard waypoint symbol").strip().upper()
                system = "-".join(wp.split("-")[:2])
                data = universe_api.get_shipyard(system, wp)
                display.show_shipyard(data)
                _pause()

            elif choice == "View jump gate":
                wp = Prompt.ask("Jump gate waypoint symbol").strip().upper()
                system = "-".join(wp.split("-")[:2])
                data = universe_api.get_jump_gate(system, wp)
                connections = data.get("connections", [])
                display.info(f"Jump gate {wp} connects to: {', '.join(connections) or 'none'}")
                _pause()

            elif choice == "List factions":
                factions = universe_api.get_factions()
                display.show_factions(factions)
                _pause()

        except SpaceTradersError as e:
            display.error(str(e))
            _pause()


# ──────────────────────────────────────────────────────────────────────────────
#  Main loop
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    console.print(f"[bold cyan]{BANNER}[/bold cyan]")
    console.print("[dim]An API-driven space trading game | spacetraders.io[/dim]\n")

    _ensure_authenticated()

    while True:
        # Show agent summary at top of main menu
        try:
            me = agent_api.get_my_agent()
        except SpaceTradersError:
            me = {}

        if me:
            console.print(
                f"\n[bold]{me.get('symbol')}[/bold]  "
                f"[green]{me.get('credits', 0):,} cr[/green]  "
                f"Ships: {me.get('shipCount', '?')}  "
                f"HQ: [dim]{me.get('headquarters', '?')}[/dim]"
            )

        display.header("Main Menu")
        options = [
            "Agent info",
            "Fleet management",
            "Contracts",
            "Universe / Systems",
            "Server status",
            "Quit",
        ]
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/cyan]. {opt}")

        raw = Prompt.ask("Choice")
        if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
            display.error("Invalid choice.")
            continue

        choice = options[int(raw) - 1]

        try:
            if choice == "Quit":
                console.print("[dim]Goodbye, Commander.[/dim]")
                sys.exit(0)

            elif choice == "Agent info":
                display.show_agent(me)
                _pause()

            elif choice == "Fleet management":
                menu_fleet()

            elif choice == "Contracts":
                menu_contracts()

            elif choice == "Universe / Systems":
                menu_universe()

            elif choice == "Server status":
                data = universe_api.get_status()
                display.show_server_status(data)
                _pause()

        except SpaceTradersError as e:
            display.error(str(e))
            _pause()
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
            sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/dim]")
