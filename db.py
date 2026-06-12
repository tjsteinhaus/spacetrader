"""
db.py — Persistent SQLite cache for SpaceTraders game state.

Stores waypoints, market listings, market prices, contracts, surveys, and
transaction history so the bot can warm-start without re-fetching everything
and status.py can answer sourcing questions instantly.

Usage:
    import db
    db.init_db()                         # create tables + seed static data
    db.upsert_waypoints(waypoints_list)  # from universe_api.get_waypoints()
    loaded = db.load_market_caches()     # warm-start play.py in-memory dicts
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "game_data.db"

# ---------------------------------------------------------------------------
# Known deposit trait → mineable goods (static SpaceTraders v2 game data)
# IMPORTANT: These are ORE variants. Processed goods (IRON, COPPER, etc.) are
# smelted goods and CANNOT be obtained by mining — only bought from markets.
# ---------------------------------------------------------------------------
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

# Processed/smelted goods that are frequently confused with mineable ores
SMELTED_GOODS = {
    "IRON":      "IRON_ORE",
    "COPPER":    "COPPER_ORE",
    "ALUMINUM":  "ALUMINUM_ORE",
    "GOLD":      "GOLD_ORE",
    "SILVER":    "SILVER_ORE",
    "PLATINUM":  "PLATINUM_ORE",
    "URANITE":   "URANITE_ORE",
    "MERITIUM":  "MERITIUM_ORE",
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn(path: Path | str | None = None) -> sqlite3.Connection:
    p = str(path or DB_PATH)
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS waypoints (
    symbol          TEXT PRIMARY KEY,
    system_symbol   TEXT NOT NULL,
    type            TEXT NOT NULL,
    x               INTEGER NOT NULL DEFAULT 0,
    y               INTEGER NOT NULL DEFAULT 0,
    faction         TEXT,
    last_updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS waypoint_traits (
    waypoint_symbol TEXT NOT NULL,
    trait_symbol    TEXT NOT NULL,
    trait_name      TEXT,
    PRIMARY KEY (waypoint_symbol, trait_symbol)
);

CREATE TABLE IF NOT EXISTS market_listings (
    waypoint_symbol TEXT NOT NULL,
    trade_symbol    TEXT NOT NULL,
    listing_type    TEXT NOT NULL,   -- EXPORT | IMPORT | EXCHANGE
    last_updated    REAL NOT NULL,
    PRIMARY KEY (waypoint_symbol, trade_symbol, listing_type)
);

CREATE TABLE IF NOT EXISTS market_prices (
    waypoint_symbol TEXT NOT NULL,
    trade_symbol    TEXT NOT NULL,
    listing_type    TEXT,
    supply          TEXT,
    activity        TEXT,
    purchase_price  INTEGER,
    sell_price      INTEGER,
    trade_volume    INTEGER,
    last_updated    REAL NOT NULL,
    PRIMARY KEY (waypoint_symbol, trade_symbol)
);

CREATE TABLE IF NOT EXISTS contracts (
    id              TEXT PRIMARY KEY,
    faction_symbol  TEXT,
    type            TEXT,
    accepted        INTEGER NOT NULL DEFAULT 0,
    fulfilled       INTEGER NOT NULL DEFAULT 0,
    expiration      TEXT,
    deadline        TEXT,
    deadline_to_accept TEXT,
    on_accepted     INTEGER,
    on_fulfilled    INTEGER,
    last_updated    REAL NOT NULL,
    accepted_at     REAL,
    fulfilled_at    REAL
);

CREATE TABLE IF NOT EXISTS contract_deliverables (
    contract_id         TEXT NOT NULL,
    trade_symbol        TEXT NOT NULL,
    destination_symbol  TEXT,
    units_required      INTEGER NOT NULL DEFAULT 0,
    units_fulfilled     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contract_id, trade_symbol)
);

CREATE TABLE IF NOT EXISTS surveys (
    signature       TEXT PRIMARY KEY,
    waypoint_symbol TEXT NOT NULL,
    expiration      TEXT NOT NULL,
    last_updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS survey_deposits (
    survey_signature TEXT NOT NULL,
    deposit_symbol   TEXT NOT NULL,
    size             TEXT,
    PRIMARY KEY (survey_signature, deposit_symbol)
);

CREATE TABLE IF NOT EXISTS deposit_goods (
    trait_symbol    TEXT NOT NULL,
    trade_symbol    TEXT NOT NULL,
    PRIMARY KEY (trait_symbol, trade_symbol)
);

CREATE TABLE IF NOT EXISTS market_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    waypoint_symbol TEXT NOT NULL,
    ship_symbol     TEXT,
    trade_symbol    TEXT NOT NULL,
    type            TEXT NOT NULL,   -- PURCHASE | SELL
    units           INTEGER NOT NULL,
    price_per_unit  INTEGER NOT NULL,
    total_price     INTEGER NOT NULL,
    timestamp       REAL NOT NULL,
    trip_id         TEXT             -- groups the BUY + SELL of a single trade run
);

CREATE INDEX IF NOT EXISTS idx_waypoints_system
    ON waypoints(system_symbol);
CREATE INDEX IF NOT EXISTS idx_traits_symbol
    ON waypoint_traits(trait_symbol);
CREATE INDEX IF NOT EXISTS idx_listings_trade
    ON market_listings(trade_symbol, listing_type);
CREATE INDEX IF NOT EXISTS idx_prices_trade
    ON market_prices(trade_symbol);
CREATE INDEX IF NOT EXISTS idx_contracts_active
    ON contracts(fulfilled, accepted);
CREATE INDEX IF NOT EXISTS idx_surveys_wp
    ON surveys(waypoint_symbol, expiration);
CREATE INDEX IF NOT EXISTS idx_txn_timestamp
    ON market_transactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_txn_trip
    ON market_transactions(trip_id);

CREATE TABLE IF NOT EXISTS extraction_yields (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    waypoint_symbol  TEXT NOT NULL,
    ship_symbol      TEXT NOT NULL,
    survey_signature TEXT,          -- NULL when extracted without a survey
    trade_symbol     TEXT NOT NULL,
    units            INTEGER NOT NULL,
    timestamp        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_yields_trade
    ON extraction_yields(trade_symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_yields_survey
    ON extraction_yields(survey_signature);

CREATE TABLE IF NOT EXISTS agent_config (
    callsign      TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    PRIMARY KEY (callsign, key)
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_trips (
    trip_id          TEXT PRIMARY KEY,
    ship_symbol      TEXT NOT NULL,
    trade_symbol     TEXT NOT NULL,
    buy_waypoint     TEXT NOT NULL,
    sell_waypoint    TEXT NOT NULL,
    units            INTEGER NOT NULL,
    buy_total        INTEGER NOT NULL,
    sell_total       INTEGER NOT NULL,
    profit           INTEGER NOT NULL,
    trip_type        TEXT NOT NULL DEFAULT 'arbitrage',
    timestamp        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_trips_ship
    ON trade_trips(ship_symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_trade_trips_good
    ON trade_trips(trade_symbol, timestamp);

CREATE TABLE IF NOT EXISTS bot_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_logs_ts ON bot_logs(timestamp);

CREATE TABLE IF NOT EXISTS credits_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    credits   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credits_ts ON credits_history(timestamp);

CREATE TABLE IF NOT EXISTS contract_progress (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    contract_id     TEXT NOT NULL,
    trade_symbol    TEXT NOT NULL,
    ship_symbol     TEXT NOT NULL,
    units_delivered INTEGER NOT NULL,
    units_fulfilled INTEGER NOT NULL,
    units_required  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_contract ON contract_progress(contract_id, timestamp);

CREATE TABLE IF NOT EXISTS market_price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    waypoint_symbol TEXT NOT NULL,
    ship_symbol     TEXT NOT NULL,
    trade_symbol    TEXT NOT NULL,
    listing_type    TEXT,
    supply          TEXT,
    activity        TEXT,
    purchase_price  INTEGER,
    sell_price      INTEGER,
    trade_volume    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mph_wp ON market_price_history(waypoint_symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_mph_good ON market_price_history(trade_symbol, timestamp);

CREATE TABLE IF NOT EXISTS ship_cargo_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    ship_symbol     TEXT NOT NULL,
    waypoint_symbol TEXT NOT NULL,
    nav_status      TEXT NOT NULL,
    cargo_units     INTEGER NOT NULL DEFAULT 0,
    cargo_capacity  INTEGER NOT NULL DEFAULT 0,
    cargo_goods     TEXT NOT NULL DEFAULT '[]'  -- JSON: [{symbol, units}, ...]
);
CREATE INDEX IF NOT EXISTS idx_scs_ship ON ship_cargo_snapshots(ship_symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_scs_ts   ON ship_cargo_snapshots(timestamp);
"""


def init_db(path: Path | str | None = None) -> None:
    """Create all tables and seed static deposit_goods data. Safe to call multiple times."""
    with _conn(path) as con:
        # Pre-migrate trip_id before executescript creates the index on it
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "market_transactions" in tables:
            txn_cols_pre = {r[1] for r in con.execute("PRAGMA table_info(market_transactions)").fetchall()}
            if "trip_id" not in txn_cols_pre:
                con.execute("ALTER TABLE market_transactions ADD COLUMN trip_id TEXT")
                con.commit()
        con.executescript(_SCHEMA)
        # Migrate existing contracts table — add timing columns if absent
        existing = {r[1] for r in con.execute("PRAGMA table_info(contracts)").fetchall()}
        if "accepted_at" not in existing:
            con.execute("ALTER TABLE contracts ADD COLUMN accepted_at REAL")
        if "fulfilled_at" not in existing:
            con.execute("ALTER TABLE contracts ADD COLUMN fulfilled_at REAL")
        # Migrate credits_history / contract_progress (added later — safe no-op if present)
        tables2 = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "credits_history" not in tables2:
            con.execute("""
                CREATE TABLE IF NOT EXISTS credits_history (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    credits   INTEGER NOT NULL
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_credits_ts ON credits_history(timestamp)")
        if "contract_progress" not in tables2:
            con.execute("""
                CREATE TABLE IF NOT EXISTS contract_progress (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL NOT NULL,
                    contract_id     TEXT NOT NULL,
                    trade_symbol    TEXT NOT NULL,
                    ship_symbol     TEXT NOT NULL,
                    units_delivered INTEGER NOT NULL,
                    units_fulfilled INTEGER NOT NULL,
                    units_required  INTEGER NOT NULL
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_cp_contract ON contract_progress(contract_id, timestamp)")
        if "market_price_history" not in tables2:
            con.execute("""
                CREATE TABLE IF NOT EXISTS market_price_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL NOT NULL,
                    waypoint_symbol TEXT NOT NULL,
                    ship_symbol     TEXT NOT NULL,
                    trade_symbol    TEXT NOT NULL,
                    listing_type    TEXT,
                    supply          TEXT,
                    activity        TEXT,
                    purchase_price  INTEGER,
                    sell_price      INTEGER,
                    trade_volume    INTEGER
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_mph_wp ON market_price_history(waypoint_symbol, timestamp)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_mph_good ON market_price_history(trade_symbol, timestamp)")
        if "ship_cargo_snapshots" not in tables2:
            con.execute("""
                CREATE TABLE IF NOT EXISTS ship_cargo_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL NOT NULL,
                    ship_symbol     TEXT NOT NULL,
                    waypoint_symbol TEXT NOT NULL,
                    nav_status      TEXT NOT NULL,
                    cargo_units     INTEGER NOT NULL DEFAULT 0,
                    cargo_capacity  INTEGER NOT NULL DEFAULT 0,
                    cargo_goods     TEXT NOT NULL DEFAULT '[]'
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_scs_ship ON ship_cargo_snapshots(ship_symbol, timestamp)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_scs_ts   ON ship_cargo_snapshots(timestamp)")
        # Seed deposit_goods (INSERT OR IGNORE so re-runs are idempotent)
        con.executemany(
            "INSERT OR IGNORE INTO deposit_goods (trait_symbol, trade_symbol) VALUES (?, ?)",
            DEPOSIT_GOODS,
        )


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def save_agent_config(callsign: str, config: dict[str, str], path: Path | str | None = None) -> None:
    """Persist key/value config for an agent callsign."""
    with _conn(path) as con:
        con.executemany(
            "INSERT OR REPLACE INTO agent_config (callsign, key, value) VALUES (?, ?, ?)",
            [(callsign, k, v) for k, v in config.items()],
        )


def load_agent_config(callsign: str, path: Path | str | None = None) -> dict[str, str]:
    """Load saved config for an agent callsign. Returns {} if none stored."""
    with _conn(path) as con:
        rows = con.execute(
            "SELECT key, value FROM agent_config WHERE callsign = ?",
            (callsign,),
        ).fetchall()
    return {k: v for k, v in rows}


def get_bot_setting(key: str, default: str = "") -> str:
    """Read a single bot control setting; returns default if not set."""
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_bot_setting(key: str, value: str) -> None:
    """Persist a bot control setting to the database."""
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def write_log(message: str) -> None:
    """Write a log entry to the bot_logs table for dashboard consumption."""
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO bot_logs (timestamp, message) VALUES (?, ?)",
                (time.time(), message),
            )
            # Keep only the last 2000 entries to prevent unbounded growth
            con.execute(
                "DELETE FROM bot_logs WHERE id NOT IN "
                "(SELECT id FROM bot_logs ORDER BY id DESC LIMIT 2000)"
            )
    except Exception:
        pass  # Never crash the bot over a log write failure


def record_credits(credits: int) -> None:
    """Snapshot current credit balance for time-series charting."""
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO credits_history (timestamp, credits) VALUES (?, ?)",
                (time.time(), credits),
            )
            # Keep ~30 days at 1-minute resolution (43,200 rows)
            con.execute(
                "DELETE FROM credits_history WHERE id NOT IN "
                "(SELECT id FROM credits_history ORDER BY id DESC LIMIT 43200)"
            )
    except Exception:
        pass


def record_delivery(
    contract_id: str,
    trade_symbol: str,
    ship_symbol: str,
    units_delivered: int,
    units_fulfilled: int,
    units_required: int,
) -> None:
    """Log a single contract delivery event."""
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO contract_progress "
                "(timestamp, contract_id, trade_symbol, ship_symbol, "
                " units_delivered, units_fulfilled, units_required) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), contract_id, trade_symbol, ship_symbol,
                 units_delivered, units_fulfilled, units_required),
            )
    except Exception:
        pass


def record_ship_cargo(
    ship_symbol: str,
    waypoint_symbol: str,
    nav_status: str,
    cargo_units: int,
    cargo_capacity: int,
    cargo_goods: list[dict],
) -> None:
    """Snapshot a ship's cargo hold to the DB. Called each status table print."""
    import json
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO ship_cargo_snapshots "
                "(timestamp, ship_symbol, waypoint_symbol, nav_status, "
                " cargo_units, cargo_capacity, cargo_goods) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), ship_symbol, waypoint_symbol, nav_status,
                 cargo_units, cargo_capacity, json.dumps(cargo_goods)),
            )
            # Keep the last 10,000 rows per ship to avoid unbounded growth
            con.execute(
                "DELETE FROM ship_cargo_snapshots WHERE id NOT IN "
                "(SELECT id FROM ship_cargo_snapshots ORDER BY id DESC LIMIT 50000)"
            )
    except Exception:
        pass


def log_market_visit(
    waypoint_symbol: str,
    ship_symbol: str,
    trade_goods: list[dict],
) -> None:
    """Append a timestamped snapshot of all trade goods to market_price_history."""
    if not trade_goods:
        return
    now = time.time()
    try:
        with _conn() as con:
            con.executemany(
                """INSERT INTO market_price_history
                   (timestamp, waypoint_symbol, ship_symbol, trade_symbol,
                    listing_type, supply, activity, purchase_price, sell_price, trade_volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        now,
                        waypoint_symbol,
                        ship_symbol,
                        g.get("symbol", ""),
                        g.get("type"),
                        g.get("supply"),
                        g.get("activity"),
                        g.get("purchasePrice"),
                        g.get("sellPrice"),
                        g.get("tradeVolume"),
                    )
                    for g in trade_goods if g.get("symbol")
                ],
            )
            # Keep 90 days at ~hourly resolution per waypoint (no hard cap — indexes keep it fast)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def upsert_waypoints(waypoints: list[dict[str, Any]], path: Path | str | None = None) -> int:
    """
    Insert or update waypoints from a universe_api.get_waypoints() response.
    Also upserts their traits.
    Returns the number of waypoints processed.
    """
    now = time.time()
    with _conn(path) as con:
        for wp in waypoints:
            symbol = wp.get("symbol", "")
            if not symbol:
                continue
            faction_sym = (wp.get("faction") or {}).get("symbol")
            con.execute(
                """INSERT OR REPLACE INTO waypoints
                   (symbol, system_symbol, type, x, y, faction, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol,
                    wp.get("systemSymbol", ""),
                    wp.get("type", ""),
                    wp.get("x", 0),
                    wp.get("y", 0),
                    faction_sym,
                    now,
                ),
            )
            for trait in wp.get("traits", []):
                t_sym = trait.get("symbol", "")
                if t_sym:
                    con.execute(
                        """INSERT OR REPLACE INTO waypoint_traits
                           (waypoint_symbol, trait_symbol, trait_name)
                           VALUES (?, ?, ?)""",
                        (symbol, t_sym, trait.get("name", "")),
                    )
    return len(waypoints)


def upsert_market_listings(
    waypoint_symbol: str,
    market_data: dict[str, Any],
    path: Path | str | None = None,
) -> None:
    """
    Upsert market listings (exports / imports / exchange) from a
    universe_api.get_market() response.  Does NOT require a ship to be docked.
    Prices (tradeGoods) are handled separately by upsert_market_prices().
    """
    now = time.time()
    with _conn(path) as con:
        for listing_type, key in [("EXPORT", "exports"), ("IMPORT", "imports"), ("EXCHANGE", "exchange")]:
            for good in market_data.get(key, []):
                sym = good.get("symbol", "")
                if sym:
                    con.execute(
                        """INSERT OR REPLACE INTO market_listings
                           (waypoint_symbol, trade_symbol, listing_type, last_updated)
                           VALUES (?, ?, ?, ?)""",
                        (waypoint_symbol, sym, listing_type, now),
                    )


def upsert_market_prices(
    waypoint_symbol: str,
    trade_goods: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """
    Upsert live market prices from a get_market()["tradeGoods"] list.
    Requires a ship to be docked at the waypoint to get full price data.
    """
    now = time.time()
    with _conn(path) as con:
        for g in trade_goods:
            sym = g.get("symbol", "")
            if not sym:
                continue
            con.execute(
                """INSERT OR REPLACE INTO market_prices
                   (waypoint_symbol, trade_symbol, listing_type, supply, activity,
                    purchase_price, sell_price, trade_volume, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    waypoint_symbol,
                    sym,
                    g.get("type"),
                    g.get("supply"),
                    g.get("activity"),
                    g.get("purchasePrice"),
                    g.get("sellPrice"),
                    g.get("tradeVolume"),
                    now,
                ),
            )


def upsert_contract(contract: dict[str, Any], path: Path | str | None = None) -> None:
    """
    Upsert a contract and its deliverables from a contracts_api response dict.
    accepted_at and fulfilled_at are set once on first transition and never overwritten.
    """
    now = time.time()
    cid = contract.get("id", "")
    if not cid:
        return
    terms = contract.get("terms", {})
    payment = terms.get("payment", {})
    accepted_at  = now if contract.get("accepted")  else None
    fulfilled_at = now if contract.get("fulfilled") else None
    with _conn(path) as con:
        con.execute(
            """INSERT INTO contracts
               (id, faction_symbol, type, accepted, fulfilled, expiration,
                deadline, deadline_to_accept, on_accepted, on_fulfilled, last_updated,
                accepted_at, fulfilled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 faction_symbol     = excluded.faction_symbol,
                 type               = excluded.type,
                 accepted           = excluded.accepted,
                 fulfilled          = excluded.fulfilled,
                 expiration         = excluded.expiration,
                 deadline           = excluded.deadline,
                 deadline_to_accept = excluded.deadline_to_accept,
                 on_accepted        = excluded.on_accepted,
                 on_fulfilled       = excluded.on_fulfilled,
                 last_updated       = excluded.last_updated,
                 accepted_at  = COALESCE(contracts.accepted_at,  excluded.accepted_at),
                 fulfilled_at = COALESCE(contracts.fulfilled_at, excluded.fulfilled_at)""",
            (
                cid,
                contract.get("factionSymbol"),
                contract.get("type"),
                1 if contract.get("accepted") else 0,
                1 if contract.get("fulfilled") else 0,
                contract.get("expiration"),
                terms.get("deadline"),
                contract.get("deadlineToAccept"),
                payment.get("onAccepted"),
                payment.get("onFulfilled"),
                now,
                accepted_at,
                fulfilled_at,
            ),
        )
        for d in terms.get("deliver", []):
            trade_sym = d.get("tradeSymbol", "")
            if trade_sym:
                con.execute(
                    """INSERT OR REPLACE INTO contract_deliverables
                       (contract_id, trade_symbol, destination_symbol,
                        units_required, units_fulfilled)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        cid,
                        trade_sym,
                        d.get("destinationSymbol"),
                        d.get("unitsRequired", 0),
                        d.get("unitsFulfilled", 0),
                    ),
                )


def upsert_survey(survey: dict[str, Any], path: Path | str | None = None) -> None:
    """
    Upsert a survey and its deposits from a fleet_api.survey() response.
    survey is a single survey dict (not the top-level surveys list).
    """
    sig = survey.get("signature", "")
    if not sig:
        return
    now = time.time()
    with _conn(path) as con:
        con.execute(
            """INSERT OR REPLACE INTO surveys
               (signature, waypoint_symbol, expiration, last_updated)
               VALUES (?, ?, ?, ?)""",
            (sig, survey.get("symbol", ""), survey.get("expiration", ""), now),
        )
        for deposit in survey.get("deposits", []):
            dep_sym = deposit.get("symbol", "")
            if dep_sym:
                con.execute(
                    """INSERT OR REPLACE INTO survey_deposits
                       (survey_signature, deposit_symbol, size)
                       VALUES (?, ?, ?)""",
                    (sig, dep_sym, deposit.get("size")),
                )


def log_transaction(
    waypoint_symbol: str,
    ship_symbol: str,
    trade_symbol: str,
    txn_type: str,
    units: int,
    price_per_unit: int,
    total_price: int,
    path: Path | str | None = None,
    trip_id: str | None = None,
) -> None:
    """Append a buy or sell transaction to market_transactions for later analysis."""
    with _conn(path) as con:
        con.execute(
            """INSERT INTO market_transactions
               (waypoint_symbol, ship_symbol, trade_symbol, type,
                units, price_per_unit, total_price, timestamp, trip_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (waypoint_symbol, ship_symbol, trade_symbol, txn_type,
             units, price_per_unit, total_price, time.time(), trip_id),
        )


def log_trade_trip(
    trip_id: str,
    ship_symbol: str,
    trade_symbol: str,
    buy_waypoint: str,
    sell_waypoint: str,
    units: int,
    buy_total: int,
    sell_total: int,
    trip_type: str = "arbitrage",
    path: Path | str | None = None,
) -> None:
    """Record a completed arbitrage round-trip for P&L tracking and ML training."""
    with _conn(path) as con:
        con.execute(
            """INSERT OR REPLACE INTO trade_trips
               (trip_id, ship_symbol, trade_symbol, buy_waypoint, sell_waypoint,
                units, buy_total, sell_total, profit, trip_type, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trip_id, ship_symbol, trade_symbol, buy_waypoint, sell_waypoint,
             units, buy_total, sell_total, sell_total - buy_total, trip_type, time.time()),
        )


def get_trade_stats(
    ship_symbol: str | None = None,
    since_hours: float = 24.0,
    path: Path | str | None = None,
) -> list[dict]:
    """Return per-ship, per-good P&L summary for the last N hours."""
    cutoff = time.time() - since_hours * 3600
    params: list = []
    where = "WHERE timestamp >= ?"
    params.append(cutoff)
    if ship_symbol:
        where += " AND ship_symbol = ?"
        params.append(ship_symbol)
    with _conn(path) as con:
        rows = con.execute(
            f"""SELECT ship_symbol, trade_symbol, trip_type,
                       COUNT(*) trips, SUM(units) total_units,
                       SUM(buy_total) total_cost, SUM(sell_total) total_revenue,
                       SUM(profit) total_profit,
                       AVG(profit * 1.0 / NULLIF(buy_total, 0)) avg_roi
                FROM trade_trips {where}
                GROUP BY ship_symbol, trade_symbol, trip_type
                ORDER BY total_profit DESC""",
            params,
        ).fetchall()
    return [
        {
            "ship": r[0], "good": r[1], "type": r[2], "trips": r[3],
            "units": r[4], "cost": r[5], "revenue": r[6],
            "profit": r[7], "roi": round((r[8] or 0.0) * 100, 1),
        }
        for r in rows
    ]


def log_extraction(
    waypoint_symbol: str,
    ship_symbol: str,
    survey_signature: str | None,
    trade_symbol: str,
    units: int,
    path: Path | str | None = None,
) -> None:
    """Log a single extraction yield for later survey-quality analysis."""
    with _conn(path) as con:
        con.execute(
            """INSERT INTO extraction_yields
               (waypoint_symbol, ship_symbol, survey_signature, trade_symbol, units, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (waypoint_symbol, ship_symbol, survey_signature, trade_symbol, units, time.time()),
        )


# ---------------------------------------------------------------------------
# Cache loaders (warm-start play.py in-memory dicts from DB)
# ---------------------------------------------------------------------------

def load_market_caches(
    system: str,
    cache_ttl: float = 600.0,
    path: Path | str | None = None,
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]], dict[str, dict[str, int]], dict[str, float]]:
    """
    Load market intelligence from DB to warm-start play.py's in-memory caches.

    Returns:
        known_markets   — list of waypoint symbols with MARKETPLACE trait
        good_exporters  — {trade_symbol: [waypoints that sell/exchange it]}
        good_buyers     — {trade_symbol: [waypoints that import/exchange it]}
        market_cache    — {waypoint: {good: sell_price, _buy_good: purchase_price}}
        market_cache_ts — {waypoint: last_updated unix timestamp}
    """
    with _conn(path) as con:
        # Known markets
        rows = con.execute(
            """SELECT DISTINCT waypoint_symbol FROM waypoint_traits
               WHERE trait_symbol = 'MARKETPLACE'
               AND waypoint_symbol IN (
                   SELECT symbol FROM waypoints WHERE system_symbol = ?
               )""",
            (system,),
        ).fetchall()
        known_markets: list[str] = [r[0] for r in rows]

        # Good exporters (EXPORT + EXCHANGE → things we can buy)
        exporter_rows = con.execute(
            """SELECT ml.trade_symbol, ml.waypoint_symbol
               FROM market_listings ml
               JOIN waypoints w ON w.symbol = ml.waypoint_symbol
               WHERE w.system_symbol = ?
               AND ml.listing_type IN ('EXPORT', 'EXCHANGE')""",
            (system,),
        ).fetchall()
        good_exporters: dict[str, list[str]] = {}
        for trade_sym, wp in exporter_rows:
            good_exporters.setdefault(trade_sym, []).append(wp)

        # Good buyers (IMPORT + EXCHANGE → things we can sell)
        buyer_rows = con.execute(
            """SELECT ml.trade_symbol, ml.waypoint_symbol
               FROM market_listings ml
               JOIN waypoints w ON w.symbol = ml.waypoint_symbol
               WHERE w.system_symbol = ?
               AND ml.listing_type IN ('IMPORT', 'EXCHANGE')""",
            (system,),
        ).fetchall()
        good_buyers: dict[str, list[str]] = {}
        for trade_sym, wp in buyer_rows:
            good_buyers.setdefault(trade_sym, []).append(wp)

        # Market prices (only those still within TTL)
        cutoff = time.time() - cache_ttl
        price_rows = con.execute(
            """SELECT mp.waypoint_symbol, mp.trade_symbol, mp.sell_price,
                      mp.purchase_price, mp.last_updated
               FROM market_prices mp
               JOIN waypoints w ON w.symbol = mp.waypoint_symbol
               WHERE w.system_symbol = ?
               AND mp.last_updated > ?""",
            (system, cutoff),
        ).fetchall()
        market_cache: dict[str, dict[str, int]] = {}
        market_cache_ts: dict[str, float] = {}
        for wp, trade_sym, sell_p, buy_p, ts in price_rows:
            if wp not in market_cache:
                market_cache[wp] = {}
            if sell_p is not None:
                market_cache[wp][trade_sym] = sell_p
            if buy_p is not None:
                market_cache[wp][f"_buy_{trade_sym}"] = buy_p
            if market_cache_ts.get(wp, 0) < ts:
                market_cache_ts[wp] = ts

    return known_markets, good_exporters, good_buyers, market_cache, market_cache_ts


def load_active_surveys(path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Load non-expired surveys from DB, returned as play.py-compatible survey dicts.
    Expired surveys are pruned from the DB automatically.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    with _conn(path) as con:
        # Prune expired first
        con.execute("DELETE FROM survey_deposits WHERE survey_signature IN "
                    "(SELECT signature FROM surveys WHERE expiration < ?)", (now_iso,))
        con.execute("DELETE FROM surveys WHERE expiration < ?", (now_iso,))

        rows = con.execute(
            "SELECT signature, waypoint_symbol, expiration FROM surveys",
        ).fetchall()
        surveys: list[dict[str, Any]] = []
        for sig, wp, exp in rows:
            dep_rows = con.execute(
                "SELECT deposit_symbol, size FROM survey_deposits WHERE survey_signature = ?",
                (sig,),
            ).fetchall()
            surveys.append({
                "signature": sig,
                "symbol": wp,
                "expiration": exp,
                "deposits": [{"symbol": d[0], "size": d[1]} for d in dep_rows],
            })
    return surveys


# ---------------------------------------------------------------------------
# Query helpers (used by status.py)
# ---------------------------------------------------------------------------

def can_be_mined(good: str, system: str, path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Return list of waypoints in `system` where `good` can be mined.
    Each result: {waypoint_symbol, waypoint_type, trait_symbol}

    Returns [] if the good is not in deposit_goods (i.e. it's a processed good).
    """
    with _conn(path) as con:
        rows = con.execute(
            """SELECT w.symbol, w.type, dg.trait_symbol
               FROM deposit_goods dg
               JOIN waypoint_traits wt ON wt.trait_symbol = dg.trait_symbol
               JOIN waypoints w ON w.symbol = wt.waypoint_symbol
               WHERE dg.trade_symbol = ?
               AND w.system_symbol = ?""",
            (good, system),
        ).fetchall()
    return [{"waypoint_symbol": r[0], "waypoint_type": r[1], "trait_symbol": r[2]} for r in rows]


def can_be_bought(good: str, system: str, path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Return list of markets in `system` that export or exchange `good`
    (i.e. we can purchase it there).
    Each result: {waypoint_symbol, listing_type, purchase_price (or None), last_price_update}
    """
    with _conn(path) as con:
        rows = con.execute(
            """SELECT ml.waypoint_symbol, ml.listing_type,
                      mp.purchase_price, mp.supply, mp.last_updated
               FROM market_listings ml
               JOIN waypoints w ON w.symbol = ml.waypoint_symbol
               LEFT JOIN market_prices mp
                   ON mp.waypoint_symbol = ml.waypoint_symbol
                   AND mp.trade_symbol = ml.trade_symbol
               WHERE ml.trade_symbol = ?
               AND w.system_symbol = ?
               AND ml.listing_type IN ('EXPORT', 'EXCHANGE')""",
            (good, system),
        ).fetchall()
    return [
        {
            "waypoint_symbol": r[0],
            "listing_type": r[1],
            "purchase_price": r[2],
            "supply": r[3],
            "last_price_update": r[4],
        }
        for r in rows
    ]


def get_active_contracts(path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Return all non-fulfilled contracts with their deliverables.
    """
    with _conn(path) as con:
        contracts = con.execute(
            """SELECT id, faction_symbol, type, accepted, fulfilled,
                      expiration, deadline, on_accepted, on_fulfilled, last_updated
               FROM contracts
               WHERE fulfilled = 0
               ORDER BY last_updated DESC""",
        ).fetchall()
        result: list[dict[str, Any]] = []
        for c in contracts:
            cid = c[0]
            deliverables = con.execute(
                """SELECT trade_symbol, destination_symbol,
                          units_required, units_fulfilled
                   FROM contract_deliverables
                   WHERE contract_id = ?""",
                (cid,),
            ).fetchall()
            result.append({
                "id": cid,
                "faction_symbol": c[1],
                "type": c[2],
                "accepted": bool(c[3]),
                "fulfilled": bool(c[4]),
                "expiration": c[5],
                "deadline": c[6],
                "on_accepted": c[7],
                "on_fulfilled": c[8],
                "last_updated": c[9],
                "deliver": [
                    {
                        "trade_symbol": d[0],
                        "destination_symbol": d[1],
                        "units_required": d[2],
                        "units_fulfilled": d[3],
                    }
                    for d in deliverables
                ],
            })
    return result


def get_all_waypoints(system: str, path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return all waypoints in a system with their traits."""
    with _conn(path) as con:
        wps = con.execute(
            "SELECT symbol, type, x, y, faction FROM waypoints WHERE system_symbol = ?",
            (system,),
        ).fetchall()
        result = []
        for wp in wps:
            sym = wp[0]
            traits = con.execute(
                "SELECT trait_symbol, trait_name FROM waypoint_traits WHERE waypoint_symbol = ?",
                (sym,),
            ).fetchall()
            result.append({
                "symbol": sym,
                "type": wp[1],
                "x": wp[2],
                "y": wp[3],
                "faction": wp[4],
                "traits": [{"symbol": t[0], "name": t[1]} for t in traits],
            })
    return result


def get_market_prices_for_waypoint(
    waypoint_symbol: str,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return all cached prices for a specific waypoint."""
    with _conn(path) as con:
        rows = con.execute(
            """SELECT trade_symbol, listing_type, supply, activity,
                      purchase_price, sell_price, trade_volume, last_updated
               FROM market_prices
               WHERE waypoint_symbol = ?
               ORDER BY trade_symbol""",
            (waypoint_symbol,),
        ).fetchall()
    return [
        {
            "trade_symbol": r[0],
            "listing_type": r[1],
            "supply": r[2],
            "activity": r[3],
            "purchase_price": r[4],
            "sell_price": r[5],
            "trade_volume": r[6],
            "last_updated": r[7],
        }
        for r in rows
    ]


def get_arbitrage_opportunities(
    system: str,
    min_margin: int = 100,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """
    Find goods that can be bought cheap at one market and sold for more at another
    within the same system. Only returns pairs where live price data exists for both
    sides (ship must have docked at those markets). Ordered by margin descending.
    """
    with _conn(path) as con:
        rows = con.execute(
            """
            SELECT
                buy_p.waypoint_symbol            AS buy_at,
                sell_p.waypoint_symbol           AS sell_at,
                buy_p.trade_symbol,
                buy_p.purchase_price             AS buy_price,
                sell_p.sell_price,
                sell_p.sell_price - buy_p.purchase_price  AS margin,
                ROUND(
                    100.0 * (sell_p.sell_price - buy_p.purchase_price)
                    / buy_p.purchase_price, 1
                )                                AS pct_margin,
                buy_p.supply                     AS buy_supply,
                sell_p.supply                    AS sell_supply,
                MIN(buy_p.last_updated, sell_p.last_updated) AS oldest_data
            FROM market_prices buy_p
            JOIN market_listings buy_l
                ON  buy_l.waypoint_symbol = buy_p.waypoint_symbol
                AND buy_l.trade_symbol    = buy_p.trade_symbol
                AND buy_l.listing_type   IN ('EXPORT', 'EXCHANGE')
            JOIN waypoints bw
                ON  bw.symbol        = buy_p.waypoint_symbol
                AND bw.system_symbol = ?
            JOIN market_prices sell_p
                ON  sell_p.trade_symbol     = buy_p.trade_symbol
                AND sell_p.waypoint_symbol != buy_p.waypoint_symbol
            JOIN market_listings sell_l
                ON  sell_l.waypoint_symbol = sell_p.waypoint_symbol
                AND sell_l.trade_symbol    = sell_p.trade_symbol
                AND sell_l.listing_type   IN ('IMPORT', 'EXCHANGE')
            JOIN waypoints sw
                ON  sw.symbol        = sell_p.waypoint_symbol
                AND sw.system_symbol = ?
            WHERE buy_p.purchase_price  > 0
              AND sell_p.sell_price     > 0
              AND sell_p.sell_price - buy_p.purchase_price >= ?
            ORDER BY margin DESC
            LIMIT 30
            """,
            (system, system, min_margin),
        ).fetchall()
    return [
        {
            "buy_at":       r[0],
            "sell_at":      r[1],
            "trade_symbol": r[2],
            "buy_price":    r[3],
            "sell_price":   r[4],
            "margin":       r[5],
            "pct_margin":   r[6],
            "buy_supply":   r[7],
            "sell_supply":  r[8],
            "oldest_data":  r[9],
        }
        for r in rows
    ]


def get_db_stats(path: Path | str | None = None) -> dict[str, Any]:
    """Return row counts and last-updated timestamps for each table."""
    with _conn(path) as con:
        stats: dict[str, Any] = {}
        for table in ("waypoints", "waypoint_traits", "market_listings",
                      "market_prices", "contracts", "surveys",
                      "market_transactions", "extraction_yields"):
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats[table] = {"count": count}
        # Last market update
        row = con.execute(
            "SELECT MAX(last_updated) FROM market_listings"
        ).fetchone()
        stats["last_market_refresh"] = row[0] if row else None
        row = con.execute(
            "SELECT MAX(last_updated) FROM contracts"
        ).fetchone()
        stats["last_contract_refresh"] = row[0] if row else None
    return stats
