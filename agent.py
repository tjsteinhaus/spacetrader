"""Agent & account endpoints."""
import client


def register(symbol: str, faction: str = "COSMIC") -> dict:
    """Register a new agent. Returns {token, agent, faction, contract, ships}."""
    return client.post("/register", {"symbol": symbol, "faction": faction})


def get_my_agent() -> dict:
    return client.get("/my/agent")


def get_agent_events() -> dict:
    return client.get("/my/agent/events")


def get_my_account() -> dict:
    return client.get("/my/account")
