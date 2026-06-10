"""
strategy.py — Strategy Protocol and implementations.
Decouples "what to do next" from the orchestrator's "how to run it".
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from client import SpaceTradersClient
    from config import Config

log = logging.getLogger(__name__)


@runtime_checkable
class Strategy(Protocol):
    """Protocol that all strategy implementations must satisfy."""

    async def select_contract(self, client: "SpaceTradersClient", cfg: "Config", navigator=None) -> dict | None:
        """Choose the next contract to work on. Returns contract dict or None."""
        ...

    def should_buy_ships(self, credits: int, cfg: "Config") -> bool:
        """Return True when the bot should try to expand its fleet."""
        ...

    def assign_role(self, ship: dict, contract: dict | None, cfg: "Config") -> str:
        """Return role name for a given ship ('miner'|'surveyor'|'hauler'|'explorer'|'siphoner'|'idle')."""
        ...


class ContractGrindStrategy:
    """Default strategy: accept the highest-payout contract, mine/buy to fill it."""

    def __init__(
        self,
        min_payout: int = 30_000,
        ship_role_overrides: dict[str, str] | None = None,
    ) -> None:
        self.min_payout = min_payout
        # e.g. {"TYLERMASTERY2-5": "trader"} — pinned regardless of contract state
        self.ship_role_overrides: dict[str, str] = ship_role_overrides or {}

    async def select_contract(self, client: "SpaceTradersClient", cfg: "Config", navigator=None) -> dict | None:
        from api import contracts as contracts_api
        from client import SpaceTradersError
        try:
            all_contracts = await contracts_api.get_contracts(client)
        except Exception as e:
            log.warning("Could not fetch contracts: %s", e)
            return None

        # Prefer accepted+unfulfilled contracts first
        active = [c for c in all_contracts if c.get("accepted") and not c.get("fulfilled")]
        if active:
            return max(active, key=lambda c: c.get("terms", {}).get("payment", {}).get("onFulfilled", 0))

        # Accept the best available (already offered) contract
        available = [
            c for c in all_contracts
            if not c.get("accepted") and not c.get("fulfilled")
            and c.get("terms", {}).get("payment", {}).get("onFulfilled", 0) >= self.min_payout
        ]
        if available:
            best = max(available, key=lambda c: c.get("terms", {}).get("payment", {}).get("onFulfilled", 0))
            try:
                result = await contracts_api.accept_contract(client, best["id"])
                accepted = result.get("contract", best)
                log.info(
                    "Accepted contract %s: %s for %d cr",
                    accepted.get("id"), accepted.get("type"),
                    accepted.get("terms", {}).get("payment", {}).get("onFulfilled", 0),
                )
                return accepted
            except Exception as e:
                log.warning("Could not accept contract %s: %s", best.get("id"), e)
                return best

        # No contracts available — negotiate a new one from HQ using command ship
        log.info("No contracts available — negotiating new contract at HQ")
        from api import fleet as fleet_api
        ship_symbol = cfg.command_ship
        try:
            ship = await fleet_api.get_ship(client, ship_symbol)
            nav = ship.get("nav", {})
            if nav.get("waypointSymbol") != cfg.faction_hq_wp:
                # Need to navigate to HQ — orbit first if docked
                if nav.get("status") == "DOCKED":
                    await fleet_api.orbit(client, ship_symbol)
                if navigator is not None:
                    await navigator.navigate_with_refuel(ship_symbol, cfg.faction_hq_wp)
                else:
                    await fleet_api.navigate(client, ship_symbol, cfg.faction_hq_wp)
                # Wait for arrival
            # Dock if needed
            ship = await fleet_api.get_ship(client, ship_symbol)
            if ship["nav"]["status"] != "DOCKED":
                await fleet_api.dock(client, ship_symbol)
            result = await contracts_api.negotiate_contract(client, ship_symbol)
            new_contract = result.get("contract", {})
            log.info(
                "Negotiated contract %s: %s for %d cr",
                new_contract.get("id"), new_contract.get("type"),
                new_contract.get("terms", {}).get("payment", {}).get("onFulfilled", 0),
            )
            # Accept it immediately
            accepted = await contracts_api.accept_contract(client, new_contract["id"])
            c = accepted.get("contract", new_contract)
            log.info("Accepted negotiated contract %s", c.get("id"))
            return c
        except SpaceTradersError as e:
            if e.code == 4511:
                # Already has active contract — re-fetch
                log.warning("4511: already has active contract — re-fetching")
                try:
                    all_contracts = await contracts_api.get_contracts(client)
                    active = [c for c in all_contracts if c.get("accepted") and not c.get("fulfilled")]
                    if active:
                        return max(active, key=lambda c: c.get("terms", {}).get("payment", {}).get("onFulfilled", 0))
                except Exception:
                    pass
            log.warning("Could not negotiate contract: %s", e)
            return None
        except Exception as e:
            log.warning("Could not negotiate contract: %s", e)
            return None

    def should_buy_ships(self, credits: int, cfg: "Config") -> bool:
        return credits >= cfg.min_buy_credits

    def assign_role(self, ship: dict, contract: dict | None, cfg: "Config") -> str:
        # Pinned overrides take priority over all other logic
        sym = ship.get("symbol", "")
        if sym in self.ship_role_overrides:
            return self.ship_role_overrides[sym]

        mounts = ship.get("mounts", [])
        modules = ship.get("modules", [])
        has_mining = any("MINING" in m.get("symbol", "") for m in mounts)
        has_survey = any("SURVEYOR" in m.get("symbol", "") for m in mounts)
        has_siphon = any("SIPHON" in m.get("symbol", "") or "GAS" in m.get("symbol", "") for m in mounts)
        has_jump   = any("JUMP_DRIVE" in m.get("symbol", "") for m in modules)
        has_warp   = any("WARP_DRIVE" in m.get("symbol", "") for m in modules)
        capacity   = ship.get("cargo", {}).get("capacity", 0)

        if has_survey and not has_mining:
            return "surveyor"
        if has_siphon and not has_mining:
            return "siphoner"
        if has_jump or has_warp:
            return "explorer"
        if has_mining:
            return "miner"
        if capacity >= 40 and not has_mining and not has_survey:
            # When a contract is active, dedicate large-cargo ships to hauling
            # (miners stay at the asteroid; hauler handles delivery runs).
            # When no contract is active, switch to arbitrage trading.
            return "hauler" if contract is not None else "trader"
        return "idle"


class IdleStrategy:
    """Does nothing — useful for manual control mode."""

    async def select_contract(self, client, cfg) -> dict | None:
        return None

    def should_buy_ships(self, credits: int, cfg) -> bool:
        return False

    def assign_role(self, ship: dict, contract: dict | None, cfg) -> str:
        return "idle"


def load_strategy(path: Path) -> Strategy:
    """Load strategy configuration from a JSON file."""
    if not path.exists():
        log.info("No strategy.json found — using ContractGrindStrategy defaults")
        return ContractGrindStrategy()

    try:
        data = json.loads(path.read_text())
        strategy_name = data.get("strategy", "contract_grind")
        if strategy_name == "idle":
            return IdleStrategy()
        # ContractGrindStrategy with optional overrides
        min_payout = data.get("min_contract_payout", 30_000)
        overrides  = data.get("ship_role_overrides", {})
        return ContractGrindStrategy(min_payout=min_payout, ship_role_overrides=overrides)
    except Exception as e:
        log.warning("Could not load strategy.json (%s) — using defaults", e)
        return ContractGrindStrategy()
