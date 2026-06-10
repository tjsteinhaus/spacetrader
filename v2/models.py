"""
models.py — Pydantic v2 models for SpaceTraders API response shapes.
All API responses are parsed into these models at the client boundary.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

_cfg = ConfigDict(populate_by_name=True, extra="ignore")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

class NavWaypoint(BaseModel):
    model_config = _cfg
    symbol: str
    type: str = ""
    system_symbol: str = Field("", alias="systemSymbol")
    x: int = 0
    y: int = 0


class NavRoute(BaseModel):
    model_config = _cfg
    departure: NavWaypoint | None = None
    destination: NavWaypoint
    departure_time: str = Field("", alias="departureTime")
    arrival: str = ""


class ShipNav(BaseModel):
    model_config = _cfg
    system_symbol: str = Field("", alias="systemSymbol")
    waypoint_symbol: str = Field("", alias="waypointSymbol")
    status: str = "IN_ORBIT"       # DOCKED | IN_ORBIT | IN_TRANSIT
    flight_mode: str = Field("CRUISE", alias="flightMode")
    route: NavRoute | None = None


# ---------------------------------------------------------------------------
# Cargo
# ---------------------------------------------------------------------------

class CargoItem(BaseModel):
    model_config = _cfg
    symbol: str
    name: str = ""
    description: str = ""
    units: int = 0


class ShipCargo(BaseModel):
    model_config = _cfg
    capacity: int = 0
    units: int = 0
    inventory: list[CargoItem] = Field(default_factory=list)

    @property
    def free(self) -> int:
        return self.capacity - self.units


# ---------------------------------------------------------------------------
# Fuel
# ---------------------------------------------------------------------------

class ShipFuel(BaseModel):
    model_config = _cfg
    current: int = 0
    capacity: int = 0

    @property
    def pct(self) -> float:
        return self.current / self.capacity if self.capacity else 1.0


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class ComponentCondition(BaseModel):
    model_config = _cfg
    symbol: str = ""
    condition: float = 1.0
    integrity: float = 1.0

    @property
    def condition_normalized(self) -> float:
        """Normalize to 0.0–1.0 regardless of whether API returns 0–1 or 0–100."""
        return self.condition / 100.0 if self.condition > 1.0 else self.condition


class Mount(BaseModel):
    model_config = _cfg
    symbol: str
    name: str = ""
    description: str = ""
    strength: int = 0
    deposits: list[str] = Field(default_factory=list)
    requirements: dict[str, Any] = Field(default_factory=dict)


class Module(BaseModel):
    model_config = _cfg
    symbol: str
    name: str = ""
    description: str = ""
    capacity: int = 0
    requirements: dict[str, Any] = Field(default_factory=dict)


class Registration(BaseModel):
    model_config = _cfg
    name: str = ""
    faction_symbol: str = Field("", alias="factionSymbol")
    role: str = ""


class Cooldown(BaseModel):
    model_config = _cfg
    ship_symbol: str = Field("", alias="shipSymbol")
    total_seconds: int = Field(0, alias="totalSeconds")
    remaining_seconds: int = Field(0, alias="remainingSeconds")
    expiration: str = ""


# ---------------------------------------------------------------------------
# Ship
# ---------------------------------------------------------------------------

class Ship(BaseModel):
    model_config = _cfg
    symbol: str
    registration: Registration = Field(default_factory=Registration)
    nav: ShipNav = Field(default_factory=ShipNav)
    cargo: ShipCargo = Field(default_factory=ShipCargo)
    fuel: ShipFuel = Field(default_factory=ShipFuel)
    frame: ComponentCondition = Field(default_factory=ComponentCondition)
    engine: ComponentCondition = Field(default_factory=ComponentCondition)
    reactor: ComponentCondition = Field(default_factory=ComponentCondition)
    mounts: list[Mount] = Field(default_factory=list)
    modules: list[Module] = Field(default_factory=list)

    def has_mining_mount(self) -> bool:
        return any("MINING" in m.symbol for m in self.mounts)

    def has_survey_mount(self) -> bool:
        return any("SURVEYOR" in m.symbol for m in self.mounts)

    def has_jump_drive(self) -> bool:
        return any("JUMP_DRIVE" in m.symbol for m in self.modules)

    def has_warp_drive(self) -> bool:
        return any("WARP_DRIVE" in m.symbol for m in self.modules)

    def has_siphon_mount(self) -> bool:
        return any("SIPHON" in m.symbol or "GAS" in m.symbol for m in self.mounts)

    def best_mining_tier(self, tiers: list[str]) -> int:
        """Return 0-based index of best mining mount (-1 if none)."""
        symbols = {m.symbol for m in self.mounts}
        for i in range(len(tiers) - 1, -1, -1):
            if tiers[i] in symbols:
                return i
        return -1

    def needs_repair(self, threshold: float = 0.80) -> bool:
        return any(
            c.condition_normalized < threshold
            for c in (self.frame, self.engine, self.reactor)
        )

    def worst_condition(self) -> float:
        return min(
            c.condition_normalized
            for c in (self.frame, self.engine, self.reactor)
        )


# ---------------------------------------------------------------------------
# Waypoints & Universe
# ---------------------------------------------------------------------------

class WaypointTrait(BaseModel):
    model_config = _cfg
    symbol: str
    name: str = ""
    description: str = ""


class WaypointFaction(BaseModel):
    model_config = _cfg
    symbol: str = ""


class Waypoint(BaseModel):
    model_config = _cfg
    symbol: str
    type: str = ""
    system_symbol: str = Field("", alias="systemSymbol")
    x: int = 0
    y: int = 0
    traits: list[WaypointTrait] = Field(default_factory=list)
    faction: WaypointFaction | None = None
    is_under_construction: bool = Field(False, alias="isUnderConstruction")

    def has_trait(self, trait: str) -> bool:
        return any(t.symbol == trait for t in self.traits)

    def trait_symbols(self) -> frozenset[str]:
        return frozenset(t.symbol for t in self.traits)


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

class TradeGood(BaseModel):
    model_config = _cfg
    symbol: str
    name: str = ""
    description: str = ""


class MarketTradeGood(BaseModel):
    model_config = _cfg
    symbol: str
    type: str = ""
    trade_volume: int = Field(0, alias="tradeVolume")
    supply: str = ""
    activity: str = ""
    purchase_price: int = Field(0, alias="purchasePrice")
    sell_price: int = Field(0, alias="sellPrice")


class MarketTransaction(BaseModel):
    model_config = _cfg
    waypoint_symbol: str = Field("", alias="waypointSymbol")
    ship_symbol: str = Field("", alias="shipSymbol")
    trade_symbol: str = Field("", alias="tradeSymbol")
    type: str = ""
    units: int = 0
    price_per_unit: int = Field(0, alias="pricePerUnit")
    total_price: int = Field(0, alias="totalPrice")
    timestamp: str = ""


class Market(BaseModel):
    model_config = _cfg
    symbol: str
    exports: list[TradeGood] = Field(default_factory=list)
    imports: list[TradeGood] = Field(default_factory=list)
    exchange: list[TradeGood] = Field(default_factory=list)
    transactions: list[MarketTransaction] = Field(default_factory=list)
    trade_goods: list[MarketTradeGood] = Field(default_factory=list, alias="tradeGoods")

    def price_map(self) -> dict[str, int]:
        """Return {symbol: sell_price} for all trade goods with live prices."""
        return {g.symbol: g.sell_price for g in self.trade_goods if g.sell_price > 0}

    def buy_price_map(self) -> dict[str, int]:
        return {g.symbol: g.purchase_price for g in self.trade_goods if g.purchase_price > 0}

    def exports_good(self, symbol: str) -> bool:
        return any(g.symbol == symbol for g in (*self.exports, *self.exchange))

    def imports_good(self, symbol: str) -> bool:
        return any(g.symbol == symbol for g in (*self.imports, *self.exchange))


# ---------------------------------------------------------------------------
# Shipyard
# ---------------------------------------------------------------------------

class ShipyardShip(BaseModel):
    model_config = _cfg
    type: str
    name: str = ""
    description: str = ""
    purchase_price: int = Field(0, alias="purchasePrice")
    frame: dict[str, Any] = Field(default_factory=dict)
    engine: dict[str, Any] = Field(default_factory=dict)
    reactor: dict[str, Any] = Field(default_factory=dict)
    mounts: list[dict[str, Any]] = Field(default_factory=list)
    modules: list[dict[str, Any]] = Field(default_factory=list)


class Shipyard(BaseModel):
    model_config = _cfg
    symbol: str
    ship_types: list[dict[str, Any]] = Field(default_factory=list, alias="shipTypes")
    ships: list[ShipyardShip] = Field(default_factory=list)
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    modifications_fee: int = Field(0, alias="modificationsFee")


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

class ContractDelivery(BaseModel):
    model_config = _cfg
    trade_symbol: str = Field("", alias="tradeSymbol")
    destination_symbol: str = Field("", alias="destinationSymbol")
    units_required: int = Field(0, alias="unitsRequired")
    units_fulfilled: int = Field(0, alias="unitsFulfilled")

    @property
    def remaining(self) -> int:
        return max(0, self.units_required - self.units_fulfilled)

    @property
    def is_complete(self) -> bool:
        return self.units_fulfilled >= self.units_required


class ContractPayment(BaseModel):
    model_config = _cfg
    on_accepted: int = Field(0, alias="onAccepted")
    on_fulfilled: int = Field(0, alias="onFulfilled")


class ContractTerms(BaseModel):
    model_config = _cfg
    deadline: str = ""
    payment: ContractPayment = Field(default_factory=ContractPayment)
    deliver: list[ContractDelivery] = Field(default_factory=list)


class Contract(BaseModel):
    model_config = _cfg
    id: str
    faction_symbol: str = Field("", alias="factionSymbol")
    type: str = ""
    terms: ContractTerms = Field(default_factory=ContractTerms)
    accepted: bool = False
    fulfilled: bool = False
    expiration: str = ""
    deadline_to_accept: str = Field("", alias="deadlineToAccept")

    @property
    def first_delivery(self) -> ContractDelivery | None:
        return self.terms.deliver[0] if self.terms.deliver else None

    @property
    def is_complete(self) -> bool:
        return all(d.is_complete for d in self.terms.deliver)


# ---------------------------------------------------------------------------
# Surveys
# ---------------------------------------------------------------------------

class SurveyDeposit(BaseModel):
    model_config = _cfg
    symbol: str
    size: str = ""


class Survey(BaseModel):
    model_config = _cfg
    signature: str
    symbol: str = ""
    deposits: list[SurveyDeposit] = Field(default_factory=list)
    expiration: str = ""
    size: str = ""

    def is_expired(self) -> bool:
        if not self.expiration:
            return True
        try:
            exp = datetime.fromisoformat(self.expiration.replace("Z", "+00:00"))
            return exp <= datetime.now(exp.tzinfo)
        except Exception:
            return True

    def count_good(self, symbol: str) -> int:
        return sum(1 for d in self.deposits if d.symbol == symbol)

    def raw(self) -> dict[str, Any]:
        """Return as plain dict suitable for the extract_with_survey API call."""
        return self.model_dump(by_alias=False, mode="json")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class ExtractionYield(BaseModel):
    model_config = _cfg
    symbol: str
    units: int = 0


class ExtractionResult(BaseModel):
    model_config = _cfg
    extraction: dict[str, Any] = Field(default_factory=dict)
    cargo: ShipCargo = Field(default_factory=ShipCargo)
    cooldown: Cooldown = Field(default_factory=Cooldown)
    events: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def extracted_good(self) -> str:
        return self.extraction.get("yield", {}).get("symbol", "")

    @property
    def extracted_units(self) -> int:
        return self.extraction.get("yield", {}).get("units", 0)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent(BaseModel):
    model_config = _cfg
    account_id: str = Field("", alias="accountId")
    symbol: str
    headquarters: str = ""
    credits: int = 0
    starting_faction: str = Field("", alias="startingFaction")
    ship_count: int = Field(0, alias="shipCount")


# ---------------------------------------------------------------------------
# Purchase / transaction
# ---------------------------------------------------------------------------

class PurchaseResult(BaseModel):
    model_config = _cfg
    agent: Agent | None = None
    ship: Ship | None = None
    transaction: dict[str, Any] = Field(default_factory=dict)
