"""api/contracts.py — Contract endpoints."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client import SpaceTradersClient


async def get_contracts(client: "SpaceTradersClient") -> list[dict]:
    return await client.get_all_pages("/my/contracts")


async def get_contract(client: "SpaceTradersClient", contract_id: str) -> dict:
    return await client.get(f"/my/contracts/{contract_id}")


async def accept_contract(client: "SpaceTradersClient", contract_id: str) -> dict:
    return await client.post(f"/my/contracts/{contract_id}/accept")


async def fulfill_contract(client: "SpaceTradersClient", contract_id: str) -> dict:
    return await client.post(f"/my/contracts/{contract_id}/fulfill")


async def deliver_contract(
    client: "SpaceTradersClient",
    contract_id: str,
    ship_symbol: str,
    trade_symbol: str,
    units: int,
) -> dict:
    return await client.post(
        f"/my/contracts/{contract_id}/deliver",
        {"shipSymbol": ship_symbol, "tradeSymbol": trade_symbol, "units": units},
    )


async def negotiate_contract(client: "SpaceTradersClient", ship_symbol: str) -> dict:
    """Negotiate a new contract. Ship must be docked at its faction's HQ."""
    return await client.post(f"/my/ships/{ship_symbol}/negotiate/contract")
