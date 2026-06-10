"""api/agent.py — Agent and account endpoints."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client import SpaceTradersClient


async def register(client: "SpaceTradersClient", symbol: str, faction: str = "COSMIC") -> dict:
    """Register a new agent. Returns {token, agent, faction, contract, ships}."""
    return await client.post("/register", {"symbol": symbol, "faction": faction})


async def get_my_agent(client: "SpaceTradersClient") -> dict:
    return await client.get("/my/agent")


async def get_agent_events(client: "SpaceTradersClient") -> dict:
    return await client.get("/my/agent/events")


async def get_my_account(client: "SpaceTradersClient") -> dict:
    return await client.get("/my/account")
