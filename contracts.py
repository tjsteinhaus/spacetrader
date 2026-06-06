"""Contract endpoints."""
import client


def get_contracts() -> list:
    return client.get_all_pages("/my/contracts")


def get_contract(contract_id: str) -> dict:
    return client.get(f"/my/contracts/{contract_id}")


def accept_contract(contract_id: str) -> dict:
    return client.post(f"/my/contracts/{contract_id}/accept")


def fulfill_contract(contract_id: str) -> dict:
    return client.post(f"/my/contracts/{contract_id}/fulfill")


def deliver_contract(contract_id: str, ship_symbol: str, trade_symbol: str, units: int) -> dict:
    return client.post(
        f"/my/contracts/{contract_id}/deliver",
        {"shipSymbol": ship_symbol, "tradeSymbol": trade_symbol, "units": units},
    )
