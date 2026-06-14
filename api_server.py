#!/usr/bin/env python3
"""
FastAPI backend for the SpaceTraders v1 React GUI.
Mirrors the data exposed by dashboard2.py (9 screens).

Run (from root directory):
    uvicorn api_server:app --reload --port 8000

Or:
    python3 api_server.py
"""
from __future__ import annotations

import json
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import agent as agent_api
import fleet as fleet_api
import universe as universe_api
from client import SpaceTradersError

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="SpaceTraders Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_system() -> str:
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT system_symbol FROM waypoints WHERE system_symbol IS NOT NULL LIMIT 1"
            ).fetchone()
        return row[0] if row else "X1-GK27"
    except Exception:
        return "X1-GK27"


def _system_from_wp(waypoint: str) -> str:
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else _get_system()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    return {
        "ok": True,
        "system": _get_system(),
        "db": str(db.DB_PATH),
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Background-refresh cache
# Keeps agent + ships always hot in memory so requests return instantly.
# A daemon thread refreshes them every REFRESH_INTERVAL seconds.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {}  # key -> value  (no TTL – background thread controls freshness)

REFRESH_INTERVAL = 8  # seconds between background API fetches

def _cache_get(key: str):
    with _cache_lock:
        return _cache.get(key)

def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = value


def _background_refresh():
    """Daemon thread: pre-populates and continuously refreshes live API data."""
    import logging
    log = logging.getLogger("uvicorn.error")
    # Initial warm-up — populate cache before first request arrives
    for key, fn in [("agent", agent_api.get_my_agent), ("ships", fleet_api.get_my_ships)]:
        try:
            _cache_set(key, fn())
            log.info(f"[cache] warmed {key}")
        except Exception as e:
            log.warning(f"[cache] warm-up failed for {key}: {e}")
    while True:
        time.sleep(REFRESH_INTERVAL)
        for key, fn in [("agent", agent_api.get_my_agent), ("ships", fleet_api.get_my_ships)]:
            try:
                _cache_set(key, fn())
            except Exception as e:
                log.warning(f"[cache] refresh failed for {key}: {e}")


@app.on_event("startup")
def start_background_refresh():
    t = threading.Thread(target=_background_refresh, daemon=True, name="cache-refresh")
    t.start()


# ---------------------------------------------------------------------------
# Agent & Ships
# ---------------------------------------------------------------------------

@app.get("/api/agent")
def get_agent():
    cached = _cache_get("agent")
    if cached is not None:
        return cached
    return {}  # cache warming — return empty, background thread will populate shortly


@app.get("/api/ships")
def get_ships():
    cached = _cache_get("ships")
    if cached is not None:
        return cached
    return []  # cache warming — return empty, background thread will populate shortly


@app.get("/api/cph")
def get_cph():
    now = time.time()
    try:
        with db._conn() as con:
            row = con.execute(
                """SELECT
                    SUM(CASE WHEN type='SELL'     AND timestamp > ? THEN  total_price
                             WHEN type='PURCHASE' AND timestamp > ? THEN -total_price
                             ELSE 0 END),
                    SUM(CASE WHEN type='SELL'     AND timestamp > ? THEN  total_price
                             WHEN type='PURCHASE' AND timestamp > ? THEN -total_price
                             ELSE 0 END)
                   FROM market_transactions""",
                (now - 3600, now - 3600, now - 600, now - 600),
            ).fetchone()
        return {"cph_1h": int(row[0] or 0), "cph_10m": int(row[1] or 0)}
    except Exception:
        return {"cph_1h": 0, "cph_10m": 0}


# ---------------------------------------------------------------------------
# Bot Logs (Mission Control screen)
# ---------------------------------------------------------------------------

@app.get("/api/logs")
def get_logs(limit: int = 120):
    try:
        with db._conn() as con:
            rows = con.execute(
                "SELECT timestamp, message FROM bot_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"timestamp": r[0], "message": r[1]} for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@app.get("/api/contracts")
def get_contracts():
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT c.id, c.faction_symbol, c.type, c.accepted, c.fulfilled,
                          c.expiration, c.deadline, c.on_accepted, c.on_fulfilled,
                          cd.trade_symbol, cd.destination_symbol,
                          cd.units_required, cd.units_fulfilled
                   FROM contracts c
                   LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                   ORDER BY c.fulfilled ASC, c.accepted DESC, c.last_updated DESC"""
            ).fetchall()
        contracts: dict = {}
        for r in rows:
            cid = r[0]
            if cid not in contracts:
                contracts[cid] = {
                    "id": cid, "faction_symbol": r[1], "type": r[2],
                    "accepted": bool(r[3]), "fulfilled": bool(r[4]),
                    "expiration": r[5], "deadline": r[6],
                    "on_accepted": r[7], "on_fulfilled": r[8],
                    "deliver": [],
                }
            if r[9]:
                contracts[cid]["deliver"].append({
                    "trade_symbol":       r[9],
                    "destination_symbol": r[10],
                    "units_required":     r[11] or 0,
                    "units_fulfilled":    r[12] or 0,
                })
        return list(contracts.values())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Transactions & Yields
# ---------------------------------------------------------------------------

@app.get("/api/transactions")
def get_transactions(limit: int = 300):
    try:
        with db._conn() as con:
            rows = con.execute(
                "SELECT id, timestamp, type, trade_symbol, units, price_per_unit, "
                "total_price, waypoint_symbol, ship_symbol, trip_id "
                "FROM market_transactions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/yields")
def get_yields(window: str = "20m"):
    now = time.time()
    cutoffs: dict = {"20m": now - 1200, "1h": now - 3600}
    cutoff = cutoffs.get(window)
    try:
        with db._conn() as con:
            if cutoff:
                rows = con.execute(
                    "SELECT trade_symbol, SUM(units), COUNT(*), "
                    "SUM(CASE WHEN survey_signature IS NOT NULL THEN 1 ELSE 0 END) "
                    "FROM extraction_yields WHERE timestamp > ? "
                    "GROUP BY trade_symbol ORDER BY SUM(units) DESC",
                    (cutoff,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT trade_symbol, SUM(units), COUNT(*), "
                    "SUM(CASE WHEN survey_signature IS NOT NULL THEN 1 ELSE 0 END) "
                    "FROM extraction_yields "
                    "GROUP BY trade_symbol ORDER BY SUM(units) DESC"
                ).fetchall()
        return [
            {"trade_symbol": r[0], "total_units": r[1], "count": r[2], "surveyed": r[3]}
            for r in rows
        ]
    except Exception:
        return []


@app.get("/api/trade-runs")
def get_trade_runs(limit: int = 100):
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT
                       mt.trip_id,
                       mt.ship_symbol,
                       mt.trade_symbol,
                       SUM(CASE WHEN mt.type='PURCHASE' THEN mt.units       ELSE 0 END),
                       SUM(CASE WHEN mt.type='PURCHASE' THEN mt.total_price ELSE 0 END),
                       SUM(CASE WHEN mt.type='SELL'     THEN mt.total_price ELSE 0 END),
                       MIN(CASE WHEN mt.type='PURCHASE' THEN mt.timestamp   END),
                       tt.buy_waypoint,
                       tt.sell_waypoint
                   FROM market_transactions mt
                   LEFT JOIN trade_trips tt ON tt.trip_id = mt.trip_id
                   WHERE mt.trip_id IS NOT NULL
                   GROUP BY mt.trip_id
                   ORDER BY COALESCE(
                       MAX(CASE WHEN mt.type='SELL' THEN mt.timestamp END),
                       MIN(mt.timestamp)
                   ) DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "trip_id":       r[0],
                "ship_symbol":   r[1],
                "trade_symbol":  r[2],
                "units":         int(r[3] or 0),
                "buy_cost":      int(r[4] or 0),
                "sell_revenue":  int(r[5] or 0),
                "profit":        int(r[5] or 0) - int(r[4] or 0),
                "timestamp":     r[6],
                "buy_waypoint":  r[7],
                "sell_waypoint": r[8],
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

@app.get("/api/markets")
def get_markets():
    system = _get_system()
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT ml.waypoint_symbol,
                          COUNT(DISTINCT ml.trade_symbol)     AS good_count,
                          COUNT(DISTINCT mp.trade_symbol)     AS price_count,
                          MAX(ml.last_updated)                AS updated,
                          GROUP_CONCAT(
                              CASE WHEN ml.listing_type='EXPORT' THEN ml.trade_symbol END,
                              ', '
                          )                                   AS top_exports
                   FROM market_listings ml
                   JOIN waypoints w ON w.symbol = ml.waypoint_symbol
                                   AND w.system_symbol = ?
                   LEFT JOIN market_prices mp ON mp.waypoint_symbol = ml.waypoint_symbol
                   GROUP BY ml.waypoint_symbol
                   ORDER BY ml.waypoint_symbol""",
                (system,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/markets/{waypoint}/prices")
def get_market_prices(waypoint: str):
    try:
        return db.get_market_prices_for_waypoint(waypoint)
    except Exception:
        return []


@app.post("/api/markets/{waypoint}/refresh")
def refresh_market(waypoint: str):
    try:
        system = _system_from_wp(waypoint)
        data = universe_api.get_market(system, waypoint)
        db.upsert_market_listings(waypoint, data)
        tg = data.get("tradeGoods", [])
        if tg:
            db.upsert_market_prices(waypoint, tg)
        return {"status": "ok", "prices_count": len(tg)}
    except SpaceTradersError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/arbitrage")
def get_arbitrage(min_margin: int = 50):
    system = _get_system()
    try:
        return db.get_arbitrage_opportunities(system, min_margin=min_margin)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Waypoints / Universe
# ---------------------------------------------------------------------------

@app.get("/api/waypoints")
def get_waypoints_endpoint(filter: str = ""):
    system = _get_system()
    try:
        wps = db.get_all_waypoints(system)
        if filter:
            f = filter.upper().strip()
            wps = [
                w for w in wps
                if f in w.get("type", "") or w.get("symbol", "").upper().find(f) >= 0 or any(
                    f in tr.get("symbol", "") for tr in w.get("traits", [])
                )
            ]
        return wps
    except Exception:
        return []


@app.get("/api/waypoints/{symbol}/analysis")
def get_waypoint_analysis(symbol: str):
    system = _system_from_wp(symbol)
    try:
        with db._conn() as con:
            wp_row = con.execute(
                "SELECT symbol, type, x, y FROM waypoints WHERE symbol = ?", (symbol,)
            ).fetchone()
            if not wp_row:
                return {"error": "Waypoint not found"}
            traits_rows = con.execute(
                "SELECT trait_symbol, trait_name FROM waypoint_traits WHERE waypoint_symbol = ?",
                (symbol,),
            ).fetchall()
            trait_symbols = [t[0] for t in traits_rows]
            mineable: dict = {}
            for trait in trait_symbols:
                goods = con.execute(
                    "SELECT trade_symbol FROM deposit_goods WHERE trait_symbol = ?", (trait,)
                ).fetchall()
                for (g,) in goods:
                    mineable.setdefault(g, []).append(trait)
            listings = None
            prices_count = 0
            if "MARKETPLACE" in trait_symbols:
                listing_rows = con.execute(
                    "SELECT trade_symbol, listing_type FROM market_listings "
                    "WHERE waypoint_symbol = ? ORDER BY listing_type, trade_symbol",
                    (symbol,),
                ).fetchall()
                listings = [{"trade_symbol": r[0], "listing_type": r[1]} for r in listing_rows]
                prices_count = len(db.get_market_prices_for_waypoint(symbol))
        arb = db.get_arbitrage_opportunities(system, min_margin=50)
        relevant_arb = [o for o in arb if symbol in (o.get("buy_at"), o.get("sell_at"))]
        return {
            "symbol": wp_row[0],
            "type": wp_row[1],
            "x": wp_row[2],
            "y": wp_row[3],
            "traits": [{"symbol": t[0], "name": t[1]} for t in traits_rows],
            "mineable": [{"trade_symbol": g, "traits": ts} for g, ts in mineable.items()],
            "listings": listings,
            "prices_count": prices_count,
            "arbitrage": relevant_arb,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Surveys
# ---------------------------------------------------------------------------

@app.get("/api/surveys")
def get_surveys():
    try:
        return db.load_active_surveys()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.get("/api/analytics/income")
def get_income():
    now = time.time()
    income = []
    try:
        with db._conn() as con:
            for i in range(12):
                start = now - (i + 1) * 3600
                end   = now - i * 3600
                row = con.execute(
                    """SELECT
                        SUM(CASE WHEN type='SELL'     THEN total_price ELSE 0 END),
                        SUM(CASE WHEN type='PURCHASE' THEN total_price ELSE 0 END)
                       FROM market_transactions WHERE timestamp BETWEEN ? AND ?""",
                    (start, end),
                ).fetchone()
                inc  = int(row[0] or 0)
                spnd = int(row[1] or 0)
                income.append({
                    "hour":   f"{i}-{i+1}h ago",
                    "income": inc,
                    "spend":  spnd,
                    "net":    inc - spnd,
                })
    except Exception:
        pass
    return income


@app.get("/api/analytics/contracts")
def get_contract_history():
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT c.id, cd.trade_symbol, cd.units_required,
                          c.on_accepted, c.on_fulfilled,
                          c.accepted_at, c.fulfilled_at, c.fulfilled
                   FROM contracts c
                   LEFT JOIN contract_deliverables cd ON cd.contract_id = c.id
                   WHERE c.accepted = 1
                   ORDER BY COALESCE(c.fulfilled_at, c.accepted_at) DESC
                   LIMIT 50"""
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Sourcing
# ---------------------------------------------------------------------------

@app.get("/api/sourcing/{good}")
def get_sourcing(good: str):
    system = _get_system()
    try:
        minable = db.can_be_mined(good, system)
        buyable = db.can_be_bought(good, system)
        ore_hint = db.SMELTED_GOODS.get(good)
        return {
            "good": good,
            "minable": minable,
            "buyable": buyable,
            "ore_hint": ore_hint,
        }
    except Exception as e:
        return {"error": str(e), "good": good, "minable": [], "buyable": [], "ore_hint": None}


# ---------------------------------------------------------------------------
# Settings (bot_settings table)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    try:
        raw = db.get_bot_setting("ship_buy_list", "[]")
        try:
            targets = json.loads(raw)
        except Exception:
            targets = []
        return {
            "auto_buy":          db.get_bot_setting("auto_buy_ships", "true").lower() == "true",
            "command_role":      db.get_bot_setting("command_ship_role", "idle"),
            "ship_buy_targets":  targets,
        }
    except Exception as e:
        return {"error": str(e), "auto_buy": False, "command_role": "idle", "ship_buy_targets": []}


@app.post("/api/settings/auto-buy")
def toggle_auto_buy():
    current = db.get_bot_setting("auto_buy_ships", "true").lower() == "true"
    db.set_bot_setting("auto_buy_ships", "false" if current else "true")
    return {"auto_buy": not current}


@app.post("/api/settings/command-role/{role}")
def set_command_role(role: str):
    # Normalize: accept case-insensitive input; map "trader" -> "hauler" for UI compatibility
    normalized = role.lower()
    if normalized == "trader":
        normalized = "hauler"
    if normalized not in ("idle", "hauler"):
        raise HTTPException(status_code=400, detail="role must be idle, hauler, or trader")
    db.set_bot_setting("command_ship_role", normalized)
    return {"command_ship_role": normalized}


@app.post("/api/settings/ship-targets")
def set_ship_targets(body: dict):
    targets = body.get("targets", [])
    db.set_bot_setting("ship_buy_list", json.dumps(targets))
    return {"ship_buy_list": targets}


# ---------------------------------------------------------------------------
# Serve built React app (after: cd gui && npm run build)
# ---------------------------------------------------------------------------

gui_dist = Path(__file__).parent / "gui" / "dist"
if gui_dist.exists():
    app.mount("/", StaticFiles(directory=str(gui_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    db.init_db()
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
