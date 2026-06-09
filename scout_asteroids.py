"""
scout_asteroids.py — Rank all asteroids in X1-KU6 by resource traits and
proximity to a fuel/market waypoint. Uses the API directly (no ship movement).

Run: python3 scout_asteroids.py
"""

import math
import universe as universe_api
import db

SYSTEM = "X1-KU6"

ASTEROID_TYPES = {"ASTEROID", "ASTEROID_FIELD", "ENGINEERED_ASTEROID"}

TRAIT_SCORES: dict[str, int] = {
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

GOOD_TRAITS = {
    "PRECIOUS_METAL_DEPOSITS", "RARE_METAL_DEPOSITS",
    "COMMON_METAL_DEPOSITS", "MINERAL_DEPOSITS", "DEEP_CRATERS",
    "HOLLOWED_INTERIOR",
}

def dist(a: tuple[int,int], b: tuple[int,int]) -> float:
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)


def main() -> None:
    print(f"Fetching all waypoints in {SYSTEM} from API…")
    waypoints = universe_api.get_waypoints(SYSTEM)
    print(f"  → {len(waypoints)} waypoints loaded")

    coords: dict[str, tuple[int,int]] = {}
    traits_map: dict[str, set[str]] = {}
    market_wps: list[str] = []
    asteroid_base_wps: list[str] = []

    for wp in waypoints:
        sym = wp["symbol"]
        coords[sym] = (wp["x"], wp["y"])
        wp_traits = {t["symbol"] for t in wp.get("traits", [])}
        traits_map[sym] = wp_traits
        if wp["type"] == "ASTEROID_BASE":
            asteroid_base_wps.append(sym)
        if "MARKETPLACE" in wp_traits:
            market_wps.append(sym)

    # Fuel sources = markets (they all sell FUEL in this game)
    fuel_sources = market_wps + asteroid_base_wps
    fuel_sources = list(set(fuel_sources))

    # Score each asteroid
    results = []
    for wp in waypoints:
        if wp["type"] not in ASTEROID_TYPES:
            continue
        sym = wp["symbol"]
        traits = traits_map.get(sym, set())
        trait_score = sum(TRAIT_SCORES.get(t, 0) for t in traits)
        if trait_score <= -9000:
            continue  # STRIPPED — skip

        ax, ay = coords[sym]

        # Distance to nearest fuel/market
        nearest_market, nearest_dist = None, float("inf")
        for mkt in fuel_sources:
            d = dist((ax, ay), coords[mkt])
            if d < nearest_dist:
                nearest_dist, nearest_market = d, mkt

        # Fuel needed in cruise mode (roughly dist/2)
        fuel_needed = nearest_dist / 2.0

        # Distance bonus/penalty on top of trait score
        if nearest_dist < 50:
            dist_bonus = 30
        elif nearest_dist < 120:
            dist_bonus = 20
        elif nearest_dist < 200:
            dist_bonus = 10
        elif nearest_dist < 300:
            dist_bonus = 0
        else:
            dist_bonus = -15

        total_score = trait_score + dist_bonus

        good_trait_list = sorted(traits & GOOD_TRAITS)
        all_trait_list = sorted(traits)

        results.append({
            "symbol": sym,
            "type": wp["type"],
            "x": ax, "y": ay,
            "trait_score": trait_score,
            "dist_bonus": dist_bonus,
            "total_score": total_score,
            "nearest_market": nearest_market,
            "nearest_dist": nearest_dist,
            "fuel_needed": fuel_needed,
            "good_traits": good_trait_list,
            "all_traits": all_trait_list,
        })

    results.sort(key=lambda r: r["total_score"], reverse=True)

    # Current asteroid from DB
    current = None
    try:
        with db._conn() as con:
            row = con.execute(
                "SELECT value FROM bot_settings WHERE key='asteroid'"
            ).fetchone()
            if row:
                current = row[0]
    except Exception:
        pass

    print()
    print("=" * 90)
    print(f"{'#':>3}  {'Symbol':<22} {'Pos':>12}  {'Dist':>6}  {'Fuel':>5}  "
          f"{'TScore':>7}  {'Total':>6}  Nearest Market")
    print("=" * 90)

    for i, r in enumerate(results[:20], 1):
        marker = " ◄ CURRENT" if r["symbol"] == current else ""
        print(
            f"{i:>3}  {r['symbol']:<22} ({r['x']:4},{r['y']:4})  "
            f"{r['nearest_dist']:6.0f}  {r['fuel_needed']:5.0f}  "
            f"{r['trait_score']:>7}  {r['total_score']:>6}  "
            f"{r['nearest_market']}{marker}"
        )
        if r["good_traits"]:
            print(f"       └─ resources: {', '.join(r['good_traits'])}")

    print()
    print("=" * 90)
    best = results[0]
    print(f"BEST ASTEROID: {best['symbol']}  (total score {best['total_score']})")
    print(f"  Position  : ({best['x']}, {best['y']})")
    print(f"  Nearest   : {best['nearest_market']}  ({best['nearest_dist']:.0f} units)")
    print(f"  Fuel/trip : ~{best['fuel_needed']:.0f}  (cruise mode)")
    print(f"  Resources : {', '.join(best['good_traits']) or 'unknown'}")
    if current and best["symbol"] != current:
        cur_row = next((r for r in results if r["symbol"] == current), None)
        if cur_row:
            print()
            print(f"CURRENT ASTEROID: {current}  (score {cur_row['total_score']}, "
                  f"dist {cur_row['nearest_dist']:.0f}, fuel ~{cur_row['fuel_needed']:.0f})")
            improvement = best["total_score"] - cur_row["total_score"]
            print(f"  Score improvement if switching: +{improvement}")
    print()
    print("To switch the bot to a new asteroid, run:")
    best_sym = best["symbol"]
    print(f'  python3 -c "import db; db.set_bot_setting(\'asteroid\', \'{best_sym}\')"')
    print("(The bot reads ASTEROID from DB at next auto_configure — or restart to apply)")


if __name__ == "__main__":
    main()
