"""
orchestrator.py — Manages the full fleet: spin up role tasks, monitor contract
progress, issue commands, handle fleet expansion.

Architecture
------------
All ship roles run as independent asyncio.Task objects (no threads).
The orchestrator:
 1. Runs startup (config, markets, warm-start DB).
 2. Selects a contract via the Strategy.
 3. Assigns each ship to a role coroutine.
 4. Monitors contract progress; when done, triggers a new contract cycle.
 5. Periodically calls FleetManagerRole for maintenance/upgrades/purchases.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from client import SpaceTradersClient, SpaceTradersError
from config import Config
from navigation import Navigator
from market import MarketIntelligence
from surveys import SurveyPool
from strategy import Strategy, load_strategy
from roles.base import ContractContext
from roles.miner import MinerRole
from roles.surveyor import SurveyorRole
from roles.hauler import HaulerRole
from roles.trader import TraderRole
from roles.explorer import ExplorerRole
from roles.siphoner import SiphonerRole
from roles.siphon_hauler import SiphonHaulerRole
from roles.miner_hauler import MinerHaulerRole
from roles.fleet_manager import FleetManagerRole
import db
import discord_notify as discord
import groups

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates all ships and manages the game loop."""

    def __init__(self, config: Config | None = None) -> None:
        self._cfg = config or Config()
        self._client: SpaceTradersClient | None = None
        self._nav: Navigator | None = None
        self._market: MarketIntelligence | None = None
        self._surveys = SurveyPool()
        self._strategy: Strategy = load_strategy(self._cfg.strategy_file)
        self._stop: asyncio.Event | None = None  # created inside run() once loop is running
        self._active_tasks: dict[str, asyncio.Task] = {}  # ship_symbol → task
        self._current_ctx: ContractContext | None = None  # for status table

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point. Runs until interrupted."""
        self._stop = asyncio.Event()  # must be created inside the running event loop
        heartbeat = asyncio.create_task(self._heartbeat(), name="heartbeat")
        async with SpaceTradersClient() as client:
            self._client = client
            log.info("[STEP] init_db")
            db.init_db()
            log.info("[STEP] startup")
            await self._startup()
            log.info("Orchestrator ready — entering main loop")
            try:
                await self._main_loop()
            except (KeyboardInterrupt, asyncio.CancelledError):
                discord.send_shutdown("KeyboardInterrupt")
                raise
            except Exception as _e:
                discord.send_shutdown(f"Unhandled exception: {_e}")
                raise
            finally:
                self._stop.set()
                heartbeat.cancel()
                await self._cancel_all_tasks()

    def request_stop(self) -> None:
        """Signal the orchestrator to wind down gracefully."""
        if self._stop:
            self._stop.set()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        """Configure agent, warm-start market intelligence from DB, then live scan."""
        log.info("[STEP] auto_configure")
        await self._cfg.auto_configure(self._client)
        self._nav = Navigator(self._client, self._cfg, None)  # type: ignore[arg-type]
        self._market = MarketIntelligence(self._client, self._cfg, self._nav)
        # Give Navigator a reference to market so it can resolve fuel markets
        self._nav._market = self._market

        log.info("[STEP] warm_start_from_db")
        await self._market.warm_start_from_db()

        log.info("[STEP] discover_markets")
        try:
            await self._market.discover_markets()
        except Exception as e:
            log.warning("Market discovery failed: %s — continuing with DB data", e)

        log.info(
            "Startup complete. System=%s Asteroid=%s Base=%s",
            self._cfg.system, self._cfg.asteroid, self._cfg.asteroid_base,
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat(self) -> None:
        """Logs every 60 s so we can see the event loop is alive."""
        while True:
            try:
                tasks = [t for t in asyncio.all_tasks() if not t.done()]
                names = ", ".join(t.get_name() for t in tasks)
                log.info("[HEARTBEAT] %d tasks running: %s", len(tasks), names)
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------
    # Background market scan
    # ------------------------------------------------------------------

    async def _background_market_scan(self) -> None:
        try:
            await self._market.scan_good_sources()
        except Exception as e:
            log.warning("Background market scan failed: %s", e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Continuously work contracts until stopped."""
        import traceback as _tb
        fm_task: asyncio.Task | None = None
        status_task: asyncio.Task | None = None

        while not self._stop.is_set():
            # ── Background tasks ──────────────────────────────────────
            if status_task is None or status_task.done():
                status_task = asyncio.create_task(
                    self._status_loop(), name="status_printer"
                )
            if fm_task is None or fm_task.done():
                fm_role = FleetManagerRole(
                    ship_symbol=self._cfg.fleet_manager_ship,
                    config=self._cfg,
                    client=self._client,
                    navigator=self._nav,
                    market=self._market,
                    surveys=self._surveys,
                )
                fm_task = asyncio.create_task(fm_role.run(self._stop), name="fleet_manager")

            # ── Contract selection ────────────────────────────────────
            log.info("[STEP] select_contract")
            try:
                contract = await self._strategy.select_contract(
                    self._client, self._cfg, self._nav
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("select_contract failed: %s\n%s", e, _tb.format_exc())
                await asyncio.sleep(60)
                continue

            log.info("[STEP] contract=%s", contract.get('id', 'None') if contract else 'None')
            if not contract:
                log.info("No contract available — waiting 2 min before retry")
                await asyncio.sleep(120)
                continue

            # ── Contract setup ────────────────────────────────────────
            try:
                cid = contract.get("id", "")
                d = (contract.get("terms", {}).get("deliver") or [{}])[0]
                good = d.get("tradeSymbol", "")
                dest = d.get("destinationSymbol", "")
                req = d.get("unitsRequired", 0)
                fulf = d.get("unitsFulfilled", 0)

                try:
                    db.upsert_contract(contract)
                except Exception as e:
                    log.debug("Could not persist contract to DB: %s", e)

                log.info(
                    "Working contract %s: %s × %d (fulfilled %d) → %s",
                    cid, good, req, fulf, dest,
                )
                discord.send_contract_start(contract)

                ctx = ContractContext(
                    contract_id=cid,
                    trade_symbol=good,
                    destination=dest,
                    units_required=req,
                    units_fulfilled=fulf,
                    done=asyncio.Event(),
                    fulfill_lock=asyncio.Lock(),
                )
                self._current_ctx = ctx

                log.info("[STEP] assign_all_ships for contract=%s", cid)
                await self._assign_all_ships(ctx, contract)
                log.info("[STEP] assign_all_ships done, waiting for ctx.done or stop")

                # Wait for contract done OR stop — cancel the loser to avoid task leak
                done_task = asyncio.create_task(ctx.done.wait())
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    [done_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

                log.info("[STEP] wait complete. ctx.done=%s stop=%s", ctx.done.is_set(), self._stop.is_set())
                if ctx.done.is_set():
                    log.info("Contract %s complete — starting new cycle", cid)
                    earned = contract.get("terms", {}).get("payment", {}).get("onFulfilled", 0)
                    discord.send_contract_finish(contract, earned)
                    try:
                        me = await self._client.get("/my/agent")
                        db.record_credits(me.get("credits", 0))
                    except Exception:
                        pass
                    await self._cancel_all_tasks()
                    await asyncio.sleep(10)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Contract cycle error: %s\n%s", e, _tb.format_exc())
                await self._cancel_all_tasks()
                await asyncio.sleep(30)

        for bg in (fm_task, status_task):
            if bg and not bg.done():
                bg.cancel()
                try:
                    await bg
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # Role assignment
    # ------------------------------------------------------------------

    async def _assign_all_ships(self, ctx: ContractContext, contract: dict) -> None:
        """Cancel stale tasks and start fresh role tasks for every ship."""
        log.info("[ASSIGN] cancelling stale tasks")
        await self._cancel_all_tasks()

        log.info("[ASSIGN] fetching ships")
        try:
            ships = await self._client.get_all_pages("/my/ships")
        except SpaceTradersError as e:
            log.error("Could not fetch ships: %s", e)
            return

        log.info("[ASSIGN] got %d ships", len(ships))
        fleet_syms = {s["symbol"] for s in ships}

        # ── Load and validate ship groups ────────────────────────────────────
        raw_groups = await groups.auto_group_ships(self._client, self._cfg)
        active_groups = groups.validate_groups(raw_groups, fleet_syms)
        groups.clear_all_events()

        grouped_haulers: set[str] = set()
        grouped_workers: set[str] = set()
        all_worker_syms: list[str] = []
        for grp in active_groups:
            grouped_haulers.add(grp["hauler"])
            grouped_workers.update(grp["workers"])
            all_worker_syms.extend(grp["workers"])
            log.info("Group '%s': hauler=%s workers=%s", grp["name"], grp["hauler"], grp["workers"])

        groups.init_worker_events(all_worker_syms)

        # ── Direct-buy contract detection ──────────────────────────────────
        # If the contract good can be bought directly from a market, assign
        # roles the same way as v1: one dedicated buyer, rest mine for income.
        # Group-assigned ships are excluded from the free pool.
        contract_good = ""
        if contract:
            deliver = (contract.get("terms", {}).get("deliver") or [{}])[0]
            contract_good = deliver.get("tradeSymbol", "")

        buy_wp = await self._market.best_buy_waypoint(contract_good) if contract_good else None
        is_direct_buy_contract = bool(buy_wp)

        if is_direct_buy_contract:
            free_miners  = [s["symbol"] for s in ships
                            if any("MINING" in m.get("symbol", "") for m in s.get("mounts", []))
                            and s["symbol"] not in grouped_workers
                            and s["symbol"] not in grouped_haulers]
            free_haulers = [s["symbol"] for s in ships
                            if s.get("registration", {}).get("role", "") in ("HAULER", "TRANSPORT")
                            and s["symbol"] != self._cfg.fleet_manager_ship
                            and s["symbol"] not in grouped_haulers
                            and s["symbol"] not in grouped_workers]

            if len(free_haulers) >= 2:
                forced_buyer_ships = frozenset([free_haulers[0]])
                mine_only_ships    = frozenset(free_miners)
                trader_ships       = set(free_haulers[1:])
                log.info("[ASSIGN] direct-buy: hauler %s buys/delivers; %d miner(s) mine; %d hauler(s) trade",
                         free_haulers[0], len(free_miners), len(free_haulers) - 1)
            elif free_haulers:
                forced_buyer_ships = frozenset([free_miners[0]]) if free_miners else frozenset()
                mine_only_ships    = frozenset(free_miners[1:]) if len(free_miners) > 1 else frozenset()
                trader_ships       = set(free_haulers)
                log.info("[ASSIGN] direct-buy: miner %s buys/delivers; hauler %s trades; %d other miner(s) mine",
                         (free_miners[0] if free_miners else "none"), free_haulers[0], max(0, len(free_miners) - 1))
            else:
                forced_buyer_ships = frozenset([free_miners[0]]) if free_miners else frozenset()
                mine_only_ships    = frozenset(free_miners[1:]) if len(free_miners) > 1 else frozenset()
                trader_ships: set[str] = set()
                log.info("[ASSIGN] direct-buy: miner %s buys/delivers; %d other miner(s) mine",
                         (free_miners[0] if free_miners else "none"), max(0, len(free_miners) - 1))

            ctx.forced_buyer_ships = forced_buyer_ships
            ctx.mine_only_ships    = mine_only_ships
        else:
            trader_ships = set()

        # ── Launch group hauler tasks ─────────────────────────────────────────
        _role_kwargs = dict(
            config=self._cfg,
            client=self._client,
            navigator=self._nav,
            market=self._market,
            surveys=self._surveys,
        )
        for grp in active_groups:
            grp_type    = grp["type"]
            grp_hauler  = grp["hauler"]
            grp_workers = grp["workers"]
            if grp_type == "siphon":
                role = SiphonHaulerRole(workers=grp_workers, ship_symbol=grp_hauler, **_role_kwargs)
            else:
                role = MinerHaulerRole(workers=grp_workers, ship_symbol=grp_hauler, **_role_kwargs)
            task = asyncio.create_task(role.run(self._stop), name=f"{grp_hauler}:group_{grp_type}_hauler")
            self._active_tasks[grp_hauler] = task
            log.info("%s → group_%s_hauler (workers: %s)", grp_hauler, grp_type, grp_workers)

        # ── Launch individual ship tasks (skip group haulers) ─────────────────
        for ship in ships:
            sym = ship["symbol"]

            # Group haulers already have their task
            if sym in grouped_haulers:
                continue

            # Override role for trader ships in direct-buy mode
            if is_direct_buy_contract and sym in trader_ships:
                role_name = "trader"
            else:
                role_name = self._strategy.assign_role(ship, contract, self._cfg)

            role = self._build_role(sym, role_name, ctx)
            if role is None:
                log.debug("%s → idle", sym)
                continue
            log.info("%s → %s", sym, role_name)
            task = asyncio.create_task(role.run(self._stop), name=f"{sym}:{role_name}")
            self._active_tasks[sym] = task

        log.info("[ASSIGN] %d tasks launched", len(self._active_tasks))

    def _build_role(self, sym: str, role_name: str, ctx: ContractContext) -> object | None:
        kwargs = dict(
            ship_symbol=sym,
            config=self._cfg,
            client=self._client,
            navigator=self._nav,
            market=self._market,
            surveys=self._surveys,
        )
        if role_name == "miner":
            return MinerRole(**kwargs, contract_ctx=ctx)
        if role_name == "surveyor":
            return SurveyorRole(**kwargs, contract_ctx=ctx)
        if role_name == "hauler":
            return HaulerRole(**kwargs, contract_ctx=ctx)
        if role_name == "trader":
            return TraderRole(**kwargs)
        if role_name == "explorer":
            return ExplorerRole(**kwargs)
        if role_name == "siphoner":
            return SiphonerRole(**kwargs, contract_ctx=ctx)
        return None

    # ------------------------------------------------------------------
    # Periodic status table
    # ------------------------------------------------------------------

    async def _status_loop(self) -> None:
        """Print a fleet status table every 30 seconds."""
        INTERVAL = 30  # seconds
        # Short initial wait so startup noise settles first
        await asyncio.sleep(5)
        while not self._stop.is_set():
            try:
                await self._print_status_table()
            except Exception as e:
                log.debug("Status table error: %s", e)
            await asyncio.sleep(INTERVAL)

    async def _print_status_table(self) -> None:
        """Fetch live ship data and log a summary table."""
        _RESET  = "\x1b[0m"
        _BOLD   = "\x1b[1m"
        _DIM    = "\x1b[2m"
        _CYAN   = "\x1b[96m"
        _GREEN  = "\x1b[92m"
        _YELLOW = "\x1b[93m"
        _RED    = "\x1b[91m"
        _MAG    = "\x1b[95m"
        _BLUE   = "\x1b[94m"
        _WHITE  = "\x1b[97m"

        ROLE_COLOURS = [
            _CYAN, _GREEN, _MAG, _YELLOW, _BLUE, _RED, _WHITE,
        ]

        try:
            ships = await self._client.get_all_pages("/my/ships")
        except Exception:
            return

        # Assign stable colours based on ship symbol sort order
        all_syms = sorted(s["symbol"] for s in ships)
        colour_map = {
            sym: ROLE_COLOURS[i % len(ROLE_COLOURS)]
            for i, sym in enumerate(all_syms)
        }

        # Column widths
        W_SYM   = 22
        W_ROLE  = 12
        W_LOC   = 18
        W_FUEL  = 12
        W_CARGO = 10
        W_STAT  = 20
        TOTAL_W = W_SYM + W_ROLE + W_LOC + W_FUEL + W_CARGO + W_STAT + 5

        sep    = _DIM + "-" * TOTAL_W + _RESET
        header = (
            f"{_BOLD}"
            f"{'SHIP':<{W_SYM}} {'ROLE':<{W_ROLE}} {'LOCATION':<{W_LOC}}"
            f" {'FUEL':>{W_FUEL}} {'CARGO':>{W_CARGO}} {'STATUS':<{W_STAT}}"
            f"{_RESET}"
        )

        lines: list[str] = []

        # ── Contract block ──────────────────────────────────────────────
        ctx = self._current_ctx
        if ctx is not None:
            pct = int(100 * ctx.units_fulfilled / max(ctx.units_required, 1))
            bar_len = 30
            filled = int(bar_len * ctx.units_fulfilled / max(ctx.units_required, 1))
            bar = _GREEN + "█" * filled + _DIM + "░" * (bar_len - filled) + _RESET
            lines += [
                sep,
                f"{_BOLD}CONTRACT{_RESET}  "
                f"{_CYAN}{ctx.trade_symbol}{_RESET}  "
                f"{_WHITE}{ctx.units_fulfilled}/{ctx.units_required}{_RESET} "
                f"({_GREEN}{pct}%{_RESET})  "
                f"→ {ctx.destination}",
                f"  [{bar}]",
            ]

        # ── Fleet block ──────────────────────────────────────────────────
        lines += [sep, header, sep]

        now = datetime.now(timezone.utc)
        for ship in sorted(ships, key=lambda s: s["symbol"]):
            sym   = ship["symbol"]
            nav   = ship.get("nav", {})
            fuel  = ship.get("fuel", {})
            cargo = ship.get("cargo", {})
            col   = colour_map.get(sym, "")

            # Role from active task name
            task  = self._active_tasks.get(sym)
            role  = task.get_name().split(":", 1)[-1] if task else "idle"

            # Location / status
            status = nav.get("status", "?")
            wp     = nav.get("waypointSymbol", "?")
            if status == "IN_TRANSIT":
                arr_str = nav.get("route", {}).get("arrival", "")
                dest_wp = nav.get("route", {}).get("destination", {}).get("symbol", "?")
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    arr = _dt.fromisoformat(arr_str.replace("Z", "+00:00"))
                    secs = max(0, (arr - now).total_seconds())
                    from navigation import _fmt_secs
                    stat_str = f"{_YELLOW}→{dest_wp} ({_fmt_secs(secs)}){_RESET}"
                except Exception:
                    stat_str = f"{_YELLOW}→ {dest_wp}{_RESET}"
            elif status == "DOCKED":
                stat_str = f"{_DIM}DOCKED{_RESET}"
            else:
                stat_str = status  # IN_ORBIT

            fuel_str  = f"{fuel.get('current', 0)}/{fuel.get('capacity', 0)}"
            cargo_str = f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}"

            lines.append(
                f"{col}{sym:<{W_SYM}}{_RESET} "
                f"{role:<{W_ROLE}} "
                f"{wp:<{W_LOC}} "
                f"{fuel_str:>{W_FUEL}} "
                f"{cargo_str:>{W_CARGO}} "
                f"{stat_str}"
            )

        lines.append(sep)
        log.info("Fleet status:\n%s", "\n".join(lines))

    async def _cancel_all_tasks(self) -> None:
        if self._active_tasks:
            log.warning("_cancel_all_tasks: cancelling %d tasks", len(self._active_tasks))
        for sym, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._active_tasks.clear()

    # ------------------------------------------------------------------
    # Status snapshot (for dashboard / MCP)
    # ------------------------------------------------------------------

    def get_status_snapshot(self) -> dict:
        """Return a dict snapshot of current orchestrator state for display."""
        running = {sym: not task.done() for sym, task in self._active_tasks.items()}
        return {
            "system": self._cfg.system,
            "asteroid": self._cfg.asteroid,
            "asteroid_base": self._cfg.asteroid_base,
            "shipyard_wp": self._cfg.shipyard_wp,
            "active_tasks": running,
            "survey_pool_size": 0,  # async call needed; snapshot is sync
            "market_count": len(self._market.known_markets) if self._market else 0,
        }
