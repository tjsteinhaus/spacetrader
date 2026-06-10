#!/usr/bin/env python3
"""
dashboard.py — Interactive SpaceTraders TUI dashboard.

7 screens navigated by number keys or mouse clicks:
  1  Dashboard   — live overview: credits, fleet, contract, transactions, yields
  2  Fleet       — all ships; click/Enter → ship detail modal
  3  Contracts   — all contracts; click/Enter → contract detail modal
  4  Markets     — market list + prices tab + arbitrage tab; Refresh Listings button
  5  Universe    — all waypoints, filterable; click → sourcing analysis
  6  Surveys     — active survey pool with deposit breakdown
  7  Analytics   — transaction history, extraction yields, hourly income chart

Usage:
    python3 dashboard.py
    python3 dashboard.py --interval 5
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header, Footer, DataTable, Label, Static,
    TabbedContent, TabPane, Button, Input, Select,
)
from textual.containers import Container, Horizontal, Vertical
from textual import work
from textual import on, events
from rich.text import Text

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

import db
import sync_api as agent_api
import sync_api as fleet_api
import sync_api as universe_api
from client import SpaceTradersError

# Derive SYSTEM from DB — v2 has no play.py global
try:
    with db._conn() as _c:
        _row = _c.execute(
            "SELECT system_symbol FROM waypoints WHERE system_symbol IS NOT NULL LIMIT 1"
        ).fetchone()
    SYSTEM = _row[0] if _row else "X1-GK27"
except Exception:
    SYSTEM = "X1-GK27"

POLL_INTERVAL: float = 2.0  # seconds — overrideable via --interval CLI arg

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
        return f"{int(ago // 60)}m ago"
    return f"{int(ago // 3600)}h ago"


def _deadline_str(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() / 3600)
        if hours < 0:
            return "EXPIRED"
        if hours < 24:
            return f"{hours}h left"
        return f"{hours // 24}d {hours % 24}h"
    except Exception:
        return iso[:10] if iso else "—"


def _eta_str(arrival_iso: str | None) -> str:
    if not arrival_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(arrival_iso.replace("Z", "+00:00"))
        secs = int((dt - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return "arriving"
        mins, s = divmod(secs, 60)
        return f"{mins}m {s}s" if mins else f"{s}s"
    except Exception:
        return "—"


def _calc_cph() -> tuple[int, int]:
    """Returns (net_last_1hr, net_last_10min)."""
    now = time.time()
    try:
        with db._conn() as con:
            row = con.execute(
                """SELECT
                    SUM(CASE WHEN type='SELL'     AND timestamp > ? THEN  total_price
                             WHEN type='PURCHASE' AND timestamp > ? THEN -total_price
                             ELSE 0 END),
                    SUM(CASE WHEN type='SELL'     AND timestamp > ? THEN  total_price
                             WHEN type='PURCHASE' AND timestamp > ? THEN -total_price
                             ELSE 0 END)
                   FROM market_transactions""",
                (now - 3600, now - 3600, now - 600, now - 600),
            ).fetchone()
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception:
        return (0, 0)


def _ship_icon(ship: dict) -> str:
    mounts = [m.get("symbol", "") for m in ship.get("mounts", [])]
    status = ship.get("nav", {}).get("status", "")
    if status == "IN_TRANSIT":
        return "🚀"
    if any("MINING_LASER" in m for m in mounts):
        return "⛏"
    if any("SURVEYING" in m for m in mounts):
        return "🔭"
    if status == "DOCKED":
        return "⚓"
    return "🛸"


def _ship_activity(ship: dict) -> str:
    nav = ship.get("nav", {})
    status = nav.get("status", "")
    cooldown = ship.get("cooldown", {}).get("remainingSeconds", 0)
    mounts = [m.get("symbol", "") for m in ship.get("mounts", [])]
    if status == "IN_TRANSIT":
        dest = nav.get("route", {}).get("destination", {}).get("symbol", "?")
        eta = _eta_str(nav.get("route", {}).get("arrival"))
        return f"→ {dest} ({eta})"
    if cooldown > 0:
        if any("MINING_LASER" in m for m in mounts):
            return f"Mining  cd:{cooldown}s"
        if any("SURVEYING" in m for m in mounts):
            return f"Surveying  cd:{cooldown}s"
        return f"Cooling  {cooldown}s"
    if status == "DOCKED":
        return "Docked"
    return "Idle"


def _fill_bar(current: int, capacity: int, width: int = 10) -> Text:
    if capacity == 0:
        return Text("—", style="dim")
    pct = current / capacity
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    color = "green" if pct < 0.6 else "yellow" if pct < 0.85 else "red"
    return Text(f"{bar} {current}/{capacity}", style=color)


def _condition_bar(value: float, width: int = 8) -> Text:
    filled = int(value * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(value * 100)
    color = "green" if value > 0.8 else "yellow" if value > 0.5 else "red"
    return Text(f"{bar} {pct}%", style=color)


def _supply_text(supply: str | None) -> Text:
    colors = {
        "ABUNDANT": "green", "HIGH": "green",
        "MODERATE": "yellow",
        "LIMITED": "red", "SCARCE": "red",
    }
    s = supply or "?"
    return Text(s, style=colors.get(s, "white"))


def _nav_status_text(status: str) -> Text:
    mapping = {
        "IN_TRANSIT": ("TRANSIT", "yellow"),
        "DOCKED":     ("DOCKED",  "green"),
        "IN_ORBIT":   ("ORBIT",   "cyan"),
    }
    label, color = mapping.get(status, (status, "white"))
    return Text(label, style=color)


def _system_from_wp(waypoint: str) -> str:
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else SYSTEM


# Map helpers
# Unique icon + color assigned to each ship in map order
_SHIP_MAP_PALETTE: list[tuple[str, str]] = [
    ("✦", "bold bright_yellow"),
    ("★", "bold bright_red"),
    ("◆", "bold bright_green"),
    ("▲", "bold bright_cyan"),
    ("●", "bold bright_magenta"),
    ("◈", "bold yellow"),
    ("♦", "bold red"),
    ("⬟", "bold green"),
    ("✿", "bold cyan"),
    ("◉", "bold magenta"),
]

_WP_MAP_ICONS: dict[str, tuple[str, str]] = {
    "PLANET":              ("◉", "bright_blue"),
    "GAS_GIANT":           ("◎", "blue"),
    "MOON":                ("○", "white"),
    "ORBITAL_STATION":     ("▣", "bright_green"),
    "SPACE_STATION":       ("▣", "green"),
    "JUMP_GATE":           ("⊕", "bright_cyan"),
    "ASTEROID_FIELD":      ("⬡", "yellow"),
    "ASTEROID":            ("⬡", "yellow"),
    "ENGINEERED_ASTEROID": ("⬡", "bright_yellow"),
    "ASTEROID_BASE":       ("⬣", "bright_yellow"),
    "NEBULA":              ("≋", "magenta"),
    "DEBRIS_FIELD":        ("⁘", "dim"),
    "GRAVITY_WELL":        ("◌", "bright_black"),
    "FUEL_STATION":        ("⊡", "bright_green"),
}


def _wp_map_icon(wp_type: str) -> tuple[str, str]:
    return _WP_MAP_ICONS.get(wp_type, ("·", "dim"))


def _ship_transit_pos(ship: dict) -> tuple[float, float] | None:
    """Interpolate ship position when in transit between two waypoints."""
    nav = ship.get("nav", {})
    if nav.get("status") != "IN_TRANSIT":
        return None
    route    = nav.get("route", {})
    dep      = route.get("departure", {})
    dest     = route.get("destination", {})
    dep_time = route.get("departureTime", "")
    arr_time = route.get("arrival", "")
    if dep.get("x") is None or dest.get("x") is None or not dep_time or not arr_time:
        return None
    now    = datetime.now(timezone.utc)
    dep_dt = datetime.fromisoformat(dep_time.replace("Z", "+00:00"))
    arr_dt = datetime.fromisoformat(arr_time.replace("Z", "+00:00"))
    total   = (arr_dt - dep_dt).total_seconds()
    elapsed = (now - dep_dt).total_seconds()
    frac    = max(0.0, min(1.0, elapsed / max(1.0, total)))
    return (
        dep["x"] + frac * (dest["x"] - dep["x"]),
        dep["y"] + frac * (dest["y"] - dep["y"]),
    )


def _render_map(
    waypoints: list[dict],
    ships: list[dict],
    width: int,
    height: int,
    ship_icons: dict[str, tuple[str, str]],
) -> Text:
    """Render an ASCII map of waypoints + ship positions as a Rich Text object."""
    if not waypoints:
        return Text("No waypoint data — run: python3 refresh_db.py", style="dim")
    xs = [w["x"] for w in waypoints]
    ys = [w["y"] for w in waypoints]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_range = max(1, x_max - x_min)
    y_range = max(1, y_max - y_min)
    pad   = 1
    map_w = max(1, width  - pad * 2)
    map_h = max(1, height - pad * 2)

    def to_grid(x: float, y: float) -> tuple[int, int]:
        gx = int((x - x_min) / x_range * map_w) + pad
        gy = int((y_max - y) / y_range * map_h) + pad  # flip Y: game +y=up, screen +y=down
        return max(0, min(width - 1, gx)), max(0, min(height - 1, gy))

    wp_coords = {w["symbol"]: (w["x"], w["y"]) for w in waypoints}

    cells: dict[tuple[int, int], tuple[str, str]] = {}
    for wp in waypoints:
        gx, gy = to_grid(wp["x"], wp["y"])
        cells[(gx, gy)] = _wp_map_icon(wp["type"])

    # Map each grid cell to the list of ship symbols present there
    ship_cells: dict[tuple[int, int], list[str]] = {}
    for ship in ships:
        pos = _ship_transit_pos(ship)
        if pos is None:
            coords = wp_coords.get(ship.get("nav", {}).get("waypointSymbol", ""))
            pos = coords
        if pos:
            gx, gy = to_grid(pos[0], pos[1])
            ship_cells.setdefault((gx, gy), []).append(ship.get("symbol", "?"))

    output = Text()
    for row in range(height):
        for col in range(width):
            key = (col, row)
            if key in ship_cells:
                syms = ship_cells[key]
                # Show the first ship's unique icon; if stacked show count superscript
                icon, style = ship_icons.get(syms[0], ("✦", "bold bright_yellow"))
                label = icon if len(syms) == 1 else f"{icon}{len(syms)}"
                output.append(label, style=style)
            elif key in cells:
                char, style = cells[key]
                output.append(char, style=style)
            else:
                output.append("·", style="#1e2030")
        if row < height - 1:
            output.append("\n")
    return output


# ---------------------------------------------------------------------------
# Ship type constants (for Settings screen)
# ---------------------------------------------------------------------------

_BUYABLE_SHIP_TYPES = [
    "SHIP_ORE_HOUND",
    "SHIP_MINING_DRONE",
    "SHIP_SURVEYOR",
    "SHIP_LIGHT_HAULER",
    "SHIP_HEAVY_FREIGHTER",
]

_SHIP_ROLE = {
    "SHIP_ORE_HOUND":       "miner",
    "SHIP_MINING_DRONE":    "miner",
    "SHIP_SURVEYOR":        "surveyor",
    "SHIP_LIGHT_HAULER":    "hauler",
    "SHIP_HEAVY_FREIGHTER": "hauler",
}

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ShipDetailModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, ship: dict) -> None:
        super().__init__()
        self._ship = ship

    def compose(self) -> ComposeResult:
        sym = self._ship.get("symbol", "?")
        with Container(id="modal-box"):
            yield Label(f" {sym} — Ship Detail ", classes="modal-title")
            with TabbedContent():
                with TabPane("Overview"):
                    yield Static(id="ship-overview")
                with TabPane("Cargo"):
                    yield DataTable(id="cargo-table", show_cursor=False, zebra_stripes=True)
                with TabPane("Mounts & Modules"):
                    yield DataTable(id="mounts-table", show_cursor=False, zebra_stripes=True)
            yield Button("Close  [Esc]", id="close-btn", variant="default")

    def on_mount(self) -> None:
        ship = self._ship
        nav   = ship.get("nav", {})
        cargo = ship.get("cargo", {})
        fuel  = ship.get("fuel", {})
        frame = ship.get("frame", {})
        reactor = ship.get("reactor", {})
        engine  = ship.get("engine", {})
        crew    = ship.get("crew", {})
        cd      = ship.get("cooldown", {}).get("remainingSeconds", 0)

        lines = [
            f"[bold cyan]── Navigation ──[/bold cyan]",
            f"  Status:    {nav.get('status', '?')}  |  Mode: {nav.get('flightMode', '?')}",
            f"  Location:  {nav.get('waypointSymbol', '?')}",
            f"  Activity:  {_ship_activity(ship)}",
            f"  Cooldown:  {cd}s" if cd > 0 else "  Cooldown:  —",
            "",
            f"[bold cyan]── Cargo [{cargo.get('units',0)}/{cargo.get('capacity',0)}] ──[/bold cyan]",
            "",
            f"[bold cyan]── Fuel ──[/bold cyan]",
            f"  {fuel.get('current',0)}/{fuel.get('capacity',0)} units",
            "",
            f"[bold cyan]── Crew ──[/bold cyan]",
            f"  {crew.get('current',0)}/{crew.get('capacity',0)}  Morale: {crew.get('morale',0)}%",
            "",
            f"[bold cyan]── Components ──[/bold cyan]",
            f"  Frame:    {frame.get('name','?'):30s}  {_condition_bar(frame.get('condition',1)).plain}",
            f"  Reactor:  {reactor.get('name','?'):30s}  {_condition_bar(reactor.get('condition',1)).plain}",
            f"  Engine:   {engine.get('name','?'):30s}  {_condition_bar(engine.get('condition',1)).plain}",
            f"  Speed:    {engine.get('speed','?')}",
        ]
        self.query_one("#ship-overview", Static).update("\n".join(lines))

        # Cargo table
        ct = self.query_one("#cargo-table", DataTable)
        ct.add_columns("Good", "Units", "Name")
        inventory = cargo.get("inventory", [])
        for item in inventory:
            ct.add_row(
                Text(item["symbol"], style="bold green"),
                str(item["units"]),
                item.get("name", ""),
            )
        if not inventory:
            ct.add_row(Text("Empty", style="dim"), "", "")

        # Mounts table
        mt = self.query_one("#mounts-table", DataTable)
        mt.add_columns("Type", "Name", "Strength", "Deposits")
        for m in ship.get("mounts", []):
            deps = ", ".join(d if isinstance(d, str) else d.get("symbol", "") for d in m.get("deposits", []))
            mt.add_row(
                Text(m.get("symbol", "?"), style="cyan"),
                m.get("name", "?"),
                str(m.get("strength", "—")),
                deps or "—",
            )
        for mod in ship.get("modules", []):
            mt.add_row(
                Text(mod.get("symbol", "?"), style="yellow"),
                mod.get("name", "?"),
                "—", "module",
            )
        if not ship.get("mounts") and not ship.get("modules"):
            mt.add_row(Text("None", style="dim"), "", "", "")

    @on(Button.Pressed, "#close-btn")
    def _close(self) -> None:
        self.dismiss()


class ContractDetailModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, contract: dict) -> None:
        super().__init__()
        self._contract = contract

    def compose(self) -> ComposeResult:
        cid = self._contract.get("id", "?")
        with Container(id="modal-box"):
            yield Label(f" Contract {cid[:16]}… ", classes="modal-title")
            yield Static(id="contract-detail")
            yield Button("Close  [Esc]", id="close-btn", variant="default")

    def on_mount(self) -> None:
        c = self._contract
        lines = [
            f"[bold cyan]ID:[/bold cyan]       {c.get('id', '?')}",
            f"[bold cyan]Type:[/bold cyan]     {c.get('type','?')}   "
            f"[bold cyan]Faction:[/bold cyan] {c.get('faction_symbol','?')}",
            f"[bold cyan]Status:[/bold cyan]   "
            + ("[green]✅ Fulfilled[/green]" if c.get("fulfilled") else
               "[cyan]🔄 Active[/cyan]"    if c.get("accepted")  else
               "[yellow]📋 Available[/yellow]"),
            f"[bold cyan]Deadline:[/bold cyan]  {_deadline_str(c.get('deadline'))}",
            f"[bold cyan]Expiry:[/bold cyan]    {_deadline_str(c.get('expiration'))}",
            "",
            f"[bold cyan]Payment:[/bold cyan]",
            f"  On accept:  {(c.get('on_accepted') or 0):>12,} cr",
            f"  On fulfill: {(c.get('on_fulfilled') or 0):>12,} cr",
            "",
            "[bold cyan]Deliverables:[/bold cyan]",
        ]
        for d in c.get("deliver", []):
            req  = d["units_required"]
            fulf = d["units_fulfilled"]
            pct  = int(100 * fulf / max(1, req))
            bar  = "█" * (pct // 10) + "░" * (10 - pct // 10)
            color = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
            lines.append(
                f"  [{color}]{d['trade_symbol']:25s}[/{color}]  "
                f"{fulf:>5,}/{req:<5,}  {bar} {pct}%"
            )
            lines.append(f"  → Destination: {d.get('destination_symbol','?')}")

        lines += ["", "[bold cyan]Sourcing Analysis:[/bold cyan]"]
        for d in c.get("deliver", []):
            good = d["trade_symbol"]
            minable = db.can_be_mined(good, SYSTEM)
            buyable  = db.can_be_bought(good, SYSTEM)
            ore_hint = db.SMELTED_GOODS.get(good)
            if minable:
                wps = ", ".join(m["waypoint_symbol"] for m in minable[:3])
                lines.append(f"  [green]⛏  {good}[/green] → mine at {wps}")
            elif ore_hint:
                lines.append(
                    f"  [red]✗  {good}[/red] — smelted good, cannot mine "
                    f"(raw ore: {ore_hint})"
                )
            else:
                lines.append(f"  [yellow]?  {good}[/yellow] — no deposit data found")
            if buyable:
                buy_list = ", ".join(
                    f"{b['waypoint_symbol']}"
                    + (f" ({b['purchase_price']:,}cr)" if b["purchase_price"] else "")
                    for b in buyable[:3]
                )
                lines.append(f"  [cyan]🛒 Buy at:[/cyan] {buy_list}")

        self.query_one("#contract-detail", Static).update("\n".join(lines))

    @on(Button.Pressed, "#close-btn")
    def _close(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Screen 1 — Dashboard
# ---------------------------------------------------------------------------

class DashboardScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="dashboard-main"):
            with Vertical(id="dash-left"):
                yield Static("", id="agent-stats",   classes="info-panel")
                yield Static("", id="fleet-mini",    classes="info-panel")
            with Vertical(id="dash-right"):
                yield Static("", id="contract-panel",  classes="info-panel")
                yield Static("", id="activity-panel",  classes="info-panel")
                yield Static("", id="yields-panel",    classes="info-panel")
        yield Label("", id="dash-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            agent     = agent_api.get_my_agent()
            ships     = fleet_api.get_my_ships()
            contracts = db.get_active_contracts()
            cph_1h, cph_10m = _calc_cph()
            now = time.time()
            with db._conn() as con:
                txns = con.execute(
                    "SELECT type, trade_symbol, units, total_price, timestamp "
                    "FROM market_transactions ORDER BY timestamp DESC LIMIT 8"
                ).fetchall()
                yields = con.execute(
                    "SELECT trade_symbol, SUM(units) FROM extraction_yields "
                    "WHERE timestamp > ? GROUP BY trade_symbol ORDER BY SUM(units) DESC",
                    (now - 1200,),
                ).fetchall()
            self.app.call_from_thread(
                self._update, agent, ships, contracts, cph_1h, cph_10m, txns, yields
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#dash-status", Label).update,
                f"[red]Fetch error: {e}[/red]"
            )

    def _update(self, agent, ships, contracts, cph_1h, cph_10m, txns, yields) -> None:
        credits   = agent.get("credits", 0)
        symbol    = agent.get("symbol", "?")
        ship_cnt  = agent.get("shipCount", 0)
        cph_color = "green" if cph_1h >= 0 else "red"
        sign      = "+" if cph_1h >= 0 else ""
        sign10    = "+" if cph_10m >= 0 else ""

        # Agent stats
        self.query_one("#agent-stats", Static).update(
            f"[bold cyan]╔══ {symbol} ══╗[/bold cyan]\n"
            f"  Credits:    [bold green]{credits:>14,} cr[/bold green]\n"
            f"  Ships:      {ship_cnt}\n"
            f"  CPH (1hr):  [{cph_color}]{sign}{cph_1h:>12,} cr[/{cph_color}]\n"
            f"  CPH (10m→hr): [{cph_color}]{sign10}{cph_10m * 6:>9,} cr[/{cph_color}]\n"
            f"  [dim]DB: {db.DB_PATH.name}[/dim]"
        )

        # Fleet mini
        fleet_lines = ["[bold cyan]╔══ FLEET ══╗[/bold cyan]"]
        for ship in ships:
            icon  = _ship_icon(ship)
            sym   = ship.get("symbol", "?")
            nav   = ship.get("nav", {})
            loc   = nav.get("waypointSymbol", "?")
            cargo = ship.get("cargo", {})
            cu, cap = cargo.get("units", 0), cargo.get("capacity", 0)
            act   = _ship_activity(ship)
            fleet_lines.append(
                f"  {icon} [cyan]{sym:12s}[/cyan] {loc:16s} "
                f"[dim]{cu}/{cap}[/dim]  {act}"
            )
        self.query_one("#fleet-mini", Static).update("\n".join(fleet_lines))

        # Contract panel
        c_lines = ["[bold cyan]╔══ ACTIVE CONTRACTS ══╗[/bold cyan]"]
        if contracts:
            for c in contracts[:3]:
                for d in c.get("deliver", []):
                    req  = d["units_required"]
                    fulf = d["units_fulfilled"]
                    pct  = int(100 * fulf / max(1, req))
                    bar  = "█" * (pct // 10) + "░" * (10 - pct // 10)
                    color = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
                    c_lines.append(
                        f"  [{color}]{d['trade_symbol']:20s}[/{color}]  "
                        f"{bar} {fulf:,}/{req:,} ({pct}%)"
                    )
                c_lines.append(
                    f"  Deadline: {_deadline_str(c.get('deadline'))}  "
                    f"Reward: {(c.get('on_fulfilled') or 0):,} cr"
                )
        else:
            c_lines.append("  [dim]No active contracts[/dim]")
        self.query_one("#contract-panel", Static).update("\n".join(c_lines))

        # Transactions feed
        act_lines = ["[bold cyan]╔══ RECENT TRANSACTIONS ══╗[/bold cyan]"]
        for ttype, good, units, total, ts in txns:
            color = "green" if ttype == "SELL" else "red"
            sign2 = "+" if ttype == "SELL" else "-"
            act_lines.append(
                f"  [{color}]{ttype:4s}[/{color}] {good:22s} "
                f"{units:>4}u  {sign2}{total:>8,} cr  [dim]{_ts_ago(ts)}[/dim]"
            )
        if not txns:
            act_lines.append("  [dim]No transactions yet — run play.py to populate[/dim]")
        self.query_one("#activity-panel", Static).update("\n".join(act_lines))

        # Yields
        yield_lines = ["[bold cyan]╔══ MINING YIELDS (last 20m) ══╗[/bold cyan]  "]
        if yields:
            yield_lines.append(
                "  " + "   ".join(f"[green]{g}[/green]: {u}u" for g, u in yields)
            )
        else:
            yield_lines.append("  [dim]No yield data — mining populates this[/dim]")
        self.query_one("#yields-panel", Static).update("\n".join(yield_lines))

        self.query_one("#dash-status", Label).update(
            f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}  "
            f"Auto-refresh every {int(POLL_INTERVAL)}s  •  R to force[/dim]"
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 2 — Fleet
# ---------------------------------------------------------------------------

class FleetScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        self._ships_map: dict[str, dict] = {}
        t = self.query_one("#ships-table", DataTable)
        t.add_columns(
            "Ship", "Type", "Status", "Location", "Activity",
            "Cargo", "Fuel", "Cooldown",
        )
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Fleet  •  ↑↓ navigate  •  Enter or click to view ship detail",
            classes="screen-hint",
        )
        yield DataTable(id="ships-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="fleet-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            ships = fleet_api.get_my_ships()
            self.app.call_from_thread(self._update_table, ships)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#fleet-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_table(self, ships: list[dict]) -> None:
        t = self.query_one("#ships-table", DataTable)
        t.clear()
        self._ships_map = {}
        for ship in ships:
            sym  = ship.get("symbol", "?")
            self._ships_map[sym] = ship
            nav  = ship.get("nav", {})
            cargo = ship.get("cargo", {})
            fuel  = ship.get("fuel", {})
            cd    = ship.get("cooldown", {}).get("remainingSeconds", 0)
            frame_sym = ship.get("frame", {}).get("symbol", "")
            ship_type = frame_sym.removeprefix("FRAME_").replace("_", " ").title()
            t.add_row(
                Text(f"{_ship_icon(ship)} {sym}", style="bold cyan"),
                Text(ship_type, style="magenta"),
                _nav_status_text(nav.get("status", "?")),
                Text(nav.get("waypointSymbol", "?"), style="yellow"),
                Text(_ship_activity(ship)),
                _fill_bar(cargo.get("units", 0), cargo.get("capacity", 0)),
                _fill_bar(fuel.get("current", 0), fuel.get("capacity", 0)),
                Text(f"{cd}s", style="yellow") if cd > 0 else Text("—", style="dim"),
                key=sym,
            )
        self.query_one("#fleet-status", Label).update(
            f"[dim]{len(ships)} ships  •  Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    @on(DataTable.RowSelected, "#ships-table")
    def _on_ship_selected(self, event: DataTable.RowSelected) -> None:
        sym  = str(event.row_key.value)
        ship = self._ships_map.get(sym)
        if ship:
            self.app.push_screen(ShipDetailModal(ship))

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 3 — Contracts
# ---------------------------------------------------------------------------

class ContractsScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        self._contracts_map: dict[str, dict] = {}
        t = self.query_one("#contracts-table", DataTable)
        t.add_columns(
            "ID", "Type", "Good", "Progress", "%",
            "Destination", "Deadline", "Reward", "Status",
        )
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Contracts  •  Enter or click to view detail",
            classes="screen-hint",
        )
        yield DataTable(id="contracts-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="contracts-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            with db._conn() as con:
                rows = con.execute(
                    """SELECT c.id, c.faction_symbol, c.type, c.accepted, c.fulfilled,
                              c.expiration, c.deadline, c.on_accepted, c.on_fulfilled,
                              cd.trade_symbol, cd.destination_symbol,
                              cd.units_required, cd.units_fulfilled
                       FROM contracts c
                       LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                       ORDER BY c.fulfilled ASC, c.last_updated DESC"""
                ).fetchall()
            # group by contract id
            contracts: dict[str, dict] = {}
            for r in rows:
                cid = r[0]
                if cid not in contracts:
                    contracts[cid] = {
                        "id": cid, "faction_symbol": r[1], "type": r[2],
                        "accepted": bool(r[3]), "fulfilled": bool(r[4]),
                        "expiration": r[5], "deadline": r[6],
                        "on_accepted": r[7], "on_fulfilled": r[8],
                        "deliver": [],
                    }
                if r[9]:  # trade_symbol not null
                    contracts[cid]["deliver"].append({
                        "trade_symbol":      r[9],
                        "destination_symbol": r[10],
                        "units_required":    r[11],
                        "units_fulfilled":   r[12],
                    })
            self.app.call_from_thread(self._update_table, list(contracts.values()))
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#contracts-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_table(self, contracts: list[dict]) -> None:
        t = self.query_one("#contracts-table", DataTable)
        t.clear()
        self._contracts_map = {}
        for c in contracts:
            cid = c["id"]
            self._contracts_map[cid] = c
            status_text = (
                Text("✅ DONE",  style="green")  if c["fulfilled"] else
                Text("🔄 ACTIVE", style="cyan")   if c["accepted"]  else
                Text("📋 AVAIL", style="yellow")
            )
            for i, d in enumerate(c.get("deliver", [{"trade_symbol": "—", "destination_symbol": "—",
                                                       "units_required": 0, "units_fulfilled": 0}])):
                req  = d["units_required"]
                fulf = d["units_fulfilled"]
                pct  = int(100 * fulf / max(1, req))
                bar  = "█" * (pct // 10) + "░" * (10 - pct // 10)
                pct_color = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
                row_key = f"{cid}|{i}"
                t.add_row(
                    Text(cid[:12] + "…", style="dim"),
                    c["type"],
                    Text(d["trade_symbol"], style="bold"),
                    f"{fulf:,}/{req:,}",
                    Text(f"{bar} {pct}%", style=pct_color),
                    d.get("destination_symbol") or "—",
                    _deadline_str(c.get("deadline")),
                    f"{(c.get('on_fulfilled') or 0):,} cr",
                    status_text,
                    key=row_key,
                )
        self.query_one("#contracts-status", Label).update(
            f"[dim]{len(contracts)} contracts  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    @on(DataTable.RowSelected, "#contracts-table")
    def _on_contract_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        cid = key.split("|")[0]
        contract = self._contracts_map.get(cid)
        if contract:
            self.app.push_screen(ContractDetailModal(contract))

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 4 — Markets
# ---------------------------------------------------------------------------

class MarketsScreen(Screen):
    BINDINGS = [
        ("r", "refresh_data", "Refresh"),
        ("f", "fetch_listings", "Fetch Listings"),
    ]

    def on_mount(self) -> None:
        self._selected_market: str = ""

        mt = self.query_one("#markets-table", DataTable)
        mt.add_columns("Waypoint", "Goods", "Prices", "Updated")

        pt = self.query_one("#prices-table", DataTable)
        pt.add_columns("Good", "Type", "Supply", "Activity", "Buy", "Sell", "Volume", "Age")

        at = self.query_one("#arb-table", DataTable)
        at.add_columns("Good", "Buy At", "Buy Price", "Sell At", "Sell Price", "Margin", "ROI%", "Age")

        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Markets  •  Click market → prices  •  F / button → Refresh Listings from API",
            classes="screen-hint",
        )
        with Horizontal(id="markets-main"):
            with Vertical(id="market-list-panel"):
                yield Label("Markets", classes="panel-title")
                yield DataTable(id="markets-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="market-detail-panel"):
                yield Label("Select a market", id="market-header", classes="panel-title")
                with TabbedContent(id="market-tabs"):
                    with TabPane("Prices"):
                        yield DataTable(id="prices-table", cursor_type="row", zebra_stripes=True)
                    with TabPane("Arbitrage"):
                        yield DataTable(id="arb-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="market-actions"):
                    yield Button("Refresh Listings  [F]", id="refresh-btn", variant="primary")
                    yield Label("", id="refresh-status")
        yield Label("", id="markets-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            with db._conn() as con:
                rows = con.execute(
                    """SELECT ml.waypoint_symbol,
                              COUNT(DISTINCT ml.trade_symbol)              AS good_count,
                              COUNT(DISTINCT mp.trade_symbol)              AS price_count,
                              MAX(ml.last_updated)                         AS updated
                       FROM market_listings ml
                       JOIN waypoints w ON w.symbol = ml.waypoint_symbol
                                       AND w.system_symbol = ?
                       LEFT JOIN market_prices mp ON mp.waypoint_symbol = ml.waypoint_symbol
                       GROUP BY ml.waypoint_symbol
                       ORDER BY ml.waypoint_symbol""",
                    (SYSTEM,),
                ).fetchall()
            arb = db.get_arbitrage_opportunities(SYSTEM, min_margin=50)
            self.app.call_from_thread(self._update_markets, rows, arb)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#markets-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_markets(self, rows: list, arb: list[dict]) -> None:
        mt = self.query_one("#markets-table", DataTable)
        mt.clear()
        for wp, good_count, price_count, updated in rows:
            has = Text("✅ Yes", "green") if price_count > 0 else Text("⚠ No", "yellow")
            mt.add_row(
                Text(wp, style="cyan"),
                str(good_count),
                has,
                _ts_ago(updated),
                key=wp,
            )

        at = self.query_one("#arb-table", DataTable)
        at.clear()
        for o in arb:
            at.add_row(
                Text(o["trade_symbol"], style="bold"),
                o["buy_at"],
                f"{o['buy_price']:,}",
                o["sell_at"],
                f"{o['sell_price']:,}",
                Text(f"+{o['margin']:,}", style="bold green"),
                f"{o['pct_margin']}%",
                _ts_ago(o["oldest_data"]),
            )
        if not arb:
            at.add_row("No opportunities found", "Need ship price data", "", "", "", "", "", "")

        self.query_one("#markets-status", Label).update(
            f"[dim]{len(rows)} markets  •  {len(arb)} arbitrage opportunities  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    @on(DataTable.RowSelected, "#markets-table")
    def _on_market_selected(self, event: DataTable.RowSelected) -> None:
        wp = str(event.row_key.value)
        self._selected_market = wp
        self.query_one("#market-header", Label).update(f"  {wp}  ")
        self._load_prices(wp)

    @work(thread=True)
    def _load_prices(self, waypoint: str) -> None:
        prices = db.get_market_prices_for_waypoint(waypoint)
        self.app.call_from_thread(self._update_prices, prices)

    def _update_prices(self, prices: list[dict]) -> None:
        pt = self.query_one("#prices-table", DataTable)
        pt.clear()
        for p in prices:
            pt.add_row(
                Text(p["trade_symbol"], style="bold"),
                p["listing_type"] or "—",
                _supply_text(p["supply"]),
                p["activity"] or "—",
                f"{p['purchase_price']:,}" if p["purchase_price"] else "—",
                f"{p['sell_price']:,}"     if p["sell_price"]     else "—",
                str(p["trade_volume"])     if p["trade_volume"]   else "—",
                _ts_ago(p["last_updated"]),
            )
        if not prices:
            pt.add_row(
                Text("No price data — a ship must dock here to record live prices", style="dim"),
                "", "", "", "", "", "", "",
            )

    @on(Button.Pressed, "#refresh-btn")
    def _on_refresh_btn(self) -> None:
        self.action_fetch_listings()

    def action_fetch_listings(self) -> None:
        if not self._selected_market:
            self.query_one("#refresh-status", Label).update("[yellow]Select a market first[/yellow]")
            return
        self._fetch_from_api(self._selected_market)

    @work(thread=True)
    def _fetch_from_api(self, waypoint: str) -> None:
        self.app.call_from_thread(
            self.query_one("#refresh-status", Label).update,
            f"[yellow]Fetching {waypoint}…[/yellow]",
        )
        try:
            system = _system_from_wp(waypoint)
            data   = universe_api.get_market(system, waypoint)
            db.upsert_market_listings(waypoint, data)
            tg = data.get("tradeGoods", [])
            if tg:
                db.upsert_market_prices(waypoint, tg)
                msg = f"[green]✅ Updated listings + {len(tg)} live prices[/green]"
            else:
                msg = "[green]✅ Listings updated  [dim](prices need ship docked)[/dim][/green]"
            prices = db.get_market_prices_for_waypoint(waypoint)
            self.app.call_from_thread(self._update_prices, prices)
            self.app.call_from_thread(
                self.query_one("#refresh-status", Label).update, msg
            )
        except SpaceTradersError as e:
            self.app.call_from_thread(
                self.query_one("#refresh-status", Label).update,
                f"[red]API error: {e}[/red]",
            )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 5 — Universe
# ---------------------------------------------------------------------------

class UniverseScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        self._waypoints: list[dict] = []
        t = self.query_one("#universe-table", DataTable)
        t.add_columns("Waypoint", "Type", "Coords", "Traits")
        self.refresh_data()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Universe  •  Type to filter  •  Click row for sourcing analysis",
            classes="screen-hint",
        )
        with Horizontal(id="universe-filter-bar"):
            yield Input(
                placeholder="Filter by type or trait  (e.g. ASTEROID, MARKETPLACE, SHIPYARD)…",
                id="universe-filter",
            )
        with Horizontal(id="universe-main"):
            with Vertical(id="universe-list-panel"):
                yield DataTable(id="universe-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="universe-detail-panel"):
                yield Static("", id="universe-analysis")
        yield Label("", id="universe-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            wps = db.get_all_waypoints(SYSTEM)
            self.app.call_from_thread(self._loaded, wps)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#universe-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _loaded(self, wps: list[dict]) -> None:
        self._waypoints = wps
        self._apply_filter(self.query_one("#universe-filter", Input).value)
        self.query_one("#universe-status", Label).update(
            f"[dim]{len(wps)} waypoints in {SYSTEM}[/dim]"
        )

    def _apply_filter(self, text: str) -> None:
        t = self.query_one("#universe-table", DataTable)
        t.clear()
        f = text.upper().strip()
        for wp in self._waypoints:
            if f and f not in wp["type"] and not any(
                f in tr["symbol"] for tr in wp.get("traits", [])
            ):
                continue
            traits = ", ".join(tr["symbol"] for tr in wp.get("traits", []))
            t.add_row(
                Text(wp["symbol"], style="cyan"),
                Text(wp["type"],   style="yellow"),
                f"({wp['x']}, {wp['y']})",
                Text(traits or "—", style="dim"),
                key=wp["symbol"],
            )

    @on(Input.Changed, "#universe-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(DataTable.RowSelected, "#universe-table")
    def _on_wp_selected(self, event: DataTable.RowSelected) -> None:
        sym = str(event.row_key.value)
        self._show_analysis(sym)

    def _show_analysis(self, symbol: str) -> None:
        wp = next((w for w in self._waypoints if w["symbol"] == symbol), None)
        if not wp:
            return
        traits = [t["symbol"] for t in wp.get("traits", [])]
        lines = [
            f"[bold cyan]╔══ {symbol} ══╗[/bold cyan]",
            f"  Type:    {wp['type']}",
            f"  Coords:  ({wp['x']}, {wp['y']})",
            f"  Traits:  {', '.join(traits) or '—'}",
            "",
        ]
        # Mining
        mineable: dict[str, list[str]] = {}
        for trait in traits:
            with db._conn() as con:
                goods = con.execute(
                    "SELECT trade_symbol FROM deposit_goods WHERE trait_symbol = ?",
                    (trait,),
                ).fetchall()
            for (g,) in goods:
                mineable.setdefault(g, []).append(trait)
        if mineable:
            lines.append("[bold green]⛏  Mineable goods:[/bold green]")
            for good, from_traits in mineable.items():
                lines.append(
                    f"    [green]{good}[/green]  "
                    f"[dim](via {', '.join(from_traits)})[/dim]"
                )
        else:
            lines.append("[dim]No mining deposits at this waypoint[/dim]")

        lines.append("")
        # Market
        if "MARKETPLACE" in traits:
            lines.append("[bold cyan]🛒  Market listings:[/bold cyan]")
            with db._conn() as con:
                listings = con.execute(
                    "SELECT trade_symbol, listing_type FROM market_listings "
                    "WHERE waypoint_symbol = ? ORDER BY listing_type, trade_symbol",
                    (symbol,),
                ).fetchall()
            for ltype, label in [("EXPORT", "Exports"), ("IMPORT", "Imports"), ("EXCHANGE", "Exchange")]:
                items = [g for g, lt in listings if lt == ltype]
                if items:
                    color = "green" if ltype == "EXPORT" else "red" if ltype == "IMPORT" else "yellow"
                    lines.append(f"  {label}: [{color}]{', '.join(items[:8])}[/{color}]")
            # Price summary
            prices = db.get_market_prices_for_waypoint(symbol)
            if prices:
                lines.append(f"  [dim]{len(prices)} live prices cached[/dim]")
            else:
                lines.append("  [dim]No live prices — dock a ship to record them[/dim]")

        # Arbitrage
        lines.append("")
        arb = db.get_arbitrage_opportunities(SYSTEM, min_margin=50)
        relevant = [o for o in arb if symbol in (o["buy_at"], o["sell_at"])]
        if relevant:
            lines.append("[bold yellow]💰  Arbitrage involving this market:[/bold yellow]")
            for o in relevant[:5]:
                direction = "BUY" if o["buy_at"] == symbol else "SELL"
                lines.append(
                    f"  [{('cyan' if direction=='BUY' else 'green')}]{direction}[/] "
                    f"{o['trade_symbol']:22s}  +{o['margin']:,} cr/u  ({o['pct_margin']}% ROI)"
                )

        self.query_one("#universe-analysis", Static).update("\n".join(lines))

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 6 — Surveys
# ---------------------------------------------------------------------------

class SurveysScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        t = self.query_one("#surveys-table", DataTable)
        t.add_columns("Signature", "Waypoint", "Deposits", "L", "M", "S", "Expires")
        self.refresh_data()
        self.set_interval(15, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Active Surveys  •  Deposits in the shared survey pool — surveyors populate this",
            classes="screen-hint",
        )
        yield DataTable(id="surveys-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="surveys-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            surveys = db.load_active_surveys()
            self.app.call_from_thread(self._update_table, surveys)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#surveys-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_table(self, surveys: list[dict]) -> None:
        t = self.query_one("#surveys-table", DataTable)
        t.clear()
        for sv in surveys:
            sig  = sv.get("signature", "?")
            wp   = sv.get("symbol", "?")
            deps = sv.get("deposits", [])
            goods = ", ".join(sorted(set(d["symbol"] for d in deps)))
            L = sum(1 for d in deps if d.get("size") == "LARGE")
            M = sum(1 for d in deps if d.get("size") == "MODERATE")
            S = sum(1 for d in deps if d.get("size") == "SMALL")
            t.add_row(
                Text(sig[:22] + "…", style="dim"),
                Text(wp, style="cyan"),
                Text(goods or "—"),
                Text(str(L) if L else "—", style="green"  if L else "dim"),
                Text(str(M) if M else "—", style="yellow" if M else "dim"),
                Text(str(S) if S else "—", style="white"  if S else "dim"),
                _deadline_str(sv.get("expiration")),
            )
        if not surveys:
            t.add_row("No active surveys", "Surveyor ships populate this", "", "", "", "", "")

        self.query_one("#surveys-status", Label).update(
            f"[dim]{len(surveys)} active surveys  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 7 — Analytics
# ---------------------------------------------------------------------------

class AnalyticsScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        self._all_txns:    list = []
        self._yields_20m:  list = []
        self._yields_1h:   list = []
        self._yields_all:  list = []
        self._txn_filter   = "ALL"
        self._yield_window = 1200

        tt = self.query_one("#txn-table", DataTable)
        tt.add_columns("Time", "Type", "Good", "Units", "Price/u", "Total", "Waypoint", "Ship")

        yt = self.query_one("#yields-table", DataTable)
        yt.add_columns("Good", "Total Units", "Extractions", "Avg/Extract", "Surveyed %")

        ct = self.query_one("#contracts-history-table", DataTable)
        ct.add_columns("Good", "Units", "Payout", "Advance", "Accepted", "Completed", "Duration", "Status")

        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Analytics  •  Transaction history, extraction yields, hourly income",
            classes="screen-hint",
        )
        with TabbedContent(id="analytics-tabs"):
            with TabPane("Transactions"):
                with Horizontal(id="txn-filters"):
                    yield Button("All",       id="btn-all",  variant="primary")
                    yield Button("Sales",     id="btn-sell", variant="default")
                    yield Button("Purchases", id="btn-buy",  variant="default")
                    yield Label("", id="txn-summary")
                yield DataTable(id="txn-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Yields"):
                with Horizontal(id="yields-filters"):
                    yield Button("Last 20m", id="btn-20m", variant="primary")
                    yield Button("Last 1hr", id="btn-1h",  variant="default")
                    yield Button("All Time", id="btn-all-time", variant="default")
                yield DataTable(id="yields-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Income Chart"):
                yield Static("", id="income-breakdown")
            with TabPane("Contracts"):
                yield DataTable(id="contracts-history-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="analytics-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            now = time.time()
            with db._conn() as con:
                txns = con.execute(
                    "SELECT timestamp, type, trade_symbol, units, price_per_unit, "
                    "total_price, waypoint_symbol, ship_symbol "
                    "FROM market_transactions ORDER BY timestamp DESC LIMIT 200"
                ).fetchall()

                def _yields(cutoff: float | None) -> list:
                    q = (
                        "SELECT trade_symbol, SUM(units), COUNT(*), "
                        "SUM(CASE WHEN survey_signature IS NOT NULL THEN 1 ELSE 0 END) "
                        "FROM extraction_yields"
                    )
                    if cutoff:
                        return con.execute(
                            q + " WHERE timestamp > ? GROUP BY trade_symbol ORDER BY SUM(units) DESC",
                            (cutoff,),
                        ).fetchall()
                    return con.execute(
                        q + " GROUP BY trade_symbol ORDER BY SUM(units) DESC"
                    ).fetchall()

                y_20m = _yields(now - 1200)
                y_1h  = _yields(now - 3600)
                y_all = _yields(None)

                income: list[tuple[int, int]] = []
                for i in range(12):
                    start = now - (i + 1) * 3600
                    end   = now - i * 3600
                    row = con.execute(
                        """SELECT SUM(CASE WHEN type='SELL'     THEN  total_price ELSE 0 END)
                                - SUM(CASE WHEN type='PURCHASE' THEN  total_price ELSE 0 END)
                           FROM market_transactions WHERE timestamp BETWEEN ? AND ?""",
                        (start, end),
                    ).fetchone()
                    income.append((i, int(row[0] or 0)))

                contract_history = con.execute(
                    """SELECT c.id, cd.trade_symbol, cd.units_required,
                              c.on_accepted, c.on_fulfilled,
                              c.accepted_at, c.fulfilled_at, c.fulfilled
                       FROM contracts c
                       LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                       WHERE c.accepted = 1
                       ORDER BY COALESCE(c.fulfilled_at, c.accepted_at) DESC
                       LIMIT 50"""
                ).fetchall()

            self.app.call_from_thread(
                self._update, txns, y_20m, y_1h, y_all, income, contract_history
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#analytics-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update(self, txns, y_20m, y_1h, y_all, income, contract_history=None) -> None:
        self._all_txns        = list(txns)
        self._yields_20m      = list(y_20m)
        self._yields_1h       = list(y_1h)
        self._yields_all      = list(y_all)
        self._income          = income
        self._contract_history = list(contract_history or [])
        self._update_txn_table()
        self._update_yields_table()
        self._update_income()
        self._update_contracts_history()
        self.query_one("#analytics-status", Label).update(
            f"[dim]{len(txns)} transactions  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    def _update_txn_table(self) -> None:
        tt = self.query_one("#txn-table", DataTable)
        tt.clear()
        f = self._txn_filter
        sell_total = buy_total = 0
        for ts, ttype, good, units, ppu, total, wp, ship in self._all_txns:
            if f == "SELL" and ttype != "SELL":
                continue
            if f == "BUY" and ttype != "PURCHASE":
                continue
            color = "green" if ttype == "SELL" else "red"
            sign  = "+" if ttype == "SELL" else "-"
            if ttype == "SELL":
                sell_total += total
            else:
                buy_total += total
            dt = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")
            tt.add_row(
                Text(dt, style="dim"),
                Text(ttype, style=color),
                Text(good, style="bold"),
                str(units),
                f"{ppu:,}",
                Text(f"{sign}{total:,}", style=color),
                Text(wp   or "—", style="dim"),
                Text(ship or "—", style="dim"),
            )
        net = sell_total - buy_total
        net_color = "green" if net >= 0 else "red"
        net_sign  = "+" if net >= 0 else ""
        self.query_one("#txn-summary", Label).update(
            f"  [green]+{sell_total:,}[/green] sales  "
            f"[red]-{buy_total:,}[/red] buys  "
            f"Net: [{net_color}]{net_sign}{net:,}[/{net_color}] cr"
        )

    def _update_yields_table(self) -> None:
        yt = self.query_one("#yields-table", DataTable)
        yt.clear()
        data = (
            self._yields_20m if self._yield_window == 1200 else
            self._yields_1h  if self._yield_window == 3600 else
            self._yields_all
        )
        for good, total_u, count, surveyed in data:
            avg = total_u / max(1, count)
            pct = int(100 * surveyed / max(1, count))
            yt.add_row(
                Text(good, style="bold green"),
                f"{total_u:,}",
                str(count),
                f"{avg:.1f}",
                f"{pct}%",
            )
        if not data:
            yt.add_row("No yield data", "Mining populates this", "", "", "")

    def _update_income(self) -> None:
        income = getattr(self, "_income", [])
        if not income:
            return
        max_val = max((abs(v) for _, v in income), default=1)
        lines   = ["[bold cyan]Hourly Net Income — last 12 hours[/bold cyan]", ""]
        for i, net in income:
            label   = f"{i}–{i+1}h ago"
            bar_len = int(abs(net) / max(max_val, 1) * 28)
            bar     = "▓" * bar_len
            if net >= 0:
                lines.append(
                    f"  [dim]{label:12s}[/dim] [green]{bar:<28s}  +{net:>10,} cr[/green]"
                )
            else:
                lines.append(
                    f"  [dim]{label:12s}[/dim] [red]{bar:<28s}   {net:>10,} cr[/red]"
                )
        self.query_one("#income-breakdown", Static).update("\n".join(lines))

    def _update_contracts_history(self) -> None:
        ct = self.query_one("#contracts-history-table", DataTable)
        ct.clear()
        rows = getattr(self, "_contract_history", [])
        if not rows:
            ct.add_row("No contract history yet", "", "", "", "", "", "", "")
            return
        total_payout = 0
        total_secs   = 0
        completed    = 0
        for cid, good, units_req, on_accepted, on_fulfilled, accepted_at, fulfilled_at, is_fulfilled in rows:
            payout   = on_fulfilled or 0
            advance  = on_accepted  or 0
            good_str = good or "—"
            units_str = f"{units_req:,}" if units_req else "—"

            if accepted_at:
                accepted_str = datetime.fromtimestamp(accepted_at).strftime("%m/%d %H:%M")
            else:
                accepted_str = "—"

            if fulfilled_at and is_fulfilled:
                completed_str = datetime.fromtimestamp(fulfilled_at).strftime("%m/%d %H:%M")
                if accepted_at:
                    secs = int(fulfilled_at - accepted_at)
                    h, rem = divmod(secs, 3600)
                    m      = rem // 60
                    dur_str = f"{h}h {m}m" if h else f"{m}m"
                    total_secs += secs
                else:
                    dur_str = "—"
                status = Text("✅ Done", style="green")
                total_payout += payout
                completed += 1
            else:
                completed_str = "—"
                dur_str       = "—"
                status = Text("🔄 Active", style="cyan")

            ct.add_row(
                Text(good_str, style="bold"),
                units_str,
                Text(f"{payout:,} cr", style="green"),
                Text(f"{advance:,} cr", style="dim"),
                Text(accepted_str,  style="dim"),
                Text(completed_str, style="dim"),
                dur_str,
                status,
            )
        if completed > 0:
            avg_secs = total_secs // completed
            ah, arem = divmod(avg_secs, 3600)
            am       = arem // 60
            avg_str  = f"{ah}h {am}m" if ah else f"{am}m"
            ct.add_row(
                Text(f"── {completed} completed ──", style="bold dim"),
                "",
                Text(f"Total: {total_payout:,} cr", style="bold green"),
                "",
                "",
                "",
                Text(f"Avg: {avg_str}", style="bold"),
                "",
            )

    # Filter buttons
    @on(Button.Pressed, "#btn-all")
    def _f_all(self) -> None:
        self._txn_filter = "ALL"
        self._update_txn_table()

    @on(Button.Pressed, "#btn-sell")
    def _f_sell(self) -> None:
        self._txn_filter = "SELL"
        self._update_txn_table()

    @on(Button.Pressed, "#btn-buy")
    def _f_buy(self) -> None:
        self._txn_filter = "BUY"
        self._update_txn_table()

    @on(Button.Pressed, "#btn-20m")
    def _y_20m(self) -> None:
        self._yield_window = 1200
        self._update_yields_table()

    @on(Button.Pressed, "#btn-1h")
    def _y_1h(self) -> None:
        self._yield_window = 3600
        self._update_yields_table()

    @on(Button.Pressed, "#btn-all-time")
    def _y_all(self) -> None:
        self._yield_window = 0
        self._update_yields_table()

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 8 — Map
# ---------------------------------------------------------------------------

class MapScreen(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(3.0, self.refresh_data)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Map  •  ✦ = your ship  •  ships in transit shown at interpolated position",
            classes="screen-hint",
        )
        with Horizontal(id="map-main"):
            yield Static("", id="map-canvas")
            with Vertical(id="map-legend-panel"):
                yield Static("", id="map-legend")
                yield Static("", id="map-ships")
        yield Label("", id="map-status", classes="status-bar")
        yield Footer()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            ships = fleet_api.get_my_ships()

            # Detect current system from ships' locations (may differ from play.py SYSTEM)
            current_system = SYSTEM
            for ship in ships:
                wp = ship.get("nav", {}).get("waypointSymbol", "")
                parts = wp.split("-")
                if len(parts) >= 3:
                    current_system = "-".join(parts[:2])
                    break

            waypoints = db.get_all_waypoints(current_system)

            # DB miss — fetch from API and cache for future refreshes
            if not waypoints:
                raw = universe_api.get_waypoints(current_system)
                waypoints = [
                    {"symbol": w["symbol"], "type": w["type"],
                     "x": w.get("x", 0), "y": w.get("y", 0)}
                    for w in raw
                ]
                db.upsert_waypoints(raw)

            self.app.call_from_thread(self._update_map, waypoints, ships)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#map-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_map(self, waypoints: list[dict], ships: list[dict]) -> None:
        # Assign a unique icon+color to each ship (stable: sorted by symbol)
        sorted_ships = sorted(ships, key=lambda s: s.get("symbol", ""))
        ship_icons: dict[str, tuple[str, str]] = {
            s.get("symbol", "?"): _SHIP_MAP_PALETTE[i % len(_SHIP_MAP_PALETTE)]
            for i, s in enumerate(sorted_ships)
        }

        canvas = self.query_one("#map-canvas", Static)
        # Use app size as fallback before first layout pass
        cw = canvas.size.width  if canvas.size.width  > 5 else self.app.size.width  - 28
        ch = canvas.size.height if canvas.size.height > 5 else self.app.size.height - 5
        w = max(20, cw - 2)
        h = max(10, ch - 2)
        canvas.update(_render_map(waypoints, ships, w, h, ship_icons))

        legend = Text()
        legend.append("╔══ LEGEND ══╗\n", style="bold cyan")
        for label, (char, style) in [
            ("Planet",         ("◉", "bright_blue")),
            ("Gas Giant",      ("◎", "blue")),
            ("Moon",           ("○", "white")),
            ("Station",        ("▣", "bright_green")),
            ("Jump Gate",      ("⊕", "bright_cyan")),
            ("Asteroid",       ("⬡", "yellow")),
            ("Eng. Asteroid",  ("⬡", "bright_yellow")),
        ]:
            legend.append(f"  {char} ", style=style)
            legend.append(f"{label}\n")
        self.query_one("#map-legend", Static).update(legend)

        ships_text = Text()
        ships_text.append("╔══ FLEET ══╗\n", style="bold cyan")
        for ship in ships:
            sym    = ship.get("symbol", "?")
            nav    = ship.get("nav", {})
            status = nav.get("status", "")
            wp     = nav.get("waypointSymbol", "?")
            map_icon, map_style = ship_icons.get(sym, ("✦", "bold bright_yellow"))
            ships_text.append(f"  {map_icon} ", style=map_style)
            ships_text.append(f"{sym}\n", style="cyan")
            if status == "IN_TRANSIT":
                route = nav.get("route", {})
                dest  = route.get("destination", {}).get("symbol", wp)
                arr   = route.get("arrival", "")
                ships_text.append(f"    \u2192 {dest}\n", style="yellow")
                ships_text.append(f"    ETA {_eta_str(arr)}\n", style="dim")
            else:
                ships_text.append(f"    @ {wp}\n", style="yellow")
                ships_text.append(f"    {_ship_activity(ship)}\n", style="dim")
        self.query_one("#map-ships", Static).update(ships_text)

        self.query_one("#map-status", Label).update(
            f"[dim]{len(waypoints)} waypoints  \u2022  {len(ships)} ships  \u2022  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}  \u2022  Auto-refresh 3s[/dim]"
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 9 — Settings
# ---------------------------------------------------------------------------

# All assignable roles (auto = derive from ship type)
_ALL_ROLES = ["auto", "miner", "surveyor", "hauler", "trader", "explorer", "siphoner", "idle"]


class SettingsScreen(Screen):
    """Live bot control: toggle auto-buy and manage the full ship buy list."""

    BINDINGS = [("r", "action_refresh", "Refresh")]

    # Track which row index is loaded in the edit form (-1 = new entry mode)
    _editing_index: int = -1

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(
            "Settings  •  Changes take effect on the bot's next loop — no restart needed",
            classes="screen-hint",
        )
        with Vertical(id="settings-main"):

            # ── Auto-buy toggle ───────────────────────────────────────────
            with Container(id="auto-buy-section"):
                yield Label("[bold cyan]╔══ FLEET PURCHASING ══╗[/bold cyan]", classes="panel-title")
                with Horizontal(id="auto-buy-row"):
                    yield Label("Auto-Buy Ships:  ")
                    yield Button("loading…", id="toggle-auto-buy", variant="default")
                    yield Label(
                        "   When ON, the bot buys ships from the list below after each contract",
                        classes="hint-text",
                    )

            # ── Command ship role ─────────────────────────────────────────
            with Container(id="cmd-ship-section"):
                yield Label("[bold cyan]╔══ COMMAND SHIP ROLE (Ship-2) ══╗[/bold cyan]", classes="panel-title")
                with Horizontal(id="cmd-ship-row"):
                    yield Label("Ship-2 Role:  ")
                    yield Button("loading…", id="btn-cmd-idle",   variant="default")
                    yield Button("loading…", id="btn-cmd-hauler", variant="default")
                    yield Label(
                        "   Hauler: Ship-2 ferries ore from miners at the asteroid to the delivery WP",
                        classes="hint-text",
                    )

            # ── Ship buy list ─────────────────────────────────────────────
            with Container(id="ship-list-section"):
                yield Label("[bold cyan]╔══ SHIP BUY LIST ══╗[/bold cyan]", classes="panel-title")
                yield Label(
                    "  Click a row to edit it.  Leave role as 'auto' to let the bot decide.",
                    classes="hint-text",
                )
                yield DataTable(id="targets-table", cursor_type="row", zebra_stripes=True)

                # ── Edit / Add form ───────────────────────────────────────
                with Container(id="edit-form"):
                    yield Label("[bold]── Edit / Add entry ──[/bold]", classes="panel-title")
                    with Horizontal(id="form-row-1"):
                        yield Label("Ship type: ", classes="form-label")
                        yield Select(
                            [(t, t) for t in _BUYABLE_SHIP_TYPES],
                            prompt="Select ship type",
                            id="sel-type",
                            allow_blank=True,
                        )
                        yield Label("  Role: ", classes="form-label")
                        yield Select(
                            [(r, r) for r in _ALL_ROLES],
                            prompt="Select role",
                            id="sel-role",
                            allow_blank=False,
                            value="auto",
                        )
                        yield Label("  Max to buy: ", classes="form-label")
                        yield Input(
                            placeholder="e.g. 3",
                            id="inp-max",
                            restrict=r"\d*",
                        )
                    with Horizontal(id="form-row-2"):
                        yield Button("➕  Add New",        id="btn-add",    variant="primary")
                        yield Button("💾  Save Changes",   id="btn-save",   variant="success")
                        yield Button("🗑  Remove Selected", id="btn-remove", variant="error")
                        yield Button("🧹  Clear All",       id="btn-clear",  variant="warning")
                        yield Label("", id="form-msg")

        yield Label("", id="settings-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#targets-table", DataTable)
        t.add_columns("  #", "Ship Type", "Role", "Max")
        self._refresh()
        self._set_form_mode_new()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        # Auto-buy toggle
        enabled = db.get_bot_setting("auto_buy_ships", "true").lower() == "true"
        btn = self.query_one("#toggle-auto-buy", Button)
        btn.label   = "✓  ENABLED"  if enabled else "✗  DISABLED"
        btn.variant = "success"     if enabled else "error"

        # Command ship role buttons
        cmd_role   = db.get_bot_setting("command_ship_role", "idle")
        idle_btn   = self.query_one("#btn-cmd-idle",   Button)
        hauler_btn = self.query_one("#btn-cmd-hauler", Button)
        if cmd_role == "hauler":
            idle_btn.label,   idle_btn.variant   = "Idle",        "default"
            hauler_btn.label, hauler_btn.variant = "✓  Hauler",   "success"
        else:
            idle_btn.label,   idle_btn.variant   = "✓  Idle",     "success"
            hauler_btn.label, hauler_btn.variant = "Hauler",      "default"

        # Ship buy list table
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets: list[dict] = json.loads(raw)
        except Exception:
            targets = []

        t = self.query_one("#targets-table", DataTable)
        t.clear()
        for i, entry in enumerate(targets):
            stype = entry.get("type", "?")
            role  = entry.get("role", "auto")
            maxc  = entry.get("max",  1)
            role_style = {
                "miner": "green", "surveyor": "magenta", "hauler": "blue",
                "trader": "yellow", "explorer": "cyan", "siphoner": "cyan",
                "idle": "dim", "auto": "white",
            }.get(role, "white")
            t.add_row(
                Text(str(i + 1), style="dim"),
                Text(stype,      style="bold cyan"),
                Text(role,       style=role_style),
                Text(str(maxc),  style="bold green"),
                key=str(i),
            )

        self.query_one("#settings-status", Label).update(
            f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            + ("[green]Auto-buy ON[/green]" if enabled else "[red]Auto-buy OFF[/red]")
            + f"  •  {len(targets)} entr{'y' if len(targets) == 1 else 'ies'} in buy list[/dim]"
        )

    # ------------------------------------------------------------------
    # Form helpers
    # ------------------------------------------------------------------

    def _set_form_mode_new(self) -> None:
        """Clear the form for adding a brand-new entry."""
        self._editing_index = -1
        self.query_one("#sel-type", Select).value = Select.NULL
        self.query_one("#sel-role", Select).value = "auto"
        self.query_one("#inp-max",  Input).value  = ""
        self.query_one("#btn-save", Button).disabled = True
        self.query_one("#form-msg", Label).update("[dim]Fill in the fields above and click Add New[/dim]")

    def _load_row_into_form(self, idx: int) -> None:
        """Populate the edit form from the entry at list index idx."""
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
        except Exception:
            return
        if idx < 0 or idx >= len(targets):
            return
        entry = targets[idx]
        self._editing_index = idx
        stype = entry.get("type", "")
        role  = entry.get("role", "auto")
        maxc  = entry.get("max", 1)
        if stype in _BUYABLE_SHIP_TYPES:
            self.query_one("#sel-type", Select).value = stype
        self.query_one("#sel-role", Select).value = role if role in _ALL_ROLES else "auto"
        self.query_one("#inp-max",  Input).value  = str(maxc)
        self.query_one("#btn-save", Button).disabled = False
        self.query_one("#form-msg", Label).update(
            f"[yellow]Editing row {idx + 1}: [bold]{stype}[/bold] — change fields then click Save Changes[/yellow]"
        )

    def _read_form(self) -> tuple[str, str, int] | None:
        """Read and validate current form values. Returns (type, role, max) or None."""
        msg = self.query_one("#form-msg", Label)
        stype_val = self.query_one("#sel-type", Select).value
        role_val  = self.query_one("#sel-role", Select).value
        max_str   = self.query_one("#inp-max",  Input).value.strip()

        if stype_val is Select.NULL or not stype_val:
            msg.update("[red]Please select a ship type[/red]")
            return None
        if not max_str:
            msg.update("[red]Max count is required[/red]")
            return None
        try:
            max_count = int(max_str)
            if max_count < 1:
                raise ValueError
        except ValueError:
            msg.update("[red]Max must be a whole number ≥ 1[/red]")
            return None

        role = str(role_val) if role_val and role_val is not Select.NULL else "auto"
        return str(stype_val), role, max_count

    def _save_targets(self, targets: list[dict]) -> None:
        db.set_bot_setting("ship_buy_list", json.dumps(targets))
        self._refresh()

    # ------------------------------------------------------------------
    # Row click → populate form
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "targets-table":
            return
        self._load_row_into_form(event.cursor_row)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#btn-cmd-idle")
    def _set_cmd_idle(self) -> None:
        db.set_bot_setting("command_ship_role", "idle")
        self._refresh()

    @on(Button.Pressed, "#btn-cmd-hauler")
    def _set_cmd_hauler(self) -> None:
        db.set_bot_setting("command_ship_role", "hauler")
        self._refresh()

    @on(Button.Pressed, "#toggle-auto-buy")
    def _toggle_auto_buy(self) -> None:
        current = db.get_bot_setting("auto_buy_ships", "true").lower() == "true"
        db.set_bot_setting("auto_buy_ships", "false" if current else "true")
        self._refresh()

    @on(Button.Pressed, "#btn-add")
    def _add_ship(self) -> None:
        parsed = self._read_form()
        if parsed is None:
            return
        stype, role, max_count = parsed
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
        except Exception:
            targets = []
        targets.append({"type": stype, "role": role, "max": max_count})
        self._save_targets(targets)
        self.query_one("#form-msg", Label).update(
            f"[green]Added {stype} (role={role}, max={max_count}) ✓[/green]"
        )
        self._set_form_mode_new()

    @on(Button.Pressed, "#btn-save")
    def _save_ship(self) -> None:
        if self._editing_index < 0:
            self.query_one("#form-msg", Label).update("[yellow]Click a row first to edit it[/yellow]")
            return
        parsed = self._read_form()
        if parsed is None:
            return
        stype, role, max_count = parsed
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
        except Exception:
            targets = []
        if self._editing_index >= len(targets):
            self.query_one("#form-msg", Label).update("[red]Row no longer exists — try refreshing[/red]")
            return
        targets[self._editing_index] = {"type": stype, "role": role, "max": max_count}
        self._save_targets(targets)
        self.query_one("#form-msg", Label).update(
            f"[green]Row {self._editing_index + 1} saved: {stype} (role={role}, max={max_count}) ✓[/green]"
        )
        self._editing_index = -1
        self.query_one("#btn-save", Button).disabled = True

    @on(Button.Pressed, "#btn-remove")
    def _remove_ship(self) -> None:
        t = self.query_one("#targets-table", DataTable)
        rows = t.ordered_rows
        if not rows or t.cursor_row >= len(rows):
            self.query_one("#form-msg", Label).update("[yellow]Click a row to select it first[/yellow]")
            return
        idx = t.cursor_row
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
            removed = targets.pop(idx)
            self._save_targets(targets)
            self.query_one("#form-msg", Label).update(
                f"[green]Removed {removed.get('type', '?')} ✓[/green]"
            )
            self._set_form_mode_new()
        except Exception as e:
            self.query_one("#form-msg", Label).update(f"[red]Error: {e}[/red]")

    @on(Button.Pressed, "#btn-clear")
    def _clear_all(self) -> None:
        self._save_targets([])
        self.query_one("#form-msg", Label).update("[green]Buy list cleared ✓[/green]")
        self._set_form_mode_new()

    def action_refresh(self) -> None:
        self._refresh()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SpaceTradersApp(App):
    CSS_PATH = "dashboard.tcss"
    TITLE    = "SpaceTraders Live"
    SUB_TITLE = f"System: {SYSTEM}"

    SCREENS = {
        "dashboard": DashboardScreen,
        "fleet":     FleetScreen,
        "contracts": ContractsScreen,
        "markets":   MarketsScreen,
        "universe":  UniverseScreen,
        "surveys":   SurveysScreen,
        "analytics": AnalyticsScreen,
        "map":       MapScreen,
        "settings":  SettingsScreen,
    }

    BINDINGS = [
        Binding("1", "switch_screen('dashboard')", "1:Dashboard"),
        Binding("2", "switch_screen('fleet')",     "2:Fleet"),
        Binding("3", "switch_screen('contracts')", "3:Contracts"),
        Binding("4", "switch_screen('markets')",   "4:Markets"),
        Binding("5", "switch_screen('universe')",  "5:Universe"),
        Binding("6", "switch_screen('surveys')",   "6:Surveys"),
        Binding("7", "switch_screen('analytics')", "7:Analytics"),
        Binding("8", "switch_screen('map')",       "8:Map"),
        Binding("9", "switch_screen('settings')",  "9:Settings"),
        Binding("q", "quit",                        "Quit"),
    ]

    def on_mount(self) -> None:
        db.init_db()
        self.push_screen("dashboard")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        if idx + 1 < len(sys.argv):
            try:
                POLL_INTERVAL = float(sys.argv[idx + 1])
            except ValueError:
                pass
    SpaceTradersApp().run()
