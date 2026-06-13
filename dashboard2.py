#!/usr/bin/env python3
"""
dashboard2.py — Comprehensive SpaceTraders Mission Control TUI.

Screens:
  1  Mission Control  — live logs + full fleet panel + contracts + stats
  2  Fleet            — detailed fleet table with ship modal
  3  Contracts        — active & available contracts
  4  Analytics        — transactions, yields, income chart, trade runs
  5  Markets          — market list + prices + arbitrage
  6  Universe         — all waypoints with sourcing analysis
  7  Surveys          — active survey pool
  8  Map              — visual system map with ship positions
  9  Settings         — auto-buy controls

Usage:
    python3 dashboard2.py
    python3 dashboard2.py --interval 3
"""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header, Footer, DataTable, Label, Static,
    TabbedContent, TabPane, Button, Input, RichLog, Select,
)
from textual.containers import Container, Horizontal, Vertical
from textual import work, on, events
from rich.text import Text

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

import db
import agent as agent_api
import contracts as contracts_api
import fleet as fleet_api
import universe as universe_api
from client import SpaceTradersError

try:
    from play import SYSTEM
except Exception:
    try:
        with db._conn() as _c:
            _row = _c.execute(
                "SELECT system_symbol FROM waypoints WHERE system_symbol IS NOT NULL LIMIT 1"
            ).fetchone()
        SYSTEM = _row[0] if _row else "X1-GK27"
    except Exception:
        SYSTEM = "X1-GK27"

POLL_INTERVAL: float = 2.0
LOG_LINES: int = 120   # lines kept in the live log panel

# ---------------------------------------------------------------------------
# Shared helpers (copied/refined from dashboard.py)
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
            return "[red]EXPIRED[/red]"
        if hours < 24:
            return f"[yellow]{hours}h left[/yellow]"
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
        hrs, m  = divmod(mins, 60)
        if hrs:
            return f"{hrs}h {m}m"
        return f"{mins}m {s}s" if mins else f"{s}s"
    except Exception:
        return "—"


def _fill_bar(current: int, capacity: int, width: int = 10) -> Text:
    if capacity == 0:
        return Text("—", style="dim")
    pct    = current / capacity
    filled = int(pct * width)
    bar    = "█" * filled + "░" * (width - filled)
    color  = "green" if pct < 0.6 else "yellow" if pct < 0.85 else "red"
    return Text(f"{bar} {current}/{capacity}", style=color)


def _condition_bar(value: float, width: int = 8) -> Text:
    filled = int(value * width)
    bar    = "█" * filled + "░" * (width - filled)
    pct    = int(value * 100)
    color  = "green" if value > 0.8 else "yellow" if value > 0.5 else "red"
    return Text(f"{bar} {pct}%", style=color)


def _supply_text(supply: str | None) -> Text:
    colors = {
        "ABUNDANT": "green", "HIGH": "green",
        "MODERATE": "yellow",
        "LIMITED": "red",    "SCARCE": "red",
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


def _flight_mode_text(mode: str, status: str) -> Text:
    if status != "IN_TRANSIT":
        return Text("—", style="dim")
    colors = {"DRIFT": "red", "BURN": "bright_red", "CRUISE": "cyan", "STEALTH": "magenta"}
    icons  = {"DRIFT": "💨", "BURN": "🔥", "CRUISE": "→", "STEALTH": "🌑"}
    return Text(f"{icons.get(mode,'?')} {mode}", style=colors.get(mode, "white"))


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
    nav      = ship.get("nav", {})
    status   = nav.get("status", "")
    cooldown = ship.get("cooldown", {}).get("remainingSeconds", 0)
    mounts   = [m.get("symbol", "") for m in ship.get("mounts", [])]
    if status == "IN_TRANSIT":
        dest = nav.get("route", {}).get("destination", {}).get("symbol", "?")
        eta  = _eta_str(nav.get("route", {}).get("arrival"))
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


def _system_from_wp(waypoint: str) -> str:
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else SYSTEM


def _ship_role_label(ship: dict) -> str:
    """Return a display label like 'TYLERDEVRUN-8  [siphoner]' for use in dropdowns."""
    sym = ship.get("symbol", "?")
    mounts = [m.get("symbol", "") for m in ship.get("mounts", [])]
    frame_sym = ship.get("frame", {}).get("symbol", "")
    if any("GAS_SIPHON" in m for m in mounts):
        role = "siphoner"
    elif any("MINING_LASER" in m for m in mounts):
        role = "miner"
    elif any("SURVEYING" in m for m in mounts):
        role = "surveyor"
    elif "LIGHT_HAULER" in frame_sym or "HEAVY_FREIGHTER" in frame_sym:
        role = "hauler"
    elif "COMMAND" in frame_sym:
        role = "command"
    else:
        role = frame_sym.removeprefix("FRAME_").replace("_", " ").title().lower()
    return f"{sym}  [{role}]"


def _calc_cph() -> tuple[int, int]:
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


# ---------------------------------------------------------------------------
# Map helpers
# ---------------------------------------------------------------------------

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
        gy = int((y_max - y) / y_range * map_h) + pad
        return max(0, min(width - 1, gx)), max(0, min(height - 1, gy))

    wp_coords = {w["symbol"]: (w["x"], w["y"]) for w in waypoints}
    cells: dict[tuple[int, int], tuple[str, str]] = {}
    for wp in waypoints:
        gx, gy = to_grid(wp["x"], wp["y"])
        cells[(gx, gy)] = _wp_map_icon(wp["type"])

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
                icon, style = ship_icons.get(syms[0], ("✦", "bold bright_yellow"))
                label = icon if len(syms) == 1 else f"{icon}{len(syms)}"
                output.append(label, style=style)
            elif key in cells:
                char, style = cells[key]
                output.append(char, style=style)
            else:
                output.append("·", style="#0d1520")
        if row < height - 1:
            output.append("\n")
    return output


# ---------------------------------------------------------------------------
# Ship type constants
# ---------------------------------------------------------------------------
_BUYABLE_SHIP_TYPES = [
    "SHIP_ORE_HOUND",
    "SHIP_MINING_DRONE",
    "SHIP_SIPHON_DRONE",
    "SHIP_SURVEYOR",
    "SHIP_LIGHT_HAULER",
    "SHIP_HEAVY_FREIGHTER",
]

# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ShipDetailModal2(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, ship: dict) -> None:
        super().__init__()
        self._ship = ship

    def _is_queued_for_scrap(self) -> bool:
        raw = db.get_bot_setting("pending_scrap", "[]")
        try:
            return self._ship.get("symbol", "") in __import__("json").loads(raw)
        except Exception:
            return False

    def _get_groups(self) -> list[dict]:
        try:
            return json.loads(db.get_bot_setting("ship_groups", "[]"))
        except Exception:
            return []

    def _get_my_group_info(self) -> tuple[str | None, str | None]:
        """Returns (role, hauler_or_None) where role is 'hauler', 'worker', or None."""
        sym = self._ship.get("symbol", "")
        for grp in self._get_groups():
            if grp.get("hauler") == sym:
                return "hauler", None
            if sym in grp.get("workers", []):
                return "worker", grp.get("hauler")
        return None, None

    def compose(self) -> ComposeResult:
        sym = self._ship.get("symbol", "?")
        queued = self._is_queued_for_scrap()
        scrap_label = "✅ Cancel Scrap" if queued else "🗑  Queue for Scrap"
        scrap_variant = "warning" if queued else "error"
        with Container(id="modal-box2"):
            yield Label(f" {sym} — Ship Detail ", classes="modal-title")
            with TabbedContent():
                with TabPane("Overview"):
                    yield Static(id="ship-overview2")
                with TabPane("Cargo"):
                    yield DataTable(id="cargo-table2", show_cursor=False, zebra_stripes=True)
                with TabPane("Mounts & Modules"):
                    yield DataTable(id="mounts-table2", show_cursor=False, zebra_stripes=True)
                with TabPane("Group"):
                    yield Static(id="ship-group-info2")
                    yield Label("[dim]To change group assignment, use Fleet → Groups tab.[/dim]", classes="dim")
            with Horizontal(id="ship-modal-actions"):
                yield Button(scrap_label, id="scrap-btn2", variant=scrap_variant)
                yield Button("Close  [Esc]", id="close-btn2", variant="default")

    def on_mount(self) -> None:
        ship    = self._ship
        nav     = ship.get("nav", {})
        cargo   = ship.get("cargo", {})
        fuel    = ship.get("fuel", {})
        frame   = ship.get("frame", {})
        reactor = ship.get("reactor", {})
        engine  = ship.get("engine", {})
        crew    = ship.get("crew", {})
        cd      = ship.get("cooldown", {}).get("remainingSeconds", 0)
        route   = nav.get("route", {})
        dep_sym = route.get("departure", {}).get("symbol", "—")
        dst_sym = route.get("destination", {}).get("symbol", "—")
        arr     = _eta_str(route.get("arrival"))

        lines = [
            "[bold cyan]── Navigation ──[/bold cyan]",
            f"  Status:     {nav.get('status','?')}  |  Mode: {nav.get('flightMode','?')}",
            f"  Location:   {nav.get('waypointSymbol','?')}",
        ]
        if nav.get("status") == "IN_TRANSIT":
            lines.append(f"  From → To:  {dep_sym}  →  {dst_sym}  (ETA: {arr})")
        lines += [
            f"  Cooldown:   {cd}s" if cd > 0 else "  Cooldown:   —",
            "",
            f"[bold cyan]── Cargo [{cargo.get('units',0)}/{cargo.get('capacity',0)}] ──[/bold cyan]",
            "",
            f"[bold cyan]── Fuel ──[/bold cyan]",
            f"  {_fill_bar(fuel.get('current',0), fuel.get('capacity',0), 16).plain}",
            "",
            f"[bold cyan]── Crew ──[/bold cyan]",
            f"  {crew.get('current',0)}/{crew.get('capacity',0)}  Morale: {crew.get('morale',0)}%",
            "",
            "[bold cyan]── Components ──[/bold cyan]",
            f"  Frame:    {frame.get('name','?'):30s}  {_condition_bar(frame.get('condition',1)).plain}",
            f"  Reactor:  {reactor.get('name','?'):30s}  {_condition_bar(reactor.get('condition',1)).plain}",
            f"  Engine:   {engine.get('name','?'):30s}  {_condition_bar(engine.get('condition',1)).plain}",
            f"  Speed:    {engine.get('speed','?')}",
        ]
        self.query_one("#ship-overview2", Static).update("\n".join(lines))

        ct = self.query_one("#cargo-table2", DataTable)
        ct.add_columns("Good", "Units", "Name")
        inventory = cargo.get("inventory", [])
        for item in inventory:
            ct.add_row(Text(item["symbol"], style="bold green"), str(item["units"]), item.get("name", ""))
        if not inventory:
            ct.add_row(Text("Empty", style="dim"), "", "")

        mt = self.query_one("#mounts-table2", DataTable)
        mt.add_columns("Type", "Name", "Strength", "Deposits")
        for m in ship.get("mounts", []):
            deps = ", ".join(d if isinstance(d, str) else d.get("symbol", "") for d in m.get("deposits", []))
            mt.add_row(Text(m.get("symbol","?"), style="cyan"), m.get("name","?"), str(m.get("strength","—")), deps or "—")
        for mod in ship.get("modules", []):
            mt.add_row(Text(mod.get("symbol","?"), style="yellow"), mod.get("name","?"), "—", "module")
        if not ship.get("mounts") and not ship.get("modules"):
            mt.add_row(Text("None", style="dim"), "", "", "")

        # ── Group tab ──
        self._refresh_group_tab()

    def _refresh_group_tab(self) -> None:
        sym    = self._ship.get("symbol", "")
        groups = self._get_groups()
        role, hauler = self._get_my_group_info()
        lines: list[str] = ["[bold cyan]── Current Group Assignment ──[/bold cyan]", ""]
        if role == "hauler":
            grp = next((g for g in groups if g.get("hauler") == sym), None)
            workers = grp.get("workers", []) if grp else []
            gtype   = grp.get("type", "?") if grp else "?"
            gname   = grp.get("name", "") if grp else ""
            lines += [
                f"  Group:    [bold]{gname}[/bold]" if gname else "",
                f"  Role:     [green]Hauler[/green]",
                f"  Type:     [cyan]{gtype}[/cyan]",
                f"  Workers:  {', '.join(workers) if workers else '[dim]none[/dim]'}",
            ]
        elif role == "worker":
            grp = next((g for g in groups if sym in g.get("workers", [])), None)
            gname = grp.get("name", "") if grp else ""
            gtype = grp.get("type", "?") if grp else "?"
            lines += [
                f"  Group:    [bold]{gname}[/bold]" if gname else "",
                f"  Role:     [yellow]Worker[/yellow]",
                f"  Type:     [cyan]{gtype}[/cyan]",
                f"  Hauler:   [green]{hauler}[/green]",
            ]
        else:
            lines += ["  [dim]Not assigned to any group[/dim]"]
        lines += ["", "[bold cyan]── All Groups ──[/bold cyan]", ""]
        if groups:
            for g in groups:
                gname = g.get("name", "")
                icon  = "⛽" if g.get("type") == "siphon" else "⛏"
                lines.append(
                    f"  {icon} [bold]{gname}[/bold]  "
                    f"[green]{g.get('hauler','—')}[/green] ← {', '.join(g.get('workers', []))}"
                )
        else:
            lines.append("  [dim]No groups configured[/dim]")
        self.query_one("#ship-group-info2", Static).update("\n".join(l for l in lines if l != ""))

    @on(Button.Pressed, "#scrap-btn2")
    def _toggle_scrap(self) -> None:
        import json as _json
        sym = self._ship.get("symbol", "")
        raw = db.get_bot_setting("pending_scrap", "[]")
        try:
            queue: list[str] = _json.loads(raw)
        except Exception:
            queue = []
        if sym in queue:
            queue.remove(sym)
            label, variant = "🗑  Queue for Scrap", "error"
            self.notify(f"{sym} removed from scrap queue", severity="information")
        else:
            queue.append(sym)
            label, variant = "✅ Cancel Scrap", "warning"
            self.notify(f"{sym} queued for scrap — will be scrapped after next contract", severity="warning")
        db.set_bot_setting("pending_scrap", _json.dumps(queue))
        btn = self.query_one("#scrap-btn2", Button)
        btn.label = label
        btn.variant = variant

    @on(Button.Pressed, "#close-btn2")
    def _close(self) -> None:
        self.dismiss()


class ContractDetailModal2(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, contract: dict) -> None:
        super().__init__()
        self._contract = contract

    def compose(self) -> ComposeResult:
        cid = self._contract.get("id", "?")
        with Container(id="modal-box2"):
            yield Label(f" Contract {cid[:16]}… ", classes="modal-title")
            yield Static(id="contract-detail2")
            yield Button("Close  [Esc]", id="close-btn2", variant="default")

    def on_mount(self) -> None:
        c = self._contract
        status_str = (
            "[green]✅ Fulfilled[/green]" if c.get("fulfilled") else
            "[cyan]🔄 Active[/cyan]"      if c.get("accepted")  else
            "[yellow]📋 Available[/yellow]"
        )
        lines = [
            f"[bold cyan]ID:[/bold cyan]       {c.get('id','?')}",
            f"[bold cyan]Type:[/bold cyan]     {c.get('type','?')}",
            f"[bold cyan]Status:[/bold cyan]   {status_str}",
            f"[bold cyan]Deadline:[/bold cyan] {_deadline_str(c.get('deadline'))}",
            f"[bold cyan]Expiry:[/bold cyan]   {_deadline_str(c.get('expiration'))}",
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
        self.query_one("#contract-detail2", Static).update("\n".join(lines))

    @on(Button.Pressed, "#close-btn2")
    def _close(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Screen 1 — Mission Control
# ---------------------------------------------------------------------------

class MissionControlScreen(Screen):
    """Split layout: left=live logs, right=fleet table, bottom=contracts+stats."""
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("", id="mc-agent-bar")
        with Horizontal(id="mc-main"):
            with Vertical(id="mc-logs-panel"):
                yield Label("  📡 LIVE BOT LOGS ", id="mc-logs-title")
                yield RichLog(id="mc-logs-feed", markup=True, highlight=False, max_lines=150, wrap=False)
            with Vertical(id="mc-fleet-panel"):
                yield Label("  🚀 FLEET STATUS ", id="mc-fleet-title")
                yield DataTable(
                    id="mc-fleet-table",
                    show_cursor=False,
                    zebra_stripes=True,
                )
        with Horizontal(id="mc-bottom"):
            with Vertical(id="mc-contracts-panel"):
                yield Label("  📋 CONTRACTS ", classes="panel-title-green")
                yield Static("", id="mc-contracts-body")
            with Vertical(id="mc-stats-panel"):
                yield Label("  📊 STATS & YIELDS ", classes="panel-title-yellow")
                yield Static("", id="mc-stats-body")
        yield Label("", id="mc-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#mc-fleet-table", DataTable)
        t.add_columns(
            "Ship", "Role", "Status", "From", "→ To",
            "Mode", "Fuel", "Cargo", "ETA / CD",
        )
        self.refresh_data()
        self._poll_logs()
        self.set_interval(POLL_INTERVAL, self.refresh_data)
        self.set_interval(2.0, self._poll_logs)

    @work(thread=True)
    def _poll_logs(self) -> None:
        """Lightweight DB-only log refresh — runs independently of API calls."""
        try:
            with db._conn() as con:
                rows = con.execute(
                    "SELECT timestamp, message FROM bot_logs ORDER BY id DESC LIMIT ?",
                    (LOG_LINES,),
                ).fetchall()
            self.app.call_from_thread(self._update_logs, list(rows))
        except Exception:
            pass

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            agent     = agent_api.get_my_agent()
            ships     = fleet_api.get_my_ships()
            contracts = db.get_active_contracts()
            # If no accepted contract in DB, fall back to the live API and sync
            if not any(c["accepted"] and not c["fulfilled"] for c in contracts):
                try:
                    live = contracts_api.get_contracts()
                    for lc in live:
                        db.upsert_contract(lc)
                    contracts = db.get_active_contracts()
                except Exception:
                    pass
            cph_1h, cph_10m = _calc_cph()
            now = time.time()
            with db._conn() as con:
                yields = con.execute(
                    "SELECT trade_symbol, SUM(units) FROM extraction_yields "
                    "WHERE timestamp > ? GROUP BY trade_symbol ORDER BY SUM(units) DESC",
                    (now - 1200,),
                ).fetchall()
                available_rows = con.execute(
                    """SELECT c.id, c.type, c.on_accepted, c.on_fulfilled,
                              c.expiration, cd.trade_symbol, cd.units_required
                       FROM contracts c
                       LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                       WHERE c.accepted = 0 AND c.fulfilled = 0
                       ORDER BY c.on_fulfilled DESC LIMIT 5"""
                ).fetchall()
            self.app.call_from_thread(
                self._update, agent, ships, contracts, cph_1h, cph_10m,
                list(yields), list(available_rows),
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#mc-status", Label).update,
                f"[red]Fetch error: {e}[/red]",
            )

    def _update_logs(self, rows: list) -> None:
        log_widget = self.query_one("#mc-logs-feed", RichLog)
        log_widget.clear()
        if not rows:
            log_widget.write("[dim]No log entries yet — start play.py[/dim]")
            return
        for ts_f, msg in reversed(rows):
            dt_str = datetime.fromtimestamp(ts_f).strftime("%H:%M:%S")
            log_widget.write(f"[dim]{dt_str}[/dim] {msg}")

    def _update(self, agent, ships, contracts, cph_1h, cph_10m,
                yields, available_rows) -> None:
        credits  = agent.get("credits", 0)
        symbol   = agent.get("symbol", "?")
        ship_cnt = agent.get("shipCount", 0)

        # Agent bar
        cph_color = "green" if cph_1h >= 0 else "red"
        sign = "+" if cph_1h >= 0 else ""
        self.query_one("#mc-agent-bar", Label).update(
            f"[bold cyan]{symbol}[/bold cyan]  "
            f"Credits: [bold green]{credits:>12,} cr[/bold green]  "
            f"Ships: {ship_cnt}  "
            f"CPH(1h): [{cph_color}]{sign}{cph_1h:>9,} cr[/{cph_color}]  "
            f"CPH→hr(10m): [{cph_color}]{'+' if cph_10m>=0 else ''}{cph_10m*6:>9,} cr[/{cph_color}]  "
            f"[dim]System: {SYSTEM}[/dim]"
        )

        # Fleet table
        t = self.query_one("#mc-fleet-table", DataTable)
        t.clear()
        for ship in ships:
            sym   = ship.get("symbol", "?")
            nav   = ship.get("nav", {})
            cargo = ship.get("cargo", {})
            fuel  = ship.get("fuel", {})
            cd    = ship.get("cooldown", {}).get("remainingSeconds", 0)
            route = nav.get("route", {})
            status_str = nav.get("status", "?")
            mode_str   = nav.get("flightMode", "CRUISE")
            loc        = nav.get("waypointSymbol", "?")
            dep_sym    = route.get("departure",   {}).get("symbol", loc) if status_str == "IN_TRANSIT" else "—"
            dst_sym    = route.get("destination", {}).get("symbol", "—") if status_str == "IN_TRANSIT" else "—"
            eta_str    = _eta_str(route.get("arrival")) if status_str == "IN_TRANSIT" else ("—" if cd == 0 else f"cd:{cd}s")

            # Derive role from mounts
            mounts = [m.get("symbol","") for m in ship.get("mounts",[])]
            if any("SURVEYING" in m for m in mounts):
                role = Text("surveyor", style="magenta")
            elif any("MINING_LASER" in m for m in mounts):
                role = Text("miner", style="green")
            else:
                role = Text("hauler/cmd", style="yellow")

            short = sym.split("-")[-1] if "-" in sym else sym
            t.add_row(
                Text(f"{_ship_icon(ship)} …-{short}", style="bold cyan"),
                role,
                _nav_status_text(status_str),
                Text(dep_sym.split("-")[-1] if dep_sym != "—" else "—", style="dim"),
                Text(dst_sym.split("-")[-1] if dst_sym != "—" else loc.split("-")[-1], style="yellow"),
                _flight_mode_text(mode_str, status_str),
                _fill_bar(fuel.get("current",0), fuel.get("capacity",0), 8),
                _fill_bar(cargo.get("units",0), cargo.get("capacity",0), 8),
                Text(eta_str, style="cyan" if status_str == "IN_TRANSIT" else "dim"),
                key=sym,
            )

        # Contracts panel
        c_lines: list[str] = []
        if contracts:
            c_lines.append("[bold green]— Active Contracts —[/bold green]")
            for c in contracts:
                for d in c.get("deliver", []):
                    req  = d["units_required"]
                    fulf = d["units_fulfilled"]
                    pct  = int(100 * fulf / max(1, req))
                    done = "█" * (pct // 5) + "░" * (20 - pct // 5)
                    color = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
                    c_lines.append(
                        f"  [{color}]{d['trade_symbol']:20s}[/{color}]  "
                        f"{done} {fulf:,}/{req:,} ({pct}%)"
                    )
                    c_lines.append(
                        f"    → {d.get('destination_symbol','?')}  "
                        f"Reward: [green]{(c.get('on_fulfilled') or 0):,} cr[/green]  "
                        f"Deadline: {_deadline_str(c.get('deadline'))}"
                    )
        else:
            c_lines.append("  [dim]No active contracts — negotiate one[/dim]")

        if available_rows:
            c_lines.append("")
            c_lines.append("[bold yellow]— Available Contracts —[/bold yellow]")
            seen: set[str] = set()
            for cid, ctype, on_acc, on_ful, exp, good, qty in available_rows:
                if cid in seen:
                    continue
                seen.add(cid)
                c_lines.append(
                    f"  [yellow]{ctype}[/yellow]  {good or '?'} x{qty or '?'}  "
                    f"→ [green]{(on_ful or 0):,} cr[/green]  "
                    f"[dim]exp: {_deadline_str(exp)}[/dim]"
                )

        self.query_one("#mc-contracts-body", Static).update("\n".join(c_lines))

        # Stats panel
        stats_lines: list[str] = [
            "[bold yellow]— Credits Flow —[/bold yellow]",
            f"  CPH (1h):   [{'green' if cph_1h>=0 else 'red'}]{'+' if cph_1h>=0 else ''}{cph_1h:,} cr[/{'green' if cph_1h>=0 else 'red'}]",
            f"  CPH (10m→): [{'green' if cph_10m>=0 else 'red'}]{'+' if cph_10m>=0 else ''}{cph_10m*6:,} cr[/{'green' if cph_10m>=0 else 'red'}]",
            "",
            "[bold yellow]— Mining Yields (20m) —[/bold yellow]",
        ]
        if yields:
            for good, units in yields:
                bar_len = min(12, max(1, units // 2))
                bar = "▓" * bar_len
                stats_lines.append(f"  [green]{good:18s}[/green] {bar} {units}u")
        else:
            stats_lines.append("  [dim]No yields yet[/dim]")
        self.query_one("#mc-stats-body", Static).update("\n".join(stats_lines))

        self.query_one("#mc-status", Label).update(
            f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            f"Auto-refresh {int(POLL_INTERVAL)}s  •  R to force  •  "
            f"Keys 1-9 to switch screens[/dim]"
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 2 — Fleet (Ships tab + Groups tab)
# ---------------------------------------------------------------------------

class FleetScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Fleet  •  ↑↓ navigate  •  Enter / click → ship detail", classes="screen-hint")
        with TabbedContent(id="fleet-tabs"):
            with TabPane("Ships", id="tab-ships"):
                yield DataTable(id="fleet2-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Groups", id="tab-groups"):
                with Horizontal(id="groups-layout"):
                    # Left: group list
                    with Vertical(id="groups-list-panel"):
                        yield Label("  Groups ", classes="panel-title")
                        yield DataTable(id="groups-table", cursor_type="row", zebra_stripes=True)
                        with Horizontal(id="group-list-btns"):
                            yield Button("+ New Group", id="btn-new-group", variant="success")
                            yield Button("Delete Group", id="btn-delete-group", variant="error")
                    # Right: group editor
                    with Vertical(id="groups-editor-panel"):
                        yield Label("  Group Editor ", classes="panel-title", id="group-editor-title")
                        yield Static("Select a group or create a new one.", id="group-editor-body")
                        with Vertical(id="group-form", classes="hidden"):
                            with Horizontal(classes="form-row"):
                                yield Label("Name:", classes="form-label")
                                yield Input(placeholder="e.g. Gas Giant Team", id="group-name-input")
                            with Horizontal(classes="form-row"):
                                yield Label("Type:", classes="form-label")
                                yield Select(
                                    options=[("⛽ Siphon (gas giant)", "siphon"), ("⛏ Miner (asteroid)", "miner")],
                                    id="group-type-select2",
                                    value="siphon",
                                )
                            yield Label("  ── Hauler ──", classes="dim")
                            with Horizontal(id="hauler-assign-row"):
                                yield Select(options=[("— none —", "__none__")], id="hauler-ship-select")
                                yield Button("Set Hauler", id="btn-set-hauler2", variant="primary")
                            yield Label("  ── Workers ──", classes="dim")
                            with Horizontal(id="worker-assign-row"):
                                yield Select(options=[("— none —", "__none__")], id="worker-ship-select")
                                yield Button("Add Worker", id="btn-add-worker2", variant="primary")
                                yield Button("Remove Worker", id="btn-remove-worker2", variant="warning")
                            yield Label("", id="group-form-msg")
                            with Horizontal(id="group-save-row"):
                                yield Button("Save Group", id="btn-save-group", variant="success")
        yield Label("", id="fleet2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._ships_map: dict[str, dict] = {}
        self._all_ships: list[dict] = []
        self._editing_group_idx: int | None = None  # index into _groups
        self._groups: list[dict] = []
        self._draft_group: dict = {}  # working copy while editing

        t = self.query_one("#fleet2-table", DataTable)
        t.add_columns(
            "Ship", "Frame", "Status", "Location",
            "From", "→ Destination", "Mode",
            "Fuel", "Cargo", "ETA", "Cooldown",
        )
        gt = self.query_one("#groups-table", DataTable)
        gt.add_columns("Name", "Type", "Hauler", "Workers")

        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    # ── Data refresh ──────────────────────────────────────────────────────

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            ships = fleet_api.get_my_ships()
            agent = agent_api.get_my_agent()
            self.app.call_from_thread(self._update_table, ships, agent)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#fleet2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_table(self, ships: list[dict], agent: dict) -> None:
        self._all_ships = ships
        t = self.query_one("#fleet2-table", DataTable)
        t.clear()
        self._ships_map = {}
        for ship in ships:
            sym   = ship.get("symbol", "?")
            self._ships_map[sym] = ship
            nav   = ship.get("nav", {})
            cargo = ship.get("cargo", {})
            fuel  = ship.get("fuel", {})
            cd    = ship.get("cooldown", {}).get("remainingSeconds", 0)
            frame_sym  = ship.get("frame", {}).get("symbol", "")
            ship_type  = frame_sym.removeprefix("FRAME_").replace("_", " ").title()
            route      = nav.get("route", {})
            status_str = nav.get("status", "?")
            mode_str   = nav.get("flightMode", "CRUISE")
            dep_sym    = route.get("departure",   {}).get("symbol", "—") if status_str == "IN_TRANSIT" else "—"
            dst_sym    = route.get("destination", {}).get("symbol", "—") if status_str == "IN_TRANSIT" else "—"
            eta_str    = _eta_str(route.get("arrival")) if status_str == "IN_TRANSIT" else "—"
            t.add_row(
                Text(f"{_ship_icon(ship)} {sym}", style="bold cyan"),
                Text(ship_type, style="magenta"),
                _nav_status_text(status_str),
                Text(nav.get("waypointSymbol", "?"), style="yellow"),
                Text(dep_sym, style="dim"),
                Text(dst_sym, style="yellow"),
                _flight_mode_text(mode_str, status_str),
                _fill_bar(fuel.get("current", 0), fuel.get("capacity", 0)),
                _fill_bar(cargo.get("units",  0), cargo.get("capacity", 0)),
                Text(eta_str, style="cyan"),
                Text(f"{cd}s", style="yellow") if cd > 0 else Text("—", style="dim"),
                key=sym,
            )
        credits = agent.get("credits", 0)
        cph_1h, _ = _calc_cph()
        cph_color = "green" if cph_1h >= 0 else "red"
        sign = "+" if cph_1h >= 0 else ""
        self.query_one("#fleet2-status", Label).update(
            f"Credits: [bold green]{credits:,} cr[/bold green]  "
            f"CPH(1h): [{cph_color}]{sign}{cph_1h:,}[/{cph_color}]  "
            f"[dim]{len(ships)} ships  •  Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )
        self._reload_groups_table()

    # ── Ships tab ─────────────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#fleet2-table")
    def _on_ship_selected(self, event: DataTable.RowSelected) -> None:
        sym  = str(event.row_key.value)
        ship = self._ships_map.get(sym)
        if ship:
            self.app.push_screen(ShipDetailModal2(ship))

    # ── Groups tab helpers ────────────────────────────────────────────────

    def _load_groups(self) -> list[dict]:
        try:
            return json.loads(db.get_bot_setting("ship_groups", "[]"))
        except Exception:
            return []

    def _save_groups(self, groups: list[dict]) -> None:
        db.set_bot_setting("ship_groups", json.dumps(groups))

    def _reload_groups_table(self) -> None:
        self._groups = self._load_groups()
        gt = self.query_one("#groups-table", DataTable)
        gt.clear()
        for g in self._groups:
            name    = g.get("name", "—")
            gtype   = g.get("type", "?")
            hauler  = g.get("hauler", "—") or "—"
            workers = ", ".join(g.get("workers", [])) or "—"
            type_icon = "⛽" if gtype == "siphon" else "⛏"
            gt.add_row(
                Text(name, style="bold"),
                Text(f"{type_icon} {gtype}", style="cyan"),
                Text(hauler, style="green"),
                Text(workers, style="yellow"),
                key=str(self._groups.index(g)),
            )

    def _populate_ship_selects(self) -> None:
        """Fill hauler and worker Select widgets with labeled ship options."""
        opts = [("— none —", "__none__")] + [
            (_ship_role_label(s), s["symbol"])
            for s in self._all_ships
        ]
        try:
            self.query_one("#hauler-ship-select", Select).set_options(opts)
            self.query_one("#worker-ship-select", Select).set_options(opts)
        except Exception:
            pass

    def _open_editor(self, group: dict, idx: int | None) -> None:
        """Show the group form populated with group data."""
        import copy
        self._draft_group = copy.deepcopy(group)
        self._editing_group_idx = idx
        self.query_one("#group-form").remove_class("hidden")
        self.query_one("#group-editor-body", Static).update("")
        title = "New Group" if idx is None else f"Editing: {group.get('name','—')}"
        self.query_one("#group-editor-title", Label).update(f"  {title} ")
        self.query_one("#group-name-input", Input).value = group.get("name", "")
        # type select
        sel_type = self.query_one("#group-type-select2", Select)
        sel_type.value = group.get("type", "siphon")
        # hauler select
        self._populate_ship_selects()
        hauler_sel = self.query_one("#hauler-ship-select", Select)
        hauler_sel.value = group.get("hauler") or "__none__"
        self._refresh_editor_body()

    def _refresh_editor_body(self) -> None:
        g = self._draft_group
        workers = g.get("workers", [])
        hauler  = g.get("hauler") or "none"
        lines = [
            f"[bold cyan]Hauler:[/bold cyan]  [green]{hauler}[/green]",
            f"[bold cyan]Workers:[/bold cyan] {', '.join(workers) if workers else '[dim]none[/dim]'}",
        ]
        self.query_one("#group-editor-body", Static).update("\n".join(lines))
        self.query_one("#group-form-msg", Label).update("")

    # ── Groups tab events ─────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#groups-table")
    def _on_group_selected(self, event: DataTable.RowSelected) -> None:
        idx = int(str(event.row_key.value))
        if 0 <= idx < len(self._groups):
            self._open_editor(self._groups[idx], idx)

    @on(Button.Pressed, "#btn-new-group")
    def _new_group(self) -> None:
        self._open_editor({"name": "", "type": "siphon", "hauler": None, "workers": []}, None)

    @on(Button.Pressed, "#btn-delete-group")
    def _delete_group(self) -> None:
        gt = self.query_one("#groups-table", DataTable)
        if gt.cursor_row is None:
            return
        try:
            row_key = gt.get_row_at(gt.cursor_row)
        except Exception:
            return
        idx = gt.cursor_row
        if 0 <= idx < len(self._groups):
            self._groups.pop(idx)
            self._save_groups(self._groups)
            self._reload_groups_table()
            self.query_one("#group-form").add_class("hidden")
            self.query_one("#group-editor-body", Static).update("Group deleted.")
            self._editing_group_idx = None

    @on(Button.Pressed, "#btn-set-hauler2")
    def _set_hauler(self) -> None:
        sel = self.query_one("#hauler-ship-select", Select)
        val = sel.value
        if val == "__none__" or not val:
            self._draft_group["hauler"] = None
        else:
            self._draft_group["hauler"] = str(val)
        self._refresh_editor_body()

    @on(Button.Pressed, "#btn-add-worker2")
    def _add_worker(self) -> None:
        sel = self.query_one("#worker-ship-select", Select)
        val = sel.value
        msg = self.query_one("#group-form-msg", Label)
        if val == "__none__" or not val:
            msg.update("[red]Select a ship first[/red]")
            return
        sym = str(val)
        workers = self._draft_group.setdefault("workers", [])
        if sym == self._draft_group.get("hauler"):
            msg.update("[red]That ship is the hauler — can't also be a worker[/red]")
            return
        if sym not in workers:
            workers.append(sym)
        self._refresh_editor_body()

    @on(Button.Pressed, "#btn-remove-worker2")
    def _remove_worker(self) -> None:
        sel = self.query_one("#worker-ship-select", Select)
        val = sel.value
        msg = self.query_one("#group-form-msg", Label)
        if val == "__none__" or not val:
            msg.update("[red]Select a ship to remove[/red]")
            return
        sym = str(val)
        workers = self._draft_group.get("workers", [])
        if sym in workers:
            workers.remove(sym)
            self._refresh_editor_body()
        else:
            msg.update(f"[yellow]{sym} is not a worker in this group[/yellow]")

    @on(Button.Pressed, "#btn-save-group")
    def _save_group(self) -> None:
        name_input = self.query_one("#group-name-input", Input)
        sel_type   = self.query_one("#group-type-select2", Select)
        msg        = self.query_one("#group-form-msg", Label)

        name  = name_input.value.strip()
        gtype = str(sel_type.value) if sel_type.value != Select.BLANK else "siphon"

        if not name:
            msg.update("[red]Name is required[/red]")
            return

        self._draft_group["name"]  = name
        self._draft_group["type"]  = gtype
        if not self._draft_group.get("workers"):
            self._draft_group["workers"] = []

        groups = self._load_groups()
        if self._editing_group_idx is None:
            groups.append(self._draft_group)
        else:
            if 0 <= self._editing_group_idx < len(groups):
                groups[self._editing_group_idx] = self._draft_group
            else:
                groups.append(self._draft_group)

        self._save_groups(groups)
        self._reload_groups_table()
        msg.update(f"[green]✓ Saved '{name}'[/green]")
        self._editing_group_idx = groups.index(self._draft_group) if self._draft_group in groups else None

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 3 — Contracts
# ---------------------------------------------------------------------------

class ContractsScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Contracts  •  Active & available  •  Enter / click → detail", classes="screen-hint")
        with Vertical(id="contracts2-main"):
            with Vertical(id="active-contracts-panel"):
                yield Label("  ✅ Active Contracts ", classes="panel-title-green")
                yield Static("", id="active-contracts-body")
            with Vertical(id="available-contracts-panel"):
                yield Label("  📋 Available / Pre-negotiated Contracts ", classes="panel-title-yellow")
                yield DataTable(
                    id="available-contracts-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
        yield Label("", id="contracts2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._contracts_map: dict[str, dict] = {}
        t = self.query_one("#available-contracts-table", DataTable)
        t.add_columns("ID", "Type", "Good", "Qty", "Advance", "Reward", "Expires", "Status")
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

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
                       ORDER BY c.fulfilled ASC, c.accepted DESC, c.last_updated DESC"""
                ).fetchall()
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
                if r[9]:
                    contracts[cid]["deliver"].append({
                        "trade_symbol":      r[9],
                        "destination_symbol": r[10],
                        "units_required":    r[11],
                        "units_fulfilled":   r[12],
                    })
            # If DB has no active accepted contract, sync from API
            contract_list = list(contracts.values())
            if not any(c["accepted"] and not c["fulfilled"] for c in contract_list):
                try:
                    live = contracts_api.get_contracts()
                    for lc in live:
                        db.upsert_contract(lc)
                    # Re-read after sync
                    with db._conn() as con:
                        rows2 = con.execute(
                            """SELECT c.id, c.faction_symbol, c.type, c.accepted, c.fulfilled,
                                      c.expiration, c.deadline, c.on_accepted, c.on_fulfilled,
                                      cd.trade_symbol, cd.destination_symbol,
                                      cd.units_required, cd.units_fulfilled
                               FROM contracts c
                               LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                               ORDER BY c.fulfilled ASC, c.accepted DESC, c.last_updated DESC"""
                        ).fetchall()
                    contracts = {}
                    for r in rows2:
                        cid = r[0]
                        if cid not in contracts:
                            contracts[cid] = {
                                "id": cid, "faction_symbol": r[1], "type": r[2],
                                "accepted": bool(r[3]), "fulfilled": bool(r[4]),
                                "expiration": r[5], "deadline": r[6],
                                "on_accepted": r[7], "on_fulfilled": r[8],
                                "deliver": [],
                            }
                        if r[9]:
                            contracts[cid]["deliver"].append({
                                "trade_symbol":      r[9],
                                "destination_symbol": r[10],
                                "units_required":    r[11],
                                "units_fulfilled":   r[12],
                            })
                    contract_list = list(contracts.values())
                except Exception:
                    pass
            self.app.call_from_thread(self._update, contract_list)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#contracts2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update(self, contracts: list[dict]) -> None:
        self._contracts_map = {c["id"]: c for c in contracts}
        active    = [c for c in contracts if c["accepted"] and not c["fulfilled"]]
        available = [c for c in contracts if not c["accepted"] and not c["fulfilled"]]
        done      = [c for c in contracts if c["fulfilled"]]

        # Active contracts — rich text with progress bars
        lines: list[str] = []
        for c in active:
            lines.append(
                f"[bold cyan]{c['type']}[/bold cyan]  "
                f"ID: [dim]{c['id'][:16]}…[/dim]  "
                f"Deadline: {_deadline_str(c.get('deadline'))}"
            )
            for d in c.get("deliver", []):
                req  = d["units_required"]
                fulf = d["units_fulfilled"]
                pct  = int(100 * fulf / max(1, req))
                done_bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                color    = "green" if pct >= 80 else "yellow" if pct >= 40 else "red"
                lines.append(
                    f"  [{color}]{d['trade_symbol']:22s}[/{color}]  "
                    f"{done_bar} [bold]{fulf:,}/{req:,}[/bold] ({pct}%)"
                )
                lines.append(
                    f"    → {d.get('destination_symbol','?')}  "
                    f"Reward: [green]{(c.get('on_fulfilled') or 0):,} cr[/green]"
                )
            lines.append("")
        for c in done[:3]:
            for d in c.get("deliver", []):
                lines.append(
                    f"  [dim green]✅ {d['trade_symbol']} {d['units_fulfilled']}/{d['units_required']} "
                    f"— {(c.get('on_fulfilled') or 0):,} cr  (fulfilled)[/dim green]"
                )
        if not active and not done:
            lines.append("[dim]No active contracts[/dim]")

        self.query_one("#active-contracts-body", Static).update("\n".join(lines))

        # Available table
        t = self.query_one("#available-contracts-table", DataTable)
        t.clear()
        for c in available:
            cid = c["id"]
            for i, d in enumerate(c.get("deliver", [{"trade_symbol": "—", "destination_symbol": "—",
                                                       "units_required": 0, "units_fulfilled": 0}])):
                row_key = f"{cid}|{i}"
                t.add_row(
                    Text(cid[:12] + "…", style="dim"),
                    c["type"],
                    Text(d["trade_symbol"], style="bold yellow"),
                    f"{d['units_required']:,}",
                    f"{(c.get('on_accepted') or 0):,} cr",
                    Text(f"{(c.get('on_fulfilled') or 0):,} cr", style="bold green"),
                    _deadline_str(c.get("expiration")),
                    Text("📋 AVAIL", style="yellow"),
                    key=row_key,
                )
        if not available:
            t.add_row(Text("No available contracts — negotiate one via play.py", style="dim"),
                      "", "", "", "", "", "", "")

        self.query_one("#contracts2-status", Label).update(
            f"[dim]{len(active)} active  •  {len(available)} available  •  "
            f"{len(done)} fulfilled  •  Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    @on(DataTable.RowSelected, "#available-contracts-table")
    def _on_contract_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        cid = key.split("|")[0]
        contract = self._contracts_map.get(cid)
        if contract:
            self.app.push_screen(ContractDetailModal2(contract))

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 4 — Analytics
# ---------------------------------------------------------------------------

class AnalyticsScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Analytics  •  Transactions, yields, income, trade runs", classes="screen-hint")
        with TabbedContent():
            with TabPane("Transactions"):
                with Horizontal(id="txn2-filters"):
                    yield Button("All",       id="btn2-all",  variant="primary")
                    yield Button("Sales",     id="btn2-sell", variant="default")
                    yield Button("Purchases", id="btn2-buy",  variant="default")
                    yield Label("", id="txn2-summary")
                yield DataTable(id="txn2-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Yields"):
                with Horizontal(id="yields2-filters"):
                    yield Button("Last 20m", id="btn2-20m",      variant="primary")
                    yield Button("Last 1hr", id="btn2-1h",       variant="default")
                    yield Button("All Time", id="btn2-all-time", variant="default")
                yield DataTable(id="yields2-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Income Chart"):
                yield Static("", id="income2-breakdown", classes="info-panel")
            with TabPane("Trade Runs"):
                yield DataTable(id="runs2-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="analytics2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._all_txns:    list = []
        self._yields_20m:  list = []
        self._yields_1h:   list = []
        self._yields_all:  list = []
        self._txn_filter   = "ALL"
        self._yield_window = 1200

        tt = self.query_one("#txn2-table", DataTable)
        tt.add_columns("Time", "Type", "Good", "Units", "Price/u", "Total", "Waypoint", "Ship")

        yt = self.query_one("#yields2-table", DataTable)
        yt.add_columns("Good", "Total Units", "Extractions", "Avg/Extract", "Surveyed %")

        rt = self.query_one("#runs2-table", DataTable)
        rt.add_columns("Time", "Ship", "Good", "Units", "Buy Cost", "Sell Rev", "Profit", "ROI %")

        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            now = time.time()
            with db._conn() as con:
                txns = con.execute(
                    "SELECT timestamp, type, trade_symbol, units, price_per_unit, "
                    "total_price, waypoint_symbol, ship_symbol "
                    "FROM market_transactions ORDER BY timestamp DESC LIMIT 300"
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

                trade_runs = con.execute(
                    """SELECT
                           trip_id,
                           ship_symbol,
                           trade_symbol,
                           SUM(CASE WHEN type='PURCHASE' THEN units       ELSE 0 END),
                           SUM(CASE WHEN type='PURCHASE' THEN total_price ELSE 0 END),
                           SUM(CASE WHEN type='SELL'     THEN total_price ELSE 0 END),
                           MIN(CASE WHEN type='PURCHASE' THEN timestamp   END)
                       FROM market_transactions
                       WHERE trip_id IS NOT NULL
                       GROUP BY trip_id
                       ORDER BY COALESCE(
                           MAX(CASE WHEN type='SELL' THEN timestamp END),
                           MIN(timestamp)
                       ) DESC
                       LIMIT 100"""
                ).fetchall()

            self.app.call_from_thread(
                self._update, list(txns), list(y_20m), list(y_1h), list(y_all),
                income, list(trade_runs),
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#analytics2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update(self, txns, y_20m, y_1h, y_all, income, trade_runs) -> None:
        self._all_txns   = txns
        self._yields_20m = y_20m
        self._yields_1h  = y_1h
        self._yields_all = y_all
        self._income     = income
        self._trade_runs = trade_runs
        self._update_txn_table()
        self._update_yields_table()
        self._update_income()
        self._update_trade_runs()
        self.query_one("#analytics2-status", Label).update(
            f"[dim]{len(txns)} transactions  •  Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    def _update_txn_table(self) -> None:
        tt = self.query_one("#txn2-table", DataTable)
        tt.clear()
        f = self._txn_filter
        sell_total = buy_total = 0
        for ts, ttype, good, units, ppu, total, wp, ship in self._all_txns:
            if f == "SELL"    and ttype != "SELL":     continue
            if f == "BUY"     and ttype != "PURCHASE": continue
            color = "green" if ttype == "SELL" else "red"
            sign  = "+" if ttype == "SELL" else "-"
            if ttype == "SELL":     sell_total += total
            else:                   buy_total  += total
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
        nc  = "green" if net >= 0 else "red"
        self.query_one("#txn2-summary", Label).update(
            f"  [green]+{sell_total:,}[/green] sales  "
            f"[red]-{buy_total:,}[/red] buys  "
            f"Net: [{nc}]{'+' if net>=0 else ''}{net:,}[/{nc}] cr"
        )

    def _update_yields_table(self) -> None:
        yt = self.query_one("#yields2-table", DataTable)
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
        income  = getattr(self, "_income", [])
        if not income:
            return
        max_val = max((abs(v) for _, v in income), default=1)
        lines   = ["[bold cyan]Hourly Net Income — last 12 hours[/bold cyan]", ""]
        for i, net in income:
            label   = f"{i}–{i+1}h ago"
            bar_len = int(abs(net) / max(max_val, 1) * 30)
            bar     = "▓" * bar_len
            if net >= 0:
                lines.append(f"  [dim]{label:12s}[/dim] [green]{bar:<30s}  +{net:>10,} cr[/green]")
            else:
                lines.append(f"  [dim]{label:12s}[/dim] [red]{bar:<30s}   {net:>10,} cr[/red]")
        lines += [
            "",
            f"[bold cyan]Total (12h):[/bold cyan] [{'green' if sum(v for _,v in income)>=0 else 'red'}]"
            f"{sum(v for _,v in income):+,} cr[/]",
        ]
        self.query_one("#income2-breakdown", Static).update("\n".join(lines))

    def _update_trade_runs(self) -> None:
        rt = self.query_one("#runs2-table", DataTable)
        rt.clear()
        for row in self._trade_runs:
            tid, ship, good, units, buy_cost, sell_rev, ts_buy = row
            profit = (sell_rev or 0) - (buy_cost or 0)
            roi    = int(100 * profit / max(1, buy_cost or 1))
            dt     = datetime.fromtimestamp(ts_buy).strftime("%m/%d %H:%M") if ts_buy else "—"
            pc     = "green" if profit > 0 else "red"
            rt.add_row(
                Text(dt, style="dim"),
                Text(ship or "—", style="dim"),
                Text(good, style="bold"),
                f"{int(units or 0):,}",
                f"{int(buy_cost or 0):,}",
                f"{int(sell_rev or 0):,}",
                Text(f"{'+' if profit>=0 else ''}{profit:,}", style=pc),
                Text(f"{'+' if roi>=0 else ''}{roi}%", style=pc),
            )
        if not self._trade_runs:
            rt.add_row("No trade run data yet", "Haulers + trip_id tracking populates this",
                       "", "", "", "", "", "")

    # ── Button handlers for filters ──────────────────────────────────────────

    @on(Button.Pressed, "#btn2-all")
    def _filter_all(self)  -> None:
        self._txn_filter = "ALL";  self._update_txn_table()
        self.query_one("#btn2-all",  Button).variant = "primary"
        self.query_one("#btn2-sell", Button).variant = "default"
        self.query_one("#btn2-buy",  Button).variant = "default"

    @on(Button.Pressed, "#btn2-sell")
    def _filter_sell(self) -> None:
        self._txn_filter = "SELL"; self._update_txn_table()
        self.query_one("#btn2-all",  Button).variant = "default"
        self.query_one("#btn2-sell", Button).variant = "primary"
        self.query_one("#btn2-buy",  Button).variant = "default"

    @on(Button.Pressed, "#btn2-buy")
    def _filter_buy(self)  -> None:
        self._txn_filter = "BUY";  self._update_txn_table()
        self.query_one("#btn2-all",  Button).variant = "default"
        self.query_one("#btn2-sell", Button).variant = "default"
        self.query_one("#btn2-buy",  Button).variant = "primary"

    @on(Button.Pressed, "#btn2-20m")
    def _yields_20m(self) -> None:
        self._yield_window = 1200;  self._update_yields_table()
        self.query_one("#btn2-20m",      Button).variant = "primary"
        self.query_one("#btn2-1h",       Button).variant = "default"
        self.query_one("#btn2-all-time", Button).variant = "default"

    @on(Button.Pressed, "#btn2-1h")
    def _yields_1h(self) -> None:
        self._yield_window = 3600;  self._update_yields_table()
        self.query_one("#btn2-20m",      Button).variant = "default"
        self.query_one("#btn2-1h",       Button).variant = "primary"
        self.query_one("#btn2-all-time", Button).variant = "default"

    @on(Button.Pressed, "#btn2-all-time")
    def _yields_all_time(self) -> None:
        self._yield_window = 0;  self._update_yields_table()
        self.query_one("#btn2-20m",      Button).variant = "default"
        self.query_one("#btn2-1h",       Button).variant = "default"
        self.query_one("#btn2-all-time", Button).variant = "primary"

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 5 — Markets
# ---------------------------------------------------------------------------

class MarketsScreen2(Screen):
    BINDINGS = [
        ("r", "refresh_data",   "Refresh"),
        ("f", "fetch_listings", "Fetch Listings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Markets  •  Click market → prices  •  F / button → Refresh from API", classes="screen-hint")
        with Horizontal(id="markets2-main"):
            with Vertical(id="market2-list-panel"):
                yield Label("Markets", classes="panel-title")
                yield DataTable(id="markets2-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="market2-detail-panel"):
                yield Label("Select a market", id="market2-header", classes="panel-title")
                with TabbedContent():
                    with TabPane("Prices"):
                        yield DataTable(id="prices2-table", cursor_type="row", zebra_stripes=True)
                    with TabPane("Arbitrage"):
                        yield DataTable(id="arb2-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="market2-actions"):
                    yield Button("Refresh Listings  [F]", id="refresh2-btn", variant="primary")
                    yield Label("", id="refresh2-status")
        yield Label("", id="markets2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._selected_market: str = ""
        mt = self.query_one("#markets2-table", DataTable)
        mt.add_columns("Waypoint", "Goods", "Prices", "Exports (top)", "Updated")
        pt = self.query_one("#prices2-table", DataTable)
        pt.add_columns("Good", "Type", "Supply", "Activity", "Buy", "Sell", "Volume", "Age")
        at = self.query_one("#arb2-table", DataTable)
        at.add_columns("Good", "Buy At", "Buy Price", "Sell At", "Sell Price", "Margin", "ROI%", "Age")
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            with db._conn() as con:
                rows = con.execute(
                    """SELECT ml.waypoint_symbol,
                              COUNT(DISTINCT ml.trade_symbol)             AS good_count,
                              COUNT(DISTINCT mp.trade_symbol)             AS price_count,
                              MAX(ml.last_updated)                        AS updated,
                              GROUP_CONCAT(
                                  CASE WHEN ml.listing_type='EXPORT' THEN ml.trade_symbol END,
                                  ', '
                              )                                           AS exports
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
                self.query_one("#markets2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_markets(self, rows: list, arb: list[dict]) -> None:
        mt = self.query_one("#markets2-table", DataTable)
        mt.clear()
        for wp, good_count, price_count, updated, exports in rows:
            has_prices = Text("✅", "green") if price_count > 0 else Text("⚠", "yellow")
            # Trim export list to 3 items
            export_str = exports or "—"
            exports_trunc = ", ".join(export_str.split(", ")[:3]) + ("…" if export_str.count(",") >= 3 else "")
            mt.add_row(
                Text(wp, style="cyan"),
                str(good_count),
                has_prices,
                Text(exports_trunc, style="green"),
                _ts_ago(updated),
                key=wp,
            )
        at = self.query_one("#arb2-table", DataTable)
        at.clear()
        for o in arb:
            at.add_row(
                Text(o["trade_symbol"], style="bold"),
                Text(o["buy_at"],  style="cyan"),
                f"{o['buy_price']:,}",
                Text(o["sell_at"], style="green"),
                f"{o['sell_price']:,}",
                Text(f"+{o['margin']:,}", style="bold green"),
                f"{o['pct_margin']}%",
                _ts_ago(o["oldest_data"]),
            )
        if not arb:
            at.add_row("No opportunities found", "Need ship price data", "", "", "", "", "", "")
        self.query_one("#markets2-status", Label).update(
            f"[dim]{len(rows)} markets  •  {len(arb)} arbitrage opportunities  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    @on(DataTable.RowSelected, "#markets2-table")
    def _on_market_selected(self, event: DataTable.RowSelected) -> None:
        wp = str(event.row_key.value)
        self._selected_market = wp
        self.query_one("#market2-header", Label).update(f"  {wp}  ")
        self._load_prices(wp)

    @work(thread=True)
    def _load_prices(self, waypoint: str) -> None:
        prices = db.get_market_prices_for_waypoint(waypoint)
        self.app.call_from_thread(self._update_prices, prices)

    def _update_prices(self, prices: list[dict]) -> None:
        pt = self.query_one("#prices2-table", DataTable)
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

    @on(Button.Pressed, "#refresh2-btn")
    def _on_refresh_btn(self) -> None:
        self.action_fetch_listings()

    def action_fetch_listings(self) -> None:
        if not self._selected_market:
            self.query_one("#refresh2-status", Label).update("[yellow]Select a market first[/yellow]")
            return
        self._fetch_from_api(self._selected_market)

    @work(thread=True)
    def _fetch_from_api(self, waypoint: str) -> None:
        self.app.call_from_thread(
            self.query_one("#refresh2-status", Label).update,
            f"[yellow]Fetching {waypoint}…[/yellow]",
        )
        try:
            system = _system_from_wp(waypoint)
            data   = universe_api.get_market(system, waypoint)
            db.upsert_market_listings(waypoint, data)
            tg  = data.get("tradeGoods", [])
            if tg:
                db.upsert_market_prices(waypoint, tg)
                msg = f"[green]✅ Updated listings + {len(tg)} live prices[/green]"
            else:
                msg = "[green]✅ Listings updated  [dim](prices need ship docked)[/dim][/green]"
            prices = db.get_market_prices_for_waypoint(waypoint)
            self.app.call_from_thread(self._update_prices, prices)
            self.app.call_from_thread(
                self.query_one("#refresh2-status", Label).update, msg
            )
        except SpaceTradersError as e:
            self.app.call_from_thread(
                self.query_one("#refresh2-status", Label).update,
                f"[red]API error: {e}[/red]",
            )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 6 — Universe
# ---------------------------------------------------------------------------

class UniverseScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Universe  •  Type to filter  •  Click row → sourcing analysis", classes="screen-hint")
        with Horizontal(id="universe2-filter-bar"):
            yield Input(
                placeholder="Filter by type or trait  (e.g. ASTEROID, MARKETPLACE, SHIPYARD)…",
                id="universe2-filter",
            )
        with Horizontal(id="universe2-main"):
            with Vertical(id="universe2-list-panel"):
                yield DataTable(id="universe2-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="universe2-detail-panel"):
                yield Static("", id="universe2-analysis")
        yield Label("", id="universe2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._waypoints: list[dict] = []
        t = self.query_one("#universe2-table", DataTable)
        t.add_columns("Waypoint", "Type", "Coords", "Traits")
        self.refresh_data()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            wps = db.get_all_waypoints(SYSTEM)
            self.app.call_from_thread(self._loaded, wps)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#universe2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _loaded(self, wps: list[dict]) -> None:
        self._waypoints = wps
        self._apply_filter(self.query_one("#universe2-filter", Input).value)
        self.query_one("#universe2-status", Label).update(
            f"[dim]{len(wps)} waypoints in {SYSTEM}[/dim]"
        )

    def _apply_filter(self, text: str) -> None:
        t = self.query_one("#universe2-table", DataTable)
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

    @on(Input.Changed, "#universe2-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(DataTable.RowSelected, "#universe2-table")
    def _on_wp_selected(self, event: DataTable.RowSelected) -> None:
        sym = str(event.row_key.value)
        self._show_analysis(sym)

    def _show_analysis(self, symbol: str) -> None:
        wp = next((w for w in self._waypoints if w["symbol"] == symbol), None)
        if not wp:
            return
        traits = [t["symbol"] for t in wp.get("traits", [])]
        lines  = [
            f"[bold cyan]╔══ {symbol} ══╗[/bold cyan]",
            f"  Type:    {wp['type']}",
            f"  Coords:  ({wp['x']}, {wp['y']})",
            f"  Traits:  {', '.join(traits) or '—'}",
            "",
        ]
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
                lines.append(f"    [green]{good}[/green]  [dim](via {', '.join(from_traits)})[/dim]")
        else:
            lines.append("[dim]No mining deposits at this waypoint[/dim]")

        lines.append("")
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
                    lines.append(f"  {label}: [{color}]{', '.join(items[:10])}[/{color}]")
            prices = db.get_market_prices_for_waypoint(symbol)
            if prices:
                lines.append(f"  [dim]{len(prices)} live prices cached[/dim]")
            else:
                lines.append("  [dim]No live prices — dock a ship to record them[/dim]")

        lines.append("")
        arb = db.get_arbitrage_opportunities(SYSTEM, min_margin=50)
        relevant = [o for o in arb if symbol in (o["buy_at"], o["sell_at"])]
        if relevant:
            lines.append("[bold yellow]💰  Arbitrage involving this market:[/bold yellow]")
            for o in relevant[:5]:
                direction = "BUY" if o["buy_at"] == symbol else "SELL"
                lines.append(
                    f"  [{'cyan' if direction=='BUY' else 'green'}]{direction}[/] "
                    f"{o['trade_symbol']:22s}  +{o['margin']:,} cr/u  ({o['pct_margin']}% ROI)"
                )
        self.query_one("#universe2-analysis", Static).update("\n".join(lines))

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 7 — Surveys
# ---------------------------------------------------------------------------

class SurveysScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Active Surveys  •  Shared survey pool — surveyors populate this", classes="screen-hint")
        yield DataTable(id="surveys2-table", cursor_type="row", zebra_stripes=True)
        yield Label("", id="surveys2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#surveys2-table", DataTable)
        t.add_columns("Signature", "Waypoint", "Deposits", "L", "M", "S", "Expires", "Age")
        self.refresh_data()
        self.set_interval(15, self.refresh_data)

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            surveys = db.load_active_surveys()
            self.app.call_from_thread(self._update_table, surveys)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#surveys2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update_table(self, surveys: list[dict]) -> None:
        t = self.query_one("#surveys2-table", DataTable)
        t.clear()
        now = time.time()
        for sv in surveys:
            sig  = sv.get("signature", "?")
            wp   = sv.get("symbol", "?")
            deps = sv.get("deposits", [])
            goods = ", ".join(sorted(set(d["symbol"] for d in deps)))
            L = sum(1 for d in deps if d.get("size") == "LARGE")
            M = sum(1 for d in deps if d.get("size") == "MODERATE")
            S = sum(1 for d in deps if d.get("size") == "SMALL")
            created_at = sv.get("created_at") or now
            t.add_row(
                Text(sig[:20] + "…", style="dim"),
                Text(wp, style="cyan"),
                Text(goods or "—"),
                Text(str(L) if L else "—", style="green"  if L else "dim"),
                Text(str(M) if M else "—", style="yellow" if M else "dim"),
                Text(str(S) if S else "—", style="white"  if S else "dim"),
                _deadline_str(sv.get("expiration")),
                _ts_ago(created_at),
            )
        if not surveys:
            t.add_row("No active surveys", "Surveyor ships populate this", "", "", "", "", "", "")
        self.query_one("#surveys2-status", Label).update(
            f"[dim]{len(surveys)} active surveys  •  Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 8 — Map
# ---------------------------------------------------------------------------

class MapScreen2(Screen):
    BINDINGS = [("r", "refresh_data", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(f"Map  •  System: {SYSTEM}  •  Ships shown with unique icons", classes="screen-hint")
        with Horizontal(id="map2-main"):
            yield Static("", id="map2-canvas")
            with Vertical(id="map2-side"):
                yield Label("Legend", classes="panel-title")
                yield Static("", id="map2-legend")
                yield Label("Ships", classes="panel-title")
                yield Static("", id="map2-ships")
        yield Label("", id="map2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._waypoints: list[dict] = []
        self._ships:     list[dict] = []
        self._ship_icons: dict[str, tuple[str, str]] = {}
        self.refresh_data()
        self.set_interval(POLL_INTERVAL, self.refresh_data)

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            wps   = db.get_all_waypoints(SYSTEM)
            ships = fleet_api.get_my_ships()
            self.app.call_from_thread(self._update, wps, ships)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#map2-status", Label).update,
                f"[red]Error: {e}[/red]",
            )

    def _update(self, wps: list[dict], ships: list[dict]) -> None:
        self._waypoints  = wps
        self._ships      = ships
        self._ship_icons = {
            ship["symbol"]: _SHIP_MAP_PALETTE[i % len(_SHIP_MAP_PALETTE)]
            for i, ship in enumerate(ships)
        }
        # Defer until after layout so canvas.size is known
        self.call_after_refresh(self._draw_map)

    def _draw_map(self) -> None:
        canvas = self.query_one("#map2-canvas", Static)
        size   = canvas.size
        w, h   = size.width - 2, size.height - 2
        if w < 10 or h < 5:
            # Layout not ready yet — retry after next refresh
            self.call_after_refresh(self._draw_map)
            return
        canvas.update(_render_map(self._waypoints, self._ships, w, h, self._ship_icons))

        # Legend — waypoint type icons
        legend_lines: list[str] = []
        for wp_type, (icon, style) in _WP_MAP_ICONS.items():
            short = wp_type.replace("_", " ").title()
            legend_lines.append(f"  [{style}]{icon}[/{style}]  {short}")
        self.query_one("#map2-legend", Static).update("\n".join(legend_lines))

        # Ships list
        ship_lines: list[str] = []
        for ship in self._ships:
            sym  = ship.get("symbol", "?")
            nav  = ship.get("nav", {})
            icon, style = self._ship_icons.get(sym, ("✦", "bold bright_yellow"))
            pos_str = nav.get("waypointSymbol", "?")
            if nav.get("status") == "IN_TRANSIT":
                dst = nav.get("route", {}).get("destination", {}).get("symbol", "?")
                eta = _eta_str(nav.get("route", {}).get("arrival"))
                pos_str = f"→ {dst} ({eta})"
            fuel  = ship.get("fuel", {})
            cargo = ship.get("cargo", {})
            ship_lines += [
                f"  [{style}]{icon}[/{style}] [cyan]{sym}[/cyan]",
                f"    {pos_str}",
                f"    ⛽ {fuel.get('current',0)}/{fuel.get('capacity',1)}  "
                f"📦 {cargo.get('units',0)}/{cargo.get('capacity',0)}",
            ]
        self.query_one("#map2-ships", Static).update("\n".join(ship_lines))
        self.query_one("#map2-status", Label).update(
            f"[dim]{len(self._waypoints)} waypoints  •  {len(self._ships)} ships  •  "
            f"Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

    def on_show(self) -> None:
        """Re-render map when this screen becomes visible."""
        if self._waypoints:
            self.call_after_refresh(self._draw_map)

    def on_resize(self, _event: events.Resize) -> None:
        self.call_after_refresh(self._draw_map)

    def action_refresh_data(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# Screen 9 — Settings
# ---------------------------------------------------------------------------

class SettingsScreen2(Screen):
    BINDINGS = [("r", "action_refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Settings  •  Fleet auto-buy configuration", classes="screen-hint")
        with Horizontal(id="settings2-main"):
            with Vertical(id="settings2-controls"):
                yield Label("Bot Controls", classes="panel-title")
                yield Static("", id="settings2-info")
                yield Button("Toggle Auto-Buy", id="toggle2-auto-buy", variant="primary")
                yield Button("Toggle Auto-Group", id="toggle2-auto-group", variant="default")
                yield Label("", id="settings2-cmd-label", classes="panel-title")
                yield Button("✓  Idle",   id="btn2-cmd-idle",   variant="success")
                yield Button("Hauler",    id="btn2-cmd-hauler", variant="default")
                yield Label("Discord Notifications", classes="panel-title")
                yield Input(
                    placeholder="https://discord.com/api/webhooks/...",
                    id="discord2-webhook",
                    password=False,
                )
                with Horizontal(id="discord2-interval-row"):
                    yield Input(
                        placeholder="Status interval (min, default 5)",
                        id="discord2-interval",
                        restrict="0123456789",
                    )
                    yield Button("Save Discord", id="btn2-discord-save", variant="primary")
                yield Label("", id="discord2-msg")
            with Vertical(id="settings2-targets"):
                yield Label("Ship Buy Targets", classes="panel-title")
                yield DataTable(id="targets2-table", zebra_stripes=True)
                with Horizontal(id="settings2-add"):
                    yield Input(placeholder="Type (e.g. SHIP_ORE_HOUND)", id="add2-type")
                    yield Input(placeholder="Max", id="add2-max", restrict="0123456789")
                    yield Button("Add / Update", id="btn2-add", variant="success")
                    yield Button("Remove Row",   id="btn2-remove", variant="error")
                    yield Label("", id="add2-msg")
        yield Label("", id="settings2-status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#targets2-table", DataTable)
        t.add_columns("Ship Type", "Max", "Role")
        # Pre-fill Discord fields from DB
        wh = db.get_bot_setting("discord_webhook", "")
        if wh:
            self.query_one("#discord2-webhook", Input).value = wh
        iv = db.get_bot_setting("discord_status_interval", "300")
        self.query_one("#discord2-interval", Input).value = str(int(iv) // 60)
        self._refresh()
        self.set_interval(5, self._refresh)

    def _refresh(self) -> None:
        raw     = db.get_bot_setting("ship_buy_list", "[]")
        enabled = db.get_bot_setting("auto_buy_ships", "true").lower() == "true"
        auto_group = db.get_bot_setting("auto_group_ships", "0") == "1"
        try:
            targets = json.loads(raw)
        except Exception:
            targets = []

        t = self.query_one("#targets2-table", DataTable)
        t.clear()
        for i, tgt in enumerate(targets):
            stype = tgt.get("type", "?")
            role  = {
                "SHIP_ORE_HOUND":       "miner",
                "SHIP_MINING_DRONE":    "miner",
                "SHIP_SIPHON_DRONE":    "siphoner",
                "SHIP_SURVEYOR":        "surveyor",
                "SHIP_LIGHT_HAULER":    "hauler",
                "SHIP_HEAVY_FREIGHTER": "hauler",
            }.get(stype, "?")
            t.add_row(
                Text(stype,  style="cyan"),
                str(tgt.get("max", "?")),
                Text(role,   style="yellow"),
                key=str(i),
            )

        auto_buy_str   = "[green]ENABLED ✓[/green]"  if enabled    else "[red]DISABLED ✗[/red]"
        auto_group_str = "[green]ENABLED ✓[/green]"  if auto_group else "[yellow]OFF (manual groups)[/yellow]"
        self.query_one("#settings2-info", Static).update(
            f"  Auto-buy: {auto_buy_str}\n"
            f"  Auto-group: {auto_group_str}\n"
            f"  Ship targets: {len(targets)}\n"
            f"  [dim]Team fill: 5 workers + 1 hauler/team  •  max 2 siphon / 2 miner teams[/dim]\n"
            f"  [dim]Edit targets below, then restart play.py to apply[/dim]"
        )
        self.query_one("#settings2-status", Label).update(
            f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            + ("[green]Auto-buy ON[/green]" if enabled else "[red]Auto-buy OFF[/red]")
            + "  •  "
            + ("[green]Auto-group ON[/green]" if auto_group else "[yellow]Auto-group OFF[/yellow]")
            + f"  •  {len(targets)} target(s)[/dim]"
        )
        cmd_role = db.get_bot_setting("command_ship_role", "idle")
        self.query_one("#settings2-cmd-label", Label).update(
            f"  Command Ship Role: [bold]{cmd_role.upper()}[/bold]"
        )
        idle_btn   = self.query_one("#btn2-cmd-idle",   Button)
        hauler_btn = self.query_one("#btn2-cmd-hauler", Button)
        if cmd_role == "hauler":
            idle_btn.label   = "Idle";       idle_btn.variant   = "default"
            hauler_btn.label = "✓  Hauler";  hauler_btn.variant = "success"
        else:
            idle_btn.label   = "✓  Idle";    idle_btn.variant   = "success"
            hauler_btn.label = "Hauler";     hauler_btn.variant = "default"
        # Reflect auto-group toggle state in button variant
        ag_btn = self.query_one("#toggle2-auto-group", Button)
        ag_btn.variant = "success" if auto_group else "default"
        ag_btn.label   = "✓ Auto-Group ON" if auto_group else "Auto-Group (manual)"

    @on(Button.Pressed, "#btn2-cmd-idle")
    def _set_cmd_idle(self)   -> None:
        db.set_bot_setting("command_ship_role", "idle");   self._refresh()

    @on(Button.Pressed, "#btn2-cmd-hauler")
    def _set_cmd_hauler(self) -> None:
        db.set_bot_setting("command_ship_role", "hauler"); self._refresh()

    @on(Button.Pressed, "#toggle2-auto-buy")
    def _toggle_auto_buy(self) -> None:
        current = db.get_bot_setting("auto_buy_ships", "true").lower() == "true"
        db.set_bot_setting("auto_buy_ships", "false" if current else "true")
        self._refresh()

    @on(Button.Pressed, "#toggle2-auto-group")
    def _toggle_auto_group(self) -> None:
        current = db.get_bot_setting("auto_group_ships", "0") == "1"
        db.set_bot_setting("auto_group_ships", "0" if current else "1")
        self._refresh()

    @on(Button.Pressed, "#btn2-discord-save")
    def _save_discord(self) -> None:
        wh  = self.query_one("#discord2-webhook", Input).value.strip()
        iv  = self.query_one("#discord2-interval", Input).value.strip()
        msg = self.query_one("#discord2-msg", Label)
        if wh and not wh.startswith("https://discord.com/api/webhooks/"):
            msg.update("[red]Invalid webhook URL[/red]")
            return
        db.set_bot_setting("discord_webhook", wh)
        try:
            secs = max(60, int(iv) * 60) if iv else 300
        except ValueError:
            secs = 300
        db.set_bot_setting("discord_status_interval", str(secs))
        status = "[green]Saved ✓[/green]" if wh else "[yellow]Cleared (notifications disabled)[/yellow]"
        msg.update(status)
        self.notify("Discord settings saved", severity="information")

    @on(Button.Pressed, "#btn2-add")
    def _add_ship(self) -> None:
        stype   = self.query_one("#add2-type", Input).value.strip().upper()
        max_str = self.query_one("#add2-max",  Input).value.strip()
        msg_lbl = self.query_one("#add2-msg",  Label)
        if stype not in _BUYABLE_SHIP_TYPES:
            msg_lbl.update(f"[red]Unknown. Valid: {', '.join(_BUYABLE_SHIP_TYPES)}[/red]")
            return
        try:
            max_count = int(max_str)
            if max_count < 1:
                raise ValueError
        except ValueError:
            msg_lbl.update("[red]Max must be ≥ 1[/red]")
            return
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
        except Exception:
            targets = []
        existing = next((t for t in targets if t["type"] == stype), None)
        if existing:
            existing["max"] = max_count
            msg_lbl.update("[green]Updated ✓[/green]")
        else:
            targets.append({"type": stype, "max": max_count})
            msg_lbl.update("[green]Added ✓[/green]")
        db.set_bot_setting("ship_buy_list", json.dumps(targets))
        self.query_one("#add2-type", Input).value = ""
        self.query_one("#add2-max",  Input).value = ""
        self._refresh()

    @on(Button.Pressed, "#btn2-remove")
    def _remove_ship(self) -> None:
        t   = self.query_one("#targets2-table", DataTable)
        key = t.cursor_row_key
        if key is None:
            self.query_one("#add2-msg", Label).update("[yellow]Select a row first[/yellow]")
            return
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
            targets.pop(int(str(key.value)))
            db.set_bot_setting("ship_buy_list", json.dumps(targets))
            self.query_one("#add2-msg", Label).update("[green]Removed ✓[/green]")
        except Exception:
            pass
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SpaceTradersApp2(App):
    CSS_PATH  = "dashboard2.tcss"
    TITLE     = "SpaceTraders Mission Control"
    SUB_TITLE = f"System: {SYSTEM}"

    SCREENS = {
        "mission":   MissionControlScreen,
        "fleet":     FleetScreen2,
        "contracts": ContractsScreen2,
        "analytics": AnalyticsScreen2,
        "markets":   MarketsScreen2,
        "universe":  UniverseScreen2,
        "surveys":   SurveysScreen2,
        "map":       MapScreen2,
        "settings":  SettingsScreen2,
    }

    BINDINGS = [
        Binding("1", "switch_screen('mission')",   "1:Mission"),
        Binding("2", "switch_screen('fleet')",     "2:Fleet"),
        Binding("3", "switch_screen('contracts')", "3:Contracts"),
        Binding("4", "switch_screen('analytics')", "4:Analytics"),
        Binding("5", "switch_screen('markets')",   "5:Markets"),
        Binding("6", "switch_screen('universe')",  "6:Universe"),
        Binding("7", "switch_screen('surveys')",   "7:Surveys"),
        Binding("8", "switch_screen('map')",       "8:Map"),
        Binding("9", "switch_screen('settings')",  "9:Settings"),
        Binding("q", "quit",                       "Quit"),
    ]

    def on_mount(self) -> None:
        db.init_db()
        self.push_screen("mission")
        self.set_interval(5.0, self._refresh_credits)

    @work(thread=True)
    def _refresh_credits(self) -> None:
        try:
            agent = agent_api.get_my_agent()
            credits = agent.get("credits", 0)
            self.app.call_from_thread(
                setattr, self.app, "sub_title",
                f"System: {SYSTEM}  |  Credits: {credits:,} cr"
            )
        except Exception:
            pass


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
    SpaceTradersApp2().run()
