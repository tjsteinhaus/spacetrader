#!/usr/bin/env python3
"""
SpaceTraders Strategic Advisor — MCP Server

Exposes tools for AI-driven strategic decisions:
  - get_situation()            comprehensive snapshot (agent, fleet, contracts, strategy)
  - get_market_prices()        current prices at all known markets
  - get_shipyard()             ship listings + prices at SHIPYARD_WP
  - analyze_contract_value()   estimated $/hr for each contract
  - get_upgrade_analysis()     current mounts and upgrade opportunities
  - set_strategy()             write directives to strategy.json
  - get_strategy()             read current strategy.json
  - negotiate_new_contract()   command ship docks + negotiates a fresh contract

Run via VS Code MCP integration (stdio transport).
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import agent as agent_api
import contracts as contracts_api
import fleet as fleet_api
import universe as universe_api
from client import SpaceTradersError

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────
# NOTE: Update these for each server reset
SYSTEM        = "X1-BX78"
COMMAND_SHIP  = "TYLERDEVRUN-1"
SHIPYARD_WP   = "X1-BX78-A2"
HQ_WP         = "X1-BX78-A1"
ASTEROID      = "X1-BX78-C44"
STRATEGY_FILE = Path(__file__).parent / "strategy.json"

VALID_MODES = {"contract_grind", "fleet_expansion", "upgrade_first", "idle"}

LASER_TIERS = {
    "MOUNT_MINING_LASER_I":   1,
    "MOUNT_MINING_LASER_II":  2,
    "MOUNT_MINING_LASER_III": 3,
}

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("spacetraders-advisor")


def _read_strategy() -> dict:
    try:
        return json.loads(STRATEGY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"mode": "contract_grind", "notes": "", "target_contract_id": None}


def _write_strategy(data: dict) -> None:
    STRATEGY_FILE.write_text(json.dumps(data, indent=2))


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_situation() -> dict:
    """
    Return a comprehensive snapshot of the current game state:
    agent credits, fleet status (location, cargo, mounts), active contracts
    (what's needed, how much is delivered), and the current strategy directive.

    Use this as your first call to understand what's happening before making
    any strategic recommendation.
    """
    try:
        me    = agent_api.get_my_agent()
        ships = fleet_api.get_my_ships()
        cs    = contracts_api.get_contracts()
    except SpaceTradersError as e:
        return {"error": str(e)}

    fleet_summary = []
    for s in ships:
        symbol  = s.get("symbol", "?")
        nav     = s.get("nav", {})
        cargo   = s.get("cargo", {})
        mounts  = [m.get("symbol", "") for m in s.get("mounts", [])]
        fuel    = s.get("fuel", {})
        fleet_summary.append({
            "symbol":   symbol,
            "role":     s.get("registration", {}).get("role", "?"),
            "status":   nav.get("status", "?"),
            "location": nav.get("waypointSymbol", "?"),
            "cargo":    f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}",
            "fuel":     f"{fuel.get('current', 0)}/{fuel.get('capacity', 0)}",
            "mounts":   mounts,
        })

    contract_summary = []
    for c in cs:
        if c.get("fulfilled"):
            continue
        terms   = c.get("terms", {})
        deliver = terms.get("deliver", [])
        payment = terms.get("payment", {})
        contract_summary.append({
            "id":            c.get("id"),
            "accepted":      c.get("accepted", False),
            "deadline":      terms.get("deadline", "?"),
            "on_accept":     payment.get("onAccepted", 0),
            "on_fulfill":    payment.get("onFulfilled", 0),
            "deliverables":  [
                {
                    "good":         d.get("tradeSymbol"),
                    "required":     d.get("unitsRequired"),
                    "fulfilled":    d.get("unitsFulfilled"),
                    "destination":  d.get("destinationSymbol"),
                }
                for d in deliver
            ],
        })

    return {
        "agent": {
            "symbol":    me.get("symbol"),
            "credits":   me.get("credits"),
            "ship_count": me.get("shipCount"),
            "headquarters": me.get("headquarters"),
        },
        "fleet":     fleet_summary,
        "contracts": contract_summary,
        "strategy":  _read_strategy(),
    }


@mcp.tool()
def get_market_prices(waypoints: list[str] | None = None) -> dict:
    """
    Return live market prices at the specified waypoints (or all known markets
    in the current system if none provided).

    Returns: {waypoint: {trade_symbol: {sell, buy, supply, trade_volume}}}

    Use this to identify where to sell ore, where to buy upgrade materials,
    or to check if a contract good is available to purchase.
    """
    if not waypoints:
        try:
            all_wps = universe_api.get_waypoints(SYSTEM)
        except SpaceTradersError as e:
            return {"error": str(e)}
        waypoints = [
            wp.get("symbol", "")
            for wp in all_wps
            if any(t.get("symbol") == "MARKETPLACE" for t in wp.get("traits", []))
        ]

    result: dict[str, dict] = {}
    for wp in waypoints:
        try:
            market = universe_api.get_market(SYSTEM, wp)
            trade_goods = market.get("tradeGoods", [])
            if trade_goods:
                result[wp] = {
                    tg["symbol"]: {
                        "sell":         tg.get("sellPrice", 0),
                        "buy":          tg.get("purchasePrice", 0),
                        "supply":       tg.get("supply", "?"),
                        "trade_volume": tg.get("tradeVolume", 0),
                    }
                    for tg in trade_goods
                }
            else:
                # Market exists but no ship present — only imports/exports visible
                result[wp] = {
                    "note": "No ship at this market — prices not available",
                    "imports":  [g.get("symbol") for g in market.get("imports", [])],
                    "exports":  [g.get("symbol") for g in market.get("exports", [])],
                }
        except SpaceTradersError as e:
            result[wp] = {"error": str(e)}
    return result


@mcp.tool()
def get_shipyard() -> dict:
    """
    Return current ship listings and prices at the shipyard (X1-BX78-A2).

    Includes ship type, purchase price, available mounts, frame, and whether
    it has mining or survey capability. Use this to decide which ships to buy next.

    Note: A ship must be physically present at the shipyard for prices to appear.
    """
    try:
        shipyard = universe_api.get_shipyard(SYSTEM, SHIPYARD_WP)
    except SpaceTradersError as e:
        return {"error": str(e)}

    ships_for_sale = shipyard.get("ships", [])
    if not ships_for_sale:
        return {
            "note": "No price data — need a ship at the shipyard",
            "available_types": [st.get("type") for st in shipyard.get("shipTypes", [])],
        }

    result = []
    for s in ships_for_sale:
        mounts = [m.get("symbol", "") for m in s.get("mounts", [])]
        result.append({
            "type":          s.get("type"),
            "price":         s.get("purchasePrice", 0),
            "supply":        s.get("supply", "?"),
            "frame":         s.get("frame", {}).get("symbol", "?"),
            "can_mine":      any("MINING_LASER" in m for m in mounts),
            "can_survey":    any("SURVEYOR" in m for m in mounts),
            "mounts":        mounts,
            "cargo_capacity": s.get("cargo", {}).get("capacity", 0),
        })

    result.sort(key=lambda x: x["price"])
    return {"shipyard": SHIPYARD_WP, "ships": result}


@mcp.tool()
def analyze_contract_value(contract_id: str | None = None) -> list[dict]:
    """
    Estimate the $/hr value of each active (unfulfilled) contract, or a specific
    contract by ID.

    Model assumptions:
    - ~18 ore units/hr per miner at base rate
    - 1.5x multiplier when a surveyor is active
    - Uses current fleet miner/surveyor counts

    Returns each contract with: id, good, total_required, remaining, on_fulfill,
    estimated_hours, estimated_credits_per_hour, and a recommendation string.
    """
    try:
        cs    = contracts_api.get_contracts()
        ships = fleet_api.get_my_ships()
        me    = agent_api.get_my_agent()
    except SpaceTradersError as e:
        return [{"error": str(e)}]

    pending = [c for c in cs if not c.get("fulfilled")]
    if contract_id:
        pending = [c for c in pending if c.get("id") == contract_id]

    # Count mining-capable and survey-capable ships
    miner_count   = 0
    surveyor_count = 0
    for s in ships:
        mounts = [m.get("symbol", "") for m in s.get("mounts", [])]
        if any("MINING_LASER" in m for m in mounts):
            miner_count += 1
        if any("SURVEYOR" in m for m in mounts):
            surveyor_count += 1

    base_ore_per_hour = 18.0
    surveyor_bonus    = 1.5 if surveyor_count > 0 else 1.0
    ore_per_hour      = base_ore_per_hour * miner_count * surveyor_bonus

    results = []
    for c in pending:
        terms    = c.get("terms", {})
        payment  = terms.get("payment", {})
        deliver  = terms.get("deliver", [])
        on_fulfill = payment.get("onFulfilled", 0)
        on_accept  = payment.get("onAccepted", 0)
        total_credits = on_fulfill + on_accept

        total_required = sum(d.get("unitsRequired", 0) for d in deliver)
        total_delivered = sum(d.get("unitsFulfilled", 0) for d in deliver)
        remaining = total_required - total_delivered

        if ore_per_hour > 0 and remaining > 0:
            est_hours = remaining / ore_per_hour
            cph = total_credits / est_hours if est_hours > 0 else 0
        else:
            est_hours = 0
            cph = 0

        goods = [d.get("tradeSymbol") for d in deliver]
        results.append({
            "id":              c.get("id"),
            "accepted":        c.get("accepted", False),
            "goods":           goods,
            "total_required":  total_required,
            "delivered":       total_delivered,
            "remaining":       remaining,
            "on_accept":       on_accept,
            "on_fulfill":      on_fulfill,
            "total_credits":   total_credits,
            "est_hours":       round(est_hours, 2),
            "credits_per_hour": round(cph),
            "fleet": {
                "miners":    miner_count,
                "surveyors": surveyor_count,
                "ore_per_hr": round(ore_per_hour, 1),
            },
            "recommendation": (
                "Accept immediately" if not c.get("accepted") and cph > 5000
                else "Already working" if c.get("accepted")
                else "Low value — consider skipping"
            ),
        })

    results.sort(key=lambda x: x["credits_per_hour"], reverse=True)
    return results


@mcp.tool()
def get_upgrade_analysis() -> dict:
    """
    Analyse the current mining laser tiers across all miners and identify
    upgrade opportunities.

    Returns per-ship mount breakdown, upgrade path (LASER_I→II→III), and
    estimated credit cost based on the last known shipyard buy prices.

    The mining ship upgrade path is:
    LASER_I (default) → LASER_II → LASER_III
    Each tier increases extraction yield.
    """
    try:
        ships    = fleet_api.get_my_ships()
        shipyard = universe_api.get_shipyard(SYSTEM, SHIPYARD_WP)
    except SpaceTradersError as e:
        return {"error": str(e)}

    # Build mount price map from shipyard data
    mount_prices: dict[str, int] = {}
    for s in shipyard.get("ships", []):
        for m in s.get("mounts", []):
            sym = m.get("symbol", "")
            price = m.get("requirements", {}).get("credits", 0)
            if sym and price:
                mount_prices[sym] = price

    upgrades = []
    for s in ships:
        symbol = s.get("symbol", "?")
        mounts = s.get("mounts", [])
        for m in mounts:
            sym   = m.get("symbol", "")
            tier  = LASER_TIERS.get(sym, 0)
            if tier == 0:
                continue
            next_tier_name = {1: "MOUNT_MINING_LASER_II", 2: "MOUNT_MINING_LASER_III"}.get(tier)
            upgrades.append({
                "ship":       symbol,
                "current":    sym,
                "tier":       tier,
                "next_mount": next_tier_name,
                "upgrade_cost": mount_prices.get(next_tier_name, "unknown") if next_tier_name else None,
                "maxed":      tier == 3,
            })

    already_maxed = [u for u in upgrades if u["maxed"]]
    can_upgrade   = [u for u in upgrades if not u["maxed"]]

    return {
        "upgradeable":   can_upgrade,
        "maxed":         already_maxed,
        "mount_prices":  mount_prices,
        "summary": (
            f"{len(can_upgrade)} laser(s) can be upgraded, "
            f"{len(already_maxed)} already at tier III"
        ),
    }


@mcp.tool()
def get_strategy() -> dict:
    """
    Read and return the current strategy.json directive being followed by play.py.

    Fields:
      mode               — current operating mode
      notes              — optional advisory message shown in play.py logs
      target_contract_id — if set, play.py will prefer this contract ID

    Valid modes:
      contract_grind   — default: mine → deliver → repeat
      fleet_expansion  — same as contract_grind but buy_ships() is prioritised
      upgrade_first    — skip buying ships; focus on laser upgrades
      idle             — pause the main loop (play.py sleeps each iteration)
    """
    return _read_strategy()


@mcp.tool()
def set_strategy(
    mode: str,
    notes: str = "",
    target_contract_id: str | None = None,
) -> dict:
    """
    Write a new strategic directive to strategy.json. play.py reads this at
    the top of every loop iteration.

    Args:
        mode: One of "contract_grind", "fleet_expansion", "upgrade_first", "idle"
        notes: Optional free-text note logged by play.py (e.g. why you chose this mode)
        target_contract_id: If set, play.py will work this specific contract ID
                            instead of defaulting to the first pending contract.

    Returns the strategy that was written.
    """
    if mode not in VALID_MODES:
        return {
            "error": f"Invalid mode '{mode}'. Valid modes: {sorted(VALID_MODES)}"
        }

    strategy = {
        "mode":               mode,
        "notes":              notes,
        "target_contract_id": target_contract_id,
    }
    _write_strategy(strategy)
    return {"written": strategy}


@mcp.tool()
def negotiate_new_contract() -> dict:
    """
    Command the command ship (TYLERDEVRUN-1) to navigate to the faction HQ
    (X1-BX78-A1), dock, and negotiate a new contract.

    Use this when play.py is stuck with no contracts, or when you want to
    pre-queue a contract before the current one finishes.

    Returns the negotiated contract object, or an error message.
    """
    try:
        # Navigate to faction HQ
        nav_resp = fleet_api.navigate(COMMAND_SHIP, HQ_WP)
        nav_data = nav_resp.get("nav", {})
        arrival  = nav_data.get("route", {}).get("arrival", "")

        # Wait for arrival if in transit
        if nav_data.get("status") == "IN_TRANSIT":
            from datetime import datetime, timezone
            try:
                arr_dt = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                wait_s = max(0, (arr_dt - datetime.now(timezone.utc)).total_seconds()) + 2
                time.sleep(wait_s)
            except Exception:
                time.sleep(30)

        # Dock
        fleet_api.dock(COMMAND_SHIP)

        # Negotiate
        result    = fleet_api.negotiate_contract(COMMAND_SHIP)
        contract  = result.get("contract", {})
        terms     = contract.get("terms", {})
        payment   = terms.get("payment", {})
        return {
            "success":    True,
            "contract_id": contract.get("id"),
            "on_accept":  payment.get("onAccepted", 0),
            "on_fulfill": payment.get("onFulfilled", 0),
            "deadline":   terms.get("deadline", "?"),
            "deliverables": [
                {
                    "good":      d.get("tradeSymbol"),
                    "required":  d.get("unitsRequired"),
                    "destination": d.get("destinationSymbol"),
                }
                for d in terms.get("deliver", [])
            ],
        }
    except SpaceTradersError as e:
        return {"success": False, "error": str(e)}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
