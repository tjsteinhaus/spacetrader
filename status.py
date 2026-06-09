#!/usr/bin/env python3
"""
status.py — Query the SpaceTraders game state database.

Simple mode (no args):
    python3 status.py
    → Active contracts, delivery progress, and sourcing analysis for each good

Argument mode:
    python3 status.py find <GOOD>          — full sourcing breakdown
    python3 status.py contract             — contract detail
    python3 status.py waypoints [--type X] — list waypoints with traits
    python3 status.py market <WAYPOINT>    — show prices at a waypoint
    python3 status.py refresh              — re-pull everything from the API
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

load_dotenv()

import db

sys.path.insert(0, str(Path(__file__).parent))
try:
    from play import SYSTEM
except Exception:
    SYSTEM = "X1-GK27"

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    ago = time.time() - ts
    if ago < 60:
        return f"{int(ago)}s ago"
    if ago < 3600:
        return f"{int(ago/60)}m ago"
    return f"{int(ago/3600)}h ago"


def _deadline_str(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() / 3600)
        if hours < 0:
            return f"[red]EXPIRED {abs(hours)}h ago[/red]"
        if hours < 6:
            return f"[red]{hours}h left[/red]"
        if hours < 24:
            return f"[yellow]{hours}h left[/yellow]"
        return f"[green]{hours//24}d {hours%24}h left[/green]"
    except Exception:
        return iso


def _sourcing_analysis(good: str, system: str) -> None:
    """Print detailed sourcing analysis for a trade good."""
    minable = db.can_be_mined(good, system)
    buyable = db.can_be_bought(good, system)
    ore_hint = db.SMELTED_GOODS.get(good)

    console.print(f"\n[bold white]Sourcing analysis for [cyan]{good}[/cyan]:[/bold white]")

    # Mining
    if minable:
        console.print(f"  [green bold]⛏  CAN BE MINED[/green bold]")
        for m in minable:
            console.print(
                f"     • {m['waypoint_symbol']}  [dim]({m['waypoint_type']} — "
                f"trait: {m['trait_symbol']})[/dim]"
            )
    else:
        if ore_hint:
            console.print(
                f"  [red bold]✗  CANNOT BE MINED[/red bold]  "
                f"[dim]{good} is a smelted/processed good. "
                f"The raw ore is {ore_hint} — but you'd need a smelter market to refine it.[/dim]"
            )
        else:
            console.print(
                f"  [yellow]?  No mining deposits for {good} found in {system}[/yellow]  "
                f"[dim](may be available in other systems, or data not yet refreshed)[/dim]"
            )

    # Buying
    if buyable:
        console.print(f"  [cyan bold]🛒  CAN BE BOUGHT[/cyan bold]")
        bt = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 1))
        bt.add_column("Waypoint")
        bt.add_column("Type")
        bt.add_column("Supply")
        bt.add_column("Buy Price", justify="right")
        bt.add_column("Price Age")
        for b in buyable:
            price_str = f"{b['purchase_price']:,} cr" if b['purchase_price'] else "[dim]unknown[/dim]"
            supply_color = {
                "ABUNDANT": "green", "HIGH": "green", "MODERATE": "yellow",
                "LIMITED": "red", "SCARCE": "red",
            }.get(b["supply"] or "", "white")
            bt.add_row(
                b["waypoint_symbol"],
                b["listing_type"],
                f"[{supply_color}]{b['supply'] or '?'}[/{supply_color}]",
                price_str,
                _ts_ago(b["last_price_update"]),
            )
        console.print(bt)
    else:
        console.print(
            f"  [red]✗  No markets in {system} export or exchange {good}[/red]  "
            f"[dim](may need refresh, or good is not available in this system)[/dim]"
        )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_default(system: str) -> None:
    """Default: show active contracts + sourcing analysis for each contract good."""
    db.init_db()
    db_stats = db.get_db_stats()

    # Header: DB freshness
    last_refresh = db_stats.get("last_market_refresh")
    last_contract = db_stats.get("last_contract_refresh")
    console.print(
        Panel(
            f"System: [bold]{system}[/bold]   "
            f"Markets: [cyan]{db_stats['market_listings']['count']}[/cyan] listings   "
            f"Last market refresh: [dim]{_ts_ago(last_refresh)}[/dim]   "
            f"Last contract sync: [dim]{_ts_ago(last_contract)}[/dim]",
            title="[bold cyan]SpaceTraders Status",
            box=box.ROUNDED,
        )
    )

    active = db.get_active_contracts()
    if not active:
        console.print("\n[yellow]No active (non-fulfilled) contracts in DB. Run:[/yellow]")
        console.print("  [bold]python3 refresh_db.py[/bold]\n")
        return

    # Contract progress table
    ct = Table(title="Active Contracts", box=box.ROUNDED, show_header=True, header_style="bold magenta")
    ct.add_column("ID", style="dim", no_wrap=True)
    ct.add_column("Type")
    ct.add_column("Good", style="bold")
    ct.add_column("Progress", justify="right")
    ct.add_column("Pct", justify="right")
    ct.add_column("Destination")
    ct.add_column("Deadline")
    ct.add_column("Reward", justify="right")

    for c in active:
        for d in c["deliver"]:
            req = d["units_required"]
            fulf = d["units_fulfilled"]
            pct = int(100 * fulf / max(1, req))
            bar = ("█" * (pct // 10)).ljust(10, "░")
            color = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
            ct.add_row(
                c["id"][:14] + "…",
                c["type"],
                d["trade_symbol"],
                f"{fulf:,}/{req:,}",
                f"[{color}]{bar} {pct}%[/{color}]",
                d["destination_symbol"] or "—",
                _deadline_str(c["deadline"]),
                f"{c['on_fulfilled']:,} cr" if c["on_fulfilled"] else "—",
            )

    console.print(ct)

    # Sourcing analysis for every unique contract good
    contract_goods: set[str] = set()
    for c in active:
        for d in c["deliver"]:
            contract_goods.add(d["trade_symbol"])

    console.rule("[bold]Sourcing Analysis")
    for good in sorted(contract_goods):
        _sourcing_analysis(good, system)

    console.print()


def cmd_find(good: str, system: str) -> None:
    """Find: detailed sourcing breakdown for a specific good."""
    db.init_db()
    good = good.upper()
    console.print(f"\n[bold cyan]SpaceTraders — Find: {good}[/bold cyan]  [dim](System: {system})[/dim]")
    _sourcing_analysis(good, system)

    # Also check if the ORE variant of this processed good can be bought
    ore = db.SMELTED_GOODS.get(good)
    if ore:
        console.print(f"\n[dim]Checking raw ore ({ore}) availability in case you can smelt it:[/dim]")
        _sourcing_analysis(ore, system)

    console.print()


def cmd_contract(system: str) -> None:
    """Show detailed contract view including fulfilled ones."""
    db.init_db()
    with db._conn() as con:
        contracts = con.execute(
            """SELECT id, faction_symbol, type, accepted, fulfilled,
                      expiration, deadline, deadline_to_accept,
                      on_accepted, on_fulfilled, last_updated
               FROM contracts ORDER BY last_updated DESC"""
        ).fetchall()

    if not contracts:
        console.print("[yellow]No contracts in DB. Run: python3 refresh_db.py[/yellow]")
        return

    for c in contracts:
        cid, faction, ctype, accepted, fulfilled = c[0], c[1], c[2], c[3], c[4]
        expiration, deadline, deadline_to_accept = c[5], c[6], c[7]
        on_accepted, on_fulfilled = c[8], c[9]
        last_updated = c[10]

        status_tag = "[green]FULFILLED[/green]" if fulfilled else (
            "[cyan]ACCEPTED[/cyan]" if accepted else "[yellow]AVAILABLE[/yellow]"
        )

        with db._conn() as con:
            deliverables = con.execute(
                "SELECT trade_symbol, destination_symbol, units_required, units_fulfilled "
                "FROM contract_deliverables WHERE contract_id = ?", (cid,)
            ).fetchall()

        lines = [
            f"ID: [bold]{cid}[/bold]  Status: {status_tag}  Type: {ctype}  Faction: {faction}",
            f"Reward: {on_accepted:,} on accept + {on_fulfilled:,} on fulfill" if on_accepted else "",
            f"Deadline: {_deadline_str(deadline)}  Expiry: {_deadline_str(expiration)}",
            f"[dim]Last synced: {_ts_ago(last_updated)}[/dim]",
        ]
        if deliverables:
            lines.append("")
            for d in deliverables:
                pct = int(100 * d[3] / max(1, d[2]))
                bar = ("█" * (pct // 10)).ljust(10, "░")
                lines.append(f"  {d[0]:30s}  {d[3]:>5,}/{d[2]:<5,}  {bar} {pct}%  → {d[1] or '?'}")

        console.print(Panel("\n".join(l for l in lines if l), box=box.ROUNDED))
        console.print()


def cmd_waypoints(system: str, type_filter: str | None = None) -> None:
    """List all waypoints with their traits."""
    db.init_db()
    wps = db.get_all_waypoints(system)
    if type_filter:
        wps = [w for w in wps if w["type"].upper() == type_filter.upper()]

    if not wps:
        console.print(f"[yellow]No waypoints found in DB for {system}. Run: python3 refresh_db.py[/yellow]")
        return

    t = Table(
        title=f"Waypoints in {system}" + (f" (type={type_filter})" if type_filter else ""),
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    t.add_column("Symbol", style="bold", no_wrap=True)
    t.add_column("Type")
    t.add_column("Coords", justify="right")
    t.add_column("Traits")

    for wp in sorted(wps, key=lambda w: w["symbol"]):
        traits = ", ".join(tr["symbol"] for tr in wp["traits"])
        t.add_row(
            wp["symbol"],
            wp["type"],
            f"({wp['x']}, {wp['y']})",
            traits or "[dim]—[/dim]",
        )
    console.print(t)


def cmd_market(waypoint_symbol: str) -> None:
    """Show cached prices for a specific waypoint."""
    db.init_db()
    prices = db.get_market_prices_for_waypoint(waypoint_symbol)
    if not prices:
        console.print(f"[yellow]No price data for {waypoint_symbol}. "
                      f"A ship must be docked there to record live prices.[/yellow]")
        return

    t = Table(
        title=f"Prices at {waypoint_symbol}",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    t.add_column("Good", style="bold")
    t.add_column("Type")
    t.add_column("Supply")
    t.add_column("Activity")
    t.add_column("Buy", justify="right")
    t.add_column("Sell", justify="right")
    t.add_column("Volume", justify="right")
    t.add_column("Age")

    supply_colors = {
        "ABUNDANT": "green", "HIGH": "green", "MODERATE": "yellow",
        "LIMITED": "red", "SCARCE": "red",
    }

    for p in prices:
        sc = supply_colors.get(p["supply"] or "", "white")
        t.add_row(
            p["trade_symbol"],
            p["listing_type"] or "—",
            f"[{sc}]{p['supply'] or '?'}[/{sc}]",
            p["activity"] or "—",
            f"{p['purchase_price']:,}" if p["purchase_price"] else "—",
            f"{p['sell_price']:,}" if p["sell_price"] else "—",
            str(p["trade_volume"]) if p["trade_volume"] else "—",
            _ts_ago(p["last_updated"]),
        )

    console.print(t)


def cmd_refresh() -> None:
    """Inline refresh — delegates to refresh_db.py logic."""
    import refresh_db
    results = refresh_db.refresh(SYSTEM)
    refresh_db.print_summary(results, SYSTEM)


def cmd_arbitrage(system: str, min_margin: int = 100) -> None:
    """Show goods where buying at one market and selling at another turns a profit."""
    db.init_db()
    opportunities = db.get_arbitrage_opportunities(system, min_margin)

    if not opportunities:
        console.print(
            f"[yellow]No arbitrage found with margin ≥ {min_margin:,} cr/unit.[/yellow]\n"
            f"[dim]Prices require ships to have docked at both markets. "
            f"Run play.py longer to build price history, or lower the threshold with --min N.[/dim]"
        )
        return

    supply_colors = {
        "ABUNDANT": "green", "HIGH": "green", "MODERATE": "yellow",
        "LIMITED": "red", "SCARCE": "red",
    }

    t = Table(
        title=f"Arbitrage Opportunities — {system}  (min {min_margin:,} cr/unit margin)",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    t.add_column("Good", style="bold")
    t.add_column("Buy At")
    t.add_column("Buy Price", justify="right")
    t.add_column("Supply")
    t.add_column("Sell At")
    t.add_column("Sell Price", justify="right")
    t.add_column("Margin", justify="right", style="bold green")
    t.add_column("ROI %", justify="right")
    t.add_column("Data Age")

    for o in opportunities:
        bsc = supply_colors.get(o["buy_supply"] or "", "white")
        t.add_row(
            o["trade_symbol"],
            o["buy_at"],
            f"{o['buy_price']:,}",
            f"[{bsc}]{o['buy_supply'] or '?'}[/{bsc}]",
            o["sell_at"],
            f"{o['sell_price']:,}",
            f"+{o['margin']:,}",
            f"{o['pct_margin']}%",
            _ts_ago(o["oldest_data"]),
        )

    console.print(t)
    console.print(
        "\n[dim]Prices shift dynamically with supply. Verify before committing a hauler run.[/dim]"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if not args:
        cmd_default(SYSTEM)
        return

    cmd = args[0].lower()

    if cmd == "find" and len(args) >= 2:
        cmd_find(args[1], SYSTEM)
    elif cmd == "contract":
        cmd_contract(SYSTEM)
    elif cmd == "waypoints":
        type_filter = None
        if "--type" in args:
            idx = args.index("--type")
            if idx + 1 < len(args):
                type_filter = args[idx + 1]
        cmd_waypoints(SYSTEM, type_filter)
    elif cmd == "market" and len(args) >= 2:
        cmd_market(args[1])
    elif cmd == "arbitrage":
        min_m = 100
        if "--min" in args:
            idx = args.index("--min")
            if idx + 1 < len(args):
                try:
                    min_m = int(args[idx + 1])
                except ValueError:
                    pass
        cmd_arbitrage(SYSTEM, min_m)
    elif cmd == "refresh":
        cmd_refresh()
    else:
        console.print("[yellow]Usage:[/yellow]")
        console.print("  python3 status.py                       — contracts + sourcing")
        console.print("  python3 status.py find <GOOD>           — sourcing for a good")
        console.print("  python3 status.py contract              — all contracts detail")
        console.print("  python3 status.py waypoints [--type X]  — list waypoints")
        console.print("  python3 status.py market <WAYPOINT>     — cached prices")
        console.print("  python3 status.py arbitrage [--min N]   — buy/sell margin opportunities")
        console.print("  python3 status.py refresh               — re-fetch from API")


if __name__ == "__main__":
    main()
