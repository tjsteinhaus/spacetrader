"""
constants.py — Static SpaceTraders v2 game data.
No imports, no side effects. Pure data for the whole v2 package.
"""
from __future__ import annotations

# Goods that can be extracted from asteroids by mining.
# Always prefer mining these rather than purchasing unless the market price
# is at or below CHEAP_BUY_THRESHOLD (trivial cost) or the asteroid has no
# matching deposit trait.
MINEABLE_GOODS: frozenset[str] = frozenset({
    "ALUMINUM_ORE", "IRON_ORE", "COPPER_ORE", "SILVER_ORE", "GOLD_ORE",
    "PLATINUM_ORE", "URANITE_ORE", "MERITIUM_ORE",
    "SILICON_CRYSTALS", "QUARTZ_SAND", "PRECIOUS_STONES", "DIAMONDS",
    "AMMONIA_ICE", "ICE_WATER", "LIQUID_HYDROGEN", "LIQUID_NITROGEN",
    "HYDROCARBON",
})

# Smelted goods frequently confused with their mineable ore counterparts.
SMELTED_GOODS: dict[str, str] = {
    "IRON":      "IRON_ORE",
    "COPPER":    "COPPER_ORE",
    "ALUMINUM":  "ALUMINUM_ORE",
    "GOLD":      "GOLD_ORE",
    "SILVER":    "SILVER_ORE",
    "PLATINUM":  "PLATINUM_ORE",
    "URANITE":   "URANITE_ORE",
    "MERITIUM":  "MERITIUM_ORE",
}

# Maps each mineable good to the asteroid deposit trait(s) that signal its presence.
GOOD_TO_DEPOSIT_TRAITS: dict[str, frozenset[str]] = {
    "IRON_ORE":         frozenset({"COMMON_METAL_DEPOSITS"}),
    "COPPER_ORE":       frozenset({"COMMON_METAL_DEPOSITS"}),
    "ALUMINUM_ORE":     frozenset({"COMMON_METAL_DEPOSITS"}),
    "SILVER_ORE":       frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "GOLD_ORE":         frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "PRECIOUS_STONES":  frozenset({"PRECIOUS_METAL_DEPOSITS"}),
    "DIAMONDS":         frozenset({"PRECIOUS_METAL_DEPOSITS", "RARE_METAL_DEPOSITS"}),
    "PLATINUM_ORE":     frozenset({"RARE_METAL_DEPOSITS"}),
    "URANITE_ORE":      frozenset({"RARE_METAL_DEPOSITS"}),
    "MERITIUM_ORE":     frozenset({"RARE_METAL_DEPOSITS", "PRECIOUS_METAL_DEPOSITS"}),
    "SILICON_CRYSTALS": frozenset({"MINERAL_DEPOSITS"}),
    "QUARTZ_SAND":      frozenset({"MINERAL_DEPOSITS"}),
    "AMMONIA_ICE":      frozenset({"MINERAL_DEPOSITS"}),
    "ICE_WATER":        frozenset({"MINERAL_DEPOSITS"}),
    "LIQUID_HYDROGEN":  frozenset({"EXPLOSIVE_GASES"}),
    "LIQUID_NITROGEN":  frozenset({"EXPLOSIVE_GASES"}),
    "HYDROCARBON":      frozenset({"EXPLOSIVE_GASES"}),
}

# Quality score for each asteroid deposit trait — used when scoring & ranking asteroids.
ASTEROID_TRAIT_SCORES: dict[str, int] = {
    "STRIPPED":                -9999,
    "PRECIOUS_METAL_DEPOSITS":    50,
    "RARE_METAL_DEPOSITS":        40,
    "COMMON_METAL_DEPOSITS":      20,
    "MINERAL_DEPOSITS":           10,
    "DEEP_CRATERS":               15,
    "HOLLOWED_INTERIOR":           5,
    "EXPLOSIVE_GASES":            -5,
    "UNSTABLE_COMPOSITION":       -5,
    "RADIOACTIVE":               -10,
    "DEBRIS_CLUSTER":             -5,
}

# Ship purchase priority (higher score = buy first; -1 = never buy).
# Dynamic safety checks in fleet_manager override positive scores when
# the asteroid or gas giant is too far from any fuel market.
SHIP_SCORES: dict[str, int] = {
    "SHIP_LIGHT_HAULER":    100,  # Seed new teams + traders — always first priority
    "SHIP_MINING_DRONE":     60,  # Cheap miner — no-drift check required
    "SHIP_SIPHON_DRONE":     55,  # Passive gas — no-drift check required
    "SHIP_PROBE":            20,  # Scout/charter — buy when wealthy (>1M cr), explore all systems
    "SHIP_ORE_HOUND":        15,  # Miner — lower priority until groups prove out
    "SHIP_SURVEYOR":         -1,  # Skip — buy goods from market, don't mine
    "SHIP_HEAVY_FREIGHTER":  -1,  # Never buy
    "SHIP_COMMAND_FRIGATE":  -1,  # Already have one
    "SHIP_GAS_DRONE":        -1,  # Never buy
    "SHIP_LIGHT_SHUTTLE":    -1,  # Never buy — tiny cargo, wrong role
}

# Maximum distance from a fuel market for mining/siphon drones to be considered
# safe to deploy. Ships with small fuel tanks (80u) can't cruise home if the
# asteroid/gas-giant is more than this many units from the nearest fuel stop.
NO_DRIFT_DIST_MAX: int = 70

# Mining laser tiers, weakest → strongest.
MINING_MOUNT_TIERS: list[str] = [
    "MOUNT_MINING_LASER_I",
    "MOUNT_MINING_LASER_II",
    "MOUNT_MINING_LASER_III",
]

# Static deposit trait → mineable goods mapping (for DB seeding).
DEPOSIT_GOODS: list[tuple[str, str]] = [
    ("COMMON_METAL_DEPOSITS",   "IRON_ORE"),
    ("COMMON_METAL_DEPOSITS",   "COPPER_ORE"),
    ("COMMON_METAL_DEPOSITS",   "ALUMINUM_ORE"),
    ("COMMON_METAL_DEPOSITS",   "SILICON_CRYSTALS"),
    ("RARE_METAL_DEPOSITS",     "GOLD_ORE"),
    ("RARE_METAL_DEPOSITS",     "SILVER_ORE"),
    ("RARE_METAL_DEPOSITS",     "PLATINUM_ORE"),
    ("RARE_METAL_DEPOSITS",     "URANITE_ORE"),
    ("RARE_METAL_DEPOSITS",     "MERITIUM_ORE"),
    ("PRECIOUS_METAL_DEPOSITS", "GOLD_ORE"),
    ("PRECIOUS_METAL_DEPOSITS", "SILVER_ORE"),
    ("PRECIOUS_METAL_DEPOSITS", "PLATINUM_ORE"),
    ("MINERAL_DEPOSITS",        "SILICON_CRYSTALS"),
    ("MINERAL_DEPOSITS",        "QUARTZ_SAND"),
    ("MINERAL_DEPOSITS",        "AMMONIA_ICE"),
    ("METHANE_ICE_DEPOSITS",    "ICE_CRYSTALS"),
    ("METHANE_ICE_DEPOSITS",    "LIQUID_HYDROGEN"),
    ("METHANE_ICE_DEPOSITS",    "LIQUID_NITROGEN"),
    ("METHANE_ICE_DEPOSITS",    "HYDROCARBON"),
    ("ICE_CRYSTALS",            "ICE_CRYSTALS"),
    ("ICE_CRYSTALS",            "LIQUID_HYDROGEN"),
    ("ICE_CRYSTALS",            "LIQUID_NITROGEN"),
    ("ICE_CRYSTALS",            "AMMONIA_ICE"),
    ("EXPLOSIVE_GASES",         "EXPLOSIVE_COMPOUNDS"),
    ("EXPLOSIVE_GASES",         "HYDROCARBON"),
    ("AMMONIA_ICE_DEPOSITS",    "AMMONIA_ICE"),
    ("SWAMP_DEPOSITS",          "HYDROCARBON"),
    ("SANDY_SHORES",            "QUARTZ_SAND"),
    ("MINERAL_DEPOSITS",        "FAB_MATS"),
]

# Asteroid waypoint types that can be mined.
ASTEROID_TYPES: frozenset[str] = frozenset({
    "ASTEROID",
    "ASTEROID_FIELD",
    "ENGINEERED_ASTEROID",
})

# Default tunables (overridden by Config).
DEFAULT_CREDIT_RESERVE      = 50_000
DEFAULT_MIN_BUY_CREDITS     = 100_000
DEFAULT_REPAIR_THRESHOLD    = 0.80
DEFAULT_MIN_SELL_PRICE      = 30
DEFAULT_MARKET_CACHE_TTL    = 600
DEFAULT_DRY_EXTRACT_THRESHOLD = 5
DEFAULT_CHEAP_BUY_THRESHOLD = 200
DEFAULT_SELL_ROUTING_DIST_COST = 20
DEFAULT_MIN_CONTRACT_PAYOUT = 30_000
DEFAULT_MIN_FUEL_CAPACITY   = 150

# Team composition / purchase caps.
MAX_TRADERS               = 3           # free (non-group) haulers dedicated to arbitrage
PROBE_CREDIT_THRESHOLD    = 1_000_000   # don't buy probes until we have this many credits
MAX_PROBES                = 10          # max probe fleet (one per reachable system)

# Hauler departure thresholds.
HAULER_DEPART_FRACTION   = 0.50   # leave when cargo >= 50% full
HAULER_MAX_WAIT_SECS     = 300    # leave if no new cargo for 5 min
HAULER_MIN_CONTRACT_UNITS = 30    # leave early if we have this many contract units
