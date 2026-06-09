#!/usr/bin/env python3
"""
refresh_db.py — Full API sync to game_data.db.

Fetches all waypoints, market listings, and contracts for the current system
and persists them to the SQLite database. Safe to run alongside a live play.py.

Usage:
    python3 refresh_db.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich import box

load_dotenv()

import db
import universe as universe_api
import contracts as contracts_api
from client import SpaceTradersError

# Read SYSTEM from play.py config (avoids duplicating it here)
sys.path.insert(0, str(Path(__file__).parent))
try:
    from play import SYSTEM
except Exception:
    SYSTEM = "X1-GK27"

console = Console()


def refresh(system: str = SYSTEM) -> dict:
    """Run a full refresh and return a summary dict."""
    db.init_db()
    stats: dict = {
        "waypoints": 0,
        "markets": 0,
        "goods_indexed": 0,
        "contracts": 0,
        "errors": [],
    }

    # ── 1. Waypoints ──────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching waypoints...", total=None)
        try:
            waypoints = universe_api.get_waypoints(system)
            stats["waypoints"] = db.upsert_waypoints(waypoints)
            progress.update(task, description=f"[green]Fetched {stats['waypoints']} waypoints")
        except SpaceTradersError as e:
            stats["errors"].append(f"Waypoints: {e}")
            progress.update(task, description=f"[red]Waypoints failed: {e}")
            waypoints = []

    # ── 2. Market listings ────────────────────────────────────────────────
    market_wps = [
        wp["symbol"]
        for wp in waypoints
        if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
    ]

    goods_set: set[str] = set()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Indexing markets...", total=len(market_wps))
        for wp in market_wps:
            try:
                data = universe_api.get_market(system, wp)
                db.upsert_market_listings(wp, data)
                # Also upsert any live prices that came back (tradeGoods)
                trade_goods = data.get("tradeGoods", [])
                if trade_goods:
                    db.upsert_market_prices(wp, trade_goods)
                for category in ("exports", "imports", "exchange"):
                    for g in data.get(category, []):
                        sym = g.get("symbol", "")
                        if sym:
                            goods_set.add(sym)
                stats["markets"] += 1
            except SpaceTradersError as e:
                stats["errors"].append(f"Market {wp}: {e}")
            progress.update(task, advance=1, description=f"[cyan]{wp}")
            time.sleep(0.35)  # stay well within rate limit

    stats["goods_indexed"] = len(goods_set)

    # ── 3. Contracts ──────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching contracts...", total=None)
        try:
            all_contracts = contracts_api.get_contracts()
            for c in all_contracts:
                db.upsert_contract(c)
            stats["contracts"] = len(all_contracts)
            progress.update(task, description=f"[green]Fetched {stats['contracts']} contracts")
        except SpaceTradersError as e:
            stats["errors"].append(f"Contracts: {e}")
            progress.update(task, description=f"[red]Contracts failed: {e}")

    return stats


def print_summary(stats: dict, system: str) -> None:
    db_stats = db.get_db_stats()

    console.print()
    console.rule("[bold cyan]Refresh Complete")

    # Summary table
    t = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    t.add_column("Category", style="bold")
    t.add_column("This Run", justify="right")
    t.add_column("DB Total", justify="right")
    t.add_row("Waypoints fetched",   str(stats["waypoints"]),     str(db_stats["waypoints"]["count"]))
    t.add_row("Markets indexed",     str(stats["markets"]),       str(db_stats["market_listings"]["count"]) + " listings")
    t.add_row("Unique goods found",  str(stats["goods_indexed"]), str(db_stats["market_prices"]["count"]) + " prices")
    t.add_row("Contracts loaded",    str(stats["contracts"]),     str(db_stats["contracts"]["count"]))
    console.print(t)

    if stats["errors"]:
        console.print("\n[yellow]Warnings / errors:[/yellow]")
        for err in stats["errors"]:
            console.print(f"  [yellow]• {err}[/yellow]")

    # Active contracts quick summary
    active = db.get_active_contracts()
    if active:
        console.print()
        ct = Table(title="Active Contracts", box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
        ct.add_column("Contract ID", style="dim")
        ct.add_column("Good")
        ct.add_column("Progress", justify="right")
        ct.add_column("Destination")
        for c in active:
            for d in c["deliver"]:
                pct = int(100 * d["units_fulfilled"] / max(1, d["units_required"]))
                bar = ("█" * (pct // 10)).ljust(10)
                ct.add_row(
                    c["id"][:12] + "…",
                    f"[bold]{d['trade_symbol']}[/bold]",
                    f"{d['units_fulfilled']}/{d['units_required']} [{pct}%] {bar}",
                    d["destination_symbol"] or "",
                )
        console.print(ct)

        # Sourcing hint for each contract good
        console.print()
        for c in active:
            for d in c["deliver"]:
                good = d["trade_symbol"]
                minable = db.can_be_mined(good, system)
                buyable = db.can_be_bought(good, system)
                if minable:
                    wps = ", ".join(m["waypoint_symbol"] for m in minable)
                    console.print(f"  [green]⛏  {good}[/green] can be mined at: [bold]{wps}[/bold]")
                else:
                    ore_hint = db.SMELTED_GOODS.get(good)
                    if ore_hint:
                        console.print(
                            f"  [red]✗  {good}[/red] is a [bold]smelted/processed good[/bold] "
                            f"— cannot be mined. (Raw ore would be {ore_hint})"
                        )
                    else:
                        console.print(f"  [yellow]?  {good}[/yellow] — no mining deposits found in {system}")
                if buyable:
                    markets = ", ".join(
                        f"{b['waypoint_symbol']} ({b['listing_type']}"
                        + (f" @ {b['purchase_price']:,} cr" if b['purchase_price'] else "")
                        + ")"
                        for b in buyable
                    )
                    console.print(f"     [cyan]🛒 Can be bought at:[/cyan] {markets}")
                else:
                    console.print(f"     [red]No markets in {system} export {good}[/red]")

    console.print()
    console.print(f"[dim]DB: {db.DB_PATH}[/dim]")


if __name__ == "__main__":
    console.rule("[bold cyan]SpaceTraders DB Refresh")
    console.print(f"System: [bold]{SYSTEM}[/bold]\n")
    results = refresh(SYSTEM)
    print_summary(results, SYSTEM)
