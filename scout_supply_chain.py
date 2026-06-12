"""
scout_supply_chain.py — Map the SpaceTraders production chain for your system.

Calls the global /market/supply-chain endpoint (no ship movement required) and
cross-references with your local DB to show:
  - All known production steps (input goods → output good)
  - Which inputs are locally available (minable or buyable in your system)
  - Whether a complete chain exists locally
  - Waypoints in your system that have REFINERY / FABRICATOR / INDUSTRIAL traits

Run: python3 scout_supply_chain.py
     python3 scout_supply_chain.py --good IRON      # filter to chains producing IRON
     python3 scout_supply_chain.py --complete-only  # only show fully local chains
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import universe as universe_api
import db
from client import SpaceTradersError


# Waypoint traits that indicate manufacturing capability
PRODUCTION_TRAITS = {
    "REFINERY":          "Smelts ores → metals",
    "FABRICATOR":        "Fabricates metals/materials → components",
    "INDUSTRIAL":        "General manufacturing",
    "OUTFITTING":        "Produces ship components",
    "RESEARCH_FACILITY": "Produces advanced/rare goods",
    "CHEMICAL_LABS":     "Produces chemicals and reagents",
    "NANITE_REFINERY":   "Produces nanites and advanced materials",
}

# Tier labels for display ordering
TIER_LABELS = {
    0: "Raw Extraction",
    1: "Tier 1 — Smelting / Refining",
    2: "Tier 2 — Fabrication",
    3: "Tier 3 — Advanced Components",
}


def _get_system() -> str:
    """Detect current system from DB."""
    try:
        with db._conn() as con:
            row = con.execute(
                "SELECT system_symbol FROM waypoints WHERE system_symbol IS NOT NULL LIMIT 1"
            ).fetchone()
        return row[0] if row else "X1-GK27"
    except Exception:
        return "X1-GK27"


def _local_exporters(system: str) -> dict[str, list[str]]:
    """Return {trade_symbol: [waypoint_symbols]} for all EXPORT listings in the system."""
    exporters: dict[str, list[str]] = {}
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT ml.trade_symbol, ml.waypoint_symbol
                   FROM market_listings ml
                   JOIN waypoints w ON w.symbol = ml.waypoint_symbol
                   WHERE w.system_symbol = ? AND ml.listing_type = 'EXPORT'""",
                (system,),
            ).fetchall()
        for trade_symbol, wp in rows:
            exporters.setdefault(trade_symbol, []).append(wp)
    except Exception:
        pass
    return exporters


def _local_importers(system: str) -> dict[str, list[str]]:
    """Return {trade_symbol: [waypoint_symbols]} for all IMPORT listings in the system."""
    importers: dict[str, list[str]] = {}
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT ml.trade_symbol, ml.waypoint_symbol
                   FROM market_listings ml
                   JOIN waypoints w ON w.symbol = ml.waypoint_symbol
                   WHERE w.system_symbol = ? AND ml.listing_type = 'IMPORT'""",
                (system,),
            ).fetchall()
        for trade_symbol, wp in rows:
            importers.setdefault(trade_symbol, []).append(wp)
    except Exception:
        pass
    return importers


def _mineable_goods(system: str) -> set[str]:
    """Return set of goods that can be mined in the system (from deposit_goods + local asteroids)."""
    goods: set[str] = set()
    try:
        with db._conn() as con:
            # Traits present on asteroids in this system
            rows = con.execute(
                """SELECT DISTINCT wt.trait_symbol
                   FROM waypoint_traits wt
                   JOIN waypoints w ON w.symbol = wt.waypoint_symbol
                   WHERE w.system_symbol = ?
                     AND w.type IN ('ASTEROID','ASTEROID_FIELD','ENGINEERED_ASTEROID')""",
                (system,),
            ).fetchall()
            traits_present = {r[0] for r in rows}

            # All goods from those traits
            for trait in traits_present:
                dep_rows = con.execute(
                    "SELECT trade_symbol FROM deposit_goods WHERE trait_symbol = ?",
                    (trait,),
                ).fetchall()
                for (g,) in dep_rows:
                    goods.add(g)
    except Exception:
        pass
    return goods


def _production_waypoints(system: str) -> list[dict]:
    """Return waypoints in system that have any production trait."""
    results = []
    try:
        with db._conn() as con:
            rows = con.execute(
                """SELECT w.symbol, w.type, w.x, w.y, wt.trait_symbol
                   FROM waypoints w
                   JOIN waypoint_traits wt ON wt.waypoint_symbol = w.symbol
                   WHERE w.system_symbol = ?
                     AND wt.trait_symbol IN ({})""".format(
                    ",".join("?" * len(PRODUCTION_TRAITS))
                ),
                (system, *PRODUCTION_TRAITS.keys()),
            ).fetchall()
        # Group by waypoint
        wp_map: dict[str, dict] = {}
        for sym, wp_type, x, y, trait in rows:
            if sym not in wp_map:
                wp_map[sym] = {"symbol": sym, "type": wp_type, "x": x, "y": y, "traits": []}
            wp_map[sym]["traits"].append(trait)
        results = list(wp_map.values())
    except Exception:
        pass
    return results


def _estimate_tier(inputs: list[str], output: str) -> int:
    """Rough tier estimation based on input goods."""
    raw_ores = {
        "IRON_ORE", "COPPER_ORE", "ALUMINUM_ORE", "GOLD_ORE", "SILVER_ORE",
        "PLATINUM_ORE", "URANITE_ORE", "MERITIUM_ORE", "SILICON_CRYSTALS",
        "QUARTZ_SAND", "AMMONIA_ICE", "ICE_WATER", "LIQUID_HYDROGEN",
        "LIQUID_NITROGEN", "HYDROCARBON", "FAB_MATS",
    }
    tier1_metals = {
        "IRON", "COPPER", "ALUMINUM", "GOLD", "SILVER", "PLATINUM",
        "URANITE", "MERITIUM",
    }
    all_inputs = set(inputs)
    if all_inputs <= raw_ores:
        return 1
    if all_inputs <= (raw_ores | tier1_metals):
        return 2
    return 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Map SpaceTraders production chains")
    parser.add_argument("--good", default="", help="Filter to chains producing this good")
    parser.add_argument("--complete-only", action="store_true",
                        help="Only show chains where all inputs are locally available")
    parser.add_argument("--system", default="", help="Override system symbol")
    args = parser.parse_args()

    system = args.system.upper() if args.system else _get_system()
    print(f"\nSpaceTraders Supply Chain Scout — System: {system}")
    print("=" * 72)

    # Fetch global supply chain
    print("Fetching supply chain from API…")
    try:
        data = universe_api.get_supply_chain()
    except SpaceTradersError as e:
        print(f"  ERROR: {e}")
        print("  Make sure your token is set in .env")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    # The API returns a list of production step objects.
    # Each step has: produced_good, inputs (list of {good, units}), produced_units
    # Shape varies slightly by server version — handle both common shapes.
    steps: list[dict] = []
    if isinstance(data, list):
        steps = data
    elif isinstance(data, dict):
        # May be wrapped under a key
        for key in ("data", "chain", "steps", "productions"):
            if key in data and isinstance(data[key], list):
                steps = data[key]
                break
        if not steps:
            # Flat dict: {output_good: {inputs: [...], ...}}
            for good, info in data.items():
                if isinstance(info, dict) and ("inputs" in info or "input" in info):
                    inp = info.get("inputs") or info.get("input") or []
                    steps.append({"produced_good": good, "inputs": inp,
                                  "produced_units": info.get("produced_units", 1)})

    if not steps:
        print(f"\n  Raw API response (no recognised shape):\n  {str(data)[:500]}")
        print("\n  Could not parse supply chain — the API may have returned an unexpected format.")
        print("  Try: python3 -c \"import universe; import json; "
              "print(json.dumps(universe.get_supply_chain(), indent=2))\" | head -80")
        sys.exit(0)

    print(f"  → {len(steps)} production step(s) found")

    # Build local knowledge
    db.init_db()
    exporters  = _local_exporters(system)
    importers  = _local_importers(system)
    mineable   = _mineable_goods(system)
    prod_wps   = _production_waypoints(system)

    locally_available = set(exporters.keys()) | mineable

    # Filter by --good
    if args.good:
        good_filter = args.good.upper()
        steps = [s for s in steps
                 if s.get("produced_good", "").upper() == good_filter
                 or any(
                     (i.get("good") or i.get("trade_symbol") or i.get("symbol") or "").upper() == good_filter
                     for i in (s.get("inputs") or [])
                 )]
        if not steps:
            print(f"\n  No production steps found for '{args.good}'")
            sys.exit(0)

    # Group by tier
    by_tier: dict[int, list[dict]] = {}
    for step in steps:
        output = step.get("produced_good", "?")
        inputs_raw = step.get("inputs") or step.get("input") or []
        # Normalise input list — each item might be {good, units} or just a string
        inputs: list[tuple[str, int]] = []
        for inp in inputs_raw:
            if isinstance(inp, str):
                inputs.append((inp, 1))
            elif isinstance(inp, dict):
                good = (inp.get("good") or inp.get("trade_symbol") or
                        inp.get("symbol") or inp.get("tradeSymbol") or "?")
                units = inp.get("units", inp.get("amount", 1))
                inputs.append((good, int(units)))

        tier = _estimate_tier([g for g, _ in inputs], output)
        by_tier.setdefault(tier, []).append({
            "output": output,
            "inputs": inputs,
            "produced_units": step.get("produced_units", step.get("producedUnits", 1)),
            "tier": tier,
        })

    # Print production map
    for tier in sorted(by_tier.keys()):
        label = TIER_LABELS.get(tier, f"Tier {tier}")
        print(f"\n{'─'*72}")
        print(f"  {label}")
        print(f"{'─'*72}")

        tier_steps = sorted(by_tier[tier], key=lambda s: s["output"])
        for step in tier_steps:
            output = step["output"]
            inputs = step["inputs"]
            out_units = step["produced_units"]

            # Check local availability of each input
            input_parts = []
            all_local = True
            for good, units in inputs:
                avail = good in locally_available
                if not avail:
                    all_local = False
                mark = "✓" if avail else "✗"
                input_parts.append(f"{mark}{good}×{units}")

            if args.complete_only and not all_local:
                continue

            status = "✅ COMPLETE" if all_local else "⚠  partial"
            out_avail = "exported" if output in exporters else ("imported" if output in importers else "—")

            # Output line
            inputs_str = "  +  ".join(input_parts)
            print(f"  {inputs_str}")
            print(f"    → {output} ×{out_units}  [{status}]  local market: {out_avail}")

            # Show where inputs are sourced
            for good, _ in inputs:
                sources = []
                if good in mineable:
                    sources.append("minable")
                if good in exporters:
                    wps = exporters[good][:2]
                    sources.append(f"exported by {', '.join(w.split('-')[-1] for w in wps)}")
                if sources:
                    print(f"       {good}: {' | '.join(sources)}")
            print()

    # Production waypoints in system
    print("=" * 72)
    print(f"  Production waypoints in {system}")
    print("─" * 72)
    if not prod_wps:
        print("  None found in local DB.")
        print("  Run: python3 refresh_db.py  to populate waypoint data")
    else:
        for wp in sorted(prod_wps, key=lambda w: w["symbol"]):
            trait_labels = ", ".join(
                f"{t} ({PRODUCTION_TRAITS.get(t, '?')})" for t in wp["traits"]
            )
            print(f"  {wp['symbol']:<25} {wp['type']:<22} ({wp['x']:4},{wp['y']:4})")
            print(f"    └─ {trait_labels}")

    # Quick summary
    print()
    print("=" * 72)
    total = sum(len(v) for v in by_tier.values())
    complete = sum(
        1 for tier_steps in by_tier.values()
        for step in tier_steps
        if all(g in locally_available for g, _ in step["inputs"])
    )
    print(f"  Summary: {total} production steps | {complete} fully local | {len(prod_wps)} production waypoints in system")
    if complete == 0:
        print("  → No complete chains locally. Use Command Frigate + jump gate to scout adjacent systems.")
    elif prod_wps:
        print("  → You have local production capability! Check complete chains above for best routes.")
    print()


if __name__ == "__main__":
    main()
