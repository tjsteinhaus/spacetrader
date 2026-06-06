"""
Rich-based display helpers for the SpaceTraders CLI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_credits(n: int | None) -> str:
    if n is None:
        return "—"
    return f"[green]{n:,}[/green] cr"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = dt - now
        secs = int(delta.total_seconds())
        if secs < 0:
            return f"[dim]{dt.strftime('%H:%M:%S')} (arrived)[/dim]"
        mins, s = divmod(secs, 60)
        hrs, m = divmod(mins, 60)
        if hrs:
            return f"[yellow]{hrs}h {m}m {s}s[/yellow]"
        if m:
            return f"[yellow]{m}m {s}s[/yellow]"
        return f"[yellow]{s}s[/yellow]"
    except Exception:
        return ts


def _nav_status_color(status: str) -> str:
    colors = {"IN_TRANSIT": "yellow", "IN_ORBIT": "cyan", "DOCKED": "green"}
    c = colors.get(status, "white")
    return f"[{c}]{status}[/{c}]"


def _flight_mode_color(mode: str) -> str:
    colors = {"DRIFT": "dim", "STEALTH": "magenta", "CRUISE": "blue", "BURN": "red"}
    c = colors.get(mode, "white")
    return f"[{c}]{mode}[/{c}]"


def _condition_bar(val: float) -> str:
    filled = int(val * 10)
    empty = 10 - filled
    if val >= 0.8:
        color = "green"
    elif val >= 0.5:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{'█' * filled}{'░' * empty}[/{color}] {val*100:.0f}%"


# ── Server status ─────────────────────────────────────────────────────────────

def show_server_status(data: dict) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("Status", f"[green]{data.get('status', '?')}[/green]")
    t.add_row("Version", data.get("version", "?"))
    t.add_row("Reset date", data.get("resetDate", "?"))
    t.add_row("Next reset", data.get("serverResets", {}).get("next", "?"))
    stats = data.get("stats", {})
    t.add_row("Agents", str(stats.get("agents", "?")))
    t.add_row("Ships", str(stats.get("ships", "?")))
    t.add_row("Systems", str(stats.get("systems", "?")))
    t.add_row("Waypoints", str(stats.get("waypoints", "?")))
    console.print(Panel(t, title="[bold]Server Status[/bold]", border_style="blue"))

    lb = data.get("leaderboards", {})
    credits_lb = lb.get("mostCredits", [])
    if credits_lb:
        tbl = Table(title="Top Agents by Credits", box=box.SIMPLE_HEAVY)
        tbl.add_column("#", style="dim", width=4)
        tbl.add_column("Agent", style="bold")
        tbl.add_column("Credits", justify="right")
        for i, entry in enumerate(credits_lb[:10], 1):
            tbl.add_row(str(i), entry.get("agentSymbol", "?"), f"{entry.get('credits', 0):,}")
        console.print(tbl)


# ── Agent ─────────────────────────────────────────────────────────────────────

def show_agent(data: dict) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("Symbol", f"[bold]{data.get('symbol', '?')}[/bold]")
    t.add_row("Credits", _fmt_credits(data.get("credits")))
    t.add_row("HQ", data.get("headquarters", "?"))
    t.add_row("Faction", data.get("startingFaction", "?"))
    t.add_row("Ships", str(data.get("shipCount", "?")))
    console.print(Panel(t, title="[bold]Your Agent[/bold]", border_style="cyan"))


# ── Ships / Fleet ─────────────────────────────────────────────────────────────

def show_ships_list(ships: list) -> None:
    tbl = Table(title=f"Your Fleet ({len(ships)} ships)", box=box.SIMPLE_HEAVY)
    tbl.add_column("Symbol", style="bold")
    tbl.add_column("Role")
    tbl.add_column("Status")
    tbl.add_column("Flight Mode")
    tbl.add_column("Location")
    tbl.add_column("Fuel")
    tbl.add_column("Cargo")
    tbl.add_column("Arrival")

    for ship in ships:
        nav = ship.get("nav", {})
        fuel = ship.get("fuel", {})
        cargo = ship.get("cargo", {})
        arrival = nav.get("route", {}).get("arrival")
        status = nav.get("status", "?")
        fmode = nav.get("flightMode", "?")
        location = nav.get("waypointSymbol", "?")
        fuel_str = f"{fuel.get('current', 0)}/{fuel.get('capacity', 0)}"
        cargo_str = f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}"

        tbl.add_row(
            ship.get("symbol", "?"),
            ship.get("registration", {}).get("role", "?"),
            _nav_status_color(status),
            _flight_mode_color(fmode),
            location,
            fuel_str,
            cargo_str,
            _fmt_ts(arrival) if status == "IN_TRANSIT" else "—",
        )
    console.print(tbl)


def show_ship_detail(ship: dict) -> None:
    nav = ship.get("nav", {})
    fuel = ship.get("fuel", {})
    cargo = ship.get("cargo", {})
    crew = ship.get("crew", {})
    frame = ship.get("frame", {})
    reactor = ship.get("reactor", {})
    engine = ship.get("engine", {})
    cooldown = ship.get("cooldown", {})

    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold cyan", width=18)
    info.add_column()

    info.add_row("Symbol", f"[bold]{ship.get('symbol')}[/bold]")
    info.add_row("Role", ship.get("registration", {}).get("role", "?"))
    info.add_row("Status", _nav_status_color(nav.get("status", "?")))
    info.add_row("Flight Mode", _flight_mode_color(nav.get("flightMode", "?")))
    info.add_row("System", nav.get("systemSymbol", "?"))
    info.add_row("Waypoint", nav.get("waypointSymbol", "?"))

    route = nav.get("route", {})
    if nav.get("status") == "IN_TRANSIT":
        info.add_row("Destination", route.get("destination", {}).get("symbol", "?"))
        info.add_row("Arrival", _fmt_ts(route.get("arrival")))

    info.add_row("Fuel", f"{fuel.get('current')}/{fuel.get('capacity')}")
    info.add_row("Cargo", f"{cargo.get('units')}/{cargo.get('capacity')}")
    info.add_row("Crew", f"{crew.get('current')}/{crew.get('capacity')} (morale: {crew.get('morale', '?')})")
    info.add_row("Frame", f"{frame.get('name', '?')} ({_condition_bar(frame.get('condition', 1))})")
    info.add_row("Reactor", f"{reactor.get('name', '?')} ({_condition_bar(reactor.get('condition', 1))})")
    info.add_row("Engine", f"{engine.get('name', '?')} ({_condition_bar(engine.get('condition', 1))})")

    remaining = cooldown.get("remainingSeconds", 0)
    if remaining:
        info.add_row("Cooldown", f"[yellow]{remaining}s remaining[/yellow]")

    console.print(Panel(info, title=f"[bold]Ship: {ship.get('symbol')}[/bold]", border_style="cyan"))

    # Cargo inventory
    items = cargo.get("inventory", [])
    if items:
        t = Table(title="Cargo Hold", box=box.SIMPLE)
        t.add_column("Good", style="bold")
        t.add_column("Units", justify="right")
        t.add_column("Description")
        for item in items:
            t.add_row(item.get("symbol"), str(item.get("units")), item.get("description", "")[:60])
        console.print(t)

    # Mounts
    mounts = ship.get("mounts", [])
    if mounts:
        t = Table(title="Mounts", box=box.SIMPLE)
        t.add_column("Symbol", style="bold")
        t.add_column("Name")
        t.add_column("Strength", justify="right")
        for m in mounts:
            t.add_row(m.get("symbol"), m.get("name"), str(m.get("strength", "—")))
        console.print(t)

    # Modules
    modules = ship.get("modules", [])
    if modules:
        t = Table(title="Modules", box=box.SIMPLE)
        t.add_column("Symbol", style="bold")
        t.add_column("Name")
        for m in modules:
            t.add_row(m.get("symbol"), m.get("name"))
        console.print(t)


# ── Contracts ─────────────────────────────────────────────────────────────────

def show_contracts(contracts: list) -> None:
    tbl = Table(title=f"Contracts ({len(contracts)})", box=box.SIMPLE_HEAVY)
    tbl.add_column("ID", style="dim", max_width=12)
    tbl.add_column("Type")
    tbl.add_column("Faction")
    tbl.add_column("Accepted")
    tbl.add_column("Fulfilled")
    tbl.add_column("Payment (accept)", justify="right")
    tbl.add_column("Payment (fulfill)", justify="right")
    tbl.add_column("Deadline")

    for c in contracts:
        terms = c.get("terms", {})
        payment = terms.get("payment", {})
        accepted = "[green]Yes[/green]" if c.get("accepted") else "[red]No[/red]"
        fulfilled = "[green]Yes[/green]" if c.get("fulfilled") else "[dim]No[/dim]"
        deadline = c.get("terms", {}).get("deadline", "")
        tbl.add_row(
            c.get("id", "?")[:12],
            c.get("type", "?"),
            c.get("factionSymbol", "?"),
            accepted,
            fulfilled,
            f"{payment.get('onAccepted', 0):,}",
            f"{payment.get('onFulfilled', 0):,}",
            _fmt_ts(deadline),
        )
    console.print(tbl)


def show_contract_detail(c: dict) -> None:
    terms = c.get("terms", {})
    payment = terms.get("payment", {})
    deliver = terms.get("deliver", [])

    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold cyan", width=20)
    info.add_column()
    info.add_row("ID", c.get("id", "?"))
    info.add_row("Type", c.get("type", "?"))
    info.add_row("Faction", c.get("factionSymbol", "?"))
    info.add_row("Accepted", "[green]Yes[/green]" if c.get("accepted") else "[red]No[/red]")
    info.add_row("Fulfilled", "[green]Yes[/green]" if c.get("fulfilled") else "[dim]No[/dim]")
    info.add_row("Payment on accept", _fmt_credits(payment.get("onAccepted")))
    info.add_row("Payment on fulfill", _fmt_credits(payment.get("onFulfilled")))
    info.add_row("Deadline", _fmt_ts(terms.get("deadline")))
    info.add_row("Accept by", _fmt_ts(c.get("deadlineToAccept")))

    console.print(Panel(info, title="[bold]Contract Detail[/bold]", border_style="yellow"))

    if deliver:
        t = Table(title="Deliveries Required", box=box.SIMPLE)
        t.add_column("Good", style="bold")
        t.add_column("Destination")
        t.add_column("Required", justify="right")
        t.add_column("Fulfilled", justify="right")
        t.add_column("Progress")
        for d in deliver:
            req = d.get("unitsRequired", 0)
            fulfilled = d.get("unitsFulfilled", 0)
            pct = fulfilled / req if req else 0
            bar = f"{'█' * int(pct * 10)}{'░' * (10 - int(pct * 10))} {pct*100:.0f}%"
            t.add_row(
                d.get("tradeSymbol", "?"),
                d.get("destinationSymbol", "?"),
                str(req),
                str(fulfilled),
                f"[{'green' if pct >= 1 else 'yellow'}]{bar}[/]",
            )
        console.print(t)


# ── Markets ───────────────────────────────────────────────────────────────────

def show_market(data: dict) -> None:
    console.print(Panel(f"[bold]Market: {data.get('symbol')}[/bold]", border_style="green"))

    exports = data.get("exports", [])
    imports = data.get("imports", [])
    exchange = data.get("exchange", [])

    if exports:
        t = Table(title="Exports", box=box.SIMPLE)
        t.add_column("Good", style="green")
        t.add_column("Name")
        for g in exports:
            t.add_row(g.get("symbol"), g.get("name"))
        console.print(t)

    if imports:
        t = Table(title="Imports", box=box.SIMPLE)
        t.add_column("Good", style="red")
        t.add_column("Name")
        for g in imports:
            t.add_row(g.get("symbol"), g.get("name"))
        console.print(t)

    if exchange:
        t = Table(title="Exchange", box=box.SIMPLE)
        t.add_column("Good", style="yellow")
        t.add_column("Name")
        for g in exchange:
            t.add_row(g.get("symbol"), g.get("name"))
        console.print(t)

    trade_goods = data.get("tradeGoods", [])
    if trade_goods:
        t = Table(title="Live Prices (ship present)", box=box.SIMPLE_HEAVY)
        t.add_column("Good", style="bold")
        t.add_column("Type")
        t.add_column("Supply")
        t.add_column("Activity")
        t.add_column("Buy Price", justify="right")
        t.add_column("Sell Price", justify="right")
        t.add_column("Trade Vol", justify="right")
        for g in trade_goods:
            t.add_row(
                g.get("symbol"),
                g.get("type", "?"),
                g.get("supply", "?"),
                g.get("activity", "?"),
                f"{g.get('purchasePrice', 0):,}",
                f"{g.get('sellPrice', 0):,}",
                str(g.get("tradeVolume", "?")),
            )
        console.print(t)

    transactions = data.get("transactions", [])
    if transactions:
        t = Table(title="Recent Transactions", box=box.SIMPLE)
        t.add_column("Ship")
        t.add_column("Good")
        t.add_column("Type")
        t.add_column("Units", justify="right")
        t.add_column("Price/unit", justify="right")
        t.add_column("Total", justify="right")
        for tx in transactions[:10]:
            t.add_row(
                tx.get("shipSymbol"),
                tx.get("tradeSymbol"),
                tx.get("type"),
                str(tx.get("units")),
                f"{tx.get('pricePerUnit', 0):,}",
                f"{tx.get('totalPrice', 0):,}",
            )
        console.print(t)


# ── Waypoints ─────────────────────────────────────────────────────────────────

def show_waypoints(waypoints: list, title: str = "Waypoints") -> None:
    tbl = Table(title=f"{title} ({len(waypoints)})", box=box.SIMPLE_HEAVY)
    tbl.add_column("Symbol", style="bold")
    tbl.add_column("Type")
    tbl.add_column("X", justify="right")
    tbl.add_column("Y", justify="right")
    tbl.add_column("Faction")
    tbl.add_column("Traits")

    for wp in waypoints:
        faction = wp.get("faction", {}).get("symbol", "—") if wp.get("faction") else "—"
        traits = ", ".join(t.get("symbol", "") for t in wp.get("traits", [])[:3])
        if len(wp.get("traits", [])) > 3:
            traits += "…"
        tbl.add_row(
            wp.get("symbol", "?"),
            wp.get("type", "?"),
            str(wp.get("x", 0)),
            str(wp.get("y", 0)),
            faction,
            traits,
        )
    console.print(tbl)


def show_shipyard(data: dict) -> None:
    console.print(Panel(
        f"[bold]Shipyard: {data.get('symbol')}[/bold]\nModification fee: [yellow]{data.get('modificationsFee', '?')} cr[/yellow]",
        border_style="magenta",
    ))

    ships = data.get("ships", [])
    if ships:
        t = Table(title="Ships for Sale", box=box.SIMPLE_HEAVY)
        t.add_column("Type", style="bold")
        t.add_column("Name")
        t.add_column("Description", max_width=40)
        t.add_column("Price", justify="right")
        t.add_column("Supply")
        for s in ships:
            t.add_row(
                s.get("type", "?"),
                s.get("name", "?"),
                s.get("description", "")[:40],
                f"{s.get('purchasePrice', 0):,}",
                s.get("supply", "?"),
            )
        console.print(t)
    else:
        ship_types = data.get("shipTypes", [])
        if ship_types:
            t = Table(title="Available Ship Types", box=box.SIMPLE)
            t.add_column("Type", style="bold")
            for st in ship_types:
                t.add_row(st.get("type", "?"))
            console.print(t)


# ── Extraction / Survey ───────────────────────────────────────────────────────

def show_extraction(data: dict) -> None:
    extraction = data.get("extraction", {})
    yld = extraction.get("yield", {})
    cooldown = data.get("cooldown", {})
    cargo = data.get("cargo", {})

    console.print(Panel(
        f"Extracted [bold green]{yld.get('units', 0)}x {yld.get('symbol', '?')}[/bold green]\n"
        f"Cargo: {cargo.get('units', 0)}/{cargo.get('capacity', 0)} units\n"
        f"Cooldown: [yellow]{cooldown.get('remainingSeconds', 0)}s[/yellow]",
        title="[bold]Extraction Result[/bold]",
        border_style="green",
    ))

    events = data.get("events", [])
    if events:
        for ev in events:
            console.print(f"  [yellow]⚠ {ev.get('name')}: {ev.get('description')}[/yellow]")


def show_survey_result(data: dict) -> None:
    surveys = data.get("surveys", [])
    cooldown = data.get("cooldown", {})
    console.print(f"[dim]Cooldown: {cooldown.get('remainingSeconds', 0)}s[/dim]")
    for survey in surveys:
        deposits = ", ".join(d.get("symbol", "") for d in survey.get("deposits", []))
        console.print(Panel(
            f"Signature: [dim]{survey.get('signature')}[/dim]\n"
            f"Size: [bold]{survey.get('size')}[/bold]\n"
            f"Deposits: [green]{deposits}[/green]\n"
            f"Expires: {_fmt_ts(survey.get('expiration'))}",
            title="Survey",
            border_style="yellow",
        ))


# ── Factions ──────────────────────────────────────────────────────────────────

def show_factions(factions: list) -> None:
    tbl = Table(title=f"Factions ({len(factions)})", box=box.SIMPLE_HEAVY)
    tbl.add_column("Symbol", style="bold")
    tbl.add_column("Name")
    tbl.add_column("HQ")
    tbl.add_column("Recruiting")

    for f in factions:
        recruiting = "[green]Yes[/green]" if f.get("isRecruiting") else "[dim]No[/dim]"
        tbl.add_row(
            f.get("symbol", "?"),
            f.get("name", "?"),
            f.get("headquarters", "—"),
            recruiting,
        )
    console.print(tbl)


# ── Navigation result ─────────────────────────────────────────────────────────

def show_navigate_result(data: dict) -> None:
    nav = data.get("nav", {})
    fuel = data.get("fuel", {})
    route = nav.get("route", {})
    dest = route.get("destination", {})

    console.print(Panel(
        f"Destination: [bold]{dest.get('symbol')} ({dest.get('type')})[/bold]\n"
        f"Status: {_nav_status_color(nav.get('status', '?'))}\n"
        f"Arrival: {_fmt_ts(route.get('arrival'))}\n"
        f"Fuel consumed: {fuel.get('consumed', {}).get('amount', 0) if isinstance(fuel.get('consumed'), dict) else 0} units\n"
        f"Fuel remaining: {fuel.get('current', 0)}/{fuel.get('capacity', 0)}",
        title="[bold]Navigating[/bold]",
        border_style="cyan",
    ))


# ── Generic success / error ───────────────────────────────────────────────────

def success(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red]✗[/bold red] {msg}")


def info(msg: str) -> None:
    console.print(f"[bold cyan]ℹ[/bold cyan] {msg}")


def header(title: str) -> None:
    console.print(f"\n[bold magenta]{'─' * 60}[/bold magenta]")
    console.print(f"[bold magenta]  {title}[/bold magenta]")
    console.print(f"[bold magenta]{'─' * 60}[/bold magenta]\n")
